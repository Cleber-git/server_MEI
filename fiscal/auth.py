from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

from db import get_conn, put_conn
from models import loginIn

from .config import settings
from .errors import ApiProblem
from .schemas import LogoutRequest, RefreshRequest


ALL_FISCAL_PERMISSIONS = {
    "FISCAL_CONFIG_READ",
    "FISCAL_CONFIG_WRITE",
    "FISCAL_CERTIFICATE_MANAGE",
    "FISCAL_DOCUMENT_CREATE",
    "FISCAL_DOCUMENT_READ",
    "FISCAL_DOCUMENT_CANCEL",
    "FISCAL_PRODUCTION_ENABLE",
}
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])
bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant_id: str
    session_id: UUID
    permissions: frozenset[str]


def _utcnow():
    return datetime.now(timezone.utc)


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _keys() -> dict[str, str]:
    keys = settings.signing_keys()
    if not keys or settings.jwt_active_kid not in keys:
        raise ApiProblem(
            503,
            "INTERNAL_ERROR",
            "Autenticação indisponível",
            "Chaves de assinatura não configuradas",
        )
    return keys


def _encode_access(user_id: str, tenant_id: str, session_id: UUID) -> str:
    now = _utcnow()
    payload = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": user_id,
        "tenant": tenant_id,
        "sid": str(session_id),
        "jti": str(uuid4()),
        "typ": "access",
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(seconds=settings.access_ttl),
    }
    return jwt.encode(
        payload,
        _keys()[settings.jwt_active_kid],
        algorithm="HS256",
        headers={"kid": settings.jwt_active_kid},
    )


def _password_valid(raw: str, stored: str) -> tuple[bool, bool]:
    if stored.startswith("$2"):
        return pwd.verify(raw, stored), False
    # One-time compatibility path for legacy plaintext; upgraded after success.
    return hmac.compare_digest(raw.encode(), stored.encode()), True


def _rate_key(login: str, request: Request) -> str:
    ip = request.client.host if request.client else "unknown"
    return hashlib.sha256(f"{login.strip().lower()}|{ip}".encode()).hexdigest()


def _issue_session(cur, user_id: str, tenant_id: str, request: Request):
    now, session_id, family_id = _utcnow(), uuid4(), uuid4()
    refresh = secrets.token_urlsafe(48)
    ua = hashlib.sha256((request.headers.get("user-agent") or "").encode()).hexdigest()
    ip = hashlib.sha256(
        ((request.client.host if request.client else "")[:7]).encode()
    ).hexdigest()
    cur.execute(
        """INSERT INTO auth_sessions
      (id,user_id,tenant_id,refresh_token_hash,refresh_family_id,issued_at,expires_at,last_seen_at,user_agent_hash,ip_prefix_hash)
      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            session_id,
            user_id,
            tenant_id,
            _token_hash(refresh),
            family_id,
            now,
            now + timedelta(seconds=settings.refresh_ttl),
            now,
            ua,
            ip,
        ),
    )
    return session_id, refresh


def _login_response(user, company, session_id, refresh):
    return {
        "success": True,
        "accessToken": _encode_access(user[0], user[4], session_id),
        "tokenType": "Bearer",
        "expiresIn": settings.access_ttl,
        "refreshToken": refresh,
        "refreshExpiresIn": settings.refresh_ttl,
        "sessionId": str(session_id),
        "user": {"uuid": user[0], "email": user[1], "name": user[3]},
        "company": {
            "uuid": company[0],
            "cnpj": company[1],
            "legalName": company[2],
            "tradeName": company[3],
        },
    }


@router.post("/login")
def bearer_login(data: loginIn, request: Request):
    conn = get_conn()
    try:
        cur, now, rate_key = conn.cursor(), _utcnow(), _rate_key(data.login, request)
        cur.execute(
            "SELECT attempts,window_started_at,blocked_until FROM auth_login_attempts WHERE subject_hash=%s FOR UPDATE",
            (rate_key,),
        )
        attempt = cur.fetchone()
        if attempt and attempt[2] and attempt[2] > now:
            raise ApiProblem(
                429, "RATE_LIMITED", "Muitas tentativas", "Tente novamente mais tarde"
            )
        cur.execute(
            """SELECT u.uuid,u.email,u.senhahash,u.nome,u.empresauuid,u.ativo,u.datacadastro,u.ultimologin
          FROM usuariomei u LEFT JOIN empresa e ON e.uuid=u.empresauuid
          WHERE lower(u.email)=lower(%s) OR e.cnpj=%s ORDER BY lower(u.email)=lower(%s) DESC LIMIT 1""",
            (data.login, data.login, data.login),
        )
        user = cur.fetchone()
        valid, upgrade = (
            _password_valid(data.senha, user[2]) if user and user[5] else (False, False)
        )
        if not valid:
            attempts = (
                1
                if not attempt or now - attempt[1] > timedelta(minutes=15)
                else attempt[0] + 1
            )
            blocked = now + timedelta(minutes=15) if attempts >= 5 else None
            cur.execute(
                """INSERT INTO auth_login_attempts(subject_hash,window_started_at,attempts,blocked_until) VALUES(%s,%s,%s,%s)
              ON CONFLICT(subject_hash) DO UPDATE SET window_started_at=EXCLUDED.window_started_at,attempts=EXCLUDED.attempts,blocked_until=EXCLUDED.blocked_until""",
                (rate_key, now, attempts, blocked),
            )
            conn.commit()
            raise ApiProblem(
                401,
                "AUTHENTICATION_REQUIRED",
                "Credenciais inválidas",
                "Login ou senha inválidos",
            )
        cur.execute(
            "SELECT uuid,cnpj,razaosocial,nomefantasia,ativo,bloqueado FROM empresa WHERE uuid=%s",
            (user[4],),
        )
        company = cur.fetchone()
        if not company or not company[4] or company[5]:
            raise ApiProblem(
                403, "ACCESS_DENIED", "Acesso negado", "Empresa inativa ou bloqueada"
            )
        if upgrade:
            cur.execute(
                "UPDATE usuariomei SET senhahash=%s WHERE uuid=%s",
                (pwd.hash(data.senha), user[0]),
            )
        cur.execute(
            "DELETE FROM auth_login_attempts WHERE subject_hash=%s", (rate_key,)
        )
        session_id, refresh = _issue_session(cur, user[0], user[4], request)
        conn.commit()
        return _login_response(user, company, session_id, refresh)
    except ApiProblem:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


@router.post("/refresh")
def refresh_session(data: RefreshRequest):
    conn = get_conn()
    try:
        cur, now, presented = conn.cursor(), _utcnow(), _token_hash(data.refreshToken)
        cur.execute(
            """SELECT id,user_id,tenant_id,refresh_family_id,expires_at,revoked_at
          FROM auth_sessions WHERE refresh_token_hash=%s FOR UPDATE""",
            (presented,),
        )
        row = cur.fetchone()
        if not row:
            cur.execute(
                "SELECT refresh_family_id FROM auth_sessions WHERE previous_refresh_token_hash=%s LIMIT 1",
                (presented,),
            )
            reused = cur.fetchone()
            if reused:
                cur.execute(
                    "UPDATE auth_sessions SET revoked_at=%s,revoke_reason='refresh_reuse' WHERE refresh_family_id=%s AND revoked_at IS NULL",
                    (now, reused[0]),
                )
                conn.commit()
            raise ApiProblem(
                401,
                "AUTHENTICATION_REQUIRED",
                "Sessão inválida",
                "Refresh token inválido ou reutilizado",
            )
        if row[5] or row[4] <= now:
            raise ApiProblem(
                401,
                "AUTHENTICATION_REQUIRED",
                "Sessão inválida",
                "Sessão revogada ou expirada",
            )
        cur.execute(
            """SELECT u.ativo,e.ativo,e.bloqueado FROM usuariomei u JOIN empresa e ON e.uuid=u.empresauuid
          WHERE u.uuid=%s AND u.empresauuid=%s""",
            (row[1], row[2]),
        )
        active = cur.fetchone()
        if not active or not active[0] or not active[1] or active[2]:
            raise ApiProblem(
                401,
                "AUTHENTICATION_REQUIRED",
                "Sessão inválida",
                "Vínculo de usuário ou empresa inativo",
            )
        new_refresh = secrets.token_urlsafe(48)
        cur.execute(
            "UPDATE auth_sessions SET previous_refresh_token_hash=refresh_token_hash,refresh_token_hash=%s,last_seen_at=%s WHERE id=%s",
            (_token_hash(new_refresh), now, row[0]),
        )
        conn.commit()
        return {
            "accessToken": _encode_access(row[1], row[2], row[0]),
            "tokenType": "Bearer",
            "expiresIn": settings.access_ttl,
            "refreshToken": new_refresh,
            "refreshExpiresIn": settings.refresh_ttl,
            "sessionId": str(row[0]),
        }
    except ApiProblem:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> Principal:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise ApiProblem(
            401,
            "AUTHENTICATION_REQUIRED",
            "Autenticação necessária",
            "Informe um Bearer token válido",
        )
    token = credentials.credentials
    try:
        header = jwt.get_unverified_header(token)
        key = _keys().get(header.get("kid"))
        if not key:
            raise JWTError("unknown kid")
        claims = jwt.decode(
            token,
            key,
            algorithms=["HS256"],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require_exp": True, "require_iat": True, "require_sub": True},
        )
        if claims.get("typ") != "access":
            raise JWTError("wrong token type")
        session_id = UUID(claims["sid"])
    except (JWTError, KeyError, ValueError):
        raise ApiProblem(
            401,
            "AUTHENTICATION_REQUIRED",
            "Sessão inválida",
            "Token inválido ou expirado",
        )
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT s.user_id,s.tenant_id FROM auth_sessions s JOIN usuariomei u ON u.uuid=s.user_id
          JOIN empresa e ON e.uuid=s.tenant_id WHERE s.id=%s AND s.user_id=%s AND s.tenant_id=%s
          AND s.revoked_at IS NULL AND s.expires_at>NOW() AND u.ativo=TRUE AND u.empresauuid=s.tenant_id
          AND e.ativo=TRUE AND COALESCE(e.bloqueado,FALSE)=FALSE""",
            (session_id, claims["sub"], claims["tenant"]),
        )
        session = cur.fetchone()
        if not session:
            raise ApiProblem(
                401,
                "AUTHENTICATION_REQUIRED",
                "Sessão inválida",
                "Sessão ou vínculo inativo",
            )
        cur.execute(
            "SELECT permission FROM auth_permissions WHERE user_id=%s AND tenant_id=%s",
            session,
        )
        return Principal(
            session[0],
            session[1],
            session_id,
            frozenset(row[0] for row in cur.fetchall()),
        )
    finally:
        put_conn(conn)


def require_permission(permission: str):
    def dependency(principal: Principal = Depends(current_principal)):
        if permission not in principal.permissions:
            raise ApiProblem(
                403, "ACCESS_DENIED", "Acesso negado", "Permissão fiscal insuficiente"
            )
        return principal

    return dependency


@router.post("/logout", status_code=204)
def logout(data: LogoutRequest, principal: Principal = Depends(current_principal)):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE auth_sessions SET revoked_at=NOW(),revoke_reason='logout' WHERE id=%s AND user_id=%s AND tenant_id=%s AND revoked_at IS NULL",
            (principal.session_id, principal.user_id, principal.tenant_id),
        )
        conn.commit()
    finally:
        put_conn(conn)

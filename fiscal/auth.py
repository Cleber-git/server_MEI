"""Auth Bearer do contrato fiscal v1 (seção 3): login, refresh, logout e me.

- Access token JWT HS256 com ``kid`` (``JWT_SIGNING_KEYS``), 15 min.
- Refresh token opaco, rotativo, guardado só como SHA-256. Reapresentar um
  refresh já usado revoga a família inteira (``REFRESH_TOKEN_REUSED``).
- A sessão tem validade absoluta (``JWT_REFRESH_TTL_SECONDS`` a partir do login):
  o refresh não a estende.
- O tenant vem só do token, nunca de header ou corpo.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import ExpiredSignatureError, JWTError, jwt

from db import get_conn, put_conn

from .config import settings
from .errors import ApiProblem
from .passwords import burn_verification, check_and_upgrade
from .schemas import LoginRequest, LogoutRequest, RefreshRequest

ALL_FISCAL_PERMISSIONS = {
    "FISCAL_CONFIG_READ",
    "FISCAL_CONFIG_WRITE",
    "FISCAL_CERTIFICATE_MANAGE",
    "FISCAL_DOCUMENT_CREATE",
    "FISCAL_DOCUMENT_READ",
    "FISCAL_DOCUMENT_CANCEL",
    "FISCAL_PRODUCTION_ENABLE",
}
# Seção 3.4: o dono recebe todas menos FISCAL_PRODUCTION_ENABLE (só operador).
OWNER_DEFAULT_PERMISSIONS = tuple(
    sorted(ALL_FISCAL_PERMISSIONS - {"FISCAL_PRODUCTION_ENABLE"})
)
# Mesma regra da migration 002, para empresas criadas depois dela.
OWNER_DEFAULTS_SQL = """
WITH owner AS (
    SELECT u.uuid AS user_id, u.empresauuid AS tenant_id
    FROM usuariomei u JOIN empresa e ON e.uuid = u.empresauuid
    WHERE u.empresauuid = %(tenant)s
    ORDER BY (u.ativo IS TRUE) DESC, u.datacadastro ASC NULLS LAST, u.uuid
    LIMIT 1
), applied AS (
    INSERT INTO auth_permission_defaults (tenant_id, owner_user_id)
    SELECT tenant_id, user_id FROM owner
    ON CONFLICT (tenant_id) DO NOTHING
    RETURNING tenant_id, owner_user_id
)
INSERT INTO auth_permissions (user_id, tenant_id, permission)
SELECT a.owner_user_id, a.tenant_id, p.permission
FROM applied a CROSS JOIN unnest(%(perms)s::text[]) AS p(permission)
ON CONFLICT (user_id, tenant_id, permission) DO NOTHING
"""
FISCAL_PREFIX = "/api/v1/fiscal"
_MAX_FAILED_LOGINS = 5
_LOGIN_WINDOW = timedelta(minutes=15)
_WWW_AUTH = {"WWW-Authenticate": "Bearer"}

router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])
bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant_id: str
    session_id: UUID
    permissions: frozenset[str]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _unauthorized(code: str, message: str) -> ApiProblem:
    return ApiProblem(401, code, message, headers=_WWW_AUTH)


def _keys() -> dict[str, str]:
    keys = settings.signing_keys()
    if not keys or settings.jwt_active_kid not in keys:
        raise ApiProblem(
            503, "INTERNAL_ERROR", "Autenticação indisponível no momento."
        )
    return keys


def _encode_access(user_id: str, tenant_id: str, session_id: UUID, now: datetime):
    expires_at = now + timedelta(seconds=settings.access_ttl)
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
        "exp": expires_at,
    }
    token = jwt.encode(
        payload,
        _keys()[settings.jwt_active_kid],
        algorithm="HS256",
        headers={"kid": settings.jwt_active_kid},
    )
    return token, expires_at


def _rate_key(login: str, request: Request) -> str:
    ip = request.client.host if request.client else "unknown"
    return _sha256(f"{login.strip().lower()}|{ip}")


def _account(cur, user_id: str, tenant_id: str):
    """Usuário + empresa do vínculo, ou ``None``."""
    cur.execute(
        """SELECT u.uuid,u.email,u.nome,u.empresauuid,COALESCE(u.ativo,FALSE),
                  e.uuid,e.cnpj,e.razaosocial,e.nomefantasia,COALESCE(e.ativo,FALSE),
                  COALESCE(e.bloqueado,FALSE)
           FROM usuariomei u JOIN empresa e ON e.uuid=u.empresauuid
           WHERE u.uuid=%s AND u.empresauuid=%s""",
        (user_id, tenant_id),
    )
    return cur.fetchone()


def _account_allowed(account) -> bool:
    return bool(account and account[4] and account[9] and not account[10])


def _account_json(account) -> dict:
    return {
        "user": {
            "id": account[0],
            "email": account[1],
            "name": account[2],
            "companyId": account[3],
            "active": bool(account[4]),
        },
        "company": {
            "id": account[5],
            "cnpj": account[6],
            "legalName": account[7],
            "tradeName": account[8],
            "active": bool(account[9]),
            "blocked": bool(account[10]),
        },
    }


def _session_body(account, session_id: UUID, refresh: str, session_expires_at, now):
    access, access_expires_at = _encode_access(account[0], account[5], session_id, now)
    return {
        "accessToken": access,
        "tokenType": "Bearer",
        "expiresIn": settings.access_ttl,
        "expiresAt": _iso(access_expires_at),
        "refreshToken": refresh,
        "refreshExpiresIn": max(0, int((session_expires_at - now).total_seconds())),
        "sessionId": str(session_id),
        **_account_json(account),
    }


def _issue_session(cur, user_id: str, tenant_id: str, request: Request, device_id, now):
    session_id, family_id = uuid4(), uuid4()
    refresh = secrets.token_urlsafe(48)
    expires_at = now + timedelta(seconds=settings.refresh_ttl)
    ua = _sha256(request.headers.get("user-agent") or "")
    ip = _sha256((request.client.host if request.client else "")[:7])
    cur.execute(
        """INSERT INTO auth_sessions
      (id,user_id,tenant_id,refresh_token_hash,refresh_family_id,issued_at,expires_at,
       last_seen_at,user_agent_hash,ip_prefix_hash,device_id_hash)
      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            session_id,
            user_id,
            tenant_id,
            _sha256(refresh),
            family_id,
            now,
            expires_at,
            now,
            ua,
            ip,
            _sha256(device_id) if device_id else None,
        ),
    )
    return session_id, refresh, expires_at


def ensure_owner_defaults(cur, tenant_id: str) -> None:
    """Concede as permissões padrão ao dono uma única vez por empresa."""
    cur.execute(
        OWNER_DEFAULTS_SQL,
        {"tenant": tenant_id, "perms": list(OWNER_DEFAULT_PERMISSIONS)},
    )


def _register_failure(cur, rate_key: str, attempt, now) -> None:
    attempts = 1 if not attempt or now - attempt[1] > _LOGIN_WINDOW else attempt[0] + 1
    blocked = now + _LOGIN_WINDOW if attempts >= _MAX_FAILED_LOGINS else None
    cur.execute(
        """INSERT INTO auth_login_attempts(subject_hash,window_started_at,attempts,blocked_until)
           VALUES(%s,%s,%s,%s)
           ON CONFLICT(subject_hash) DO UPDATE SET window_started_at=EXCLUDED.window_started_at,
             attempts=EXCLUDED.attempts,blocked_until=EXCLUDED.blocked_until""",
        (
            rate_key,
            attempt[1] if attempt and attempts > 1 else now,
            attempts,
            blocked,
        ),
    )


@router.post("/login")
def bearer_login(data: LoginRequest, request: Request):
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
                429,
                "RATE_LIMITED",
                "Muitas tentativas. Tente novamente mais tarde.",
                retryable=True,
                headers={
                    "Retry-After": str(max(1, int((attempt[2] - now).total_seconds())))
                },
            )
        cur.execute(
            """SELECT u.uuid,u.senhahash,u.empresauuid
          FROM usuariomei u LEFT JOIN empresa e ON e.uuid=u.empresauuid
          WHERE lower(u.email)=lower(%s) OR e.cnpj=%s
          ORDER BY lower(u.email)=lower(%s) DESC, u.uuid LIMIT 1""",
            (data.login, data.login, data.login),
        )
        user = cur.fetchone()
        if user:
            valid = check_and_upgrade(cur, user[0], data.password, user[1])
        else:
            burn_verification(data.password)
            valid = False
        if not valid:
            _register_failure(cur, rate_key, attempt, now)
            conn.commit()
            raise _unauthorized("INVALID_CREDENTIALS", "Login ou senha inválidos.")
        account = _account(cur, user[0], user[2]) if user[2] else None
        if not _account_allowed(account):
            # Senha certa: a falha é do vínculo. Não conta como tentativa errada.
            conn.rollback()
            raise ApiProblem(
                403, "ACCOUNT_BLOCKED", "Usuário ou empresa inativos ou bloqueados."
            )
        cur.execute("DELETE FROM auth_login_attempts WHERE subject_hash=%s", (rate_key,))
        ensure_owner_defaults(cur, account[5])
        session_id, refresh, expires_at = _issue_session(
            cur, account[0], account[5], request, data.deviceId, now
        )
        body = _session_body(account, session_id, refresh, expires_at, now)
        conn.commit()
        return body
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


@router.post("/refresh")
def refresh_session(data: RefreshRequest):
    conn = get_conn()
    try:
        cur, now, presented = conn.cursor(), _utcnow(), _sha256(data.refreshToken)
        cur.execute(
            """SELECT id,user_id,tenant_id,refresh_family_id,expires_at,revoked_at
          FROM auth_sessions WHERE refresh_token_hash=%s FOR UPDATE""",
            (presented,),
        )
        row = cur.fetchone()
        if not row:
            cur.execute(
                "SELECT refresh_family_id FROM auth_refresh_token_uses WHERE token_hash=%s",
                (presented,),
            )
            reused = cur.fetchone()
            if reused:
                cur.execute(
                    """UPDATE auth_sessions SET revoked_at=%s,revoke_reason='refresh_reuse'
                       WHERE refresh_family_id=%s AND revoked_at IS NULL""",
                    (now, reused[0]),
                )
                conn.commit()
                raise _unauthorized(
                    "REFRESH_TOKEN_REUSED", "Sessão encerrada por segurança. Entre de novo."
                )
            raise _unauthorized("INVALID_TOKEN", "Sessão inválida. Entre de novo.")
        if row[5] or row[4] <= now:
            raise _unauthorized("INVALID_TOKEN", "Sessão encerrada. Entre de novo.")
        account = _account(cur, row[1], row[2])
        if not _account_allowed(account):
            raise ApiProblem(
                403, "ACCOUNT_BLOCKED", "Usuário ou empresa inativos ou bloqueados."
            )
        new_refresh = secrets.token_urlsafe(48)
        cur.execute(
            """INSERT INTO auth_refresh_token_uses(token_hash,session_id,refresh_family_id,used_at)
               VALUES(%s,%s,%s,%s) ON CONFLICT (token_hash) DO NOTHING""",
            (presented, row[0], row[3], now),
        )
        cur.execute(
            """UPDATE auth_sessions SET previous_refresh_token_hash=refresh_token_hash,
               refresh_token_hash=%s,last_seen_at=%s WHERE id=%s""",
            (_sha256(new_refresh), now, row[0]),
        )
        body = _session_body(account, row[0], new_refresh, row[4], now)
        conn.commit()
        return body
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def current_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> Principal:
    if not credentials or credentials.scheme.lower() != "bearer":
        if request.url.path.startswith(FISCAL_PREFIX) and request.headers.get(
            "validation-uuid"
        ):
            raise _unauthorized(
                "FISCAL_AUTH_UPGRADE_REQUIRED",
                "Entre de novo no app para usar a nota fiscal.",
            )
        raise _unauthorized("AUTHENTICATION_REQUIRED", "Autenticação necessária.")
    token = credentials.credentials
    keys = _keys()
    try:
        header = jwt.get_unverified_header(token)
        key = keys.get(header.get("kid"))
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
        user_id, tenant_id = str(claims["sub"]), str(claims["tenant"])
    except ExpiredSignatureError:
        # A assinatura já foi conferida antes da expiração (python-jose).
        raise _unauthorized("TOKEN_EXPIRED", "Sessão expirada.")
    except (JWTError, KeyError, ValueError, TypeError):
        raise _unauthorized("INVALID_TOKEN", "Sessão inválida. Entre de novo.")
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT 1 FROM auth_sessions WHERE id=%s AND user_id=%s AND tenant_id=%s
               AND revoked_at IS NULL AND expires_at>NOW()""",
            (session_id, user_id, tenant_id),
        )
        if not cur.fetchone():
            raise _unauthorized("INVALID_TOKEN", "Sessão inválida. Entre de novo.")
        if not _account_allowed(_account(cur, user_id, tenant_id)):
            raise ApiProblem(
                403, "ACCOUNT_BLOCKED", "Usuário ou empresa inativos ou bloqueados."
            )
        cur.execute(
            "SELECT permission FROM auth_permissions WHERE user_id=%s AND tenant_id=%s",
            (user_id, tenant_id),
        )
        permissions = frozenset(row[0] for row in cur.fetchall())
        conn.rollback()  # só leitura: não deixa a conexão do pool em transação
        return Principal(user_id, tenant_id, session_id, permissions)
    finally:
        put_conn(conn)


def require_permission(permission: str):
    def dependency(principal: Principal = Depends(current_principal)):
        if permission not in principal.permissions:
            raise ApiProblem(
                403, "PERMISSION_DENIED", "Você não tem permissão para esta ação."
            )
        return principal

    return dependency


@router.get("/me")
def me(principal: Principal = Depends(current_principal)):
    conn = get_conn()
    try:
        cur = conn.cursor()
        account = _account(cur, principal.user_id, principal.tenant_id)
        conn.rollback()
        if not account:  # o vínculo sumiu entre a validação e esta leitura
            raise _unauthorized("INVALID_TOKEN", "Sessão inválida. Entre de novo.")
        return {
            **_account_json(account),
            "permissions": sorted(principal.permissions),
        }
    finally:
        put_conn(conn)


@router.post("/logout", status_code=204)
def logout(
    data: LogoutRequest | None = Body(default=None),
    principal: Principal = Depends(current_principal),
):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE auth_sessions SET revoked_at=NOW(),revoke_reason='logout'
               WHERE user_id=%s AND tenant_id=%s AND revoked_at IS NULL
               AND (id=%s OR refresh_token_hash=%s)""",
            (
                principal.user_id,
                principal.tenant_id,
                principal.session_id,
                _sha256(data.refreshToken) if data and data.refreshToken else None,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)

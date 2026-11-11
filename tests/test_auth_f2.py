"""F2 — auth Bearer definitivo (contrato seção 3) com Postgres local.

Login, refresh com rotação e detecção de reuso, expiração, logout, ``/me``,
permissões padrão do dono (migration 002), upgrade exigido para rota fiscal só
com ``validation-uuid``, isolamento A/B em todas as rotas fiscais, rate limit e
a checagem de startup das chaves JWT.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from jose import jwt

import fiscal.auth as fiscal_auth
import migrate
from fiscal.config import Settings, assert_startup_ready
from fiscal.passwords import BCRYPT_MAX_BYTES, check_and_upgrade, hash_password
from fiscal_support import (
    CNPJ_A,
    ApiClient,
    base_app,
    bearer,
    create_legacy,
    error_of,
    insert_tenant,
    insert_user,
    login,
    seed_two_tenants,
)

OWNER_PERMS = [
    "FISCAL_CERTIFICATE_MANAGE",
    "FISCAL_CONFIG_READ",
    "FISCAL_CONFIG_WRITE",
    "FISCAL_DOCUMENT_CANCEL",
    "FISCAL_DOCUMENT_CREATE",
    "FISCAL_DOCUMENT_READ",
]
LOGIN_KEYS = {
    "accessToken",
    "tokenType",
    "expiresIn",
    "expiresAt",
    "refreshToken",
    "refreshExpiresIn",
    "sessionId",
    "user",
    "company",
}


@pytest.fixture
def client():
    return ApiClient(base_app())


@pytest.fixture
def db(pg_conn, monkeypatch):
    return seed_two_tenants(pg_conn, monkeypatch)


def _refresh(client, token):
    return client.post("/api/v1/auth/refresh", json={"refreshToken": token})


def _me(client, body):
    return client.get("/api/v1/auth/me", headers=bearer(body))


def _scalar(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    value = cur.fetchone()[0]
    conn.commit()
    return value


def _forge(claims_override=None, *, key=None, kid="k1"):
    now = datetime.now(timezone.utc)
    claims = {
        "iss": fiscal_auth.settings.jwt_issuer,
        "aud": fiscal_auth.settings.jwt_audience,
        "sub": "user-tenant-a",
        "tenant": "tenant-a",
        "sid": str(uuid4()),
        "jti": str(uuid4()),
        "typ": "access",
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=5),
        **(claims_override or {}),
    }
    secret = key or fiscal_auth.settings.signing_keys()["k1"]
    return jwt.encode(claims, secret, algorithm="HS256", headers={"kid": kid})


# --- Login --------------------------------------------------------------------


def test_login_body_matches_contract(db, client):
    before = datetime.now(timezone.utc).replace(microsecond=0)
    body = login(client, "tenant-a", deviceId="instalacao-1")
    assert set(body) == LOGIN_KEYS
    assert body["tokenType"] == "Bearer" and body["expiresIn"] == 900
    assert body["refreshExpiresIn"] == 2592000 and len(body["refreshToken"]) >= 43
    expires_at = datetime.strptime(body["expiresAt"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    assert before + timedelta(seconds=900) <= expires_at <= before + timedelta(seconds=905)
    claims = jwt.get_unverified_claims(body["accessToken"])
    assert claims["exp"] == int(expires_at.timestamp())
    assert claims["sid"] == body["sessionId"] and claims["tenant"] == "tenant-a"
    assert body["user"] == {
        "id": "user-tenant-a",
        "email": "tenant-a@example.com",
        "name": "Dono",
        "companyId": "tenant-a",
        "active": True,
    }
    assert body["company"] == {
        "id": "tenant-a",
        "cnpj": CNPJ_A,
        "legalName": "Empresa tenant-a",
        "tradeName": "Fantasia tenant-a",
        "active": True,
        "blocked": False,
    }
    stored = _scalar(
        db, "SELECT device_id_hash FROM auth_sessions WHERE id=%s", (body["sessionId"],)
    )
    assert stored and "instalacao-1" not in stored


def test_login_accepts_cnpj_and_case_insensitive_email(db, client):
    for value in (CNPJ_A, "TENANT-A@Example.com"):
        response = client.post(
            "/api/v1/auth/login", json={"login": value, "password": "senha-tenant-a"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["company"]["id"] == "tenant-a"


def test_wrong_password_and_unknown_user_look_the_same(db, client):
    wrong = client.post(
        "/api/v1/auth/login", json={"login": "tenant-a@example.com", "password": "x"}
    )
    unknown = client.post(
        "/api/v1/auth/login", json={"login": "ninguem@example.com", "password": "x"}
    )
    for response in (wrong, unknown):
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert error_of(response)["code"] == "INVALID_CREDENTIALS"
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]


def test_login_rejects_unknown_fields(db, client):
    response = client.post(
        "/api/v1/auth/login",
        json={"login": "tenant-a@example.com", "password": "senha-tenant-a", "companyId": "tenant-b"},
    )
    assert response.status_code == 422
    assert error_of(response)["fieldErrors"] == [{"path": "companyId", "code": "UNKNOWN_FIELD"}]


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE empresa SET bloqueado=TRUE WHERE uuid='tenant-a'",
        "UPDATE empresa SET ativo=FALSE WHERE uuid='tenant-a'",
        "UPDATE usuariomei SET ativo=FALSE WHERE uuid='user-tenant-a'",
    ],
)
def test_blocked_account_is_403_only_with_right_password(db, client, sql):
    db.cursor().execute(sql)
    db.commit()
    right = client.post(
        "/api/v1/auth/login",
        json={"login": "tenant-a@example.com", "password": "senha-tenant-a"},
    )
    assert right.status_code == 403 and error_of(right)["code"] == "ACCOUNT_BLOCKED"
    wrong = client.post(
        "/api/v1/auth/login", json={"login": "tenant-a@example.com", "password": "x"}
    )
    assert wrong.status_code == 401
    assert _scalar(db, "SELECT count(*) FROM auth_sessions") == 0


def test_login_rate_limit_and_success_resets_counter(db, client):
    bad = {"login": "tenant-a@example.com", "password": "x"}
    for _ in range(4):
        assert client.post("/api/v1/auth/login", json=bad).status_code == 401
    login(client, "tenant-a")  # sucesso zera o contador
    for _ in range(5):
        assert client.post("/api/v1/auth/login", json=bad).status_code == 401
    blocked = client.post(
        "/api/v1/auth/login",
        json={"login": "tenant-a@example.com", "password": "senha-tenant-a"},
    )
    assert blocked.status_code == 429  # nem a senha certa passa durante o bloqueio
    assert error_of(blocked)["code"] == "RATE_LIMITED"
    assert 0 < int(blocked.headers["Retry-After"]) <= 900
    other = login(client, "tenant-b")  # o bloqueio é por login, não global
    assert other["company"]["id"] == "tenant-b"


# --- Senha legada --------------------------------------------------------------


def test_bcrypt_password_is_accepted_and_kept(db, client):
    hashed = hash_password("senha-tenant-a")
    db.cursor().execute(
        "UPDATE usuariomei SET senhahash=%s WHERE uuid='user-tenant-a'", (hashed,)
    )
    db.commit()
    login(client, "tenant-a")
    assert _scalar(db, "SELECT senhahash FROM usuariomei WHERE uuid='user-tenant-a'") == hashed


def test_plaintext_longer_than_bcrypt_limit_is_not_hashed(pg_conn):
    create_legacy(pg_conn)
    long_password = "a" * (BCRYPT_MAX_BYTES + 1)
    insert_tenant(pg_conn, "tenant-a", CNPJ_A, password=long_password)
    cur = pg_conn.cursor()
    assert check_and_upgrade(cur, "user-tenant-a", long_password, long_password)
    assert not check_and_upgrade(cur, "user-tenant-a", "a" * BCRYPT_MAX_BYTES, long_password)
    pg_conn.commit()
    assert _scalar(pg_conn, "SELECT senhahash FROM usuariomei") == long_password


def test_upgrade_does_not_overwrite_concurrent_reset(pg_conn):
    create_legacy(pg_conn)
    insert_tenant(pg_conn, "tenant-a", CNPJ_A, password="antiga")
    cur = pg_conn.cursor()
    cur.execute("UPDATE usuariomei SET senhahash='nova-da-redefinicao'")
    # O login leu "antiga" antes da redefinição: o CAS não grava o hash dela.
    assert check_and_upgrade(cur, "user-tenant-a", "antiga", "antiga")
    pg_conn.commit()
    assert _scalar(pg_conn, "SELECT senhahash FROM usuariomei") == "nova-da-redefinicao"


def test_legacy_login_route_accepts_both_formats_and_hides_hash(db, monkeypatch):
    main = pytest.importorskip("main")
    monkeypatch.setattr(main, "get_conn", lambda: db)
    monkeypatch.setattr(main, "put_conn", lambda _conn: None)
    # Colunas de empresa que só o /login legado lê (DDL de main.py).
    db.cursor().execute(
        """ALTER TABLE empresa ADD COLUMN motivobloqueio text, ADD COLUMN plano text,
           ADD COLUMN statusassinatura text, ADD COLUMN datainicioassinatura bigint,
           ADD COLUMN datafimassinatura bigint, ADD COLUMN origemassinatura text,
           ADD COLUMN datacadastro bigint, ADD COLUMN dataatualizacao bigint,
           ADD COLUMN sincronizado boolean DEFAULT TRUE"""
    )
    db.commit()
    legacy = ApiClient(main.app)
    payload = {"login": "tenant-a@example.com", "senha": "senha-tenant-a"}
    first = legacy.post("/login", json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["usuario"]["senhaHash"] == ""
    stored = _scalar(db, "SELECT senhahash FROM usuariomei WHERE uuid='user-tenant-a'")
    assert stored.startswith("$2")
    second = legacy.post("/login", json=payload)  # já em bcrypt
    assert second.status_code == 200 and second.json()["empresa"]["uuid"] == "tenant-a"
    wrong = legacy.post("/login", json={**payload, "senha": "x"})
    assert wrong.status_code == 402  # código legado preservado para o app


# --- Refresh ------------------------------------------------------------------


def test_refresh_rotates_and_keeps_absolute_expiry(db, client):
    first = login(client, "tenant-a")
    db.cursor().execute(
        "UPDATE auth_sessions SET expires_at=NOW()+INTERVAL '1 hour' WHERE id=%s",
        (first["sessionId"],),
    )
    db.commit()
    second = _refresh(client, first["refreshToken"])
    assert second.status_code == 200, second.text
    body = second.json()
    assert set(body) == LOGIN_KEYS
    assert body["sessionId"] == first["sessionId"]
    assert body["refreshToken"] != first["refreshToken"]
    assert 3500 <= body["refreshExpiresIn"] <= 3600  # não estende a sessão
    assert _me(client, body).status_code == 200


def test_reusing_old_refresh_revokes_whole_family(db, client):
    first = login(client, "tenant-a")
    second = _refresh(client, first["refreshToken"]).json()
    third = _refresh(client, second["refreshToken"]).json()
    # Reuso de um refresh de duas rotações atrás também é detectado.
    reused = _refresh(client, first["refreshToken"])
    assert reused.status_code == 401
    assert error_of(reused)["code"] == "REFRESH_TOKEN_REUSED"
    current = _refresh(client, third["refreshToken"])
    assert error_of(current)["code"] == "INVALID_TOKEN"
    assert error_of(_me(client, third))["code"] == "INVALID_TOKEN"
    reason = _scalar(
        db, "SELECT revoke_reason FROM auth_sessions WHERE id=%s", (first["sessionId"],)
    )
    assert reason == "refresh_reuse"
    # Outra sessão do mesmo usuário (outra família) continua valendo.
    assert _me(client, login(client, "tenant-a")).status_code == 200


def test_unknown_or_expired_refresh_is_invalid_token(db, client):
    unknown = _refresh(client, "z" * 64)
    assert unknown.status_code == 401 and error_of(unknown)["code"] == "INVALID_TOKEN"
    body = login(client, "tenant-a")
    db.cursor().execute(
        "UPDATE auth_sessions SET expires_at=NOW()-INTERVAL '1 second' WHERE id=%s",
        (body["sessionId"],),
    )
    db.commit()
    expired = _refresh(client, body["refreshToken"])
    assert expired.status_code == 401 and error_of(expired)["code"] == "INVALID_TOKEN"


def test_refresh_of_blocked_company_is_403(db, client):
    body = login(client, "tenant-a")
    db.cursor().execute("UPDATE empresa SET bloqueado=TRUE WHERE uuid='tenant-a'")
    db.commit()
    response = _refresh(client, body["refreshToken"])
    assert response.status_code == 403 and error_of(response)["code"] == "ACCOUNT_BLOCKED"
    me = _me(client, body)
    assert me.status_code == 403 and error_of(me)["code"] == "ACCOUNT_BLOCKED"


# --- Access token ---------------------------------------------------------------


def test_expired_access_token_is_token_expired(db, client):
    body = login(client, "tenant-a")
    past = datetime.now(timezone.utc) - timedelta(minutes=30)
    token = _forge(
        {"sid": body["sessionId"], "iat": past, "nbf": past, "exp": past + timedelta(minutes=15)}
    )
    response = client.get("/api/v1/fiscal/config", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert error_of(response)["code"] == "TOKEN_EXPIRED"


@pytest.mark.parametrize(
    "token_kwargs",
    [
        {"key": "y" * 40},  # assinatura com outra chave
        {"kid": "desconhecido"},
        {"claims_override": {"typ": "refresh"}},
        {"claims_override": {"aud": "outro-app"}},
        {"claims_override": {"iss": "https://outro"}},
        {"claims_override": {"sid": "nao-uuid"}},
    ],
)
def test_tampered_access_token_is_invalid_token(db, client, token_kwargs):
    body = login(client, "tenant-a")
    claims = {"sid": body["sessionId"], **token_kwargs.pop("claims_override", {})}
    token = _forge(claims, **token_kwargs)
    response = client.get("/api/v1/fiscal/config", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 401
    assert error_of(response)["code"] == "INVALID_TOKEN"


def test_token_cannot_switch_tenant(db, client):
    body = login(client, "tenant-a")
    token = _forge({"sid": body["sessionId"], "tenant": "tenant-b"})
    response = client.get("/api/v1/fiscal/config", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 401
    assert error_of(response)["code"] == "INVALID_TOKEN"


def test_logout_revokes_session(db, client):
    body = login(client, "tenant-a")
    other = login(client, "tenant-a")
    response = client.post("/api/v1/auth/logout", headers=bearer(body))
    assert response.status_code == 204
    assert error_of(_me(client, body))["code"] == "INVALID_TOKEN"
    assert error_of(_refresh(client, body["refreshToken"]))["code"] == "INVALID_TOKEN"
    assert _me(client, other).status_code == 200  # só a sessão atual


def test_logout_with_refresh_token_also_revokes_that_session(db, client):
    body = login(client, "tenant-a")
    other = login(client, "tenant-a")
    response = client.post(
        "/api/v1/auth/logout",
        headers=bearer(body),
        json={"refreshToken": other["refreshToken"]},
    )
    assert response.status_code == 204
    assert error_of(_me(client, other))["code"] == "INVALID_TOKEN"
    # O refresh de outra empresa não é revogado por este usuário.
    b = login(client, "tenant-b")
    client.post(
        "/api/v1/auth/logout",
        headers=bearer(login(client, "tenant-a")),
        json={"refreshToken": b["refreshToken"]},
    )
    assert _me(client, b).status_code == 200


def test_me_returns_account_and_permissions(db, client):
    body = login(client, "tenant-a")
    response = _me(client, body)
    assert response.status_code == 200
    me = response.json()
    assert me["user"] == body["user"] and me["company"] == body["company"]
    assert me["permissions"] == OWNER_PERMS


# --- Bearer x validation-uuid ---------------------------------------------------


def test_fiscal_route_with_only_validation_uuid_requires_upgrade(db, client):
    response = client.get("/api/v1/fiscal/config", headers={"validation-uuid": "tenant-a"})
    assert response.status_code == 401
    assert error_of(response)["code"] == "FISCAL_AUTH_UPGRADE_REQUIRED"
    none = client.get("/api/v1/fiscal/config")
    assert error_of(none)["code"] == "AUTHENTICATION_REQUIRED"
    me = client.get("/api/v1/auth/me", headers={"validation-uuid": "tenant-a"})
    assert error_of(me)["code"] == "AUTHENTICATION_REQUIRED"


def test_validation_uuid_never_changes_tenant(db, client):
    headers = {**bearer(login(client, "tenant-a")), "validation-uuid": "tenant-b"}
    response = client.get("/api/v1/auth/me", headers=headers)
    assert response.json()["company"]["id"] == "tenant-a"


# --- Permissões padrão (migration 002) -----------------------------------------


def test_migration_grants_owner_defaults_only_to_first_user(pg_conn, monkeypatch):
    create_legacy(pg_conn)
    insert_tenant(pg_conn, "tenant-a", CNPJ_A, datacadastro=5)
    insert_user(pg_conn, "tenant-a", "user2-tenant-a", datacadastro=9)
    insert_user(pg_conn, "tenant-a", "user0-tenant-a", datacadastro=1, ativo=False)
    migrate.migrate(pg_conn)
    cur = pg_conn.cursor()
    cur.execute("SELECT user_id,permission FROM auth_permissions ORDER BY 2")
    rows = cur.fetchall()
    # Ativo vem antes de inativo; entre os ativos, o mais antigo.
    assert rows == [("user-tenant-a", p) for p in OWNER_PERMS]
    cur.execute("SELECT owner_user_id FROM auth_permission_defaults")
    assert cur.fetchall() == [("user-tenant-a",)]
    pg_conn.commit()


def test_company_created_after_migration_gets_defaults_on_login(db, client):
    insert_tenant(db, "tenant-c", "19131243000197")
    assert _scalar(db, "SELECT count(*) FROM auth_permissions WHERE tenant_id='tenant-c'") == 0
    assert _me(client, login(client, "tenant-c")).json()["permissions"] == OWNER_PERMS


def test_revoked_default_grant_is_not_given_back(db, client):
    db.cursor().execute(
        "DELETE FROM auth_permissions WHERE tenant_id='tenant-a' AND permission='FISCAL_DOCUMENT_CANCEL'"
    )
    db.commit()
    permissions = _me(client, login(client, "tenant-a")).json()["permissions"]
    assert "FISCAL_DOCUMENT_CANCEL" not in permissions
    assert "FISCAL_PRODUCTION_ENABLE" not in permissions


def test_non_owner_user_has_no_fiscal_permission(db, client):
    insert_user(db, "tenant-a", "user9-tenant-a", datacadastro=99)
    response = client.post(
        "/api/v1/auth/login",
        json={"login": "user9-tenant-a@example.com", "password": "senha-tenant-a"},
    )
    assert response.status_code == 200
    me = _me(client, response.json()).json()
    assert me["permissions"] == []
    denied = client.get("/api/v1/fiscal/config", headers=bearer(response.json()))
    assert denied.status_code == 403 and error_of(denied)["code"] == "PERMISSION_DENIED"


# --- Isolamento A/B em todas as rotas fiscais ------------------------------------

ID_ROUTES = [
    ("GET", "/api/v1/fiscal/documents/{id}", None),
    ("GET", "/api/v1/fiscal/documents/{id}/xml", None),
    ("GET", "/api/v1/fiscal/documents/{id}/auxiliary-document", None),
    ("POST", "/api/v1/fiscal/documents/{id}/cancel", {"reason": "Cancelamento de teste A/B"}),
    ("POST", "/api/v1/fiscal/documents/{id}/retry", None),
    ("DELETE", "/api/v1/fiscal/certificates/{id}", None),
]


@pytest.fixture
def tenant_b_resources(db):
    cur = db.cursor()
    cur.execute("SELECT id FROM fiscal_establishments WHERE tenant_id='tenant-b'")
    est_b = cur.fetchone()[0]
    doc_b, cert_b = uuid4(), uuid4()
    cur.execute(
        """INSERT INTO fiscal_documents (id,tenant_id,establishment_id,idempotency_key,
           canonical_request_hash,schema_version,document_type,environment,status,correlation_id)
           VALUES (%s,'tenant-b',%s,'k-b',%s,'1.2','NFSE','RESTRICTED_PRODUCTION','QUEUED',%s)""",
        (str(doc_b), est_b, "0" * 64, str(uuid4())),
    )
    cur.execute(
        """INSERT INTO fiscal_certificates (id,tenant_id,establishment_id,vault_reference,fingerprint,
           serial_number,holder_masked,issuer,valid_from,expires_at,type,status)
           VALUES (%s,'tenant-b',%s,'ref-b',%s,'1','***','AC teste',NOW(),NOW()+INTERVAL '1 year','A1','ACTIVE')""",
        (str(cert_b), est_b, "f" * 64),
    )
    db.commit()
    return {"document": str(doc_b), "certificate": str(cert_b)}


@pytest.mark.parametrize(("method", "route", "body"), ID_ROUTES)
def test_other_tenant_resource_answers_like_missing(
    db, client, tenant_b_resources, method, route, body
):
    kind = "certificate" if "certificates" in route else "document"
    headers = {
        **bearer(login(client, "tenant-a")),
        "Idempotency-Key": str(uuid4()),
        "X-Confirm-Revocation": "REVOKE",
    }
    kwargs = {"headers": headers, **({"json": body} if body else {})}
    other = client.request(method, route.format(id=tenant_b_resources[kind]), **kwargs)
    missing = client.request(method, route.format(id=uuid4()), **kwargs)
    assert other.status_code == missing.status_code
    assert other.status_code in (404, 409)
    assert error_of(other)["code"] == error_of(missing)["code"]
    assert other.json()["error"]["message"] == missing.json()["error"]["message"]
    # Nada de B mudou.
    assert _scalar(db, "SELECT status FROM fiscal_documents WHERE id=%s", (tenant_b_resources["document"],)) == "QUEUED"
    assert _scalar(db, "SELECT status FROM fiscal_certificates WHERE id=%s", (tenant_b_resources["certificate"],)) == "ACTIVE"


def test_tenant_scoped_reads_never_show_other_tenant(db, client, tenant_b_resources):
    headers = bearer(login(client, "tenant-a"))
    listing = client.get("/api/v1/fiscal/documents", headers=headers)
    assert listing.status_code == 200 and listing.json()["items"] == []
    status = client.get("/api/v1/fiscal/certificates/status", headers=headers)
    assert status.status_code == 200 and status.json()["configured"] is False
    as_b = bearer(login(client, "tenant-b"))
    assert [d["id"] for d in client.get("/api/v1/fiscal/documents", headers=as_b).json()["items"]] == [
        tenant_b_resources["document"]
    ]


# --- Startup ------------------------------------------------------------------


def test_startup_requires_jwt_keys_when_fiscal_enabled():
    disabled = Settings(fiscal_enabled=False, jwt_keys="", jwt_active_kid="")
    assert_startup_ready(disabled)  # fiscal desligado: sobe
    secret = "s" * 40
    cases = [
        Settings(fiscal_enabled=True, jwt_keys="", jwt_active_kid="k1"),
        Settings(fiscal_enabled=True, jwt_keys="k1:curto", jwt_active_kid="k1"),
        Settings(fiscal_enabled=True, jwt_keys="k1:" + secret, jwt_active_kid="k2"),
    ]
    for settings in cases:
        with pytest.raises(RuntimeError) as exc:
            assert_startup_ready(settings)
        assert secret not in str(exc.value) and "curto" not in str(exc.value)
    assert_startup_ready(
        Settings(fiscal_enabled=True, jwt_keys="k1:" + secret, jwt_active_kid="k1")
    )


def test_auth_without_keys_is_503_without_details(db, client, monkeypatch):
    monkeypatch.setattr(
        fiscal_auth,
        "settings",
        dataclasses.replace(fiscal_auth.settings, jwt_keys="", jwt_active_kid=""),
    )
    response = client.post(
        "/api/v1/auth/login",
        json={"login": "tenant-a@example.com", "password": "senha-tenant-a"},
    )
    assert response.status_code == 503
    assert "JWT" not in response.text


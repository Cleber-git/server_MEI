"""Envelope de erro (contrato seção 2), correlação e API fiscal com Postgres local.

Os testes sem banco montam um app mínimo com ``fiscal.http.install``. Os de
banco usam ``pg_conn`` (schema descartável) com o legado de ``main.py`` +
migrations e dois tenants (A e B).
"""

import asyncio
import dataclasses
import io
import logging
import re
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Query

import fiscal.api as fiscal_api
import fiscal.auth as fiscal_auth
import migrate
from fiscal.http import install

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CNPJ_A = "11222333000181"
CNPJ_B = "45997418000153"
ENVELOPE_KEYS = {"code", "message", "retryable", "correlationId"}


class ApiClient:
    """Cliente síncrono sobre ``httpx.ASGITransport``.

    O ``TestClient`` do Starlette 0.36 não funciona com o httpx 0.28 fixado.
    """

    def __init__(self, app, raise_server_exceptions=True):
        self._transport = httpx.ASGITransport(
            app=app, raise_app_exceptions=raise_server_exceptions
        )

    def request(self, method, url, **kwargs):
        async def send():
            async with httpx.AsyncClient(
                transport=self._transport, base_url="http://testserver"
            ) as client:
                return await client.request(method, url, **kwargs)

        return asyncio.run(send())

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self.request("PUT", url, **kwargs)

    def delete(self, url, **kwargs):
        return self.request("DELETE", url, **kwargs)


def _app() -> FastAPI:
    app = FastAPI()
    install(app)
    app.include_router(fiscal_auth.router)
    app.include_router(fiscal_api.router)

    @app.get("/api/v1/_test/boom")
    def boom():
        raise RuntimeError(f"falha com cnpj {CNPJ_A} e senha=segredo123")

    @app.get("/legacy/http")
    def legacy_http():
        raise HTTPException(404, "Empresa não encontrada")

    @app.get("/legacy/validation")
    def legacy_validation(n: int = Query(...)):
        return {"n": n}

    @app.get("/legacy/boom")
    def legacy_boom():
        raise RuntimeError("legado")

    return app


@pytest.fixture
def client():
    return ApiClient(_app())


def _error(response) -> dict:
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert set(body) == {"error"}
    error = body["error"]
    assert ENVELOPE_KEYS <= set(error)
    assert UUID_RE.match(error["correlationId"])
    assert response.headers["X-Correlation-Id"] == error["correlationId"]
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    return error


# --- Sem banco ----------------------------------------------------------------


def test_401_without_bearer_uses_envelope(client):
    error = _error(client.get("/api/v1/fiscal/config"))
    assert error["code"] == "AUTHENTICATION_REQUIRED"
    assert error["retryable"] is False


def test_401_with_garbage_bearer(client, monkeypatch):
    monkeypatch.setattr(
        fiscal_auth,
        "settings",
        dataclasses.replace(
            fiscal_auth.settings, jwt_keys="k1:" + "x" * 40, jwt_active_kid="k1"
        ),
    )
    response = client.get(
        "/api/v1/fiscal/config", headers={"Authorization": "Bearer nao-e-jwt"}
    )
    assert response.status_code == 401
    assert _error(response)["code"] == "AUTHENTICATION_REQUIRED"


def test_404_unknown_v1_route_uses_envelope(client):
    response = client.get("/api/v1/fiscal/rota-que-nao-existe")
    assert response.status_code == 404
    assert _error(response)["code"] == "NOT_FOUND"


def test_405_uses_envelope(client):
    response = client.delete("/api/v1/auth/login")
    assert response.status_code == 405
    assert _error(response)["code"] == "METHOD_NOT_ALLOWED"


def test_422_lists_fields_without_echoing_input(client):
    response = client.post("/api/v1/auth/login", json={"login": "123.456.789-09"})
    assert response.status_code == 422
    error = _error(response)
    assert error["code"] == "VALIDATION_FAILED"
    assert error["fieldErrors"] == [{"path": "senha", "code": "REQUIRED"}]
    assert "123.456.789-09" not in response.text


def test_422_on_unreadable_json(client):
    response = client.post(
        "/api/v1/auth/login",
        content=b"{nao json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert _error(response)["fieldErrors"][0]["code"] == "INVALID_BODY"


def test_500_is_generic_and_log_is_redacted(client):
    handler = next(
        h for h in logging.getLogger("fiscal").handlers if h.get_name() == "fiscal-json"
    )
    stream = io.StringIO()
    previous = handler.setStream(stream)
    try:
        response = client.get("/api/v1/_test/boom")
    finally:
        handler.setStream(previous)
    assert response.status_code == 500
    error = _error(response)
    assert error["code"] == "INTERNAL_ERROR"
    assert CNPJ_A not in response.text and "RuntimeError" not in response.text
    logged = stream.getvalue()
    assert "RuntimeError" in logged and error["correlationId"] in logged
    assert CNPJ_A not in logged and "segredo123" not in logged


def test_valid_correlation_id_is_echoed(client):
    corr = str(uuid4())
    response = client.get("/api/v1/fiscal/config", headers={"X-Correlation-Id": corr})
    assert _error(response)["correlationId"] == corr


@pytest.mark.parametrize("bad", ["nao-uuid", "1' OR '1'='1", "a" * 300, ""])
def test_invalid_correlation_id_is_replaced(client, bad):
    response = client.get("/api/v1/fiscal/config", headers={"X-Correlation-Id": bad})
    error = _error(response)
    assert error["correlationId"] != bad


def test_legacy_routes_keep_fastapi_format(client):
    response = client.get("/legacy/http")
    assert response.json() == {"detail": "Empresa não encontrada"}
    assert "X-Correlation-Id" not in response.headers
    response = client.get("/legacy/validation")
    assert response.status_code == 422 and "detail" in response.json()
    lenient = ApiClient(_app(), raise_server_exceptions=False)
    response = lenient.get("/legacy/boom")
    assert response.status_code == 500 and response.text == "Internal Server Error"


def test_rate_limited_problem_carries_retry_after():
    from fiscal.errors import ApiProblem

    app = _app()

    @app.get("/api/v1/_test/limited")
    def limited():
        raise ApiProblem(
            429,
            "RATE_LIMITED",
            "Muitas tentativas.",
            retryable=True,
            headers={"Retry-After": "60"},
        )

    response = ApiClient(app).get("/api/v1/_test/limited")
    error = _error(response)
    assert response.headers["Retry-After"] == "60" and error["retryable"] is True


# --- Com Postgres local (tenants A/B) ------------------------------------------

# Recorte do DDL legado de main.py:create_tables() (colunas usadas por fiscal/).
LEGACY = [
    """CREATE TABLE empresa (
        uuid text NOT NULL, cnpj text, razaosocial text, nomefantasia text,
        municipio text, uf text, cnae text, ativo boolean, bloqueado boolean,
        regime_tributario text DEFAULT 'MEI', optante_simples boolean DEFAULT true,
        id bigint NOT NULL GENERATED BY DEFAULT AS IDENTITY)""",
    """CREATE TABLE usuariomei (
        uuid text NOT NULL, email text NOT NULL, senhahash text NOT NULL, nome text,
        empresauuid text, ativo boolean, datacadastro bigint, ultimologin bigint)""",
    # Bloqueio herdado da F0: o legado não tem UNIQUE em uuid (ver test_migrate).
    "CREATE UNIQUE INDEX empresa_uuid_key ON empresa (uuid)",
    "CREATE UNIQUE INDEX usuariomei_uuid_key ON usuariomei (uuid)",
]
PERMS = [
    "FISCAL_CONFIG_READ",
    "FISCAL_CONFIG_WRITE",
    "FISCAL_DOCUMENT_READ",
    "FISCAL_CERTIFICATE_MANAGE",
]


@pytest.fixture
def db(pg_conn, monkeypatch):
    cur = pg_conn.cursor()
    for sql in LEGACY:
        cur.execute(sql)
    for tenant, cnpj in (("tenant-a", CNPJ_A), ("tenant-b", CNPJ_B)):
        cur.execute(
            "INSERT INTO empresa (uuid,cnpj,razaosocial,uf,ativo,bloqueado) VALUES (%s,%s,%s,'SP',TRUE,FALSE)",
            (tenant, cnpj, "Empresa " + tenant),
        )
        cur.execute(
            "INSERT INTO usuariomei (uuid,email,senhahash,nome,empresauuid,ativo) VALUES (%s,%s,%s,%s,%s,TRUE)",
            (
                "user-" + tenant,
                tenant + "@example.com",
                "senha-" + tenant,
                "Dono",
                tenant,
            ),
        )
    pg_conn.commit()
    migrate.migrate(pg_conn)
    cur = pg_conn.cursor()
    for tenant in ("tenant-a", "tenant-b"):
        for perm in PERMS:
            cur.execute(
                "INSERT INTO auth_permissions (user_id,tenant_id,permission) VALUES (%s,%s,%s)",
                ("user-" + tenant, tenant, perm),
            )
    pg_conn.commit()
    for module in (fiscal_api, fiscal_auth):
        monkeypatch.setattr(module, "get_conn", lambda: pg_conn)
        monkeypatch.setattr(module, "put_conn", lambda conn: None)
    monkeypatch.setattr(
        fiscal_auth,
        "settings",
        dataclasses.replace(
            fiscal_auth.settings, jwt_keys="k1:" + "x" * 40, jwt_active_kid="k1"
        ),
    )
    return pg_conn


def _login(client, tenant) -> dict:
    response = client.post(
        "/api/v1/auth/login",
        json={"login": tenant + "@example.com", "senha": "senha-" + tenant},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["accessToken"]}


def test_put_config_writes_audit_as_jsonb(db, client):
    headers = _login(client, "tenant-a")
    corr = str(uuid4())
    response = client.put(
        "/api/v1/fiscal/config",
        headers={**headers, "X-Correlation-Id": corr},
        json={
            "nfseEnvironment": "RESTRICTED_PRODUCTION",
            "nfeEnvironment": "RESTRICTED_PRODUCTION",
            "productionEnabled": False,
            "provider": None,
            "lastValidatedAt": None,
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["X-Correlation-Id"] == corr
    cur = db.cursor()
    cur.execute(
        "SELECT tenant_id,action,sanitized_metadata,correlation_id::text FROM fiscal_audit_log"
    )
    assert cur.fetchall() == [("tenant-a", "FISCAL_CONFIG_UPDATE", {}, corr)]


def test_login_upgrades_plaintext_password_to_bcrypt(db, client):
    _login(client, "tenant-a")
    cur = db.cursor()
    cur.execute("SELECT senhahash FROM usuariomei WHERE uuid='user-tenant-a'")
    assert cur.fetchone()[0].startswith("$2")
    _login(client, "tenant-a")  # segue entrando com o hash novo


def test_permission_missing_is_403_envelope(db, client):
    headers = _login(client, "tenant-a")
    response = client.get(f"/api/v1/fiscal/documents/{uuid4()}", headers=headers)
    assert response.status_code == 404  # tem FISCAL_DOCUMENT_READ
    cur = db.cursor()
    cur.execute("DELETE FROM auth_permissions WHERE permission='FISCAL_DOCUMENT_READ'")
    db.commit()
    response = client.get(f"/api/v1/fiscal/documents/{uuid4()}", headers=headers)
    assert response.status_code == 403
    assert _error(response)["code"] == "PERMISSION_DENIED"


def test_document_of_other_tenant_is_404_without_revealing(db, client):
    cur = db.cursor()
    cur.execute("SELECT id FROM fiscal_establishments WHERE tenant_id='tenant-b'")
    est_b = cur.fetchone()[0]
    doc_b = uuid4()
    cur.execute(
        """INSERT INTO fiscal_documents (id,tenant_id,establishment_id,idempotency_key,
           canonical_request_hash,schema_version,document_type,environment,status,correlation_id)
           VALUES (%s,'tenant-b',%s,'k-b',%s,'1.2','NFSE','RESTRICTED_PRODUCTION','QUEUED',%s)""",
        (str(doc_b), est_b, "0" * 64, str(uuid4())),
    )
    db.commit()
    as_a = client.get(
        f"/api/v1/fiscal/documents/{doc_b}", headers=_login(client, "tenant-a")
    )
    missing = client.get(
        f"/api/v1/fiscal/documents/{uuid4()}", headers=_login(client, "tenant-a")
    )
    assert as_a.status_code == missing.status_code == 404
    assert _error(as_a)["code"] == _error(missing)["code"] == "NOT_FOUND"
    assert as_a.json()["error"]["message"] == missing.json()["error"]["message"]
    as_b = client.get(
        f"/api/v1/fiscal/documents/{doc_b}", headers=_login(client, "tenant-b")
    )
    assert as_b.status_code == 200 and as_b.json()["id"] == str(doc_b)


def test_revoke_other_tenant_certificate_is_404(db, client):
    response = client.delete(
        f"/api/v1/fiscal/certificates/{uuid4()}",
        headers={**_login(client, "tenant-a"), "X-Confirm-Revocation": "REVOKE"},
    )
    assert response.status_code == 404
    assert _error(response)["code"] == "NOT_FOUND"


def test_login_rate_limit_returns_429_with_retry_after(db, client):
    for _ in range(5):
        response = client.post(
            "/api/v1/auth/login", json={"login": "tenant-a@example.com", "senha": "x"}
        )
        assert response.status_code == 401
        assert CNPJ_A not in response.text
    response = client.post(
        "/api/v1/auth/login", json={"login": "tenant-a@example.com", "senha": "x"}
    )
    assert response.status_code == 429
    error = _error(response)
    assert error["code"] == "RATE_LIMITED" and error["retryable"] is True
    assert int(response.headers["Retry-After"]) > 0

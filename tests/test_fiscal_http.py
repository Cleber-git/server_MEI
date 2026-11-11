"""Envelope de erro (contrato seção 2), correlação e API fiscal com Postgres local.

Os testes sem banco montam um app mínimo com ``fiscal.http.install``. Os de
banco usam ``pg_conn`` (schema descartável) com o legado de ``main.py`` +
migrations e dois tenants (A e B).
"""

import dataclasses
import io
import logging
from uuid import uuid4

import pytest
from fastapi import HTTPException, Query

import fiscal.auth as fiscal_auth
from fiscal_support import (
    CNPJ_A,
    ApiClient,
    JWT_KEYS,
    base_app,
    bearer,
    error_of as _error,
    login,
    seed_two_tenants,
)


def _app():
    app = base_app()

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


# --- Sem banco ----------------------------------------------------------------


def test_401_without_bearer_uses_envelope(client):
    error = _error(client.get("/api/v1/fiscal/config"))
    assert error["code"] == "AUTHENTICATION_REQUIRED"
    assert error["retryable"] is False


def test_401_with_garbage_bearer(client, monkeypatch):
    monkeypatch.setattr(
        fiscal_auth,
        "settings",
        dataclasses.replace(fiscal_auth.settings, jwt_keys=JWT_KEYS, jwt_active_kid="k1"),
    )
    response = client.get(
        "/api/v1/fiscal/config", headers={"Authorization": "Bearer nao-e-jwt"}
    )
    assert response.status_code == 401
    assert _error(response)["code"] == "INVALID_TOKEN"


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
    assert error["fieldErrors"] == [{"path": "password", "code": "REQUIRED"}]
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

@pytest.fixture
def db(pg_conn, monkeypatch):
    return seed_two_tenants(pg_conn, monkeypatch)


def _login(client, tenant) -> dict:
    return bearer(login(client, tenant))


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
            "/api/v1/auth/login", json={"login": "tenant-a@example.com", "password": "x"}
        )
        assert response.status_code == 401
        assert CNPJ_A not in response.text
    response = client.post(
        "/api/v1/auth/login", json={"login": "tenant-a@example.com", "password": "x"}
    )
    assert response.status_code == 429
    error = _error(response)
    assert error["code"] == "RATE_LIMITED" and error["retryable"] is True
    assert int(response.headers["Retry-After"]) > 0

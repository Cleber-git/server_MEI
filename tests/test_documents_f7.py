"""F7 — motor de documentos: POST idempotente, worker, estados, eventos, artefatos."""

from __future__ import annotations

import base64
import copy
import dataclasses
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest

import fiscal.api as fiscal_api
import fiscal.auth as fiscal_auth
import fiscal.capabilities as caps_mod
import migrate
from fiscal import documents, providers, vault, worker
from fiscal.config import Settings, assert_startup_ready
from fiscal.domain import FiscalStatus
from fiscal.providers import FakeProvider, Outcome, OutcomeKind, SimulatedCrash
from fiscal_support import (
    ApiClient,
    base_app,
    bearer,
    error_of,
    login,
    seed_two_tenants,
)
from test_capabilities_f5 import (
    _add_capability,
    _add_certificate,
    _homologate,
    _put_est,
)

MASTER = "v1:" + base64.b64encode(b"m" * 32).decode()
DOCS = "/api/v1/fiscal/documents"
DOC_KEYS = {
    "id",
    "documentType",
    "environment",
    "status",
    "series",
    "number",
    "officialKey",
    "protocol",
    "authorizedAt",
    "createdAt",
    "updatedAt",
    "fiscalErrorCode",
    "fiscalErrorMessage",
    "xmlSha256",
    "xmlArtifactId",
    "auxiliaryArtifactId",
    "correlationId",
    "attempt",
    "retryAfter",
    "etag",
}


def service_body(**over):
    """Exatamente o que o ServiceFiscalDraftFactory manda (Gson omite os nulos)."""
    body = {
        "schemaVersion": "1.2",
        "documentType": "NFSE",
        "environment": "RESTRICTED_PRODUCTION",
        "service": {
            "nationalTaxCode": "010101",
            "emissionMode": "SIMPLIFIED",
            "description": "Corte de cabelo",
            "amount": "50.00",
        },
        "recipient": {"taxId": "52998224725", "name": "Maria Cliente"},
        "items": [],
        "payments": [],
        "totals": {
            "products": "0.00",
            "discount": "0.00",
            "freight": "0.00",
            "other": "0.00",
            "invoice": "50.00",
            "paid": "50.00",
            "change": "0.00",
        },
    }
    body.update(over)
    return body


def product_body(
    capability_id="SP:NFCE_65:RESTRICTED_PRODUCTION:2026-11-11T01:00:00+00:00",
):
    """Formato do ProductFiscalDraftFactory.createRouted (2.3)."""
    totals = {
        "products": "20.00",
        "discount": "1.00",
        "freight": "0.00",
        "other": "0.00",
        "invoice": "19.00",
        "paid": "20.00",
        "change": "1.00",
    }
    item = {
        "productId": "p1",
        "description": "Sabonete",
        "gtin": "SEM GTIN",
        "ncm": "34011190",
        "classification": {
            "source": "USER_CONFIRMED",
            "tableVersion": "2026.1",
            "tablePublishedAt": 1700000000000,
            "confirmedAt": 1700000000000,
        },
        "productSource": "RESALE",
        "cfop": "5102",
        "unit": "UN",
        "quantity": "2",
        "unitPrice": "10",
        "grossAmount": "20.00",
        "discountAmount": "1.00",
        "netAmount": "19.00",
        "origin": "0",
        "csosn": "102",
    }
    return {
        "schemaVersion": "2.3",
        "documentType": "NFCE_65",
        "environment": "RESTRICTED_PRODUCTION",
        "product": {
            "contractVersion": "2.3",
            "route": {
                "document": "NFCE_65",
                "reason": "CAPABILITY_ACCEPTED",
                "capabilityId": capability_id,
                "capabilityVersion": "2026-11-11.test",
            },
            "operation": {
                "purpose": "SALE",
                "establishmentUf": "SP",
                "destination": "INTERNAL",
                "recipientType": "INDIVIDUAL",
                "recipientIcmsStatus": "NON_TAXPAYER",
                "consumerFinal": True,
                "salesChannel": "IN_PERSON",
            },
            "recipient": {
                "type": "INDIVIDUAL",
                "taxId": "52998224725",
                "icmsStatus": "NON_TAXPAYER",
                "countryCode": "BR",
                "completeAddressProvided": True,
                "address": {
                    "countryCode": "BR",
                    "postalCode": "01001000",
                    "street": "Rua A",
                    "number": "1",
                    "district": "Centro",
                    "city": "São Paulo",
                    "cityCode": "3550308",
                    "uf": "SP",
                },
            },
            "logistics": {"mode": "NO_TRANSPORT", "transportMode": "NONE"},
            "paymentExpectation": "PAID",
            "items": [item],
            "payments": [{"method": "CASH", "amount": "20.00"}],
            "totals": totals,
        },
        "recipient": {
            "taxId": "52998224725",
            "type": "INDIVIDUAL",
            "countryCode": "BR",
        },
        "items": [],
        "payments": [],
        "totals": dict(totals),
    }


@pytest.fixture
def client():
    return ApiClient(base_app())


@pytest.fixture
def fake():
    provider = FakeProvider()
    providers.set_provider(provider)
    yield provider
    providers.set_provider(None)


@pytest.fixture
def db(pg_conn, monkeypatch):
    conn = seed_two_tenants(pg_conn, monkeypatch)
    for module in (caps_mod, fiscal_api):
        monkeypatch.setattr(
            module,
            "settings",
            dataclasses.replace(module.settings, fiscal_enabled=True),
        )
    monkeypatch.setattr(
        vault,
        "settings",
        dataclasses.replace(
            vault.settings, vault_master_keys=MASTER, vault_active_key_id="v1"
        ),
    )
    return conn


def _ready_nfse(db, client, tenant="tenant-a"):
    headers = bearer(login(client, tenant))
    _put_est(client, headers)
    if tenant == "tenant-a":
        _add_capability(db, "SP", "NFSE")
    _homologate(db, "SP", "NFSE", tenant)
    _add_certificate(db, tenant)
    return headers


@pytest.fixture
def ready(db, client, fake):
    return _ready_nfse(db, client)


def _post(client, headers, body=None, key=None):
    return client.post(
        DOCS,
        headers={**headers, "Idempotency-Key": key or str(uuid4())},
        json=body if body is not None else service_body(),
    )


def _drain(db, owner="w1", *, limit=20, provider_for=None):
    count = 0
    while count < limit and worker.run_once(db, owner=owner, provider_for=provider_for):
        count += 1
    return count


def _make_due(db):
    db.cursor().execute(
        "UPDATE fiscal_jobs SET available_at=NOW() WHERE status='RETRY'"
    )
    db.commit()


def _scalar(db, sql, params=()):
    cur = db.cursor()
    cur.execute(sql, params)
    value = cur.fetchone()
    db.commit()
    return value[0] if value else None


def _doc(client, headers, doc_id):
    response = client.get(f"{DOCS}/{doc_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# --- POST /documents -----------------------------------------------------------------


def test_service_document_is_queued_with_contract_shape(db, client, ready):
    response = _post(client, ready)
    assert response.status_code == 202, response.text
    body = response.json()
    assert set(body) == DOC_KEYS
    assert body["status"] == "QUEUED" and body["documentType"] == "NFSE"
    assert body["series"] == "1" and body["number"] == "1"  # número como texto (app)
    assert body["officialKey"] is None and body["xmlSha256"] is None
    assert body["attempt"] == 0 and body["etag"]
    assert _scalar(db, "SELECT count(*) FROM fiscal_jobs WHERE job_type='SUBMIT'") == 1
    # Payload só cifrado: nada da intenção em texto puro no banco.
    cur = db.cursor()
    cur.execute(
        "SELECT ciphertext FROM fiscal_vault_blobs WHERE purpose='DOCUMENT_PAYLOAD'"
    )
    (cipher,) = cur.fetchone()
    assert b"Corte de cabelo" not in bytes(cipher) and b"52998224725" not in bytes(
        cipher
    )
    db.commit()


def test_ten_identical_posts_create_one_document_and_one_number(db, client, ready):
    key = str(uuid4())
    ids = {_post(client, ready, key=key).json()["id"] for _ in range(10)}
    assert len(ids) == 1
    assert _scalar(db, "SELECT count(*) FROM fiscal_documents") == 1
    assert _scalar(db, "SELECT next_number FROM fiscal_series") == 2
    assert _scalar(db, "SELECT count(*) FROM fiscal_jobs") == 1


def test_ten_concurrent_identical_posts_create_one_document(
    db, client, ready, monkeypatch
):
    """Corrida real (10 conexões): INSERT … ON CONFLICT na idempotência, sem 500."""
    dsn = db.dsn
    opened = []

    def new_conn():
        conn = psycopg2.connect(dsn)
        opened.append(conn)
        return conn

    for module in (fiscal_api, fiscal_auth):
        monkeypatch.setattr(module, "get_conn", new_conn)
        monkeypatch.setattr(module, "put_conn", lambda conn: conn.close())
    key, app = str(uuid4()), base_app()

    def send(_):
        return _post(ApiClient(app), ready, key=key)

    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(pool.map(send, range(10)))
    assert {r.status_code for r in responses} == {202}, [r.text for r in responses]
    assert len({r.json()["id"] for r in responses}) == 1
    assert _scalar(db, "SELECT count(*) FROM fiscal_documents") == 1
    assert _scalar(db, "SELECT next_number FROM fiscal_series") == 2


def test_same_key_with_other_body_is_409(db, client, ready):
    key = str(uuid4())
    assert _post(client, ready, key=key).status_code == 202
    body = service_body()
    body["service"]["description"] = "Outro serviço"
    response = _post(client, ready, body, key=key)
    assert response.status_code == 409
    assert error_of(response)["code"] == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.parametrize(
    "key", [None, "", "curta", "a" * 129, "chave com espaco 123", "chave+com/barra123"]
)
def test_idempotency_key_is_required_and_validated(db, client, ready, key):
    headers = dict(ready)
    if key is not None:
        headers["Idempotency-Key"] = key
    response = client.post(DOCS, headers=headers, json=service_body())
    assert response.status_code == 400
    assert error_of(response)["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_duplicate_intent_for_same_sale_with_other_key_is_409(db, client, ready):
    first = _post(client, ready, service_body(saleId="venda-1")).json()
    response = _post(client, ready, service_body(saleId="venda-1"))
    assert response.status_code == 409
    error = error_of(response)
    assert error["code"] == "DUPLICATE_FISCAL_INTENT" and error["reasons"] == [
        first["id"]
    ]
    # Outra venda com o mesmo conteúdo é outro documento.
    assert _post(client, ready, service_body(saleId="venda-2")).status_code == 202


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"schemaVersion": "1"}, "VALIDATION_FAILED"),
        ({"schemaVersion": "1.0"}, "VALIDATION_FAILED"),
        ({"surprise": True}, "VALIDATION_FAILED"),
        ({"documentType": "NFCE_65"}, "VALIDATION_FAILED"),
        ({"items": [{"x": 1}]}, "VALIDATION_FAILED"),
        ({"documentType": "OFFICIAL_NFF"}, "CAPABILITY_UNAVAILABLE"),
    ],
)
def test_schema_and_document_type_rules(db, client, ready, change, code):
    response = _post(client, ready, service_body(**change))
    assert response.status_code == 422
    assert error_of(response)["code"] == code
    assert _scalar(db, "SELECT count(*) FROM fiscal_documents") == 0


def test_legacy_product_schema_parses_but_is_not_issued(db, client, ready):
    body = product_body()
    product = body.pop("product")
    body.update(
        schemaVersion="1.1",
        operation={
            "consumerFinal": True,
            "presencial": True,
            "interstate": False,
            "recipientIsIcmsTaxpayer": False,
            "delivery": False,
            "foreignTrade": False,
            "logistics": {"mode": "NO_TRANSPORT", "transportMode": "NONE"},
        },
        items=product["items"],
        payments=product["payments"],
    )
    response = _post(client, ready, body)
    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "FISCAL_OPERATION_INCOMPLETE"
    assert error["reasons"] == ["PRODUCT_INTENT_V2_REQUIRED"]


@pytest.mark.parametrize(
    "path",
    [
        ("totals", "invoice", "49.00"),
        ("totals", "paid", "60.00"),
        ("totals", "discount", "1.00"),
    ],
)
def test_service_totals_are_recomputed(db, client, ready, path):
    body = service_body()
    body[path[0]][path[1]] = path[2]
    response = _post(client, ready, body)
    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "VALIDATION_FAILED"
    assert {"path": f"totals.{path[1]}", "code": "TOTAL_MISMATCH"} in error[
        "fieldErrors"
    ]


def test_production_without_gate_is_403(db, client, ready):
    response = _post(client, ready, service_body(environment="PRODUCTION"))
    assert response.status_code == 403
    assert error_of(response)["code"] == "PRODUCTION_NOT_ENABLED"


def test_fiscal_disabled_is_503(db, client, ready, monkeypatch):
    monkeypatch.setattr(
        fiscal_api,
        "settings",
        dataclasses.replace(fiscal_api.settings, fiscal_enabled=False),
    )
    response = _post(client, ready)
    assert (
        response.status_code == 503 and error_of(response)["code"] == "FISCAL_DISABLED"
    )


def test_without_homologation_capability_is_unavailable(db, client, fake):
    headers = bearer(login(client, "tenant-a"))
    _put_est(client, headers)
    _add_capability(db, "SP", "NFSE")
    _add_certificate(db)
    response = _post(client, headers)
    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "CAPABILITY_UNAVAILABLE"
    assert error["reasons"] == ["NFSE_AUTOMATIC_UNAVAILABLE"]


def test_certificate_of_other_holder_blocks_emission(db, client, fake):
    """Pendência da F5: certificado de outro CNPJ (empresa trocou de CNPJ) não emite."""
    headers = bearer(login(client, "tenant-a"))
    _put_est(client, headers)
    _add_capability(db, "SP", "NFSE")
    _homologate(db, "SP", "NFSE")
    _add_certificate(db)
    db.cursor().execute("UPDATE fiscal_certificates SET holder_tax_id='45997418000153'")
    db.commit()
    response = _post(client, headers)
    assert response.status_code == 422
    assert error_of(response)["reasons"] == ["CERTIFICATE_REQUIRED"]


def test_incomplete_establishment_is_422(db, client, ready):
    _put_est(client, ready, address={"postalCode": "01001000"})
    response = _post(client, ready)
    assert response.status_code == 422
    assert error_of(response)["code"] == "ESTABLISHMENT_INCOMPLETE"


# --- Worker: emissão -------------------------------------------------------------------


def test_worker_authorizes_with_full_evidence(db, client, ready, fake):
    doc_id = _post(client, ready).json()["id"]
    assert _drain(db) == 1
    doc = _doc(client, ready, doc_id)
    assert doc["status"] == "AUTHORIZED"
    assert len(doc["officialKey"]) == 50 and doc["protocol"] and doc["authorizedAt"]
    assert (
        len(doc["xmlSha256"]) == 64
        and doc["xmlArtifactId"]
        and doc["auxiliaryArtifactId"]
    )
    assert doc["attempt"] == 1 and doc["retryAfter"] is None
    xml = client.get(f"{DOCS}/{doc_id}/xml", headers=ready)
    assert xml.status_code == 200
    assert xml.headers["content-type"].startswith("application/xml")
    assert xml.headers["Cache-Control"] == "no-store"
    assert xml.headers["X-Content-Type-Options"] == "nosniff"
    assert hashlib.sha256(xml.content).hexdigest() == doc["xmlSha256"]
    pdf = client.get(f"{DOCS}/{doc_id}/auxiliary-document", headers=ready)
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF")
    events = client.get(f"{DOCS}/{doc_id}/events", headers=ready).json()["items"]
    assert [e["type"] for e in events] == [
        "CREATED",
        "QUEUED",
        "SUBMITTED",
        "AUTHORIZED",
    ]
    assert (
        events[-1]["fromStatus"] == "SUBMITTED"
        and events[-1]["toStatus"] == "AUTHORIZED"
    )
    text = str(events)
    assert "52998224725" not in text and "Corte de cabelo" not in text
    assert _scalar(db, "SELECT status FROM fiscal_jobs") == "DONE"


def test_timeout_then_query_never_duplicates(db, client, ready, fake):
    fake.script["submit"] = ["timeout_after_receipt"]
    doc_id = _post(client, ready).json()["id"]
    _drain(db)
    doc = _doc(client, ready, doc_id)
    assert doc["status"] == "SUBMITTED" and doc["retryAfter"] >= 1
    assert _scalar(db, "SELECT outcome_uncertain FROM fiscal_documents") is True
    _make_due(db)
    _drain(db)
    assert _doc(client, ready, doc_id)["status"] == "AUTHORIZED"
    assert fake.count("submit") == 1 and fake.count("query") == 1


def test_timeout_before_receipt_queries_then_sends_once_more(db, client, ready, fake):
    fake.script["submit"] = ["timeout_before_receipt"]
    doc_id = _post(client, ready).json()["id"]
    _drain(db)
    _make_due(db)
    _drain(db)
    doc = _doc(client, ready, doc_id)
    assert doc["status"] == "AUTHORIZED" and doc["attempt"] == 2
    # Só reenviou porque a consulta disse que nada tinha chegado.
    assert [op for op, _ in fake.calls] == ["submit", "query", "submit"]
    assert doc["number"] == "1"  # mesmo número


def test_restart_in_the_middle_queries_instead_of_resending(db, client, ready, fake):
    fake.script["submit"] = ["crash_after_receipt"]
    doc_id = _post(client, ready).json()["id"]
    with pytest.raises(SimulatedCrash):
        worker.run_once(db, owner="dead-worker")
    db.rollback()
    assert _scalar(db, "SELECT status FROM fiscal_jobs") == "RUNNING"
    assert _drain(db, owner="w2") == 0  # lease ainda vale
    db.cursor().execute(
        "UPDATE fiscal_jobs SET lease_expires_at=NOW()-INTERVAL '1 second'"
    )
    db.commit()
    assert _drain(db, owner="w2") == 1
    assert _doc(client, ready, doc_id)["status"] == "AUTHORIZED"
    assert fake.count("submit") == 1 and fake.count("query") == 1


def test_authorization_without_full_evidence_is_never_authorized(
    db, client, ready, fake
):
    fake.script["submit"] = ["incomplete"]
    doc_id = _post(client, ready).json()["id"]
    _drain(db)
    doc = _doc(client, ready, doc_id)
    assert doc["status"] == "PROCESSING" and doc["officialKey"] is None
    assert _scalar(db, "SELECT count(*) FROM fiscal_artifacts") == 0
    _make_due(db)
    _drain(db)  # a consulta devolve a autorização completa
    assert _doc(client, ready, doc_id)["status"] == "AUTHORIZED"
    events = [
        e["type"]
        for e in client.get(f"{DOCS}/{doc_id}/events", headers=ready).json()["items"]
    ]
    assert "AUTHORIZATION_INCOMPLETE" in events


def test_database_refuses_authorized_without_evidence(db, client, ready):
    doc_id = _post(client, ready).json()["id"]
    cur = db.cursor()
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            """UPDATE fiscal_documents SET status='AUTHORIZED',official_key='k',protocol='p',
               authorized_at=NOW() WHERE id=%s""",
            (doc_id,),
        )
    db.rollback()


def test_rejection_keeps_official_code_and_is_final(db, client, ready, fake):
    fake.script["submit"] = ["reject"]
    doc_id = _post(client, ready).json()["id"]
    _drain(db)
    doc = _doc(client, ready, doc_id)
    assert doc["status"] == "REJECTED" and doc["fiscalErrorCode"] == "E999"
    assert doc["xmlArtifactId"] is None
    retry = client.post(
        f"{DOCS}/{doc_id}/retry", headers={**ready, "Idempotency-Key": str(uuid4())}
    )
    assert (
        retry.status_code == 409
        and error_of(retry)["code"] == "INVALID_STATE_TRANSITION"
    )


def test_processing_is_polled_until_authorized(db, client, ready, fake):
    fake.script["submit"] = ["processing"]
    fake.processing_polls = 1
    doc_id = _post(client, ready).json()["id"]
    _drain(db)
    assert _doc(client, ready, doc_id)["status"] == "PROCESSING"
    _make_due(db)
    _drain(db)
    assert _doc(client, ready, doc_id)["status"] == "PROCESSING"
    _make_due(db)
    _drain(db)
    assert _doc(client, ready, doc_id)["status"] == "AUTHORIZED"
    assert fake.count("submit") == 1


def test_temporary_error_retries_with_backoff_then_authorizes(db, client, ready, fake):
    fake.script["submit"] = ["temporary"]
    doc_id = _post(client, ready).json()["id"]
    _drain(db)
    doc = _doc(client, ready, doc_id)
    assert (
        doc["status"] == "ERROR_RETRYABLE"
        and doc["fiscalErrorCode"] == "UPSTREAM_UNAVAILABLE"
    )
    assert doc["retryAfter"] >= 1
    assert _scalar(db, "SELECT status FROM fiscal_jobs") == "RETRY"
    _make_due(db)
    _drain(db)
    doc = _doc(client, ready, doc_id)
    assert doc["status"] == "AUTHORIZED" and doc["number"] == "1"
    assert (
        fake.count("query") == 0
    )  # erro temporário = não chegou: não precisa consultar


def test_exhausted_retries_then_manual_retry_queries_first(db, client, ready, fake):
    fake.script["submit"] = ["timeout_after_receipt"]
    fake.script["query"] = ["timeout"] * 20
    doc_id = _post(client, ready).json()["id"]
    db.cursor().execute("UPDATE fiscal_jobs SET max_attempts=3")
    db.commit()
    for _ in range(4):
        _make_due(db)
        _drain(db)
    doc = _doc(client, ready, doc_id)
    assert doc["status"] == "ERROR_RETRYABLE"
    assert doc["fiscalErrorCode"] == "AUTHORIZATION_PENDING"
    assert _scalar(db, "SELECT status FROM fiscal_jobs") == "DEAD"
    fake.script["query"] = []
    key = str(uuid4())
    retry = client.post(
        f"{DOCS}/{doc_id}/retry", headers={**ready, "Idempotency-Key": key}
    )
    assert retry.status_code == 202 and retry.json()["status"] == "QUEUED"
    again = client.post(
        f"{DOCS}/{doc_id}/retry", headers={**ready, "Idempotency-Key": key}
    )
    assert again.status_code == 202 and again.json()["id"] == doc_id  # idempotente
    _drain(db)
    doc = _doc(client, ready, doc_id)
    assert doc["status"] == "AUTHORIZED" and doc["number"] == "1"
    assert fake.count("submit") == 1  # nunca reenviou: a consulta achou a autorização


def test_default_provider_never_transmits(db, client, ready):
    providers.set_provider(None)
    doc_id = _post(client, ready).json()["id"]
    _drain(db)
    doc = _doc(client, ready, doc_id)
    assert doc["status"] == "ERROR_RETRYABLE"
    assert doc["fiscalErrorCode"] == "PROVIDER_UNAVAILABLE"
    assert _scalar(db, "SELECT status FROM fiscal_jobs") == "DEAD"


def test_fake_provider_refuses_production():
    fake = FakeProvider()
    doc = providers.DocumentContext("1", "t", "NFSE", "PRODUCTION", "1", 1, {})
    assert fake.submit(doc).kind == OutcomeKind.NOT_SENT
    with pytest.raises(RuntimeError):
        assert_startup_ready(
            Settings(
                fiscal_enabled=True,
                jwt_keys="k1:" + "s" * 40,
                jwt_active_kid="k1",
                vault_master_keys=MASTER,
                fiscal_provider="fake",
                production_enabled=True,
            )
        )


def test_backoff_is_exponential_with_jitter_and_capped():
    low = [worker.backoff_seconds(n, rng=lambda: 0.0) for n in range(1, 10)]
    high = [worker.backoff_seconds(n, rng=lambda: 1.0) for n in range(1, 10)]
    assert low[:4] == [2.5, 5.0, 10.0, 20.0] and high[:4] == [5.0, 10.0, 20.0, 40.0]
    assert max(high) == worker.BACKOFF_CAP_SECONDS


def test_unexpected_provider_exception_counts_as_timeout(db, client, ready, fake):
    class Exploding(FakeProvider):
        def submit(self, doc):
            super().submit(doc)
            raise ConnectionResetError("reset")

    exploding = Exploding()
    providers.set_provider(exploding)
    doc_id = _post(client, ready).json()["id"]
    _drain(db)
    assert _doc(client, ready, doc_id)["status"] == "SUBMITTED"
    _make_due(db)
    _drain(db)
    assert _doc(client, ready, doc_id)["status"] == "AUTHORIZED"
    assert exploding.count("submit") == 1


def test_skip_locked_two_workers_never_take_the_same_job(db, client, ready, fake):
    for _ in range(2):
        _post(client, ready)
    other = psycopg2.connect(db.dsn)
    try:
        first = worker.claim(db, "w1")
        second = worker.claim(other, "w2")
        assert first and second and first.id != second.id
        assert worker.claim(other, "w3") is None
        # Lease de outro dono: heartbeat falha e o worker não mexe em nada.
        assert worker.heartbeat(db, second, "w1") is False
        assert worker.heartbeat(other, second, "w2") is True
    finally:
        other.close()


def test_stale_document_cas_is_refused(db, client, ready):
    doc_id = _post(client, ready).json()["id"]
    cur = db.cursor()
    doc = documents.load(cur, "tenant-a", doc_id)
    documents.transition(cur, doc, FiscalStatus.SUBMITTED, event="SUBMITTED")
    with pytest.raises(documents.StaleDocument):
        documents.transition(cur, doc, FiscalStatus.SUBMITTED, event="SUBMITTED")
    db.rollback()


# --- Cancelamento e substituição ---------------------------------------------------------


def _authorized(db, client, headers, body=None):
    doc_id = _post(client, headers, body).json()["id"]
    _drain(db)
    assert _doc(client, headers, doc_id)["status"] == "AUTHORIZED"
    return doc_id


def _cancel(
    client, headers, doc_id, key=None, reason="Serviço não foi prestado ao cliente"
):
    return client.post(
        f"{DOCS}/{doc_id}/cancel",
        headers={**headers, "Idempotency-Key": key or str(uuid4())},
        json={"reason": reason},
    )


def test_cancel_flow(db, client, ready, fake):
    doc_id = _authorized(db, client, ready)
    key = str(uuid4())
    response = _cancel(client, ready, doc_id, key)
    assert response.status_code == 202 and response.json()["status"] == "CANCEL_PENDING"
    assert response.json()["xmlArtifactId"]  # evidência continua visível
    assert _cancel(client, ready, doc_id, key).json()["status"] == "CANCEL_PENDING"
    _drain(db)
    doc = _doc(client, ready, doc_id)
    assert doc["status"] == "CANCELLED" and doc["xmlSha256"]
    assert client.get(f"{DOCS}/{doc_id}/xml", headers=ready).status_code == 200
    assert fake.count("cancel") == 1
    assert (
        _scalar(
            db,
            "SELECT count(*) FROM fiscal_artifacts WHERE artifact_type='XML_CANCEL_EVENT'",
        )
        == 1
    )
    # Motivo só cifrado.
    assert (
        _scalar(
            db, "SELECT count(*) FROM fiscal_events WHERE message LIKE '%%prestado%%'"
        )
        == 0
    )


def test_cancel_rejected_returns_to_authorized(db, client, ready, fake):
    doc_id = _authorized(db, client, ready)
    fake.script["cancel"] = ["cancel_reject"]
    _cancel(client, ready, doc_id)
    _drain(db)
    doc = _doc(client, ready, doc_id)
    assert doc["status"] == "AUTHORIZED" and doc["fiscalErrorCode"] == "E840"


def test_cancel_timeout_queries_before_resending(db, client, ready, fake):
    doc_id = _authorized(db, client, ready)
    fake.script["cancel"] = ["timeout_after_receipt"]
    _cancel(client, ready, doc_id)
    _drain(db)
    assert _doc(client, ready, doc_id)["status"] == "CANCEL_PENDING"
    _make_due(db)
    _drain(db)
    assert _doc(client, ready, doc_id)["status"] == "CANCELLED"
    assert fake.count("cancel") == 1


@pytest.mark.parametrize("reason", ["curto", "x" * 256])
def test_cancel_reason_length(db, client, ready, reason):
    doc_id = _post(client, ready).json()["id"]
    response = _cancel(client, ready, doc_id, reason=reason)
    assert response.status_code == 422


def test_cancel_or_retry_in_wrong_state_is_409(db, client, ready):
    doc_id = _post(client, ready).json()["id"]
    for path, body in (
        ("cancel", {"reason": "Motivo suficientemente longo"}),
        ("retry", None),
    ):
        response = client.post(
            f"{DOCS}/{doc_id}/{path}",
            headers={**ready, "Idempotency-Key": str(uuid4())},
            **({"json": body} if body else {}),
        )
        assert response.status_code == 409
        assert error_of(response)["code"] == "INVALID_STATE_TRANSITION"


def test_substitution_cancels_original_after_replacement_is_authorized(
    db, client, ready, fake
):
    original = _authorized(db, client, ready)
    body = service_body()
    body["service"]["description"] = "Corte de cabelo e barba"
    key = str(uuid4())
    request = {"reason": "Descrição do serviço estava errada", "replacement": body}
    response = client.post(
        f"{DOCS}/{original}/substitute",
        headers={**ready, "Idempotency-Key": key},
        json=request,
    )
    assert response.status_code == 202, response.text
    replacement = response.json()
    assert replacement["id"] != original and replacement["status"] == "QUEUED"
    assert replacement["number"] == "2"
    same = client.post(
        f"{DOCS}/{original}/substitute",
        headers={**ready, "Idempotency-Key": key},
        json=request,
    )
    assert same.json()["id"] == replacement["id"]
    # Enquanto o substituto não sai, o original não pode ser cancelado nem substituído.
    assert _cancel(client, ready, original).status_code == 409
    _drain(db)
    assert _doc(client, ready, replacement["id"])["status"] == "AUTHORIZED"
    assert _doc(client, ready, original)["status"] == "CANCELLED"
    types = [
        e["type"]
        for e in client.get(f"{DOCS}/{original}/events", headers=ready).json()["items"]
    ]
    assert types[-1] == "SUBSTITUTED"


def test_substitution_needs_authorized_nfse(db, client, ready, fake):
    queued = _post(client, ready).json()["id"]
    response = client.post(
        f"{DOCS}/{queued}/substitute",
        headers={**ready, "Idempotency-Key": str(uuid4())},
        json={
            "reason": "Descrição do serviço estava errada",
            "replacement": service_body(),
        },
    )
    assert response.status_code == 409
    fake.supports_substitution = False
    original = _authorized(db, client, ready)
    response = client.post(
        f"{DOCS}/{original}/substitute",
        headers={**ready, "Idempotency-Key": str(uuid4())},
        json={
            "reason": "Descrição do serviço estava errada",
            "replacement": service_body(),
        },
    )
    assert response.status_code == 422
    assert error_of(response)["reasons"] == ["SUBSTITUTION_UNAVAILABLE"]


# --- Produto 2.3 ----------------------------------------------------------------------


def _ready_nfce(db, client):
    headers = bearer(login(client, "tenant-a"))
    _put_est(client, headers, stateRegistration="123456789012")
    _add_capability(db, "SP", "NFCE_65")
    _homologate(db, "SP", "NFCE_65")
    _add_certificate(db)
    csc = client.put(
        "/api/v1/fiscal/csc", headers=headers, json={"cscId": "1", "cscToken": "A" * 20}
    )
    assert csc.status_code == 204
    return headers


def test_routed_product_intent_is_issued(db, client, fake):
    headers = _ready_nfce(db, client)
    response = _post(client, headers, product_body())
    assert response.status_code == 202, response.text
    _drain(db)
    doc = _doc(client, headers, response.json()["id"])
    assert doc["documentType"] == "NFCE_65" and doc["status"] == "AUTHORIZED"


def test_product_route_must_match_server_capability(db, client, fake):
    headers = _ready_nfce(db, client)
    response = _post(client, headers, product_body(capability_id="outra"))
    assert response.status_code == 422
    assert error_of(response)["reasons"] == ["CAPABILITY_ROUTE_MISMATCH"]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("items", 0, "grossAmount"), "21.00"),
        (("items", 0, "netAmount"), "20.00"),
        (("totals", "invoice"), "20.00"),
        (("totals", "change"), "0.00"),
    ],
)
def test_product_totals_are_recomputed(db, client, fake, path, value):
    headers = _ready_nfce(db, client)
    body = product_body()
    target = body["product"]
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    if path[0] == "totals":
        body["totals"] = copy.deepcopy(body["product"]["totals"])
    response = _post(client, headers, body)
    assert response.status_code == 422
    assert error_of(response)["code"] == "VALIDATION_FAILED"


def test_product_top_level_totals_must_match_intent(db, client, fake):
    headers = _ready_nfce(db, client)
    body = product_body()
    body["totals"]["invoice"] = "1.00"
    response = _post(client, headers, body)
    assert (
        response.status_code == 422
        and error_of(response)["code"] == "VALIDATION_FAILED"
    )


# --- Leitura: histórico, eventos, downloads e isolamento ----------------------------------


def test_history_is_paginated_by_cursor(db, client, ready, fake):
    ids = [_post(client, ready).json()["id"] for _ in range(5)]
    seen, cursor = [], None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = client.get(DOCS, headers=ready, params=params).json()
        seen += [d["id"] for d in page["items"]]
        cursor = page["nextCursor"]
        if not cursor:
            break
    assert seen == list(reversed(ids))
    filtered = client.get(DOCS, headers=ready, params={"status": "AUTHORIZED"}).json()
    assert filtered["items"] == [] and filtered["nextCursor"] is None
    for params in (
        {"cursor": "lixo"},
        {"limit": 101},
        {"status": "NOPE"},
        {"documentType": "X"},
    ):
        response = client.get(DOCS, headers=ready, params=params)
        assert response.status_code == 422, params
        assert error_of(response)["code"] == "VALIDATION_FAILED"


def test_downloads_before_authorization_are_404(db, client, ready):
    doc_id = _post(client, ready).json()["id"]
    for suffix in ("xml", "auxiliary-document"):
        response = client.get(f"{DOCS}/{doc_id}/{suffix}", headers=ready)
        assert response.status_code == 404 and error_of(response)["code"] == "NOT_FOUND"


def test_other_tenant_never_sees_documents_events_or_artifacts(db, client, ready, fake):
    doc_id = _authorized(db, client, ready)
    as_b = bearer(login(client, "tenant-b"))
    for suffix in ("", "/events", "/xml", "/auxiliary-document"):
        other = client.get(f"{DOCS}/{doc_id}{suffix}", headers=as_b)
        missing = client.get(f"{DOCS}/{uuid4()}{suffix}", headers=as_b)
        assert other.status_code == missing.status_code == 404
        assert other.json() == {
            **missing.json(),
            "error": {
                **missing.json()["error"],
                "correlationId": other.json()["error"]["correlationId"],
            },
        }
    for path in ("cancel", "retry"):
        response = client.post(
            f"{DOCS}/{doc_id}/{path}",
            headers={**as_b, "Idempotency-Key": str(uuid4())},
            json={"reason": "Tentativa de outro tenant aqui"},
        )
        assert response.status_code == 404
    assert client.get(DOCS, headers=as_b).json()["items"] == []
    assert _doc(client, ready, doc_id)["status"] == "AUTHORIZED"


def test_artifact_rows_are_immutable_and_integrity_checked(db, client, ready, fake):
    doc_id = _authorized(db, client, ready)
    cur = db.cursor()
    with pytest.raises(psycopg2.errors.RaiseException):
        cur.execute("UPDATE fiscal_artifacts SET sha256=%s", ("0" * 64,))
    db.rollback()
    with pytest.raises(psycopg2.errors.RaiseException):
        cur.execute("DELETE FROM fiscal_artifacts")
    db.rollback()
    # Conteúdo trocado no cofre (mesmo tenant/finalidade) não passa no SHA-256.
    cur.execute(
        "SELECT vault_blob_id FROM fiscal_artifacts WHERE artifact_type='XML_AUTHORIZED'"
    )
    blob = cur.fetchone()[0]
    ring = vault.keyring()
    fake_blob = vault.seal(cur, "tenant-a", "ARTIFACT", b"<outro/>", ring)
    cur.execute(
        """UPDATE fiscal_vault_blobs b SET dek_nonce=f.dek_nonce,wrapped_dek=f.wrapped_dek,
           nonce=f.nonce,ciphertext=f.ciphertext FROM fiscal_vault_blobs f
           WHERE b.id=%s AND f.id=%s""",
        (blob, fake_blob),
    )
    db.commit()
    app = base_app()
    response = ApiClient(app, raise_server_exceptions=False).get(
        f"{DOCS}/{doc_id}/xml", headers=ready
    )
    assert (
        response.status_code == 500 and error_of(response)["code"] == "INTERNAL_ERROR"
    )


def test_downloads_are_audited(db, client, ready, fake):
    doc_id = _authorized(db, client, ready)
    client.get(f"{DOCS}/{doc_id}/xml", headers=ready)
    assert (
        _scalar(
            db,
            "SELECT count(*) FROM fiscal_audit_log WHERE action='FISCAL_ARTIFACT_DOWNLOAD'",
        )
        == 1
    )


# --- Migration 007 -------------------------------------------------------------------------


def test_migration_007_is_additive_and_idempotent(db):
    assert migrate.migrate(db) == []  # já aplicada pelo seed: nada pendente
    sql = Path("migrations/007_fiscal_document_engine.sql").read_text(encoding="utf-8")
    db.cursor().execute(sql)  # reexecutar o arquivo inteiro não falha
    db.commit()
    migrate.validate_sql("007_fiscal_document_engine.sql", sql)


def test_outcome_repr_never_shows_content():
    outcome = Outcome(OutcomeKind.AUTHORIZED, xml=b"<segredo/>", auxiliary_pdf=b"%PDF")
    assert "segredo" not in repr(outcome)
    ctx = providers.DocumentContext(
        "1", "t", "NFSE", "RESTRICTED_PRODUCTION", "1", 1, {"a": "b"}
    )
    assert "'a'" not in repr(ctx)

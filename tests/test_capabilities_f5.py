"""F5 — estabelecimento, config, capabilities versionadas, evaluate e catálogos."""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import psycopg2
import pytest

import fiscal.capabilities as caps_mod
from fiscal.capabilities import is_safe_gov_url
from fiscal.establishment import valid_cnpj, valid_cpf
from fiscal_support import (
    CNPJ_A,
    ApiClient,
    base_app,
    bearer,
    error_of,
    insert_user,
    login,
    seed_two_tenants,
)

SP_CITY = "3550308"
RS_CITY = "4314902"
CPF_OK = "52998224725"
CAP_KEYS = {
    "uf",
    "version",
    "nfseAutomatic",
    "nfce",
    "nfe",
    "nffOfficialChannel",
    "requiresCertificate",
    "reason",
    "nfseUnidentifiedRecipientAllowed",
    "stateOfficialChannelUrl",
}
SOURCE = "https://www.gov.br/nfse/pt-br"


@pytest.fixture
def client():
    return ApiClient(base_app())


@pytest.fixture
def db(pg_conn, monkeypatch):
    return seed_two_tenants(pg_conn, monkeypatch)


@pytest.fixture
def fiscal_on(monkeypatch):
    monkeypatch.setattr(
        caps_mod,
        "settings",
        dataclasses.replace(caps_mod.settings, fiscal_enabled=True),
    )


def _h(client, tenant="tenant-a"):
    return bearer(login(client, tenant))


def _est_body(**over):
    body = {
        "legalName": "Empresa A",
        "tradeName": "Fantasia A",
        "stateRegistration": None,
        "municipalRegistration": None,
        "responsibleCpf": None,
        "cityCode": SP_CITY,
        "uf": "SP",
        "address": {
            "postalCode": "01001000",
            "street": "Praça da Sé",
            "number": "100",
            "complement": None,
            "district": "Sé",
            "city": "São Paulo",
        },
        "taxRegime": "MEI",
        "simei": True,
        "cnaes": ["4751201"],
    }
    body.update(over)
    return body


def _put_est(client, headers, **over):
    response = client.put(
        "/api/v1/fiscal/establishment", headers=headers, json=_est_body(**over)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _est_id(db, tenant="tenant-a"):
    cur = db.cursor()
    cur.execute("SELECT id FROM fiscal_establishments WHERE tenant_id=%s", (tenant,))
    return cur.fetchone()[0]


def _add_capability(
    db, uf, operation, *, env="RESTRICTED_PRODUCTION", when=None, **cols
):
    when = when or datetime(2026, 11, 11, 1, tzinfo=timezone.utc)
    values = {
        "id": f"{uf}:{operation}:{env}:{when.isoformat()}",
        "uf": uf,
        "operation": operation,
        "environment": env,
        "ruleset_version": "2026-11-11.test",
        "effective_from": when,
        "available": True,
        "accredited": True,
        "requires_certificate": operation != "OFFICIAL_NFF",
        "requires_csc": operation == "NFCE_65",
        "requires_state_registration": operation in ("NFCE_65", "NFE_55"),
        "source_url": SOURCE,
        "verified_at": when.date(),
        **cols,
    }
    names = ",".join(values)
    marks = ",".join(["%s"] * len(values))
    db.cursor().execute(
        f"INSERT INTO fiscal_capabilities ({names}) VALUES ({marks})",
        list(values.values()),
    )
    db.commit()


def _homologate(db, uf, model, tenant="tenant-a"):
    db.cursor().execute(
        """INSERT INTO fiscal_homologation_checks (id,tenant_id,establishment_id,uf,model,
           scenario,result,schema_manual_version,checked_at,responsible_user_id)
           VALUES (%s,%s,%s,%s,%s,'AUTORIZACAO','APPROVED','test',NOW(),%s)""",
        (uuid4(), tenant, _est_id(db, tenant), uf, model, "user-" + tenant),
    )
    db.commit()


def _add_certificate(db, tenant="tenant-a", *, expires_in=timedelta(days=300)):
    now = datetime.now(timezone.utc)
    db.cursor().execute(
        """INSERT INTO fiscal_certificates (id,tenant_id,establishment_id,vault_reference,
           fingerprint,serial_number,holder_masked,issuer,valid_from,expires_at,type,status,
           holder_tax_id,holder_kind)
           VALUES (%s,%s,%s,'ref',%s,'1','***','AC',%s,%s,'A1','ACTIVE',
                   (SELECT cnpj FROM empresa WHERE uuid=%s),'CNPJ')""",
        (
            uuid4(),
            tenant,
            _est_id(db, tenant),
            "f" * 64,
            now - timedelta(days=1),
            now + expires_in,
            tenant,
        ),
    )
    db.commit()


def _add_csc(db, tenant="tenant-a"):
    db.cursor().execute(
        """INSERT INTO fiscal_credentials (id,tenant_id,establishment_id,credential_type,
           vault_reference,status) VALUES (%s,%s,%s,'NFCE_CSC','ref','ACTIVE')""",
        (uuid4(), tenant, _est_id(db, tenant)),
    )
    db.commit()


# --- Validadores e régua de URL (sem banco) ----------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://gov.br",
        "https://www.gov.br/nfse/pt-br",
        "https://dfe-portal.svrs.rs.gov.br/Nff",
        "https://www.fazenda.sp.gov.br/x?y=1",
    ],
)
def test_gov_url_accepted(url):
    assert is_safe_gov_url(url)


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "http://www.gov.br",
        "https://evil.com",
        "https://gov.br.evil.com",
        "https://evilgov.br",
        "https://user@sefaz.sp.gov.br",
        "https://user:pw@sefaz.sp.gov.br",
        "https://sefaz.sp.gov.br@evil.com",
        "https://sefaz.sp.gov.br:8443/",
        " https://www.gov.br",
        "https://www.gov.br/a b",
        "javascript:alert(1)",
        "ftp://www.gov.br",
    ],
)
def test_url_outside_gov_br_is_rejected(url):
    assert not is_safe_gov_url(url)


def test_document_validators():
    assert valid_cnpj(CNPJ_A) and valid_cnpj("12ABC34501DE35")
    assert not valid_cnpj("11222333000180") and not valid_cnpj("00000000000000")
    assert not valid_cnpj("1122233300018") and not valid_cnpj(None)
    assert valid_cpf(CPF_OK)
    assert not valid_cpf("52998224724") and not valid_cpf("11111111111")


def test_capabilities_dto_without_data_is_blocked():
    now = datetime.now(timezone.utc)
    body = caps_mod.capabilities_dto("SP", {}, set(), now)
    assert set(body) == CAP_KEYS
    assert not any(
        body[k] for k in ("nfseAutomatic", "nfce", "nfe", "nffOfficialChannel")
    )
    assert body["requiresCertificate"] is True and body["reason"]


# --- Migration 006 -------------------------------------------------------------------


def test_seed_is_conservative(db):
    cur = db.cursor()
    cur.execute("SELECT count(*), count(DISTINCT uf) FROM fiscal_capabilities")
    assert cur.fetchone() == (27 * 4 * 2, 27)
    cur.execute(
        "SELECT count(*) FROM fiscal_capabilities WHERE operation<>'OFFICIAL_NFF' AND (available OR accredited)"
    )
    assert cur.fetchone()[0] == 0
    cur.execute(
        """SELECT DISTINCT uf FROM fiscal_capabilities WHERE operation='OFFICIAL_NFF' AND available
           AND source_url IS NOT NULL AND verified_at IS NOT NULL ORDER BY uf"""
    )
    assert [r[0] for r in cur.fetchall()] == ["PI", "RS", "SC"]
    cur.execute(
        "SELECT count(*) FROM fiscal_capabilities WHERE official_channel_url IS NOT NULL"
    )
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT count(*) FROM fiscal_catalog_metadata")
    assert cur.fetchone()[0] == 0


def test_migration_006_is_idempotent(db):
    sql = Path("migrations/006_fiscal_capabilities.sql").read_text(encoding="utf-8")
    cur = db.cursor()
    cur.execute(sql)
    cur.execute(sql)
    db.commit()
    cur.execute("SELECT count(*) FROM fiscal_capabilities")
    assert cur.fetchone()[0] == 216


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.com",
        "http://www.fazenda.sp.gov.br",
        "https://gov.br.evil.com",
        "https://user@sefaz.sp.gov.br",
        "https://sefaz.sp.gov.br@evil.com",
        "https://sefaz.sp.gov.br:8443/",
    ],
)
def test_database_rejects_channel_url_outside_gov_br(db, url):
    with pytest.raises(psycopg2.errors.CheckViolation):
        _add_capability(db, "SP", "STATE_NFAE", official_channel_url=url)
    db.rollback()


def test_database_rejects_available_without_source(db):
    with pytest.raises(psycopg2.errors.CheckViolation):
        _add_capability(db, "SP", "NFSE", source_url=None)
    db.rollback()
    with pytest.raises(psycopg2.errors.CheckViolation):
        _add_capability(db, "SP", "NFSE", verified_at=None)
    db.rollback()


def test_database_rejects_catalog_source_outside_gov_br(db):
    with pytest.raises(psycopg2.errors.CheckViolation):
        db.cursor().execute(
            """INSERT INTO fiscal_catalog_metadata (type,version,source_url,published_at,sha256)
               VALUES ('ncm','1','https://example.com/ncm.json',NOW(),%s)""",
            ("a" * 64,),
        )
    db.rollback()


# --- GET /capabilities ---------------------------------------------------------------


def test_capabilities_seed_blocks_everything(db, client, fiscal_on):
    headers = _h(client)
    _put_est(client, headers)
    response = client.get("/api/v1/fiscal/capabilities?uf=SP", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == CAP_KEYS
    assert body["version"] == "2026-11-11.1"
    assert not any(
        body[k] for k in ("nfseAutomatic", "nfce", "nfe", "nffOfficialChannel")
    )
    assert body["requiresCertificate"] is True
    assert body["nfseUnidentifiedRecipientAllowed"] is None
    assert body["stateOfficialChannelUrl"] is None
    assert body["reason"]


def test_capabilities_incomplete_establishment_is_blocked(db, client, fiscal_on):
    headers = _h(client)
    _add_capability(db, "SP", "NFSE")
    _homologate(db, "SP", "NFSE")
    body = client.get("/api/v1/fiscal/capabilities?uf=SP", headers=headers).json()
    assert body["nfseAutomatic"] is False
    assert "cadastro" in body["reason"]


def test_capabilities_other_uf_is_blocked(db, client, fiscal_on):
    headers = _h(client)
    _put_est(client, headers)
    _add_capability(db, "RJ", "NFSE")
    body = client.get("/api/v1/fiscal/capabilities?uf=RJ", headers=headers).json()
    assert body["nfseAutomatic"] is False and body["uf"] == "RJ"


def test_nfse_automatic_requires_capability_homologation_and_feature(
    db, client, monkeypatch
):
    headers = _h(client)
    _put_est(client, headers)
    _add_capability(db, "SP", "NFSE", unidentified_recipient_allowed=True)

    def get():
        return client.get("/api/v1/fiscal/capabilities?uf=SP", headers=headers).json()

    assert get()["nfseAutomatic"] is False  # sem homologação e com fiscal desligado
    _homologate(db, "SP", "NFSE")
    assert get()["nfseAutomatic"] is False  # FISCAL_FEATURE_ENABLED=false
    monkeypatch.setattr(
        caps_mod,
        "settings",
        dataclasses.replace(caps_mod.settings, fiscal_enabled=True),
    )
    body = get()
    assert body["nfseAutomatic"] is True and body["requiresCertificate"] is True
    assert body["nfseUnidentifiedRecipientAllowed"] is True
    assert body["version"] == "2026-11-11.test"
    # Homologação de outro tenant não vale para B.
    headers_b = _h(client, "tenant-b")
    _put_est(client, headers_b, legalName="Empresa B")
    b = client.get("/api/v1/fiscal/capabilities?uf=SP", headers=headers_b).json()
    assert b["nfseAutomatic"] is False


def test_capability_versions_and_expiry(db, client, fiscal_on):
    headers = _h(client)
    _put_est(client, headers)
    _homologate(db, "SP", "NFSE")
    now = datetime.now(timezone.utc)
    # Todas depois da seed (2026-11-11T00:00Z): a vigente é a mais nova já em vigor.
    _add_capability(
        db,
        "SP",
        "NFSE",
        when=now - timedelta(hours=1),
        valid_until=now - timedelta(minutes=30),
    )
    body = client.get("/api/v1/fiscal/capabilities?uf=SP", headers=headers).json()
    assert (
        body["nfseAutomatic"] is False
    )  # versão vigente venceu (não volta para a antiga)
    # Nova versão (append-only) volta a liberar; uma futura ainda não vale.
    _add_capability(db, "SP", "NFSE", when=now - timedelta(minutes=10))
    assert client.get("/api/v1/fiscal/capabilities?uf=SP", headers=headers).json()[
        "nfseAutomatic"
    ]
    _add_capability(db, "SP", "NFSE", when=now + timedelta(days=1), available=False)
    assert client.get("/api/v1/fiscal/capabilities?uf=SP", headers=headers).json()[
        "nfseAutomatic"
    ]


def test_nff_state_reports_official_channel(db, client):
    headers = _h(client)
    _put_est(client, headers, uf="RS", cityCode=RS_CITY)
    body = client.get("/api/v1/fiscal/capabilities?uf=RS", headers=headers).json()
    assert body["nffOfficialChannel"] is True
    assert body["nfseAutomatic"] is body["nfce"] is body["nfe"] is False
    assert "Nota Fiscal Fácil" in body["reason"]


def test_state_channel_url_comes_from_capability(db, client):
    headers = _h(client)
    _put_est(client, headers)
    _add_capability(
        db,
        "SP",
        "STATE_NFAE",
        official_channel_url="https://www.fazenda.sp.gov.br/emissor",
        requires_certificate=False,
    )
    body = client.get("/api/v1/fiscal/capabilities?uf=SP", headers=headers).json()
    assert body["stateOfficialChannelUrl"] == "https://www.fazenda.sp.gov.br/emissor"


# --- Estabelecimento -----------------------------------------------------------------


def test_get_establishment_from_seed_lists_missing(db, client):
    headers = _h(client)
    response = client.get("/api/v1/fiscal/establishment", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cnpj"] == CNPJ_A and body["uf"] == "SP"
    assert body["fiscalEnabled"] is False
    missing = {m["documentType"]: m["fields"] for m in body["missingFields"]}
    assert set(missing) == {"NFSE", "NFCE_65", "NFE_55"}
    assert "cityCode" in missing["NFSE"] and "address.street" in missing["NFSE"]
    assert "stateRegistration" in missing["NFE_55"]


def test_put_establishment_enables_fiscal_when_complete(db, client):
    headers = _h(client)
    body = _put_est(client, headers, responsibleCpf=CPF_OK)
    assert body["fiscalEnabled"] is True and body["cnpj"] == CNPJ_A
    missing = {m["documentType"]: m["fields"] for m in body["missingFields"]}
    assert missing["NFSE"] == [] and missing["NFCE_65"] == ["stateRegistration"]
    assert body["address"]["city"] == "São Paulo" and body["responsibleCpf"] == CPF_OK
    again = client.get("/api/v1/fiscal/establishment", headers=headers).json()
    assert again == body
    with_ie = _put_est(client, headers, stateRegistration="123456789012")
    assert all(not m["fields"] for m in with_ie["missingFields"])
    # Volta a incompleto se tirar o endereço.
    off = _put_est(client, headers, address={"postalCode": "01001000"})
    assert off["fiscalEnabled"] is False
    cur = db.cursor()
    cur.execute(
        "SELECT action,sanitized_metadata::text FROM fiscal_audit_log WHERE tenant_id='tenant-a'"
    )
    rows = cur.fetchall()
    assert [r[0] for r in rows] == ["ESTABLISHMENT_UPDATE"] * 3
    for _, meta in rows:
        assert CPF_OK not in meta and CNPJ_A not in meta and "Sé" not in meta
    cur.execute(
        "SELECT fiscal_enabled FROM fiscal_establishments WHERE tenant_id='tenant-b'"
    )
    assert cur.fetchone()[0] is False


def test_put_establishment_round_trips_get_body(db, client):
    headers = _h(client)
    body = _put_est(client, headers)
    response = client.put("/api/v1/fiscal/establishment", headers=headers, json=body)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "over, path, code",
    [
        ({"cnpj": "45997418000153"}, "cnpj", "READ_ONLY"),
        ({"cityCode": RS_CITY}, "cityCode", "UF_MISMATCH"),
        ({"responsibleCpf": "52998224724"}, "responsibleCpf", "INVALID"),
        ({"uf": "XX"}, "uf", "INVALID"),
    ],
)
def test_put_establishment_rejects_without_echo(db, client, over, path, code):
    headers = _h(client)
    response = client.put(
        "/api/v1/fiscal/establishment", headers=headers, json=_est_body(**over)
    )
    assert response.status_code == 422, response.text
    error = error_of(response)
    assert error["code"] == "VALIDATION_FAILED"
    assert {"path": path, "code": code} in error["fieldErrors"]
    for value in over.values():
        assert value not in response.text


def test_put_establishment_accepts_same_cnpj_formatted(db, client):
    headers = _h(client)
    body = _put_est(client, headers, cnpj="11.222.333/0001-81")
    assert body["cnpj"] == CNPJ_A


@pytest.mark.parametrize(
    "over",
    [
        {"unknown": 1},
        {"taxRegime": "SIMPLES"},
        {"stateRegistration": "ISENTO"},
        {"cnaes": ["123"]},
        {"address": {"postalCode": "123"}},
        {"legalName": ""},
    ],
)
def test_put_establishment_schema_errors(db, client, over):
    headers = _h(client)
    response = client.put(
        "/api/v1/fiscal/establishment", headers=headers, json=_est_body(**over)
    )
    assert response.status_code == 422
    assert error_of(response)["code"] == "VALIDATION_FAILED"


def test_establishment_created_when_missing(db, client):
    cur = db.cursor()
    cur.execute(
        """INSERT INTO empresa (uuid,cnpj,razaosocial,nomefantasia,uf,ativo,bloqueado)
           VALUES ('tenant-c','12ABC34501DE35','Empresa C',NULL,NULL,TRUE,FALSE)"""
    )
    db.commit()
    insert_user(db, "tenant-c", "user-tenant-c")
    headers = _h(client, "tenant-c")
    # Sem estabelecimento: config pede o cadastro; o GET mostra o que se sabe da empresa.
    config = client.get("/api/v1/fiscal/config", headers=headers)
    assert config.status_code == 422
    assert error_of(config)["code"] == "ESTABLISHMENT_INCOMPLETE"
    got = client.get("/api/v1/fiscal/establishment", headers=headers).json()
    assert got["cnpj"] == "12ABC34501DE35" and got["uf"] is None
    caps = client.get("/api/v1/fiscal/capabilities?uf=SP", headers=headers).json()
    assert caps["nfseAutomatic"] is False and "cadastro" in caps["reason"]
    body = _put_est(client, headers, legalName="Empresa C")
    assert body["fiscalEnabled"] is True
    expected = UUID(hashlib.md5(b"tenant-c:fiscal-establishment").hexdigest())
    assert str(_est_id(db, "tenant-c")) == str(expected)
    assert client.get("/api/v1/fiscal/config", headers=headers).status_code == 200


def test_establishment_requires_write_permission(db, client):
    insert_user(db, "tenant-a", "user-extra", datacadastro=99)
    response = client.post(
        "/api/v1/auth/login",
        json={"login": "extra@example.com", "password": "senha-tenant-a"},
    )
    headers = bearer(response.json())
    denied = client.put(
        "/api/v1/fiscal/establishment", headers=headers, json=_est_body()
    )
    assert denied.status_code == 403
    assert error_of(denied)["code"] == "PERMISSION_DENIED"


# --- Config --------------------------------------------------------------------------


def _config(**over):
    body = {
        "nfseEnvironment": "RESTRICTED_PRODUCTION",
        "nfeEnvironment": "RESTRICTED_PRODUCTION",
        "productionEnabled": False,
        "provider": "SEFIN_NACIONAL",
        "lastValidatedAt": None,
    }
    body.update(over)
    return body


def test_config_get_and_put(db, client):
    headers = _h(client)
    got = client.get("/api/v1/fiscal/config", headers=headers).json()
    assert got["productionEnabled"] is False
    assert got["nfseEnvironment"] == "RESTRICTED_PRODUCTION"
    put = client.put("/api/v1/fiscal/config", headers=headers, json=_config())
    assert put.status_code == 200, put.text
    assert client.get("/api/v1/fiscal/config", headers=headers).json()["provider"] == (
        "SEFIN_NACIONAL"
    )


def test_config_cannot_enable_production(db, client):
    headers = _h(client)
    response = client.put(
        "/api/v1/fiscal/config",
        headers=headers,
        json=_config(nfseEnvironment="PRODUCTION"),
    )
    assert response.status_code == 403
    assert error_of(response)["code"] == "PRODUCTION_NOT_ENABLED"
    response = client.put(
        "/api/v1/fiscal/config", headers=headers, json=_config(productionEnabled=True)
    )
    assert response.status_code == 422
    assert error_of(response)["fieldErrors"] == [
        {"path": "productionEnabled", "code": "READ_ONLY"}
    ]
    # Mesmo com flags no banco, sem o gate do servidor a produção segue desligada.
    db.cursor().execute(
        """UPDATE fiscal_configurations SET production_enabled=TRUE,
           homologation_approved_at=NOW(),monitoring_active=TRUE,
           operational_checklist_approved_at=NOW()"""
    )
    db.cursor().execute("UPDATE fiscal_establishments SET production_enabled=TRUE")
    db.commit()
    assert (
        client.get("/api/v1/fiscal/config", headers=headers).json()["productionEnabled"]
        is False
    )


def test_config_rejects_unknown_provider(db, client):
    headers = _h(client)
    response = client.put(
        "/api/v1/fiscal/config", headers=headers, json=_config(provider="TERCEIRO")
    )
    assert response.status_code == 422


# --- POST /capabilities/evaluate -----------------------------------------------------


def _service(**over):
    body = {
        "documentType": "NFSE",
        "service": {
            "nationalTaxCode": "010101",
            "nbs": None,
            "emissionMode": "SIMPLIFIED",
            "description": "Serviço",
            "amount": "100.00",
        },
    }
    body["service"].update(over)
    return body


def _product(uf="SP", **op):
    operation = {
        "purpose": "SALE",
        "establishmentUf": uf,
        "destination": "INTERNAL",
        "recipientType": "INDIVIDUAL",
        "recipientIcmsStatus": "NON_TAXPAYER",
        "consumerFinal": True,
        "salesChannel": "IN_PERSON",
    }
    operation.update(op)
    return {
        "operation": operation,
        "recipient": {
            "type": operation["recipientType"],
            "taxId": None,
            "icmsStatus": operation["recipientIcmsStatus"],
            "stateRegistration": None,
            "countryCode": "BR",
            "completeAddressProvided": False,
        },
        "logistics": {"mode": "NO_TRANSPORT", "transportMode": "NONE"},
    }


def _evaluate(client, headers, body):
    response = client.post(
        "/api/v1/fiscal/capabilities/evaluate", headers=headers, json=body
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_evaluate_service_seed_goes_to_official_portal(db, client, fiscal_on):
    headers = _h(client)
    _put_est(client, headers)
    body = _evaluate(client, headers, _service())
    assert body["decision"] == "OFFICIAL_PORTAL"
    assert body["productionEligible"] is False
    assert body["environment"] == "RESTRICTED_PRODUCTION"
    assert body["capabilityId"] == "SP:NFSE:RESTRICTED_PRODUCTION:2026-11-11.1"
    assert body["validUntil"] is not None and body["allowedDocumentTypes"] == []
    review = _evaluate(client, headers, _service(emissionMode="BLOCKED_REVIEW"))
    assert review["decision"] == "REQUIRES_FISCAL_REVIEW"


def test_evaluate_service_automatic_path(db, client, fiscal_on):
    headers = _h(client)
    _put_est(client, headers)
    _add_capability(db, "SP", "NFSE")
    _homologate(db, "SP", "NFSE")
    body = _evaluate(client, headers, _service())
    assert body["decision"] == "CERTIFICATE_SETUP"
    _add_certificate(db)
    body = _evaluate(client, headers, _service())
    assert body["decision"] == "NFSE_AUTOMATIC"
    assert body["allowedDocumentTypes"] == ["NFSE"]
    assert body["rulesetVersion"] == "2026-11-11.test"
    valid_until = datetime.fromisoformat(body["validUntil"].replace("Z", "+00:00"))
    assert valid_until <= datetime.now(timezone.utc) + timedelta(hours=1, minutes=1)


def test_evaluate_expired_certificate_needs_setup(db, client, fiscal_on):
    headers = _h(client)
    _put_est(client, headers)
    _add_capability(db, "SP", "NFSE")
    _homologate(db, "SP", "NFSE")
    _add_certificate(db, expires_in=timedelta(seconds=-1))
    assert _evaluate(client, headers, _service())["decision"] == "CERTIFICATE_SETUP"


@pytest.mark.parametrize(
    "op, reason",
    [
        ({"purpose": "RETURN"}, "OPERATION_REQUIRES_REVIEW"),
        ({"recipientType": "FOREIGN"}, "OPERATION_REQUIRES_REVIEW"),
        ({"recipientIcmsStatus": "UNKNOWN"}, "OPERATION_REQUIRES_REVIEW"),
        ({"establishmentUf": "RJ"}, "CAPABILITY_UF_MISMATCH"),
    ],
)
def test_evaluate_product_requires_review(db, client, fiscal_on, op, reason):
    headers = _h(client)
    _put_est(client, headers)
    body = _evaluate(client, headers, _product(**op))
    assert body["decision"] == "REQUIRES_FISCAL_REVIEW" and reason in body["reasons"]


def test_evaluate_product_seed_is_unavailable_and_nff_routes(db, client, fiscal_on):
    headers = _h(client)
    _put_est(client, headers)
    body = _evaluate(client, headers, _product())
    assert body["decision"] == "UNAVAILABLE" and body["capabilityId"] is None
    _put_est(client, headers, uf="RS", cityCode=RS_CITY)
    nff = _evaluate(client, headers, _product("RS"))
    assert nff["decision"] == "OFFICIAL_NFF"
    assert nff["capabilityId"] == "RS:OFFICIAL_NFF:RESTRICTED_PRODUCTION:2026-11-11.1"
    assert nff["requiresCertificate"] is False


def test_evaluate_product_nfce_path(db, client, fiscal_on):
    headers = _h(client)
    _put_est(client, headers)
    _add_capability(db, "SP", "NFCE_65")
    _homologate(db, "SP", "NFCE_65")
    incomplete = _evaluate(client, headers, _product())
    assert incomplete["decision"] == "UNAVAILABLE"
    assert incomplete["reasons"] == ["ESTABLISHMENT_INCOMPLETE"]
    _put_est(client, headers, stateRegistration="123456789012")
    assert _evaluate(client, headers, _product())["decision"] == "CERTIFICATE_SETUP"
    _add_certificate(db)
    no_csc = _evaluate(client, headers, _product())
    assert no_csc["decision"] == "UNAVAILABLE" and no_csc["reasons"] == ["CSC_REQUIRED"]
    _add_csc(db)
    ok = _evaluate(client, headers, _product())
    assert ok["decision"] == "NFCE_65" and ok["requiresCsc"] is True
    assert ok["capabilityId"].startswith("SP:NFCE_65:")
    # Fora do cenário de NFC-e e sem NF-e liberada: indisponível.
    interstate = _evaluate(client, headers, _product(destination="INTERSTATE"))
    assert interstate["decision"] == "UNAVAILABLE"


def test_evaluate_product_nfe_requires_recipient_address(db, client, fiscal_on):
    headers = _h(client)
    _put_est(client, headers, stateRegistration="123456789012")
    _add_capability(db, "SP", "NFE_55")
    _homologate(db, "SP", "NFE_55")
    _add_certificate(db)
    body = _product(destination="INTERSTATE")
    assert _evaluate(client, headers, body)["decision"] == "REQUIRES_FISCAL_REVIEW"
    body["recipient"]["completeAddressProvided"] = True
    body["recipient"]["address"] = {
        "countryCode": "BR",
        "postalCode": "20040002",
        "street": "Rua A",
        "number": "1",
        "complement": None,
        "district": "Centro",
        "city": "Rio de Janeiro",
        "cityCode": "3304557",
        "uf": "RJ",
    }
    assert _evaluate(client, headers, body)["decision"] == "NFE_55"


def test_evaluate_delivery_without_transport_mode_needs_review(db, client, fiscal_on):
    headers = _h(client)
    _put_est(client, headers)
    body = _product(salesChannel="DELIVERY")
    body["logistics"] = {"mode": "DELIVERY", "transportMode": "NONE"}
    result = _evaluate(client, headers, body)
    assert result["decision"] == "REQUIRES_FISCAL_REVIEW"
    assert result["reasons"] == ["DELIVERY_CONTRACT_INCOMPLETE"]


def test_evaluate_production_and_bad_input(db, client):
    headers = _h(client)
    _put_est(client, headers)
    response = client.post(
        "/api/v1/fiscal/capabilities/evaluate",
        headers=headers,
        json={**_service(), "environment": "PRODUCTION"},
    )
    assert response.status_code == 403
    assert error_of(response)["code"] == "PRODUCTION_NOT_ENABLED"
    for bad in (
        {**_service(), "extra": True},
        {"documentType": "NFSE"},
        {**_product(), "operation": {**_product()["operation"], "purpose": "GIFT"}},
        {},
    ):
        response = client.post(
            "/api/v1/fiscal/capabilities/evaluate", headers=headers, json=bad
        )
        assert response.status_code == 422, bad
        assert error_of(response)["code"] == "VALIDATION_FAILED"


def test_evaluate_without_establishment_is_unavailable(db, client):
    cur = db.cursor()
    cur.execute(
        """INSERT INTO empresa (uuid,cnpj,razaosocial,uf,ativo,bloqueado)
           VALUES ('tenant-c','12ABC34501DE35','Empresa C',NULL,TRUE,FALSE)"""
    )
    db.commit()
    insert_user(db, "tenant-c", "user-tenant-c")
    headers = _h(client, "tenant-c")
    body = _evaluate(client, headers, _service())
    assert body["decision"] == "UNAVAILABLE"
    assert body["reasons"] == ["ESTABLISHMENT_INCOMPLETE"]


# --- Catálogos -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind", ["ncm", "cest", "cfop", "nbs", "nationalTaxCode", "xyz"]
)
def test_catalog_without_official_source_is_404(db, client, kind):
    response = client.get(
        f"/api/v1/fiscal/catalogs/{kind}/metadata", headers=_h(client)
    )
    assert response.status_code == 404
    assert error_of(response)["code"] == "NOT_FOUND"


def test_catalog_metadata_returns_latest(db, client):
    cur = db.cursor()
    for version, published in (("1", "2026-01-01"), ("2", "2026-06-01")):
        cur.execute(
            """INSERT INTO fiscal_catalog_metadata (type,version,source_url,published_at,sha256)
               VALUES ('nbs',%s,%s,%s,%s)""",
            (version, SOURCE, published + "T00:00:00Z", version * 64),
        )
    db.commit()
    response = client.get("/api/v1/fiscal/catalogs/nbs/metadata", headers=_h(client))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == "2" and body["source"] == SOURCE
    assert body["sha256"] == "2" * 64
    assert set(body) == {
        "type",
        "source",
        "version",
        "publishedAt",
        "validUntil",
        "sha256",
    }


def test_new_routes_are_tenant_isolated(db, client, fiscal_on):
    headers_a = _h(client)
    _put_est(client, headers_a, stateRegistration="123456789012")
    headers_b = _h(client, "tenant-b")
    b = client.get("/api/v1/fiscal/establishment", headers=headers_b).json()
    assert b["cnpj"] != CNPJ_A and b["stateRegistration"] is None
    assert b["fiscalEnabled"] is False


def test_default_provider_never_transmits():
    from fiscal import providers

    provider = providers.provider_for("NFSE")
    assert isinstance(provider, providers.UnavailableProvider)
    assert provider.submit(None).kind == providers.OutcomeKind.NOT_SENT

"""F8 — NFS-e pela SEFIN Nacional: DPS, XSD, assinatura, provider, worker, cancelamento, DANFSe.

Nenhum teste usa rede: a SEFIN é ``tests/sefin_fake.py`` sobre ``httpx.MockTransport``.
"""

from __future__ import annotations

import base64
import copy
import dataclasses
import gzip
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from fiscal import danfse, documents, nfse, providers, vault, worker
from fiscal.config import Settings, assert_startup_ready
from fiscal.domain import FiscalStatus
from fiscal.providers import DocumentContext, OutcomeKind, UnavailableProvider
from fiscal.sefin import SefinNacionalProvider
import fiscal.api as fiscal_api
import fiscal.capabilities as caps_mod
from fiscal_support import CNPJ_A, CNPJ_B, ApiClient, base_app, seed_two_tenants
from sefin_fake import FIXTURES, FakeSefin, build_nfse, municipality_credential
from test_capabilities_f5 import _add_capability, _est_id, _homologate, _put_est
from test_documents_f7 import (
    DOCS,
    MASTER,
    _doc,
    _drain,
    _make_due,
    _post,
    _scalar,
    service_body,
)
from test_vault_f4 import PFX_PASSWORD, make_ca, make_leaf, make_pfx

NOW = datetime(2026, 11, 12, 13, 0, tzinfo=timezone.utc)
ISSUER = nfse.Issuer(CNPJ_A, "3550308", None)
EST = {
    "cnpj": CNPJ_A,
    "cityCode": "3550308",
    "municipalRegistration": None,
    "taxRegime": "MEI",
}
N = {"n": nfse.NS}


@pytest.fixture
def client():
    return ApiClient(base_app())


@pytest.fixture
def db(pg_conn, monkeypatch):
    conn = seed_two_tenants(pg_conn, monkeypatch)
    for module in (caps_mod, fiscal_api):
        monkeypatch.setattr(
            module, "settings", dataclasses.replace(module.settings, fiscal_enabled=True)
        )
    monkeypatch.setattr(
        vault,
        "settings",
        dataclasses.replace(
            vault.settings, vault_master_keys=MASTER, vault_active_key_id="v1"
        ),
    )
    return conn


@pytest.fixture(scope="module")
def pki():
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca = make_ca(ca_key)
    keys = {
        name: rsa.generate_private_key(public_exponent=65537, key_size=2048)
        for name in ("a", "b")
    }
    certs = {
        "a": make_leaf(keys["a"], ca_key, ca, cnpj=CNPJ_A),
        "b": make_leaf(keys["b"], ca_key, ca, cnpj=CNPJ_B),
    }
    return SimpleNamespace(ca=ca, keys=keys, certs=certs)


@pytest.fixture(scope="module")
def cred(pki):
    return SimpleNamespace(private_key=pki.keys["a"], certificate=pki.certs["a"])


def _payload(**service):
    body = service_body()
    body["service"].update(service)
    return body


def _dps(payload=None, **kw):
    args = dict(
        payload=payload or _payload(),
        issuer=ISSUER,
        environment="RESTRICTED_PRODUCTION",
        series="1",
        number=7,
        created_at=NOW,
        now=NOW,
    )
    args.update(kw)
    return nfse.build_dps(**args)


def _ctx(cred, **over):
    values = dict(
        id=str(uuid4()),
        tenant_id="tenant-a",
        document_type="NFSE",
        environment="RESTRICTED_PRODUCTION",
        series="1",
        number=7,
        payload=_payload(),
        created_at=NOW,
        issuer=dict(EST),
        credential=cred,
    )
    values.update(over)
    return DocumentContext(**values)


def _provider(fake: FakeSefin, **kw):
    return SefinNacionalProvider(
        transport=fake.transport(), clock=lambda: NOW, **kw
    )


# --- XSD versionado -----------------------------------------------------------------


def test_versioned_xsds_match_official_checksums():
    lines = (nfse.XSD_DIR / "SHA256SUMS").read_text().splitlines()
    assert len(lines) == 20
    for line in lines:
        digest, name = line.split()
        assert hashlib.sha256((nfse.XSD_DIR / name).read_bytes()).hexdigest() == digest
    source = (nfse.XSD_DIR / "SOURCE.md").read_text()
    assert "e7935cbd9470527c6cc32984c1b2263e614183bf0139ce2733eaaed2de9a8072" in source


def test_series_pattern_with_literal_anchors_is_fixed_only_in_memory():
    raw = (nfse.XSD_DIR / "1.01" / "tiposSimples_v1.01.xsd").read_bytes()
    assert b'value="^0{0,4}\\d{1,5}$"' in raw  # arquivo oficial intacto
    for series in ("1", "00001", "49999"):
        assert nfse.schema_errors(_dps(series=series), "DPS") == []
    with pytest.raises(nfse.NfseError, match="DPS_SERIES_INVALID"):
        _dps(series="123456")


# --- DPS ------------------------------------------------------------------------------


def test_dps_from_app_body_is_valid_and_follows_mei_rules():
    dps = _dps()
    assert nfse.schema_errors(dps, "DPS") == []
    inf = dps.find("n:infDPS", N)
    assert inf.get("Id") == "DPS3550308" + "2" + CNPJ_A + "00001" + "0" * 14 + "7"
    assert len(inf.get("Id")) == 45
    get = lambda p: inf.findtext(p, namespaces=N)  # noqa: E731
    assert get("n:tpAmb") == "2"
    assert get("n:tpEmit") == "1"
    assert get("n:dhEmi") == "2026-11-12T10:00:00-03:00"
    assert get("n:dCompet") == "2026-11-12"
    assert get("n:verAplic") == nfse.VER_APLIC
    assert get("n:prest/n:CNPJ") == CNPJ_A
    assert inf.find("n:prest/n:xNome", N) is None  # E0121
    assert inf.find("n:prest/n:end", N) is None  # E0128
    assert inf.find("n:prest/n:IM", N) is None
    assert get("n:prest/n:regTrib/n:opSimpNac") == "2"
    assert get("n:prest/n:regTrib/n:regEspTrib") == "0"
    assert get("n:toma/n:CPF") == "52998224725"
    assert get("n:toma/n:xNome") == "Maria Cliente"
    assert get("n:serv/n:locPrest/n:cLocPrestacao") == "3550308"
    assert get("n:serv/n:cServ/n:cTribNac") == "010101"
    assert get("n:valores/n:vServPrest/n:vServ") == "50.00"
    assert get("n:valores/n:trib/n:tribMun/n:tribISSQN") == "1"
    assert get("n:valores/n:trib/n:tribMun/n:tpRetISSQN") == "1"
    assert get("n:valores/n:trib/n:totTrib/n:indTotTrib") == "0"
    assert inf.find("n:IBSCBS", N) is None


def test_dps_optional_fields_and_competence():
    body = _payload(nbs="123456789", incidenceMunicipalityCode="3509502")
    body["recipient"] = {"taxId": "11.444.777/0001-61", "name": "Cliente PJ"}
    dps = _dps(
        payload=body,
        issuer=nfse.Issuer(CNPJ_A, "3550308", "12345"),
        created_at=NOW - timedelta(days=3),
    )
    assert nfse.schema_errors(dps, "DPS") == []
    inf = dps.find("n:infDPS", N)
    assert inf.findtext("n:prest/n:IM", namespaces=N) == "12345"
    assert inf.findtext("n:toma/n:CNPJ", namespaces=N) == "11444777000161"
    assert inf.findtext("n:serv/n:cServ/n:cNBS", namespaces=N) == "123456789"
    assert inf.findtext("n:serv/n:locPrest/n:cLocPrestacao", namespaces=N) == "3509502"
    assert inf.findtext("n:dCompet", namespaces=N) == "2026-11-09"


def test_dps_without_recipient_has_no_toma_group():
    body = _payload()
    body.pop("recipient")
    dps = _dps(payload=body)
    assert dps.find("n:infDPS/n:toma", N) is None
    assert nfse.schema_errors(dps, "DPS") == []


@pytest.mark.parametrize(
    "change,code",
    [
        ({"recipient": {"taxId": "52998224700", "name": "X"}}, "RECIPIENT_TAX_ID_INVALID"),
        ({"recipient": {"taxId": "52998224725"}}, "RECIPIENT_NAME_REQUIRED"),
        ({"recipient": {"taxId": "12ABC34501DE35", "name": "X"}}, "RECIPIENT_UNSUPPORTED"),
        ({"service": {"withholdingResponsibility": "RECIPIENT"}}, "MEI_WITHHOLDING_NOT_ALLOWED"),
        ({"service": {"amount": "0.00"}}, "SERVICE_AMOUNT_INVALID"),
        ({"service": {"description": "   "}}, "SERVICE_DESCRIPTION_REQUIRED"),
        ({"service": {"nationalTaxCode": "1234"}}, "SERVICE_CODE_INVALID"),
    ],
)
def test_dps_build_refusals_carry_stable_codes(change, code):
    body = _payload()
    for key, value in change.items():
        if key == "service":
            body["service"].update(value)
        else:
            body[key] = value
    with pytest.raises(nfse.NfseError) as error:
        _dps(payload=body)
    assert error.value.code == code
    assert "52998224" not in str(error.value)


def test_issuer_rules():
    assert nfse.issuer_from_establishment(EST) == ISSUER
    with pytest.raises(nfse.NfseError, match="ISSUER_CNPJ_UNSUPPORTED"):
        nfse.issuer_from_establishment({**EST, "cnpj": "12ABC34501DE35"})
    with pytest.raises(nfse.NfseError, match="ESTABLISHMENT_INCOMPLETE"):
        nfse.issuer_from_establishment({**EST, "cityCode": None})
    assert "112223" not in repr(ISSUER)
    with pytest.raises(nfse.NfseError, match="ENVIRONMENT_INVALID"):
        _dps(environment="HOMOLOGATION")
    assert _dps(environment="PRODUCTION").findtext("n:infDPS/n:tpAmb", namespaces=N) == "1"


def test_clean_text_keeps_only_what_the_xsd_accepts():
    assert nfse.clean_text("  Corte — de\tcabelo ✂️ ação  ", 50) == "Corte de cabelo ação"
    assert nfse.clean_text("ﬁno", 10) == "fino"
    assert nfse.clean_text("linha 1 \r\n  linha 2\n", 50, multiline=True) == "linha 1\nlinha 2"
    assert nfse.clean_text("a" * 30, 10) == "a" * 10
    body = _payload(description="Serviço 🚀 com emoji")
    assert nfse.schema_errors(_dps(payload=body), "DPS") == []


def test_substitution_group():
    body = _payload()
    body["substitution"] = {"reason": "Valor do serviço informado errado"}
    dps = _dps(payload=body, substitutes_official_key="1" * 50)
    assert nfse.schema_errors(dps, "DPS") == []
    subst = dps.find("n:infDPS/n:subst", N)
    assert subst.findtext("n:chSubstda", namespaces=N) == "1" * 50
    assert subst.findtext("n:cMotivo", namespaces=N) == "99"
    with pytest.raises(nfse.NfseError, match="SUBSTITUTED_KEY_MISSING"):
        _dps(payload=body)


# --- Assinatura e empacotamento ------------------------------------------------------


def test_signature_is_valid_without_prefix_and_detects_tampering(cred, pki):
    signed = nfse.sign(_dps(), cred)
    assert nfse.schema_errors(signed, "DPS") == []
    assert signed[-1].tag == f"{{{nfse.DS_NS}}}Signature"
    xml = nfse.to_bytes(signed)
    assert b"ds:" not in xml and b'<Signature xmlns="http://www.w3.org/2000/09/xmldsig#">' in xml
    assert b"rsa-sha256" in xml and b"enveloped-signature" in xml
    assert nfse.verify_signature(xml, pki.certs["a"])
    assert not nfse.verify_signature(xml.replace(b"50.00", b"51.00"), pki.certs["a"])
    assert not nfse.verify_signature(xml, pki.certs["b"])
    with pytest.raises(nfse.NfseError, match="SIGNATURE_TARGET_INVALID"):
        nfse.sign(signed, cred)  # nunca assina duas vezes


def test_cancel_event_is_valid_and_signed(cred, pki):
    event = nfse.build_cancel_event(
        official_key="3" * 50,
        issuer=ISSUER,
        environment="RESTRICTED_PRODUCTION",
        reason="Serviço lançado em duplicidade",
        now=NOW,
    )
    signed = nfse.sign(event, cred)
    assert nfse.schema_errors(signed, "pedRegEvento") == []
    inf = signed.find("n:infPedReg", N)
    assert inf.get("Id") == "PRE" + "3" * 50 + "101101"
    assert inf.findtext("n:CNPJAutor", namespaces=N) == CNPJ_A
    assert inf.findtext("n:e101101/n:xDesc", namespaces=N) == "Cancelamento de NFS-e"
    assert inf.findtext("n:e101101/n:cMotivo", namespaces=N) == "9"
    assert nfse.verify_signature(nfse.to_bytes(signed), pki.certs["a"])
    with pytest.raises(nfse.NfseError, match="CANCEL_REASON_INVALID"):
        nfse.build_cancel_event(
            official_key="3" * 50,
            issuer=ISSUER,
            environment="RESTRICTED_PRODUCTION",
            reason="🚀" * 20,
            now=NOW,
        )


def test_pack_unpack_and_limits():
    xml = b"<a>" + b"x" * 1000 + b"</a>"
    packed = nfse.pack(xml)
    assert gzip.decompress(base64.b64decode(packed)) == xml
    assert nfse.unpack(packed) == xml
    assert nfse.pack(xml) == packed  # determinístico (mtime=0)
    bomb = base64.b64encode(gzip.compress(b"0" * (nfse.MAX_UNPACKED_BYTES + 10))).decode()
    for value, code in (
        (bomb, "RESPONSE_XML_TOO_LARGE"),
        ("não é base64", "RESPONSE_XML_INVALID"),
        (base64.b64encode(b"sem gzip").decode(), "RESPONSE_XML_INVALID"),
        (None, "RESPONSE_XML_MISSING"),
    ):
        with pytest.raises(nfse.NfseError, match=code):
            nfse.unpack(value)


def test_response_parser_refuses_dtd_and_entities():
    xxe = b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><a>&x;</a>'
    with pytest.raises(nfse.NfseError, match="RESPONSE_XML_INVALID"):
        nfse.parse_xml(xxe)


# --- NFS-e autorizada (fixture gravada) e DANFSe ----------------------------------------


FIXTURE_DPS_ID = "DPS3550308" + "2" + CNPJ_A + "00001" + "0" * 14 + "1"


def test_recorded_authorized_nfse_is_read_and_checked():
    xml = (FIXTURES / "nfse_autorizada.xml").read_bytes()
    found = nfse.read_authorized(
        xml, expected_dps_id=FIXTURE_DPS_ID, issuer=ISSUER, environment="RESTRICTED_PRODUCTION"
    )
    assert len(found.official_key) == 50 and found.official_key.startswith("3550308")
    assert found.nfse_number == "1"
    assert found.authorized_at == NOW
    assert "1125" not in repr(found)
    for kwargs, code in (
        ({"expected_dps_id": FIXTURE_DPS_ID[:-1] + "2"}, "NFSE_DPS_MISMATCH"),
        ({"issuer": nfse.Issuer(CNPJ_B, "3550308")}, "NFSE_ISSUER_MISMATCH"),
        ({"environment": "PRODUCTION"}, "NFSE_ENVIRONMENT_MISMATCH"),
    ):
        args = dict(
            expected_dps_id=FIXTURE_DPS_ID, issuer=ISSUER, environment="RESTRICTED_PRODUCTION"
        )
        args.update(kwargs)
        with pytest.raises(nfse.NfseError, match=code):
            nfse.read_authorized(xml, **args)
    broken = xml.replace(b"<cStat>107</cStat>", b"<cStat>999</cStat>")
    with pytest.raises(nfse.NfseError, match="NFSE_SCHEMA_INVALID"):
        nfse.read_authorized(
            broken, expected_dps_id=FIXTURE_DPS_ID, issuer=ISSUER, environment="RESTRICTED_PRODUCTION"
        )


def test_danfse_has_nt008_content_and_one_page():
    xml = (FIXTURES / "nfse_autorizada.xml").read_bytes()
    data = danfse.extract(xml)
    assert data["restricted"] is True
    assert len(data["key"]) == 50
    ident = dict(data["identification"])
    assert ident["Número da NFS-e"] == "1"
    assert ident["Número da DPS"] == "1"
    assert ident["Emitente da NFS-e"] == "Prestador do Serviço"
    assert ident["Situação da NFS-e"] == "NFS-e MEI"
    provider = dict(data["provider"])
    assert provider["CNPJ / CPF / NIF"] == "11.222.333/0001-81"
    assert provider["Simples Nacional na competência"].endswith("(MEI)")
    assert dict(data["recipient"])["CNPJ / CPF / NIF"] == "529.982.247-25"
    assert dict(data["totals"])["Valor Líquido da NFS-e"] == "R$ 1.250,00"
    assert data["description"].startswith("Desenvolvimento de site")
    pdf = danfse.render(xml)
    assert pdf.startswith(b"%PDF-") and pdf.rstrip().endswith(b"%%EOF")
    assert pdf.count(b"/Type /Page\n") + pdf.count(b"/Type /Page ") + pdf.count(
        b"/Type /Page>"
    ) >= 1
    assert b"/Count 1" in pdf
    assert danfse.CONSULTA_URL == "https://www.nfse.gov.br/ConsultaPublica/?tpc=1&chave="


def test_danfse_with_long_text_stays_on_one_page(cred):
    body = _payload(description="Linha muito longa de descrição " * 120)
    body.pop("recipient")
    dps = nfse.to_bytes(nfse.sign(_dps(payload=body), cred))
    xml = build_nfse(dps, municipality_credential())
    data = danfse.extract(xml)
    assert data["recipient"] is None
    pdf = danfse.render(xml)
    assert b"/Count 1" in pdf


# --- Provider com a SEFIN falsa (sem banco) ----------------------------------------------


def test_submit_authorizes_with_signed_dps_and_full_evidence(cred, pki):
    fake = FakeSefin(expected_certificate=pki.certs["a"])
    outcome = _provider(fake).submit(_ctx(cred))
    assert outcome.kind == OutcomeKind.AUTHORIZED
    assert len(outcome.official_key) == 50
    assert outcome.protocol == "1"  # número da NFS-e
    assert outcome.authorized_at is not None and outcome.xml.startswith(b"<?xml")
    assert outcome.auxiliary_pdf.startswith(b"%PDF-")
    assert fake.count("POST", "/nfse") == 1
    assert fake.count("GET", "/parametros_municipais/3550308/convenio") == 1
    dps_id = next(iter(fake.received))
    assert dps_id == "DPS3550308" + "2" + CNPJ_A + "00001" + "0" * 14 + "7"
    assert "XML" not in repr(outcome)


def test_submit_rejection_keeps_code_and_description_but_never_the_complement(cred):
    fake = FakeSefin(script={"submit": ["reject"]})
    outcome = _provider(fake).submit(_ctx(cred))
    assert outcome.kind == OutcomeKind.REJECTED
    assert outcome.official_code == "E0310"
    assert "código de tributação nacional" in outcome.official_message
    assert "52998224725" not in (outcome.official_message or "")


def test_duplicate_dps_queries_instead_of_failing(cred):
    fake = FakeSefin()
    provider = _provider(fake)
    first = provider.submit(_ctx(cred, id="d1"))
    again = provider.submit(_ctx(cred, id="d1"))  # mesmo nº de DPS: E0014
    assert again.kind == OutcomeKind.AUTHORIZED
    assert again.official_key == first.official_key
    assert fake.count("GET", "/dps/") == 1
    assert len(fake.received) == 1


@pytest.mark.parametrize(
    "behavior,kind",
    [
        ("timeout_after_receipt", OutcomeKind.TIMEOUT),
        ("connect_error", OutcomeKind.TEMPORARY),
        ("http_503", OutcomeKind.TIMEOUT),
        ("http_403", OutcomeKind.NOT_SENT),
        ("bad_nfse", OutcomeKind.PROCESSING),
    ],
)
def test_submit_transport_outcomes(cred, behavior, kind):
    fake = FakeSefin(script={"submit": [behavior]})
    outcome = _provider(fake).submit(_ctx(cred))
    assert outcome.kind == kind
    if behavior == "http_503":
        assert outcome.retry_after == 7


def test_query_finds_by_dps_id_or_says_not_found(cred):
    fake = FakeSefin(script={"submit": ["timeout_after_receipt"]})
    provider = _provider(fake)
    assert provider.query(_ctx(cred, number=99)).kind == OutcomeKind.NOT_FOUND
    assert provider.submit(_ctx(cred)).kind == OutcomeKind.TIMEOUT
    found = provider.query(_ctx(cred))
    assert found.kind == OutcomeKind.AUTHORIZED and found.xml
    assert fake.count("POST", "/nfse") == 1


def test_production_and_missing_credential_never_touch_the_network(cred):
    fake = FakeSefin()
    provider = _provider(fake)
    blocked = provider.submit(_ctx(cred, environment="PRODUCTION"))
    assert (blocked.kind, blocked.official_code) == (OutcomeKind.NOT_SENT, "PRODUCTION_LOCKED")
    no_cert = provider.submit(
        _ctx(None, credential_error="CERTIFICATE_HOLDER_MISMATCH")
    )
    assert no_cert.official_code == "CERTIFICATE_HOLDER_MISMATCH"
    assert provider.submit(_ctx(cred, issuer=None)).official_code == "ESTABLISHMENT_INCOMPLETE"
    assert provider.submit(_ctx(cred, document_type="NFCE_65")).kind == OutcomeKind.NOT_SENT
    # Consulta sem credencial nunca afirma que "não chegou".
    assert provider.query(_ctx(None)).kind == OutcomeKind.TIMEOUT
    built = provider.submit(_ctx(cred, payload=_payload(withholdingResponsibility="RECIPIENT")))
    assert built.official_code == "MEI_WITHHOLDING_NOT_ALLOWED"
    assert fake.requests == []
    import fiscal.sefin as sefin_mod

    source = Path(sefin_mod.__file__).read_text(encoding="utf-8")
    assert "https://sefin.nfse.gov.br" not in source  # sem URL de produção no código


def test_municipal_parameters_are_cached_and_never_block(cred):
    fake = FakeSefin()
    provider = _provider(fake)
    provider.submit(_ctx(cred, id="x1", number=1))
    provider.submit(_ctx(cred, id="x2", number=2))
    assert fake.count("GET", "/parametros_municipais/") == 1

    def failing(request):
        if "parametros_municipais" in request.url.path:
            raise httpx.ConnectError("fora", request=request)
        return fake.handle(request)

    other = SefinNacionalProvider(transport=httpx.MockTransport(failing), clock=lambda: NOW)
    assert other.submit(_ctx(cred, id="x3", number=3)).kind == OutcomeKind.AUTHORIZED


def test_cancel_outcomes(cred, pki):
    fake = FakeSefin(expected_certificate=pki.certs["a"])
    provider = _provider(fake)
    authorized = provider.submit(_ctx(cred))
    key = authorized.official_key
    reason = "Serviço lançado em duplicidade"
    assert provider.query(_ctx(cred, official_key=key)).kind == OutcomeKind.AUTHORIZED
    cancelled = provider.cancel(_ctx(cred, official_key=key), reason)
    assert cancelled.kind == OutcomeKind.CANCELLED and key.encode() in cancelled.event_xml
    assert provider.query(_ctx(cred, official_key=key)).kind == OutcomeKind.CANCELLED

    fake2 = FakeSefin(script={"cancel": ["cancel_reject"]})
    p2 = _provider(fake2)
    key2 = p2.submit(_ctx(cred)).official_key
    refused = p2.cancel(_ctx(cred, official_key=key2), reason)
    assert (refused.kind, refused.official_code) == (OutcomeKind.CANCEL_REJECTED, "E1235")

    fake3 = FakeSefin(script={"cancel": ["already_cancelled"]})
    p3 = _provider(fake3)
    key3 = p3.submit(_ctx(cred)).official_key
    assert p3.cancel(_ctx(cred, official_key=key3), reason).kind == OutcomeKind.CANCELLED


def test_provider_logs_have_no_xml_or_tax_ids(cred, caplog):
    caplog.set_level(logging.DEBUG, logger="fiscal")
    fake = FakeSefin(script={"submit": ["reject"]})
    _provider(fake).submit(_ctx(cred))
    _provider(FakeSefin()).submit(_ctx(cred))
    text = "\n".join(
        r.getMessage() + " " + str(r.__dict__) for r in caplog.records
    )
    for secret in (CNPJ_A, "52998224725", "<DPS", "<NFSe", "dpsXmlGZipB64"):
        assert secret not in text


# --- Seleção do provider e startup ---------------------------------------------------------


def test_provider_selection_and_startup(monkeypatch, tmp_path):
    from fiscal import config

    monkeypatch.setattr(
        config, "settings", dataclasses.replace(config.settings, fiscal_provider="sefin")
    )
    monkeypatch.setattr(providers, "_sefin_singleton", None)
    first = providers.provider_for("NFSE")
    assert isinstance(first, SefinNacionalProvider)
    assert providers.provider_for("NFSE") is first
    assert first.supports_substitution
    assert isinstance(providers.provider_for("NFCE_65"), UnavailableProvider)
    base = dict(
        fiscal_enabled=True,
        jwt_keys="k1:" + "x" * 40,
        jwt_active_kid="k1",
        vault_master_keys=MASTER,
        vault_active_key_id="v1",
        fiscal_provider="sefin",
    )
    assert_startup_ready(Settings(**base)) is None
    with pytest.raises(RuntimeError, match="FISCAL_NFSE_CA_FILE"):
        assert_startup_ready(Settings(**base, nfse_ca_file=str(tmp_path / "nada.pem")))
    with pytest.raises(RuntimeError, match="FISCAL_PROVIDER"):
        assert_startup_ready(Settings(**{**base, "fiscal_provider": "sefaz"}))


# --- Worker + banco + cofre ----------------------------------------------------------------


def _store_cert(db, pki, holder="a", tenant="tenant-a"):
    data = make_pfx(pki.keys[holder], pki.certs[holder], [pki.ca])
    parsed = vault.parse_pkcs12(data, bytearray(PFX_PASSWORD.encode("utf-8")))
    vault.store_certificate(
        db.cursor(),
        tenant,
        _est_id(db, tenant),
        parsed,
        vault.keyring(),
        datetime.now(timezone.utc),
    )
    db.commit()


@pytest.fixture
def sefin_ready(db, client, pki):
    from fiscal_support import bearer, login

    headers = bearer(login(client, "tenant-a"))
    _put_est(client, headers)
    _add_capability(db, "SP", "NFSE")
    _homologate(db, "SP", "NFSE", "tenant-a")
    _store_cert(db, pki)
    fake = FakeSefin(expected_certificate=pki.certs["a"])
    provider = SefinNacionalProvider(transport=fake.transport())
    providers.set_provider(provider)
    yield SimpleNamespace(headers=headers, fake=fake, provider=provider)
    providers.set_provider(None)


def test_worker_authorizes_nfse_end_to_end(db, client, sefin_ready):
    h = sefin_ready.headers
    created = _post(client, h)
    assert created.status_code == 202, created.text
    doc_id = created.json()["id"]
    _drain(db)
    doc = _doc(client, h, doc_id)
    assert doc["status"] == "AUTHORIZED"
    assert len(doc["officialKey"]) == 50 and doc["protocol"] == "1"
    assert doc["series"] == "1" and doc["number"] == "1"
    xml = client.get(f"{DOCS}/{doc_id}/xml", headers=h)
    assert xml.status_code == 200
    assert xml.content == sefin_ready.fake.nfse_by_key[doc["officialKey"]]
    assert hashlib.sha256(xml.content).hexdigest() == doc["xmlSha256"]
    pdf = client.get(f"{DOCS}/{doc_id}/auxiliary-document", headers=h)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")
    assert pdf.headers["content-type"].startswith("application/pdf")
    assert sefin_ready.fake.count("POST", "/nfse") == 1


def test_worker_timeout_after_receipt_queries_and_never_duplicates(db, client, sefin_ready):
    fake = sefin_ready.fake
    fake.script["submit"] = ["timeout_after_receipt"]
    doc_id = _post(client, sefin_ready.headers).json()["id"]
    _drain(db)
    _make_due(db)
    _drain(db)
    doc = _doc(client, sefin_ready.headers, doc_id)
    assert doc["status"] == "AUTHORIZED"
    assert fake.count("POST", "/nfse") == 1
    assert fake.count("GET", "/dps/") >= 1
    assert len(fake.received) == 1


def test_worker_checks_certificate_holder_before_signing(db, client, sefin_ready, pki):
    doc_id = _post(client, sefin_ready.headers).json()["id"]
    # Depois da criação, o certificado ativo passa a ser de outro CNPJ (cofre trocado).
    _store_cert(db, pki, holder="b")
    _drain(db)
    doc = _doc(client, sefin_ready.headers, doc_id)
    assert doc["status"] == "ERROR_RETRYABLE"
    assert doc["fiscalErrorCode"] == "CERTIFICATE_HOLDER_MISMATCH"
    assert sefin_ready.fake.requests == []


def test_worker_cancel_end_to_end(db, client, sefin_ready):
    h = sefin_ready.headers
    doc_id = _post(client, h).json()["id"]
    _drain(db)
    response = client.post(
        f"{DOCS}/{doc_id}/cancel",
        headers={**h, "Idempotency-Key": str(uuid4())},
        json={"reason": "Serviço lançado em duplicidade"},
    )
    assert response.status_code == 202, response.text
    _drain(db)
    doc = _doc(client, h, doc_id)
    assert doc["status"] == "CANCELLED"
    assert (
        _scalar(
            db,
            "SELECT COUNT(*) FROM fiscal_artifacts WHERE document_id=%s AND artifact_type=%s",
            (doc_id, documents.XML_CANCEL_EVENT),
        )
        == 1
    )


def test_worker_substitution_uses_subst_group(db, client, sefin_ready):
    h = sefin_ready.headers
    original_id = _post(client, h).json()["id"]
    _drain(db)
    original = _doc(client, h, original_id)
    replacement = copy.deepcopy(service_body())
    replacement["service"]["amount"] = "60.00"
    replacement["totals"]["invoice"] = replacement["totals"]["paid"] = "60.00"
    response = client.post(
        f"{DOCS}/{original_id}/substitute",
        headers={**h, "Idempotency-Key": str(uuid4())},
        json={"reason": "Valor do serviço informado errado", "replacement": replacement},
    )
    assert response.status_code == 202, response.text
    _drain(db)
    new = _doc(client, h, response.json()["id"])
    assert new["status"] == "AUTHORIZED"
    assert _doc(client, h, original_id)["status"] == "CANCELLED"
    dps = sefin_ready.fake.received[
        next(k for k, v in sefin_ready.fake.key_by_dps.items() if v == new["officialKey"])
    ]
    subst = nfse.parse_xml(dps).find("n:infDPS/n:subst", N)
    assert subst.findtext("n:chSubstda", namespaces=N) == original["officialKey"]


def test_worker_status_never_authorizes_without_valid_nfse(db, client, sefin_ready):
    sefin_ready.fake.script["submit"] = ["bad_nfse"]
    doc_id = _post(client, sefin_ready.headers).json()["id"]
    _drain(db)
    assert _doc(client, sefin_ready.headers, doc_id)["status"] == FiscalStatus.PROCESSING.value
    assert worker  # módulo usado pelo _drain

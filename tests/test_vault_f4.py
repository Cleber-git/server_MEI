"""F4 — cofre de segredos: envelope AES-256-GCM, importação do PFX A1, CSC e mTLS.

Os PFX são sintéticos (AC de teste + folha com as extensões da ICP-Brasil). Os
testes de banco usam só o Postgres local de ``scripts/dev_check.sh``.
"""

from __future__ import annotations

import base64
import dataclasses
import logging
import os
import ssl
import stat
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID, ObjectIdentifier

import fiscal.vault as vault
from fiscal.config import Settings, assert_startup_ready
from fiscal.errors import ApiProblem
from fiscal_support import (
    CNPJ_A,
    CNPJ_B,
    ApiClient,
    base_app,
    bearer,
    error_of,
    login,
    seed_two_tenants,
)

NOW = datetime.now(timezone.utc)
PFX_PASSWORD = "senha-do-pfx-Ã§"
CPF_TITULAR = "52998224725"
CSC_TOKEN = "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6"
MASTER_1 = base64.b64encode(b"1" * 32).decode()
MASTER_2 = base64.urlsafe_b64encode(b"2" * 32).decode().rstrip("=")
A1_POLICY = "2.16.76.1.2.1.51"


# --- PFX sintéticos ---------------------------------------------------------------


@pytest.fixture(scope="module")
def keys():
    return {
        name: rsa.generate_private_key(public_exponent=65537, key_size=2048)
        for name in ("ca", "leaf", "leaf2", "other")
    }


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _octet(text: str) -> bytes:
    data = text.encode("latin-1")
    return b"\x04" + bytes([len(data)]) + data


def make_ca(key, cn="AC Teste ICP", *, start=None, end=None):
    start = start or NOW - timedelta(days=365)
    end = end or NOW + timedelta(days=3650)
    return (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(_name(cn))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )


def make_leaf(
    key,
    ca_key,
    ca_cert,
    *,
    cnpj=CNPJ_A,
    cpf=None,
    cn="EMPRESA TESTE",
    policy=A1_POLICY,
    start=None,
    end=None,
    signature_usage=True,
):
    others = []
    if cnpj:
        others.append(
            x509.OtherName(ObjectIdentifier(vault.ICP_CNPJ_OID), _octet(cnpj))
        )
    if cpf:
        others.append(
            x509.OtherName(
                ObjectIdentifier(vault.ICP_CPF_OID), _octet("01011990" + cpf + "0" * 26)
            )
        )
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start or NOW - timedelta(days=1))
        .not_valid_after(end or NOW + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=signature_usage,
                content_commitment=True,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    )
    if others:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(others), critical=False
        )
    if policy:
        builder = builder.add_extension(
            x509.CertificatePolicies(
                [x509.PolicyInformation(ObjectIdentifier(policy), None)]
            ),
            critical=False,
        )
    return builder.sign(ca_key, hashes.SHA256())


def make_pfx(key, cert, cas=(), password=PFX_PASSWORD):
    return pkcs12.serialize_key_and_certificates(
        b"cert",
        key,
        cert,
        list(cas),
        serialization.BestAvailableEncryption(password.encode("utf-8")),
    )


@pytest.fixture(scope="module")
def ca(keys):
    return make_ca(keys["ca"])


@pytest.fixture(scope="module")
def good_pfx(keys, ca):
    return make_pfx(keys["leaf"], make_leaf(keys["leaf"], keys["ca"], ca), [ca])


def parse(data, password=PFX_PASSWORD):
    return vault.parse_pkcs12(data, bytearray(password.encode("utf-8")))


def problem_code(call):
    with pytest.raises(ApiProblem) as exc:
        call()
    return exc.value.code


# --- Parser do PFX (sem banco) ------------------------------------------------------


def test_parse_valid_a1_reads_holder_chain_and_fingerprint(keys, ca, good_pfx):
    parsed = parse(good_pfx)
    assert parsed.holder == vault.Holder("CNPJ", CNPJ_A)
    assert [c.subject for c in parsed.chain] == [ca.subject]
    assert parsed.fingerprint == parsed.certificate.fingerprint(hashes.SHA256()).hex()
    assert CNPJ_A not in repr(parsed)


def test_wrong_password_is_specific_code(good_pfx):
    assert (
        problem_code(lambda: parse(good_pfx, "errada"))
        == "CERTIFICATE_INVALID_PASSWORD"
    )


@pytest.mark.parametrize(
    "data",
    [b"", b"nao e pfx", b"\x30\x03\x02\x01\x02", b"\x30\x82\xff\xff\x02\x01\x03"],
)
def test_garbage_is_invalid_not_password(data):
    assert problem_code(lambda: parse(data)) == "CERTIFICATE_INVALID"


def test_truncated_pfx_is_invalid(good_pfx):
    assert problem_code(lambda: parse(good_pfx[:-10])) == "CERTIFICATE_INVALID"


def test_oversized_pfx_is_invalid():
    assert (
        problem_code(lambda: parse(b"\x30" * (vault.PFX_MAX_BYTES + 1)))
        == "CERTIFICATE_INVALID"
    )


def test_expired_and_not_yet_valid(keys, ca):
    expired = make_leaf(
        keys["leaf"],
        keys["ca"],
        ca,
        start=NOW - timedelta(days=400),
        end=NOW - timedelta(days=1),
    )
    future = make_leaf(keys["leaf"], keys["ca"], ca, start=NOW + timedelta(days=1))
    assert (
        problem_code(lambda: parse(make_pfx(keys["leaf"], expired, [ca])))
        == "CERTIFICATE_EXPIRED"
    )
    assert (
        problem_code(lambda: parse(make_pfx(keys["leaf"], future, [ca])))
        == "CERTIFICATE_INVALID"
    )


@pytest.mark.parametrize(
    "policy", [None, "2.16.76.1.2.3.10", "2.16.76.1.2.101.1", "1.2.3.4"]
)
def test_only_icp_a1_policy_is_accepted(keys, ca, policy):
    cert = make_leaf(keys["leaf"], keys["ca"], ca, policy=policy)
    assert (
        problem_code(lambda: parse(make_pfx(keys["leaf"], cert, [ca])))
        == "CERTIFICATE_INVALID"
    )


def test_self_signed_leaf_and_ca_leaf_are_rejected(keys, ca):
    self_signed = make_ca(keys["leaf"], "EMPRESA TESTE:" + CNPJ_A)
    assert (
        problem_code(lambda: parse(make_pfx(keys["leaf"], self_signed)))
        == "CERTIFICATE_INVALID"
    )


def test_without_digital_signature_usage_is_rejected(keys, ca):
    cert = make_leaf(keys["leaf"], keys["ca"], ca, signature_usage=False)
    assert (
        problem_code(lambda: parse(make_pfx(keys["leaf"], cert, [ca])))
        == "CERTIFICATE_INVALID"
    )


def test_forged_issuer_in_chain_is_rejected(keys, ca):
    leaf = make_leaf(keys["leaf"], keys["ca"], ca)
    forged_ca = make_ca(keys["other"])  # mesmo nome, outra chave
    assert (
        problem_code(lambda: parse(make_pfx(keys["leaf"], leaf, [forged_ca])))
        == "CERTIFICATE_INVALID"
    )


def test_expired_intermediate_is_rejected(keys):
    old_ca = make_ca(
        keys["ca"], start=NOW - timedelta(days=900), end=NOW - timedelta(days=1)
    )
    leaf = make_leaf(keys["leaf"], keys["ca"], old_ca)
    assert (
        problem_code(lambda: parse(make_pfx(keys["leaf"], leaf, [old_ca])))
        == "CERTIFICATE_INVALID"
    )


def test_leaf_without_chain_in_pfx_is_accepted(keys, ca):
    parsed = parse(make_pfx(keys["leaf"], make_leaf(keys["leaf"], keys["ca"], ca)))
    assert parsed.chain == ()


def test_small_rsa_key_is_rejected(keys, ca):
    small = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    cert = make_leaf(small, keys["ca"], ca)
    assert (
        problem_code(lambda: parse(make_pfx(small, cert, [ca])))
        == "CERTIFICATE_INVALID"
    )


def test_holder_from_ecpf_and_from_cn(keys, ca):
    ecpf = make_leaf(
        keys["leaf"],
        keys["ca"],
        ca,
        cnpj=None,
        cpf=CPF_TITULAR,
        cn="FULANO:" + CPF_TITULAR,
    )
    assert vault.read_holder(ecpf) == vault.Holder("CPF", CPF_TITULAR)
    by_cn = make_leaf(keys["leaf"], keys["ca"], ca, cnpj=None, cn="EMPRESA:" + CNPJ_B)
    assert vault.read_holder(by_cn) == vault.Holder("CNPJ", CNPJ_B)
    anonymous = make_leaf(keys["leaf"], keys["ca"], ca, cnpj=None, cn="SEM ID")
    assert vault.read_holder(anonymous) is None


def test_check_holder_rules(keys, ca):
    ecnpj = parse(make_pfx(keys["leaf"], make_leaf(keys["leaf"], keys["ca"], ca), [ca]))
    vault.check_holder(
        ecnpj, cnpj="11.222.333/0001-81", is_mei=False, responsible_cpf=None
    )
    with pytest.raises(ApiProblem) as exc:
        vault.check_holder(ecnpj, cnpj=CNPJ_B, is_mei=True, responsible_cpf=None)
    assert exc.value.code == "CERTIFICATE_HOLDER_MISMATCH"
    assert CNPJ_A not in exc.value.message and CNPJ_B not in exc.value.message

    ecpf = parse(
        make_pfx(
            keys["leaf"],
            make_leaf(keys["leaf"], keys["ca"], ca, cnpj=None, cpf=CPF_TITULAR),
            [ca],
        )
    )
    vault.check_holder(ecpf, cnpj=CNPJ_A, is_mei=True, responsible_cpf=CPF_TITULAR)
    for kwargs in (
        {
            "is_mei": True,
            "responsible_cpf": None,
        },  # sem CPF cadastrado não há conferência
        {"is_mei": False, "responsible_cpf": CPF_TITULAR},  # e-CPF só para MEI
        {"is_mei": True, "responsible_cpf": "11144477735"},
    ):
        with pytest.raises(ApiProblem):
            vault.check_holder(ecpf, cnpj=CNPJ_A, **kwargs)

    anonymous = parse(
        make_pfx(
            keys["leaf"],
            make_leaf(keys["leaf"], keys["ca"], ca, cnpj=None, cn="SEM ID"),
            [ca],
        )
    )
    with pytest.raises(ApiProblem):
        vault.check_holder(
            anonymous, cnpj=CNPJ_A, is_mei=True, responsible_cpf=CPF_TITULAR
        )


# --- Master keys ------------------------------------------------------------------


def test_keyring_parsing_and_active_key():
    settings = Settings(
        vault_master_keys=f"v1:{MASTER_1}, bad, v2:{MASTER_2},v3:curta,v$:{MASTER_1}",
        vault_active_key_id="v2",
    )
    ring = vault.keyring(settings)
    assert sorted(ring.keys) == ["v1", "v2"] and ring.active_id == "v2"
    assert MASTER_1 not in repr(ring) and "1111" not in repr(ring)
    single = vault.keyring(
        Settings(vault_master_keys=f"v1:{MASTER_1}", vault_active_key_id="")
    )
    assert single.active_id == "v1"
    for broken in (
        Settings(vault_master_keys="", vault_active_key_id=""),
        Settings(
            vault_master_keys=f"v1:{MASTER_1},v2:{MASTER_2}", vault_active_key_id=""
        ),
        Settings(vault_master_keys=f"v1:{MASTER_1}", vault_active_key_id="v9"),
    ):
        with pytest.raises(ApiProblem) as exc:
            vault.keyring(broken)
        assert exc.value.status == 503 and MASTER_1 not in exc.value.message


def test_startup_requires_vault_key_when_fiscal_enabled():
    base = dict(fiscal_enabled=True, jwt_keys="k1:" + "s" * 40, jwt_active_kid="k1")
    for broken in ("", "v1:curta", f"v1:{MASTER_1},v2:{MASTER_2}"):
        with pytest.raises(RuntimeError) as exc:
            assert_startup_ready(
                Settings(vault_master_keys=broken, vault_active_key_id="", **base)
            )
        assert MASTER_1 not in str(exc.value) and "curta" not in str(exc.value)
    assert_startup_ready(
        Settings(vault_master_keys=f"v1:{MASTER_1}", vault_active_key_id="", **base)
    )


# --- Envelope no banco ------------------------------------------------------------


def ring_of(*ids, active=None):
    material = {"v1": MASTER_1, "v2": MASTER_2}
    return vault.keyring(
        Settings(
            vault_master_keys=",".join(f"{i}:{material[i]}" for i in ids),
            vault_active_key_id=active or ids[-1],
        )
    )


@pytest.fixture
def db(pg_conn, monkeypatch):
    seed_two_tenants(pg_conn, monkeypatch)
    monkeypatch.setattr(
        vault,
        "settings",
        dataclasses.replace(
            vault.settings, vault_master_keys=f"v1:{MASTER_1}", vault_active_key_id="v1"
        ),
    )
    vault._mtls_cache.clear()
    return pg_conn


def test_seal_open_roundtrip_and_aad_binding(db):
    cur, ring = db.cursor(), ring_of("v1")
    secret = b"segredo-" + os.urandom(16)
    blob_a = vault.seal(cur, "tenant-a", "CSC", secret, ring)
    blob_b = vault.seal(cur, "tenant-a", "CSC", b"outro", ring)
    db.commit()
    assert vault.open_blob(cur, "tenant-a", blob_a, "CSC", ring) == secret
    cur.execute("SELECT ciphertext FROM fiscal_vault_blobs WHERE id=%s", (blob_a,))
    assert secret not in bytes(cur.fetchone()[0])
    with pytest.raises(vault.VaultIntegrityError):  # outro tenant
        vault.open_blob(cur, "tenant-b", blob_a, "CSC", ring)
    with pytest.raises(vault.VaultIntegrityError):  # outra finalidade
        vault.open_blob(cur, "tenant-a", blob_a, "CERTIFICATE", ring)
    # Blob de A movido para B (ou copiado para outra linha) não decifra.
    cur.execute(
        "UPDATE fiscal_vault_blobs SET tenant_id='tenant-b' WHERE id=%s", (blob_a,)
    )
    with pytest.raises(vault.VaultIntegrityError):
        vault.open_blob(cur, "tenant-b", blob_a, "CSC", ring)
    cur.execute(
        "UPDATE fiscal_vault_blobs SET tenant_id='tenant-a' WHERE id=%s", (blob_a,)
    )
    cur.execute(
        """UPDATE fiscal_vault_blobs b SET dek_nonce=o.dek_nonce,wrapped_dek=o.wrapped_dek,nonce=o.nonce,
           ciphertext=o.ciphertext FROM fiscal_vault_blobs o WHERE b.id=%s AND o.id=%s""",
        (blob_b, blob_a),
    )
    with pytest.raises(vault.VaultIntegrityError):
        vault.open_blob(cur, "tenant-a", blob_b, "CSC", ring)
    vault.destroy(cur, "tenant-a", blob_a)
    cur.execute(
        "SELECT length(ciphertext),length(wrapped_dek),destroyed_at FROM fiscal_vault_blobs WHERE id=%s",
        (blob_a,),
    )
    size, wrapped, destroyed = cur.fetchone()
    assert size == 0 and wrapped == 0 and destroyed is not None
    with pytest.raises(vault.VaultIntegrityError):
        vault.open_blob(cur, "tenant-a", blob_a, "CSC", ring)
    vault.destroy(cur, "tenant-a", "referencia-legada")  # formato antigo: ignorado


def test_master_key_rotation_rewraps_only_the_dek(db):
    cur = db.cursor()
    old = ring_of("v1")
    ids = [vault.seal(cur, "tenant-a", "CSC", b"s%d" % i, old) for i in range(5)]
    vault.destroy(cur, "tenant-a", ids[4])
    db.commit()
    cur.execute("SELECT id,ciphertext FROM fiscal_vault_blobs ORDER BY id")
    before = {row[0]: bytes(row[1]) for row in cur.fetchall()}
    both = ring_of("v1", "v2", active="v2")
    assert vault.rewrap_all(db, both, batch_size=2) == 4
    assert vault.rewrap_all(db, both) == 0  # idempotente
    cur = db.cursor()
    cur.execute(
        "SELECT id,key_id,ciphertext,rotated_at FROM fiscal_vault_blobs WHERE destroyed_at IS NULL"
    )
    rows = cur.fetchall()
    assert {r[1] for r in rows} == {"v2"} and all(r[3] for r in rows)
    assert all(bytes(r[2]) == before[r[0]] for r in rows)
    only_new = ring_of("v2")
    assert [vault.open_blob(cur, "tenant-a", i, "CSC", only_new) for i in ids[:4]] == [
        b"s0",
        b"s1",
        b"s2",
        b"s3",
    ]
    with pytest.raises(vault.VaultIntegrityError):
        vault.open_blob(cur, "tenant-a", ids[0], "CSC", ring_of("v1"))


# --- API: certificados --------------------------------------------------------------


@pytest.fixture
def client(db):
    return ApiClient(base_app(), raise_server_exceptions=False)


@pytest.fixture
def fiscal_logs():
    records = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    handler = Grab(logging.DEBUG)
    logger = logging.getLogger("fiscal")
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield records
    logger.removeHandler(handler)
    logger.setLevel(previous)


def upload(
    client,
    headers,
    data,
    *,
    password=PFX_PASSWORD,
    part="certificate",
    content_type="application/x-pkcs12",
):
    return client.post(
        "/api/v1/fiscal/certificates",
        headers=headers,
        files={part: ("certificado.pfx", data, content_type)},
        data={"password": password},
    )


def _all_rows_text(db):
    cur = db.cursor()
    text = []
    for table in (
        "fiscal_certificates",
        "fiscal_credentials",
        "fiscal_audit_log",
        "fiscal_establishments",
    ):
        cur.execute(f"SELECT row_to_json(t)::text FROM {table} t")
        text += [r[0] for r in cur.fetchall()]
    return "\n".join(text)


def test_upload_reencrypts_key_and_signs_without_password(
    db, client, keys, ca, good_pfx, fiscal_logs
):
    headers = bearer(login(client, "tenant-a"))
    response = upload(client, headers, good_pfx)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["configured"] is True and body["valid"] is True and body["type"] == "A1"
    assert body["holderTaxId"] == CNPJ_A and body["holder"].endswith(CNPJ_A[-4:])
    assert PFX_PASSWORD not in response.text

    status = client.get("/api/v1/fiscal/certificates/status", headers=headers).json()
    assert (
        status["certificateId"] == body["certificateId"]
        and status["holderTaxId"] == CNPJ_A
    )

    cur = db.cursor()
    credential = vault.get_signing_credential(cur, "tenant-a", vault.keyring())
    assert str(credential.certificate_id) == body["certificateId"]
    assert [c.subject for c in credential.chain] == [ca.subject]
    signature = credential.private_key.sign(b"dps", padding.PKCS1v15(), hashes.SHA256())
    credential.certificate.public_key().verify(
        signature, b"dps", padding.PKCS1v15(), hashes.SHA256()
    )

    key_der = keys["leaf"].private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    cur.execute(
        "SELECT ciphertext,purpose,key_id FROM fiscal_vault_blobs WHERE tenant_id='tenant-a'"
    )
    ((ciphertext, purpose, key_id),) = cur.fetchall()
    assert purpose == "CERTIFICATE" and key_id == "v1"
    assert key_der[:64] not in bytes(ciphertext) and good_pfx[:64] not in bytes(
        ciphertext
    )
    rows = _all_rows_text(db)
    assert PFX_PASSWORD not in rows and "BEGIN" not in rows
    logs = "\n".join(fiscal_logs)
    assert PFX_PASSWORD not in logs and CNPJ_A not in logs and "PRIVATE KEY" not in logs


def test_reupload_same_fingerprint_reactivates_without_500(db, client, good_pfx):
    headers = bearer(login(client, "tenant-a"))
    first = upload(client, headers, good_pfx).json()
    again = upload(client, headers, good_pfx)
    assert (
        again.status_code == 200
        and again.json()["certificateId"] == first["certificateId"]
    )
    revoke = client.delete(
        f"/api/v1/fiscal/certificates/{first['certificateId']}",
        headers={**headers, "X-Confirm-Revocation": "REVOKE"},
    )
    assert revoke.status_code == 204
    reactivated = upload(client, headers, good_pfx)
    assert (
        reactivated.status_code == 200
        and reactivated.json()["certificateId"] == first["certificateId"]
    )
    cur = db.cursor()
    cur.execute(
        "SELECT status,revoked_at FROM fiscal_certificates WHERE tenant_id='tenant-a'"
    )
    assert cur.fetchall() == [("ACTIVE", None)]
    cur.execute(
        "SELECT count(*) FILTER (WHERE destroyed_at IS NULL),count(*) FROM fiscal_vault_blobs"
    )
    assert cur.fetchone() == (1, 3)  # só o blob atual sobrevive
    assert vault.get_signing_credential(cur, "tenant-a", vault.keyring())


def test_new_certificate_replaces_active_and_destroys_old_secret(
    db, client, keys, ca, good_pfx
):
    headers = bearer(login(client, "tenant-a"))
    first = upload(client, headers, good_pfx).json()
    second_pfx = make_pfx(keys["leaf2"], make_leaf(keys["leaf2"], keys["ca"], ca), [ca])
    second = upload(client, headers, second_pfx).json()
    assert second["certificateId"] != first["certificateId"]
    cur = db.cursor()
    cur.execute(
        "SELECT id::text,status FROM fiscal_certificates WHERE tenant_id='tenant-a'"
    )
    assert dict(cur.fetchall()) == {
        first["certificateId"]: "REPLACED",
        second["certificateId"]: "ACTIVE",
    }
    cur.execute("SELECT count(*) FROM fiscal_vault_blobs WHERE destroyed_at IS NULL")
    assert cur.fetchone()[0] == 1
    credential = vault.get_signing_credential(cur, "tenant-a", vault.keyring())
    assert str(credential.certificate_id) == second["certificateId"]


@pytest.mark.parametrize(
    ("case", "status", "code"),
    [
        ("wrong_password", 422, "CERTIFICATE_INVALID_PASSWORD"),
        ("other_holder", 422, "CERTIFICATE_HOLDER_MISMATCH"),
        ("expired", 422, "CERTIFICATE_EXPIRED"),
        ("not_pfx", 422, "CERTIFICATE_INVALID"),
        ("too_large", 413, "PAYLOAD_TOO_LARGE"),
        ("wrong_part", 422, "VALIDATION_FAILED"),
        ("content_type", 415, "CERTIFICATE_INVALID"),
    ],
)
def test_rejected_uploads_store_nothing_and_leak_nothing(
    db, client, keys, ca, good_pfx, fiscal_logs, case, status, code
):
    headers = bearer(login(client, "tenant-a"))
    kwargs, data = {}, good_pfx
    if case == "wrong_password":
        kwargs["password"] = "senha-errada-123"
    elif case == "other_holder":
        data = make_pfx(
            keys["leaf"], make_leaf(keys["leaf"], keys["ca"], ca, cnpj=CNPJ_B), [ca]
        )
    elif case == "expired":
        cert = make_leaf(
            keys["leaf"],
            keys["ca"],
            ca,
            start=NOW - timedelta(days=30),
            end=NOW - timedelta(seconds=5),
        )
        data = make_pfx(keys["leaf"], cert, [ca])
    elif case == "not_pfx":
        data = b"%PDF-1.4 nao e certificado"
    elif case == "too_large":
        data = b"\x30" * (vault.PFX_MAX_BYTES + 1)
    elif case == "wrong_part":
        kwargs["part"] = "file"
    elif case == "content_type":
        kwargs["content_type"] = "text/plain"
    response = upload(client, headers, data, **kwargs)
    assert response.status_code == status, response.text
    assert error_of(response)["code"] == code
    secret = kwargs.get("password", PFX_PASSWORD)
    for leaked in (secret, CNPJ_A, CNPJ_B):
        assert leaked not in response.text
        assert leaked not in "\n".join(fiscal_logs)
    cur = db.cursor()
    cur.execute(
        "SELECT (SELECT count(*) FROM fiscal_certificates),(SELECT count(*) FROM fiscal_vault_blobs)"
    )
    assert cur.fetchone() == (0, 0)


def test_ecpf_of_mei_owner_is_accepted_only_with_registered_cpf(db, client, keys, ca):
    headers = bearer(login(client, "tenant-a"))
    cert = make_leaf(
        keys["leaf"],
        keys["ca"],
        ca,
        cnpj=None,
        cpf=CPF_TITULAR,
        cn="FULANO:" + CPF_TITULAR,
    )
    data = make_pfx(keys["leaf"], cert, [ca])
    refused = upload(client, headers, data)
    assert (
        refused.status_code == 422
        and error_of(refused)["code"] == "CERTIFICATE_HOLDER_MISMATCH"
    )
    db.cursor().execute(
        "UPDATE fiscal_establishments SET responsible_cpf=%s,is_mei=TRUE WHERE tenant_id='tenant-a'",
        (CPF_TITULAR,),
    )
    db.commit()
    accepted = upload(client, headers, data)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["holderTaxId"] == CPF_TITULAR


def test_revocation_requires_confirmation_and_destroys_secret(db, client, good_pfx):
    headers = bearer(login(client, "tenant-a"))
    cert_id = upload(client, headers, good_pfx).json()["certificateId"]
    for value in (None, "revoke", "SIM"):
        extra = {"X-Confirm-Revocation": value} if value else {}
        response = client.delete(
            f"/api/v1/fiscal/certificates/{cert_id}", headers={**headers, **extra}
        )
        assert response.status_code == 400
        assert error_of(response)["code"] == "REVOCATION_CONFIRMATION_REQUIRED"
    cur = db.cursor()
    assert vault.get_signing_credential(cur, "tenant-a", vault.keyring())
    db.rollback()
    response = client.delete(
        f"/api/v1/fiscal/certificates/{cert_id}",
        headers={**headers, "X-Confirm-Revocation": "REVOKE"},
    )
    assert response.status_code == 204
    cur = db.cursor()
    cur.execute("SELECT count(*) FROM fiscal_vault_blobs WHERE destroyed_at IS NULL")
    assert cur.fetchone()[0] == 0
    cur.execute(
        "SELECT action FROM fiscal_audit_log WHERE resource_id=%s ORDER BY occurred_at",
        (cert_id,),
    )
    assert [r[0] for r in cur.fetchall()] == [
        "CERTIFICATE_IMPORT",
        "CERTIFICATE_REVOKE",
    ]
    with pytest.raises(ApiProblem) as exc:
        vault.get_signing_credential(cur, "tenant-a", vault.keyring())
    assert exc.value.code == "CERTIFICATE_INVALID"
    status = client.get("/api/v1/fiscal/certificates/status", headers=headers).json()
    assert status == {
        "configured": False,
        "valid": False,
        "certificateId": None,
        "holder": None,
        "expiresAt": None,
        "type": None,
        "lastValidatedAt": None,
        "holderTaxId": None,
    }


def test_tenant_b_cannot_see_or_use_tenant_a_certificate(db, client, good_pfx):
    cert_id = upload(client, bearer(login(client, "tenant-a")), good_pfx).json()[
        "certificateId"
    ]
    as_b = bearer(login(client, "tenant-b"))
    assert (
        client.get("/api/v1/fiscal/certificates/status", headers=as_b).json()[
            "configured"
        ]
        is False
    )
    response = client.delete(
        f"/api/v1/fiscal/certificates/{cert_id}",
        headers={**as_b, "X-Confirm-Revocation": "REVOKE"},
    )
    assert response.status_code == 404
    with pytest.raises(ApiProblem):
        vault.get_signing_credential(db.cursor(), "tenant-b", vault.keyring())


def test_upload_without_vault_key_is_503_without_details(
    db, client, good_pfx, monkeypatch
):
    monkeypatch.setattr(
        vault, "settings", dataclasses.replace(vault.settings, vault_master_keys="")
    )
    response = upload(client, bearer(login(client, "tenant-a")), good_pfx)
    assert response.status_code == 503
    assert error_of(response)["code"] == "FISCAL_DISABLED"
    assert "FISCAL_VAULT" not in response.text


def test_tampered_vault_row_fails_closed(db, client, good_pfx):
    upload(client, bearer(login(client, "tenant-a")), good_pfx)
    cur = db.cursor()
    cur.execute(
        "UPDATE fiscal_vault_blobs SET ciphertext=overlay(ciphertext placing '\\x00'::bytea from 20)"
    )
    db.commit()
    with pytest.raises(vault.VaultIntegrityError):
        vault.get_signing_credential(cur, "tenant-a", vault.keyring())


# --- API: CSC ---------------------------------------------------------------------


def test_csc_goes_only_to_the_vault(db, client, fiscal_logs):
    headers = bearer(login(client, "tenant-a"))
    response = client.put(
        "/api/v1/fiscal/csc",
        headers=headers,
        json={"cscId": "000001", "cscToken": CSC_TOKEN},
    )
    assert response.status_code == 204 and response.content == b""
    cur = db.cursor()
    assert vault.get_csc(cur, "tenant-a", vault.keyring()) == ("000001", CSC_TOKEN)
    new_token = "Z" * 36
    assert (
        client.put(
            "/api/v1/fiscal/csc",
            headers=headers,
            json={"cscId": "2", "cscToken": new_token},
        ).status_code
        == 204
    )
    cur = db.cursor()
    assert vault.get_csc(cur, "tenant-a", vault.keyring()) == ("2", new_token)
    cur.execute(
        "SELECT count(*) FILTER (WHERE destroyed_at IS NULL),count(*) FROM fiscal_vault_blobs WHERE purpose='CSC'"
    )
    assert cur.fetchone() == (1, 2)
    cur.execute("SELECT count(*) FROM fiscal_credentials WHERE tenant_id='tenant-a'")
    assert cur.fetchone()[0] == 1
    rows = _all_rows_text(db)
    assert CSC_TOKEN not in rows and new_token not in rows
    assert CSC_TOKEN not in "\n".join(fiscal_logs) and new_token not in "\n".join(
        fiscal_logs
    )
    with pytest.raises(ApiProblem):
        vault.get_csc(db.cursor(), "tenant-b", vault.keyring())


@pytest.mark.parametrize(
    "body",
    [
        {"cscId": "000000", "cscToken": CSC_TOKEN},
        {"cscId": "1234567", "cscToken": CSC_TOKEN},
        {"cscId": "1", "cscToken": "curto"},
        {"cscId": "1", "cscToken": CSC_TOKEN + "-"},
        {"cscId": "1", "cscToken": CSC_TOKEN, "extra": True},
        {"cscToken": CSC_TOKEN},
    ],
)
def test_invalid_csc_is_422_without_echo(db, client, body):
    response = client.put(
        "/api/v1/fiscal/csc", headers=bearer(login(client, "tenant-a")), json=body
    )
    assert response.status_code == 422
    assert error_of(response)["code"] == "VALIDATION_FAILED"
    assert CSC_TOKEN not in response.text and "curto" not in response.text
    cur = db.cursor()
    cur.execute("SELECT count(*) FROM fiscal_vault_blobs")
    assert cur.fetchone()[0] == 0


def test_csc_requires_config_write(db, client):
    db.cursor().execute(
        "DELETE FROM auth_permissions WHERE tenant_id='tenant-a' AND permission='FISCAL_CONFIG_WRITE'"
    )
    db.commit()
    response = client.put(
        "/api/v1/fiscal/csc",
        headers=bearer(login(client, "tenant-a")),
        json={"cscId": "1", "cscToken": CSC_TOKEN},
    )
    assert (
        response.status_code == 403
        and error_of(response)["code"] == "PERMISSION_DENIED"
    )


# --- mTLS -------------------------------------------------------------------------


def test_mtls_context_uses_private_temp_file_and_caches(
    db, client, good_pfx, monkeypatch
):
    upload(client, bearer(login(client, "tenant-a")), good_pfx)
    credential = vault.get_signing_credential(db.cursor(), "tenant-a", vault.keyring())
    seen = []
    original = ssl.SSLContext.load_cert_chain

    def spy(self, certfile, *args, **kwargs):
        seen.append((certfile, stat.S_IMODE(os.stat(certfile).st_mode)))
        return original(self, certfile, *args, **kwargs)

    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", spy)
    context = vault.mtls_context(credential)
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    ((path, mode),) = seen
    assert mode == 0o600 and not os.path.exists(path)
    assert vault.mtls_context(credential) is context and len(seen) == 1
    vault.forget_mtls([credential.fingerprint])
    assert vault.mtls_context(credential) is not context and len(seen) == 2


def test_unknown_reference_never_decrypts(db):
    with pytest.raises(vault.VaultIntegrityError):
        vault.open_blob(db.cursor(), "tenant-a", uuid4(), "CSC", vault.keyring())

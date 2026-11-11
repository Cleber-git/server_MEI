"""Cofre fiscal (F4): segredos cifrados no Postgres com envelope AES-256-GCM.

- Cada segredo ganha uma DEK aleatória de 256 bits. A DEK é embrulhada pela
  master key ativa (``FISCAL_VAULT_MASTER_KEY``, com ``key_id`` para rotação).
- AAD = versão + tenant + id do blob + finalidade: um blob copiado para outra
  linha ou outro tenant não decifra.
- PFX: decifrado uma vez com a senha do usuário; a chave PKCS#8 e a cadeia são
  recifradas com a DEK. A senha e o PFX nunca são guardados.
- Revogar/substituir destrói o blob (zera DEK embrulhada e cifra).

Nada daqui loga ou devolve segredo, CPF/CNPJ completo ou PEM.
"""

from __future__ import annotations

import logging
import os
import re
import ssl
import struct
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from cryptography import x509
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import pkcs12

from .config import Settings, settings
from .errors import ApiProblem

log = logging.getLogger("fiscal.vault")

PFX_MAX_BYTES = 64 * 1024
ICP_CNPJ_OID = "2.16.76.1.3.3"  # SAN otherName do e-CNPJ: CNPJ da PJ titular
ICP_CPF_OID = "2.16.76.1.3.1"  # SAN otherName do e-CPF: nascimento(8) + CPF(11) + …
ICP_A1_POLICY = "2.16.76.1.2.1"  # arco das políticas A1 da ICP-Brasil (2.16.76.1.2.1.n)
MIN_RSA_BITS = 2048
_AAD_PREFIX = b"caltech-mei/fiscal-vault/v1"
_BUNDLE_MAGIC = b"CMV1"
_CNPJ_RE = re.compile(r"^[0-9A-Z]{12}[0-9]{2}$")
_CPF_RE = re.compile(r"^[0-9]{11}$")


class VaultIntegrityError(Exception):
    """Blob ausente, destruído, adulterado ou cifrado com master key indisponível."""


# --- Master keys ----------------------------------------------------------------


@dataclass(frozen=True)
class Keyring:
    keys: dict[str, bytes]
    active_id: str

    def __repr__(self) -> str:  # nunca expõe material de chave
        return f"Keyring(ids={sorted(self.keys)}, active_id={self.active_id!r})"

    @property
    def active(self) -> bytes:
        return self.keys[self.active_id]


def keyring(current: Settings | None = None) -> Keyring:
    keys, active = (current or settings).vault_keyring()
    if not keys or active not in keys:
        raise ApiProblem(
            503, "FISCAL_DISABLED", "O cofre fiscal não está configurado no servidor."
        )
    return Keyring(keys, active)


def _aad(tenant_id: str, blob_id: UUID, purpose: str) -> bytes:
    return b"|".join(
        (_AAD_PREFIX, tenant_id.encode(), str(blob_id).encode(), purpose.encode())
    )


def seal(cur, tenant_id: str, purpose: str, plaintext: bytes, ring: Keyring) -> UUID:
    """Grava ``plaintext`` cifrado e devolve o id do blob (mesma transação do chamador)."""
    blob_id = uuid4()
    aad = _aad(tenant_id, blob_id, purpose)
    dek = AESGCM.generate_key(bit_length=256)
    nonce, dek_nonce = os.urandom(12), os.urandom(12)
    ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad)
    wrapped = AESGCM(ring.active).encrypt(dek_nonce, dek, aad)
    del dek
    cur.execute(
        """INSERT INTO fiscal_vault_blobs
           (id,tenant_id,purpose,key_id,dek_nonce,wrapped_dek,nonce,ciphertext)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            blob_id,
            tenant_id,
            purpose,
            ring.active_id,
            dek_nonce,
            wrapped,
            nonce,
            ciphertext,
        ),
    )
    return blob_id


def _unwrap(ring: Keyring, key_id: str, dek_nonce, wrapped, aad: bytes) -> bytes:
    master = ring.keys.get(key_id)
    if master is None:
        raise VaultIntegrityError("master key do blob indisponível")
    try:
        return AESGCM(master).decrypt(bytes(dek_nonce), bytes(wrapped), aad)
    except InvalidTag as exc:
        raise VaultIntegrityError("DEK não confere") from exc


def open_blob(cur, tenant_id: str, blob_id, purpose: str, ring: Keyring) -> bytes:
    try:
        blob_uuid = UUID(str(blob_id))
    except ValueError as exc:
        raise VaultIntegrityError("referência de cofre inválida") from exc
    cur.execute(
        """SELECT key_id,dek_nonce,wrapped_dek,nonce,ciphertext FROM fiscal_vault_blobs
           WHERE id=%s AND tenant_id=%s AND purpose=%s AND destroyed_at IS NULL""",
        (blob_uuid, tenant_id, purpose),
    )
    row = cur.fetchone()
    if not row:
        raise VaultIntegrityError("blob ausente ou destruído")
    aad = _aad(tenant_id, blob_uuid, purpose)
    dek = _unwrap(ring, row[0], row[1], row[2], aad)
    try:
        return AESGCM(dek).decrypt(bytes(row[3]), bytes(row[4]), aad)
    except InvalidTag as exc:
        raise VaultIntegrityError("conteúdo não confere") from exc
    finally:
        del dek


def destroy(cur, tenant_id: str, blob_id) -> None:
    """Destrói o segredo (zera DEK e cifra). Referência legada fora do formato é ignorada."""
    try:
        blob_uuid = UUID(str(blob_id))
    except ValueError:
        return
    cur.execute(
        """UPDATE fiscal_vault_blobs SET wrapped_dek=%s,ciphertext=%s,destroyed_at=NOW()
           WHERE id=%s AND tenant_id=%s AND destroyed_at IS NULL""",
        (b"", b"", blob_uuid, tenant_id),
    )


def rewrap_all(conn, ring: Keyring, *, batch_size: int = 100) -> int:
    """Reembrulha com a master key ativa toda DEK de outra chave. Devolve quantos.

    Só a DEK muda (o conteúdo não é recifrado). Um lote por transação, com
    ``SKIP LOCKED``; pode rodar de novo sem efeito. Depois de zerar, a chave
    antiga pode sair de ``FISCAL_VAULT_MASTER_KEY``.
    """
    total = 0
    while True:
        cur = conn.cursor()
        cur.execute(
            """SELECT id,tenant_id,purpose,key_id,dek_nonce,wrapped_dek FROM fiscal_vault_blobs
               WHERE key_id<>%s AND destroyed_at IS NULL
               ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED""",
            (ring.active_id, batch_size),
        )
        rows = cur.fetchall()
        if not rows:
            conn.commit()
            return total
        try:
            for blob_id, tenant_id, purpose, key_id, dek_nonce, wrapped in rows:
                aad = _aad(tenant_id, UUID(str(blob_id)), purpose)
                dek = _unwrap(ring, key_id, dek_nonce, wrapped, aad)
                new_nonce = os.urandom(12)
                new_wrapped = AESGCM(ring.active).encrypt(new_nonce, dek, aad)
                del dek
                cur.execute(
                    """UPDATE fiscal_vault_blobs SET key_id=%s,dek_nonce=%s,wrapped_dek=%s,
                       rotated_at=NOW() WHERE id=%s""",
                    (ring.active_id, new_nonce, new_wrapped, blob_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        total += len(rows)


# --- PKCS#12 ----------------------------------------------------------------------


def _problem(code: str, message: str) -> ApiProblem:
    return ApiProblem(422, code, message)


def _der_length(data: bytes, offset: int) -> tuple[int, int]:
    if offset >= len(data):
        raise ValueError("DER truncado")
    first = data[offset]
    if first < 0x80:
        return first, offset + 1
    count = first & 0x7F
    if count == 0 or count > 4 or offset + 1 + count > len(data):
        raise ValueError("comprimento DER inválido")
    return int.from_bytes(
        data[offset + 1 : offset + 1 + count], "big"
    ), offset + 1 + count


def looks_like_pfx(data: bytes) -> bool:
    """Estrutura externa do PFX (RFC 7292): SEQUENCE { INTEGER 3, … } sem sobra.

    Serve para separar "senha errada" de "arquivo que não é PKCS#12": a
    biblioteca devolve o mesmo erro para os dois.
    """
    try:
        if len(data) < 8 or data[0] != 0x30:
            return False
        length, offset = _der_length(data, 1)
        if offset + length != len(data):
            return False
        return data[offset : offset + 3] == b"\x02\x01\x03"
    except ValueError:
        return False


def _der_text(value: bytes) -> str | None:
    """Texto de um valor DER simples (OCTET/UTF8/Printable/IA5/Visible), com [0] opcional."""
    for _ in range(3):
        try:
            if len(value) < 2:
                return None
            tag = value[0]
            length, offset = _der_length(value, 1)
        except ValueError:
            return None
        if offset + length != len(value):
            return None
        content = value[offset:]
        if tag == 0xA0:
            value = content
            continue
        if tag in (0x04, 0x0C, 0x13, 0x16, 0x1A):
            return content.decode("latin-1")
        return None
    return None


@dataclass(frozen=True)
class Holder:
    kind: str  # CNPJ | CPF
    tax_id: str


def read_holder(cert: x509.Certificate) -> Holder | None:
    """Titular pelo SAN otherName da ICP-Brasil; na falta, pelo sufixo ``:<id>`` do CN."""
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        others = san.get_values_for_type(x509.OtherName)
    except x509.ExtensionNotFound:
        others = []
    by_oid = {}
    for other in others:
        text = _der_text(other.value)
        if text is not None:
            by_oid.setdefault(other.type_id.dotted_string, text.strip())
    cnpj = re.sub(r"[^0-9A-Za-z]", "", by_oid.get(ICP_CNPJ_OID, "")).upper()
    if _CNPJ_RE.match(cnpj) and set(cnpj) != {"0"}:
        return Holder("CNPJ", cnpj)
    cpf_field = by_oid.get(ICP_CPF_OID, "")
    cpf = cpf_field[8:19] if len(cpf_field) >= 19 else ""
    if _CPF_RE.match(cpf) and set(cpf) != {"0"}:
        return Holder("CPF", cpf)
    for attribute in cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME):
        _, sep, suffix = str(attribute.value).rpartition(":")
        suffix = suffix.strip().upper()
        if sep and _CNPJ_RE.match(suffix):
            return Holder("CNPJ", suffix)
        if sep and _CPF_RE.match(suffix):
            return Holder("CPF", suffix)
    return None


def mask_tax_id(value: str | None) -> str:
    if not value:
        return "titular não identificado"
    return "*" * max(0, len(value) - 4) + value[-4:]


@dataclass(frozen=True)
class ParsedCertificate:
    private_key: rsa.RSAPrivateKey
    certificate: x509.Certificate
    chain: tuple[x509.Certificate, ...]  # intermediárias/raiz, da folha para cima
    holder: Holder | None
    fingerprint: str
    valid_from: datetime
    expires_at: datetime

    def __repr__(self) -> str:
        return f"ParsedCertificate(fingerprint={self.fingerprint[:12]}…)"


def _is_ca(cert: x509.Certificate) -> bool:
    try:
        return cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    except x509.ExtensionNotFound:
        return False


def _build_chain(leaf, extras, now) -> tuple[x509.Certificate, ...]:
    """Cadeia consistente a partir da folha: nome do emissor, assinatura e validade."""
    pool = [
        c
        for c in extras
        if c.fingerprint(hashes.SHA256()) != leaf.fingerprint(hashes.SHA256())
    ]
    chain: list[x509.Certificate] = []
    current = leaf
    while current.issuer != current.subject:
        issuer = next((c for c in pool if c.subject == current.issuer), None)
        if issuer is None:
            break
        try:
            current.verify_directly_issued_by(issuer)
        except (InvalidSignature, ValueError, TypeError) as exc:
            raise _problem(
                "CERTIFICATE_INVALID", "A cadeia do certificado não confere."
            ) from exc
        if not _is_ca(issuer) or not (
            issuer.not_valid_before_utc <= now < issuer.not_valid_after_utc
        ):
            raise _problem(
                "CERTIFICATE_INVALID", "A cadeia do certificado não confere."
            )
        chain.append(issuer)
        pool.remove(issuer)
        current = issuer
    return tuple(chain)


def _is_a1(cert: x509.Certificate) -> bool:
    try:
        policies = cert.extensions.get_extension_for_class(
            x509.CertificatePolicies
        ).value
    except x509.ExtensionNotFound:
        return False
    return any(
        p.policy_identifier.dotted_string == ICP_A1_POLICY
        or p.policy_identifier.dotted_string.startswith(ICP_A1_POLICY + ".")
        for p in policies
    )


def parse_pkcs12(
    data: bytes, password: bytearray, *, now: datetime | None = None
) -> ParsedCertificate:
    """Valida o PFX A1 e devolve chave, certificado e cadeia em memória.

    Não confere o titular contra o tenant (ver ``check_holder``). A senha não é
    alterada aqui; quem chama zera o ``bytearray``.
    """
    now = now or datetime.now(timezone.utc)
    if not data or len(data) > PFX_MAX_BYTES or not looks_like_pfx(bytes(data)):
        raise _problem(
            "CERTIFICATE_INVALID", "O arquivo não é um certificado A1 (.pfx) válido."
        )
    try:
        key, cert, extras = pkcs12.load_key_and_certificates(
            bytes(data), bytes(password)
        )
    except (ValueError, TypeError) as exc:
        raise _problem(
            "CERTIFICATE_INVALID_PASSWORD", "A senha do certificado está incorreta."
        ) from exc
    if key is None or cert is None:
        raise _problem("CERTIFICATE_INVALID", "O certificado não traz a chave privada.")
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < MIN_RSA_BITS:
        raise _problem(
            "CERTIFICATE_INVALID",
            "A chave do certificado não é RSA de 2048 bits ou mais.",
        )
    if key.public_key().public_numbers() != cert.public_key().public_numbers():
        raise _problem(
            "CERTIFICATE_INVALID", "A chave privada não corresponde ao certificado."
        )
    if cert.issuer == cert.subject or _is_ca(cert):
        raise _problem(
            "CERTIFICATE_INVALID", "O certificado não é de titular ICP-Brasil."
        )
    if not _is_a1(cert):
        raise _problem(
            "CERTIFICATE_INVALID", "O certificado não é do tipo A1 da ICP-Brasil."
        )
    try:
        usage = cert.extensions.get_extension_for_class(x509.KeyUsage).value
        if not usage.digital_signature:
            raise _problem(
                "CERTIFICATE_INVALID", "O certificado não permite assinatura digital."
            )
    except x509.ExtensionNotFound:
        pass
    valid_from, expires_at = cert.not_valid_before_utc, cert.not_valid_after_utc
    if valid_from > now:
        raise _problem(
            "CERTIFICATE_INVALID", "A validade do certificado ainda não começou."
        )
    if expires_at <= now:
        raise _problem("CERTIFICATE_EXPIRED", "O certificado está vencido.")
    chain = _build_chain(cert, list(extras or ()), now)
    return ParsedCertificate(
        private_key=key,
        certificate=cert,
        chain=chain,
        holder=read_holder(cert),
        fingerprint=cert.fingerprint(hashes.SHA256()).hex(),
        valid_from=valid_from,
        expires_at=expires_at,
    )


def _alnum(value: str | None) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", value or "").upper()


def check_holder(
    parsed: ParsedCertificate,
    *,
    cnpj: str | None,
    is_mei: bool,
    responsible_cpf: str | None,
) -> None:
    """e-CNPJ igual ao CNPJ do estabelecimento, ou e-CPF do titular MEI cadastrado."""
    holder = parsed.holder
    if holder is not None and holder.kind == "CNPJ" and holder.tax_id == _alnum(cnpj):
        return
    if (
        holder is not None
        and holder.kind == "CPF"
        and is_mei
        and responsible_cpf
        and holder.tax_id == _alnum(responsible_cpf)
    ):
        return
    raise _problem(
        "CERTIFICATE_HOLDER_MISMATCH", "O certificado não pertence a esta empresa."
    )


def _pack(parts: list[bytes]) -> bytes:
    return _BUNDLE_MAGIC + b"".join(struct.pack(">I", len(p)) + p for p in parts)


def _unpack(blob: bytes) -> list[bytes]:
    if not blob.startswith(_BUNDLE_MAGIC):
        raise VaultIntegrityError("formato do pacote desconhecido")
    parts, offset = [], len(_BUNDLE_MAGIC)
    while offset < len(blob):
        if offset + 4 > len(blob):
            raise VaultIntegrityError("pacote truncado")
        (size,) = struct.unpack(">I", blob[offset : offset + 4])
        offset += 4
        if offset + size > len(blob):
            raise VaultIntegrityError("pacote truncado")
        parts.append(blob[offset : offset + size])
        offset += size
    return parts


# --- Certificados no banco --------------------------------------------------------


@dataclass(frozen=True)
class StoredCertificate:
    certificate_id: UUID
    reactivated: bool
    replaced_fingerprints: tuple[str, ...]


def store_certificate(
    cur,
    tenant_id: str,
    establishment_id,
    parsed: ParsedCertificate,
    ring: Keyring,
    now: datetime,
) -> StoredCertificate:
    """Recifra chave+cadeia e ativa o certificado; o ativo anterior vira REPLACED.

    O mesmo fingerprint reenviado reativa a linha existente (UNIQUE por tenant).
    Quem chama já travou o estabelecimento (``FOR UPDATE``).
    """
    key_der = parsed.private_key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    bundle = _pack(
        [key_der, parsed.certificate.public_bytes(serialization.Encoding.DER)]
        + [c.public_bytes(serialization.Encoding.DER) for c in parsed.chain]
    )
    blob_id = seal(cur, tenant_id, "CERTIFICATE", bundle, ring)
    del key_der, bundle
    cur.execute(
        """SELECT id,vault_reference,fingerprint,status FROM fiscal_certificates
           WHERE tenant_id=%s AND (fingerprint=%s OR (establishment_id=%s AND status='ACTIVE'))
           FOR UPDATE""",
        (tenant_id, parsed.fingerprint, establishment_id),
    )
    existing, replaced = None, []
    for cert_id, reference, fingerprint, status in cur.fetchall():
        destroy(cur, tenant_id, reference)
        if fingerprint.strip() == parsed.fingerprint:
            existing = cert_id
            continue
        replaced.append(fingerprint.strip())
        cur.execute(
            "UPDATE fiscal_certificates SET status='REPLACED',revoked_at=%s WHERE id=%s AND tenant_id=%s",
            (now, cert_id, tenant_id),
        )
    holder = parsed.holder
    values = (
        str(blob_id),
        format(parsed.certificate.serial_number, "x"),
        mask_tax_id(holder.tax_id if holder else None),
        parsed.certificate.issuer.rfc4514_string()[:500],
        parsed.valid_from,
        parsed.expires_at,
        now,
        holder.tax_id if holder else None,
        holder.kind if holder else None,
    )
    if existing is not None:
        cur.execute(
            """UPDATE fiscal_certificates SET vault_reference=%s,serial_number=%s,holder_masked=%s,
               issuer=%s,valid_from=%s,expires_at=%s,last_validated_at=%s,holder_tax_id=%s,holder_kind=%s,
               establishment_id=%s,status='ACTIVE',revoked_at=NULL WHERE id=%s AND tenant_id=%s""",
            values + (establishment_id, existing, tenant_id),
        )
        return StoredCertificate(UUID(str(existing)), True, tuple(replaced))
    cert_id = uuid4()
    cur.execute(
        """INSERT INTO fiscal_certificates(id,tenant_id,establishment_id,fingerprint,type,status,
           vault_reference,serial_number,holder_masked,issuer,valid_from,expires_at,last_validated_at,
           holder_tax_id,holder_kind) VALUES(%s,%s,%s,%s,'A1','ACTIVE',%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (cert_id, tenant_id, establishment_id, parsed.fingerprint) + values,
    )
    return StoredCertificate(cert_id, False, tuple(replaced))


@dataclass(frozen=True)
class SigningCredential:
    certificate_id: UUID
    private_key: rsa.RSAPrivateKey
    certificate: x509.Certificate
    chain: tuple[x509.Certificate, ...]
    fingerprint: str
    expires_at: datetime

    def __repr__(self) -> str:
        return f"SigningCredential(fingerprint={self.fingerprint[:12]}…)"


def get_signing_credential(
    cur, tenant_id: str, ring: Keyring, *, now: datetime | None = None
) -> SigningCredential:
    """Chave e cadeia do certificado ativo do tenant, só em memória."""
    now = now or datetime.now(timezone.utc)
    cur.execute(
        """SELECT id,vault_reference,fingerprint,valid_from,expires_at FROM fiscal_certificates
           WHERE tenant_id=%s AND status='ACTIVE' ORDER BY created_at DESC LIMIT 1""",
        (tenant_id,),
    )
    row = cur.fetchone()
    if not row:
        raise _problem(
            "CERTIFICATE_INVALID", "Envie o certificado digital A1 da empresa."
        )
    cert_id, reference, fingerprint, valid_from, expires_at = row
    if not (valid_from <= now < expires_at):
        raise _problem("CERTIFICATE_EXPIRED", "O certificado está vencido.")
    parts = _unpack(open_blob(cur, tenant_id, reference, "CERTIFICATE", ring))
    if len(parts) < 2:
        raise VaultIntegrityError("pacote sem chave ou certificado")
    key = serialization.load_der_private_key(parts[0], password=None)
    cert = x509.load_der_x509_certificate(parts[1])
    if cert.fingerprint(hashes.SHA256()).hex() != fingerprint.strip():
        raise VaultIntegrityError("certificado do cofre diferente do cadastrado")
    return SigningCredential(
        certificate_id=UUID(str(cert_id)),
        private_key=key,
        certificate=cert,
        chain=tuple(x509.load_der_x509_certificate(p) for p in parts[2:]),
        fingerprint=fingerprint.strip(),
        expires_at=expires_at,
    )


# --- mTLS -------------------------------------------------------------------------

_MTLS_MAX_AGE_SECONDS = 3600
_mtls_cache: dict[str, tuple[float, datetime, ssl.SSLContext]] = {}
_mtls_lock = threading.Lock()


def _private_tmp_dir() -> str | None:
    shm = "/dev/shm"  # memória, quando existe
    return shm if os.path.isdir(shm) and os.access(shm, os.W_OK) else None


def mtls_context(
    credential: SigningCredential, *, cafile: str | None = None
) -> ssl.SSLContext:
    """``SSLContext`` cliente com o certificado do tenant, em cache por fingerprint.

    A chave passa por um arquivo temporário 0600 que é apagado logo após o
    ``load_cert_chain``. O cache expira com o certificado ou em 1 h, o que vier
    antes (a revogação vale para o próximo ``get_signing_credential``).
    """
    now = datetime.now(timezone.utc)
    cache_key = credential.fingerprint + "|" + (cafile or "")
    with _mtls_lock:
        hit = _mtls_cache.get(cache_key)
        if hit and time.monotonic() - hit[0] < _MTLS_MAX_AGE_SECONDS and hit[1] > now:
            return hit[2]
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=cafile)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    pem = credential.private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ) + b"".join(
        c.public_bytes(serialization.Encoding.PEM)
        for c in (credential.certificate, *credential.chain)
    )
    fd, path = tempfile.mkstemp(
        prefix="cm-mtls-", suffix=".pem", dir=_private_tmp_dir()
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(pem)
        context.load_cert_chain(path)
    finally:
        del pem
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    with _mtls_lock:
        _mtls_cache[cache_key] = (time.monotonic(), credential.expires_at, context)
    return context


def forget_mtls(fingerprints) -> None:
    wanted = set(fingerprints)
    with _mtls_lock:
        for key in [k for k in _mtls_cache if k.split("|", 1)[0] in wanted]:
            del _mtls_cache[key]


# --- CSC (NFC-e) ------------------------------------------------------------------

CSC_CREDENTIAL = "NFCE_CSC"


def store_csc(
    cur, tenant_id: str, establishment_id, csc_id: str, csc_token: str, ring: Keyring
) -> bool:
    """Guarda o CSC no cofre; devolve True se substituiu um anterior."""
    payload = _pack([csc_id.encode("ascii"), csc_token.encode("ascii")])
    blob_id = seal(cur, tenant_id, "CSC", payload, ring)
    del payload
    cur.execute(
        """SELECT id,vault_reference FROM fiscal_credentials
           WHERE tenant_id=%s AND establishment_id=%s AND credential_type=%s FOR UPDATE""",
        (tenant_id, establishment_id, CSC_CREDENTIAL),
    )
    row = cur.fetchone()
    if row:
        destroy(cur, tenant_id, row[1])
        cur.execute(
            """UPDATE fiscal_credentials SET vault_reference=%s,masked_identifier=%s,status='ACTIVE',
               revoked_at=NULL WHERE id=%s AND tenant_id=%s""",
            (str(blob_id), csc_id, row[0], tenant_id),
        )
        return True
    cur.execute(
        """INSERT INTO fiscal_credentials(id,tenant_id,establishment_id,credential_type,vault_reference,
           masked_identifier,status) VALUES(%s,%s,%s,%s,%s,%s,'ACTIVE')""",
        (uuid4(), tenant_id, establishment_id, CSC_CREDENTIAL, str(blob_id), csc_id),
    )
    return False


def get_csc(cur, tenant_id: str, ring: Keyring) -> tuple[str, str]:
    """``(cscId, cscToken)`` do tenant, só em memória (para o QR da NFC-e, F11)."""
    cur.execute(
        """SELECT vault_reference FROM fiscal_credentials
           WHERE tenant_id=%s AND credential_type=%s AND status='ACTIVE' LIMIT 1""",
        (tenant_id, CSC_CREDENTIAL),
    )
    row = cur.fetchone()
    if not row:
        raise _problem("ESTABLISHMENT_INCOMPLETE", "Cadastre o CSC da NFC-e.")
    parts = _unpack(open_blob(cur, tenant_id, row[0], "CSC", ring))
    if len(parts) != 2:
        raise VaultIntegrityError("pacote de CSC inválido")
    return parts[0].decode("ascii"), parts[1].decode("ascii")

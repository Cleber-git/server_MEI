from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509 import OtherName
from cryptography.x509.oid import ObjectIdentifier

from .config import settings
from .errors import ApiProblem


CNPJ_OID = "2.16.76.1.3.3"


def _der_text_values(value: bytes) -> list[str]:
    values: list[str] = []
    offset = 0
    while offset + 2 <= len(value):
        tag = value[offset]
        offset += 1
        first_length = value[offset]
        offset += 1
        if first_length & 0x80:
            count = first_length & 0x7F
            if count == 0 or count > 4 or offset + count > len(value):
                break
            length = int.from_bytes(value[offset : offset + count], "big")
            offset += count
        else:
            length = first_length
        end = offset + length
        if end > len(value):
            break
        content = value[offset:end]
        if tag in {0x0C, 0x13, 0x14, 0x16, 0x1E}:
            try:
                values.append(content.decode("utf-16-be" if tag == 0x1E else "utf-8"))
            except UnicodeDecodeError:
                pass
        if tag & 0x20:
            values.extend(_der_text_values(content))
        offset = end
    return values


def _cnpj_check_digits(value: str) -> bool:
    if len(value) != 14 or not value.isalnum() or not value.isupper():
        return False
    numbers = [ord(char) - 48 for char in value]
    for length, weights in (
        (12, (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)),
        (13, (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)),
    ):
        remainder = sum(n * w for n, w in zip(numbers[:length], weights)) % 11
        expected = 0 if remainder < 2 else 11 - remainder
        if numbers[length] != expected:
            return False
    return True


def _mask_tax_id(value: str | None) -> str:
    if not value:
        return "titular não identificado"
    return "*" * max(0, len(value) - 4) + value[-4:]


class LocalEncryptedCertificateVault:
    """Encrypted private vault for single-node deployments; key comes only from secret env."""

    def __init__(self):
        if not settings.local_vault_key:
            raise ApiProblem(
                503,
                "CERTIFICATE_INVALID",
                "Cofre indisponível",
                "Cofre/KMS não configurado",
            )
        try:
            self.cipher = Fernet(settings.local_vault_key.encode("ascii"))
        except ValueError:
            raise ApiProblem(
                503,
                "CERTIFICATE_INVALID",
                "Cofre indisponível",
                "Chave do cofre inválida",
            )
        self.root = Path(settings.local_vault_root).resolve()

    def import_pkcs12(self, tenant_id, certificate, password):
        if not certificate or certificate[0] != 0x30:
            raise ApiProblem(
                422,
                "CERTIFICATE_INVALID",
                "Certificado inválido",
                "Conteúdo não é PKCS#12 válido",
            )
        try:
            key, cert, extras = pkcs12.load_key_and_certificates(
                certificate, password.encode("utf-8")
            )
        except (ValueError, TypeError):
            raise ApiProblem(
                422,
                "CERTIFICATE_WRONG_PASSWORD",
                "Certificado inválido",
                "Senha do certificado incorreta ou arquivo PKCS#12 ilegível",
            )
        if not key or not cert:
            raise ApiProblem(
                422,
                "CERTIFICATE_INVALID",
                "Certificado inválido",
                "PKCS#12 A1 sem chave privada ou certificado",
            )
        now = datetime.now(timezone.utc)
        valid_from = cert.not_valid_before_utc
        expires = cert.not_valid_after_utc
        if valid_from > now:
            raise ApiProblem(
                422,
                "CERTIFICATE_INVALID",
                "Certificado ainda não válido",
                "O período de validade ainda não começou",
            )
        if expires <= now:
            raise ApiProblem(
                422,
                "CERTIFICATE_EXPIRED",
                "Certificado expirado",
                "O certificado está expirado",
            )
        cnpj = None
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        except x509.ExtensionNotFound:
            san = ()
        for name in san:
            if isinstance(name, OtherName) and name.type_id == ObjectIdentifier(CNPJ_OID):
                for text_value in _der_text_values(name.value):
                    candidate = re.sub(r"[^0-9A-Za-z]", "", text_value).upper()
                    if len(candidate) >= 14 and _cnpj_check_digits(candidate[-14:]):
                        cnpj = candidate[-14:]
                        break
        if not cnpj:
            raise ApiProblem(
                422,
                "CERTIFICATE_INVALID",
                "Certificado inválido",
                "CNPJ válido não encontrado no SubjectAltName ICP-Brasil",
            )
        fingerprint = cert.fingerprint(hashes.SHA256()).hex()
        reference = str(uuid4())
        tenant_dir = (
            self.root / hashlib.sha256(tenant_id.encode()).hexdigest()
        ).resolve()
        if self.root not in tenant_dir.parents:
            raise ApiProblem(
                500, "INTERNAL_ERROR", "Erro interno", "Destino do cofre inválido"
            )
        tenant_dir.mkdir(parents=True, exist_ok=True)
        target = (tenant_dir / (reference + ".vault")).resolve()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(target, flags, 0o600)
        try:
            os.write(fd, self.cipher.encrypt(certificate))
        finally:
            os.close(fd)
        return {
            "reference": reference,
            "fingerprint": fingerprint,
            "serial": format(cert.serial_number, "x"),
            "holder": _mask_tax_id(cnpj),
            "holderTaxId": cnpj,
            "issuer": cert.issuer.rfc4514_string()[:500],
            "validFrom": valid_from,
            "expiresAt": expires,
        }

    def revoke(self, tenant_id, reference):
        tenant_dir = (
            self.root / hashlib.sha256(tenant_id.encode()).hexdigest()
        ).resolve()
        target = (tenant_dir / (reference + ".vault")).resolve()
        if tenant_dir not in target.parents:
            raise ApiProblem(
                500, "INTERNAL_ERROR", "Erro interno", "Referência inválida"
            )
        if target.exists():
            revoked = target.with_suffix(".revoked")
            os.replace(target, revoked)

    def get_signing_credential(self, tenant_id, reference):
        raise ApiProblem(
            503,
            "CAPABILITY_NOT_AVAILABLE",
            "Assinatura indisponível",
            "Provider oficial não homologado",
        )

    def get_metadata(self, tenant_id, reference):
        return {"reference": reference}

    def replace(self, tenant_id, reference, certificate, password):
        self.revoke(tenant_id, reference)
        return self.import_pkcs12(tenant_id, certificate, password)

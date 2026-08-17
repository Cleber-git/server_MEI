from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import pkcs12

from .config import settings
from .errors import ApiProblem


CNPJ_OID = "2.16.76.1.3.3"


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
                "CERTIFICATE_INVALID",
                "Certificado inválido",
                "Arquivo ou senha PKCS#12 inválidos",
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
        for attribute in cert.subject:
            if attribute.oid.dotted_string == CNPJ_OID:
                candidate = re.sub(r"[^0-9A-Za-z]", "", attribute.value)
                cnpj = candidate[-14:] if len(candidate) >= 14 else candidate
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

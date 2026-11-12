from __future__ import annotations

import base64
import binascii
import os
import re
from dataclasses import dataclass

_VAULT_KID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() == "true"


@dataclass(frozen=True)
class Settings:
    jwt_issuer: str = os.getenv("JWT_ISSUER", "https://server-mei.local")
    jwt_audience: str = os.getenv("JWT_AUDIENCE", "caltech-mei-android")
    jwt_keys: str = os.getenv("JWT_SIGNING_KEYS", "")
    jwt_active_kid: str = os.getenv("JWT_ACTIVE_KID", "")
    access_ttl: int = int(os.getenv("JWT_ACCESS_TTL_SECONDS", "900"))
    refresh_ttl: int = int(os.getenv("JWT_REFRESH_TTL_SECONDS", "2592000"))
    fiscal_enabled: bool = _bool("FISCAL_FEATURE_ENABLED")
    production_enabled: bool = _bool("FISCAL_PRODUCTION_ENABLED")
    max_upload_bytes: int = int(os.getenv("FISCAL_MAX_UPLOAD_BYTES", "65536"))
    job_max_attempts: int = int(os.getenv("FISCAL_JOB_MAX_ATTEMPTS", "8"))
    vault_provider: str = os.getenv("VAULT_PROVIDER", "disabled")
    # Provider dos documentos (F7): "unavailable" (padrão, nunca transmite), "fake"
    # (autorizador em memória, só para backend local/testes; recusa produção) ou "sefin"
    # (F8: NFS-e pela SEFIN Nacional, travado em produção restrita; demais tipos: unavailable).
    fiscal_provider: str = os.getenv("FISCAL_PROVIDER", "unavailable").strip().lower()
    # F8: timeout de cada chamada à SEFIN (5–60 s, abaixo do lease do worker) e CA opcional
    # para validar o servidor da SEFIN (padrão: certifi).
    nfse_timeout_seconds: float = min(
        60.0, max(5.0, float(os.getenv("FISCAL_NFSE_TIMEOUT_SECONDS", "30")))
    )
    nfse_ca_file: str = os.getenv("FISCAL_NFSE_CA_FILE", "").strip()
    # Worker in-process (F7): sobe no startup quando o fiscal está ligado.
    worker_enabled: bool = _bool("FISCAL_WORKER_ENABLED", True)
    worker_poll_seconds: float = float(os.getenv("FISCAL_WORKER_POLL_SECONDS", "2"))
    # Cofre (F4): "kid:base64(32 bytes)[,kid:base64…]" e o kid ativo.
    vault_master_keys: str = os.getenv("FISCAL_VAULT_MASTER_KEY", "")
    vault_active_key_id: str = os.getenv("FISCAL_VAULT_ACTIVE_KEY_ID", "")

    def signing_keys(self) -> dict[str, str]:
        pairs = [
            entry.split(":", 1) for entry in self.jwt_keys.split(",") if ":" in entry
        ]
        return {
            kid.strip(): secret.strip()
            for kid, secret in pairs
            if kid.strip() and len(secret.strip()) >= 32
        }

    def vault_keyring(self) -> tuple[dict[str, bytes], str]:
        """Master keys do cofre e o kid ativo. Entrada inválida é descartada.

        Sem ``FISCAL_VAULT_ACTIVE_KEY_ID`` e com uma chave só, ela é a ativa.
        As mensagens de erro de quem chama nunca trazem o valor das chaves.
        """
        keys: dict[str, bytes] = {}
        for entry in self.vault_master_keys.split(","):
            kid, sep, encoded = entry.strip().partition(":")
            if not sep or not _VAULT_KID_RE.match(kid):
                continue
            raw = encoded.strip()
            try:
                material = base64.b64decode(
                    raw.replace("-", "+").replace("_", "/") + "=" * (-len(raw) % 4),
                    validate=True,
                )
            except (binascii.Error, ValueError):
                continue
            if len(material) == 32:
                keys[kid] = material
        active = self.vault_active_key_id.strip()
        if not active and len(keys) == 1:
            active = next(iter(keys))
        return keys, active


settings = Settings()


def assert_startup_ready(current: Settings | None = None) -> None:
    """Com o fiscal ligado, o servidor não sobe sem chave JWT (F2) e do cofre (F4).

    Segredo com menos de 32 bytes é descartado por ``signing_keys()``, então
    também barra aqui. A mensagem nunca traz o valor das chaves.
    """
    current = current or settings
    if not current.fiscal_enabled:
        return
    keys = current.signing_keys()
    if not keys:
        raise RuntimeError(
            "FISCAL_FEATURE_ENABLED=true exige JWT_SIGNING_KEYS no formato "
            "kid:segredo, com segredo de pelo menos 32 bytes"
        )
    if current.jwt_active_kid not in keys:
        raise RuntimeError(
            "FISCAL_FEATURE_ENABLED=true exige JWT_ACTIVE_KID igual a um kid de "
            "JWT_SIGNING_KEYS"
        )
    if current.fiscal_provider not in ("unavailable", "fake", "sefin"):
        raise RuntimeError("FISCAL_PROVIDER aceita só unavailable, fake ou sefin")
    if current.nfse_ca_file and not os.path.isfile(current.nfse_ca_file):
        raise RuntimeError("FISCAL_NFSE_CA_FILE aponta para um arquivo que não existe")
    if current.fiscal_provider == "fake" and current.production_enabled:
        raise RuntimeError(
            "FISCAL_PROVIDER=fake não pode rodar com FISCAL_PRODUCTION_ENABLED=true"
        )
    vault_keys, vault_active = current.vault_keyring()
    if not vault_keys or vault_active not in vault_keys:
        raise RuntimeError(
            "FISCAL_FEATURE_ENABLED=true exige FISCAL_VAULT_MASTER_KEY no formato "
            "kid:base64 de 32 bytes e FISCAL_VAULT_ACTIVE_KEY_ID igual a um desses kids"
        )

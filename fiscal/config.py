from __future__ import annotations

import os
from dataclasses import dataclass


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
    max_upload_bytes: int = int(os.getenv("FISCAL_MAX_UPLOAD_BYTES", "5242880"))
    job_max_attempts: int = int(os.getenv("FISCAL_JOB_MAX_ATTEMPTS", "8"))
    artifact_root: str = os.getenv("FISCAL_ARTIFACT_ROOT", "")
    vault_provider: str = os.getenv("VAULT_PROVIDER", "disabled")
    kms_key_reference: str = os.getenv("KMS_KEY_REFERENCE", "")
    local_vault_key: str = os.getenv("FISCAL_LOCAL_VAULT_KEY", "")
    local_vault_root: str = os.getenv("FISCAL_LOCAL_VAULT_ROOT", "fiscal-vault")

    def signing_keys(self) -> dict[str, str]:
        pairs = [
            entry.split(":", 1) for entry in self.jwt_keys.split(",") if ":" in entry
        ]
        return {
            kid.strip(): secret.strip()
            for kid, secret in pairs
            if kid.strip() and len(secret.strip()) >= 32
        }


settings = Settings()

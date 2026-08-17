from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from .config import settings
from .errors import ApiProblem


class LocalEncryptedArtifactStorage:
    """Private encrypted immutable storage for a single persistent node."""

    def __init__(self):
        if not settings.local_vault_key or not settings.artifact_root:
            raise ApiProblem(
                503,
                "ARTIFACT_NOT_AVAILABLE",
                "Storage indisponível",
                "Storage privado/KMS não configurado",
            )
        try:
            self.cipher = Fernet(settings.local_vault_key.encode("ascii"))
        except ValueError as exc:
            raise ApiProblem(
                503,
                "ARTIFACT_NOT_AVAILABLE",
                "Storage indisponível",
                "Chave do storage inválida",
            ) from exc
        self.root = Path(settings.artifact_root).resolve()

    def _tenant_root(self, tenant_id: str) -> Path:
        result = (self.root / hashlib.sha256(tenant_id.encode()).hexdigest()).resolve()
        if self.root not in result.parents:
            raise ApiProblem(
                500, "INTERNAL_ERROR", "Erro interno", "Caminho de storage inválido"
            )
        return result

    def put_immutable(self, tenant_id, document_id, kind, content, content_type):
        digest = hashlib.sha256(content).hexdigest()
        folder = (
            self._tenant_root(tenant_id)
            / hashlib.sha256(str(document_id).encode()).hexdigest()
        ).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        storage_key = f"{folder.relative_to(self.root).as_posix()}/{kind}-{uuid4()}.bin"
        target = (self.root / storage_key).resolve()
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, self.cipher.encrypt(content))
        finally:
            os.close(fd)
        return {
            "storageKey": storage_key,
            "sha256": digest,
            "size": len(content),
            "contentType": content_type,
        }

    def get_authorized(self, tenant_id, storage_key):
        target = (self.root / storage_key).resolve()
        tenant_root = self._tenant_root(tenant_id)
        if tenant_root not in target.parents:
            raise ApiProblem(
                404,
                "ARTIFACT_NOT_AVAILABLE",
                "Artefato indisponível",
                "Artefato não encontrado",
            )
        try:
            return self.cipher.decrypt(target.read_bytes())
        except (OSError, InvalidToken):
            raise ApiProblem(
                404,
                "ARTIFACT_NOT_AVAILABLE",
                "Artefato indisponível",
                "Artefato não encontrado",
            )

    def verify_hash(self, tenant_id, storage_key, expected):
        return (
            hashlib.sha256(self.get_authorized(tenant_id, storage_key)).hexdigest()
            == expected
        )

    def create_temporary_download(self, tenant_id, storage_key):
        raise ApiProblem(
            503,
            "ARTIFACT_NOT_AVAILABLE",
            "Download indisponível",
            "URLs assinadas exigem object storage configurado",
        )

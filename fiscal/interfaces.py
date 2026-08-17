from __future__ import annotations

from typing import Protocol


class FiscalProvider(Protocol):
    def validate_configuration(self, context: dict) -> dict: ...
    def get_capabilities(self, context: dict) -> dict: ...
    def submit(self, document: dict) -> dict: ...
    def query(self, document: dict) -> dict: ...
    def cancel(self, document: dict, reason: str) -> dict: ...
    def reconcile(self, document: dict) -> dict: ...
    def get_official_artifacts(self, document: dict) -> list[dict]: ...


class CertificateVault(Protocol):
    def import_pkcs12(
        self, tenant_id: str, certificate: bytes, password: str
    ) -> dict: ...
    def get_signing_credential(self, tenant_id: str, reference: str): ...
    def get_metadata(self, tenant_id: str, reference: str) -> dict: ...
    def replace(
        self, tenant_id: str, reference: str, certificate: bytes, password: str
    ) -> dict: ...
    def revoke(self, tenant_id: str, reference: str) -> None: ...


class FiscalArtifactStorage(Protocol):
    def put_immutable(
        self,
        tenant_id: str,
        document_id: str,
        kind: str,
        content: bytes,
        content_type: str,
    ) -> dict: ...
    def get_authorized(self, tenant_id: str, storage_key: str) -> bytes: ...
    def create_temporary_download(self, tenant_id: str, storage_key: str) -> str: ...
    def verify_hash(self, tenant_id: str, storage_key: str, expected: str) -> bool: ...


class FiscalJobQueue(Protocol):
    def enqueue_validation(
        self, tenant_id: str, document_id: str, correlation_id: str
    ) -> str: ...
    def enqueue_submission(
        self, tenant_id: str, document_id: str, correlation_id: str
    ) -> str: ...
    def enqueue_reconciliation(
        self, tenant_id: str, document_id: str, correlation_id: str
    ) -> str: ...
    def enqueue_cancellation(
        self, tenant_id: str, document_id: str, correlation_id: str
    ) -> str: ...


class UnavailableProvider:
    """Safe provider: never claims or simulates government authorization."""

    reason = "Integração oficial não homologada/configurada"

    def validate_configuration(self, context):
        return {"available": False, "reason": self.reason}

    def get_capabilities(self, context):
        return {"available": False, "reason": self.reason}

    def submit(self, document):
        raise RuntimeError(self.reason)

    def query(self, document):
        raise RuntimeError(self.reason)

    def cancel(self, document, reason):
        raise RuntimeError(self.reason)

    def reconcile(self, document):
        raise RuntimeError(self.reason)

    def get_official_artifacts(self, document):
        return []

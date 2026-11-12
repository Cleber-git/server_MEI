"""Providers fiscais usados pelo worker (F7).

Um provider fala com o autorizador (SEFIN/SEFAZ) e devolve um ``Outcome`` sem decidir
estado: quem aplica a máquina de estados é o worker (``fiscal.worker``).

- ``UnavailableProvider`` (padrão): nunca transmite e nunca simula autorização.
- ``FakeProvider``: autorizador em memória para testes e para o backend local da F9
  (``FISCAL_PROVIDER=fake``). Recusa ``PRODUCTION`` e marca tudo como falso.
- ``SefinNacionalProvider`` (F8, ``fiscal.sefin``, ``FISCAL_PROVIDER=sefin``): NFS-e pela SEFIN
  Nacional, só em produção restrita. ``SefazNfceProvider`` (F11) e o da NF-e (F13) entram
  implementando o mesmo protocolo.

Regras do protocolo:

- ``TIMEOUT``: o pedido pode ter chegado. O worker **consulta** antes de reenviar.
- ``TEMPORARY``: o pedido comprovadamente **não** chegou (conexão recusada, 503 antes de
  aceitar). Pode reenviar com backoff.
- ``NOT_SENT``: nada foi transmitido por um motivo que não se resolve sozinho
  (configuração, provider indisponível). Sem retry automático.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol


class OutcomeKind(str, Enum):
    AUTHORIZED = "AUTHORIZED"
    REJECTED = "REJECTED"
    PROCESSING = "PROCESSING"
    NOT_FOUND = "NOT_FOUND"
    CANCELLED = "CANCELLED"
    CANCEL_REJECTED = "CANCEL_REJECTED"
    TIMEOUT = "TIMEOUT"
    TEMPORARY = "TEMPORARY"
    NOT_SENT = "NOT_SENT"


@dataclass(frozen=True)
class Outcome:
    kind: OutcomeKind
    official_key: str | None = None
    protocol: str | None = None
    authorized_at: datetime | None = None
    xml: bytes | None = None
    auxiliary_pdf: bytes | None = None
    event_xml: bytes | None = None
    official_code: str | None = None
    official_message: str | None = None
    retry_after: int | None = None

    def __repr__(self) -> str:  # nunca mostra XML/PDF
        return f"Outcome({self.kind.value}, officialCode={self.official_code!r})"


@dataclass(frozen=True)
class DocumentContext:
    """O que o provider recebe. ``payload`` é a intenção validada (dict JSON)."""

    id: str
    tenant_id: str
    document_type: str
    environment: str
    series: str
    number: int
    payload: dict
    official_key: str | None = None
    substitutes_official_key: str | None = None
    # F8: só para providers com ``needs_issuer`` (o worker carrega do cadastro e do cofre).
    created_at: datetime | None = None
    issuer: dict | None = None
    credential: object | None = None
    credential_error: str | None = None

    def __repr__(self) -> str:  # sem payload, cadastro nem credencial
        return f"DocumentContext(id={self.id}, type={self.document_type})"


class FiscalProvider(Protocol):
    name: str
    supports_substitution: bool

    def submit(self, doc: DocumentContext) -> Outcome: ...
    def query(self, doc: DocumentContext) -> Outcome: ...
    def cancel(self, doc: DocumentContext, reason: str) -> Outcome: ...


class UnavailableProvider:
    """Padrão seguro: nunca transmite nem simula autorização governamental."""

    name = "unavailable"
    supports_substitution = False

    def submit(self, doc):
        return Outcome(OutcomeKind.NOT_SENT, official_code="PROVIDER_UNAVAILABLE")

    def query(self, doc):
        # Nada foi transmitido por este provider: não há o que consultar.
        return Outcome(OutcomeKind.NOT_FOUND)

    def cancel(self, doc, reason):
        return Outcome(OutcomeKind.NOT_SENT, official_code="PROVIDER_UNAVAILABLE")


class SimulatedCrash(BaseException):
    """Simula o processo morrendo no meio (não é capturado como ``Exception``)."""


@dataclass
class _FakeRecord:
    status: str  # AUTHORIZED | REJECTED | PROCESSING | CANCELLED
    pending_polls: int = 0
    authorized_at: datetime | None = None


@dataclass
class FakeProvider:
    """Autorizador em memória. ``script`` = comportamentos consumidos em ordem por operação.

    submit: ``authorize`` (padrão), ``reject``, ``processing``, ``timeout_after_receipt``,
    ``timeout_before_receipt``, ``temporary``, ``incomplete``, ``crash_after_receipt``.
    query: ``timeout``, ``temporary`` (senão reflete o que foi recebido).
    cancel: ``cancel`` (padrão), ``cancel_reject``, ``timeout_after_receipt``, ``temporary``.
    """

    script: dict[str, list[str]] = field(default_factory=dict)
    processing_polls: int = 1
    name: str = "fake"
    supports_substitution: bool = True
    records: dict[str, _FakeRecord] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def count(self, operation: str, doc_id: str | None = None) -> int:
        return sum(
            1
            for op, ident in self.calls
            if op == operation and (doc_id is None or ident == doc_id)
        )

    def _next(self, operation: str, default: str) -> str:
        queue = self.script.get(operation) or []
        return queue.pop(0) if queue else default

    @staticmethod
    def official_key(doc: DocumentContext) -> str:
        digest = hashlib.sha256(("fake:" + doc.id).encode()).hexdigest()
        return str(int(digest, 16))[:50].rjust(50, "0")

    def _authorized(self, doc: DocumentContext, record: _FakeRecord) -> Outcome:
        key = self.official_key(doc)
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<FakeAuthorization fake="true" environment="{doc.environment}" '
            f'documentType="{doc.document_type}" series="{doc.series}" '
            f'number="{doc.number}" key="{key}"/>'
        ).encode()
        return Outcome(
            OutcomeKind.AUTHORIZED,
            official_key=key,
            protocol=f"FAKE{doc.number:015d}",
            authorized_at=record.authorized_at,
            xml=xml,
            auxiliary_pdf=b"%PDF-1.4\n% documento falso (FakeProvider)\n%%EOF\n",
        )

    def _receive(self, doc: DocumentContext, status: str, polls: int = 0):
        record = _FakeRecord(status, polls, datetime.now(timezone.utc))
        self.records[doc.id] = record
        return record

    def submit(self, doc):
        with self._lock:
            self.calls.append(("submit", doc.id))
            if doc.environment != "RESTRICTED_PRODUCTION":
                return Outcome(
                    OutcomeKind.NOT_SENT, official_code="FAKE_NOT_PRODUCTION"
                )
            existing = self.records.get(doc.id)
            if existing is not None:
                # Autorizador real recusa DPS/chave repetida; aqui devolve o que já tem.
                return self._reflect(doc, existing)
            behavior = self._next("submit", "authorize")
            if behavior == "temporary":
                return Outcome(OutcomeKind.TEMPORARY, retry_after=5)
            if behavior == "timeout_before_receipt":
                return Outcome(OutcomeKind.TIMEOUT)
            if behavior == "reject":
                self._receive(doc, "REJECTED")
                return self._reflect(doc, self.records[doc.id])
            if behavior == "processing":
                self._receive(doc, "PROCESSING", self.processing_polls)
                return Outcome(OutcomeKind.PROCESSING, retry_after=2)
            record = self._receive(doc, "AUTHORIZED")
            if behavior == "timeout_after_receipt":
                return Outcome(OutcomeKind.TIMEOUT)
            if behavior == "crash_after_receipt":
                raise SimulatedCrash()
            if behavior == "incomplete":
                full = self._authorized(doc, record)
                return Outcome(
                    OutcomeKind.AUTHORIZED,
                    official_key=full.official_key,
                    authorized_at=full.authorized_at,
                    xml=full.xml,
                )
            return self._authorized(doc, record)

    def _reflect(self, doc, record: _FakeRecord) -> Outcome:
        if record.status == "PROCESSING":
            if record.pending_polls > 0:
                record.pending_polls -= 1
                return Outcome(OutcomeKind.PROCESSING, retry_after=2)
            record.status = "AUTHORIZED"
            record.authorized_at = datetime.now(timezone.utc)
        if record.status == "REJECTED":
            return Outcome(
                OutcomeKind.REJECTED,
                official_code="E999",
                official_message="Rejeição simulada pelo FakeProvider.",
            )
        if record.status == "CANCELLED":
            return Outcome(
                OutcomeKind.CANCELLED,
                protocol=f"FAKECANC{doc.number:011d}",
                event_xml=b'<?xml version="1.0" encoding="UTF-8"?><FakeCancellation fake="true"/>',
            )
        return self._authorized(doc, record)

    def query(self, doc):
        with self._lock:
            self.calls.append(("query", doc.id))
            behavior = self._next("query", "reflect")
            if behavior == "timeout":
                return Outcome(OutcomeKind.TIMEOUT)
            if behavior == "temporary":
                return Outcome(OutcomeKind.TEMPORARY, retry_after=5)
            record = self.records.get(doc.id)
            if record is None:
                return Outcome(OutcomeKind.NOT_FOUND)
            return self._reflect(doc, record)

    def cancel(self, doc, reason):
        with self._lock:
            self.calls.append(("cancel", doc.id))
            record = self.records.get(doc.id)
            if record is None or record.status not in ("AUTHORIZED", "CANCELLED"):
                return Outcome(
                    OutcomeKind.CANCEL_REJECTED,
                    official_code="E404",
                    official_message="Documento não encontrado no autorizador falso.",
                )
            behavior = self._next("cancel", "cancel")
            if behavior == "temporary":
                return Outcome(OutcomeKind.TEMPORARY, retry_after=5)
            if behavior == "cancel_reject":
                return Outcome(
                    OutcomeKind.CANCEL_REJECTED,
                    official_code="E840",
                    official_message="Cancelamento recusado (simulação).",
                )
            record.status = "CANCELLED"
            if behavior == "timeout_after_receipt":
                return Outcome(OutcomeKind.TIMEOUT)
            return self._reflect(doc, record)


_override: FiscalProvider | None = None
_fake_singleton: FakeProvider | None = None
_sefin_singleton = None


def set_provider(provider: FiscalProvider | None) -> None:
    """Testes: força um provider para todos os tipos (``None`` volta ao padrão)."""
    global _override
    _override = provider


def provider_for(document_type: str) -> FiscalProvider:
    """Provider de cada tipo. Sem provider real para o tipo, nada é transmitido."""
    global _fake_singleton, _sefin_singleton
    if _override is not None:
        return _override
    from .config import settings

    if settings.fiscal_provider == "fake":
        if _fake_singleton is None:
            _fake_singleton = FakeProvider()
        return _fake_singleton
    if settings.fiscal_provider == "sefin" and document_type == "NFSE":
        if _sefin_singleton is None:
            from .sefin import SefinNacionalProvider

            _sefin_singleton = SefinNacionalProvider(
                timeout_seconds=settings.nfse_timeout_seconds,
                cafile=settings.nfse_ca_file,
            )
        return _sefin_singleton
    return UnavailableProvider()

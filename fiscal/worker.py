"""Worker fiscal in-process (F7).

Roda numa thread iniciada no startup (``start``), sem serviço pago à parte. É seguro com
várias réplicas: o job é reivindicado com ``FOR UPDATE SKIP LOCKED`` e um lease; lease
vencido (processo morto no meio) volta a ser reivindicável.

Garantias:

- O estado ``SUBMITTED`` + ``outcome_uncertain`` é **gravado antes** de qualquer chamada ao
  autorizador. Se o processo morrer ou a resposta não vier, a próxima execução **consulta**
  antes de reenviar; reenvio só com a consulta dizendo que o documento não chegou.
- Toda mudança de estado passa por ``documents.transition`` (CAS + evento).
- Backoff exponencial com jitter só para timeout/erro temporário/processando; o resto encerra.
- Nunca guarda nem loga XML, payload ou resposta oficial bruta.
"""

from __future__ import annotations

import os
import random
import socket
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from . import documents, providers, vault
from .config import settings
from .domain import FiscalStatus, is_complete_authorization
from .logging import get_logger
from .providers import DocumentContext, Outcome, OutcomeKind

log = get_logger("fiscal.worker")

LEASE_SECONDS = 120  # maior que o timeout de qualquer chamada ao autorizador
BACKOFF_BASE_SECONDS = 5
BACKOFF_CAP_SECONDS = 600
SUBMIT, CANCEL = "SUBMIT", "CANCEL"
S = FiscalStatus

_CLAIM = """UPDATE fiscal_jobs j SET status='RUNNING',lease_owner=%s,
      lease_expires_at=NOW()+make_interval(secs=>%s),attempt=j.attempt+1,updated_at=NOW()
    FROM (SELECT id FROM fiscal_jobs
          WHERE (status IN ('QUEUED','RETRY') AND available_at<=NOW())
             OR (status='RUNNING' AND lease_expires_at<NOW())
          ORDER BY available_at,created_at LIMIT 1 FOR UPDATE SKIP LOCKED) c
    WHERE j.id=c.id
    RETURNING j.id,j.tenant_id,j.document_id,j.job_type,j.attempt,j.max_attempts,j.correlation_id"""


@dataclass(frozen=True)
class Job:
    id: str
    tenant_id: str
    document_id: str
    job_type: str
    attempt: int
    max_attempts: int
    correlation_id: str


def backoff_seconds(attempt: int, rng=random.random) -> float:
    """Exponencial com jitter: entre metade e o teto de ``base·2^(n-1)`` (limitado)."""
    ceiling = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** max(0, attempt - 1))
    return ceiling / 2 + rng() * ceiling / 2


def claim(conn, owner: str) -> Job | None:
    cur = conn.cursor()
    cur.execute(_CLAIM, (owner, LEASE_SECONDS))
    row = cur.fetchone()
    conn.commit()
    if row is None:
        return None
    return Job(str(row[0]), row[1], str(row[2]), row[3], row[4], row[5], str(row[6]))


def heartbeat(conn, job: Job, owner: str) -> bool:
    """Estende o lease. ``False`` = outro worker assumiu o job (não mexer em nada)."""
    cur = conn.cursor()
    cur.execute(
        """UPDATE fiscal_jobs SET lease_expires_at=NOW()+make_interval(secs=>%s),updated_at=NOW()
           WHERE id=%s AND lease_owner=%s AND status='RUNNING' RETURNING id""",
        (LEASE_SECONDS, job.id, owner),
    )
    alive = cur.fetchone() is not None
    conn.commit()
    return alive


def _finish(cur, job: Job, owner: str, status: str, *, delay=0.0, error_code=None):
    cur.execute(
        """UPDATE fiscal_jobs SET status=%s,available_at=NOW()+make_interval(secs=>%s),
             lease_owner=NULL,lease_expires_at=NULL,last_error_code=%s,updated_at=NOW()
           WHERE id=%s AND lease_owner=%s""",
        (status, float(delay), error_code, job.id, owner),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _call(fn, *args) -> Outcome:
    """Exceção inesperada do provider conta como timeout: o pedido pode ter chegado."""
    try:
        return fn(*args)
    except Exception:
        log.exception("provider_call_failed")
        return Outcome(OutcomeKind.TIMEOUT)


def _context(cur, doc: documents.Doc, ring) -> DocumentContext:
    payload = documents.open_json(cur, doc.tenant_id, doc.payload_blob_id, ring)
    substituted = None
    if doc.substitutes_document_id:
        original = documents.load(cur, doc.tenant_id, doc.substitutes_document_id)
        substituted = original.official_key if original else None
    return DocumentContext(
        id=doc.id,
        tenant_id=doc.tenant_id,
        document_type=doc.document_type,
        environment=doc.environment,
        series=doc.series,
        number=doc.number,
        payload=payload,
        official_key=doc.official_key,
        substitutes_official_key=substituted,
    )


def _reschedule(cur, job, owner, doc, delay, error_code, *, exhausted_code):
    """Nova tentativa com backoff, ou DEAD quando as tentativas acabam."""
    if job.attempt < job.max_attempts:
        _finish(cur, job, owner, "RETRY", delay=delay, error_code=error_code)
        return
    _finish(cur, job, owner, "DEAD", error_code=error_code)
    if doc.status in (S.SUBMITTED, S.PROCESSING):
        documents.transition(
            cur,
            doc,
            S.ERROR_RETRYABLE,
            event="RETRY_EXHAUSTED",
            correlation_id=job.correlation_id,
            fiscal_error_code=exhausted_code,
            fiscal_error_message="Sem confirmação do autorizador. Tente de novo mais tarde.",
        )
    elif doc.status == S.CANCEL_PENDING:
        documents.touch(
            cur,
            doc,
            event="RETRY_EXHAUSTED",
            correlation_id=job.correlation_id,
            fiscal_error_code=exhausted_code,
            fiscal_error_message="Cancelamento sem confirmação do autorizador.",
        )


def _delay(outcome: Outcome, job: Job) -> float:
    if outcome.retry_after:
        return float(max(1, min(outcome.retry_after, BACKOFF_CAP_SECONDS)))
    return backoff_seconds(job.attempt)


# --- Emissão -------------------------------------------------------------------------


def _process_submit(conn, job: Job, owner: str, provider_for) -> None:
    cur = conn.cursor()
    doc = documents.load(cur, job.tenant_id, job.document_id, for_update=True)
    if doc is None or doc.status not in (
        S.QUEUED,
        S.SUBMITTED,
        S.PROCESSING,
        S.ERROR_RETRYABLE,
    ):
        _finish(cur, job, owner, "DONE")
        conn.commit()
        return
    ring = vault.keyring()
    provider = provider_for(doc.document_type)
    context = _context(cur, doc, ring)
    corr = job.correlation_id
    query_first = doc.outcome_uncertain or doc.status in (S.SUBMITTED, S.PROCESSING)
    if doc.status == S.ERROR_RETRYABLE:
        doc = documents.transition(
            cur, doc, S.QUEUED, event="QUEUED", correlation_id=corr
        )
    send_now = {
        "outcome_uncertain": True,
        "attempt": doc.attempt + 1,
        "submitted_at": _now(),
    }
    if doc.status == S.QUEUED:
        doc = documents.transition(
            cur,
            doc,
            S.SUBMITTED,
            event="QUERY" if query_first else "SUBMITTED",
            correlation_id=corr,
            **({} if query_first else send_now),
        )
    conn.commit()  # o estado vai para o banco antes de qualquer chamada de rede
    if not heartbeat(conn, job, owner):
        return
    operation = "submit"
    if query_first:
        outcome = _call(provider.query, context)
        operation = "query"
        if outcome.kind == OutcomeKind.NOT_FOUND:
            # A consulta diz que nada chegou: só agora é seguro (re)enviar.
            cur = conn.cursor()
            doc = documents.load(cur, job.tenant_id, job.document_id, for_update=True)
            if doc is None or doc.status not in (S.SUBMITTED, S.PROCESSING):
                _finish(cur, job, owner, "DONE")
                conn.commit()
                return
            documents.touch(
                cur, doc, event="SUBMITTED", correlation_id=corr, **send_now
            )
            conn.commit()
            outcome = _call(provider.submit, context)
            operation = "submit"
    else:
        outcome = _call(provider.submit, context)
    _apply_submit(conn, job, owner, outcome, operation)


def _authorized_fields(cur, doc, outcome: Outcome, ring) -> dict | None:
    """Campos da autorização, ou ``None`` se falta evidência (nunca autoriza pela metade)."""
    if not (
        outcome.official_key
        and outcome.protocol
        and outcome.authorized_at
        and outcome.xml
        and doc.series
        and doc.number is not None
    ):
        return None
    xml_id, xml_sha = documents.store_artifact(
        cur, doc, documents.XML_AUTHORIZED, outcome.xml, "application/xml", ring
    )
    aux_id = None
    if outcome.auxiliary_pdf:
        aux_id, _ = documents.store_artifact(
            cur,
            doc,
            documents.AUXILIARY_PDF,
            outcome.auxiliary_pdf,
            "application/pdf",
            ring,
        )
    fields = {
        "official_key": outcome.official_key,
        "protocol": outcome.protocol,
        "authorized_at": outcome.authorized_at,
        "xml_sha256": xml_sha,
        "xml_artifact_id": xml_id,
        "auxiliary_artifact_id": aux_id,
    }
    evidence = {
        "environment": doc.environment,
        "documentType": doc.document_type,
        "series": doc.series,
        "number": doc.number,
        "officialKey": outcome.official_key,
        "protocol": outcome.protocol,
        "authorizedAt": outcome.authorized_at,
        "xmlArtifactId": xml_id,
        "xmlSha256": xml_sha,
    }
    return fields if is_complete_authorization(evidence) else None


def _apply_submit(conn, job: Job, owner: str, outcome: Outcome, operation: str) -> None:
    cur = conn.cursor()
    doc = documents.load(cur, job.tenant_id, job.document_id, for_update=True)
    if doc is None or doc.status not in (S.SUBMITTED, S.PROCESSING):
        _finish(cur, job, owner, "DONE")
        conn.commit()
        return
    ring = vault.keyring()
    corr = job.correlation_id
    code, message = documents.sanitize_official(
        outcome.official_code, outcome.official_message
    )
    kind = outcome.kind
    log.info(
        "fiscal_job_outcome",
        extra={"documentId": doc.id, "operation": operation, "outcome": kind.value},
    )
    if kind == OutcomeKind.AUTHORIZED:
        fields = _authorized_fields(cur, doc, outcome, ring)
        if fields is not None:
            doc = documents.transition(
                cur,
                doc,
                S.AUTHORIZED,
                event="AUTHORIZED",
                correlation_id=corr,
                official_code=code,
                outcome_uncertain=False,
                fiscal_error_code=None,
                fiscal_error_message=None,
                **fields,
            )
            if doc.substitutes_document_id:
                _close_substituted(cur, doc, corr)
            _finish(cur, job, owner, "DONE")
            conn.commit()
            return
        kind = OutcomeKind.PROCESSING  # evidência incompleta: consultar de novo
        event = "AUTHORIZATION_INCOMPLETE"
    else:
        event = "PROCESSING"

    if kind == OutcomeKind.REJECTED:
        documents.transition(
            cur,
            doc,
            S.REJECTED,
            event="REJECTED",
            correlation_id=corr,
            official_code=code,
            outcome_uncertain=False,
            fiscal_error_code=code or "OFFICIAL_REJECTION",
            fiscal_error_message=message or "Documento rejeitado pelo autorizador.",
        )
        _finish(cur, job, owner, "DONE")
    elif kind == OutcomeKind.PROCESSING:
        if doc.status == S.SUBMITTED:
            doc = documents.transition(
                cur, doc, S.PROCESSING, event=event, correlation_id=corr
            )
        elif event == "AUTHORIZATION_INCOMPLETE":
            documents.record_event(cur, doc, event, None, None, corr)
        _reschedule(
            cur,
            job,
            owner,
            doc,
            _delay(outcome, job),
            "PROCESSING",
            exhausted_code="AUTHORIZATION_PENDING",
        )
    elif kind == OutcomeKind.TEMPORARY and operation == "submit":
        # Comprovadamente não chegou: volta a ERROR_RETRYABLE e reenvia com backoff.
        doc = documents.transition(
            cur,
            doc,
            S.ERROR_RETRYABLE,
            event="UPSTREAM_UNAVAILABLE",
            correlation_id=corr,
            outcome_uncertain=False,
            fiscal_error_code="UPSTREAM_UNAVAILABLE",
            fiscal_error_message="Autorizador indisponível. Nova tentativa automática agendada.",
        )
        _reschedule(
            cur,
            job,
            owner,
            doc,
            _delay(outcome, job),
            "UPSTREAM_UNAVAILABLE",
            exhausted_code="UPSTREAM_UNAVAILABLE",
        )
    elif kind == OutcomeKind.NOT_SENT:
        documents.transition(
            cur,
            doc,
            S.ERROR_RETRYABLE,
            event="NOT_SENT",
            correlation_id=corr,
            official_code=code,
            outcome_uncertain=False,
            fiscal_error_code=code or "PROVIDER_UNAVAILABLE",
            fiscal_error_message="Emissão automática indisponível. Nada foi enviado.",
        )
        _finish(cur, job, owner, "DEAD", error_code=code or "NOT_SENT")
    else:
        # TIMEOUT, TEMPORARY na consulta, ou resposta inesperada: pode ter chegado.
        documents.record_event(cur, doc, "SUBMISSION_TIMEOUT", None, None, corr)
        _reschedule(
            cur,
            job,
            owner,
            doc,
            _delay(outcome, job),
            "TIMEOUT",
            exhausted_code="AUTHORIZATION_PENDING",
        )
    conn.commit()


def _close_substituted(cur, replacement: documents.Doc, corr) -> None:
    original = documents.load(
        cur, replacement.tenant_id, replacement.substitutes_document_id, for_update=True
    )
    if original is None:
        return
    if original.status == S.AUTHORIZED:
        original = documents.transition(
            cur,
            original,
            S.CANCEL_PENDING,
            event="SUBSTITUTION_REQUESTED",
            correlation_id=corr,
        )
    if original.status == S.CANCEL_PENDING:
        documents.transition(
            cur,
            original,
            S.CANCELLED,
            event="SUBSTITUTED",
            correlation_id=corr,
            cancelled_at=_now(),
            outcome_uncertain=False,
        )


# --- Cancelamento ----------------------------------------------------------------------


def _process_cancel(conn, job: Job, owner: str, provider_for) -> None:
    cur = conn.cursor()
    doc = documents.load(cur, job.tenant_id, job.document_id, for_update=True)
    if doc is None or doc.status != S.CANCEL_PENDING:
        _finish(cur, job, owner, "DONE")
        conn.commit()
        return
    ring = vault.keyring()
    provider = provider_for(doc.document_type)
    context = _context(cur, doc, ring)
    reason = documents.open_json(cur, doc.tenant_id, doc.cancel_reason_blob_id, ring)[
        "reason"
    ]
    query_first = doc.outcome_uncertain
    if not query_first:
        documents.touch(cur, doc, outcome_uncertain=True)
    conn.commit()
    if not heartbeat(conn, job, owner):
        return
    operation = "cancel"
    if query_first:
        outcome = _call(provider.query, context)
        operation = "query"
        if outcome.kind in (OutcomeKind.AUTHORIZED, OutcomeKind.NOT_FOUND):
            outcome = _call(provider.cancel, context, reason)
            operation = "cancel"
    else:
        outcome = _call(provider.cancel, context, reason)
    _apply_cancel(conn, job, owner, outcome, operation)


def _apply_cancel(conn, job: Job, owner: str, outcome: Outcome, operation: str) -> None:
    cur = conn.cursor()
    doc = documents.load(cur, job.tenant_id, job.document_id, for_update=True)
    if doc is None or doc.status != S.CANCEL_PENDING:
        _finish(cur, job, owner, "DONE")
        conn.commit()
        return
    corr = job.correlation_id
    code, message = documents.sanitize_official(
        outcome.official_code, outcome.official_message
    )
    kind = outcome.kind
    log.info(
        "fiscal_job_outcome",
        extra={"documentId": doc.id, "operation": operation, "outcome": kind.value},
    )
    if kind == OutcomeKind.CANCELLED:
        if outcome.event_xml:
            documents.store_artifact(
                cur,
                doc,
                documents.XML_CANCEL_EVENT,
                outcome.event_xml,
                "application/xml",
                vault.keyring(),
            )
        documents.transition(
            cur,
            doc,
            S.CANCELLED,
            event="CANCELLED",
            correlation_id=corr,
            official_code=code,
            cancelled_at=_now(),
            outcome_uncertain=False,
            fiscal_error_code=None,
            fiscal_error_message=None,
        )
        _finish(cur, job, owner, "DONE")
    elif kind in (OutcomeKind.CANCEL_REJECTED, OutcomeKind.NOT_SENT):
        documents.transition(
            cur,
            doc,
            S.AUTHORIZED,
            event="CANCEL_REJECTED",
            correlation_id=corr,
            official_code=code,
            outcome_uncertain=False,
            fiscal_error_code=code or "CANCEL_REJECTED",
            fiscal_error_message=message or "O cancelamento não foi aceito.",
        )
        _finish(cur, job, owner, "DONE")
    else:
        if kind == OutcomeKind.TEMPORARY and operation == "cancel":
            doc = documents.touch(cur, doc, outcome_uncertain=False)
        else:
            documents.record_event(cur, doc, "SUBMISSION_TIMEOUT", None, None, corr)
        _reschedule(
            cur,
            job,
            owner,
            doc,
            _delay(outcome, job),
            kind.value,
            exhausted_code="CANCEL_NOT_CONFIRMED",
        )
    conn.commit()


# --- Execução --------------------------------------------------------------------------


def run_once(conn, *, owner: str, provider_for=None) -> bool:
    """Processa no máximo um job. ``True`` se havia trabalho."""
    provider_for = provider_for or providers.provider_for
    job = claim(conn, owner)
    if job is None:
        return False
    try:
        if job.job_type == SUBMIT:
            _process_submit(conn, job, owner, provider_for)
        elif job.job_type == CANCEL:
            _process_cancel(conn, job, owner, provider_for)
        else:
            cur = conn.cursor()
            _finish(cur, job, owner, "DEAD", error_code="UNKNOWN_JOB")
            conn.commit()
    except documents.StaleDocument:
        conn.rollback()
        _finish(conn.cursor(), job, owner, "RETRY", error_code="STALE")
        conn.commit()
    except Exception:
        conn.rollback()
        log.exception("fiscal_job_failed", extra={"jobId": job.id})
        cur = conn.cursor()
        if job.attempt < job.max_attempts:
            _finish(
                cur,
                job,
                owner,
                "RETRY",
                delay=backoff_seconds(job.attempt),
                error_code="INTERNAL",
            )
        else:
            _finish(cur, job, owner, "DEAD", error_code="INTERNAL")
        conn.commit()
    return True


class Worker:
    """Thread do worker. Cada volta usa uma conexão do pool e devolve no fim."""

    def __init__(self, get_conn, put_conn, poll_seconds: float):
        self._get_conn, self._put_conn = get_conn, put_conn
        self._poll = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._loop, name="fiscal-worker", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            worked = False
            conn = None
            try:
                conn = self._get_conn()
                worked = run_once(conn, owner=self.owner)
            except Exception:
                log.exception("fiscal_worker_loop_failed")
                if conn is not None:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
            finally:
                if conn is not None:
                    self._put_conn(conn)
            if not worked:
                self._stop.wait(self._poll)


_worker: Worker | None = None


def start(get_conn, put_conn) -> Worker | None:
    """Sobe o worker se o fiscal e o worker estão ligados (chamado no startup)."""
    global _worker
    if not (settings.fiscal_enabled and settings.worker_enabled) or _worker is not None:
        return _worker
    _worker = Worker(get_conn, put_conn, settings.worker_poll_seconds)
    _worker.start()
    log.info("fiscal_worker_started", extra={"provider": settings.fiscal_provider})
    return _worker


def stop() -> None:
    global _worker
    if _worker is not None:
        _worker.stop()
        _worker = None

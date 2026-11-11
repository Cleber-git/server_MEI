"""Motor de documentos fiscais (F7): persistência, estados, eventos e artefatos.

Tudo aqui roda no cursor da transação de quem chama (API ou worker):

- ``transition`` aplica a máquina de estados com CAS (status + version) e grava o evento.
- ``reserve_number`` reserva o número atomicamente (``UPDATE fiscal_series … RETURNING``).
- Payload da intenção e artefatos (XML/PDF) ficam no cofre (``fiscal_vault_blobs``),
  com SHA-256 conferido na leitura; a linha de ``fiscal_artifacts`` é imutável.
- ``enqueue`` é o outbox: o job nasce na mesma transação do documento.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID, uuid4

from . import vault
from .domain import FiscalStatus, assert_transition, canonical_json
from .errors import ApiProblem
from .logging import redact

# Série padrão por tipo. A F8 (DPS) e a F11 (NFC-e) confirmam a faixa oficial.
DEFAULT_SERIES = {"NFSE": "1", "NFCE_65": "1", "NFE_55": "1"}
PAYLOAD_PURPOSE = "DOCUMENT_PAYLOAD"
ARTIFACT_PURPOSE = "ARTIFACT"
XML_AUTHORIZED = "XML_AUTHORIZED"
AUXILIARY_PDF = "AUXILIARY_PDF"
XML_CANCEL_EVENT = "XML_CANCEL_EVENT"
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
# Comandos que exigem Idempotency-Key (contrato 1.1).
OP_CREATE, OP_CANCEL, OP_RETRY, OP_SUBSTITUTE = (
    "CREATE_DOCUMENT",
    "CANCEL_DOCUMENT",
    "RETRY_DOCUMENT",
    "SUBSTITUTE_DOCUMENT",
)
EVIDENCE_STATUSES = (
    FiscalStatus.AUTHORIZED,
    FiscalStatus.CANCEL_PENDING,
    FiscalStatus.CANCELLED,
)

# Textos fixos dos eventos (sem PII; o detalhe oficial vai em officialCode/message).
EVENT_MESSAGES = {
    "CREATED": "Documento recebido.",
    "QUEUED": "Documento na fila de emissão.",
    "SUBMITTED": "Enviado ao autorizador.",
    "QUERY": "Situação consultada no autorizador antes de reenviar.",
    "PROCESSING": "O autorizador ainda está processando.",
    "AUTHORIZED": "Documento autorizado.",
    "AUTHORIZATION_INCOMPLETE": "Resposta sem toda a evidência; consultando de novo.",
    "REJECTED": "Documento rejeitado pelo autorizador.",
    "SUBMISSION_TIMEOUT": "Sem resposta do autorizador; a situação será consultada.",
    "UPSTREAM_UNAVAILABLE": "Autorizador indisponível; nova tentativa agendada.",
    "NOT_SENT": "Documento não enviado: emissão indisponível para esta empresa.",
    "RETRY_EXHAUSTED": "Tentativas automáticas esgotadas.",
    "RETRY_REQUESTED": "Nova tentativa solicitada.",
    "CANCEL_REQUESTED": "Cancelamento solicitado.",
    "CANCELLED": "Documento cancelado.",
    "CANCEL_REJECTED": "Cancelamento recusado pelo autorizador.",
    "SUBSTITUTION_REQUESTED": "Substituição solicitada.",
    "SUBSTITUTED": "Documento cancelado por substituição.",
}

_COLUMNS = (
    "id",
    "tenant_id",
    "establishment_id",
    "document_type",
    "environment",
    "status",
    "series",
    "number",
    "official_key",
    "protocol",
    "authorized_at",
    "created_at",
    "updated_at",
    "fiscal_error_code",
    "fiscal_error_message",
    "xml_sha256",
    "xml_artifact_id",
    "auxiliary_artifact_id",
    "correlation_id",
    "attempt",
    "version",
    "outcome_uncertain",
    "payload_blob_id",
    "cancel_reason_blob_id",
    "substitutes_document_id",
    "sale_id",
    "canonical_request_hash",
)
_SELECT = (
    "SELECT "
    + ",".join("d." + c for c in _COLUMNS)
    + """,(SELECT MIN(j.available_at) FROM fiscal_jobs j WHERE j.tenant_id=d.tenant_id
          AND j.document_id=d.id AND j.status IN ('QUEUED','RETRY')) FROM fiscal_documents d"""
)
# Só estes campos podem mudar junto com uma transição (lista fechada: SQL sem injeção).
_MUTABLE = {
    "series",
    "number",
    "official_key",
    "protocol",
    "authorized_at",
    "fiscal_error_code",
    "fiscal_error_message",
    "official_code",
    "xml_sha256",
    "xml_artifact_id",
    "auxiliary_artifact_id",
    "attempt",
    "outcome_uncertain",
    "submitted_at",
    "cancel_requested_at",
    "cancelled_at",
    "cancel_reason_blob_id",
}


class StaleDocument(Exception):
    """O documento mudou desde a leitura (CAS falhou)."""


@dataclass(frozen=True)
class Doc:
    id: str
    tenant_id: str
    establishment_id: str
    document_type: str
    environment: str
    status: FiscalStatus
    series: str | None
    number: int | None
    official_key: str | None
    protocol: str | None
    authorized_at: datetime | None
    created_at: datetime
    updated_at: datetime
    fiscal_error_code: str | None
    fiscal_error_message: str | None
    xml_sha256: str | None
    xml_artifact_id: str | None
    auxiliary_artifact_id: str | None
    correlation_id: str
    attempt: int
    version: int
    outcome_uncertain: bool
    payload_blob_id: str | None
    cancel_reason_blob_id: str | None
    substitutes_document_id: str | None
    sale_id: str | None
    canonical_request_hash: str
    next_job_at: datetime | None = None


def _doc(row) -> Doc:
    values = list(row)
    values[5] = FiscalStatus(values[5])
    for index in (0, 2, 16, 17, 18, 22, 23, 24):
        values[index] = str(values[index]) if values[index] is not None else None
    for index in (15, 26):  # CHAR(64)
        if values[index] is not None:
            values[index] = values[index].strip()
    return Doc(*values)


def load(cur, tenant_id: str, document_id, *, for_update: bool = False) -> Doc | None:
    try:
        ident = UUID(str(document_id))
    except ValueError:
        return None
    sql = _SELECT + " WHERE d.tenant_id=%s AND d.id=%s"
    if for_update:
        # O subselect de jobs não pode ir no FOR UPDATE: trava só o documento.
        sql = sql + " FOR UPDATE OF d"
    cur.execute(sql, (tenant_id, ident))
    row = cur.fetchone()
    return _doc(row) if row else None


def not_found() -> ApiProblem:
    return ApiProblem(404, "NOT_FOUND", "Recurso não encontrado.")


def to_dto(doc: Doc, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    retry_after = None
    if doc.next_job_at is not None and doc.next_job_at > now:
        retry_after = max(1, int((doc.next_job_at - now).total_seconds() + 0.999))
    evidence = doc.status in EVIDENCE_STATUSES
    return {
        "id": doc.id,
        "documentType": doc.document_type,
        "environment": doc.environment,
        "status": doc.status.value,
        "series": doc.series,
        "number": str(doc.number) if doc.number is not None else None,
        "officialKey": doc.official_key,
        "protocol": doc.protocol,
        "authorizedAt": doc.authorized_at,
        "createdAt": doc.created_at,
        "updatedAt": doc.updated_at,
        "fiscalErrorCode": doc.fiscal_error_code,
        "fiscalErrorMessage": doc.fiscal_error_message,
        # Evidência só aparece em documento autorizado (ou cancelado depois de autorizado).
        "xmlSha256": doc.xml_sha256 if evidence else None,
        "xmlArtifactId": doc.xml_artifact_id if evidence else None,
        "auxiliaryArtifactId": doc.auxiliary_artifact_id if evidence else None,
        "correlationId": doc.correlation_id,
        "attempt": doc.attempt,
        "retryAfter": retry_after,
        "etag": f'"{doc.version}"',
    }


# --- Eventos e estados -------------------------------------------------------------


def sanitize_official(code: str | None, message: str | None):
    """Código e texto oficiais sem PII nem marcação, com tamanho limitado."""
    clean_code = None
    if code:
        clean_code = re.sub(r"[^A-Za-z0-9_.-]", "", str(code))[:20] or None
    clean_message = None
    if message:
        text = re.sub(r"<[^>]*>", " ", str(message))
        text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
        clean_message = re.sub(r"\s+", " ", redact(text)).strip()[:255] or None
    return clean_code, clean_message


def record_event(
    cur,
    doc: Doc,
    event_type: str,
    from_status: FiscalStatus | None,
    to_status: FiscalStatus | None,
    correlation_id,
    *,
    official_code: str | None = None,
    message: str | None = None,
) -> None:
    """Grava um evento. Chamar só com o documento travado (sequência por documento)."""
    cur.execute(
        """INSERT INTO fiscal_events(id,tenant_id,document_id,event_type,sequence,status,
             from_status,to_status,correlation_id,official_code,message)
           SELECT %s,%s,%s,%s,COALESCE(MAX(sequence),0)+1,%s,%s,%s,%s,%s,%s
           FROM fiscal_events WHERE tenant_id=%s AND document_id=%s""",
        (
            uuid4(),
            doc.tenant_id,
            doc.id,
            event_type,
            (to_status or doc.status).value,
            from_status.value if from_status else None,
            to_status.value if to_status else None,
            str(correlation_id) if correlation_id else None,
            official_code,
            message or EVENT_MESSAGES.get(event_type),
            doc.tenant_id,
            doc.id,
        ),
    )


def _update(cur, doc: Doc, status: FiscalStatus, fields: dict) -> Doc:
    unknown = set(fields) - _MUTABLE
    if unknown:
        raise ValueError(f"campos não permitidos: {sorted(unknown)}")
    assignments = ["status=%s", "version=version+1", "updated_at=NOW()"]
    params: list = [status.value]
    for name, value in fields.items():
        assignments.append(f"{name}=%s")
        params.append(value)
    params += [doc.tenant_id, doc.id, doc.status.value, doc.version]
    cur.execute(
        "UPDATE fiscal_documents SET "
        + ",".join(assignments)
        + " WHERE tenant_id=%s AND id=%s AND status=%s AND version=%s RETURNING id",
        params,
    )
    if cur.fetchone() is None:
        raise StaleDocument(doc.id)
    return load(cur, doc.tenant_id, doc.id)


def transition(
    cur,
    doc: Doc,
    target: FiscalStatus,
    *,
    event: str,
    correlation_id=None,
    official_code: str | None = None,
    message: str | None = None,
    **fields,
) -> Doc:
    """Muda de estado com CAS e grava o evento. Transição fora da tabela dá 409."""
    assert_transition(doc.status, target)
    if official_code is not None:
        fields["official_code"] = official_code
    updated = _update(cur, doc, target, fields)
    record_event(
        cur,
        updated,
        event,
        doc.status,
        target,
        correlation_id or doc.correlation_id,
        official_code=official_code,
        message=message,
    )
    return updated


def touch(
    cur, doc: Doc, *, event: str | None = None, correlation_id=None, **fields
) -> Doc:
    """Atualiza campos sem mudar de estado (também com CAS)."""
    updated = _update(cur, doc, doc.status, fields)
    if event:
        record_event(
            cur, updated, event, None, None, correlation_id or doc.correlation_id
        )
    return updated


# --- Numeração, idempotência e jobs ------------------------------------------------


def reserve_number(cur, tenant_id, establishment_id, document_type, environment):
    """Reserva o próximo número da série (atômico; a linha fica travada até o commit)."""
    series = DEFAULT_SERIES[document_type]
    cur.execute(
        """INSERT INTO fiscal_series(id,tenant_id,establishment_id,document_type,environment,series)
           VALUES (%s,%s,%s,%s,%s,%s)
           ON CONFLICT (tenant_id,establishment_id,document_type,environment,series) DO NOTHING""",
        (uuid4(), tenant_id, establishment_id, document_type, environment, series),
    )
    cur.execute(
        """UPDATE fiscal_series SET next_number=next_number+1,version=version+1
           WHERE tenant_id=%s AND establishment_id=%s AND document_type=%s
             AND environment=%s AND series=%s
           RETURNING next_number-1""",
        (tenant_id, establishment_id, document_type, environment, series),
    )
    return series, cur.fetchone()[0]


def check_idempotency_key(value: str | None) -> str:
    if not value or not IDEMPOTENCY_KEY_RE.match(value):
        raise ApiProblem(
            400,
            "IDEMPOTENCY_KEY_REQUIRED",
            "Informe uma Idempotency-Key válida (UUID ou 16 a 128 caracteres).",
        )
    return value


def find_idempotency(cur, tenant_id, establishment_id, operation, key):
    cur.execute(
        """SELECT canonical_payload_hash,resource_id FROM fiscal_idempotency
           WHERE tenant_id=%s AND establishment_id=%s AND operation=%s AND idempotency_key=%s""",
        (tenant_id, establishment_id, operation, key),
    )
    row = cur.fetchone()
    return (row[0].strip(), str(row[1]) if row[1] else None) if row else None


def claim_idempotency(
    cur, tenant_id, establishment_id, operation, key, digest, resource_id
):
    """Grava a chave. Corrida com outra requisição: espera o commit dela e devolve o que ela
    gravou (``(hash, resource_id)``); ``None`` = esta requisição é a dona da chave."""
    cur.execute(
        """INSERT INTO fiscal_idempotency(tenant_id,establishment_id,operation,idempotency_key,
             canonical_payload_hash,resource_id,status,expires_at)
           VALUES (%s,%s,%s,%s,%s,%s,'COMPLETED',NOW()+INTERVAL '90 days')
           ON CONFLICT DO NOTHING RETURNING resource_id""",
        (tenant_id, establishment_id, operation, key, digest, resource_id),
    )
    if cur.fetchone() is not None:
        return None
    return find_idempotency(cur, tenant_id, establishment_id, operation, key)


def replay(existing, digest: str):
    """Mesma chave: mesmo hash devolve o recurso; hash diferente é 409."""
    stored_hash, resource_id = existing
    if stored_hash != digest:
        raise ApiProblem(
            409,
            "IDEMPOTENCY_KEY_REUSED",
            "Esta Idempotency-Key já foi usada com outro conteúdo.",
        )
    return resource_id


def enqueue(
    cur, tenant_id, document_id, job_type, correlation_id, max_attempts
) -> None:
    """Outbox: um job por documento e tipo. Job encerrado volta à fila zerado."""
    cur.execute(
        """UPDATE fiscal_jobs SET status='QUEUED',attempt=0,available_at=NOW(),
             lease_owner=NULL,lease_expires_at=NULL,last_error_code=NULL,updated_at=NOW(),
             correlation_id=%s
           WHERE tenant_id=%s AND document_id=%s AND job_type=%s
             AND status IN ('RETRY','DEAD','DONE')
           RETURNING id""",
        (str(correlation_id), tenant_id, document_id, job_type),
    )
    if cur.fetchone() is not None:
        return
    cur.execute(
        """SELECT 1 FROM fiscal_jobs WHERE tenant_id=%s AND document_id=%s AND job_type=%s""",
        (tenant_id, document_id, job_type),
    )
    if cur.fetchone() is not None:
        return  # já na fila ou rodando
    cur.execute(
        """INSERT INTO fiscal_jobs(id,tenant_id,document_id,job_type,correlation_id,max_attempts)
           VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
        (uuid4(), tenant_id, document_id, job_type, str(correlation_id), max_attempts),
    )


# --- Cofre: payload e artefatos ----------------------------------------------------


def seal_json(cur, tenant_id, value: dict, ring) -> str:
    return str(vault.seal(cur, tenant_id, PAYLOAD_PURPOSE, canonical_json(value), ring))


def open_json(cur, tenant_id, blob_id, ring) -> dict:
    return json.loads(vault.open_blob(cur, tenant_id, blob_id, PAYLOAD_PURPOSE, ring))


def store_artifact(cur, doc: Doc, artifact_type, content: bytes, content_type, ring):
    """Grava o artefato cifrado e devolve ``(id, sha256)``."""
    digest = hashlib.sha256(content).hexdigest()
    blob_id = vault.seal(cur, doc.tenant_id, ARTIFACT_PURPOSE, content, ring)
    artifact_id = uuid4()
    cur.execute(
        """INSERT INTO fiscal_artifacts(id,tenant_id,document_id,artifact_type,storage_key,
             sha256,content_type,size,vault_blob_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            artifact_id,
            doc.tenant_id,
            doc.id,
            artifact_type,
            f"vault:{blob_id}",
            digest,
            content_type,
            len(content),
            blob_id,
        ),
    )
    return str(artifact_id), digest


def read_artifact(cur, tenant_id, artifact_id, ring) -> tuple[bytes, str]:
    """Conteúdo e content-type. SHA-256 diferente do gravado é falha de integridade."""
    cur.execute(
        """SELECT vault_blob_id,sha256,content_type FROM fiscal_artifacts
           WHERE tenant_id=%s AND id=%s""",
        (tenant_id, artifact_id),
    )
    row = cur.fetchone()
    if not row or row[0] is None:
        raise not_found()
    content = vault.open_blob(cur, tenant_id, row[0], ARTIFACT_PURPOSE, ring)
    if hashlib.sha256(content).hexdigest() != row[1].strip():
        raise vault.VaultIntegrityError("artefato não confere com o SHA-256 gravado")
    return content, row[2]


# --- Totais (o servidor recalcula; divergência é 422) --------------------------------

_CENT = Decimal("0.01")


def _mismatch(paths: list[str]) -> ApiProblem:
    return ApiProblem(
        422,
        "VALIDATION_FAILED",
        "Os valores da nota não conferem.",
        field_errors=[{"path": p, "code": "TOTAL_MISMATCH"} for p in paths],
    )


def check_service_totals(request) -> None:
    amount = Decimal(request.service.amount)
    totals = request.totals
    wrong = []
    if amount <= 0:
        wrong.append("service.amount")
    for name in ("products", "discount", "freight", "other", "change"):
        if Decimal(getattr(totals, name)) != 0:
            wrong.append("totals." + name)
    if Decimal(totals.invoice) != amount:
        wrong.append("totals.invoice")
    if Decimal(totals.paid) != amount:
        wrong.append("totals.paid")
    if wrong:
        raise _mismatch(wrong)


def check_product_totals(intent) -> None:
    """Mesmas contas do ``ProductFiscalDraftFactory``/``ProductFinancialPolicy`` do app."""
    wrong = []
    gross_sum = item_discount = Decimal("0.00")
    for index, item in enumerate(intent.items):
        gross = (Decimal(item.quantity) * Decimal(item.unitPrice)).quantize(
            _CENT, rounding=ROUND_HALF_UP
        )
        discount = Decimal(item.discountAmount)
        path = f"product.items[{index}]"
        if Decimal(item.quantity) <= 0:
            wrong.append(path + ".quantity")
        if Decimal(item.grossAmount) != gross:
            wrong.append(path + ".grossAmount")
        if discount > gross:
            wrong.append(path + ".discountAmount")
        if Decimal(item.netAmount) != gross - discount:
            wrong.append(path + ".netAmount")
        gross_sum += gross
        item_discount += discount
    totals = intent.totals
    products, discount = Decimal(totals.products), Decimal(totals.discount)
    freight, other = Decimal(totals.freight), Decimal(totals.other)
    invoice, paid, change = (
        Decimal(totals.invoice),
        Decimal(totals.paid),
        Decimal(totals.change),
    )
    if products != gross_sum:
        wrong.append("product.totals.products")
    if discount < item_discount or discount > gross_sum:
        wrong.append("product.totals.discount")
    if invoice != products - discount + freight + other or invoice <= 0:
        wrong.append("product.totals.invoice")
    payments = sum((Decimal(p.amount) for p in intent.payments), Decimal("0.00"))
    cash = sum(
        (Decimal(p.amount) for p in intent.payments if p.method == "CASH"),
        Decimal("0.00"),
    )
    if intent.paymentExpectation == "PAID":
        if not intent.payments or any(Decimal(p.amount) <= 0 for p in intent.payments):
            wrong.append("product.payments")
        if paid != payments or paid < invoice:
            wrong.append("product.totals.paid")
        if change != paid - invoice or (change > 0 and change > cash):
            wrong.append("product.totals.change")
    elif intent.payments or paid != 0 or change != 0:
        wrong.append("product.payments")
    if wrong:
        raise _mismatch(wrong)


# --- Cursor do histórico ---------------------------------------------------------------


def encode_cursor(doc: Doc) -> str:
    raw = json.dumps({"c": doc.created_at.isoformat(), "i": doc.id}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        data = json.loads(raw)
        created = datetime.fromisoformat(data["c"])
        ident = str(UUID(data["i"]))
        if created.tzinfo is None:
            raise ValueError("sem fuso")
        return created, ident
    except (
        binascii.Error,
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        raise ApiProblem(
            422,
            "VALIDATION_FAILED",
            "Cursor inválido.",
            field_errors=[{"path": "cursor", "code": "INVALID"}],
        ) from exc


def list_page(
    cur, tenant_id, *, limit, cursor, document_type, status, date_from, date_to
):
    clauses, params = ["d.tenant_id=%s"], [tenant_id]
    for column, value in (("d.document_type", document_type), ("d.status", status)):
        if value:
            clauses.append(column + "=%s")
            params.append(value)
    if date_from:
        clauses.append("d.created_at>=%s")
        params.append(date_from)
    if date_to:
        clauses.append("d.created_at<%s")
        params.append(date_to)
    if cursor:
        created, ident = decode_cursor(cursor)
        clauses.append("(d.created_at,d.id)<(%s,%s::uuid)")
        params += [created, ident]
    cur.execute(
        _SELECT
        + " WHERE "
        + " AND ".join(clauses)
        + " ORDER BY d.created_at DESC,d.id DESC LIMIT %s",
        params + [limit + 1],
    )
    docs = [_doc(row) for row in cur.fetchall()]
    more = len(docs) > limit
    docs = docs[:limit]
    return docs, (encode_cursor(docs[-1]) if more and docs else None)


def events(cur, tenant_id, document_id) -> list[dict]:
    cur.execute(
        """SELECT id,event_type,from_status,to_status,created_at,correlation_id,official_code,message
           FROM fiscal_events WHERE tenant_id=%s AND document_id=%s ORDER BY sequence""",
        (tenant_id, document_id),
    )
    return [
        {
            "id": str(r[0]),
            "type": r[1],
            "fromStatus": r[2],
            "toStatus": r[3],
            "occurredAt": r[4],
            "correlationId": str(r[5]) if r[5] else None,
            "officialCode": r[6],
            "message": r[7],
        }
        for r in cur.fetchall()
    ]

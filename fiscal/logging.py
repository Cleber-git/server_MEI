"""Logging estruturado (JSON por linha) com filtro de redação.

Regra do contrato (seção 7): log nunca contém XML, PFX, senha, CSC, token,
CPF/CNPJ completo, e-mail ou endereço. A redação roda sobre a mensagem já
formatada, sobre os campos extras e sobre o traceback, porque exceções de
driver (psycopg2, requests) costumam carregar valores do usuário.

Uso: ``from fiscal.logging import get_logger``; ``configure_logging()`` é
chamado uma vez em ``fiscal.http.install``.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

correlation_id_var: ContextVar[str | None] = ContextVar(
    "fiscal_correlation_id", default=None
)

_KEY_VALUE = re.compile(
    r"(?i)(\"?\b(?:password|senha|passwd|secret|token|access_?token|refresh_?token|"
    r"csc(?:_?token)?|api[_-]?key|authorization|pfx|private[_-]?key)\b\"?\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;&}\]]+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+(?!\[TOKEN\])[^\s\"',;]+")
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # XML: declaração até o fim e qualquer elemento com fechamento.
    (re.compile(r"<\?xml[\s\S]*"), "[XML]"),
    (re.compile(r"<([A-Za-z][\w:.-]*)\b[^>]*>[\s\S]*?</\1\s*>"), "[XML]"),
    # PEM (chave/certificado).
    (
        re.compile(r"-----BEGIN [A-Z ]+-----[\s\S]*?(?:-----END [A-Z ]+-----|$)"),
        "[PEM]",
    ),
    # JWT e Bearer.
    (re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]*"), "[TOKEN]"),
    # Base64 longo (PFX, XML compactado, assinatura).
    (re.compile(r"[A-Za-z0-9+/]{80,}={0,2}"), "[BASE64]"),
    # Tokens opacos: 32+ chars com maiúscula, minúscula e dígito (hex puro passa).
    (
        re.compile(
            r"(?<![\w-])(?=[\w-]*[A-Z])(?=[\w-]*[a-z])(?=[\w-]*\d)[\w-]{32,}(?![\w-])"
        ),
        "[TOKEN]",
    ),
    # E-mail.
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[EMAIL]"),
    # CNPJ numérico ou alfanumérico (12 posições [0-9A-Z] + 2 DV), com ou sem máscara.
    (
        re.compile(
            r"(?<![\w])[0-9A-Z]{2}\.?[0-9A-Z]{3}\.?[0-9A-Z]{3}/?[0-9A-Z]{4}-?\d{2}(?![\w])"
        ),
        "[CNPJ]",
    ),
    # CPF com ou sem máscara.
    (re.compile(r"(?<![\w])\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?![\w])"), "[CPF]"),
]


def redact(value: object) -> str:
    """Devolve ``value`` como texto sem dados sensíveis."""
    text = value if isinstance(value, str) else str(value)
    # Bearer antes do chave=valor, senão "Authorization: Bearer x" perde só o "Bearer".
    text = _BEARER.sub("Bearer [TOKEN]", text)
    text = _KEY_VALUE.sub(lambda m: m.group(1) + "[REDACTED]", text)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _redact_extra(value):
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _redact_extra(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_redact_extra(v) for v in value]
    return redact(value)


_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class RedactingFilter(logging.Filter):
    """Reescreve mensagem, campos extras e traceback do record já redigidos."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "_fiscal_redacted", False):
            return True
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - formato quebrado não pode derrubar o log
            message = str(record.msg)
        record.msg, record.args = redact(message), ()
        if record.exc_info:
            record.exc_text = redact(
                logging.Formatter().formatException(record.exc_info)
            )
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        for key in list(record.__dict__):
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                record.__dict__[key] = _redact_extra(record.__dict__[key])
        if not getattr(record, "correlationId", None):
            record.correlationId = correlation_id_var.get()
        record._fiscal_redacted = True
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        body = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlationId": getattr(record, "correlationId", None)
            or correlation_id_var.get(),
        }
        for key, value in record.__dict__.items():
            if (
                key not in _STANDARD_ATTRS
                and not key.startswith("_")
                and key not in body
            ):
                body[key] = value
        if record.exc_text:
            body["exception"] = record.exc_text
        # A redação final cobre qualquer caminho que tenha escapado do filtro.
        return redact(json.dumps(body, ensure_ascii=False, default=str))


_HANDLER_NAME = "fiscal-json"
_REDACTED_LOGGERS = ("fiscal", "uvicorn.error", "uvicorn.access")


def configure_logging(level: int = logging.INFO, stream=None) -> logging.Logger:
    """Idempotente: handler JSON no logger ``fiscal`` e filtro nos do uvicorn."""
    root = logging.getLogger("fiscal")
    if not any(h.get_name() == _HANDLER_NAME for h in root.handlers):
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.set_name(_HANDLER_NAME)
        handler.setFormatter(JsonFormatter())
        handler.addFilter(RedactingFilter())
        root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
    for name in _REDACTED_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactingFilter) for f in logger.filters):
            logger.addFilter(RedactingFilter())
    return root


def get_logger(name: str) -> logging.Logger:
    """Logger filho de ``fiscal`` (aceita ``__name__`` de ``fiscal.*``)."""
    if name != "fiscal" and not name.startswith("fiscal."):
        name = "fiscal." + name
    logger = logging.getLogger(name)
    if not any(isinstance(f, RedactingFilter) for f in logger.filters):
        logger.addFilter(RedactingFilter())
    return logger

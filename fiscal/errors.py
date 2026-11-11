"""Envelope de erro do contrato fiscal v1 (seção 2 de docs/fiscal-api-contract-v1.md).

``{"error": {code, message, retryable, correlationId, fieldErrors?, reasons?,
officialCode?, officialMessage?}}`` com ``Content-Type: application/json``.
``message`` é texto pt-BR simples, sem PII; detalhes técnicos vão só para o log
(redigido) e nunca para a resposta.
"""

from __future__ import annotations

import re
from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse

_CORRELATION_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def normalize_correlation_id(value: str | None) -> str:
    """UUID recebido em ``X-Correlation-Id`` ou um novo, se ausente/inválido."""
    if value and _CORRELATION_RE.match(value.strip()):
        return value.strip().lower()
    return str(uuid4())


def correlation_id_of(request: Request) -> str:
    current = getattr(request.state, "correlation_id", None)
    if not current:
        current = normalize_correlation_id(request.headers.get("x-correlation-id"))
        request.state.correlation_id = current
    return current


class ApiProblem(Exception):
    """Erro de negócio com código estável da seção 2.1 do contrato."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        field_errors: list[dict] | None = None,
        reasons: list[str] | None = None,
        retryable: bool = False,
        official_code: str | None = None,
        official_message: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.field_errors = field_errors or []
        self.reasons = reasons or []
        self.retryable = retryable
        self.official_code = official_code
        self.official_message = official_message
        self.headers = headers or {}


def error_body(
    *,
    code: str,
    message: str,
    correlation_id: str,
    retryable: bool = False,
    field_errors: list[dict] | None = None,
    reasons: list[str] | None = None,
    official_code: str | None = None,
    official_message: str | None = None,
) -> dict:
    error: dict = {
        "code": code,
        "message": message,
        "retryable": retryable,
        "correlationId": correlation_id,
        "officialCode": official_code,
        "officialMessage": official_message,
    }
    if field_errors:
        error["fieldErrors"] = field_errors
    if reasons:
        error["reasons"] = reasons
    return {"error": error}


def error_response(
    request: Request,
    status: int,
    code: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
    **fields,
) -> JSONResponse:
    correlation_id = correlation_id_of(request)
    response = JSONResponse(
        status_code=status,
        content=error_body(
            code=code, message=message, correlation_id=correlation_id, **fields
        ),
        headers=headers,
    )
    response.headers["X-Correlation-Id"] = correlation_id
    return response


def problem_response(request: Request, exc: ApiProblem) -> JSONResponse:
    return error_response(
        request,
        exc.status,
        exc.code,
        exc.message,
        headers=exc.headers,
        retryable=exc.retryable,
        field_errors=exc.field_errors,
        reasons=exc.reasons,
        official_code=exc.official_code,
        official_message=exc.official_message,
    )


async def api_problem_handler(request: Request, exc: ApiProblem):
    return problem_response(request, exc)

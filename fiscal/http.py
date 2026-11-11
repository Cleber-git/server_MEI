"""Instalação HTTP do módulo fiscal: envelope de erro, correlação e headers.

Tudo aqui vale só para ``/api/v1/**``. As rotas legadas mantêm o formato antigo
(``{"detail": …}`` do FastAPI), para não quebrar o resto do app.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .errors import (
    ApiProblem,
    api_problem_handler,
    error_response,
    normalize_correlation_id,
)
from .logging import configure_logging, correlation_id_var, get_logger

API_PREFIX = "/api/v1"
log = get_logger("fiscal.http")

# HTTPException genérica (rota inexistente, método errado…) → código estável.
_HTTP_CODES: dict[int, tuple[str, str]] = {
    400: ("BAD_REQUEST", "Requisição inválida."),
    401: ("AUTHENTICATION_REQUIRED", "Autenticação necessária."),
    403: ("PERMISSION_DENIED", "Você não tem permissão para esta ação."),
    404: ("NOT_FOUND", "Recurso não encontrado."),
    405: ("METHOD_NOT_ALLOWED", "Método não permitido para esta rota."),
    413: ("PAYLOAD_TOO_LARGE", "Conteúdo maior que o permitido."),
    415: ("UNSUPPORTED_MEDIA_TYPE", "Tipo de conteúdo não suportado."),
    429: ("RATE_LIMITED", "Muitas tentativas. Tente novamente mais tarde."),
}
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def is_api_v1(request: Request) -> bool:
    path = request.url.path
    return path == API_PREFIX or path.startswith(API_PREFIX + "/")


def _field_path(loc: tuple) -> str:
    parts = [str(p) for p in loc]
    if parts and parts[0] == "body":
        parts = parts[1:]
    return ".".join(parts) or "body"


def _field_code(error_type: str) -> str:
    if error_type == "missing":
        return "REQUIRED"
    if error_type in {"json_invalid", "model_attributes_type", "dict_type"}:
        return "INVALID_BODY"
    if error_type == "extra_forbidden":
        return "UNKNOWN_FIELD"
    return error_type.upper()


def validation_field_errors(exc: RequestValidationError) -> list[dict]:
    # Só caminho e tipo: o valor recebido (``input``) pode conter PII.
    return [
        {
            "path": _field_path(tuple(e.get("loc", ()))),
            "code": _field_code(e.get("type", "")),
        }
        for e in exc.errors()
    ]


async def _http_exception(request: Request, exc: StarletteHTTPException):
    if not is_api_v1(request):
        return await http_exception_handler(request, exc)
    code, message = _HTTP_CODES.get(
        exc.status_code,
        ("INTERNAL_ERROR", "Não foi possível concluir a operação.")
        if exc.status_code >= 500
        else ("BAD_REQUEST", "Requisição inválida."),
    )
    return error_response(
        request,
        exc.status_code,
        code,
        message,
        headers=getattr(exc, "headers", None),
        retryable=exc.status_code == 429,
    )


async def _validation_exception(request: Request, exc: RequestValidationError):
    if not is_api_v1(request):
        return await request_validation_exception_handler(request, exc)
    return error_response(
        request,
        422,
        "VALIDATION_FAILED",
        "Alguns dados enviados são inválidos.",
        field_errors=validation_field_errors(exc),
    )


async def _unexpected_exception(request: Request, exc: Exception):
    # Rede de segurança: o middleware abaixo já trata /api/v1; isto cobre o
    # que escapar (por ex. erro dentro de outro middleware).
    if not is_api_v1(request):
        return PlainTextResponse("Internal Server Error", status_code=500)
    return error_response(
        request, 500, "INTERNAL_ERROR", "Não foi possível concluir a operação."
    )


def install(app: FastAPI) -> None:
    """Registra handlers e o middleware de ``/api/v1``. Chamar uma vez por app."""
    configure_logging()
    app.add_exception_handler(ApiProblem, api_problem_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(RequestValidationError, _validation_exception)
    app.add_exception_handler(Exception, _unexpected_exception)

    @app.middleware("http")
    async def fiscal_api_middleware(request: Request, call_next):
        if not is_api_v1(request):
            return await call_next(request)
        correlation_id = normalize_correlation_id(
            request.headers.get("x-correlation-id")
        )
        request.state.correlation_id = correlation_id
        token = correlation_id_var.set(correlation_id)
        try:
            try:
                response = await call_next(request)
            except Exception:
                log.exception(
                    "erro inesperado na API v1",
                    extra={"method": request.method, "route": request.url.path},
                )
                response = error_response(
                    request,
                    500,
                    "INTERNAL_ERROR",
                    "Não foi possível concluir a operação.",
                )
            response.headers["X-Correlation-Id"] = correlation_id
            for name, value in _SECURITY_HEADERS.items():
                response.headers[name] = value
            return response
        finally:
            correlation_id_var.reset(token)

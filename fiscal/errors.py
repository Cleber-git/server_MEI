from __future__ import annotations

from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse


class ApiProblem(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        title: str,
        detail: str,
        errors: list[dict] | None = None,
    ):
        self.status, self.code, self.title, self.detail, self.errors = (
            status,
            code,
            title,
            detail,
            errors or [],
        )


def problem_response(request: Request, exc: ApiProblem) -> JSONResponse:
    correlation_id = getattr(request.state, "correlation_id", str(uuid4()))
    body = {
        "type": f"https://server-mei/errors/{exc.code.lower().replace('_', '-')}",
        "title": exc.title,
        "status": exc.status,
        "code": exc.code,
        "detail": exc.detail,
        "correlationId": correlation_id,
    }
    if exc.errors:
        body["errors"] = exc.errors
    return JSONResponse(
        status_code=exc.status, content=body, media_type="application/problem+json"
    )


async def api_problem_handler(request: Request, exc: ApiProblem):
    return problem_response(request, exc)

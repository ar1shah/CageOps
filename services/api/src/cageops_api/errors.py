"""One error shape for the whole API: {"detail": <what happened>, "code": <stable machine name>}.

`code` is for programs (the frontend branches on it), `detail` is for people.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ErrorBody(BaseModel):
    detail: str
    code: str


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        detail: str,
        *,
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.headers = headers or {}
        self.extra = extra or {}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError) -> JSONResponse:
        body = {"detail": exc.detail, "code": exc.code, **exc.extra}
        return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"where": ".".join(str(p) for p in e["loc"]), "problem": e["msg"]} for e in exc.errors()
        ]
        first = errors[0] if errors else {"where": "request", "problem": "invalid"}
        return JSONResponse(
            status_code=422,
            content={
                "detail": f"{first['where']}: {first['problem']}",
                "code": "validation_error",
                "errors": errors,
            },
        )

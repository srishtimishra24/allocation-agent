"""Shared helpers for the three stage services.

Each stage runs as its own process with its own state. They do not share memory.
The only thing they share is this module of dumb utilities, plus the fact that
`bind` calls `publish` and `spend` over HTTP to independently verify artefacts
it was handed. That cross-check is deliberate: it is what makes `bind` a real
third system rather than a mock that trusts its caller.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse

# Ports. Overridable so tests can run on a private range.
PUBLISH_PORT = int(os.getenv("PUBLISH_PORT", "8101"))
SPEND_PORT = int(os.getenv("SPEND_PORT", "8102"))
BIND_PORT = int(os.getenv("BIND_PORT", "8103"))

PUBLISH_URL = os.getenv("PUBLISH_URL", f"http://127.0.0.1:{PUBLISH_PORT}")
SPEND_URL = os.getenv("SPEND_URL", f"http://127.0.0.1:{SPEND_PORT}")
BIND_URL = os.getenv("BIND_URL", f"http://127.0.0.1:{BIND_PORT}")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def now() -> float:
    return time.time()


class StageError(HTTPException):
    """A refusal that carries a machine-readable code the agent can classify on.

    The whole design rests on the agent being able to tell *why* a stage said no.
    A bare 500 is useless; every refusal here names its own failure code and
    attaches whatever structured hint would let a caller fix it.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable_hint: bool | None = None,
        **detail: Any,
    ) -> None:
        payload: dict[str, Any] = {
            "code": code,
            "message": message,
            # The service offers an *opinion* on retryability. The agent is not
            # obliged to believe it - see agent/taxonomy.py.
            "retryable_hint": retryable_hint,
        }
        payload.update(detail)
        super().__init__(status_code=status_code, detail=payload)


def stage_error_handler(_request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if not isinstance(detail, dict):
        detail = {"code": "UNSPECIFIED", "message": str(detail), "retryable_hint": None}
    return JSONResponse(status_code=exc.status_code, content=detail)

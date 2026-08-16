"""Tool layer: the agent's only way to touch the three services.

Every stage is at least one HTTP call to a separate process. Nothing here
interprets failures - it just turns non-2xx responses into a typed StageFailure
and hands it up. Classification is somebody else's job (agent/taxonomy.py).
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from .models import Stage, StageFailure

PUBLISH_URL = os.getenv("PUBLISH_URL", "http://127.0.0.1:8101")
SPEND_URL = os.getenv("SPEND_URL", "http://127.0.0.1:8102")
BIND_URL = os.getenv("BIND_URL", "http://127.0.0.1:8103")


class ToolFailure(Exception):
    def __init__(self, failure: StageFailure) -> None:
        super().__init__(str(failure))
        self.failure = failure


class Tools:
    """Thin async client set. One instance per run; journals every call."""

    def __init__(self, journal=None, timeout: float = 10.0) -> None:
        self._client = httpx.AsyncClient(timeout=timeout, trust_env=False)
        self.journal = journal
        self.call_count = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, stage: Stage, method: str, url: str, **kw) -> dict[str, Any]:
        self.call_count += 1
        if self.journal:
            self.journal.tool_call(stage, method, url, kw.get("json"))
        try:
            resp = await self._client.request(method, url, **kw)
        except httpx.HTTPError as exc:
            failure = StageFailure(stage, "SERVICE_UNAVAILABLE", str(exc), 0, {"retryable_hint": True})
            if self.journal:
                self.journal.tool_result(stage, None, failure)
            raise ToolFailure(failure) from exc

        try:
            body = resp.json()
        except Exception:
            body = {"code": "MALFORMED_RESPONSE", "message": resp.text[:200]}

        if resp.status_code >= 400:
            failure = StageFailure(
                stage=stage,
                code=body.get("code", "UNSPECIFIED"),
                message=body.get("message", ""),
                http_status=resp.status_code,
                detail=body,
            )
            if self.journal:
                self.journal.tool_result(stage, None, failure)
            raise ToolFailure(failure)

        if self.journal:
            self.journal.tool_result(stage, body, None)
        return body

    # ------------------------------------------------------------ publish --
    async def publish(
        self, *, allocation_id: str, content_ref: str, tier: str,
        compliance_ref: str | None, ttl_seconds: float = 900.0,
    ) -> dict[str, Any]:
        return await self._call(
            Stage.PUBLISH, "POST", f"{PUBLISH_URL}/v1/publications",
            json={
                "allocation_id": allocation_id,
                "content_ref": content_ref,
                "tier": tier,
                "compliance_ref": compliance_ref,
                "receipt_ttl_seconds": ttl_seconds,
            },
        )

    async def get_receipt(self, receipt_id: str) -> dict[str, Any]:
        return await self._call(Stage.PUBLISH, "GET", f"{PUBLISH_URL}/v1/publications/{receipt_id}")

    async def revoke_receipt(self, receipt_id: str) -> dict[str, Any]:
        return await self._call(
            Stage.PUBLISH, "POST", f"{PUBLISH_URL}/v1/publications/{receipt_id}:revoke"
        )

    # -------------------------------------------------------------- spend --
    async def reserve(
        self, *, allocation_id: str, amount: int, budget_code: str, ttl_seconds: float
    ) -> dict[str, Any]:
        return await self._call(
            Stage.SPEND, "POST", f"{SPEND_URL}/v1/reservations",
            json={
                "allocation_id": allocation_id,
                "amount": amount,
                "budget_code": budget_code,
                "ttl_seconds": ttl_seconds,
            },
        )

    async def get_reservation(self, rid: str) -> dict[str, Any]:
        return await self._call(Stage.SPEND, "GET", f"{SPEND_URL}/v1/reservations/{rid}")

    async def amend_reservation(self, rid: str, new_amount: int, approver: str | None = None) -> dict[str, Any]:
        return await self._call(
            Stage.SPEND, "PATCH", f"{SPEND_URL}/v1/reservations/{rid}",
            json={"new_amount": new_amount, "approver": approver},
        )

    async def extend_reservation(self, rid: str, seconds: float = 300.0) -> dict[str, Any]:
        return await self._call(
            Stage.SPEND, "POST", f"{SPEND_URL}/v1/reservations/{rid}:extend",
            json={"seconds": seconds},
        )

    async def release_reservation(self, rid: str) -> dict[str, Any]:
        return await self._call(Stage.SPEND, "POST", f"{SPEND_URL}/v1/reservations/{rid}:release")

    async def budget(self, budget_code: str) -> dict[str, Any]:
        return await self._call(Stage.SPEND, "GET", f"{SPEND_URL}/v1/budgets/{budget_code}")

    # --------------------------------------------------------------- bind --
    async def bind(
        self, *, allocation_id: str, resource_id: str, window: str,
        publish_receipt_id: str, reservation_id: str,
    ) -> dict[str, Any]:
        return await self._call(
            Stage.BIND, "POST", f"{BIND_URL}/v1/bindings",
            json={
                "allocation_id": allocation_id,
                "resource_id": resource_id,
                "window": window,
                "publish_receipt_id": publish_receipt_id,
                "reservation_id": reservation_id,
            },
        )

    async def availability(self, resource_id: str) -> dict[str, Any]:
        return await self._call(
            Stage.BIND, "GET", f"{BIND_URL}/v1/resources/{resource_id}/availability"
        )

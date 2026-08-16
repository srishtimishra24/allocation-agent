"""Stage 1 - PUBLISH. Content validation and receipt issuance.

Business rules this service enforces on its own, with no knowledge of the other
two stages:

  * A publication receipt is scoped to one allocation and one content version.
  * Issuing a new receipt for an allocation REVOKES the previous one. Content is
    versioned; you cannot have two live receipts for the same allocation.
    This is the rule that makes naive "just re-run everything" destructive:
    re-publishing silently invalidates the receipt a later stage may already be
    holding.
  * The `certified` tier requires a compliance reference. Without one the
    service refuses, and no amount of retrying will change that.
  * Receipts expire.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .common import StageError, new_id, now, stage_error_handler

STAGE = "publish"
# Compute units burned by a successful publish. Content moderation + embedding
# recompute is the expensive part of this pipeline; this is why the agent tries
# very hard not to redo it.
COST_UNITS = 40

app = FastAPI(title="publish-service", version="1.0.0")
app.add_exception_handler(HTTPException, stage_error_handler)

RECEIPT_TTL_SECONDS = 900.0


class State:
    def __init__(self) -> None:
        self.receipts: dict[str, dict[str, Any]] = {}
        self.by_allocation: dict[str, str] = {}
        self.calls: list[dict[str, Any]] = []
        self.units_spent: int = 0
        # Fault injection, driven by the scenario runner.
        self.latency_ms: int = 0
        self.fail_next: list[dict[str, Any]] = []


S = State()


class PublishRequest(BaseModel):
    allocation_id: str
    content_ref: str
    tier: str = Field(default="standard", pattern="^(standard|certified)$")
    compliance_ref: str | None = None
    receipt_ttl_seconds: float = RECEIPT_TTL_SECONDS


def _maybe_inject() -> None:
    if S.fail_next:
        f = S.fail_next.pop(0)
        raise StageError(
            f.get("status", 503),
            f["code"],
            f.get("message", "injected fault"),
            retryable_hint=f.get("retryable_hint", True),
            **{k: v for k, v in f.items() if k not in {"status", "code", "message", "retryable_hint"}},
        )


@app.post("/v1/publications")
async def publish(req: PublishRequest) -> dict[str, Any]:
    S.calls.append({"op": "publish", "allocation_id": req.allocation_id, "tier": req.tier, "t": now()})
    if S.latency_ms:
        await asyncio.sleep(S.latency_ms / 1000)
    _maybe_inject()

    if not req.content_ref.strip():
        raise StageError(422, "CONTENT_EMPTY", "content_ref must not be blank", retryable_hint=False)

    if req.tier == "certified" and not req.compliance_ref:
        # Structural and permanent for this input. Retrying is pointless; the
        # caller has to change the request or give up on the certified tier.
        raise StageError(
            422,
            "CONTENT_NOT_CERTIFIABLE",
            "tier=certified requires a compliance_ref",
            retryable_hint=False,
            remediation="supply compliance_ref or downgrade tier",
        )

    # Versioning: supersede any live receipt for this allocation.
    superseded = None
    prior_id = S.by_allocation.get(req.allocation_id)
    if prior_id and S.receipts[prior_id]["state"] == "ACTIVE":
        S.receipts[prior_id]["state"] = "SUPERSEDED"
        superseded = prior_id

    receipt_id = new_id("pub")
    S.receipts[receipt_id] = {
        "receipt_id": receipt_id,
        "allocation_id": req.allocation_id,
        "content_ref": req.content_ref,
        "tier": req.tier,
        "state": "ACTIVE",
        "issued_at": now(),
        "expires_at": now() + req.receipt_ttl_seconds,
    }
    S.by_allocation[req.allocation_id] = receipt_id
    S.units_spent += COST_UNITS
    return {
        "receipt_id": receipt_id,
        "tier": req.tier,
        "state": "ACTIVE",
        "expires_at": S.receipts[receipt_id]["expires_at"],
        "superseded_receipt_id": superseded,
        "cost_units": COST_UNITS,
    }


@app.get("/v1/publications/{receipt_id}")
async def get_receipt(receipt_id: str) -> dict[str, Any]:
    r = S.receipts.get(receipt_id)
    if not r:
        raise StageError(404, "RECEIPT_NOT_FOUND", f"no receipt {receipt_id}", retryable_hint=False)
    state = r["state"]
    if state == "ACTIVE" and now() > r["expires_at"]:
        state = "EXPIRED"
    return {**r, "state": state}


@app.post("/v1/publications/{receipt_id}:revoke")
async def revoke(receipt_id: str) -> dict[str, Any]:
    r = S.receipts.get(receipt_id)
    if not r:
        raise StageError(404, "RECEIPT_NOT_FOUND", f"no receipt {receipt_id}", retryable_hint=False)
    r["state"] = "REVOKED"
    return {"receipt_id": receipt_id, "state": "REVOKED"}


# ---------------------------------------------------------------- control ---
@app.post("/_control/reset")
async def reset() -> dict[str, Any]:
    global S
    S = State()
    return {"ok": True}


@app.post("/_control/configure")
async def configure(cfg: dict[str, Any]) -> dict[str, Any]:
    S.latency_ms = cfg.get("latency_ms", S.latency_ms)
    if "fail_next" in cfg:
        S.fail_next = list(cfg["fail_next"])
    return {"ok": True}


@app.get("/_control/journal")
async def journal() -> dict[str, Any]:
    return {"stage": STAGE, "calls": S.calls, "units_spent": S.units_spent, "receipts": S.receipts}


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "stage": STAGE}

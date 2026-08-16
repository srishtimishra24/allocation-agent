"""Stage 3 - BIND. Commitment lock on a concrete resource window.

This is the stage that fails after the other two have already succeeded, so it
is the one that has to be honest about *why*. It refuses for five distinct
reasons and each refusal carries enough structure for a caller to act on it:

  LOCK_CONTENDED        soft lock held by another process, expires shortly
  SLOT_CONFLICT         window is permanently committed; alternatives attached
  PUBLISH_TIER_MISMATCH resource class needs a higher content tier
  PUBLISH_RECEIPT_INVALID the receipt was superseded/revoked/expired
  RESERVATION_INSUFFICIENT hold is smaller than the slot price

Crucially, bind does not trust the identifiers it is handed. It calls the
publish and spend services itself to verify them. That is what makes the three
stages independently-enforcing systems rather than one system in three files -
and it is why "re-publish then re-bind" breaks: the old receipt bind was going
to check has been superseded out from under it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .common import PUBLISH_URL, SPEND_URL, StageError, new_id, now, stage_error_handler

STAGE = "bind"
# Cheapest stage by a wide margin. Retrying only this one is nearly free, which
# is the entire economic argument for targeted recovery.
COST_UNITS = 10

TIER_RANK = {"standard": 1, "certified": 2}

app = FastAPI(title="bind-service", version="1.0.0")
app.add_exception_handler(HTTPException, stage_error_handler)


def default_catalogue() -> dict[str, dict[str, Any]]:
    """Windows on one GPU cluster. Price and tier vary by window."""
    return {
        "2026-08-17T09:00": {"price": 60_000, "required_tier": "standard", "label": "off-peak"},
        "2026-08-17T11:00": {"price": 72_000, "required_tier": "standard", "label": "peak"},
        "2026-08-17T14:00": {"price": 60_000, "required_tier": "standard", "label": "off-peak"},
        "2026-08-17T16:00": {"price": 60_000, "required_tier": "certified", "label": "regulated"},
    }


class State:
    def __init__(self) -> None:
        self.resource_id = "gpu-cluster-a"
        self.catalogue = default_catalogue()
        # window -> allocation_id, permanent
        self.committed: dict[str, str] = {}
        # window -> expiry timestamp, transient
        self.soft_locks: dict[str, float] = {}
        self.bindings: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.units_spent: int = 0
        self.latency_ms: int = 0
        self.fail_next: list[dict[str, Any]] = []


S = State()


class BindRequest(BaseModel):
    allocation_id: str
    resource_id: str
    window: str
    publish_receipt_id: str
    reservation_id: str


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


def _alternatives(exclude: str, needed_tier_available: set[str]) -> list[dict[str, Any]]:
    out = []
    for w, meta in sorted(S.catalogue.items()):
        if w == exclude or w in S.committed:
            continue
        out.append(
            {
                "window": w,
                "price": meta["price"],
                "required_tier": meta["required_tier"],
                "label": meta["label"],
                "tier_satisfied_now": meta["required_tier"] in needed_tier_available,
            }
        )
    return out


@app.post("/v1/bindings")
async def bind(req: BindRequest) -> dict[str, Any]:
    S.calls.append({"op": "bind", "allocation_id": req.allocation_id, "window": req.window, "t": now()})
    if S.latency_ms:
        await asyncio.sleep(S.latency_ms / 1000)
    _maybe_inject()

    meta = S.catalogue.get(req.window)
    if meta is None:
        raise StageError(404, "WINDOW_UNKNOWN", f"no window {req.window}", retryable_hint=False)

    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        # --- independently verify the publish artefact -----------------------
        pr = await client.get(f"{PUBLISH_URL}/v1/publications/{req.publish_receipt_id}")
        if pr.status_code != 200:
            raise StageError(
                409, "PUBLISH_RECEIPT_INVALID", "publish stage does not recognise this receipt",
                retryable_hint=False, upstream=pr.json(),
            )
        receipt = pr.json()
        held_tier = receipt["tier"]
        if receipt["state"] != "ACTIVE":
            raise StageError(
                409,
                "PUBLISH_RECEIPT_INVALID",
                f"receipt is {receipt['state']}, not ACTIVE",
                retryable_hint=False,
                receipt_state=receipt["state"],
                remediation="replay publish stage",
            )
        if TIER_RANK[held_tier] < TIER_RANK[meta["required_tier"]]:
            raise StageError(
                409,
                "PUBLISH_TIER_MISMATCH",
                f"window {req.window} requires tier {meta['required_tier']}, receipt is {held_tier}",
                retryable_hint=False,
                required_tier=meta["required_tier"],
                held_tier=held_tier,
                remediation="replay publish stage at the required tier; reservation stays valid",
            )

        # --- independently verify the spend artefact -------------------------
        rr = await client.get(f"{SPEND_URL}/v1/reservations/{req.reservation_id}")
        if rr.status_code != 200:
            raise StageError(
                409, "RESERVATION_INVALID", "spend stage does not recognise this reservation",
                retryable_hint=False, upstream=rr.json(),
            )
        reservation = rr.json()
        if reservation["state"] != "HELD":
            raise StageError(
                409,
                "RESERVATION_EXPIRED" if reservation["state"] == "EXPIRED" else "RESERVATION_INVALID",
                f"reservation is {reservation['state']}",
                retryable_hint=False,
                reservation_state=reservation["state"],
                remediation="replay spend stage",
            )
        if reservation["amount"] < meta["price"]:
            raise StageError(
                409,
                "RESERVATION_INSUFFICIENT",
                f"window costs {meta['price']}, hold is {reservation['amount']}",
                retryable_hint=False,
                required_amount=meta["price"],
                held_amount=reservation["amount"],
                remediation="amend the reservation upward",
            )

        # --- lock checks -----------------------------------------------------
        if req.window in S.committed and S.committed[req.window] != req.allocation_id:
            tiers_available = {held_tier}
            raise StageError(
                409,
                "SLOT_CONFLICT",
                f"window {req.window} is permanently committed to {S.committed[req.window]}",
                retryable_hint=False,
                conflicting_allocation=S.committed[req.window],
                alternatives=_alternatives(req.window, tiers_available),
                remediation="choose a different window",
            )

        lock_expiry = S.soft_locks.get(req.window)
        if lock_expiry and now() < lock_expiry:
            raise StageError(
                409,
                "LOCK_CONTENDED",
                f"window {req.window} is soft-locked by a concurrent transaction",
                retryable_hint=True,
                retry_after_seconds=round(max(0.05, lock_expiry - now()), 2),
            )
        if lock_expiry:
            del S.soft_locks[req.window]

        # --- commit ----------------------------------------------------------
        cr = await client.post(f"{SPEND_URL}/v1/reservations/{req.reservation_id}:commit")
        if cr.status_code != 200:
            raise StageError(409, "COMMIT_FAILED", "spend refused to commit the hold",
                             retryable_hint=False, upstream=cr.json())

    S.committed[req.window] = req.allocation_id
    binding_id = new_id("bind")
    S.bindings[binding_id] = {
        "binding_id": binding_id,
        "allocation_id": req.allocation_id,
        "resource_id": req.resource_id,
        "window": req.window,
        "price": meta["price"],
        "bound_at": now(),
    }
    S.units_spent += COST_UNITS
    return {**S.bindings[binding_id], "cost_units": COST_UNITS}


@app.get("/v1/resources/{resource_id}/availability")
async def availability(resource_id: str) -> dict[str, Any]:
    return {
        "resource_id": resource_id,
        "windows": [
            {
                "window": w,
                **meta,
                "committed_to": S.committed.get(w),
                "soft_locked": bool(S.soft_locks.get(w, 0) > now()),
            }
            for w, meta in sorted(S.catalogue.items())
        ],
    }


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
    for w, alloc in cfg.get("commit_windows", {}).items():
        S.committed[w] = alloc
    for w, secs in cfg.get("soft_lock_windows", {}).items():
        S.soft_locks[w] = now() + float(secs)
    return {"ok": True}


@app.get("/_control/journal")
async def journal() -> dict[str, Any]:
    return {
        "stage": STAGE,
        "calls": S.calls,
        "units_spent": S.units_spent,
        "committed": S.committed,
        "bindings": S.bindings,
    }


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "stage": STAGE}

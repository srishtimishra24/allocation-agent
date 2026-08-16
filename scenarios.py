"""Scenario definitions.

Each scenario configures the three services into a specific world state and
hands the agent a request. Nothing here tells the agent what to do - the
scenario only sets up reality; the recovery choice is the agent's.

The two headline scenarios required by the brief are `transient` and
`structural_escalate`. The other four exist because the interesting question is
not "can it retry" but "how many genuinely different recovery shapes does it
distinguish", and the answer here is five.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from agent.models import AllocationRequest

PUBLISH_URL = "http://127.0.0.1:8101"
SPEND_URL = "http://127.0.0.1:8102"
BIND_URL = "http://127.0.0.1:8103"


@dataclass
class Scenario:
    key: str
    title: str
    story: str
    naive_would: str
    request: AllocationRequest
    publish_cfg: dict[str, Any] = field(default_factory=dict)
    spend_cfg: dict[str, Any] = field(default_factory=dict)
    bind_cfg: dict[str, Any] = field(default_factory=dict)
    # What the tests assert.
    expect_outcome: str = "completed"
    expect_plan: str | None = None
    expect_max_units: int | None = None
    auto_approve: bool = False


def _req(**kw: Any) -> AllocationRequest:
    base = dict(
        allocation_id="alloc-7741",
        content_ref="s3://campaigns/q3-inference-run",
        resource_id="gpu-cluster-a",
        window="2026-08-17T09:00",
        amount=60_000,
        tier="standard",
        compliance_ref=None,
    )
    base.update(kw)
    return AllocationRequest(**base)  # type: ignore[arg-type]


SCENARIOS: dict[str, Scenario] = {
    # ------------------------------------------------------------------ 1 --
    "transient": Scenario(
        key="transient",
        title="Transient lock contention - targeted retry of bind only",
        story=(
            "Publish and spend both succeed. Bind loses a race: another transaction is "
            "holding a short-lived soft lock on the 09:00 window. The lock expires on its "
            "own in about a second."
        ),
        naive_would=(
            "A full replay releases the 60,000 budget hold. A process that has been "
            "waiting on that pool takes 45,000 the instant it is freed, and the re-reserve "
            "fails with BUDGET_EXHAUSTED. The allocation is lost to fix a one-second lock."
        ),
        request=_req(),
        spend_cfg={"predatory_drain_on_release": 45_000},
        bind_cfg={"soft_lock_windows": {"2026-08-17T09:00": 1.2}},
        expect_outcome="completed",
        expect_plan="targeted_retry",
        expect_max_units=90,
    ),
    # ------------------------------------------------------------------ 2 --
    "structural_escalate": Scenario(
        key="structural_escalate",
        title="Structural conflict with no affordable alternative - escalate",
        story=(
            "Bind rejects: the 09:00 window was permanently committed by another allocation "
            "while we were working. 14:00 has gone the same way. The only same-tier window "
            "left is 11:00 at peak price (72,000 vs the 60,000 we hold), and the cheap 16:00 "
            "window needs a certified publish tier we cannot obtain without a compliance "
            "reference."
        ),
        naive_would=(
            "Retrying bind is pointless - the slot is committed, not locked. Replaying "
            "everything does not conjure a cheaper window either. The correct move is to "
            "stop spending compute and ask the budget owner for 12,000 more."
        ),
        request=_req(reservation_ttl_seconds=200.0, human_sla_seconds=420.0),
        bind_cfg={
            "commit_windows": {
                "2026-08-17T09:00": "alloc-9002-other",
                "2026-08-17T14:00": "alloc-9114-other",
            }
        },
        expect_outcome="escalated",
        expect_plan="escalate",
        expect_max_units=90,
    ),
    # ------------------------------------------------------------------ 3 --
    "structural_escalate_approved": Scenario(
        key="structural_escalate_approved",
        title="Same conflict, human approves - resume, do not restart",
        story=(
            "Identical to structural_escalate, but the budget owner approves the 12,000 "
            "increase. The agent resumes from the artefacts it kept: amend the existing "
            "hold, bind the peak window. Publish is never touched."
        ),
        naive_would=(
            "A restart-on-approval design would redo publish and spend: 75 units and a "
            "fresh trip through the shared budget pool, for an approval that only ever "
            "concerned the amount."
        ),
        request=_req(reservation_ttl_seconds=200.0, human_sla_seconds=420.0),
        bind_cfg={
            "commit_windows": {
                "2026-08-17T09:00": "alloc-9002-other",
                "2026-08-17T14:00": "alloc-9114-other",
            }
        },
        auto_approve=True,
        expect_outcome="completed",
        expect_plan="amend_then_bind[peak]",
        expect_max_units=140,
    ),
    # ------------------------------------------------------------------ 4 --
    "param_change": Scenario(
        key="param_change",
        title="Structural conflict with a free equivalent slot - change one parameter",
        story=(
            "The 09:00 window is committed elsewhere, but 14:00 is free at the same price "
            "and the same tier. Nothing upstream needs to change."
        ),
        naive_would=(
            "Retrying the same window forever, or throwing away 65 units of valid publish "
            "and spend work to reach a slot that the existing hold already covers."
        ),
        request=_req(),
        bind_cfg={"commit_windows": {"2026-08-17T09:00": "alloc-9002-other"}},
        expect_outcome="completed",
        expect_plan="reprice_window[off-peak]",
        expect_max_units=95,
    ),
    # ------------------------------------------------------------------ 5 --
    "partial_replay": Scenario(
        key="partial_replay",
        title="Upstream artefact void - replay one stage, keep the other",
        story=(
            "The target window is the regulated 16:00 slot, which bind independently "
            "requires a certified publish receipt for. Our receipt is standard. The budget "
            "hold is completely fine."
        ),
        naive_would=(
            "Full replay costs 75 units and releases a budget hold that was never the "
            "problem. Only the publish artefact is void."
        ),
        request=_req(window="2026-08-17T16:00", compliance_ref="COMP-2026-118"),
        expect_outcome="completed",
        expect_plan="replay_from_publish",
        expect_max_units=140,
    ),
    # ------------------------------------------------------------------ 6 --
    "hold_expired": Scenario(
        key="hold_expired",
        title="Budget hold lapses mid-flight - replay spend only, with a longer TTL",
        story=(
            "The reservation was created with a 1-second TTL and bind is running slowly. "
            "By the time bind checks, the hold has expired and the money is back in the "
            "pool. Publish is untouched."
        ),
        naive_would=(
            "Redoing publish as well wastes 40 units on a receipt that is still active - "
            "and re-publishing would supersede it, which is actively harmful."
        ),
        request=_req(reservation_ttl_seconds=1.0),
        bind_cfg={"latency_ms": 1500},
        expect_outcome="completed",
        expect_plan="replay_from_spend",
        expect_max_units=140,
    ),
    # ------------------------------------------------------------------ 7 --
    "compound": Scenario(
        key="compound",
        title="Two different failures in one run - two different recoveries",
        story=(
            "Bind is degraded on the first call (transient). The agent retries bind alone "
            "and the retry gets a different answer: the 09:00 window has been committed by "
            "another allocation in the meantime (structural). One run, two failure classes, "
            "two recovery shapes, and the expensive stages still run exactly once."
        ),
        naive_would=(
            "Treat the second failure the same way as the first and keep retrying a slot "
            "that is gone - or, having recovered once, conclude the workflow is cursed and "
            "restart it."
        ),
        request=_req(),
        bind_cfg={
            "fail_next": [
                {
                    "status": 503,
                    "code": "BIND_SERVICE_DEGRADED",
                    "message": "commitment ledger replica is catching up",
                    "retryable_hint": True,
                }
            ],
            "commit_windows": {"2026-08-17T09:00": "alloc-9002-other"},
        },
        expect_outcome="completed",
        expect_plan="reprice_window[off-peak]",
        expect_max_units=110,
    ),
    # ------------------------------------------------------------------ 8 --
    "budget_terminal": Scenario(
        key="budget_terminal",
        title="Shared pool drained before we get there - terminal, escalate immediately",
        story=(
            "Publish succeeds. Another process has already taken most of the pool, so the "
            "reservation is refused outright. No retry and no parameter change can create "
            "budget."
        ),
        naive_would=(
            "Retry loops against BUDGET_EXHAUSTED burn compute and delay the escalation "
            "that was needed from the first refusal."
        ),
        request=_req(),
        spend_cfg={"pool_total": 100_000},
        expect_outcome="escalated",
        expect_plan="escalate",
        expect_max_units=90,
    ),
}

# budget_terminal needs the drain applied at setup time, not on release.
SCENARIOS["budget_terminal"].spend_cfg["_drain_now"] = 70_000


async def setup(scn: Scenario) -> None:
    """Reset all three services and configure them for this scenario."""
    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as c:
        for url in (PUBLISH_URL, SPEND_URL, BIND_URL):
            await c.post(f"{url}/_control/reset")
        if scn.publish_cfg:
            await c.post(f"{PUBLISH_URL}/_control/configure", json=scn.publish_cfg)
        spend_cfg = {k: v for k, v in scn.spend_cfg.items() if k != "_drain_now"}
        if spend_cfg:
            await c.post(f"{SPEND_URL}/_control/configure", json=spend_cfg)
        if "_drain_now" in scn.spend_cfg:
            await c.post(
                f"{SPEND_URL}/_control/concurrent_drain",
                json={"amount": scn.spend_cfg["_drain_now"]},
            )
        if scn.bind_cfg:
            await c.post(f"{BIND_URL}/_control/configure", json=scn.bind_cfg)


async def services_up() -> bool:
    async with httpx.AsyncClient(timeout=2.0, trust_env=False) as c:
        for url in (PUBLISH_URL, SPEND_URL, BIND_URL):
            try:
                r = await c.get(f"{url}/healthz")
                if r.status_code != 200:
                    return False
            except httpx.HTTPError:
                return False
    return True

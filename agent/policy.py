"""The recovery policy: enumerate, veto, score, choose.

This is deliberately not "if transient: retry". The agent builds every recovery
plan that is even arguably available - including the naive full replay - runs
them all through hard guardrails, prices the survivors, and picks the best. The
rejected plans stay in the log, because "why didn't you just retry everything"
is the question a reviewer will ask, and the answer should be visible.

Three layers, in order:

  1. GENERATE   candidate plans, from the failure classification and live state
  2. VETO       hard constraints. A vetoed plan is unavailable at any price.
  3. SCORE      expected value: p(success) * value - compute - risk - human cost

Guardrails beat scores. A cost-minimising scorer alone will eventually do
something unsafe for a small saving; the veto layer is what stops it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import cost as C
from .models import (
    ActionType,
    RecoveryPlan,
    Stage,
    StageFailure,
    Step,
    WorkflowState,
)
from .taxonomy import Classification, FailureClass

# Rough wall-clock per stage, used only to check a plan fits inside the
# remaining budget-hold lifetime.
STAGE_SECONDS = {Stage.PUBLISH: 0.4, Stage.SPEND: 0.3, Stage.BIND: 0.3}

AMEND_AUTO_APPROVE_PCT = 0.10  # mirrors the spend service's own rule


@dataclass
class RecoveryContext:
    """Evidence gathered by read-only probes before deciding anything.

    The agent looks before it leaps: it asks spend how much budget is actually
    left and how long its hold has, and asks bind what else is free. Deciding
    from stale assumptions is how you pick a plan that was correct 30 seconds
    ago.
    """

    pool_available: int = 0
    reservation: dict[str, Any] | None = None
    hold_seconds_remaining: float = 0.0
    hold_extensions_used: int = 0
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    window_catalogue: dict[str, dict[str, Any]] = field(default_factory=dict)


def _stage_cost(stages: list[Stage]) -> int:
    return sum(C.STAGE_COST[s] for s in stages)


# --------------------------------------------------------------- generate ---
def generate_candidates(
    state: WorkflowState,
    failure: StageFailure,
    cls: Classification,
    ctx: RecoveryContext,
) -> list[RecoveryPlan]:
    plans: list[RecoveryPlan] = []
    stage = failure.stage
    attempts = state.attempts.get(stage, 0)

    # -- 0. the naive baseline, always enumerated so it can be seen losing ----
    plans.append(
        RecoveryPlan(
            name="full_replay",
            steps=[
                Step(ActionType.RELEASE_HOLD),
                Step(ActionType.RUN_STAGE, Stage.PUBLISH),
                Step(ActionType.RUN_STAGE, Stage.SPEND),
                Step(ActionType.RUN_STAGE, Stage.BIND),
            ],
            rationale="Discard everything and redo the sequence from scratch.",
            estimated_units=C.FULL_SEQUENCE_COST,
            # A full replay does not fix a structural cause, and it re-enters a
            # budget pool that other processes are drawing from.
            p_success=0.45 if cls.failure_class is FailureClass.TRANSIENT else 0.15,
        )
    )

    # -- 1. identical retry of just the failed stage -------------------------
    if cls.p_same_retry > 0:
        wait = float(failure.detail.get("retry_after_seconds", 0.5))
        # Confidence decays with each *repeat* attempt. The first recovery gets
        # the taxonomy's full prior; if the "transient" lock is still there on
        # the third try, the transient hypothesis is probably wrong, and the
        # score falls below escalation on its own before G4's hard cap bites.
        decay = 0.85 ** max(0, attempts - 1)
        plans.append(
            RecoveryPlan(
                name="targeted_retry",
                steps=[Step(ActionType.RETRY_SAME, stage, wait_seconds=wait)],
                rationale=(
                    f"{failure.code} is transient ({cls.note}). Only {stage} needs redoing; "
                    f"the publish receipt and budget hold are untouched, so this costs "
                    f"{C.STAGE_COST[stage]}u instead of {C.FULL_SEQUENCE_COST}u."
                ),
                estimated_units=C.STAGE_COST[stage],
                p_success=min(0.95, cls.p_same_retry * decay),
            )
        )

    # -- 2. same stage, different parameters ---------------------------------
    if "needs_alternative_window" in cls.tags:
        held_tier = _held_tier(state)
        for alt in sorted(ctx.alternatives, key=lambda a: a["price"]):
            price = alt["price"]
            window = alt["window"]
            tier_ok = alt["required_tier"] == held_tier or _tier_rank(held_tier) >= _tier_rank(
                alt["required_tier"]
            )
            held = ctx.reservation["amount"] if ctx.reservation else state.request.amount
            delta = price - held

            if tier_ok and delta <= 0:
                plans.append(
                    RecoveryPlan(
                        name=f"reprice_window[{alt['label']}]",
                        steps=[Step(ActionType.RETRY_MODIFIED, Stage.BIND, {"window": window})],
                        rationale=(
                            f"{window} ({alt['label']}) is free at {price}, which the existing "
                            f"{held} hold already covers, and the current receipt tier satisfies "
                            f"it. One bind call fixes this; nothing upstream changes."
                        ),
                        estimated_units=C.STAGE_COST[Stage.BIND],
                        p_success=0.9,
                    )
                )
            elif tier_ok and delta > 0:
                plans.append(
                    RecoveryPlan(
                        name=f"amend_then_bind[{alt['label']}]",
                        steps=[
                            Step(ActionType.AMEND_SPEND, Stage.SPEND, {"new_amount": price}),
                            Step(ActionType.RETRY_MODIFIED, Stage.BIND, {"window": window}),
                        ],
                        rationale=(
                            f"{window} costs {price}, {delta} more than the {held} hold. Amend the "
                            f"existing reservation in place rather than releasing and re-reserving - "
                            f"a release puts {held} back in a pool other processes are drawing from."
                        ),
                        estimated_units=C.STAGE_COST[Stage.SPEND] + C.STAGE_COST[Stage.BIND],
                        p_success=0.85,
                    )
                )
            elif not tier_ok:
                # Would need a higher publish tier. Priced as a partial replay.
                plans.append(
                    RecoveryPlan(
                        name=f"upgrade_tier_then_bind[{alt['label']}]",
                        steps=[
                            Step(
                                ActionType.REPLAY_FROM,
                                Stage.PUBLISH,
                                {"tier": alt["required_tier"]},
                            ),
                            Step(ActionType.RETRY_MODIFIED, Stage.BIND, {"window": window}),
                        ],
                        rationale=(
                            f"{window} needs tier {alt['required_tier']}; the live receipt is "
                            f"{held_tier}. Republishing at the higher tier keeps the budget hold "
                            f"intact, so this is {_stage_cost([Stage.PUBLISH, Stage.BIND])}u, not "
                            f"{C.FULL_SEQUENCE_COST}u."
                        ),
                        estimated_units=_stage_cost([Stage.PUBLISH, Stage.BIND]),
                        p_success=0.8,
                        intentional_redo={Stage.PUBLISH},
                    )
                )

    # -- 3. amend the hold in place (price mismatch, same window) ------------
    if "needs_amend" in cls.tags:
        required = int(failure.detail.get("required_amount", state.request.amount))
        plans.append(
            RecoveryPlan(
                name="amend_hold_in_place",
                steps=[
                    Step(ActionType.AMEND_SPEND, Stage.SPEND, {"new_amount": required}),
                    Step(ActionType.RETRY_SAME, Stage.BIND),
                ],
                rationale=(
                    f"The hold is short of the slot price. Amending the existing reservation to "
                    f"{required} keeps the money held throughout; re-reserving would drop it into "
                    f"the shared pool first."
                ),
                estimated_units=C.STAGE_COST[Stage.SPEND] + C.STAGE_COST[Stage.BIND],
                p_success=0.85,
            )
        )

    # -- 4. replay exactly one upstream stage --------------------------------
    if cls.replay_from is not None:
        rf = cls.replay_from
        params: dict[str, Any] = {}
        if failure.code == "PUBLISH_TIER_MISMATCH":
            params["tier"] = failure.detail.get("required_tier", "certified")
        if failure.code == "RESERVATION_EXPIRED":
            # Do not re-make the same mistake: the hold was too short for this
            # workflow's actual latency, so ask for a longer one.
            params["reservation_ttl_seconds"] = max(
                float(state.param("reservation_ttl_seconds")) * 4, 60.0
            )
        redo = [rf, Stage.BIND] if rf is not Stage.BIND else [Stage.BIND]
        kept = [s for s in state.completed_stages() if s not in redo]
        plans.append(
            RecoveryPlan(
                name=f"replay_from_{rf}",
                steps=[
                    Step(ActionType.REPLAY_FROM, rf, params),
                    Step(ActionType.RETRY_SAME, Stage.BIND),
                ],
                rationale=(
                    f"{failure.code} invalidates only the {rf} artefact ({cls.note}). "
                    f"Redo {rf} and {Stage.BIND}; keep {', '.join(str(k) for k in kept) or 'nothing'}. "
                    f"{_stage_cost(redo)}u instead of {C.FULL_SEQUENCE_COST}u."
                ),
                estimated_units=_stage_cost(redo),
                p_success=0.88,
                intentional_redo={rf},
            )
        )

    # -- 5. hand it to a human -----------------------------------------------
    plans.append(
        RecoveryPlan(
            name="escalate",
            steps=[Step(ActionType.ESCALATE)],
            rationale=(
                "No autonomous plan is both permitted and likely to work. Park the workflow "
                "with its completed artefacts intact and ask a human, rather than burning "
                "compute on retries that cannot succeed."
            ),
            estimated_units=0,
            p_success=0.9,
            human_cost=C.HUMAN_REVIEW_COST,
        )
    )

    # -- 6. give up ----------------------------------------------------------
    plans.append(
        RecoveryPlan(
            name="abort",
            steps=[Step(ActionType.RELEASE_HOLD), Step(ActionType.ABORT)],
            rationale="Unwind and drop the allocation.",
            estimated_units=0,
            p_success=0.0,
            risk_penalty=C.ALLOCATION_LOST_PENALTY,
        )
    )
    return plans


def _held_tier(state: WorkflowState) -> str:
    art = state.valid_artifact(Stage.PUBLISH)
    if art:
        return art.meta.get("tier", "standard")
    return state.param("tier")


def _tier_rank(t: str) -> int:
    return {"standard": 1, "certified": 2}.get(t, 0)


# ------------------------------------------------------------------ vetoes ---
def apply_guardrails(
    plans: list[RecoveryPlan],
    state: WorkflowState,
    failure: StageFailure,
    cls: Classification,
    ctx: RecoveryContext,
) -> None:
    """Hard constraints. These are not preferences - a vetoed plan cannot run."""

    still_valid = {s for s in state.completed_stages() if s not in cls.invalidates}

    for p in plans:
        redone = {s.stage for s in p.steps if s.action in
                  {ActionType.RUN_STAGE, ActionType.REPLAY_FROM} and s.stage}
        releases = any(s.action is ActionType.RELEASE_HOLD for s in p.steps)

        # G1 - never redo work whose output is still good and still needed.
        # Exempts stages the plan redoes deliberately as the fix itself.
        wasteful = (redone & still_valid) - {failure.stage} - p.intentional_redo
        if wasteful and p.name != "abort":
            p.vetoes.append(
                "G1 redundant-work: would re-execute "
                + ", ".join(sorted(str(s) for s in wasteful))
                + " whose artefacts are still valid"
            )

        # G2 - never release a hold into a pool that cannot refill it.
        if releases and ctx.reservation and p.name != "abort":
            need = ctx.reservation["amount"]
            if ctx.pool_available < need:
                p.vetoes.append(
                    f"G2 irreversible-release: the pool has only {ctx.pool_available} spare "
                    f"against a {need} hold, so releasing it is a bet that nothing else "
                    f"claims the money before we re-reserve"
                )

        # G3 - respect the compute ceiling.
        if state.units_spent + p.estimated_units > C.UNIT_BUDGET:
            p.vetoes.append(
                f"G3 unit-budget: {state.units_spent}+{p.estimated_units}u exceeds the "
                f"{C.UNIT_BUDGET}u ceiling"
            )

        # G4 - stop insisting a failure is transient.
        if p.name == "targeted_retry" and state.attempts.get(failure.stage, 0) >= C.MAX_SAME_RETRIES:
            p.vetoes.append(
                f"G4 retry-cap: {failure.stage} already attempted "
                f"{state.attempts.get(failure.stage)}x; the transient hypothesis is dead"
            )

        # G5 - the plan must finish before the budget hold lapses.
        if ctx.reservation and Stage.SPEND not in redone:
            need_s = sum(STAGE_SECONDS.get(s.stage, 0.2) + s.wait_seconds for s in p.steps)
            has_extend = any(s.action is ActionType.EXTEND_HOLD for s in p.steps)
            if not has_extend and need_s > ctx.hold_seconds_remaining and p.name not in {"escalate", "abort"}:
                p.vetoes.append(
                    f"G5 hold-lifetime: plan needs ~{need_s:.1f}s, hold has "
                    f"{ctx.hold_seconds_remaining:.1f}s left"
                )

        # G6 - do not propose spend increases beyond the agent's authority.
        for s in p.steps:
            if s.action is ActionType.AMEND_SPEND and ctx.reservation:
                original = ctx.reservation.get("original_amount", ctx.reservation["amount"])
                delta = s.params["new_amount"] - ctx.reservation["amount"]
                limit = original * AMEND_AUTO_APPROVE_PCT
                if delta > limit:
                    p.vetoes.append(
                        f"G6 spend-authority: +{delta} exceeds the {limit:.0f} auto-approval "
                        f"limit ({AMEND_AUTO_APPROVE_PCT:.0%} of {original}); needs budget_owner"
                    )
                elif delta > ctx.pool_available:
                    p.vetoes.append(
                        f"G6 spend-authority: +{delta} exceeds {ctx.pool_available} available"
                    )

        # G7 - pre-flight the upstream stage instead of learning by failing.
        for s in p.steps:
            if s.action is ActionType.REPLAY_FROM and s.stage is Stage.PUBLISH:
                tier = s.params.get("tier", state.param("tier"))
                if tier == "certified" and not state.request.compliance_ref:
                    p.vetoes.append(
                        "G7 preflight: publish will reject tier=certified without a "
                        "compliance_ref, so this plan cannot succeed"
                    )


# ------------------------------------------------------------------- score ---
def score(plans: list[RecoveryPlan]) -> None:
    for p in plans:
        if p.vetoes:
            p.score = float("-inf")
            continue
        p.score = (
            p.p_success * C.VALUE_OF_COMPLETION
            - p.estimated_units
            - p.risk_penalty
            - p.human_cost
        )


def choose(plans: list[RecoveryPlan]) -> RecoveryPlan:
    feasible = [p for p in plans if p.feasible]
    if not feasible:
        # Every autonomous option is vetoed and even escalate was blocked.
        return next(p for p in plans if p.name == "abort")
    return max(feasible, key=lambda p: p.score)


def decide(
    state: WorkflowState,
    failure: StageFailure,
    cls: Classification,
    ctx: RecoveryContext,
) -> tuple[RecoveryPlan, list[RecoveryPlan]]:
    plans = generate_candidates(state, failure, cls, ctx)
    apply_guardrails(plans, state, failure, cls, ctx)
    score(plans)
    return choose(plans), plans


# --------------------------------------------------- escalation ergonomics ---
def hold_strategy(ctx: RecoveryContext, human_sla_seconds: float) -> tuple[str, str]:
    """What to do with the budget hold while a human thinks.

    An expiring hold is not an asset. If it cannot survive the wait, releasing
    it early is strictly better for the organisation than letting it rot: the
    money goes back to the pool where something else can use it, and we record
    a replay plan so approval does not mean starting from zero.
    """
    if ctx.reservation is None:
        return "NONE", "no budget hold exists yet, so there is nothing to preserve or release"
    remaining = ctx.hold_seconds_remaining
    if remaining >= human_sla_seconds:
        return "HOLD", (
            f"hold has {remaining:.0f}s, human SLA is {human_sla_seconds:.0f}s - it will "
            f"outlive the wait, so keep it and preserve the completed spend stage"
        )
    can_extend = ctx.hold_extensions_used == 0
    if can_extend and remaining + 300 >= human_sla_seconds:
        return "EXTEND", (
            f"hold has {remaining:.0f}s against a {human_sla_seconds:.0f}s SLA, but one "
            f"+300s extension is still available and closes the gap - extend rather than "
            f"lose 25u of completed spend work"
        )
    return "UNWIND", (
        f"hold has {remaining:.0f}s and cannot be stretched to the {human_sla_seconds:.0f}s "
        f"SLA even with an extension. It will lapse anyway, so release it now: the budget "
        f"returns to the pool immediately and we attach a replay plan to the escalation"
    )

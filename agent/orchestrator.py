"""The agent loop: plan, execute, classify, recover.

Structure worth noting: recovery plans do not execute stages themselves. A plan
applies *preparatory* mutations - change a parameter, amend a hold, invalidate
one upstream artefact, wait - and then control returns to the main loop, which
runs whichever stages currently lack a valid artefact. That means there is
exactly one code path that calls a stage, so "did the recovery actually skip
the expensive work?" is answered by the artefact table rather than by trusting
two parallel implementations to agree.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from . import cost as C
from . import policy
from .journal import Journal
from .models import (
    ActionType,
    AllocationRequest,
    Artifact,
    Outcome,
    RecoveryPlan,
    RunResult,
    STAGE_DEPENDENCIES,
    Stage,
    StageFailure,
    WorkflowState,
)
from .policy import RecoveryContext
from .taxonomy import Classification, FailureClass, classify
from .tools import ToolFailure, Tools

MAX_RECOVERY_ROUNDS = 8


def plan_order() -> list[Stage]:
    """Topological sort over declared dependencies.

    Trivial for three stages, but it means the execution order is derived from
    the dependency graph rather than typed out, so the loop does not need
    editing when a stage is added.
    """
    order: list[Stage] = []
    remaining = dict(STAGE_DEPENDENCIES)
    while remaining:
        ready = sorted(
            (s for s, deps in remaining.items() if deps <= set(order)), key=lambda s: s.value
        )
        if not ready:
            raise RuntimeError("cycle in stage dependency graph")
        for s in ready:
            order.append(s)
            del remaining[s]
    return order


class Orchestrator:
    def __init__(
        self,
        tools: Tools,
        journal: Journal,
        *,
        strategy: str = "policy",
        planner: Any | None = None,
        auto_approve: bool = False,
    ) -> None:
        self.tools = tools
        self.j = journal
        self.strategy = strategy  # "policy" | "naive"
        self.planner = planner    # optional LLM planner; see llm_planner.py
        self.auto_approve = auto_approve
        self.decisions: list[dict[str, Any]] = []

    # ------------------------------------------------------------- stages --
    async def _run_stage(self, stage: Stage, st: WorkflowState) -> None:
        """Execute one stage. A failed attempt is charged too.

        The service did the work and then refused; the compute is spent either
        way. Charging only successes would make retries look free and quietly
        bias every decision toward "just try again"."""
        st.attempts[stage] = st.attempts.get(stage, 0) + 1
        try:
            await self._execute(stage, st)
        except ToolFailure:
            st.units_spent += C.STAGE_COST[stage]
            raise

    async def _execute(self, stage: Stage, st: WorkflowState) -> None:
        req = st.request

        if stage is Stage.PUBLISH:
            body = await self.tools.publish(
                allocation_id=req.allocation_id,
                content_ref=st.param("content_ref"),
                tier=st.param("tier"),
                compliance_ref=req.compliance_ref,
            )
            art = Artifact(stage, body["receipt_id"], {"tier": body["tier"], "expires_at": body["expires_at"]})

        elif stage is Stage.SPEND:
            body = await self.tools.reserve(
                allocation_id=req.allocation_id,
                amount=int(st.param("amount")),
                budget_code=req.budget_code,
                ttl_seconds=float(st.param("reservation_ttl_seconds")),
            )
            art = Artifact(stage, body["reservation_id"], {"amount": body["amount"], "expires_at": body["expires_at"]})

        else:
            pub = st.valid_artifact(Stage.PUBLISH)
            spd = st.valid_artifact(Stage.SPEND)
            assert pub and spd, "bind reached without valid upstream artefacts"
            body = await self.tools.bind(
                allocation_id=req.allocation_id,
                resource_id=req.resource_id,
                window=st.param("window"),
                publish_receipt_id=pub.ref,
                reservation_id=spd.ref,
            )
            art = Artifact(stage, body["binding_id"], {"window": body["window"], "price": body["price"]})

        units = int(body.get("cost_units", C.STAGE_COST[stage]))
        st.record(art, units)
        self.j.stage_done(stage, art.ref, units, st.units_spent)

    # ------------------------------------------------------------ evidence --
    async def _gather_context(self, st: WorkflowState, failure: StageFailure) -> RecoveryContext:
        ctx = RecoveryContext()
        try:
            b = await self.tools.budget(st.request.budget_code)
            ctx.pool_available = b["available"]
        except ToolFailure:
            pass

        spd = st.valid_artifact(Stage.SPEND)
        if spd:
            try:
                r = await self.tools.get_reservation(spd.ref)
                ctx.reservation = r
                ctx.hold_seconds_remaining = r.get("seconds_remaining", 0.0)
                ctx.hold_extensions_used = r.get("extensions_used", 0)
            except ToolFailure:
                spd.invalidate("spend service no longer recognises the hold")

        alts = failure.detail.get("alternatives")
        if alts is None and failure.stage is Stage.BIND:
            try:
                av = await self.tools.availability(st.request.resource_id)
                alts = [
                    {
                        "window": w["window"],
                        "price": w["price"],
                        "required_tier": w["required_tier"],
                        "label": w["label"],
                    }
                    for w in av["windows"]
                    if not w["committed_to"] and w["window"] != st.param("window")
                ]
            except ToolFailure:
                alts = []
        ctx.alternatives = alts or []
        return ctx

    # ------------------------------------------------------------ recovery --
    async def _apply_plan(
        self, plan: RecoveryPlan, st: WorkflowState, ctx: RecoveryContext,
        failure: StageFailure, cls: Classification, approver: str | None = None,
    ) -> RunResult | None:
        """Apply a plan's preparatory effects. Returns a RunResult only if the
        plan terminates the run (escalate / abort)."""
        for step in plan.steps:
            if step.wait_seconds:
                self.j.note(f"waiting {step.wait_seconds}s for the contended lock to clear")
                await asyncio.sleep(step.wait_seconds)

            if step.action is ActionType.RETRY_SAME:
                continue  # the main loop re-runs any stage lacking an artefact

            if step.action is ActionType.RETRY_MODIFIED:
                st.working.update(step.params)
                self.j.note(f"parameter change: {step.params}", **step.params)

            elif step.action is ActionType.AMEND_SPEND:
                spd = st.valid_artifact(Stage.SPEND)
                assert spd
                body = await self.tools.amend_reservation(
                    spd.ref, int(step.params["new_amount"]), approver=approver
                )
                spd.meta["amount"] = body["amount"]
                st.units_spent += int(body.get("cost_units", C.STAGE_COST[Stage.SPEND]))
                st.working["amount"] = body["amount"]
                self.j.note(
                    f"amended hold in place to {body['amount']} "
                    f"(kept the same reservation, budget never re-entered the pool)"
                )

            elif step.action is ActionType.EXTEND_HOLD:
                spd = st.valid_artifact(Stage.SPEND)
                if spd:
                    body = await self.tools.extend_reservation(spd.ref)
                    self.j.note(f"extended hold by {body['granted_seconds']:.0f}s")

            elif step.action is ActionType.REPLAY_FROM:
                assert step.stage
                st.working.update(step.params)
                art = st.artifacts.get(step.stage)
                if art:
                    art.invalidate(f"replay triggered by {failure.code}")
                kept = [str(s) for s in st.completed_stages()]
                self.j.note(
                    f"replaying {step.stage} only; keeping {', '.join(kept) or 'nothing'}"
                )

            elif step.action is ActionType.RELEASE_HOLD:
                spd = st.valid_artifact(Stage.SPEND)
                if spd:
                    await self.tools.release_reservation(spd.ref)
                    spd.invalidate("released")

            elif step.action is ActionType.RUN_STAGE:
                assert step.stage
                art = st.artifacts.get(step.stage)
                if art:
                    art.invalidate("discarded by full replay")

            elif step.action is ActionType.ESCALATE:
                return await self._escalate(st, ctx, failure, cls, plan)

            elif step.action is ActionType.ABORT:
                self.j.result("aborted", st.units_spent)
                return RunResult(Outcome.ABORTED, st.units_spent, st, decisions=self.decisions)
        return None

    async def _escalate(
        self, st: WorkflowState, ctx: RecoveryContext, failure: StageFailure,
        cls: Classification, plan: RecoveryPlan,
    ) -> RunResult | None:
        strategy, why = policy.hold_strategy(ctx, st.request.human_sla_seconds)
        if strategy == "EXTEND":
            spd = st.valid_artifact(Stage.SPEND)
            if spd:
                try:
                    body = await self.tools.extend_reservation(spd.ref)
                    ctx.hold_seconds_remaining = body["seconds_remaining"]
                except ToolFailure:
                    strategy, why = "UNWIND", why + " (extension refused, unwinding instead)"
        if strategy == "UNWIND":
            spd = st.valid_artifact(Stage.SPEND)
            if spd:
                await self.tools.release_reservation(spd.ref)
                spd.invalidate("released ahead of expiry while awaiting human decision")

        plans = getattr(self, "_last_plans", [])
        role = self._assignee(cls, plans)
        preserved = {str(s): st.artifacts[s].ref for s in st.completed_stages()}
        liftable = self._human_liftable(plans)
        finish_cost = liftable.estimated_units if liftable else C.STAGE_COST[Stage.BIND]
        payload = {
            "allocation_id": st.request.allocation_id,
            "assignee_role": role,
            "summary": f"{failure.stage} refused with {failure.code}: {failure.message}",
            "failure": {"code": failure.code, "detail": failure.detail},
            "classification": cls.failure_class.value,
            "preserved_artifacts": preserved,
            "hold_strategy": strategy,
            "hold_rationale": why,
            "units_spent_so_far": st.units_spent,
            "units_to_finish_if_approved": finish_cost,
            "units_if_restarted_from_scratch": C.FULL_SEQUENCE_COST,
            "options": [
                f"{p.name}: {p.rationale} [blocked by {'; '.join(p.vetoes)}]"
                for p in plans
                if p.vetoes and p.name not in {"abort", "full_replay"}
            ],
        }
        self.j.escalation(payload)

        if self.auto_approve:
            approved = self._human_liftable(getattr(self, "_last_plans", []))
            if approved:
                self.j.note(
                    f"[--auto-approve] {role} approved '{approved.name}'; resuming from "
                    f"preserved artefacts instead of restarting "
                    f"({approved.estimated_units}u to finish vs {C.FULL_SEQUENCE_COST}u for a restart)"
                )
                self.decisions.append({"plan": approved.name, "source": "human_approved"})
                await self._apply_plan(
                    approved, st, ctx, failure, cls, approver=f"{role}@example.com"
                )
                # Returning None hands control back to the main loop, which runs
                # whatever still lacks an artefact - here, bind alone.
                return None

        self.j.result("escalated", st.units_spent)
        return RunResult(Outcome.ESCALATED, st.units_spent, st, escalation=payload, decisions=self.decisions)

    @staticmethod
    def _assignee(cls: Classification, plans: list[RecoveryPlan]) -> str:
        """Route to whoever can actually unblock this.

        The failure code alone is the wrong signal. SLOT_CONFLICT sounds like an
        engineering problem, but if the only remaining plan is blocked on a
        spend-authority limit then the person who can help is the budget owner.
        So the routing reads the vetoes on the blocked plans, and falls back to
        the classification's tags only when nothing was blocked.
        """
        blockers = [v for p in plans for v in p.vetoes if p.name != "abort"]
        if any(v.startswith("G6") for v in blockers):
            return "budget_owner"
        if any("compliance_ref" in v for v in blockers):
            return "compliance_officer"
        if "needs_budget_owner" in cls.tags:
            return "budget_owner"
        if "needs_compliance" in cls.tags:
            return "compliance_officer"
        return "oncall_engineer"

    @staticmethod
    def _human_liftable(plans: list[RecoveryPlan]) -> RecoveryPlan | None:
        """Plans whose only blocker is the agent's own spend authority.

        A human can lift G6. Nobody can lift G1 (the work genuinely is
        redundant) or G7 (the upstream service will genuinely refuse), so those
        plans stay dead even with approval.
        """
        eligible = [
            p for p in plans
            if p.vetoes and all(v.startswith("G6") for v in p.vetoes) and p.name != "abort"
        ]
        if not eligible:
            return None
        return min(eligible, key=lambda p: p.estimated_units)

    # ---------------------------------------------------------- naive mode --
    async def _naive_recover(self, st: WorkflowState, failure: StageFailure) -> RunResult | None:
        """The strategy the brief warns against, implemented honestly so the
        demo can show it losing rather than just assert that it would."""
        rounds = st.working.get("_naive_rounds", 0)
        if rounds >= 2:
            self.j.result("exhausted", st.units_spent)
            return RunResult(Outcome.EXHAUSTED, st.units_spent, st,
                             error=f"naive retry exhausted after {failure.code}",
                             decisions=self.decisions)
        st.working["_naive_rounds"] = rounds + 1
        self.j.note("[naive] failure detected - discarding all artefacts and re-running the "
                    "whole sequence")
        spd = st.valid_artifact(Stage.SPEND)
        if spd:
            await self.tools.release_reservation(spd.ref)
        for art in st.artifacts.values():
            art.invalidate("naive full replay")
        return None

    # ------------------------------------------------------------- run loop --
    async def run(self, request: AllocationRequest) -> RunResult:
        st = WorkflowState(request=request)
        order = plan_order()
        self.j.plan(
            [str(s) for s in order],
            "topologically sorted from declared stage dependencies; bind requires publish+spend",
        )
        t0 = time.time()

        for _round in range(MAX_RECOVERY_ROUNDS):
            if time.time() - t0 > C.MAX_WALL_SECONDS:
                self.j.result("exhausted", st.units_spent)
                return RunResult(Outcome.EXHAUSTED, st.units_spent, st,
                                 error="wall-clock budget exceeded", decisions=self.decisions)

            failure: StageFailure | None = None
            for stage in order:
                if st.valid_artifact(stage):
                    continue
                try:
                    await self._run_stage(stage, st)
                except ToolFailure as tf:
                    failure = tf.failure
                    break

            if failure is None:
                self.j.result("completed", st.units_spent)
                return RunResult(Outcome.COMPLETED, st.units_spent, st, decisions=self.decisions)

            # ---- something refused. Work out what that actually means. ----
            if self.strategy == "naive":
                res = await self._naive_recover(st, failure)
                if res:
                    return res
                continue

            cls = classify(failure)
            self.j.classified(failure, cls)
            for s in cls.invalidates:
                art = st.artifacts.get(s)
                if art and art.valid:
                    art.invalidate(f"{failure.code} proves this artefact is void")
                    self.j.note(f"{s} artefact marked void by {failure.code}")

            ctx = await self._gather_context(st, failure)
            self.j.note(
                f"evidence: pool_available={ctx.pool_available}, "
                f"hold_remaining={ctx.hold_seconds_remaining:.0f}s, "
                f"free_windows={[a['window'] for a in ctx.alternatives]}"
            )

            chosen, plans = policy.decide(st, failure, cls, ctx)
            self._last_plans = plans
            source = "policy"

            if self.planner is not None:
                proposed = await self.planner.choose(st, failure, cls, ctx, plans)
                if proposed is not None:
                    if proposed.feasible:
                        chosen, source = proposed, "llm"
                    else:
                        # The guardrail layer is not advisory. An LLM that picks
                        # a vetoed plan gets overruled and the veto is logged.
                        self.j.veto(
                            f"llm:{proposed.name}", "; ".join(proposed.vetoes)
                            + " -- falling back to the policy choice"
                        )

            self.j.candidates(plans)
            self.j.chose(chosen, source)
            self.decisions.append({
                "failure": failure.code,
                "class": cls.failure_class.value,
                "plan": chosen.name,
                "source": source,
                "units_before": st.units_spent,
                "rejected": {p.name: p.vetoes for p in plans if p.vetoes},
            })

            res = await self._apply_plan(chosen, st, ctx, failure, cls)
            if res:
                return res

        self.j.result("exhausted", st.units_spent)
        return RunResult(Outcome.EXHAUSTED, st.units_spent, st,
                         error="recovery round limit reached", decisions=self.decisions)

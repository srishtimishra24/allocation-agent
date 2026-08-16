"""Optional Claude-backed planner.

Why it is optional, and why it sits *inside* the guardrails rather than above
them:

The recovery decision here is a small, well-specified optimisation over an
enumerable set of options. A deterministic scorer does that better than a
language model: it is reproducible, testable, and free. What a language model
adds is judgement on the parts that are not enumerable - reading an unfamiliar
failure code, noticing that two options are near-tied and one is safer for
reasons the cost model does not capture, writing an escalation a human can act
on.

So the split is: the policy engine enumerates and vetoes, the model chooses
among plans that already passed the vetoes, and its choice is re-checked before
it runs. If the model picks a vetoed plan, it is overruled and the override is
logged. An LLM in the loop should not be able to do something the same system
would refuse a human operator.

Set ANTHROPIC_API_KEY and pass --planner llm to enable. With no key the agent
runs fully offline on the deterministic policy, which is what CI does.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .models import RecoveryPlan, StageFailure, WorkflowState
from .policy import RecoveryContext
from .taxonomy import Classification

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")

SYSTEM = """You are the recovery planner for a three-stage resource allocation \
workflow (publish -> spend -> bind). Each stage is a separate service enforcing \
its own rules.

An earlier stage has succeeded and a later one has failed. Your job is to pick \
ONE recovery plan from a list that has already been filtered for safety.

Optimise for, in order:
1. Correctness - the plan must actually address the stated failure cause.
2. Not destroying completed work - publish costs 40 compute units, spend 25, \
bind 10. Redoing a stage whose output is still valid is waste.
3. Irreversibility - a released budget hold re-enters a shared pool other \
processes draw from. It may not be reacquirable. Prefer amending a hold in \
place over releasing and re-reserving.
4. Compute cost.

Escalating to a human is correct when no autonomous plan can succeed. It is not \
correct merely because the situation is awkward.

Reply with JSON only: {"plan": "<exact plan name>", "reasoning": "<two sentences>"}"""


class LLMPlanner:
    def __init__(self, journal=None, model: str = MODEL) -> None:
        self.j = journal
        self.model = model
        self.available = False
        self._client = None
        if os.getenv("ANTHROPIC_API_KEY"):
            try:
                import anthropic

                self._client = anthropic.AsyncAnthropic()
                self.available = True
            except Exception as exc:  # pragma: no cover - depends on env
                if journal:
                    journal.note(f"LLM planner unavailable ({exc}); using deterministic policy")

    def _prompt(
        self, st: WorkflowState, failure: StageFailure, cls: Classification,
        ctx: RecoveryContext, plans: list[RecoveryPlan],
    ) -> str:
        return json.dumps(
            {
                "allocation": {
                    "id": st.request.allocation_id,
                    "target_window": st.param("window"),
                    "amount": st.param("amount"),
                    "compliance_ref_available": bool(st.request.compliance_ref),
                },
                "completed_stages": {
                    str(s): {"ref": st.artifacts[s].ref, "meta": st.artifacts[s].meta}
                    for s in st.completed_stages()
                },
                "compute_units_spent": st.units_spent,
                "failure": {
                    "stage": str(failure.stage),
                    "code": failure.code,
                    "message": failure.message,
                    "detail": failure.detail,
                },
                "our_classification": {
                    "class": cls.failure_class.value,
                    "note": cls.note,
                },
                "live_state": {
                    "budget_pool_available": ctx.pool_available,
                    "hold_seconds_remaining": round(ctx.hold_seconds_remaining, 1),
                    "free_windows": ctx.alternatives,
                },
                "candidate_plans": [
                    {
                        "name": p.name,
                        "steps": p.describe(),
                        "estimated_units": p.estimated_units,
                        "policy_p_success": p.p_success,
                        "blocked": p.vetoes or None,
                    }
                    for p in plans
                ],
            },
            indent=2,
            default=str,
        )

    async def choose(
        self, st: WorkflowState, failure: StageFailure, cls: Classification,
        ctx: RecoveryContext, plans: list[RecoveryPlan],
    ) -> RecoveryPlan | None:
        if not self.available:
            return None
        try:
            resp = await self._client.messages.create(
                model=self.model,
                max_tokens=400,
                system=SYSTEM,
                messages=[{"role": "user", "content": self._prompt(st, failure, cls, ctx, plans)}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            text = text[text.find("{") : text.rfind("}") + 1]
            data: dict[str, Any] = json.loads(text)
        except Exception as exc:  # pragma: no cover - network dependent
            if self.j:
                self.j.note(f"LLM planner errored ({exc}); deferring to deterministic policy")
            return None

        name = data.get("plan")
        match = next((p for p in plans if p.name == name), None)
        if match is None:
            if self.j:
                self.j.note(f"LLM proposed unknown plan '{name}'; deferring to policy")
            return None
        # Attach the model's reasoning so the run log shows what it actually said.
        match.rationale = f"[llm] {data.get('reasoning', '').strip()} || {match.rationale}"
        return match

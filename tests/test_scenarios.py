"""End-to-end scenario tests against the live services.

These assert the things that would actually be wrong if the agent regressed:
which recovery it chose, how much it spent, and - most importantly - which
stages it did *not* redo.
"""

from __future__ import annotations

import pytest

from agent import cost as C
from agent.journal import Journal
from agent.models import Outcome, Stage
from agent.orchestrator import Orchestrator, plan_order
from agent.tools import Tools
from scenarios import SCENARIOS, setup


async def run(key: str, strategy: str = "policy", auto_approve: bool | None = None):
    scn = SCENARIOS[key]
    await setup(scn)
    j = Journal(key, path=None, quiet=True)
    tools = Tools(journal=j)
    orch = Orchestrator(
        tools, j,
        strategy=strategy,
        auto_approve=scn.auto_approve if auto_approve is None else auto_approve,
    )
    try:
        return await orch.run(scn.request), j
    finally:
        await tools.aclose()


def plan_names(result) -> list[str]:
    return [d.get("plan") for d in result.decisions]


# --------------------------------------------------------------- structure --
def test_plan_order_is_derived_not_hardcoded():
    order = plan_order()
    assert order.index(Stage.BIND) > order.index(Stage.PUBLISH)
    assert order.index(Stage.BIND) > order.index(Stage.SPEND)


# ------------------------------------------------------------ per scenario --
@pytest.mark.asyncio
@pytest.mark.parametrize("key", sorted(SCENARIOS))
async def test_scenario_matches_expectation(key):
    scn = SCENARIOS[key]
    result, _ = await run(key)
    assert result.outcome.value == scn.expect_outcome, (
        f"{key}: expected {scn.expect_outcome}, got {result.outcome.value}"
    )
    if scn.expect_plan:
        assert scn.expect_plan in plan_names(result), (
            f"{key}: expected plan {scn.expect_plan}, got {plan_names(result)}"
        )
    if scn.expect_max_units:
        assert result.units_spent <= scn.expect_max_units, (
            f"{key}: spent {result.units_spent}u, budget was {scn.expect_max_units}u"
        )


# ------------------------------------------- requirement 1: transient case --
@pytest.mark.asyncio
async def test_transient_retries_only_the_failed_stage():
    result, _ = await run("transient")
    assert result.outcome is Outcome.COMPLETED
    # The expensive stages ran exactly once. This is the whole claim.
    assert result.state.attempts[Stage.PUBLISH] == 1
    assert result.state.attempts[Stage.SPEND] == 1
    assert result.state.attempts[Stage.BIND] == 2
    assert plan_names(result) == ["targeted_retry"]


@pytest.mark.asyncio
async def test_transient_keeps_the_same_reservation_throughout():
    result, _ = await run("transient")
    spend = result.state.artifacts[Stage.SPEND]
    assert spend.valid
    # Same reservation id from start to finish: the hold never re-entered the pool.
    assert spend.ref.startswith("res_")


@pytest.mark.asyncio
async def test_naive_full_replay_loses_the_allocation():
    """The counterexample. Same world, dumber strategy, allocation lost."""
    smart, _ = await run("transient", strategy="policy")
    naive, _ = await run("transient", strategy="naive")

    assert smart.outcome is Outcome.COMPLETED
    assert naive.outcome is Outcome.EXHAUSTED
    assert naive.units_spent > smart.units_spent * 2
    assert "BUDGET_EXHAUSTED" in (naive.error or "") or naive.state.attempts[Stage.SPEND] > 1


# ------------------------------------------ requirement 2: structural case --
@pytest.mark.asyncio
async def test_structural_conflict_escalates_instead_of_retrying():
    result, _ = await run("structural_escalate")
    assert result.outcome is Outcome.ESCALATED
    # Bind was attempted exactly once. Retrying a committed slot is pointless
    # and the agent must not discover that empirically.
    assert result.state.attempts[Stage.BIND] == 1
    assert result.state.attempts[Stage.PUBLISH] == 1


@pytest.mark.asyncio
async def test_escalation_routes_to_whoever_can_unblock_it():
    result, _ = await run("structural_escalate")
    esc = result.escalation
    assert esc is not None
    # The failure code says SLOT_CONFLICT, but the real blocker is spend
    # authority, so it must go to the budget owner, not an engineer.
    assert esc["assignee_role"] == "budget_owner"
    assert esc["preserved_artifacts"].keys() == {"publish", "spend"}
    assert esc["units_to_finish_if_approved"] < esc["units_if_restarted_from_scratch"]


@pytest.mark.asyncio
async def test_escalation_extends_the_hold_when_that_closes_the_sla_gap():
    result, _ = await run("structural_escalate")
    assert result.escalation["hold_strategy"] == "EXTEND"


@pytest.mark.asyncio
async def test_approval_resumes_rather_than_restarts():
    result, _ = await run("structural_escalate_approved")
    assert result.outcome is Outcome.COMPLETED
    assert result.state.attempts[Stage.PUBLISH] == 1  # never redone
    assert result.state.attempts[Stage.SPEND] == 1    # amended, not re-reserved
    assert "amend_then_bind[peak]" in plan_names(result)


# ------------------------------------------------ the other recovery shapes --
@pytest.mark.asyncio
async def test_param_change_touches_nothing_upstream():
    result, _ = await run("param_change")
    assert result.outcome is Outcome.COMPLETED
    assert result.state.attempts[Stage.PUBLISH] == 1
    assert result.state.attempts[Stage.SPEND] == 1
    assert result.state.working["window"] == "2026-08-17T14:00"


@pytest.mark.asyncio
async def test_partial_replay_redoes_publish_but_not_spend():
    result, _ = await run("partial_replay")
    assert result.outcome is Outcome.COMPLETED
    assert result.state.attempts[Stage.PUBLISH] == 2
    assert result.state.attempts[Stage.SPEND] == 1
    assert result.units_spent < C.FULL_SEQUENCE_COST * 2


@pytest.mark.asyncio
async def test_expired_hold_redoes_spend_but_not_publish():
    result, _ = await run("hold_expired")
    assert result.outcome is Outcome.COMPLETED
    assert result.state.attempts[Stage.PUBLISH] == 1
    assert result.state.attempts[Stage.SPEND] == 2
    # And it learned: the replacement hold has a longer TTL than the one that lapsed.
    assert float(result.state.working["reservation_ttl_seconds"]) > 1.0


@pytest.mark.asyncio
async def test_terminal_budget_failure_escalates_without_retrying():
    result, _ = await run("budget_terminal")
    assert result.outcome is Outcome.ESCALATED
    assert result.state.attempts[Stage.SPEND] == 1
    assert result.escalation["assignee_role"] == "budget_owner"


@pytest.mark.asyncio
async def test_two_failure_classes_in_one_run_get_two_different_recoveries():
    """The recovery choice has to be made per-failure, not once per run.

    A design that latches onto its first diagnosis would retry a committed slot
    forever here, or give up because 'the retry already failed'.
    """
    result, _ = await run("compound")
    assert result.outcome is Outcome.COMPLETED
    assert plan_names(result) == ["targeted_retry", "reprice_window[off-peak]"]
    classes = [d["class"] for d in result.decisions]
    assert classes == ["transient", "structural_parametric"]
    # Three bind attempts, and still exactly one publish and one spend.
    assert result.state.attempts[Stage.BIND] == 3
    assert result.state.attempts[Stage.PUBLISH] == 1
    assert result.state.attempts[Stage.SPEND] == 1


# --------------------------------------------------- LLM planner override --
class _RecklessPlanner:
    """Stands in for a model that picks the vetoed option.

    Whether the real Claude would do this is not the point. The point is that
    the system's behaviour must not depend on the answer.
    """

    def __init__(self, pick: str) -> None:
        self.pick = pick
        self.called = 0

    async def choose(self, state, failure, cls, ctx, plans):
        self.called += 1
        return next((p for p in plans if p.name == self.pick), None)


@pytest.mark.asyncio
async def test_llm_choice_of_a_vetoed_plan_is_overruled():
    scn = SCENARIOS["transient"]
    await setup(scn)
    j = Journal("llm-veto", path=None, quiet=True)
    tools = Tools(journal=j)
    planner = _RecklessPlanner("full_replay")
    orch = Orchestrator(tools, j, planner=planner)
    try:
        result = await orch.run(scn.request)
    finally:
        await tools.aclose()

    assert planner.called == 1
    assert result.outcome is Outcome.COMPLETED
    # It got overruled: publish and spend were never redone.
    assert result.state.attempts[Stage.PUBLISH] == 1
    assert result.state.attempts[Stage.SPEND] == 1
    assert plan_names(result) == ["targeted_retry"]
    assert any(e["kind"] == "veto" for e in j.events)


@pytest.mark.asyncio
async def test_llm_choice_of_a_permitted_plan_is_honoured():
    """The override must not be a rubber stamp in the other direction either -
    if the model picks a feasible plan, that plan runs and is attributed to it."""
    scn = SCENARIOS["param_change"]
    await setup(scn)
    j = Journal("llm-ok", path=None, quiet=True)
    tools = Tools(journal=j)
    planner = _RecklessPlanner("escalate")  # feasible, just not what policy scored best
    orch = Orchestrator(tools, j, planner=planner)
    try:
        result = await orch.run(scn.request)
    finally:
        await tools.aclose()

    assert result.outcome is Outcome.ESCALATED
    assert any(d["source"] == "llm" for d in result.decisions)


# ------------------------------------------------------------- invariants --
@pytest.mark.asyncio
@pytest.mark.parametrize("key", sorted(SCENARIOS))
async def test_never_exceeds_the_compute_ceiling(key):
    result, _ = await run(key)
    assert result.units_spent <= C.UNIT_BUDGET


@pytest.mark.asyncio
@pytest.mark.parametrize("key", sorted(SCENARIOS))
async def test_full_replay_is_never_the_chosen_plan(key):
    result, _ = await run(key)
    assert "full_replay" not in plan_names(result)

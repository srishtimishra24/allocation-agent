"""Unit tests for classification, guardrails and scoring. No network."""

from __future__ import annotations

import pytest

from agent import cost as C
from agent import policy
from agent.models import (
    ActionType,
    AllocationRequest,
    Artifact,
    RecoveryPlan,
    Stage,
    StageFailure,
    Step,
    WorkflowState,
)
from agent.policy import RecoveryContext
from agent.taxonomy import FailureClass, classify


def a_state(**kw) -> WorkflowState:
    req = AllocationRequest(
        allocation_id="a1", content_ref="s3://x", resource_id="r1",
        window="2026-08-17T09:00", amount=60_000, **kw,
    )
    st = WorkflowState(request=req)
    st.artifacts[Stage.PUBLISH] = Artifact(Stage.PUBLISH, "pub_1", {"tier": "standard"})
    st.artifacts[Stage.SPEND] = Artifact(Stage.SPEND, "res_1", {"amount": 60_000})
    st.units_spent = 65
    st.attempts = {Stage.PUBLISH: 1, Stage.SPEND: 1}
    return st


def a_ctx(**kw) -> RecoveryContext:
    base = dict(
        pool_available=40_000,
        reservation={"amount": 60_000, "original_amount": 60_000, "state": "HELD"},
        hold_seconds_remaining=600.0,
        hold_extensions_used=0,
        alternatives=[],
    )
    base.update(kw)
    return RecoveryContext(**base)  # type: ignore[arg-type]


def fail(code: str, stage: Stage = Stage.BIND, status: int = 409, **detail) -> StageFailure:
    return StageFailure(stage, code, "boom", status, {"code": code, **detail})


# ---------------------------------------------------------- classification --
def test_transient_and_structural_are_distinguished():
    assert classify(fail("LOCK_CONTENDED")).failure_class is FailureClass.TRANSIENT
    assert classify(fail("SLOT_CONFLICT")).failure_class is FailureClass.STRUCTURAL_PARAMETRIC
    assert classify(fail("PUBLISH_TIER_MISMATCH")).failure_class is FailureClass.STRUCTURAL_UPSTREAM
    assert classify(fail("BUDGET_EXHAUSTED")).failure_class is FailureClass.STRUCTURAL_TERMINAL


def test_unknown_codes_default_to_structural_not_transient():
    """A wrong 'transient' guess is an infinite loop; a wrong 'structural'
    guess is merely a slow escalation. Fail toward the recoverable mistake."""
    c = classify(fail("SOMETHING_WE_HAVE_NEVER_SEEN", status=409))
    assert c.failure_class is FailureClass.STRUCTURAL_TERMINAL


def test_service_retry_hint_is_advisory_not_binding():
    # The service claims retryable, but we know this code and know better.
    c = classify(fail("SLOT_CONFLICT", retryable_hint=True))
    assert c.failure_class is FailureClass.STRUCTURAL_PARAMETRIC
    # For an unknown 5xx we do defer to the hint, cautiously.
    c2 = classify(fail("WEIRD_5XX", status=503, retryable_hint=True))
    assert c2.failure_class is FailureClass.TRANSIENT
    assert c2.p_same_retry < 0.5


def test_upstream_failures_name_the_single_stage_to_replay():
    assert classify(fail("PUBLISH_TIER_MISMATCH")).replay_from is Stage.PUBLISH
    assert classify(fail("RESERVATION_EXPIRED")).replay_from is Stage.SPEND


# ---------------------------------------------------------------- vetoes ----
def test_g1_blocks_full_replay_when_artefacts_are_still_valid():
    st, ctx = a_state(), a_ctx()
    f = fail("LOCK_CONTENDED", retry_after_seconds=0.1)
    chosen, plans = policy.decide(st, f, classify(f), ctx)
    full = next(p for p in plans if p.name == "full_replay")
    assert any(v.startswith("G1") for v in full.vetoes)
    assert chosen.name == "targeted_retry"


def test_g1_exempts_a_stage_the_plan_redoes_on_purpose():
    """Republishing at a higher tier is the fix, not waste. G1 must not
    confuse the two, or it would veto the plan it exists to promote."""
    st, ctx = a_state(compliance_ref="COMP-1"), a_ctx(
        alternatives=[{"window": "w2", "price": 60_000, "required_tier": "certified",
                       "label": "regulated"}]
    )
    f = fail("SLOT_CONFLICT")
    _, plans = policy.decide(st, f, classify(f), ctx)
    upgrade = next(p for p in plans if p.name.startswith("upgrade_tier_then_bind"))
    assert not any(v.startswith("G1") for v in upgrade.vetoes)


def test_g2_blocks_releasing_a_hold_the_pool_cannot_refill():
    st = a_state()
    ctx = a_ctx(pool_available=1_000)
    f = fail("LOCK_CONTENDED", retry_after_seconds=0.1)
    _, plans = policy.decide(st, f, classify(f), ctx)
    full = next(p for p in plans if p.name == "full_replay")
    assert any(v.startswith("G2") for v in full.vetoes)


def test_g3_blocks_plans_that_would_break_the_compute_ceiling():
    st = a_state()
    st.units_spent = C.UNIT_BUDGET - 5
    f = fail("LOCK_CONTENDED", retry_after_seconds=0.1)
    _, plans = policy.decide(st, f, classify(f), ctx=a_ctx())
    retry = next(p for p in plans if p.name == "targeted_retry")
    assert any(v.startswith("G3") for v in retry.vetoes)


def test_g4_stops_insisting_a_failure_is_transient():
    st = a_state()
    st.attempts[Stage.BIND] = C.MAX_SAME_RETRIES
    f = fail("LOCK_CONTENDED", retry_after_seconds=0.1)
    chosen, plans = policy.decide(st, f, classify(f), a_ctx())
    retry = next(p for p in plans if p.name == "targeted_retry")
    assert any(v.startswith("G4") for v in retry.vetoes)
    assert chosen.name == "escalate"


def test_confidence_decays_so_escalation_wins_before_the_hard_cap():
    """The retry cap is a backstop. Scoring alone should give up first."""
    st = a_state()
    st.attempts[Stage.BIND] = 3
    f = fail("LOCK_CONTENDED", retry_after_seconds=0.1)
    plans = policy.generate_candidates(st, f, classify(f), a_ctx())
    policy.score(plans)
    retry = next(p for p in plans if p.name == "targeted_retry")
    esc = next(p for p in plans if p.name == "escalate")
    assert retry.score < esc.score


def test_g5_blocks_plans_that_outlive_the_budget_hold():
    st = a_state()
    ctx = a_ctx(hold_seconds_remaining=0.2)
    f = fail("LOCK_CONTENDED", retry_after_seconds=5.0)
    _, plans = policy.decide(st, f, classify(f), ctx)
    retry = next(p for p in plans if p.name == "targeted_retry")
    assert any(v.startswith("G5") for v in retry.vetoes)


def test_g6_blocks_spend_increases_beyond_the_agents_authority():
    st = a_state()
    ctx = a_ctx(alternatives=[{"window": "w2", "price": 72_000,
                               "required_tier": "standard", "label": "peak"}])
    f = fail("SLOT_CONFLICT")
    chosen, plans = policy.decide(st, f, classify(f), ctx)
    amend = next(p for p in plans if p.name.startswith("amend_then_bind"))
    assert any(v.startswith("G6") for v in amend.vetoes)
    assert chosen.name == "escalate"


def test_g6_allows_an_increase_inside_the_authority_limit():
    st = a_state()
    ctx = a_ctx(alternatives=[{"window": "w2", "price": 63_000,
                               "required_tier": "standard", "label": "peak"}])
    f = fail("SLOT_CONFLICT")
    chosen, _ = policy.decide(st, f, classify(f), ctx)
    assert chosen.name.startswith("amend_then_bind")


def test_g7_preflights_the_upstream_service_rule():
    st = a_state()  # no compliance_ref
    ctx = a_ctx(alternatives=[{"window": "w2", "price": 60_000,
                               "required_tier": "certified", "label": "regulated"}])
    f = fail("SLOT_CONFLICT")
    _, plans = policy.decide(st, f, classify(f), ctx)
    upgrade = next(p for p in plans if p.name.startswith("upgrade_tier_then_bind"))
    assert any(v.startswith("G7") for v in upgrade.vetoes)


def test_a_free_equivalent_slot_beats_escalating():
    st = a_state()
    ctx = a_ctx(alternatives=[{"window": "w2", "price": 60_000,
                               "required_tier": "standard", "label": "off-peak"}])
    f = fail("SLOT_CONFLICT")
    chosen, _ = policy.decide(st, f, classify(f), ctx)
    assert chosen.name == "reprice_window[off-peak]"
    assert chosen.estimated_units == C.STAGE_COST[Stage.BIND]


def test_everything_vetoed_falls_through_to_abort_not_a_crash():
    st = a_state()
    st.units_spent = C.UNIT_BUDGET + 500
    f = fail("LOCK_CONTENDED", retry_after_seconds=0.1)
    plans = policy.generate_candidates(st, f, classify(f), a_ctx())
    for p in plans:
        if p.name != "abort":
            p.vetoes.append("forced")
    policy.score(plans)
    assert policy.choose(plans).name == "abort"


# ------------------------------------------------------- hold strategy -----
@pytest.mark.parametrize(
    "remaining,used,sla,expected",
    [
        (2000.0, 0, 1800.0, "HOLD"),     # outlives the wait as-is
        (1600.0, 0, 1800.0, "EXTEND"),   # +300s closes the gap
        (100.0, 0, 1800.0, "UNWIND"),    # cannot be stretched far enough
        (1600.0, 1, 1800.0, "UNWIND"),   # extension already used
    ],
)
def test_hold_strategy_branches(remaining, used, sla, expected):
    ctx = a_ctx(hold_seconds_remaining=remaining, hold_extensions_used=used)
    strategy, why = policy.hold_strategy(ctx, sla)
    assert strategy == expected
    assert why  # every branch explains itself


def test_hold_strategy_with_no_reservation():
    strategy, _ = policy.hold_strategy(a_ctx(reservation=None), 1800.0)
    assert strategy == "NONE"


# ------------------------------------------------------------- scoring -----
def test_vetoed_plans_are_unreachable_at_any_score():
    p = RecoveryPlan("x", [Step(ActionType.ESCALATE)], "", 0, p_success=1.0)
    p.vetoes.append("G1 whatever")
    policy.score([p])
    assert p.score == float("-inf")
    assert not p.feasible

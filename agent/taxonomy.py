"""Failure classification.

The agent's whole advantage over a retry loop is that it reads the failure code
before deciding what to do. This module is that reading step.

Two things worth defending:

1. We do not trust the service's own `retryable_hint`. A service knows whether
   *it* can serve the request again; it does not know whether the caller's
   upstream artefacts survive a retry. We use the hint as a tiebreaker for
   codes we have never seen, and override it for codes we have.

2. Unknown codes default to STRUCTURAL, not TRANSIENT. Guessing "transient"
   turns an unknown failure into an infinite retry loop against a system that
   will never say yes. Guessing "structural" turns it into an escalation, which
   is merely slow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .models import Stage, StageFailure


class FailureClass(str, Enum):
    # Same call, later, plausibly works. Nothing upstream is damaged.
    TRANSIENT = "transient"
    # Same call never works, but a different *parameter* on the same stage might.
    STRUCTURAL_PARAMETRIC = "structural_parametric"
    # Same call never works because an upstream artefact is void. Redo that one
    # upstream stage - not all of them.
    STRUCTURAL_UPSTREAM = "structural_upstream"
    # Nothing the agent is authorised to do will fix it. Human, or abort.
    STRUCTURAL_TERMINAL = "structural_terminal"


@dataclass(frozen=True)
class Classification:
    failure_class: FailureClass
    # Prior artefacts this failure proves are now worthless.
    invalidates: tuple[Stage, ...] = ()
    # Probability that an identical retry of the failed stage succeeds.
    p_same_retry: float = 0.0
    # Which upstream stage must be replayed, if any.
    replay_from: Stage | None = None
    note: str = ""
    tags: frozenset[str] = field(default_factory=frozenset)


TAXONOMY: dict[str, Classification] = {
    # ---- transient -------------------------------------------------------
    "LOCK_CONTENDED": Classification(
        FailureClass.TRANSIENT,
        p_same_retry=0.85,
        note="another transaction holds a short-lived soft lock; it expires on its own",
    ),
    "BIND_SERVICE_DEGRADED": Classification(
        FailureClass.TRANSIENT, p_same_retry=0.7, note="service-side degradation"
    ),
    "SERVICE_UNAVAILABLE": Classification(
        FailureClass.TRANSIENT, p_same_retry=0.6, note="transport or 503"
    ),
    "TIMEOUT": Classification(FailureClass.TRANSIENT, p_same_retry=0.6, note="no response in time"),

    # ---- structural, fixable by changing a parameter on the same stage ----
    "SLOT_CONFLICT": Classification(
        FailureClass.STRUCTURAL_PARAMETRIC,
        p_same_retry=0.0,
        note="window is permanently committed elsewhere; only a different window can work",
        tags=frozenset({"needs_alternative_window"}),
    ),
    "RESERVATION_INSUFFICIENT": Classification(
        FailureClass.STRUCTURAL_PARAMETRIC,
        p_same_retry=0.0,
        note="hold is smaller than the slot price; amend the hold rather than re-reserve",
        tags=frozenset({"needs_amend"}),
    ),

    # ---- structural, fixable by replaying exactly one upstream stage ------
    "PUBLISH_TIER_MISMATCH": Classification(
        FailureClass.STRUCTURAL_UPSTREAM,
        invalidates=(Stage.PUBLISH,),
        replay_from=Stage.PUBLISH,
        p_same_retry=0.0,
        note="content tier too low for this resource class; republish at the higher tier, "
             "the budget hold is untouched",
    ),
    "PUBLISH_RECEIPT_INVALID": Classification(
        FailureClass.STRUCTURAL_UPSTREAM,
        invalidates=(Stage.PUBLISH,),
        replay_from=Stage.PUBLISH,
        p_same_retry=0.0,
        note="receipt superseded, revoked or expired",
    ),
    "RESERVATION_EXPIRED": Classification(
        FailureClass.STRUCTURAL_UPSTREAM,
        invalidates=(Stage.SPEND,),
        replay_from=Stage.SPEND,
        p_same_retry=0.0,
        note="hold lapsed; the money went back to the shared pool",
    ),
    "RESERVATION_INVALID": Classification(
        FailureClass.STRUCTURAL_UPSTREAM,
        invalidates=(Stage.SPEND,),
        replay_from=Stage.SPEND,
        p_same_retry=0.0,
    ),
    "RESERVATION_NOT_HELD": Classification(
        FailureClass.STRUCTURAL_UPSTREAM,
        invalidates=(Stage.SPEND,),
        replay_from=Stage.SPEND,
        p_same_retry=0.0,
    ),

    # ---- terminal --------------------------------------------------------
    "BUDGET_EXHAUSTED": Classification(
        FailureClass.STRUCTURAL_TERMINAL,
        p_same_retry=0.0,
        note="shared pool is drained; only a human can free or raise budget",
        tags=frozenset({"needs_budget_owner"}),
    ),
    "AMENDMENT_LIMIT_EXCEEDED": Classification(
        FailureClass.STRUCTURAL_TERMINAL,
        p_same_retry=0.0,
        note="increase exceeds the agent's autonomous spend authority",
        tags=frozenset({"needs_budget_owner"}),
    ),
    "CONTENT_NOT_CERTIFIABLE": Classification(
        FailureClass.STRUCTURAL_TERMINAL,
        p_same_retry=0.0,
        note="no compliance reference exists for this content",
        tags=frozenset({"needs_compliance"}),
    ),
    "CONTENT_EMPTY": Classification(FailureClass.STRUCTURAL_TERMINAL, p_same_retry=0.0),
    "WINDOW_UNKNOWN": Classification(FailureClass.STRUCTURAL_TERMINAL, p_same_retry=0.0),
    "EXTENSION_LIMIT_REACHED": Classification(FailureClass.STRUCTURAL_TERMINAL, p_same_retry=0.0),
    "COMMIT_FAILED": Classification(
        FailureClass.STRUCTURAL_UPSTREAM, invalidates=(Stage.SPEND,), replay_from=Stage.SPEND
    ),
}


UNKNOWN = Classification(
    FailureClass.STRUCTURAL_TERMINAL,
    p_same_retry=0.0,
    note="unrecognised failure code - treated as structural on purpose, so that an "
         "unknown-unknown becomes an escalation rather than an infinite retry loop",
    tags=frozenset({"unknown_code"}),
)


def classify(failure: StageFailure) -> Classification:
    c = TAXONOMY.get(failure.code)
    if c is not None:
        return c
    # Never seen this code. If the service insists it is retryable and the status
    # is a 5xx, allow a small number of cautious retries; otherwise escalate.
    if failure.detail.get("retryable_hint") and failure.http_status >= 500:
        return Classification(
            FailureClass.TRANSIENT,
            p_same_retry=0.4,
            note=f"unknown code {failure.code}, but 5xx + service says retryable",
            tags=frozenset({"unknown_code"}),
        )
    return UNKNOWN

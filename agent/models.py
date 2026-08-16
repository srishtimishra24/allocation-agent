"""Domain model for the allocation workflow agent."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Stage(str, Enum):
    PUBLISH = "publish"
    SPEND = "spend"
    BIND = "bind"

    def __str__(self) -> str:  # nicer logs
        return self.value


# Declared dependencies. The planner topologically sorts these rather than
# hardcoding publish->spend->bind, so adding a stage does not mean editing the
# execution loop.
STAGE_DEPENDENCIES: dict[Stage, set[Stage]] = {
    Stage.PUBLISH: set(),
    Stage.SPEND: set(),
    Stage.BIND: {Stage.PUBLISH, Stage.SPEND},
}


@dataclass
class AllocationRequest:
    allocation_id: str
    content_ref: str
    resource_id: str
    window: str
    amount: int
    tier: str = "standard"
    compliance_ref: str | None = None
    budget_code: str = "OPEX-GPU"
    reservation_ttl_seconds: float = 600.0
    # How long a human escalation is expected to take. Drives whether the agent
    # holds, extends, or unwinds the budget reservation while it waits.
    human_sla_seconds: float = 1800.0


@dataclass
class Artifact:
    """Something a stage produced that later stages depend on."""

    stage: Stage
    ref: str
    meta: dict[str, Any] = field(default_factory=dict)
    valid: bool = True
    invalidated_reason: str | None = None
    created_at: float = field(default_factory=time.time)

    def invalidate(self, reason: str) -> None:
        self.valid = False
        self.invalidated_reason = reason


@dataclass
class StageFailure:
    stage: Stage
    code: str
    message: str
    http_status: int
    detail: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.stage}:{self.code} ({self.http_status}) {self.message}"


class ActionType(str, Enum):
    RUN_STAGE = "run_stage"
    RETRY_SAME = "retry_same"          # identical retry, after a wait
    RETRY_MODIFIED = "retry_modified"  # same stage, different parameters
    AMEND_SPEND = "amend_spend"        # adjust the existing hold in place
    EXTEND_HOLD = "extend_hold"
    REPLAY_FROM = "replay_from"        # redo one upstream stage, keep the others
    RELEASE_HOLD = "release_hold"
    ESCALATE = "escalate"
    ABORT = "abort"


@dataclass
class Step:
    action: ActionType
    stage: Stage | None = None
    params: dict[str, Any] = field(default_factory=dict)
    wait_seconds: float = 0.0

    def describe(self) -> str:
        bits = [self.action.value]
        if self.stage:
            bits.append(str(self.stage))
        if self.params:
            bits.append(str({k: v for k, v in self.params.items() if k != "_"}))
        if self.wait_seconds:
            bits.append(f"after {self.wait_seconds}s")
        return " ".join(bits)


@dataclass
class RecoveryPlan:
    name: str
    steps: list[Step]
    rationale: str
    estimated_units: int
    p_success: float
    risk_penalty: float = 0.0
    human_cost: float = 0.0
    vetoes: list[str] = field(default_factory=list)
    score: float = 0.0
    # Stages this plan redoes *on purpose*, because redoing them is the fix -
    # not as collateral damage from restarting. Guardrail G1 exempts these.
    # Without this distinction G1 would block the very plans it exists to
    # promote: "republish at a higher tier" is targeted work, not waste.
    intentional_redo: set[Stage] = field(default_factory=set)

    @property
    def feasible(self) -> bool:
        return not self.vetoes

    def describe(self) -> str:
        return " -> ".join(s.describe() for s in self.steps) or "(no steps)"


@dataclass
class WorkflowState:
    request: AllocationRequest
    artifacts: dict[Stage, Artifact] = field(default_factory=dict)
    units_spent: int = 0
    attempts: dict[Stage, int] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    # Mutable working parameters. Recovery may change the target window or the
    # publish tier; the original request stays untouched for auditability.
    working: dict[str, Any] = field(default_factory=dict)

    def param(self, key: str) -> Any:
        if key in self.working:
            return self.working[key]
        return getattr(self.request, key)

    def valid_artifact(self, stage: Stage) -> Artifact | None:
        a = self.artifacts.get(stage)
        return a if a and a.valid else None

    def completed_stages(self) -> list[Stage]:
        return [s for s in Stage if self.valid_artifact(s)]

    def record(self, artifact: Artifact, cost_units: int) -> None:
        # `attempts` is bumped by the caller before the call is made, so that
        # failed attempts count too - which is the whole point of a retry cap.
        self.artifacts[artifact.stage] = artifact
        self.units_spent += cost_units


class Outcome(str, Enum):
    COMPLETED = "completed"
    ESCALATED = "escalated"
    ABORTED = "aborted"
    EXHAUSTED = "exhausted"


@dataclass
class RunResult:
    outcome: Outcome
    units_spent: int
    state: WorkflowState
    escalation: dict[str, Any] | None = None
    error: str | None = None
    decisions: list[dict[str, Any]] = field(default_factory=list)

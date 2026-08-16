"""The cost model. Everything the policy engine argues about is priced here.

These numbers are the reason the agent behaves the way it does, so they live in
one place where they can be argued with rather than being scattered through the
decision logic as magic numbers.
"""

from __future__ import annotations

from .models import Stage

# Compute units burned by executing a stage. Publish dominates because content
# moderation and embedding recompute are the expensive parts of this pipeline.
# Bind is nearly free. That ratio (40 : 25 : 10) is what makes redoing the whole
# sequence a bad default: a full replay costs 7.5x a targeted bind retry.
STAGE_COST: dict[Stage, int] = {
    Stage.PUBLISH: 40,
    Stage.SPEND: 25,
    Stage.BIND: 10,
}

FULL_SEQUENCE_COST = sum(STAGE_COST.values())  # 75

# Value of landing the allocation. Denominated in the same compute units so the
# scorer can trade one against the other.
VALUE_OF_COMPLETION = 1000.0

# What it costs the business to pull a human in: their attention, plus the
# latency of a workflow sitting parked. Deliberately high, so escalation is a
# last resort rather than a way to avoid thinking.
HUMAN_REVIEW_COST = 250.0

# What it costs if the allocation dies outright - budget lost to another
# process, slot gone, work discarded.
ALLOCATION_LOST_PENALTY = 800.0

# Hard ceiling on compute the agent may burn before it must stop and escalate.
# Without this, a cost-minimising agent will happily grind through twenty cheap
# retries and spend more than one full replay would have cost.
UNIT_BUDGET = 200

# Wall-clock ceiling per run.
MAX_WALL_SECONDS = 120.0

# Identical-retry attempt caps, per stage.
MAX_SAME_RETRIES = 3


def replay_cost(stages: list[Stage]) -> int:
    return sum(STAGE_COST[s] for s in stages)

# Allocation workflow agent

A three-stage approval workflow (**publish → spend → bind**) across three
independently failing HTTP services, plus an agent that recovers when a later
stage rejects work the earlier stages already committed to.

The interesting part is not the happy path. It is that the obvious recovery is
usually wrong, and the agent has to work out *which* kind of wrong it is
looking at before it does anything.

```
pip install -r requirements.txt

./run_demo.sh                          # everything: starts services, runs all 8 scenarios, tears down
./run_demo.sh transient --compare      # side by side: agent vs naive baseline, same world
pytest                                 # 61 tests, spawns its own service processes

# or drive it by hand
./start_services.sh                    # three uvicorn processes on :8101 :8102 :8103
python run_agent.py --list
python run_agent.py --scenario structural_escalate
./stop_services.sh
```

Python 3.10+. No API key required. The default planner is deterministic and
runs fully offline.

---

## Results

| Scenario | Failure | Recovery chosen | Units | Outcome |
|---|---|---|---|---|
| `transient` | `LOCK_CONTENDED` on bind | retry bind only | **85** | completed |
| `transient` *(naive baseline)* | same | full replay | **205** | **allocation lost** |
| `param_change` | `SLOT_CONFLICT`, free equivalent slot | change one bind parameter | 85 | completed |
| `partial_replay` | `PUBLISH_TIER_MISMATCH` | replay publish, keep the hold | 125 | completed |
| `hold_expired` | `RESERVATION_EXPIRED` | replay spend, keep the receipt | 110 | completed |
| `structural_escalate` | `SLOT_CONFLICT`, only a pricier slot | escalate to budget owner | 75 | escalated |
| `structural_escalate_approved` | same, human approves | amend hold, bind, no restart | 110 | completed |
| `compound` | degraded, **then** `SLOT_CONFLICT` | retry bind, then change parameter | 95 | completed |
| `budget_terminal` | `BUDGET_EXHAUSTED` on spend | escalate immediately | 65 | escalated |

A full sequence costs 75 compute units (publish 40, spend 25, bind 10). Every
number above is measured, not asserted: `runs/summary.json`.

---

## The two required failure modes

### 1. Transient: targeted retry works, naive retry destroys the allocation

Bind loses a race to a soft lock that expires in about a second. The agent
retries bind alone: **85 units, completed.**

The naive baseline is implemented for real (`--strategy naive`) so you can
watch it lose rather than take my word for it. It releases the 60,000 budget
hold in order to start over. A process that was waiting on that pool takes
45,000 the instant it is freed, and the re-reserve fails:

```
FAIL bind LOCK_CONTENDED
[naive] backing off 1.16s, then discarding all artefacts and re-running the whole sequence
CALL spend POST /v1/reservations/res_b8dcfb7328:release
CALL spend POST /v1/reservations
FAIL spend BUDGET_EXHAUSTED: requested 60000, only 55000 available
RESULT EXHAUSTED  total 205 compute units
```

**205 units and the allocation is gone, to fix a one-second lock.** That is the
whole argument for the project in one trace. The budget hold is not a variable
you can reassign; releasing it is a bet on a shared resource, and the naive
strategy makes that bet without knowing it is making one.

**The baseline is deliberately steelmanned.** It honours the service's
`retry_after` hint before replaying. My first version replayed instantly, which
meant it lost every race against a time-based lock purely because it never
waited. That is a strawman, and I only noticed when a counterfactual run didn't behave
as I'd predicted. With the backoff in place, the *only* thing separating naive
from the policy agent is the **scope of what it redoes**, which is the
comparison this project is actually about.

And the honest counterfactual, since "you rigged the pool" is the obvious
challenge: set `predatory_drain_on_release` to `0` in `scenarios.py` and naive
**succeeds**, at 150 units against 85. The concurrent grab changes the
severity from "76% more expensive" to "allocation lost". It does not
manufacture the failure.

### 2. Structural: retry can never work, and neither can the clever fix

Bind rejects because the 09:00 window was permanently committed by another
allocation. 14:00 has gone the same way. The agent probes for alternatives and
finds two, and rejects both **for different reasons**:

- **11:00 at 72,000.** It could amend the existing hold from 60,000, but +12,000
  is +20%, and the spend service auto-approves only +10%. Guardrail G6 blocks it.
- **16:00 at 60,000.** Same price, but bind requires a `certified` publish tier
  for that regulated window, and publish will refuse to certify without a
  compliance reference we do not have. Guardrail G7 catches this *before* making
  the call.

So it escalates, after one bind attempt and 75 units spent, with a
structured handoff naming the person who can actually unblock it:

```
ESCALATE to budget_owner: bind refused with SLOT_CONFLICT
  hold_strategy: EXTEND  (hold has 199s against a 420s SLA, but one +300s
                          extension closes the gap - extend rather than lose
                          25u of completed spend work)
  preserved: publish=pub_c66d75d39b  spend=res_a9c84c633f
  units_to_finish_if_approved: 35     units_if_restarted: 75
```

Note the routing. The failure code says `SLOT_CONFLICT`, which sounds like an
engineering problem. The agent routes to the **budget owner** because it read
the *veto on the blocked plan*, not the failure code. The thing standing in the
way is a spend-authority limit. Routing off the error code would have paged the
wrong person.

Run `--scenario structural_escalate_approved` to see the approval path: the
agent amends the existing reservation and binds. Publish is never touched,
spend is never re-reserved. **110 units total, against 150 for a
restart-on-approval design.**

---

## How it decides

Three layers, in order. Guardrails beat scores.

**1. Classify.** `agent/taxonomy.py` maps each failure code to a class:
transient, structural-parametric (a different parameter might work),
structural-upstream (exactly one earlier artefact is void), or terminal.

Two decisions here I would defend:

- *Unknown codes default to structural, not transient.* Guessing "transient"
  on an unknown failure produces an infinite retry loop against a system that
  will never say yes. Guessing "structural" produces a slow escalation. Fail
  toward the recoverable mistake.
- *The service's own `retryable_hint` is advisory.* A service knows whether
  **it** can serve the request again. It does not know whether the caller's
  upstream artefacts survive a retry. We override the hint for codes we know
  and defer to it only for unknown 5xxs.

**2. Enumerate and veto.** `agent/policy.py` builds every plan that is even
arguably available, including the naive full replay, and runs them through
seven hard constraints:

| | Guardrail |
|---|---|
| **G1** | Never re-execute a stage whose artefact is still valid, *unless the plan redoes it deliberately as the fix* |
| **G2** | Never release a hold the pool cannot be relied on to refill |
| **G3** | Never exceed the 200-unit compute ceiling |
| **G4** | Stop insisting a failure is transient after 3 attempts |
| **G5** | A plan must finish before the budget hold lapses |
| **G6** | Never propose a spend increase beyond the agent's own authority |
| **G7** | Pre-flight upstream service rules instead of learning by failing |

G1's exemption clause is the one that took a second pass to get right. My first
version vetoed *any* re-execution of a valid stage, which killed
`upgrade_tier_then_bind`, a plan whose entire purpose is to republish at a
higher tier. Republishing on purpose is targeted work; republishing because you
gave up and restarted is waste. The guardrail has to tell those apart, so plans
declare an `intentional_redo` set.

**3. Score the survivors.** `p(success) × 1000 − compute − risk − human_cost`.
Escalation is priced at 250 units of human attention, so it wins only when the
autonomous options are genuinely poor. Retry confidence decays 15% per repeat
attempt, so a "transient" failure that keeps recurring falls below the
escalation threshold on its own, before G4's hard cap is reached. G4 is a
backstop, and there is a test asserting the scorer gives up first.

Rejected plans stay in the log with their veto reasons, because "why didn't you
just retry everything" is the first question a reviewer asks:

```
CANDIDATE RECOVERY PLANS
  plan                       units   p(ok)    risk  human    score  verdict
  targeted_retry                10    0.85       0      0    840.0
  escalate                       0    0.90       0    250    650.0
  abort                          0    0.00     800      0   -800.0
  full_replay                   75    0.45       0      0     -inf  VETO: G1 ...; G2 ...
```

---

## Why the stages are really separate

`bind` does not trust the identifiers it is handed. It calls publish and spend
over HTTP to verify the receipt is `ACTIVE`, the tier is high enough, the hold
is `HELD`, and the amount covers the slot price. Three failure modes come out of
those checks rather than out of bind's own state.

That matters because of one rule in the publish service: **issuing a receipt
supersedes the previous one for that allocation.** Content is versioned. So
"re-publish and then re-bind" is not idempotent. It invalidates the receipt
another in-flight step may be holding. The architecture is what makes naive
replay dangerous, not a flag I set to make the demo work.

Likewise the spend service holds a *shared* pool. A release is not an undo: the
money goes back where anyone can take it. `predatory_drain_on_release` models
the process that is watching for exactly that, which is what turns the naive
baseline from "wasteful" into "fatal".

---

## Where the LLM is, and why it is not in charge

`--planner llm` puts Claude in the loop. It sits **inside** the guardrails: the
policy engine enumerates and vetoes, the model picks among plans that already
passed, and its choice is re-validated before it runs. If it picks a vetoed
plan it is overruled and the override is logged.

I did not make the model the decision-maker, and I would argue against it. This
recovery decision is a small optimisation over an enumerable option set. A
deterministic scorer does that better: reproducible, testable, free, and it
cannot be talked into releasing a budget hold. What a model adds is judgement
on the parts that are not enumerable: reading an unfamiliar failure code,
breaking a near-tie for reasons the cost model does not capture, writing an
escalation a human can act on.

Two tests cover this with a fake planner: one that picks `full_replay` and gets
overruled, and one that picks a feasible plan and is honoured with attribution.
The system's behaviour should not depend on what the model says, and those
tests are how I know it does not.

**No API key is needed.** The default planner is deterministic and fully
offline; that is what the test suite runs.

---

## Layout

```
services/          three FastAPI apps, one per stage, own state, own rules
  publish_service.py   receipts, tiers, versioning (new receipt supersedes old)
  spend_service.py     shared pool, holds with TTL, ±10% amendment authority
  bind_service.py      slot locks; verifies the other two services itself
agent/
  taxonomy.py      failure code -> class, what it invalidates, retry prior
  cost.py          every number the policy argues about, in one file
  policy.py        generate -> veto -> score -> choose
  orchestrator.py  the loop
  llm_planner.py   optional Claude planner, behind the guardrails
  journal.py       terminal output and JSONL, same event stream
scenarios.py       eight worlds; sets up reality, never tells the agent what to do
tests/             61 tests; conftest spawns real service processes
```

One structural choice worth flagging: **recovery plans never execute stages.** A
plan applies preparatory effects (change a parameter, amend a hold, invalidate
one upstream artefact, wait) and returns control to the main loop, which runs
whatever currently lacks a valid artefact. So there is exactly one code path
that calls a stage. "Did the recovery actually skip the expensive work?" is
answered by the artefact table, not by trusting two parallel implementations to
stay in agreement.

---

## Further reading

[`docs/DECISIONS.md`](docs/DECISIONS.md). Every non-obvious choice with the
alternative I rejected and why, including the ones I'd expect pushback on.

---

## What I would do next

Roughly in the order I think it matters.

1. **The cost model is asserted, not learned.** `publish=40, spend=25, bind=10`
   and `p_same_retry=0.85` are my numbers. The decisions are only as good as
   they are. The fix is to record outcomes per `(failure_code, plan)` pair and
   update the priors from observed success rates. The taxonomy is already
   shaped for it, since `p_same_retry` is a single field per code. Until then,
   every number is a hypothesis wearing a decimal point.

2. **No durable state.** Everything lives in one process. If the agent dies
   mid-escalation, the receipt and the hold leak. Real version: persist the
   journal to Postgres, make the escalation a resumable record with a token,
   and run a reaper that releases orphaned holds. The `--auto-approve` path
   demonstrates resumption within a single process, which is the easy half.

3. **No idempotency keys.** If a bind request times out after the service
   committed it, the agent retries and double-books. Every mutating call should
   carry a client-generated key that the services deduplicate on. This is the
   most likely thing to break in production and I did not build it.

4. **Escalation is a print statement.** It should be a Slack message or a ticket
   with a callback that resumes the workflow.

5. **Concurrency is simulated, not real.** Locks and pool drainage are driven by
   control endpoints, not by a second agent actually racing this one. Two agents
   competing for the same window would be a much better test, and might well
   surface a livelock the current design does not handle: both back off,
   both retry, neither wins.

6. **Two thresholds are unprincipled.** `VALUE_OF_COMPLETION = 1000` and
   `HUMAN_REVIEW_COST = 250` set the escalation boundary between them, and I
   chose them by picking numbers that produced sensible behaviour on these
   eight scenarios. That is fitting to the test set. They should come from
   what an allocation is actually worth and what an analyst's time actually
   costs.

### Things I know are imperfect

- **The guardrails are partly redundant with the scorer.** I disabled G1
  entirely and only one unit test failed. End-to-end behaviour was unchanged,
  because `targeted_retry` outscores `full_replay` anyway. That is defence in
  depth rather than wasted code, and I would keep both, but I do not want to
  claim the veto layer is load-bearing in these eight scenarios when it mostly
  is not. It becomes load-bearing the moment the cost model is wrong, which is
  exactly when you want it.

- **`p_success` values feed the scores but nothing verifies them.** A plan with
  `p_success=0.85` succeeds 100% of the time in these deterministic scenarios.
  The probabilities are doing real work in the *comparison* between plans and
  no work at all as calibrated forecasts.

- **The retry wait is a blocking sleep.** Fine for one workflow, wrong for a
  service that runs thousands.

- **Eight scenarios is not coverage.** They are the cases I thought of. The
  `compound` scenario chains a transient failure into a structural one and the
  agent handles it, but three-deep chains, failures *during* recovery (an amend
  that itself fails), and simultaneous failures across two stages are all
  untested. I would expect the next bug to be in one of those.

- **Every scenario is deterministic by construction.** Fault injection is
  scripted, so nothing here demonstrates behaviour under genuine timing
  nondeterminism. `hold_expired` is the closest and it relies on a fixed
  1500ms latency injection.
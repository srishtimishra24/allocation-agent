# Design decisions

Every non-obvious choice, with the alternative I rejected and why. If a
reviewer disagrees with one of these, this is the file to argue with.

---

## 1. The stages are real processes, and bind verifies the other two itself

**Alternative:** three Python functions, or three FastAPI apps tested through
in-process ASGI transports.

**Why not:** the brief says "separate system with its own API". More
practically, if bind trusts the receipt and reservation IDs it is handed, then
the interesting failure ("the receipt you're holding was superseded") cannot
happen, and the naive-replay hazard becomes something I have to assert instead
of demonstrate. Bind calling publish and spend over HTTP is what makes three
of its five refusal codes possible at all.

The tests spawn real uvicorn subprocesses for the same reason. ASGI transports
would quietly test a different architecture from the one being demonstrated.

---

## 2. Re-publishing supersedes the previous receipt

This single rule in the publish service is what makes naive replay unsafe
rather than merely wasteful. Content is versioned; you cannot hold two live
receipts for one allocation. So "re-publish then re-bind" invalidates the
receipt another in-flight step may be checking.

**Why it's fair:** it is a rule the service enforces for its own reasons,
content versioning, not a tripwire added to make the agent look clever. Any
system with versioned artefacts behaves this way.

---

## 3. A budget hold is a hold, not a copy

Releasing a reservation returns money to a shared pool that other processes
draw from. `predatory_drain_on_release` models a process watching for exactly
that moment.

**Why it matters:** it converts "release and re-reserve" from a free undo into
an irreversible bet. This is the single most important property in the system;
guardrail G2 exists only because of it, and it is what turns the naive baseline
from wasteful into fatal.

**Objection I'd accept:** a real budget service would probably offer a
transactional swap (re-reserve while still holding). That would be a better
API. I modelled the worse, more common one on purpose, because the recovery
problem is only interesting when the undo isn't free.

---

## 4. Recovery plans do not execute stages

A plan applies preparatory effects (change a parameter, amend a hold,
invalidate one upstream artefact, wait) and hands control back to the main
loop, which runs whatever lacks a valid artefact.

**Alternative:** each plan executes its own steps end to end.

**Why not:** that gives two code paths that call stages, and they drift. With
one path, "did the recovery actually skip the expensive work?" is answered by
the artefact table and the attempt counters, which is exactly what the tests
assert. It also makes `RETRY_SAME` a genuine no-op, which is a good sign the
abstraction is right.

---

## 5. Unknown failure codes classify as structural

**Alternative:** default to transient, or trust the service's `retryable_hint`.

**Why not:** the two errors are not symmetric. Wrongly calling something
transient produces an unbounded retry loop against a system that will never say
yes. It burns compute, holds a reservation, and delays the escalation
indefinitely. Wrongly calling something structural produces one unnecessary
escalation. Fail toward the recoverable mistake.

The service hint is used only as a tiebreaker for codes we've never seen, and
only alongside a 5xx. A service knows whether *it* can serve the request again;
it has no idea whether the caller's upstream artefacts survive a retry. That is
not information it possesses, so it should not be authoritative.

---

## 6. Guardrails are vetoes, not penalties

A vetoed plan scores `-inf`. It cannot be chosen at any price.

**Alternative:** heavy score penalties, so an extremely attractive plan could
still override.

**Why not:** the guardrails encode things that are *unsafe* or *impossible*,
not things that are *expensive*. G7 says publish will refuse the request, and no
score should be able to buy past that. G2 says the money may not come back:
that is a correctness property, not a cost. Mixing safety into the same scalar
as cost is how cost-minimising systems eventually do something stupid for a
small saving.

**Honest caveat:** in all eight scenarios, scoring alone would also avoid the
full replay. I disabled G1 and end-to-end behaviour didn't change. The veto
layer is defence in depth, and it becomes load-bearing precisely when the cost
model is wrong, which is the situation it exists for. But I'm not going to
claim it's doing heavy lifting today.

---

## 7. G1 exempts deliberate re-execution

First version: veto any plan that re-runs a stage whose artefact is still
valid. That vetoed `upgrade_tier_then_bind`, whose entire purpose is to
republish at a higher tier.

The fix is a per-plan `intentional_redo` set. The distinction is real:
republishing because the tier is wrong is targeted work; republishing because
you gave up and restarted is waste. A guardrail that cannot tell those apart
blocks the plans it exists to promote.

Worth noting this only surfaced because the enumeration includes plans the
scorer would never pick. The bug was visible in the veto column before it
could affect a decision.

---

## 8. Escalation routes off the veto, not the error code

In `structural_escalate` the error code is `SLOT_CONFLICT`, which reads as an
engineering problem. The actual blocker is that the only viable plan needs
+20% spend and the agent's authority is +10%. The person who can help is the
budget owner.

So `_assignee` reads the vetoes on the blocked plans first and falls back to
the classification tags only when nothing was blocked. Routing off the error
code would page the wrong person, which is the kind of thing that makes teams
turn alerting off.

---

## 9. The hold strategy during escalation

Three branches, chosen by comparing the hold's remaining life against the
expected human response time:

| Remaining vs SLA | Action | Reasoning |
|---|---|---|
| outlives it | `HOLD` | preserve 25u of completed spend work |
| +300s extension closes the gap | `EXTEND` | cheaper than losing the hold |
| cannot be stretched far enough | `UNWIND` | it will lapse anyway |

The third is the interesting one. An expiring hold is not an asset. Releasing
it early is strictly better for the organisation, because the money returns to
the pool where something else can use it, and we attach a replay plan so approval
doesn't mean starting from zero. Letting it rot preserves nothing and denies
the budget to everyone else in the meantime.

---

## 10. Failed attempts are charged

The service did the work and then refused; the compute is spent either way.
Charging only successes would make retries look free and bias every decision
toward "try again". Deleting this line breaks a test.

---

## 11. Retry confidence decays, and the hard cap is a backstop

`p_same_retry` decays 15% per repeat attempt. By the third or fourth attempt a
`targeted_retry` scores below `escalate` and the agent gives up on its own. G4's
hard cap at 3 attempts exists in case the arithmetic ever stops working.

There's a test asserting the scorer gives up *before* the cap fires. If that
test ever fails, the cap is silently doing the work and the scoring is broken.

---

## 12. The LLM is inside the guardrails, not above them

The policy engine enumerates and vetoes; the model chooses among survivors; the
choice is re-validated before it runs. An LLM in the loop must not be able to
do something the same system would refuse a human operator.

**Why not make the model the planner:** this decision is a small optimisation
over an enumerable option set with a well-specified objective. A deterministic
scorer is reproducible, testable, free, and cannot be argued into releasing a
budget hold. The model earns its place on the parts that aren't enumerable:
unfamiliar failure codes, near-ties, writing an escalation a human can act on.

Two tests with a fake planner cover both directions: a vetoed pick is overruled
and logged, a feasible pick is honoured and attributed. The system's behaviour
should not depend on what the model says, and that is how I know it doesn't.

---

## 13. Numbers I chose by taste, and would want to replace

- `VALUE_OF_COMPLETION = 1000` and `HUMAN_REVIEW_COST = 250` together set the
  escalation threshold. I picked them to produce sensible behaviour on these
  eight scenarios, which is fitting to the test set.
- `p_success` values are priors I invented. They do real work comparing plans
  and no work as calibrated forecasts. Every plan that runs here succeeds.
- Stage costs 40/25/10 are plausible for a moderation-heavy pipeline but are
  not measured.

The taxonomy is deliberately shaped so these can be learned later:
`p_same_retry` is one field per failure code, so recording outcomes per
`(code, plan)` pair and updating from observed success rates is a contained
change rather than a rewrite. That is the first thing I'd build next.
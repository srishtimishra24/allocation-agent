# 3-minute video script

**Hard rule: they stop watching at 3:00.** So the payoff — naive retry losing
the allocation — happens at 0:40, not at 2:30. Architecture comes after,
because if they stop at 1:30 they have still seen the thing that matters.

Before recording:

```bash
cd allocation-agent
./start_services.sh
clear
```

Have two things ready: a terminal, and `agent/policy.py` open at the guardrail
section. Nothing else. No slides.

---

## 0:00 – 0:15 — What this is

> "Three-stage allocation workflow. Publish, spend, bind. Three separate HTTP
> services, each enforcing its own rules — bind actually calls the other two to
> verify what it's been handed, it doesn't trust the caller.
>
> The problem is what happens when bind rejects work that publish and spend
> already committed to."

*(Show the three services running, or just `ls services/`. Five seconds, no
lingering.)*

---

## 0:15 – 0:55 — The payoff, first

```bash
python run_agent.py --scenario transient --compare
```

While it scrolls:

> "Bind loses a race to a lock that expires in one second. The agent classifies
> that as transient and retries bind alone. Eighty-five compute units, done.
>
> Now the same world with the naive strategy — full replay on any failure."

*(Point at the naive trace.)*

> "It releases the sixty-thousand budget hold to start over. But that pool is
> shared. Another process takes forty-five thousand the instant it's freed, and
> the re-reserve fails. Two hundred and five units, and **the allocation is
> gone** — to fix a one-second lock.
>
> That's the whole point. The budget hold isn't a variable you can reassign.
> Releasing it is a bet, and naive retry makes that bet without knowing it."

**This is the most important 40 seconds. Do not rush it and do not talk over
the BUDGET_EXHAUSTED line — let it land.**

---

## 0:55 – 1:45 — The structural case

```bash
python run_agent.py --scenario structural_escalate
```

> "Different failure. The 09:00 slot is permanently committed by someone else.
> Retrying can never work.
>
> So the agent probes for alternatives and finds two — and rejects both, for
> different reasons."

*(Point at the candidate table.)*

> "Eleven o'clock is free at seventy-two thousand. It could amend the existing
> hold — but that's plus twenty percent and the spend service only auto-approves
> plus ten. Guardrail G6.
>
> Sixteen hundred is the same price, but bind requires a certified publish tier
> for that regulated window, and publish won't certify without a compliance
> reference we don't have. G7 catches that **before** making the call — it
> doesn't learn by failing.
>
> So it escalates. One bind attempt, seventy-five units."

*(Point at the escalation block.)*

> "Two things I'd defend here. It routes to the **budget owner**, not an
> engineer — even though the error code says SLOT_CONFLICT. It routes off the
> *veto on the blocked plan*, not the error code, because the thing actually in
> the way is a spend limit. Routing off the code pages the wrong person.
>
> And it extends the budget hold rather than dropping it — the hold has 199
> seconds, the human SLA is 420, one extension closes the gap. If it couldn't
> close the gap it'd release the money early instead of letting it rot. An
> expiring hold isn't an asset."

```bash
python run_agent.py --scenario structural_escalate_approved
```

> "Approval path: amend the hold, bind. Publish is never touched, spend is
> never re-reserved. 110 units against 150 for a restart-on-approval design."

---

## 1:45 – 2:20 — How it decides

Switch to `agent/policy.py`.

> "Three layers. Classify the failure, enumerate every plan including the naive
> one, veto the unsafe ones, then score the survivors on expected value.
>
> Guardrails beat scores — that ordering is deliberate. A pure cost-minimiser
> eventually does something unsafe for a small saving."

*(Scroll to G1.)*

> "G1 took two passes. First version vetoed re-running any stage whose artefact
> was still valid — which killed the plan whose entire purpose is republishing
> at a higher tier. Republishing *on purpose* is targeted work. Republishing
> because you gave up and restarted is waste. The guardrail has to tell those
> apart, so plans declare what they redo deliberately."

*(Back to a candidate table in the terminal.)*

> "And rejected plans stay in the log with their reasons. 'Why didn't you just
> retry everything' is the first question anyone asks, so the answer is printed
> every time."

---

## 2:20 – 2:45 — Two decisions I'd argue for

> "First: unknown failure codes classify as **structural**, not transient. A
> wrong 'transient' guess is an infinite retry loop against a system that will
> never say yes. A wrong 'structural' guess is a slow escalation. Fail toward
> the recoverable mistake.
>
> Second: there's an optional Claude planner, and it is deliberately **not** in
> charge. It picks among plans that already passed the guardrails, and if it
> picks a vetoed one it gets overruled and logged. This decision is a small
> optimisation over an enumerable set — a deterministic scorer does that better,
> and it can't be talked into releasing a budget hold. There's a test with a
> fake planner that picks full replay, to prove the behaviour doesn't depend on
> what the model says."

---

## 2:45 – 3:00 — What's not done

> "Sixty-one tests, all passing. What's missing, honestly:
>
> No idempotency keys — if bind times out after committing, a retry
> double-books. That's the most likely production break and I didn't build it.
>
> No durable state, so a crash mid-escalation leaks the receipt and the hold.
>
> And the cost model is asserted, not learned. Publish costs forty because I
> said so. Every number is a hypothesis wearing a decimal point — the taxonomy
> is shaped so those priors can be updated from observed outcomes, but they
> aren't yet.
>
> README has the full list. Thanks."

---

## If you overrun

Cut in this order:

1. The `structural_escalate_approved` run (mention it, don't run it) — saves 15s
2. The G1 story in the policy walkthrough — saves 20s
3. The "two decisions" section down to just the unknown-codes point — saves 15s

**Never cut:** the `--compare` run, or the "what's not done" ending. The first
is the evidence; the second is what they said they weigh most.

## Recording notes

- Font size up. They are watching in a small window.
- `--compare` takes about 3 seconds of wall clock. Don't cut the dead air out —
  it reads as real.
- If a run fails live, say so and keep going. Working code with an honest
  walkthrough beats a polished demo of something that doesn't run — they said
  it themselves.
- Upload unlisted, then **open the link in a logged-out browser** before
  sending. Same for the repo.

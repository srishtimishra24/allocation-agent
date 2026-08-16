"""Structured run journal.

Two consumers: a human watching the terminal, and a JSONL file that the tests
assert against. Both come from the same event stream, so what you see in the
demo is exactly what the tests check.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"


class Journal:
    def __init__(self, run_id: str, path: Path | None = None, quiet: bool = False) -> None:
        self.run_id = run_id
        self.events: list[dict[str, Any]] = []
        self.path = path
        self.quiet = quiet
        self._t0 = time.time()
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")

    # ------------------------------------------------------------ plumbing --
    def _emit(self, kind: str, line: str, colour: str = "", **data: Any) -> None:
        ev = {"t": round(time.time() - self._t0, 3), "kind": kind, **data}
        self.events.append(ev)
        if self.path:
            with self.path.open("a") as fh:
                fh.write(json.dumps(ev, default=str) + "\n")
        if not self.quiet:
            print(f"{DIM}{ev['t']:6.2f}s{RESET} {colour}{line}{RESET}", flush=True)

    # -------------------------------------------------------------- events --
    def header(self, title: str, subtitle: str = "") -> None:
        if not self.quiet:
            print(f"\n{BOLD}{'=' * 78}{RESET}")
            print(f"{BOLD}{title}{RESET}")
            if subtitle:
                print(f"{DIM}{subtitle}{RESET}")
            print(f"{BOLD}{'=' * 78}{RESET}")
        self.events.append({"kind": "header", "title": title, "subtitle": subtitle})

    def plan(self, order: list[str], note: str) -> None:
        self._emit("plan", f"PLAN   {' -> '.join(order)}  {DIM}({note}){RESET}", CYAN, order=order, note=note)

    def tool_call(self, stage, method: str, url: str, body: Any) -> None:
        short = url.split("/", 3)[-1]
        self._emit("tool_call", f"  CALL {stage} {method} /{short}", DIM,
                   stage=str(stage), method=method, url=url, body=body)

    def tool_result(self, stage, body: Any, failure: Any) -> None:
        if failure is None:
            self._emit("tool_ok", f"  OK   {stage}", GREEN, stage=str(stage), body=body)
        else:
            self._emit("tool_fail", f"  FAIL {stage} {failure.code}: {failure.message}", RED,
                       stage=str(stage), code=failure.code, message=failure.message,
                       http_status=failure.http_status, detail=failure.detail)

    def stage_done(self, stage, ref: str, units: int, total: int) -> None:
        self._emit("stage_done", f"DONE   {stage} -> {ref}  [+{units}u, total {total}u]", GREEN,
                   stage=str(stage), ref=ref, units=units, total_units=total)

    def classified(self, failure, classification) -> None:
        self._emit(
            "classified",
            f"CLASSIFY {failure.code} -> {classification.failure_class.value}"
            f"{DIM}  {classification.note}{RESET}",
            YELLOW,
            code=failure.code,
            failure_class=classification.failure_class.value,
            invalidates=[str(s) for s in classification.invalidates],
            note=classification.note,
        )

    def candidates(self, plans: list[Any]) -> None:
        if not self.quiet:
            print(f"{MAGENTA}CANDIDATE RECOVERY PLANS{RESET}")
            print(f"{DIM}  {'plan':<26}{'units':>6}{'p(ok)':>8}{'risk':>8}{'human':>7}{'score':>9}  verdict{RESET}")
            for p in sorted(plans, key=lambda x: -x.score):
                verdict = f"{RED}VETO: {'; '.join(p.vetoes)}{RESET}" if p.vetoes else ""
                print(f"  {p.name:<26}{p.estimated_units:>6}{p.p_success:>8.2f}"
                      f"{p.risk_penalty:>8.0f}{p.human_cost:>7.0f}{p.score:>9.1f}  {verdict}")
        self.events.append({
            "kind": "candidates",
            "plans": [
                {"name": p.name, "units": p.estimated_units, "p_success": p.p_success,
                 "risk_penalty": p.risk_penalty, "human_cost": p.human_cost,
                 "score": round(p.score, 2), "vetoes": p.vetoes, "steps": p.describe(),
                 "rationale": p.rationale}
                for p in plans
            ],
        })

    def chose(self, plan, source: str) -> None:
        self._emit("decision", f"DECIDE [{source}] {BOLD}{plan.name}{RESET}{MAGENTA} :: {plan.describe()}",
                   MAGENTA, plan=plan.name, source=source, steps=plan.describe(),
                   rationale=plan.rationale, score=round(plan.score, 2))
        if not self.quiet:
            print(f"{DIM}       why: {plan.rationale}{RESET}")

    def veto(self, plan_name: str, reason: str) -> None:
        self._emit("veto", f"VETO   {plan_name}: {reason}", RED, plan=plan_name, reason=reason)

    def escalation(self, payload: dict[str, Any]) -> None:
        self._emit("escalation", f"ESCALATE to {payload['assignee_role']}: {payload['summary']}",
                   YELLOW, **payload)
        if not self.quiet:
            for opt in payload.get("options", []):
                print(f"{DIM}       option: {opt}{RESET}")

    def note(self, text: str, **data: Any) -> None:
        self._emit("note", f"NOTE   {text}", BLUE, text=text, **data)

    def result(self, outcome: str, units: int) -> None:
        colour = GREEN if outcome == "completed" else YELLOW
        self._emit("result", f"RESULT {outcome.upper()}  total {units} compute units",
                   colour, outcome=outcome, units=units)

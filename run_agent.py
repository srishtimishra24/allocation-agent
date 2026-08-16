#!/usr/bin/env python3
"""CLI entry point.

  python run_agent.py --list
  python run_agent.py --scenario transient
  python run_agent.py --scenario transient --strategy naive     # watch it lose
  python run_agent.py --all
  python run_agent.py --scenario structural_escalate --planner llm
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from agent import cost as C
from agent.journal import BOLD, DIM, GREEN, RED, RESET, YELLOW, Journal
from agent.llm_planner import LLMPlanner
from agent.orchestrator import Orchestrator
from agent.tools import Tools
from scenarios import SCENARIOS, services_up, setup

RUNS = Path(__file__).parent / "runs"


async def run_one(key: str, strategy: str, planner_kind: str, quiet: bool = False,
                  auto_approve: bool | None = None) -> dict:
    scn = SCENARIOS[key]
    await setup(scn)

    label = f"{scn.title}"
    if strategy == "naive":
        label += "   [NAIVE BASELINE]"
    j = Journal(f"{key}-{strategy}", RUNS / f"{key}-{strategy}.jsonl", quiet=quiet)
    j.header(f"SCENARIO: {key}", label)
    if not quiet:
        print(f"{DIM}{scn.story}{RESET}")
        print(f"{DIM}Naive approach: {scn.naive_would}{RESET}\n")

    tools = Tools(journal=j)
    planner = None
    if planner_kind == "llm":
        planner = LLMPlanner(journal=j)
        if not planner.available:
            j.note("no ANTHROPIC_API_KEY - running on the deterministic policy")
            planner = None
    orch = Orchestrator(
        tools, j,
        strategy=strategy,
        planner=planner,
        auto_approve=scn.auto_approve if auto_approve is None else auto_approve,
    )
    try:
        result = await orch.run(scn.request)
    finally:
        await tools.aclose()

    saved = C.FULL_SEQUENCE_COST + result.units_spent  # what a restart would have added
    if not quiet:
        print()
        print(f"  outcome        : {result.outcome.value}")
        print(f"  compute units  : {result.units_spent}   "
              f"{DIM}(a full replay on top of this would have been +{C.FULL_SEQUENCE_COST}u){RESET}")
        print(f"  tool calls     : {tools.call_count}")
        print(f"  artefacts kept : "
              f"{ {str(s): a.ref for s, a in result.state.artifacts.items() if a.valid} }")
        print(f"  stage attempts : { {str(k): v for k, v in result.state.attempts.items()} }")
        print(f"  journal        : runs/{key}-{strategy}.jsonl")

    return {
        "scenario": key,
        "strategy": strategy,
        "outcome": result.outcome.value,
        "units": result.units_spent,
        "tool_calls": tools.call_count,
        "plans": [d.get("plan") for d in result.decisions],
        "attempts": {str(k): v for k, v in result.state.attempts.items()},
        "escalation": result.escalation,
        "_saved": saved,
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=sorted(SCENARIOS))
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--strategy", choices=["policy", "naive"], default="policy")
    ap.add_argument("--planner", choices=["policy", "llm"], default="policy")
    ap.add_argument("--compare", action="store_true",
                    help="run the scenario under both the policy agent and the naive baseline")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.list:
        for k, s in SCENARIOS.items():
            print(f"{BOLD}{k:<30}{RESET}{s.title}")
            print(f"{DIM}  {s.story}{RESET}\n")
        return 0

    if not await services_up():
        print(f"{RED}Services are not running. Start them with: ./start_services.sh{RESET}")
        return 2

    keys = sorted(SCENARIOS) if args.all else [args.scenario]
    if keys == [None]:
        ap.print_help()
        return 1

    results = []
    for k in keys:
        if args.compare:
            results.append(await run_one(k, "policy", args.planner, args.quiet))
            results.append(await run_one(k, "naive", args.planner, args.quiet, auto_approve=False))
        else:
            results.append(await run_one(k, args.strategy, args.planner, args.quiet))

    print(f"\n{BOLD}{'=' * 78}{RESET}")
    print(f"{BOLD}SUMMARY{RESET}")
    print(f"  {'scenario':<32}{'mode':<8}{'outcome':<12}{'units':>6}  recovery")
    for r in results:
        colour = GREEN if r["outcome"] == "completed" else YELLOW
        plans = ", ".join(p for p in r["plans"] if p) or "-"
        print(f"  {r['scenario']:<32}{r['strategy']:<8}{colour}{r['outcome']:<12}{RESET}"
              f"{r['units']:>6}  {plans}")
    (RUNS / "summary.json").write_text(json.dumps(results, indent=2, default=str))
    print(f"{DIM}  written to runs/summary.json{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

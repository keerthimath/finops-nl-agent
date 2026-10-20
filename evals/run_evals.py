#!/usr/bin/env python3
"""Run golden questions through the real agent (real LLM, real ADX) and check the KQL it wrote.

    az login && python evals/run_evals.py            # uses AUTH_MODE=dev (your own identity)
    python evals/run_evals.py --threshold 0.9        # exit 1 if pass rate is lower

Costs a few LLM calls per case. Run it after any change to prompts/, the schema doc, or the model.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finops_nl.adx import AdxClient  # noqa: E402
from finops_nl.agent import Agent, build_system_prompt, make_llm  # noqa: E402
from finops_nl.config import Settings  # noqa: E402
from finops_nl.tools import QUERY_TOOLS, ToolRunner, load_dashboard_queries, load_references, session_profile  # noqa: E402


async def run_case(settings, llm, refs, case, dashboard=None) -> tuple[bool, list[str]]:
    adx = AdxClient(settings, None)
    profile = await session_profile(adx, "eval")
    runner = ToolRunner(settings, adx, refs, user="eval", dashboard=dashboard, profile=profile)
    agent = Agent(settings, llm, runner, build_system_prompt(currency=profile.currency, subscriptions=profile.subscriptions))
    turn = await agent.ask([], case["question"])
    print(f"      {turn.iterations} rounds, {turn.llm_ms / 1000:.1f}s in the LLM, {turn.tokens} tokens "
          f"({turn.usage['cached']} cached, {turn.usage['reasoning']} reasoning)")
    problems: list[str] = []
    kql = "\n".join(turn.kql)
    if case.get("expect_no_query"):
        if turn.kql:
            problems.append("ran a query but should not have")
        return (not problems), problems
    if not turn.kql:
        problems.append("no query was run")
    for pat in case.get("kql_must_match", []):
        if not re.search(pat, kql, re.S):
            problems.append(f"missing /{pat}/")
    for pat in case.get("kql_must_not_match", []):
        if re.search(pat, kql, re.S):
            problems.append(f"unexpected /{pat}/")
    if turn.outcomes and all(o.is_error for o in turn.outcomes if o.name in QUERY_TOOLS) and turn.kql:
        problems.append("every query errored")
    return (not problems), problems


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(Path(__file__).with_name("golden.yaml")))
    ap.add_argument("--threshold", type=float, default=1.0)
    args = ap.parse_args()

    settings = Settings.from_env()
    settings.validate()
    llm = make_llm(settings)
    refs = load_references(settings)
    dashboard = load_dashboard_queries(settings)
    cases = yaml.safe_load(Path(args.cases).read_text())["cases"]
    if dashboard is None:  # cases that only make sense with the team dashboard (DASHBOARD_FILE)
        cases = [c for c in cases if not c.get("requires_dashboard")]

    passed = 0
    for case in cases:
        ok, problems = await run_case(settings, llm, refs, case, dashboard)
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {case['id']}" + ("" if ok else "  -> " + "; ".join(problems)))
    rate = passed / len(cases)
    print(f"\n{passed}/{len(cases)} passed ({rate:.0%})")
    return 0 if rate >= args.threshold else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

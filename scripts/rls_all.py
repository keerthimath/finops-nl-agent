#!/usr/bin/env python3
"""Run an RLS runbook step on every table's config (scripts/teams*.yaml), after checking they agree.

Each protected table has its own config (one RLS function per table), but all of them must give the same people
the same access: `allow_all` and `teams` must be identical. This keeps the eight in step.

    python scripts/rls_all.py list                         # configs, tables, functions
    python scripts/rls_all.py check                        # identical allow_all/teams, unique tables/functions
    python scripts/rls_all.py preflight                    # rls_runbook.py <step> on each config, in order
    python scripts/rls_all.py apply                        # dry run for each
    python scripts/rls_all.py apply --execute              # apply each (stops at the first failure)
    python scripts/rls_all.py verify
    python scripts/rls_all.py rollback --execute

Every step runs `check` first and refuses to run if the configs disagree.
"""
from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys

import yaml

import gen_rls

HERE = os.path.dirname(os.path.abspath(__file__))
STEPS = ("preflight", "apply", "verify", "rollback")


def config_paths(directory: str = HERE) -> list[str]:
    paths = sorted(glob.glob(os.path.join(directory, "teams*.yaml")))
    return [p for p in paths if os.path.basename(p) != "teams.example.yaml"]


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def check(paths: list[str]) -> list[str]:
    """Problems that make the configs unsafe to apply together ([] = fine)."""
    if not paths:
        return ["no configs found (scripts/teams*.yaml)"]
    problems, plans = [], {}
    for path in paths:
        try:
            plans[path] = gen_rls.plan_from_config(load(path))
        except (KeyError, ValueError) as exc:
            problems.append(f"{os.path.basename(path)}: {exc}")
    if problems:
        return problems
    first_path, first = next(iter(plans.items()))
    access = (sorted(first.allow_all), sorted((tuple(sorted(t["principals"])), tuple(t["subs"])) for t in first.teams))
    for path, p in plans.items():
        if (sorted(p.allow_all), sorted((tuple(sorted(t["principals"])), tuple(t["subs"])) for t in p.teams)) != access:
            problems.append(f"{os.path.basename(path)}: allow_all/teams differ from {os.path.basename(first_path)}")
    for attr in ("table", "fn"):
        seen: dict[str, str] = {}
        for path, p in plans.items():
            key = f"{p.table_db}.{getattr(p, attr)}"
            if key in seen:
                problems.append(f"{os.path.basename(path)}: same {attr} {key} as {seen[key]}")
            seen[key] = os.path.basename(path)
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("step", choices=("list", "check") + STEPS)
    ap.add_argument("--execute", action="store_true", help="pass --execute to apply/rollback")
    args = ap.parse_args(argv)
    paths = config_paths()

    if args.step == "list":
        for path in paths:
            p = gen_rls.plan_from_config(load(path))
            print(f"{os.path.basename(path):45} {p.table_db}.{p.table:40} {p.fn}()  teams={len(p.teams)}")
        return 0
    problems = check(paths)
    if problems:
        print("Configs disagree; fix them before running anything:\n  " + "\n  ".join(problems))
        return 1
    if args.step == "check":
        print(f"{len(paths)} configs agree (same allow_all and teams; unique tables and functions).")
        return 0
    for path in paths:
        cmd = [sys.executable, os.path.join(HERE, "rls_runbook.py"), args.step, path]
        if args.execute:
            cmd.append("--execute")
        print(f"\n===== {' '.join(['python', 'scripts/rls_runbook.py', args.step, os.path.relpath(path)] + (['--execute'] if args.execute else []))}",
              flush=True)
        if subprocess.call(cmd) != 0:
            print(f"\nSTOPPED: {os.path.basename(path)} failed. Check that table's state before re-running "
                  "(see CLAUDE.md, RLS operations).")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

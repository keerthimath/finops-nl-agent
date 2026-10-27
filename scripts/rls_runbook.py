#!/usr/bin/env python3
"""Step-by-step, safety-checked runbook to enable subscription-level RLS on the FinOps Hub table in ADX.

    az login                                   # as a principal with Database Admin on the table's database
    export ADX_CLUSTER_URI=https://<cluster>.<region>.kusto.windows.net
    python scripts/rls_runbook.py preflight scripts/teams.yaml           # read-only checks
    python scripts/rls_runbook.py apply     scripts/teams.yaml           # DRY RUN: prints every command
    python scripts/rls_runbook.py apply     scripts/teams.yaml --execute # runs them with safety checks
    python scripts/rls_runbook.py verify    scripts/teams.yaml
    python scripts/rls_runbook.py rollback  scripts/teams.yaml --execute

Run it on a NON-PROD cluster first. `apply --execute` aborts (and auto-disables RLS) if the row count seen by
you changes, which means you are not in `allow_all` or the function is wrong.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import yaml

import gen_rls


class KustoRunner:
    """Runs one command or query via the Azure CLI identity. Management commands start with '.'."""

    def __init__(self, cluster: str):
        from azure.kusto.data import KustoClient, KustoConnectionStringBuilder

        self._c = KustoClient(KustoConnectionStringBuilder.with_az_cli_authentication(cluster))

    def run(self, database: str, text: str, retries: int = 5) -> list[dict]:
        # Retry dropped connections only (every command here is idempotent: create-or-alter, enable/disable, reads),
        # so a network blip between enabling RLS and re-counting can't skip the safety check.
        for attempt in range(retries + 1):
            try:
                if text.lstrip().startswith("."):
                    resp = self._c.execute_mgmt(database, text)
                else:
                    resp = self._c.execute(database, text)
                break
            except Exception as exc:  # noqa: BLE001
                if "network request" not in str(exc).lower() or attempt == retries:
                    raise
                time.sleep(3 * (attempt + 1))
        table = resp.primary_results[0] if resp.primary_results else None
        if table is None:
            return []
        out = []
        for r in table:
            d = r.to_dict()
            out.append({k: (v if isinstance(v, (int, float, str, bool, type(None))) else str(v)) for k, v in d.items()})
        return out


def _count_query(p: gen_rls.Plan, force: bool) -> str:
    prefix = "set query_force_row_level_security;\n" if force else ""
    return f"{prefix}{p.table} | summarize Rows=count(), Subs=dcount({p.col})"


def _count(runner, p, force=False) -> tuple[int, int]:
    rows = runner.run(p.table_db, _count_query(p, force))
    return int(rows[0]["Rows"]), int(rows[0]["Subs"])


def _step(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")


def _show(rows: list[dict], limit: int = 20) -> None:
    for r in rows[:limit]:
        print("    " + json.dumps(r, default=str)[:400])
    if len(rows) > limit:
        print(f"    ... {len(rows) - limit} more")


def preflight(runner, p: gen_rls.Plan, cfg: dict) -> int:
    print(f"Preflight for table {p.table} in database {p.table_db} (read-only)")
    _step(1, "Who am I? (need Database Admin on this database to apply)")
    _show(runner.run(p.table_db, "print current_principal_details()"))
    _step(2, "Is the table referenced by an update policy? (RLS cannot be enabled if one exists without a managed identity)")
    for label, cmd in (("this table's own update policy", f".show table {p.table} policy update"),
                       ("all tables' update policies (look for this table name)", ".show table * policy update")):
        print(f"  - {label}")
        try:
            rows = runner.run(p.table_db, cmd)
            hits = [r for r in rows if p.table in json.dumps(r, default=str)]
            _show(hits if label.startswith("all") else rows)
            if label.startswith("all") and not hits:
                print("    no update policy mentions this table")
        except Exception as exc:  # noqa: BLE001
            print(f"    could not run: {str(exc)[:200]}")
    _step(3, "Restricted view access policy (must be off)")
    try:
        _show(runner.run(p.table_db, f".show table {p.table} policy restricted_view_access"))
    except Exception as exc:  # noqa: BLE001
        print(f"    could not run: {str(exc)[:200]}")
    _step(4, "Materialized views over this table (need their own RLS)")
    try:
        mvs = runner.run(p.table_db, ".show materialized-views")
        hits = [r for r in mvs if p.table in json.dumps(r, default=str)]
        _show(hits)
        if not hits:
            print("    none reference this table")
    except Exception as exc:  # noqa: BLE001
        print(f"    could not run: {str(exc)[:200]}")
    _step(5, "Existing RLS policy on the table")
    _show(runner.run(p.table_db, f".show table {p.table} policy row_level_security"))
    _step(6, f"Rows and distinct {p.col} values currently in the table")
    rows, subs = _count(runner, p)
    print(f"    {rows} rows, {subs} distinct values")
    _step(7, f"Do the subscriptions in your config exist in {p.col}? (matching is case-insensitive)")
    if rows == 0:
        print("    table is empty: nothing to compare (not an error). Re-run preflight once it has data.")
        print("\nPreflight done. Review steps 1-5 yourself; fix anything above before running `apply`.")
        return 0
    found = {str(r["v"]).lower() for r in runner.run(p.table_db, f"{p.table} | summarize by v=tolower(tostring({p.col}))")}
    problems = 0
    for t in p.teams:
        for s in t["subs"]:
            ok = s in found
            problems += not ok
            print(f"    {'ok     ' if ok else 'MISSING'}  {t['name']}: {s}")
    if problems:
        print(f"\n  {problems} configured value(s) not found. Use the exact strings from `{p.table} | distinct {p.col}`.")
        print("  Sample values in the data:")
        for v in sorted(found)[:5]:
            print(f"    {v}")
    print("\nPreflight done. Review steps 1-5 yourself; fix anything above before running `apply`.")
    return 1 if problems else 0


def _policy(runner, p: gen_rls.Plan) -> dict | None:
    """The table's RLS policy as stored ({"IsEnabled": bool, "Query": str}), or None if it has none."""
    rows = runner.run(p.table_db, f".show table {p.table} policy row_level_security")
    raw = rows[0].get("Policy") if rows else None
    if not raw or raw == "null":
        return None
    return json.loads(raw) if isinstance(raw, str) else raw


def apply(runner, p: gen_rls.Plan, execute: bool, force: bool) -> int:
    steps = [
        (p.table_db, gen_rls.function_command(p), "Create the RLS function (does nothing until enabled)"),
        (p.table_db, gen_rls.disable_command(p),
         "Attach it as a DISABLED policy (only if no policy is enabled yet), so forced RLS really applies it"),
        (p.table_db, _count_query(p, True), "Dry-run the function on you: forced RLS must see the same rows as no RLS"),
        (p.table_db, gen_rls.enable_command(p), "Enable the policy"),
        (p.table_db, _count_query(p, False), "Re-check: you must still see all rows (else auto-rollback)"),
    ] + [(db, cmd, "Grant Database Viewer") for db, cmd in gen_rls.grant_commands(p)]
    if not execute:
        print("DRY RUN - nothing is executed. Commands, in order:\n")
        for i, (db, cmd, why) in enumerate(steps, 1):
            print(f"[{i}] ({db}) {why}\n{cmd}\n")
        print("Re-run with --execute to run them with the safety checks.")
        return 0

    _step(1, "Create the RLS function")
    runner.run(p.table_db, gen_rls.function_command(p))
    # `set query_force_row_level_security` only applies a policy attached to the table. Without one (first rollout),
    # the comparison below would always match and prove nothing, so attach the function as a disabled policy first.
    # An already enabled policy stays enabled (disabling it would expose every row while we test).
    current = _policy(runner, p)
    if current is None or not current.get("IsEnabled"):
        runner.run(p.table_db, gen_rls.disable_command(p))
        print("    attached as a disabled policy (affects no one; forced RLS applies it to your queries)")
    elif str(current.get("Query", "")).replace(" ", "") != f"{p.fn}()":
        print(f"    ABORT: an enabled policy uses a different query ({current.get('Query')}); the forced test would "
              f"not exercise {p.fn}(). Review it, then disable it or point it at {p.fn}() before re-running.")
        return 1
    _step(2, "Compare rows seen with and without forced RLS")
    plain = _count(runner, p, force=False)
    forced = _count(runner, p, force=True)
    print(f"    without RLS: {plain[0]} rows / {plain[1]} values; with forced RLS: {forced[0]} rows / {forced[1]} values")
    if plain != forced and not force:
        print("    ABORT: you do not see all rows through the function. Are you (or an app you run as) in allow_all?")
        print("    The function exists but RLS is NOT enabled. Fix allow_all, or pass --force if this is intended.")
        return 1
    _step(3, "Enable the policy")
    runner.run(p.table_db, gen_rls.enable_command(p))
    _step(4, "Re-check the row count")
    after = _count(runner, p, force=False)
    if after != plain and not force:
        print(f"    MISMATCH after enabling ({after} vs {plain}). Rolling back.")
        runner.run(p.table_db, gen_rls.disable_command(p))
        return 1
    print(f"    ok: {after[0]} rows still visible to you")
    _step(5, "Grant Database Viewer to team groups")
    for db, cmd in gen_rls.grant_commands(p):
        runner.run(db, cmd)
        print(f"    ({db}) {cmd}")
    print("\nDone. Now verify as a team member (not as you): `python scripts/rls_runbook.py verify ...`, then sign in to")
    print("the app or the ADX web UI as that person and run:  " + f"{p.table} | summarize by {p.col}")
    return 0


def verify(runner, p: gen_rls.Plan) -> int:
    _step(1, "Policy as stored")
    _show(runner.run(p.table_db, f".show table {p.table} policy row_level_security"))
    _step(2, "What YOU currently see")
    rows, subs = _count(runner, p)
    print(f"    {rows} rows, {subs} distinct {p.col} values")
    print("\nTo prove scoping, the same query must be run by a member of one team (expect only their")
    print("subscriptions) and by a non-member with Viewer access (expect no rows).")
    return 0


def rollback(runner, p: gen_rls.Plan, execute: bool) -> int:
    cmd = gen_rls.disable_command(p)
    if not execute:
        print(f"DRY RUN. Would run in {p.table_db}:\n{cmd}\nRe-run with --execute.")
        return 0
    runner.run(p.table_db, cmd)
    print(f"Disabled. ({p.table_db}) {cmd}")
    print("The function and viewer grants were left in place. Remove them separately if you want a full undo.")
    return 0


def main(argv=None, runner_factory=KustoRunner) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["preflight", "apply", "verify", "rollback"])
    ap.add_argument("config")
    ap.add_argument("--cluster", default=os.environ.get("ADX_CLUSTER_URI", ""))
    ap.add_argument("--execute", action="store_true", help="actually run apply/rollback (default is a dry run)")
    ap.add_argument("--force", action="store_true", help="skip the row-count safety checks in apply")
    a = ap.parse_args(argv)

    with open(a.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    p = gen_rls.plan_from_config(cfg)

    needs_connection = a.command in ("preflight", "verify") or a.execute
    runner = None
    if needs_connection:
        if not a.cluster:
            print("Set --cluster or ADX_CLUSTER_URI", file=sys.stderr)
            return 2
        runner = runner_factory(a.cluster)
    if a.command == "preflight":
        return preflight(runner, p, cfg)
    if a.command == "apply":
        return apply(runner, p, a.execute, a.force)
    if a.command == "verify":
        return verify(runner, p)
    return rollback(runner, p, a.execute)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run every saved query against the real hub and list the ones ADX rejects. No LLM involved.

The guard tests only prove queries are allowed; this proves ADX accepts them on YOUR data (e.g. a pivot that
needs two weeks of history, or a dashboard tile that needs a tag your resources don't have).

    az login
    ADX_CLUSTER_URI=https://<cluster>.<region>.kusto.windows.net python scripts/check_queries.py
    DASHBOARD_FILE=dashboards/finops.json python scripts/check_queries.py --only dashboard --write-exclude

--write-exclude saves the dashboard tiles that fail on this hub to <DASHBOARD_FILE>.exclude.json; the app then
leaves them out of run_dashboard_query (re-run after the hub gains the missing tags/columns).

Uses your own identity (like AUTH_MODE=dev), read-only, with the app's row cap and timeout.
Exit code 1 if any query fails.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from importlib import resources
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finops_nl.adx import AdxClient, AdxError  # noqa: E402
from finops_nl.config import Settings  # noqa: E402
from finops_nl.dashboard import exclude_path, load_dashboard  # noqa: E402

SOURCES = {"catalog": "query_catalog.md", "weekly": "weekly_report.md"}


def doc_queries(name: str) -> list[tuple[str, str]]:
    """(label, kql) for each runnable ```kusto block, labelled by the nearest heading."""
    text = resources.files("finops_nl").joinpath("prompts", name).read_text(encoding="utf-8")
    out = []
    for m in re.finditer(r"```kusto\n(.*?)```", text, re.S):
        body = [x for x in re.sub(r"//[^\n]*", "", m.group(1)).split(";") if x.strip()]
        if body and body[-1].strip().startswith("let "):
            continue  # a documented fragment (e.g. date boundaries), not a runnable query
        heading = re.findall(r"(?m)^#{2,3} (.+)$", text[: m.start()])
        out.append((heading[-1].strip() if heading else "(no heading)", m.group(1)))
    return out


def collect(settings: Settings, only: str | None) -> list[tuple[str, str, str]]:
    """(source, label, kql) for every query to check."""
    queries = []
    for src, name in SOURCES.items():
        if only in (None, src):
            queries += [(src, label, kql) for label, kql in doc_queries(name)]
    if only in (None, "dashboard") and settings.dashboard_file:
        dash = load_dashboard(settings.dashboard_file, settings.allowed_entities, settings.max_query_chars,
                              use_exclusions=False)  # check every tile, including previously excluded ones
        queries += [("dashboard", tile_id, dash.query(tile_id)) for tile_id in dash.tiles]
    return queries


def _query(adx, kql: str, retries: int = 3):
    for attempt in range(retries + 1):
        try:
            return adx.query(kql)
        except AdxError as exc:  # retry dropped connections only, never query errors
            if "network request" not in str(exc) or attempt == retries:
                raise
            time.sleep(2 * (attempt + 1))


def check(adx, queries) -> list[tuple[str, str, str]]:
    failures = []
    for src, label, kql in queries:
        try:
            res = _query(adx, kql)
            print(f"  ok    {src:9} {label}  ({res.row_count} rows, {res.elapsed_ms} ms)")
        except AdxError as exc:
            msg = str(exc).replace("Request is invalid and cannot be processed: ", "")
            failures.append((src, label, msg))
            print(f"  FAIL  {src:9} {label}\n        {msg[:300]}")
    return failures


def write_exclusions(dashboard_file: str, failures) -> dict:
    """Tiles ADX rejected (not ones that hit a network error) -> <dashboard>.exclude.json."""
    excluded = {label: msg[:300] for src, label, msg in failures
                if src == "dashboard" and "network request" not in msg}
    Path(exclude_path(dashboard_file)).write_text(json.dumps(excluded, indent=2) + "\n", encoding="utf-8")
    return excluded


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", choices=["catalog", "weekly", "dashboard"])
    ap.add_argument("--write-exclude", action="store_true",
                    help="save failing dashboard tiles to <DASHBOARD_FILE>.exclude.json")
    args = ap.parse_args(argv)
    settings = Settings.from_env()
    if not settings.adx_cluster_uri:
        ap.error("set ADX_CLUSTER_URI")
    queries = collect(settings, args.only)
    print(f"Checking {len(queries)} queries against {settings.adx_cluster_uri} / {settings.adx_database}")
    failures = check(AdxClient(settings, None), queries)
    print(f"\n{len(queries) - len(failures)}/{len(queries)} ran; {len(failures)} failed")
    if args.write_exclude and settings.dashboard_file:
        excluded = write_exclusions(settings.dashboard_file, failures)
        print(f"Wrote {len(excluded)} tile exclusions to {exclude_path(settings.dashboard_file)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

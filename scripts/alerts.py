#!/usr/bin/env python3
"""Azure Monitor log search alerts on FinOps hub cost data (see docs/alerts_runbook.md).

Rules query the hub from a Log Analytics workspace through `adx()` and run as a user-assigned managed identity that
is in RLS `allow_all`. Rule definitions live in RULES below (generic); environment ids in scripts/alerts.yaml
(git-ignored; copy scripts/alerts.example.yaml).

    python scripts/alerts.py list                         # rules defined here
    python scripts/alerts.py test <rule>                  # run its query now (as you, through the workspace)
    python scripts/alerts.py apply [<rule> ...]           # dry run: the rule bodies that would be PUT
    python scripts/alerts.py apply [<rule> ...] --execute # create or update them (az rest PUT)
    python scripts/alerts.py status                       # alerts fired by these rules in the last day
    python scripts/alerts.py disable|enable [<rule> ...]  # stop / restart rules (definitions, identity, emails kept)
    python scripts/alerts.py apply <rule> --frequency PT5M --execute   # temporarily run more often (e.g. to re-send
                                                                      # a daily report now); re-apply without it after
Use this script (or `az rest`), not `az monitor scheduled-query update`: that drops the identity and Azure then
rejects adx() ("The 'adx' pattern cannot be used with the current authentication scheme").

Needs `az login` with rights to create alert rules in the resource group.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field, replace

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
API = "2023-03-15-preview"
COSTS = "adx('{cluster}/Ingestion').Costs_final_v1_2"  # adx() reads tables, not the Hub functions


@dataclass(frozen=True)
class Rule:
    name: str              # resource name, prefixed "finops-"
    display: str
    description: str
    query: str             # KQL; {costs} = the cost table through adx()
    dims: tuple[str, ...]  # string columns: one alert per combination; their values appear in the email
    severity: int = 3      # 0 critical .. 4 verbose
    frequency: str = "P1D" # ISO 8601: PT5M .. P1D
    stateful: bool = False # True: fire once, resolve when the condition clears (needs frequency <= PT12H)
    extra: dict = field(default_factory=dict)


# Days are UTC. Azure cost data lags 24-48h, so "the last complete day" is 2 days back.
RULES = [
    Rule(
        name="finops-subscription-cost-trend",
        display="FinOps - Subscription cost trending up",
        description="Per subscription: last 7 complete UTC days vs the 7 before. Fires when the increase is over 20% "
                    "and over 5 (billing currency). Runs as finops-alerts-id.",
        severity=2, frequency="PT12H", stateful=True, dims=("SubAccountName", "BillingCurrency", "Detail"),
        query="""let end = startofday(ago(1d));   // exclusive: the last complete day is 2 days back (cost data lags 24-48h)
{costs}
| where ChargePeriodStart >= end - 14d and ChargePeriodStart < end
| summarize Last7 = sumif(EffectiveCost, ChargePeriodStart >= end - 7d), Prior7 = sumif(EffectiveCost, ChargePeriodStart < end - 7d)
    by SubAccountName, BillingCurrency
| extend Change = Last7 - Prior7, ChangePct = iff(Prior7 == 0, real(null), 100.0 * (Last7 - Prior7) / Prior7)
| where Change > 5 and (isnull(ChangePct) or ChangePct > 20)
| project SubAccountName, BillingCurrency,
    Detail = strcat("Last 7 days ", round(Last7, 2), " vs ", round(Prior7, 2), " (+", round(Change, 2), ", ",
                    iff(isnull(ChangePct), "new", strcat(round(ChangePct, 1), "%")), ")")"""),
    Rule(
        name="finops-daily-top-resource",
        display="FinOps - Daily top resource",
        description="Daily report: the highest-cost resource on the last complete UTC day (EffectiveCost). Fires every "
                    "day. Runs as finops-alerts-id.",
        severity=4, dims=("Day", "TopResource", "Cost"),
        query="""let day = startofday(ago(2d));   // last complete UTC day (cost data lags 24-48h)
{costs}
| where ChargePeriodStart >= day and ChargePeriodStart < day + 1d and isnotempty(ResourceId)
| summarize DailyCost = round(sum(EffectiveCost), 2) by ResourceName, SubAccountName, BillingCurrency
| top 1 by DailyCost
| project Day = format_datetime(day, 'yyyy-MM-dd'), TopResource = strcat(ResourceName, " (", SubAccountName, ")"),
    Cost = strcat(DailyCost, " ", BillingCurrency)"""),
    Rule(
        name="finops-month-top5-resources",
        display="FinOps - Month-to-date top 5 resources",
        description="Daily report: the 5 highest-cost resources this month so far (EffectiveCost, through the last "
                    "complete UTC day), in one notification. Runs as finops-alerts-id.",
        severity=4, dims=("Month", "Top5"),
        query="""let start = startofmonth(ago(2d));
{costs}
| where ChargePeriodStart >= start and ChargePeriodStart < startofday(ago(1d)) and isnotempty(ResourceId)
| summarize Cost = round(sum(EffectiveCost), 2) by ResourceName, BillingCurrency
| top 5 by Cost
| serialize Rank = row_number()
| summarize Top5 = strcat_array(make_list(strcat(Rank, ". ", ResourceName, " ", Cost, " ", BillingCurrency)), "; ")
| project Month = format_datetime(start, 'yyyy-MM'), Top5 = substring(Top5, 0, 250)"""),
]

_DURATION = re.compile(r"^P(T(\d+)([MH])|(1)D)$")


def minutes(iso: str) -> int:
    m = _DURATION.match(iso)
    if not m:
        raise ValueError(f"frequency must be PT<n>M, PT<n>H or P1D, got {iso!r}")
    if m.group(4):
        return 1440
    return int(m.group(2)) * (60 if m.group(3) == "H" else 1)


def validate(rule: Rule) -> list[str]:
    """Limits Azure Monitor enforces (found the hard way), checked before calling it."""
    problems = []
    if not rule.name.startswith("finops-") or not re.fullmatch(r"[a-z0-9-]{1,60}", rule.name):
        problems.append("name: lowercase letters, digits and '-', prefixed 'finops-'")
    try:
        mins = minutes(rule.frequency)
        if not 5 <= mins <= 1440:
            problems.append("frequency must be between PT5M and P1D")
        if rule.stateful and mins > 720:
            problems.append("stateful rules (auto-resolve) can't run less often than every 12 hours (PT12H)")
    except ValueError as exc:
        problems.append(str(exc))
    if not 1 <= len(rule.dims) <= 6:
        problems.append("1-6 dimensions")
    if "{costs}" not in rule.query and "adx(" not in rule.query:
        problems.append("query must read the hub through {costs} (adx())")
    if not 0 <= rule.severity <= 4:
        problems.append("severity 0-4")
    return problems


def render_query(rule: Rule, cfg: dict) -> str:
    return rule.query.replace("{costs}", COSTS.format(cluster=cfg["adx_cluster_uri"].rstrip("/")))


def body(rule: Rule, cfg: dict) -> dict:
    return {
        "location": cfg["location"], "kind": "LogAlert",
        "identity": {"type": "UserAssigned", "userAssignedIdentities": {cfg["identity_id"]: {}}},
        "properties": {
            "displayName": rule.display, "description": rule.description, "severity": rule.severity,
            "enabled": True, "evaluationFrequency": rule.frequency, "windowSize": rule.frequency,
            "scopes": [cfg["workspace_id"]], "autoMitigate": rule.stateful,
            "criteria": {"allOf": [{
                "query": render_query(rule, cfg), "timeAggregation": "Count", "operator": "GreaterThan",
                "threshold": 0,
                "dimensions": [{"name": d, "operator": "Include", "values": ["*"]} for d in rule.dims],
                "failingPeriods": {"numberOfEvaluationPeriods": 1, "minFailingPeriodsToAlert": 1}}]},
            "actions": {"actionGroups": [cfg["action_group_id"]]},
            **rule.extra,
        },
    }


def rule_url(rule: Rule, cfg: dict) -> str:
    return (f"https://management.azure.com/subscriptions/{cfg['subscription']}/resourceGroups/{cfg['resource_group']}"
            f"/providers/Microsoft.Insights/scheduledQueryRules/{rule.name}?api-version={API}")


REQUIRED = ("subscription", "resource_group", "location", "workspace_id", "workspace_customer_id", "identity_id",
            "action_group_id", "adx_cluster_uri")


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    missing = [k for k in REQUIRED if not cfg.get(k)]
    if missing:
        raise SystemExit(f"{path}: missing {', '.join(missing)} (see scripts/alerts.example.yaml)")
    return cfg


def _az(args: list[str]) -> str:
    done = subprocess.run(["az", *args, "-o", "json"], capture_output=True, text=True)
    if done.returncode != 0:  # show Azure's reason, not a Python traceback
        err = "\n".join(l for l in done.stderr.splitlines() if not l.startswith("WARNING"))
        raise SystemExit(f"az {args[0]} {args[1] if len(args) > 1 else ''} failed:\n{err.strip()}")
    return done.stdout


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=("list", "test", "apply", "status", "enable", "disable"))
    ap.add_argument("rules", nargs="*", help="rule names (default: all)")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--frequency", help="override the rules' frequency for this apply (e.g. PT5M); re-apply without it after")
    ap.add_argument("--config", default=os.path.join(HERE, "alerts.yaml"))
    args = ap.parse_args(argv)
    by_name = {r.name: r for r in RULES}
    unknown = [n for n in args.rules if n not in by_name]
    if unknown:
        ap.error(f"unknown rule(s): {', '.join(unknown)}. Known: {', '.join(by_name)}")
    chosen = [by_name[n] for n in args.rules] or RULES
    if args.frequency:
        if args.command != "apply":
            ap.error("--frequency only applies to `apply`")
        chosen = [replace(r, frequency=args.frequency) for r in chosen]
    for r in chosen:
        problems = validate(r)
        if problems:
            print(f"{r.name}: " + "; ".join(problems))
            return 1

    if args.command == "list":
        for r in RULES:
            print(f"{r.name:34} every {r.frequency:5} sev {r.severity} {'stateful' if r.stateful else 'stateless'}  {r.display}")
        return 0
    cfg = load_config(args.config)
    if args.command == "test":
        for r in chosen:
            print(f"== {r.name}")
            rows = json.loads(_az(["monitor", "log-analytics", "query", "-w", cfg["workspace_customer_id"],
                                   "--analytics-query", render_query(r, cfg)]))
            for row in rows:
                print("  ", {k: v for k, v in row.items() if k != "TableName"})
            print(f"   -> {len(rows)} row(s): {'would fire' if rows else 'would not fire'}")
        return 0
    if args.command == "status":
        url = (f"https://management.azure.com/subscriptions/{cfg['subscription']}/providers/Microsoft.AlertsManagement"
               "/alerts?api-version=2019-05-05-preview&timeRange=1d")
        for a in json.loads(_az(["rest", "--method", "get", "--url", url])).get("value", []):
            e = a["properties"]["essentials"]
            if "/scheduledqueryrules/finops-" in e.get("alertRule", "").lower():
                print(f"{e['alertRule'].split('/')[-1]:34} {e.get('monitorCondition'):9} {e.get('startDateTime')}")
        return 0
    if args.command in ("enable", "disable"):
        on = args.command == "enable"
        for r in chosen:  # PATCH keeps the identity (az monitor scheduled-query update does not)
            out = json.loads(_az(["rest", "--method", "patch", "--url", rule_url(r, cfg),
                                  "--body", json.dumps({"properties": {"enabled": on}})]))
            print(f"{r.name}: enabled={out['properties']['enabled']}")
        return 0
    for r in chosen:  # apply
        b = body(r, cfg)
        if not args.execute:
            print(f"== {r.name} (dry run: PUT {rule_url(r, cfg).split('?')[0]})")
            print(json.dumps(b, indent=2))
            continue
        tmp = os.path.join(HERE, f".alert-{r.name}.json")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(b, f)
        try:
            out = json.loads(_az(["rest", "--method", "put", "--url", rule_url(r, cfg), "--body", f"@{tmp}"]))
        finally:
            os.remove(tmp)
        print(f"{r.name}: enabled={out['properties']['enabled']} every={out['properties']['evaluationFrequency']}")
    if not args.execute:
        print("\nDry run. Re-run with --execute to create or update.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

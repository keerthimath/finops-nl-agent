"""Tools exposed to the model: get_reference, describe_entity, run_kql, and run_dashboard_query when configured."""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from .adx import AdxClient, AdxError, QueryResult
from .config import Settings
from .dashboard import SCOPE_COLUMNS, Dashboard, _kql_string, load_dashboard
from .guard import GuardError, validate_kql
from .logging_utils import log_event


def load_prompt(name: str, override: str | None = None) -> str:
    if override:
        return Path(override).read_text(encoding="utf-8")
    return resources.files("finops_nl").joinpath("prompts", name).read_text(encoding="utf-8")


REFERENCE_FILES = {
    "schema": "schema.md",
    "query_catalog": "query_catalog.md",
    "weekly_report": "weekly_report.md",
}
SECTIONED = ("schema", "query_catalog")  # large docs the model can fetch by section


def split_sections(doc: str) -> tuple[str, dict[str, str]]:
    """(intro, {'## heading': text}) for a markdown doc; '###' subsections stay with their parent."""
    parts = re.split(r"(?m)^## ", doc)
    sections = {}
    for p in parts[1:]:
        sections[p.split("\n", 1)[0].strip()] = "## " + p.rstrip() + "\n"
    return parts[0], sections


REFERENCE_HELP = {
    "schema": "Column reference for Costs_v1_2(): names, types, usage notes. Look up columns before writing queries.",
    "query_catalog": "Ready-to-use KQL templates: breakdowns, trends, anomaly detection, forecasting, savings, commitment utilization, reservation recommendations.",
    "weekly_report": "Step-by-step workflow and 7 queries for the weekly cost anomaly report, with post-processing and report structure.",
}


def load_references(settings: Settings) -> dict[str, str]:
    refs = {k: load_prompt(v) for k, v in REFERENCE_FILES.items()}
    if settings.schema_file:
        refs["schema"] = load_prompt("schema.md", settings.schema_file)
    return refs


def load_dashboard_queries(settings: Settings) -> Dashboard | None:
    if not settings.dashboard_file:
        return None
    dash = load_dashboard(settings.dashboard_file, settings.allowed_entities, settings.max_query_chars)
    log_event("dashboard_loaded", file=settings.dashboard_file, tiles=len(dash.tiles),
              skipped=[f"{title}: {reason[:120]}" for title, reason in dash.skipped])
    return dash


QUERY_TOOLS = ("run_kql", "run_dashboard_query")  # tools whose outcome carries KQL that ran
# Tile parameters the model may not set, by tile-id prefix. Components tiles: any maxGroupCount but the default
# makes the "(N others)" row come out short, so the total is wrong.
FIXED_TILE_PARAMS = {"components/": ("maxGroupCount",)}

# find_names: what users call things -> the exact values in the data.
def tag_expr(name: str) -> str:
    """A tag under any of the key spellings in use (keys are case-sensitive), the x_ keys first:
    tag_expr("project") reads x_project, x_Project, project, Project."""
    cap = name[:1].upper() + name[1:]
    keys = (f"x_{name}", f"x_{cap}", name, cap)
    return "coalesce(" + ", ".join(f"tostring(Tags['{k}'])" for k in keys) + ")"


NAME_KINDS = {
    "subscription": "SubAccountName", "resource_group": "x_ResourceGroupName", "resource": "ResourceName",
    "service": "ServiceName", "service_category": "ServiceCategory", "region": "RegionName",
    "meter_category": "x_SkuMeterCategory", "billing_account": "BillingAccountName", "resource_type": "ResourceType",
    "project": tag_expr("project"), "application": tag_expr("application"), "component": tag_expr("component"),
}
NAME_LOOKBACK_DAYS = 90


def find_names_kql(column: str, text: str) -> str:
    """Values of `column` matching `text` loosely (case, spaces, dashes, partial words), best and costliest first."""
    q = _kql_string(text.strip().lower())
    typed = column in ("ResourceName", "ResourceId")  # the type tells "storage" from "workspace"
    by_type = ", Type = take_any(ResourceType)" if typed else ""
    return f"""let q = {q};
let qn = replace_regex(q, @'[^a-z0-9]', '');
let words = extract_all(@'([a-z0-9]+)', q);
Costs_v1_2()
| where ChargePeriodStart >= ago({NAME_LOOKBACK_DAYS}d)
| summarize Cost = round(sum(EffectiveCost), 2){by_type} by Value = tostring({column})
| where isnotempty(Value)
| extend v = tolower(Value){", t = tolower(Type)" if typed else ""}, w = words
| extend vn = replace_regex(v, @'[^a-z0-9]', '')
| mv-apply w to typeof(string) on (summarize Hits = countif(strlen(w) >= 3 and (v contains w{" or t contains w" if typed else ""})))
| extend Match = case(v == q, 'exact', v contains q, 'contains', isnotempty(qn) and (vn contains qn or qn contains vn), 'similar', Hits > 0, 'word', '')
| extend Rank = case(Match == 'exact', 4, Match == 'contains', 3, Match == 'similar', 2, Match == 'word', 1, 0)
| where Rank > 0
| order by Rank desc, Hits desc, Cost desc
| take 10
| project Value{", Type" if typed else ""}, Match, Cost"""


def top_names_kql(column: str) -> str:
    return f"""Costs_v1_2()
| where ChargePeriodStart >= ago({NAME_LOOKBACK_DAYS}d)
| summarize Cost = round(sum(EffectiveCost), 2) by Value = tostring({column})
| where isnotempty(Value)
| top 15 by Cost desc"""

# ADX semantic error for a column the data doesn't have (dashboard tiles read tag columns made by bag_unpack).
_MISSING_COLUMN = re.compile(r"Failed to resolve [\w ]*?named '([^']+)'")


PROMPT_SUBSCRIPTIONS_MAX = 50  # up to this many go into the system prompt; more are resolved with find_names


@dataclass
class SessionProfile:
    """What the signed-in user can see (RLS applies), loaded once per conversation in one query."""
    currency: str | None = None
    subscriptions: list[tuple[str, float]] = field(default_factory=list)  # (exact SubAccountName, cost), costliest first
    complete: bool = False  # False: list truncated or not loaded, so find_names falls back to ADX


def profile_kql(days: int = 90) -> str:
    return f"""Costs_v1_2()
| where ChargePeriodStart >= ago({days}d)
| summarize Cost = round(sum(EffectiveCost), 2), Rows = count() by SubAccountName, BillingCurrency"""


async def session_profile(adx: AdxClient, user: str = "unknown") -> SessionProfile:
    """Billing currency + the user's subscriptions, so the model needs no round trips for either."""
    try:
        res = await asyncio.to_thread(adx.query, profile_kql(NAME_LOOKBACK_DAYS))
    except AdxError as exc:
        log_event("profile_error", user=user, error=str(exc)[:300])
        return SessionProfile()
    col = {c: i for i, c in enumerate(res.columns)}
    rows_by_currency: dict[str, int] = {}
    cost_by_sub: dict[str, float] = {}
    for r in res.rows:
        cur, name = r[col["BillingCurrency"]], r[col["SubAccountName"]]
        if cur:
            rows_by_currency[cur] = rows_by_currency.get(cur, 0) + int(r[col["Rows"]] or 0)
        if name:
            cost_by_sub[name] = cost_by_sub.get(name, 0.0) + float(r[col["Cost"]] or 0)
    currency = max(rows_by_currency, key=rows_by_currency.get) if rows_by_currency else None
    subs = sorted(((n, round(c, 2)) for n, c in cost_by_sub.items()), key=lambda x: -x[1])
    log_event("session_profile", user=user, currency=currency, subscriptions=len(subs), truncated=res.truncated)
    return SessionProfile(currency, subs, complete=not res.truncated)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def match_names(text: str, items: list[tuple[str, float]], limit: int = 10) -> list[list]:
    """find_names' matching in Python (same rules as the KQL): [[value, match, cost], ...] best first."""
    q, qn = text.strip().lower(), _norm(text)
    words = [w for w in re.findall(r"[a-z0-9]+", q) if len(w) >= 3]
    scored = []
    for value, cost in items:
        v, vn = value.lower(), _norm(value)
        hits = sum(1 for w in words if w in v)
        if v == q:
            match, rank = "exact", 4
        elif q and q in v:
            match, rank = "contains", 3
        elif qn and vn and (qn in vn or vn in qn):
            match, rank = "similar", 2
        elif hits:
            match, rank = "word", 1
        else:
            continue
        scored.append((rank, hits, cost, value, match))
    scored.sort(key=lambda x: (-x[0], -x[1], -x[2]))
    return [[value, match, cost] for _, _, cost, value, match in scored[:limit]]


@dataclass
class ToolOutcome:
    name: str
    content: str                      # what the model sees
    is_error: bool = False
    kql: str | None = None            # for UI display
    result: QueryResult | None = None
    preview: str = ""                 # markdown table for UI display
    tile: str | None = None           # dashboard tile id, for charts
    source: str = ""                  # where the KQL came from, for the UI ("Dashboard tile: ...")
    repeat: bool = False              # a tile run already made this turn; not run again, no UI step


def _md_table(columns: list[str], rows: list[list[Any]], limit: int = 15) -> str:
    if not columns:
        return "(no columns)"
    head = "| " + " | ".join(columns) + " |\n| " + " | ".join("---" for _ in columns) + " |\n"
    body = "\n".join("| " + " | ".join(_fmt(v) for v in r) + " |" for r in rows[:limit])
    more = f"\n\n_{len(rows) - limit} more rows not shown_" if len(rows) > limit else ""
    return head + body + more


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:,.2f}"
    return "" if v is None else str(v).replace("|", "\\|")


def format_for_model(res: QueryResult, max_rows: int, max_chars: int) -> str:
    shown = min(len(res.rows), max_rows)
    while True:
        payload = {
            "columns": res.columns,
            "rows": res.rows[:shown],
            "row_count": res.row_count,
            "rows_shown": shown,
            "truncated_by_server": res.truncated,
        }
        text = json.dumps(payload, default=str, separators=(",", ":"))
        if len(text) <= max_chars or shown <= 5:
            if shown < res.row_count:
                payload["note"] = (
                    f"Only {shown} of {res.row_count} rows shown. Aggregate or filter further if you need the rest."
                )
                text = json.dumps(payload, default=str, separators=(",", ":"))
            return text
        shown = max(5, shown // 2)


def tile_source(tile, scope=None) -> str:
    """'Dashboard tile: Storage / Egress Cost per Storage Account (scope: ResourceName = mystorageacct)'."""
    label = f"Dashboard tile: {tile.page} / {tile.title}" if tile.page else f"Dashboard tile: {tile.title}"
    parts = [f"{s['column']} = {', '.join(s['values'])}" for s in scope or [] if isinstance(s, dict) and s.get("values")]
    return label + (f" (scope: {'; '.join(parts)})" if parts else "")


class ToolRunner:
    def __init__(self, settings: Settings, adx: AdxClient, references: dict[str, str], user: str = "unknown",
                 dashboard: Dashboard | None = None, unavailable_tiles: set[str] | None = None,
                 profile: SessionProfile | None = None):
        self._s = settings
        self._adx = adx
        self._refs = references
        self._user = user
        self._dash = dashboard if dashboard and dashboard.tiles else None
        # Tiles that failed on this user's data (e.g. a missing tag column). Pass a per-session set to keep them
        # out of the tool list for the rest of the conversation.
        self._unavailable = unavailable_tiles if unavailable_tiles is not None else set()
        self._profile = profile or SessionProfile()
        self._tile_runs: set[str] = set()  # tile + parameters + scope already run by this runner (one per turn)

    @property
    def user(self) -> str:
        return self._user

    def _tiles(self) -> list:
        return [t for t in self._dash.tiles.values() if t.id not in self._unavailable] if self._dash else []

    def definitions(self) -> list[dict]:
        entities = list(self._s.allowed_entities)
        return self._core_definitions(entities) + ([self._dashboard_definition()] if self._tiles() else [])

    def _dashboard_definition(self) -> dict:
        # Compact on purpose: this is sent with every request. Tile ids already name the page and title.
        # Tile ids (page/title) are the `tile` enum; only list the ones whose description adds something.
        notes = "\n".join(f"{t.id}: {t.description}" for t in self._tiles()
                          if t.description and t.description.lower() not in t.id.replace("-", " "))
        params = ", ".join(f"{p.name}={'[] (all)' if p.is_array else p.default}" for p in self._dash.params.values())
        props = {
            p.name: ({"type": "array", "items": {"type": "string"}} if p.is_array
                     else {"type": "integer", "minimum": 1} if p.kind == "int" else {"type": "string"})
            for p in self._dash.params.values()
        }
        return {
            "name": "run_dashboard_query",
            "description": (
                f"Run a tile of the team's '{self._dash.title}' dashboard (as the user; RLS applies). "
                f"Tiles are the `tile` values (page/title). Parameters (defaults): {params}. `scope` filters cost "
                "rows before the tile's logic." + (f"\nTile notes:\n{notes}" if notes else "")
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "tile": {"type": "string", "enum": [t.id for t in self._tiles()]},
                    "parameters": {"type": "object", "properties": props, "additionalProperties": False},
                    "scope": {"type": "array", "items": {"type": "object", "properties": {
                        "column": {"type": "string", "enum": list(SCOPE_COLUMNS)},
                        "values": {"type": "array", "items": {"type": "string"}}},
                        "required": ["column", "values"]}},
                },
                "required": ["tile"],
            },
        }

    def _core_definitions(self, entities: list[str]) -> list[dict]:
        return [
            {
                "name": "find_names",
                "description": ("Exact values for a loosely typed name (any case, partial words, with or without "
                                "dashes), best match then costliest first; suggestions if nothing matches."),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": list(NAME_KINDS)},
                        "text": {"type": "string", "description": "What the user called it."},
                    },
                    "required": ["kind", "text"],
                },
            },
            {
                "name": "get_reference",
                "description": (
                    "Fetch a FinOps reference document used to BUILD queries (never to answer directly). Topics: "
                    + "; ".join(f"{k} - {REFERENCE_HELP.get(k, '')}" for k in self._refs)
                    + "".join(
                        f"\n{k} sections (pass only the ones you need in `sections`; omit for the whole document): "
                        + "; ".join(split_sections(self._refs[k])[1])
                        for k in SECTIONED if k in self._refs and split_sections(self._refs[k])[1]
                    )
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string", "enum": list(self._refs)},
                        "sections": {"type": "array", "items": {"type": "string"},
                                     "description": "Section headings to return (schema and query_catalog)."},
                    },
                    "required": ["topic"],
                },
            },
            {
                "name": "describe_entity",
                "description": "Return the live column names and types of an entity (runs getschema).",
                "input_schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "enum": entities}},
                    "required": ["name"],
                },
            },
            {
                "name": "run_kql",
                "description": (
                    "Run a read-only KQL query against the FinOps Hub. Results are limited to what the "
                    "signed-in user is allowed to see. Aggregate in KQL; do not pull raw rows."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "kql": {"type": "string", "description": "The KQL query."},
                    },
                    "required": ["kql"],
                },
            },
        ]

    async def run(self, name: str, args: dict) -> ToolOutcome:
        if name == "get_reference":
            topic = args.get("topic", "")
            if topic not in self._refs:
                return ToolOutcome(name, f"Unknown topic. Available: {', '.join(self._refs)}", True)
            wanted = [str(s).strip().removeprefix("## ").lower() for s in args.get("sections") or []]
            _, sections = split_sections(self._refs[topic])
            if not wanted or topic not in SECTIONED or not sections:
                return ToolOutcome(name, self._refs[topic])
            by_name = {k.lower(): v for k, v in sections.items()}
            unknown = [w for w in wanted if w not in by_name]
            if unknown:
                return ToolOutcome(name, f"Unknown section(s): {', '.join(unknown)}. Available: "
                                   + "; ".join(sections), True)
            return ToolOutcome(name, "\n".join(by_name[w] for w in dict.fromkeys(wanted)))
        if name == "describe_entity":
            ent = args.get("name", "")
            if ent not in self._s.allowed_entities:
                return ToolOutcome(name, f"Unknown entity. Allowed: {', '.join(self._s.allowed_entities)}", True)
            out = await self._execute(name, f"{ent} | getschema | project ColumnName, ColumnType", validate=False)
            if not out.is_error and out.result:  # compact: every column, not a row-capped JSON table
                out.content = ", ".join(f"{r[0]}:{r[1]}" for r in out.result.rows)
            return out
        if name == "run_kql":
            return await self._execute(name, str(args.get("kql", "")), validate=True)
        if name == "find_names":
            return await self._find_names(name, args)
        if name == "run_dashboard_query" and self._dash:
            return await self._run_tile(name, args)
        return ToolOutcome(name, f"Unknown tool '{name}'.", True)

    async def _find_names(self, name: str, args: dict) -> ToolOutcome:
        kind, text = str(args.get("kind", "")), str(args.get("text", "")).strip()
        if kind not in NAME_KINDS:
            return ToolOutcome(name, f"Unknown kind. Use one of: {', '.join(NAME_KINDS)}.", True)
        if not text or len(text) > 100 or any(c < " " for c in text):
            return ToolOutcome(name, "text must be a short single-line name.", True)
        column = NAME_KINDS[kind]
        if kind == "subscription" and self._profile.subscriptions:  # in memory: no query
            rows = match_names(text, self._profile.subscriptions)
            if rows or self._profile.complete:
                return self._local_names(name, kind, text, column, rows)
        out = await self._execute(name, find_names_kql(column, text), validate=True)
        if out.is_error:
            return out
        if out.result is not None and out.result.row_count:
            out.content = (f"Matches for {kind} {text!r} ({column}); use these exact values in filters "
                           f"(e.g. {column} in~ (...)) and in dashboard scope:\n" + out.content)
            return out
        top = await self._execute(name, top_names_kql(column), validate=True)
        if not top.is_error:
            top.content = (f"No {kind} matches {text!r}. The costliest {column} values are below; if one is "
                           "clearly meant, use it and say so, otherwise ask the user which one:\n" + top.content)
        return top

    def _local_names(self, name: str, kind: str, text: str, column: str, rows: list[list]) -> ToolOutcome:
        if rows:
            res = QueryResult(["Value", "Match", "Cost"], rows, len(rows), False)
            head = (f"Matches for {kind} {text!r} ({column}); use these exact values in filters "
                    f"(e.g. {column} in~ (...)) and in dashboard scope:\n")
        else:
            top = [[n, c] for n, c in self._profile.subscriptions[:15]]
            res = QueryResult(["Value", "Cost"], top, len(top), False)
            head = (f"No {kind} matches {text!r}. The user's costliest {column} values are below; if one is "
                    "clearly meant, use it and say so, otherwise ask the user which one:\n")
        log_event("find_names_local", user=self._user, kind=kind, matches=len(rows))
        return ToolOutcome(name, head + format_for_model(res, self._s.llm_rows, self._s.max_result_chars), False,
                           result=res)

    async def _run_tile(self, name: str, args: dict) -> ToolOutcome:
        tile = str(args.get("tile", ""))
        if tile not in self._dash.tiles or tile in self._unavailable:
            return ToolOutcome(name, "Unknown tile. Pick one from the tool description.", True)
        source = tile_source(self._dash.tiles[tile], args.get("scope"))  # label the step even if the call fails
        params = dict(args.get("parameters") or {})
        if not isinstance(args.get("parameters") or {}, dict):
            params = {}
        # Parameters this tile doesn't read are dropped (and reported), not rejected: a rejection costs a model round.
        ignored = sorted(k for k in params if k not in self._dash.tiles[tile].params)
        for k in ignored:
            params.pop(k)
        fixed = sorted(k for prefix, keys in FIXED_TILE_PARAMS.items() if tile.startswith(prefix)
                       for k in keys if k in params)
        for k in fixed:
            params.pop(k)
        # The model sometimes re-runs a tile it already has; the second run adds nothing and costs a round.
        run_key = json.dumps([tile, params, args.get("scope") or []], sort_keys=True, default=str)
        if run_key in self._tile_runs:
            log_event("tile_repeat", user=self._user, tile=tile)
            return ToolOutcome(name, "You already ran this tile with these parameters and scope in this answer. "
                               "Its result is above: answer from it. Do not run it again.", tile=tile, repeat=True)
        try:
            kql = self._dash.query(tile, params, args.get("scope") or None)
        except ValueError as exc:
            return ToolOutcome(name, f"Invalid parameters: {exc}", True, tile=tile, source=source)
        out = await self._execute(name, kql, validate=True)
        out.tile = tile
        out.source = source
        if not out.is_error:
            self._tile_runs.add(run_key)
        if fixed and not out.is_error:
            out.content = f"(Used the tile's default {', '.join(fixed)}; it is fixed for this tile.)\n" + out.content
        if ignored and not out.is_error:
            out.content = (f"(Ignored parameters this tile doesn't use: {', '.join(ignored)}. It uses: "
                           f"{', '.join(self._dash.tiles[tile].params) or 'none'}.)\n" + out.content)
        missing = _MISSING_COLUMN.search(out.content) if out.is_error else None
        if missing:
            col = missing.group(1)
            self._unavailable.add(tile)
            log_event("tile_unavailable", user=self._user, tile=tile, column=col)
            out.content = (
                f"This dashboard tile can't run on this data: it needs '{col}', which the cost data does not have "
                f"(the dashboard derives such columns from resource tags, e.g. a '{col}' tag). Do not retry it, do "
                f"not try other tiles or KQL that need '{col}', and do not substitute your own definition. Tell the "
                f"user the data has no '{col}' information and what the dashboard would need (that tag on resources)."
            )
        return out

    async def _execute(self, name: str, kql: str, *, validate: bool) -> ToolOutcome:
        try:
            if validate:
                kql = validate_kql(
                    kql,
                    allowed_entities=self._s.allowed_entities,
                    max_chars=self._s.max_query_chars,
                )
        except GuardError as exc:
            log_event("kql_rejected", user=self._user, reason=str(exc), kql=kql[:2000])
            return ToolOutcome(name, f"Query rejected: {exc}", True, kql=kql)
        try:
            res = await asyncio.to_thread(self._adx.query, kql)
        except AdxError as exc:
            log_event("kql_error", user=self._user, error=str(exc)[:500], kql=kql[:2000])
            return ToolOutcome(name, f"ADX error: {exc}", True, kql=kql)
        log_event(
            "kql_ok", user=self._user, rows=res.row_count, ms=res.elapsed_ms,
            truncated=res.truncated, request_id=res.request_id, kql=kql[:2000],
        )
        return ToolOutcome(
            name,
            format_for_model(res, self._s.llm_rows, self._s.max_result_chars),
            False,
            kql=kql,
            result=res,
            preview=_md_table(res.columns, res.rows),
        )

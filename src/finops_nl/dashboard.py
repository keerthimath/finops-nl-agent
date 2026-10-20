"""Saved queries from an Azure Data Explorer dashboard export, runnable as standalone KQL.

A tile's query depends on the dashboard's base queries (let-bound, possibly built on each other) and its
parameters. Each tile is resolved into one query: parameters as leading `let` statements (so the caller can
override them), then the base queries it needs in dependency order, then the tile's own query.

Tiles whose query the guard rejects are skipped with a reason, e.g. ones reading system tables or another
database. The guard is not loosened for dashboards: they run through it like any model-written query.

    python -m finops_nl.dashboard <export.json>     # list usable and skipped tiles
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .guard import GuardError, mask, validate_kql

# Parameters whose dashboard default comes from a query (e.g. hub retention for numberOfMonths) get this.
DEFAULT_INT = 3
MAX_INT = 366
MAX_VALUES = 50
MAX_VALUE_CHARS = 200

# Applied to base queries before the guard check: (pattern, replacement, column the rewrite blanks out).
# A tile that reads the blanked column is skipped rather than given wrong data.
REWRITES = [
    # FinOps hub CostsPlus derives x_ResourceParent* with the Ingestion database's parse_resourceid; the guard
    # forbids cross-database calls.
    (re.compile(r"database\('Ingestion'\)\.parse_resourceid\([^()]*\)"), "dynamic(null)", "x_ResourceParent"),
]


@dataclass(frozen=True)
class Param:
    name: str
    label: str
    kind: str          # "int" | "string"
    is_array: bool
    default: Any

    def literal(self, value: Any) -> str:
        if self.kind == "int":
            return str(value)
        if self.is_array:
            # "All" must be dynamic(null): tiles test isempty(param), and isempty(dynamic([])) is false in ADX.
            return "dynamic([" + ", ".join(_kql_string(v) for v in value) + "])" if value else "dynamic(null)"
        return _kql_string(value)

    def coerce(self, value: Any) -> Any:
        """Validate a caller-supplied value; ValueError says what is wrong."""
        if self.kind == "int":
            if isinstance(value, str) and value.strip().isdigit():
                value = int(value)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_INT:
                raise ValueError(f"{self.name} must be an integer from 1 to {MAX_INT}.")
            return value
        values = value if self.is_array else [value]
        if self.is_array and not isinstance(value, list):
            raise ValueError(f"{self.name} must be a list of strings ([] means all).")
        if len(values) > MAX_VALUES:
            raise ValueError(f"{self.name} accepts at most {MAX_VALUES} values.")
        for v in values:
            if not isinstance(v, str) or len(v) > MAX_VALUE_CHARS or any(c < " " for c in v):
                raise ValueError(f"{self.name} values must be single-line strings up to {MAX_VALUE_CHARS} chars.")
        return value


@dataclass(frozen=True)
class Tile:
    id: str
    page: str
    title: str
    visual: str
    description: str
    body: str                  # base queries + tile query, without the parameter lets
    params: tuple[str, ...]    # parameters it reads, directly or through base queries


@dataclass
class Dashboard:
    title: str
    params: dict[str, Param]
    tiles: dict[str, Tile]
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (tile title, reason)

    def query(self, tile_id: str, overrides: dict | None = None, scope: list | None = None) -> str:
        """Standalone KQL for a tile. KeyError for an unknown tile, ValueError for bad parameters or scope.

        scope narrows the tile to some cost rows (e.g. one storage account) *before* its own logic runs, so the tile's
        definitions (tags, egress rules, grouping) still apply: [{"column": "ResourceName", "values": ["..."]}, ...].
        """
        tile = self.tiles[tile_id]
        overrides = overrides or {}
        unknown = sorted(set(overrides) - set(tile.params))
        if unknown:
            raise ValueError(f"{tile_id} does not use {', '.join(unknown)}. It uses: {', '.join(tile.params) or 'none'}.")
        lets = []
        for name in tile.params:
            p = self.params[name]
            value = p.coerce(overrides[name]) if name in overrides else p.default
            lets.append(f"let {name} = {p.literal(value)};\n")
        body = tile.body
        if scope:
            predicate = scope_predicate(scope)
            body, n = _COSTS_REF.subn(f"(Costs_v1_2() | where {predicate})", body)
            if not n:
                raise ValueError(f"{tile_id} does not read Costs_v1_2, so it can't be scoped.")
        return "".join(lets) + body


# Columns a tile can be scoped by (cost-row columns users name in questions).
SCOPE_COLUMNS = ("SubAccountName", "SubAccountId", "x_ResourceGroupName", "ResourceName", "ResourceId", "ResourceType",
                 "ServiceName", "ServiceCategory", "RegionName", "x_SkuMeterCategory", "BillingAccountName")
MAX_SCOPE_VALUES = 20
_COSTS_REF = re.compile(r"\bCosts_v1_2\b(?:\s*\(\s*\))?")


def scope_predicate(scope: list) -> str:
    """[{"column": c, "values": [...]}, ...] -> `c in~ ("v1", "v2") and ...` (values escaped; columns allow-listed)."""
    if not isinstance(scope, list) or not scope:
        raise ValueError("scope must be a non-empty list of {column, values}.")
    parts = []
    for item in scope:
        col = item.get("column") if isinstance(item, dict) else None
        values = item.get("values") if isinstance(item, dict) else None
        if col not in SCOPE_COLUMNS:
            raise ValueError(f"scope column must be one of: {', '.join(SCOPE_COLUMNS)}.")
        if not isinstance(values, list) or not values or len(values) > MAX_SCOPE_VALUES:
            raise ValueError(f"scope values for {col}: a list of 1-{MAX_SCOPE_VALUES} exact names (use find_names).")
        if any(not isinstance(v, str) or not v or len(v) > MAX_VALUE_CHARS or any(c < " " for c in v) for v in values):
            raise ValueError(f"scope values for {col} must be single-line strings up to {MAX_VALUE_CHARS} chars.")
        parts.append(f"{col} in~ ({', '.join(_kql_string(v) for v in values)})")
    return " and ".join(parts)


def exclude_path(dashboard_path: str) -> str:
    """Tiles that fail on this hub, written by scripts/check_queries.py --write-exclude: {tile_id: reason}."""
    return str(Path(dashboard_path).with_suffix("")) + ".exclude.json"


def load_dashboard(path: str, allowed_entities: tuple[str, ...] = (), max_chars: int = 8_000,
                   use_exclusions: bool = True) -> Dashboard:
    dash = parse_dashboard(json.loads(Path(path).read_text(encoding="utf-8")), allowed_entities, max_chars)
    excl = Path(exclude_path(path))
    if use_exclusions and excl.exists():
        for tile_id, reason in json.loads(excl.read_text(encoding="utf-8")).items():
            if dash.tiles.pop(tile_id, None):
                dash.skipped.append((tile_id, f"Excluded: fails on this hub ({reason[:160]})"))
        dash.params = {n: p for n, p in dash.params.items() if any(n in t.params for t in dash.tiles.values())}
    return dash


def parse_dashboard(d: dict, allowed_entities: tuple[str, ...] = (), max_chars: int = 8_000) -> Dashboard:
    queries = {q["id"]: q.get("text", "") for q in d.get("queries", [])}
    base: dict[str, str] = {}
    rewritten: set[str] = set()   # base queries a REWRITES entry changed
    for b in d.get("baseQueries", []):
        name, text = b["variableName"], queries.get(b.get("queryId"), "")
        for pattern, replacement, _ in REWRITES:
            text, n = pattern.subn(replacement, text)
            if n:
                rewritten.add(name)
        base[name] = text
    params = {p["variableName"]: _param(p) for p in d.get("parameters", [])}
    pages = {p["id"]: p.get("name", "") for p in d.get("pages", [])}

    dash = Dashboard(d.get("title", ""), {n: p for n, p in params.items() if p}, {})
    for t in d.get("tiles", []):
        query_id = (t.get("queryRef") or {}).get("queryId")
        if not query_id:
            continue  # markdown and other tiles without a query
        title = t.get("title") or "(untitled)"
        page = pages.get(t.get("pageId"), "")
        try:
            body, used = _assemble(queries.get(query_id, ""), base, rewritten, params)
        except ValueError as exc:
            dash.skipped.append((title, str(exc)))
            continue
        tile = Tile(_unique_id(dash.tiles, page, title, t.get("visualType", "")), page, title,
                    t.get("visualType", ""), (t.get("description") or "").strip(), body, used)
        dash.tiles[tile.id] = tile
        try:
            validate_kql(dash.query(tile.id), allowed_entities=allowed_entities, max_chars=max_chars)
        except GuardError as exc:
            del dash.tiles[tile.id]
            dash.skipped.append((title, f"Rejected by the guard: {exc}"))
    dash.params = {n: p for n, p in dash.params.items() if any(n in t.params for t in dash.tiles.values())}
    return dash


def _param(p: dict) -> Param | None:
    kind = {"int": "int", "long": "int", "string": "string"}.get(p.get("kind", ""))
    if kind is None:
        return None
    is_array = p.get("selectionType") == "array"
    dv = p.get("defaultValue") or {}
    if dv.get("kind") == "value":
        default = int(dv["value"]) if kind == "int" else (str(dv["value"]) if not is_array else [str(dv["value"])])
    elif dv.get("kind") == "values":
        default = [str(v) for v in dv.get("values", [])] if is_array else str((dv.get("values") or [""])[0])
    else:  # "all" (dashboards pass an empty array) or computed by a query
        default = DEFAULT_INT if kind == "int" else ([] if is_array else "")
    return Param(p["variableName"], p.get("displayName") or p["variableName"], kind, is_array, default)


def _refs(text: str, names) -> list[str]:
    masked = mask(text)
    return [n for n in names if re.search(rf"\b{re.escape(n)}\b", masked)]


def _assemble(text: str, base: dict[str, str], rewritten: set[str],
              params: dict[str, Param | None]) -> tuple[str, tuple[str, ...]]:
    order: list[str] = []

    def visit(src: str, stack: tuple[str, ...]) -> None:
        for name in _refs(src, base):
            if name in stack:
                raise ValueError(f"Base queries reference each other in a cycle: {' -> '.join(stack + (name,))}.")
            if name not in order:
                visit(base[name], stack + (name,))
                order.append(name)

    visit(text, ())
    readers = [text] + [base[n] for n in order if n not in rewritten]
    for _, _, column in REWRITES:
        if any(re.search(rf"\b{re.escape(column)}\w*", mask(src)) for src in readers):  # a column prefix
            raise ValueError(f"Reads {column}*, which needs a call the guard does not allow.")
    body = "".join(_let(n, base[n]) for n in order) + text.strip()
    used = _refs(body, params)
    unsupported = [n for n in used if params[n] is None]
    if unsupported:
        raise ValueError(f"Uses parameters of an unsupported kind: {', '.join(unsupported)}.")
    return body, tuple(used)


def _let(name: str, body: str) -> str:
    body = body.strip().rstrip(";").strip()
    if ";" in mask(body):  # a base query with its own let statements
        return f"let {name} = view () {{\n{body}\n}};\n"
    return f"let {name} = {body}\n;\n"  # ';' on its own line: the body may end in a // comment


def _unique_id(existing: dict, page: str, title: str, visual: str) -> str:
    base = f"{_slug(page)}/{_slug(title)}".strip("/")
    for candidate in (base, f"{base}-{_slug(visual)}"):
        if candidate not in existing:
            return candidate
    i = 2
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _kql_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


if __name__ == "__main__":
    from .config import Settings

    s = Settings.from_env({**__import__("os").environ, "ADX_CLUSTER_URI": "x"})
    dash = load_dashboard(sys.argv[1], s.allowed_entities, s.max_query_chars)
    print(f"{dash.title}: {len(dash.tiles)} usable tiles, {len(dash.skipped)} skipped\n")
    for t in dash.tiles.values():
        print(f"  {t.id}  [{t.visual}]  params: {', '.join(t.params)}")
    for title, reason in dash.skipped:
        print(f"  SKIPPED {title}: {reason}")

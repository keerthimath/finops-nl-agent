"""Admin page: per-question performance (rounds, queries, time, tokens, cost) for reviewing the app.

Records are the `agent_turn` events the agent logs. Deployed, they are read from the Container Apps Log
Analytics workspace (ADMIN_LOG_WORKSPACE_ID; the app's identity needs Log Analytics Reader), so history survives
restarts and costs nothing extra. Locally, the page shows this process's recent turns.

app.py serves it at /admin for users in ADMIN_USERS (AUTH_MODE=dev: anyone, it's local).
"""
from __future__ import annotations

import datetime as dt
import html
import json
import statistics
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .logging_utils import RECENT_TURNS

MAX_RECORDS = 2000

_LA_QUERY = """union isfuzzy=true
    (ContainerAppConsoleLogs_CL | project TimeGenerated, App=ContainerAppName_s, Rev=RevisionName_s, Log=Log_s),
    (ContainerAppConsoleLogs | project TimeGenerated, App=ContainerAppName, Rev=RevisionName, Log)
| where App == '{app}' and Log has '"event": "agent_turn"'
| order by TimeGenerated desc
| take {limit}"""


def load_turns(settings: Settings, days: int, credential: Any = None) -> tuple[list[dict], str]:
    """(records newest first, where they came from)."""
    if settings.admin_log_workspace_id and settings.container_app_name:
        return _from_log_analytics(settings, days, credential), "Log Analytics"
    cutoff = _now().timestamp() - days * 86400
    return sorted((r for r in RECENT_TURNS if r["ts"] >= cutoff), key=lambda r: -r["ts"]), "this process (local)"


def _from_log_analytics(settings: Settings, days: int, credential: Any) -> list[dict]:
    from azure.identity import DefaultAzureCredential
    from azure.monitor.query import LogsQueryClient

    app = settings.container_app_name.replace("'", "")
    client = LogsQueryClient(credential or DefaultAzureCredential())
    resp = client.query_workspace(settings.admin_log_workspace_id, _LA_QUERY.format(app=app, limit=MAX_RECORDS),
                                  timespan=dt.timedelta(days=days))
    out = []
    for table in getattr(resp, "tables", None) or getattr(resp, "partial_data", None) or []:
        cols = [c if isinstance(c, str) else c.name for c in table.columns]
        for row in table.rows:
            rec = parse_record(dict(zip(cols, row)).get("Log"))
            if rec:
                rec.setdefault("revision", dict(zip(cols, row)).get("Rev"))
                out.append(rec)
    return out


def parse_record(log_line: str | None) -> dict | None:
    try:
        rec = json.loads(log_line or "")
    except (TypeError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("event") == "agent_turn" else None


@dataclass(frozen=True)
class Prices:
    """USD per million tokens; all zero = cost not shown."""
    input: float = 0.0
    cached: float = 0.0
    output: float = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.input or self.cached or self.output)

    def cost(self, r: dict) -> float:
        cached = r.get("cached_tokens") or 0
        uncached = max((r.get("input_tokens") or 0) - cached, 0)
        return (uncached * self.input + cached * self.cached + (r.get("output_tokens") or 0) * self.output) / 1e6


def filter_turns(turns: list[dict], user: str = "", problems: bool = False) -> list[dict]:
    user = user.strip().lower()
    out = [r for r in turns if not user or user in str(r.get("user", "")).lower()]
    return [r for r in out if _is_problem(r)] if problems else out


def _failed(r: dict) -> int:
    return sum(1 for q in r.get("query_details") or [] if not q.get("ok"))


def _is_problem(r: dict) -> bool:
    return bool(r.get("hit_limit")) or _failed(r) > 0


def summarize(turns: list[dict], prices: Prices) -> dict:
    if not turns:
        return {}
    secs = sorted((r.get("ms") or 0) / 1000 for r in turns if r.get("ms") is not None)
    llm = sorted((r.get("llm_ms") or 0) / 1000 for r in turns if r.get("llm_ms") is not None)
    detailed = [r for r in turns if "query_details" in r]
    n_queries = sum(len(r["query_details"]) for r in detailed)
    tokens_in = sum(r.get("input_tokens") or 0 for r in turns)
    return {
        "questions": len(turns),
        "median_s": statistics.median(secs) if secs else None,
        "p90_s": secs[min(len(secs) - 1, int(0.9 * len(secs)))] if secs else None,
        "median_llm_s": statistics.median(llm) if llm else None,
        "avg_rounds": statistics.mean(r.get("iterations") or 0 for r in turns),
        "hit_limit": sum(1 for r in turns if r.get("hit_limit")),
        "failed_pct": 100 * sum(_failed(r) for r in detailed) / n_queries if n_queries else None,
        "tokens": tokens_in + sum(r.get("output_tokens") or 0 for r in turns),
        "cached_pct": 100 * sum(r.get("cached_tokens") or 0 for r in turns) / tokens_in if tokens_in else None,
        "cost": sum(prices.cost(r) for r in turns) if prices.enabled else None,
    }


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def _k(n: Any) -> str:
    if n is None:
        return "–"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _fmt_s(v: float | None) -> str:
    return "–" if v is None else f"{v:.1f} s"


def _query_rows(r: dict) -> str:
    rows = []
    for q in r.get("query_details") or []:
        what = q.get("tile") or q.get("kql") or ""
        if q.get("params"):
            what += "  " + json.dumps(q["params"], separators=(",", ":"))
        status = '<span class="ok">ok</span>' if q.get("ok") else '<span class="bad">failed</span>'
        rows.append(
            f"<tr><td>{_e(q.get('tool'))}</td><td class='kql'><pre>{_e(what)}</pre></td><td>{status}</td>"
            f"<td class='num'>{_e(q.get('rows', ''))}</td><td class='num'>{_e(q.get('ms', ''))}</td>"
            f"<td class='err'>{_e(q.get('error', ''))}</td></tr>"
        )
    other = r.get("other_tools") or {}
    extra = ("<p class='muted'>Other tools: " + _e(", ".join(f"{k} ×{v}" for k, v in other.items())) + "</p>"
             if other else "")
    if not rows:
        return extra or "<p class='muted'>No queries recorded for this question.</p>"
    return ("<table class='inner'><thead><tr><th>Tool</th><th>Tile / KQL</th><th>Result</th><th>Rows</th>"
            "<th>ms</th><th>Error</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>" + extra)


# Chainlit serves public/favicon.svg at /favicon; every page the app renders itself links it (the chat UI already does).
FAVICON = '<link rel="icon" href="/favicon" type="image/svg+xml">'


def message_page(title: str, body_html: str) -> str:
    """A small page for sign-in / access messages, with the favicon like the other pages."""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{_e(title)}</title>{FAVICON}'
            f'</head><body style="font-family:system-ui,sans-serif;margin:2rem">{body_html}</body></html>')


def render_page(turns: list[dict], *, days: int, user: str, problems: bool, source: str, prices: Prices,
                error: str = "") -> str:
    s = summarize(turns, prices)
    cards = [
        ("Questions", _e(s.get("questions", 0))),
        ("Median time", _fmt_s(s.get("median_s"))),
        ("p90 time", _fmt_s(s.get("p90_s"))),
        ("Median model time", _fmt_s(s.get("median_llm_s"))),
        ("Avg rounds", f"{s['avg_rounds']:.1f}" if s else "–"),
        ("Hit limit", _e(s.get("hit_limit", 0))),
        ("Failed queries", f"{s['failed_pct']:.0f}%" if s.get("failed_pct") is not None else "–"),
        ("Tokens", _k(s.get("tokens"))),
        ("Cached input", f"{s['cached_pct']:.0f}%" if s.get("cached_pct") is not None else "–"),
    ]
    if prices.enabled:
        cards.append(("Est. cost", f"${s['cost']:.2f}" if s.get("cost") is not None else "–"))
    card_html = "".join(f"<div class='card'><div class='label'>{k}</div><div class='value'>{v}</div></div>"
                        for k, v in cards)

    body = []
    ncols = 9 if prices.enabled else 8
    for i, r in enumerate(turns):
        when = dt.datetime.fromtimestamp(r.get("ts", 0), dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
        n_q, failed = r.get("queries", 0), _failed(r)
        flags = []
        if r.get("hit_limit"):
            flags.append("<span class='flag bad'>hit limit</span>")
        if failed:
            flags.append(f"<span class='flag warn'>{failed} failed</span>")
        question = r.get("question") or "<span class='muted'>(not recorded)</span>"
        cost = f"<td class='num'>${prices.cost(r):.3f}</td>" if prices.enabled else ""
        body.append(
            "<tr class='turn'>"
            f"<td class='nowrap'>{when}</td><td>{_e(r.get('user', '–'))}</td>"
            f"<td class='q'><button type='button' class='toggle' aria-expanded='false' aria-controls='d{i}'>"
            f"<span class='arrow'>&#9656;</span> {question if not r.get('question') else _e(question)}</button></td>"
            f"<td class='num'>{_e(r.get('iterations'))}</td>"
            f"<td class='num'>{_e(n_q)}{f' ({failed})' if failed else ''}</td>"
            f"<td class='num nowrap'>{_fmt_s((r.get('ms') or 0) / 1000 if 'ms' in r else None)}"
            f" <span class='muted'>({_fmt_s((r.get('llm_ms') or 0) / 1000 if 'llm_ms' in r else None)})</span></td>"
            f"<td class='num nowrap'>{_k(r.get('input_tokens'))} / {_k(r.get('cached_tokens'))} / "
            f"{_k(r.get('output_tokens'))} <span class='muted'>({_k(r.get('reasoning_tokens'))})</span></td>"
            f"{cost}<td>{''.join(flags)}</td></tr>"
            # details span the whole table width, so long KQL isn't squeezed into the question column
            f"<tr class='detail' id='d{i}' hidden><td colspan='{ncols}'>{_query_rows(r)}"
            f"<p class='muted'>Model {_e(r.get('model', '–'))} · revision {_e(r.get('revision', '–'))} · "
            f"answer {_e(r.get('answer_chars', '–'))} chars</p></td></tr>"
        )
    cost_head = "<th>Cost</th>" if prices.enabled else ""
    table = ("<div class='scroll'><table class='turns'><thead><tr><th>Time (UTC)</th><th>User</th>"
             "<th>Question <span class='muted'>(click for queries)</span></th><th>Rounds</th>"
             "<th>Queries (failed)</th><th>Time (model)</th><th>Tokens in / cached / out (reasoning)</th>"
             f"{cost_head}<th>Flags</th></tr></thead><tbody>{''.join(body)}</tbody></table></div>"
             if turns else "<p class='muted empty'>No questions in this period.</p>")
    day_opts = "".join(f"<option value='{d}'{' selected' if d == days else ''}>Last {d} day{'s' if d > 1 else ''}"
                       "</option>" for d in (1, 7, 30))
    err = f"<p class='error'>{_e(error)}</p>" if error else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>FinOps assistant · Admin</title>
{FAVICON}<style>{_CSS}</style></head><body><main>
<header><div><h1>Question performance</h1><p class="muted">Source: {_e(source)} · times in UTC ·
tokens as reported by the model provider</p></div><a class="back" href="/">&larr; Back to chat</a></header>
<form method="get" class="filters">
<label>Period <select name="days">{day_opts}</select></label>
<label>User <input name="user" value="{_e(user)}" placeholder="any"></label>
<label class="check"><input type="checkbox" name="problems" value="1"{' checked' if problems else ''}>
Only problems (failed queries or hit limit)</label>
<button type="submit">Apply</button></form>
{err}<section class="cards">{card_html}</section>{table}
<script>
document.querySelectorAll("button.toggle").forEach(function (b) {{
  b.addEventListener("click", function () {{
    var row = document.getElementById(b.getAttribute("aria-controls"));
    var open = b.getAttribute("aria-expanded") === "true";
    row.hidden = open;
    b.setAttribute("aria-expanded", String(!open));
  }});
}});
</script>
</main></body></html>"""


_CSS = """
:root{--bg:#f7f7f5;--panel:#fff;--text:#1d1d1b;--muted:#6b6b66;--line:#e3e2dc;--accent:#2f5d8a;
--ok:#1f7a4d;--bad:#b42318;--warn:#9a6700;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--panel:#1f1f1d;--text:#ecebe6;--muted:#a3a29b;
--line:#33332f;--accent:#7fb0e0;--ok:#5cc28f;--bad:#f2867a;--warn:#e0b252;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1400px;margin:0 auto;padding:24px 16px 48px}h1{font-size:22px;margin:0 0 4px}
.muted{color:var(--muted)}header{margin-bottom:16px;display:flex;justify-content:space-between;
align-items:flex-start;gap:12px;flex-wrap:wrap}
.back{padding:7px 14px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--text);
text-decoration:none;white-space:nowrap}.back:hover{border-color:var(--accent)}
.filters{display:flex;flex-wrap:wrap;gap:12px 20px;align-items:center;margin-bottom:16px}
.filters label{display:flex;gap:6px;align-items:center}
select,input[name=user]{padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--panel);
color:var(--text)}
button{padding:7px 14px;border:0;border-radius:6px;background:var(--accent);color:#fff;cursor:pointer}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;margin-bottom:18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.label{font-size:12px;color:var(--muted)}.value{font-size:20px;font-weight:600;font-variant-numeric:tabular-nums}
.scroll{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%}th,td{padding:8px 10px;border-bottom:1px solid var(--line);
text-align:left;vertical-align:top}th{font-size:12px;color:var(--muted);font-weight:600;white-space:nowrap}
.num{text-align:right;font-variant-numeric:tabular-nums}.nowrap{white-space:nowrap}
td.q{min-width:240px;max-width:520px}
button.toggle{all:unset;cursor:pointer;display:inline}button.toggle:hover{color:var(--accent)}
button.toggle:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:3px}
.arrow{display:inline-block;transition:transform .15s;color:var(--muted)}
button.toggle[aria-expanded="true"] .arrow{transform:rotate(90deg)}
tr.turn:has(+ tr.detail:not([hidden])) td{border-bottom:0}
tr.detail>td{background:var(--bg);padding:4px 16px 14px}
td.kql{width:100%;min-width:0}
td.kql pre{margin:0;max-height:260px;overflow:auto;white-space:pre-wrap;word-break:break-word;
font:12px/1.45 ui-monospace,Menlo,Consolas,monospace;background:var(--panel);border:1px solid var(--line);
border-radius:6px;padding:8px 10px}
table.inner{margin:4px 0;font-size:13px;width:100%}table.inner td,table.inner th{padding:4px 6px}
.code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;word-break:break-all;max-width:420px}
.err{color:var(--bad);font-size:12px;max-width:360px}.ok{color:var(--ok)}.bad{color:var(--bad)}
.flag{display:inline-block;font-size:12px;padding:1px 7px;border-radius:10px;border:1px solid currentColor;
margin:0 4px 4px 0;white-space:nowrap}.flag.bad{color:var(--bad)}.flag.warn{color:var(--warn)}
.error{color:var(--bad)}.empty{padding:24px;text-align:center}
"""

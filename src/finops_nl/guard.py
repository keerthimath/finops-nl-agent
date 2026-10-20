"""Static checks on LLM-written KQL before it reaches ADX.

This is defense in depth, NOT the security boundary. The boundary is ADX itself:
the user's own identity, Database Viewer role, and row-level security. The guard
exists to (a) fail fast with a message the model can learn from, and (b) block
operators that could reach outside the FinOps data or change request limits.
"""
from __future__ import annotations

import re


class GuardError(ValueError):
    """Raised when a query is rejected. The message is shown to the model."""


_BLOCKED_STATEMENTS = {"set", "restrict", "pattern", "declare"}
_ALLOWED_PLUGINS = {"pivot", "bag_unpack", "narrow"}

_DENY_FUNCS = re.compile(
    r"\b(externaldata|http_request|http_request_post|sql_request|cosmosdb_sql_request|"
    r"mysql_request|postgresql_request|external_table|externaltable|ingest|"
    r"ai_embed_text|ai_chat_completion)\b"
)
_CROSS_SCOPE = re.compile(r"\b(cluster|database)\s*\(")
_SEARCH_FIND = re.compile(r"(^|\|)\s*(search|find)\b")
_UNION_STAR = re.compile(r"\bunion\b(?:\s+(?:kind|withsource|isfuzzy)\s*=\s*\w+)*\s*\*")
# KQL timespans have d/h/m/s units only: "1mo", "1y", "2w" fail in ADX after a round trip. Catch them here with the fix.
_BAD_TIMESPAN = re.compile(r"\b\d+(?:\.\d+)?\s*(mo|months?|y|yrs?|years?|w|wks?|weeks?)\b", re.I)
_EVALUATE = re.compile(r"\bevaluate\s+(?:\w+\s*=\s*\w+\s+)*([A-Za-z_]\w*)")


def mask(query: str, keep_length: bool = False) -> str:
    """Remove comments and blank out string literals so keyword checks can't be fooled.

    keep_length: replace them with spaces instead, so positions in the result match the query.
    """
    out: list[str] = []
    i, n = 0, len(query)
    while i < n:
        c = query[i]
        if query.startswith("//", i):
            j = query.find("\n", i)
            end = n if j == -1 else j
            if keep_length:
                out.append(" " * (end - i))
            i = end
            continue
        if query.startswith("```", i):
            j = query.find("```", i + 3)
            if j == -1:
                raise GuardError("Unterminated multi-line string literal.")
            out.append(" " * (j + 3 - i) if keep_length else '""')
            i = j + 3
            continue
        verbatim = c == "@" and i + 1 < n and query[i + 1] in "'\""
        if c in "'\"" or verbatim:
            start = i
            if verbatim:
                i += 1
            q = query[i]
            i += 1
            while True:
                if i >= n:
                    raise GuardError("Unterminated string literal.")
                ch = query[i]
                if verbatim:
                    if ch == q:
                        if i + 1 < n and query[i + 1] == q:
                            i += 2
                            continue
                        i += 1
                        break
                    i += 1
                else:
                    if ch == "\\":
                        i += 2
                        continue
                    if ch == q:
                        i += 1
                        break
                    i += 1
            out.append(" " * (i - start) if keep_length else '""')
            continue
        out.append(c)
        i += 1
    return "".join(out)


# Tag keys that become columns (bag_unpack) or aliases and collide with KQL: `project` is an operator, so ADX fails with
# "SYN0002: The name 'project' needs to be bracketed as ['project']". `application` is bracketed too (team rule).
_BRACKETED_NAMES = ("project", "application")
_NAME_USE = re.compile(r"(\.\s*)?\b(" + "|".join(_BRACKETED_NAMES) + r")\b(?!-)")


def bracket_names(query: str) -> str:
    """Write a bare `project` / `application` used as a name as ['project'] / ['application'].

    Left alone: the operator (`| project`, `| project-away`), strings and comments, names already bracketed, and
    `let` names. `Tags.project` becomes `Tags['project']`.
    """
    masked = mask(query, keep_length=True)
    edits = []
    for m in _NAME_USE.finditer(masked):
        before = masked[:m.start()].rstrip()
        if not m.group(1) and (before.endswith("|") or re.search(r"\blet$", before)):
            continue
        edits.append((m.start(), m.end(), f"['{m.group(2)}']"))
    for start, end, text in reversed(edits):
        query = query[:start] + text + query[end:]
    return query


def normalize(query: str) -> str:
    """Strip surrounding whitespace and collapse blank lines (an empty line can end a query early)."""
    return re.sub(r"\n[ \t\r]*(?:\n[ \t\r]*)+", "\n", query.strip())


def _entity_pattern(entity: str) -> re.Pattern[str]:
    name = entity.strip().removesuffix("()")
    return re.compile(r"\b" + re.escape(name) + r"\b")


def validate_kql(
    query: str,
    *,
    allowed_entities: tuple[str, ...] = (),
    max_chars: int = 8_000,
) -> str:
    """Return the cleaned query or raise GuardError."""
    if not query or not query.strip():
        raise GuardError("Query is empty.")
    if len(query) > max_chars:
        raise GuardError(f"Query is too long ({len(query)} chars, limit {max_chars}). Simplify it.")

    masked = mask(query).strip()
    if not masked:
        raise GuardError("Query contains only comments.")
    if masked.startswith("."):
        raise GuardError("Management commands (starting with '.') are not allowed. Only read queries.")

    statements = [s.strip() for s in masked.split(";") if s.strip()]
    for stmt in statements:
        first = re.match(r"[A-Za-z_]+", stmt)
        if first and first.group(0).lower() in _BLOCKED_STATEMENTS:
            raise GuardError(f"'{first.group(0)}' statements are not allowed.")
    if statements[-1].lower().startswith("let "):
        raise GuardError("The query must end with a tabular expression, not a 'let' statement.")

    if _DENY_FUNCS.search(masked):
        raise GuardError("This query uses a function that can reach outside the FinOps data and is not allowed.")
    if _CROSS_SCOPE.search(masked):
        raise GuardError("cluster() and database() are not allowed. Use the provided FinOps entities directly.")
    if _SEARCH_FIND.search(masked):
        raise GuardError("'search' and 'find' scan every table and are not allowed. Query a specific entity.")
    if _UNION_STAR.search(masked):
        raise GuardError("'union *' is not allowed. Name the entities explicitly.")
    bad = _BAD_TIMESPAN.search(masked)
    if bad:
        raise GuardError(
            f"'{bad.group(0)}' is not a KQL timespan (units are d, h, m, s). For months use startofmonth(now(), -1) "
            "(start of last month) and startofmonth(now()); for years startofyear(now(), -1); for weeks use 7d."
        )
    for m in _EVALUATE.finditer(masked):
        if m.group(1) not in _ALLOWED_PLUGINS:
            raise GuardError(
                f"evaluate plugin '{m.group(1)}' is not allowed (allowed: {', '.join(sorted(_ALLOWED_PLUGINS))})."
            )

    if allowed_entities and not any(_entity_pattern(e).search(masked) for e in allowed_entities):
        names = ", ".join(allowed_entities)
        raise GuardError(f"The query must read from one of the allowed entities: {names}.")

    return normalize(bracket_names(query))

"""Charts for dashboard-tile results, drawn in the app from rows it already has (no LLM tokens).

`chart_spec` decides what to draw from the tile's visual type and the result's columns (pure Python, testable);
`to_figure` turns that into a Plotly figure. Chainlit renders figures as-is and the server doesn't know the
viewer's light/dark theme, so the styling is theme-neutral: transparent background, mid-gray ink and grid, and the
reference palette's dark-mode steps, which validate on both the light (#fcfcfb) and dark (#1a1a19) surfaces.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from .adx import QueryResult

# Categorical slots in fixed order (dataviz reference palette, dark steps; validated on light and dark surfaces).
SERIES_COLORS = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
OTHER_COLOR = "#898781"   # "Other" is muted gray, never a generated 9th hue
INK = "#898781"           # muted ink: legible on both surfaces
GRID = "rgba(137,135,129,0.25)"
MAX_SERIES = len(SERIES_COLORS) - 1  # top 7 by total + "Other"

CHART_VISUALS = {"column": "column", "stackedcolumn": "stacked", "bar": "bar", "timechart": "line",
                 "linechart": "line", "areachart": "line"}
PREFERRED_VALUE = ("EffectiveCost", "Cost", "Total", "BilledCost")


@dataclass
class ChartSpec:
    kind: str                                   # column | stacked | bar | line
    title: str
    x_name: str
    y_name: str
    x_is_time: bool
    series: dict[str, tuple[list, list]] = field(default_factory=dict)  # name -> (xs, ys), in color order


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


_FRACTION = re.compile(r"(\.\d{6})\d+")  # ADX sends 7 fractional digits; fromisoformat takes at most 6


def _as_time(v: Any) -> dt.datetime | None:
    if not isinstance(v, str) or len(v) < 10 or v[4] != "-":
        return None
    try:
        return dt.datetime.fromisoformat(_FRACTION.sub(r"\1", v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _expand_series(result: QueryResult) -> QueryResult | None:
    """make-series output (each row holds arrays) -> long rows [time, value, series].

    Trailing zeros are dropped per series: make-series pads days with no data (e.g. actuals after today, next to
    a forecast), and a line falling to zero there would be wrong.
    """
    cols, long = result.columns, []
    for row in result.rows:
        arrays = {i: v for i, v in enumerate(row) if isinstance(v, list)}
        times = next((i for i, v in arrays.items() if v and all(_as_time(t) for t in v)), None)
        if times is None:
            return None
        label = " · ".join(str(v) for i, v in enumerate(row) if i not in arrays and isinstance(v, str))
        for i, values in arrays.items():
            if i == times or not values or not all(_is_number(x) or x is None for x in values):
                continue
            end = len(values)
            while end and not values[end - 1]:
                end -= 1
            name = f"{label} · {cols[i]}" if label else cols[i]
            long += [[row[times][k], values[k], name] for k in range(end)]
    return QueryResult([cols[times] if result.rows else "Time", "Cost", "Series"], long, len(long), False)


def chart_spec(result: QueryResult, visual: str, title: str) -> ChartSpec | None:
    """What to draw, or None when the tile isn't a chart (table, multistat) or the result has no plottable shape."""
    kind = CHART_VISUALS.get((visual or "").lower())
    if kind is None or not result.rows or not result.columns:
        return None
    keep_source_order = False
    if any(isinstance(v, list) for v in result.rows[0]):
        result = _expand_series(result)
        if result is None or not result.rows:
            return None
        keep_source_order = True  # actual before forecast, as the query lists them
    cols, rows = result.columns, result.rows
    numeric = [i for i, _ in enumerate(cols) if all(_is_number(r[i]) or r[i] is None for r in rows)
               and any(_is_number(r[i]) for r in rows)]
    timeish = [i for i, _ in enumerate(cols) if i not in numeric and all(_as_time(r[i]) for r in rows)]
    texty = [i for i, _ in enumerate(cols) if i not in numeric and i not in timeish]
    if not numeric:
        return None
    y = next((i for name in PREFERRED_VALUE for i in numeric if cols[i] == name), numeric[0])
    if kind == "bar":  # horizontal: category on the axis
        label = bar_label(result) if len(texty) > 1 else None
        x = label if label is not None else (texty[0] if texty else (timeish[0] if timeish else None))
        group = None
    else:
        x = timeish[0] if timeish else (texty[0] if texty else None)
        group = next((i for i in texty if i != x), None)
    if x is None:
        return None
    x_is_time = x in timeish

    totals: dict[str, float] = {}
    points: dict[str, dict] = {}
    for r in rows:
        name = str(r[group]) if group is not None else cols[y]
        xv = _as_time(r[x]) if x_is_time else str(r[x])
        val = float(r[y] or 0)
        totals[name] = totals.get(name, 0.0) + val
        points.setdefault(name, {})
        points[name][xv] = points[name].get(xv, 0.0) + val

    # Color follows the entity in a stable order (largest first); past MAX_SERIES, fold into "Other".
    order = (list(totals) if keep_source_order  # tile "(N others)" goes last
             else sorted(totals, key=lambda n: (n.startswith("("), -totals[n], n)))
    keep, rest = order[:MAX_SERIES], order[MAX_SERIES:]
    if rest:
        other: dict = {}
        for name in rest:
            for xv, val in points[name].items():
                other[xv] = other.get(xv, 0.0) + val
        points["Other"] = other
        keep.append("Other")
    all_x = {xv for name in keep for xv in points[name]}
    if kind in ("column", "stacked") and len(all_x) == 1 and len(keep) > 1:
        # One period, many series: nothing to trend. Rank the series as horizontal bars instead.
        only = next(iter(all_x))
        names = sorted(keep, key=lambda n: points[n][only])
        period = only.strftime("%b %Y") if isinstance(only, dt.datetime) else str(only)
        return ChartSpec("bar", f"{title} ({period})", cols[group] if group is not None else cols[x], cols[y], False,
                         {cols[y]: (names, [round(points[n][only], 2) for n in names])})
    # Whole months on a time axis render as midnight timestamps; show them as "Sep 2026" categories instead.
    monthly = x_is_time and all(v is not None and v.day == 1 and v.hour == 0 and v.minute == 0 for v in all_x)
    series = {}
    for name in keep:
        xs = sorted(points[name], key=lambda v: (v is None, v))
        if kind == "bar":  # biggest at the top
            xs = sorted(points[name], key=lambda v: points[name][v])
        ys = [round(points[name][xv], 2) for xv in xs]
        series[name] = ([xv.strftime("%b %Y") for xv in xs] if monthly else xs, ys)
    return ChartSpec(kind, title, cols[x], cols[y], x_is_time and not monthly, series)


MAX_BAR_CATEGORIES = 30
FEW_PERIODS = 12  # up to this many time points read best as columns; more as a line


def infer_visual(result: QueryResult) -> str | None:
    """A chart type for a model-written query's result, from its columns (dashboard tiles carry their own).

    Time + value (+ category) -> columns / line / stacked; category + value -> ranked bars; make-series arrays ->
    lines. None for single rows, wide tables and anything without one numeric value column.
    """
    if not result.rows or len(result.rows) < 2 and not any(isinstance(v, list) for v in result.rows[0]):
        return None
    if any(isinstance(v, list) for v in result.rows[0]):
        return "timechart"
    cols, rows = result.columns, result.rows
    numeric = [i for i in range(len(cols)) if all(_is_number(r[i]) or r[i] is None for r in rows)
               and any(_is_number(r[i]) for r in rows)]
    timeish = [i for i in range(len(cols)) if i not in numeric and all(_as_time(r[i]) for r in rows)]
    texty = [i for i in range(len(cols)) if i not in numeric and i not in timeish]
    if not numeric:
        return None
    if timeish:
        if len(texty) > 1:
            return None  # time x several categories: a detail table
        if texty:
            return "stackedcolumn"
        points = len({r[timeish[0]] for r in rows})
        return "column" if points <= FEW_PERIODS else "timechart"
    # A ranking: one bar per row, labelled by a text column unique per row (others, e.g. Category, are details).
    if texty and len(rows) <= MAX_BAR_CATEGORIES and bar_label(result) is not None:
        return "bar"
    return None


def bar_label(result: QueryResult) -> int | None:
    """The text column that names each row (unique per row, the most specific one), or None."""
    cols, rows = result.columns, result.rows
    texty = [i for i in range(len(cols)) if all(isinstance(r[i], str) for r in rows) and not all(_as_time(r[i]) for r in rows)]
    unique = [i for i in texty if len({r[i] for r in rows}) == len(rows)]
    if not unique:
        return None
    named = [i for i in unique if any(w in cols[i].lower() for w in _NAME_WORDS)]
    pool = named or unique  # a "name" column if there is one, else the most descriptive (longest) values
    return max(pool, key=lambda i: sum(len(r[i]) for r in rows))


_NAME_WORDS = ("name", "service", "resource", "sku", "meter", "subscription", "account", "user", "tag", "region")


def _short(name: str, limit: int = 40) -> str:
    return name if len(name) <= limit else name[: limit - 1] + "…"


def to_figure(spec: ChartSpec, currency: str = ""):
    """Plotly figure for a spec (imported lazily: only the app needs plotly)."""
    import plotly.graph_objects as go

    money = (currency or "").upper() == "USD"
    fmt = "$,.2f" if money else ",.2f"
    unit = "" if money or not currency or currency == "unknown" else f" {currency}"
    fig = go.Figure()
    for i, (name, (xs, ys)) in enumerate(spec.series.items()):
        color = OTHER_COLOR if name == "Other" else SERIES_COLORS[i % len(SERIES_COLORS)]
        label = name.replace("<", "&lt;")  # full name in the tooltip; the legend gets a short one
        when = "%{x|%b %d, %Y}" if spec.x_is_time else "%{x}"
        hover = f"<b>{label}</b><br>{when}<br>%{{y:{fmt}}}{unit}<extra></extra>"
        if spec.kind == "line":
            projected = "forecast" in name.lower()  # dashed: a projection must not read as data
            fig.add_trace(go.Scatter(x=xs, y=ys, name=_short(name), mode="lines+markers",
                                     line={"width": 2, "color": color, "dash": "dash" if projected else "solid"},
                                     marker={"size": 8 if not projected else 6, "color": color},
                                     hovertemplate=hover))
        elif spec.kind == "bar":
            fig.add_trace(go.Bar(y=xs, x=ys, name=_short(name), orientation="h", marker_color=color,
                                 hovertemplate=f"<b>%{{y}}</b><br>%{{x:{fmt}}}{unit}<extra></extra>"))
        else:
            fig.add_trace(go.Bar(x=xs, y=ys, name=_short(name), marker_color=color, hovertemplate=hover))
    axis = {"color": INK, "gridcolor": GRID, "linecolor": GRID, "zerolinecolor": GRID, "tickfont": {"color": INK}}
    value_axis = {**axis, "tickformat": "$,.2~f" if money else ",.2~f", "ticksuffix": unit}  # $0.5, not $0
    fig.update_layout(
        title={"text": spec.title, "font": {"size": 15, "color": INK}, "x": 0, "xanchor": "left"},
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font={"color": INK, "size": 12},
        barmode="stack" if spec.kind == "stacked" else "group", bargap=0.45, barcornerradius=4,
        showlegend=len(spec.series) > 1,
        # Legend under the plot: long resource names would otherwise run into the title.
        legend={"orientation": "h", "yanchor": "top", "y": -0.12, "x": 0, "font": {"color": INK}},
        hoverlabel={"bgcolor": "#1a1a19", "font": {"color": "#ffffff"}, "bordercolor": "#383835"},
        margin={"l": 8, "r": 8, "t": 48, "b": 8}, height=360 + 22 * ((len(spec.series) + 1) // 2 if
                                                                   len(spec.series) > 1 else 0),
    )
    if spec.kind == "bar":
        fig.update_xaxes(**value_axis)
        fig.update_yaxes(**axis, automargin=True)
    else:
        fig.update_xaxes(**axis, showgrid=False)
        fig.update_yaxes(**value_axis)
    if spec.x_is_time and spec.kind != "bar":
        fig.update_xaxes(tickformat="%b %d")
    return fig

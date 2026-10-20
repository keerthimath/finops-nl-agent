import unittest

from finops_nl.adx import QueryResult
from finops_nl.charts import MAX_SERIES, OTHER_COLOR, SERIES_COLORS, chart_spec, infer_visual, to_figure


def qr(cols, rows):
    return QueryResult(cols, rows, len(rows), False)


DAYS = [f"2026-09-{d:02d}T00:00:00.0000000Z" for d in range(15, 18)]


class ChartSpecTests(unittest.TestCase):
    def test_not_charts(self):
        self.assertIsNone(chart_spec(qr(["a", "b"], [["x", 1]]), "table", "T"))
        self.assertIsNone(chart_spec(qr(["a", "b"], [["x", 1]]), "multistat", "T"))
        self.assertIsNone(chart_spec(qr(["a"], []), "column", "T"))
        self.assertIsNone(chart_spec(qr(["a", "b"], [["x", "y"]]), "column", "T"))  # nothing numeric

    def test_stacked_daily_with_series(self):
        rows = [[d, 1.0 + i, rg] for i, d in enumerate(DAYS) for rg in ("rg-a", "rg-b")]
        spec = chart_spec(qr(["ChargePeriodStart", "EffectiveCost", "RG"], rows), "stackedcolumn", "Daily")
        self.assertEqual((spec.kind, spec.x_is_time, spec.y_name), ("stacked", True, "EffectiveCost"))
        self.assertEqual(list(spec.series), ["rg-a", "rg-b"])
        self.assertEqual(spec.series["rg-a"][1], [1.0, 2.0, 3.0])

    def test_folds_series_past_the_palette_into_other(self):
        rows = [[DAYS[0], float(100 - i), f"r{i}"] for i in range(12)] + \
               [[DAYS[1], 1.0, f"r{i}"] for i in range(12)] + [[DAYS[0], 0.5, "(4 others)"]]
        spec = chart_spec(qr(["ChargePeriodStart", "EffectiveCost", "RG"], rows), "stackedcolumn", "T")
        names = list(spec.series)
        self.assertEqual(len(names), MAX_SERIES + 1)
        self.assertEqual(names[0], "r0")           # largest first: color follows size, stably
        self.assertEqual(names[-1], "Other")
        self.assertNotIn("(4 others)", names[:MAX_SERIES])  # the tile's own fold sorts last, into Other

    def test_months_become_categories_and_one_period_becomes_ranked_bars(self):
        rows = [["2026-08-01T00:00:00Z", 5.0, "svc-a"], ["2026-09-01T00:00:00Z", 7.0, "svc-a"],
                ["2026-09-01T00:00:00Z", 2.0, "svc-b"]]
        spec = chart_spec(qr(["ChargePeriodStart", "EffectiveCost", "Category"], rows), "column", "Monthly")
        self.assertFalse(spec.x_is_time)
        self.assertEqual(spec.series["svc-a"][0], ["Aug 2026", "Sep 2026"])
        one = chart_spec(qr(["ChargePeriodStart", "EffectiveCost", "Category"], rows[1:]), "column", "Monthly")
        self.assertEqual((one.kind, one.title), ("bar", "Monthly (Sep 2026)"))
        self.assertEqual(one.series["EffectiveCost"], (["svc-b", "svc-a"], [2.0, 7.0]))  # biggest at the top

    def test_horizontal_bar(self):
        spec = chart_spec(qr(["SKU", "EffectiveCost"], [["a", 1.0], ["b", 3.0]]), "bar", "SKUs")
        self.assertEqual(spec.kind, "bar")
        self.assertEqual(spec.series["EffectiveCost"], (["a", "b"], [1.0, 3.0]))

    def test_make_series_arrays(self):
        actual = [0.3, 0.7, 0.7, 0.0, 0.0]          # padded with zeros after the last day of data
        forecast = [0.6, 0.61, 0.62, 0.63, 0.64]
        days = [f"2026-09-{d:02d}T00:00:00.0000000Z" for d in range(15, 20)]
        spec = chart_spec(qr(["EffectiveCost", "ChargePeriodStart", "Forecast"], [[actual, days, forecast]]),
                          "timechart", "Forecast")
        self.assertEqual(spec.kind, "line")
        self.assertEqual(list(spec.series), ["EffectiveCost", "Forecast"])  # source order: actual first
        self.assertEqual(len(spec.series["EffectiveCost"][0]), 3)           # trailing zeros dropped
        self.assertEqual(len(spec.series["Forecast"][0]), 5)


class InferVisualTests(unittest.TestCase):
    def test_shapes(self):
        daily = [[f"2026-09-{d:02d}T00:00:00Z", float(d)] for d in range(1, 31)]
        self.assertEqual(infer_visual(qr(["ChargePeriodStart", "Cost"], daily)), "timechart")
        self.assertEqual(infer_visual(qr(["ChargePeriodStart", "Cost"], daily[:5])), "column")
        by_svc = [[d, 1.0, s] for d, _ in daily[:3] for s in ("a", "b")]
        self.assertEqual(infer_visual(qr(["ChargePeriodStart", "Cost", "Service"], by_svc)), "stackedcolumn")
        self.assertEqual(infer_visual(qr(["ServiceName", "Cost"], [["a", 2.0], ["b", 1.0]])), "bar")
        self.assertEqual(infer_visual(qr(["Cost", "Day", "Forecast"], [[[1.0], ["2026-09-01T00:00:00Z"], [2.0]]])),
                         "timechart")

    def test_not_charted(self):
        self.assertIsNone(infer_visual(qr(["Total", "BillingCurrency"], [[12.5, "USD"]])))         # one row
        self.assertIsNone(infer_visual(qr(["a", "b"], [["x", "y"], ["z", "w"]])))                 # no numbers
        self.assertIsNone(infer_visual(qr(["Day", "RG", "Resource", "Cost"],
                                          [["2026-09-01T00:00:00Z", "r", "x", 1.0],
                                           ["2026-09-02T00:00:00Z", "r", "y", 2.0]])))         # time x 2 categories
        self.assertIsNone(infer_visual(qr(["RG", "Service", "Cost"], [["r", "s", 1.0], ["r", "s", 2.0]])))  # no row name
        self.assertIsNone(infer_visual(qr(["Name", "Cost"], [[f"n{i}", 1.0] for i in range(40)])))  # too many bars
        self.assertIsNone(infer_visual(qr(["Cost"], [])))

    def test_ranking_with_detail_columns_uses_the_unique_label(self):
        # what gpt-5 wrote for "top 10 services by cost this month"
        rows = [["Cloud Provider", "Azure Cache for Redis", "Databases", 7.97],
                ["Cloud Provider", "Azure Container Registry", "Containers", 2.5],
                ["Cloud Provider", "Key Vault", "Security", 0.02]]
        r = qr(["Segment", "Service", "Category", "Cost"], rows)
        self.assertEqual(infer_visual(r), "bar")
        spec = chart_spec(r, "bar", "Top services")
        self.assertEqual(spec.x_name, "Service")
        self.assertEqual(spec.series["Cost"][0][-1], "Azure Cache for Redis")

    def test_inferred_visual_draws(self):
        rows = [["Azure Cache for Redis", 7.97], ["Container Registry", 2.5], ["Key Vault", 0.02]]
        spec = chart_spec(qr(["ServiceName", "EffectiveCost"], rows), infer_visual(qr(["ServiceName", "EffectiveCost"], rows)),
                          "Top services")
        self.assertEqual(spec.kind, "bar")
        self.assertEqual(spec.series["EffectiveCost"][0][-1], "Azure Cache for Redis")  # biggest at the top


class FigureTests(unittest.TestCase):
    def setUp(self):
        try:
            import plotly  # noqa: F401
        except ImportError:
            self.skipTest("plotly not installed")

    def test_theme_neutral_styling_and_colors(self):
        rows = [[d, 1.0, rg] for d in DAYS for rg in ("rg-a", "rg-b")] + [[DAYS[0], 0.1, f"r{i}"] for i in range(9)]
        fig = to_figure(chart_spec(qr(["ChargePeriodStart", "EffectiveCost", "RG"], rows), "stackedcolumn", "T"), "USD")
        lay = fig.layout
        self.assertEqual((lay.paper_bgcolor, lay.plot_bgcolor), ("rgba(0,0,0,0)", "rgba(0,0,0,0)"))
        self.assertEqual(lay.barmode, "stack")
        colors = [t.marker.color for t in fig.data]
        self.assertEqual(colors[:2], SERIES_COLORS[:2])
        self.assertEqual(colors[-1], OTHER_COLOR)
        self.assertTrue(lay.showlegend)
        self.assertIn("$", fig.data[0].hovertemplate)

    def test_forecast_is_dashed_and_long_names_shortened(self):
        days = [f"2026-09-{d:02d}T00:00:00Z" for d in range(15, 18)]
        fig = to_figure(chart_spec(qr(["EffectiveCost", "ChargePeriodStart", "Forecast"],
                                      [[[1.0, 2.0, 3.0], days, [1.5, 2.5, 3.5]]]), "timechart", "F"), "EUR")
        self.assertEqual([t.line.dash for t in fig.data], ["solid", "dash"])
        self.assertIn(" EUR", fig.layout.yaxis.ticksuffix)
        rows = [[DAYS[0], 1.0, "x" * 80], [DAYS[1], 1.0, "y"]]
        fig = to_figure(chart_spec(qr(["ChargePeriodStart", "EffectiveCost", "RG"], rows), "column", "T"), "USD")
        self.assertLessEqual(len(fig.data[0].name), 40)
        self.assertIn("x" * 80, fig.data[0].hovertemplate)  # full name in the tooltip


if __name__ == "__main__":
    unittest.main()

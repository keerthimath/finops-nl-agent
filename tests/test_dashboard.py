"""Dashboard tiles -> standalone KQL, and the run_dashboard_query tool.

The fixture mirrors the structure of a FinOps hub ADX dashboard export (nested base queries, parameters with
'all' and query-computed defaults, a cross-database call in CostsPlus, system-table tiles) without its data.
"""
import asyncio
import copy
import json
import os
import tempfile
import unittest
from unittest import mock

from finops_nl.adx import AdxError
from finops_nl.config import Settings
from finops_nl.dashboard import DEFAULT_INT, load_dashboard, parse_dashboard
from finops_nl.guard import mask, validate_kql
from finops_nl.tools import ToolRunner

ENT = ("Costs_v1_2()", "Recommendations()")

COSTS_PLUS = """Costs_v1_2
| where ChargePeriodStart >= monthsago(numberOfMonths)
| extend tmp_ResourceParent = database('Ingestion').parse_resourceid(Tags['cm-resource-parent'])
| extend x_ResourceParentName = tostring(tmp_ResourceParent.ResourceName)
| extend JsonDynamic = parse_json(Tags)
| evaluate bag_unpack(JsonDynamic)
| where SubAccountName in (subscriptionNames) or isempty(subscriptionNames)
// trailing comment"""

USERS_TILE = """let costs = CostsByDay;
let topX = costs | where isnotempty(Creator) | summarize sum(EffectiveCost) by tostring(Creator)
    | order by sum_EffectiveCost desc | limit maxGroupCount;
costs
| where isnotempty(Creator)
| extend Creator = iff(Creator in (topX), Creator, '(others)')
| summarize EffectiveCost = round(sum(EffectiveCost), 2) by ChargePeriodStart = startofday(ChargePeriodStart), Creator
| render columnchart"""

DASH = {
    "title": "Subscription",
    "pages": [{"id": "p1", "name": "Users"}, {"id": "p2", "name": "AI / ML"}],
    "parameters": [
        {"kind": "int", "variableName": "numberOfMonths", "displayName": "Monthly trend",
         "selectionType": "scalar", "defaultValue": {"kind": "query-result"}},
        {"kind": "int", "variableName": "numberOfDays", "displayName": "Daily trend",
         "selectionType": "freetext", "defaultValue": {"kind": "value", "value": 28}},
        {"kind": "int", "variableName": "maxGroupCount", "displayName": "Max group count",
         "selectionType": "freetext", "defaultValue": {"kind": "value", "value": 9}},
        {"kind": "string", "variableName": "subscriptionNames", "displayName": "Subscription",
         "selectionType": "array", "defaultValue": {"kind": "all"}},
        {"kind": "string", "variableName": "selectedCategory", "displayName": "category",
         "selectionType": "array", "defaultValue": {"kind": "values", "values": [""]}},
    ],
    "queries": [
        {"id": "qPlus", "text": COSTS_PLUS},
        {"id": "qDay", "text": "CostsPlus\n| where ChargePeriodStart >= ago(numberOfDays * 1d)"},
        {"id": "qDbx", "text": "let m = database('Ingestion').DatabricksWorkspaceMapping;\nm | take 1"},
        {"id": "qUsers", "text": USERS_TILE},
        {"id": "qUsersTable", "text": "CostsByDay | summarize sum(EffectiveCost) by Creator"},
        {"id": "qSystem", "text": "SystemTable | where toupper(billing_origin_product) in (selectedCategory)"},
        {"id": "qDbxTile", "text": "CostsPlus | join DatabricksSystemBillingUsage on x_ResourceId"},
        {"id": "qParent", "text": "CostsPlus | summarize sum(EffectiveCost) by x_ResourceParentName"},
    ],
    "baseQueries": [
        # CostsByDay is listed before the CostsPlus it depends on: the order must come from references.
        {"queryId": "qDay", "variableName": "CostsByDay"},
        {"queryId": "qPlus", "variableName": "CostsPlus"},
        {"queryId": "qDbx", "variableName": "DatabricksSystemBillingUsage"},
    ],
    "tiles": [
        {"id": "m", "title": "", "visualType": "markdownCard", "pageId": "p1"},
        {"id": "t1", "title": "Daily Trend By Users", "description": "Daily Cost by Individual Users",
         "visualType": "stackedcolumn", "pageId": "p1", "queryRef": {"queryId": "qUsers"}},
        {"id": "t2", "title": "Daily Trend By Users", "visualType": "table", "pageId": "p1",
         "queryRef": {"queryId": "qUsersTable"}},
        {"id": "t3", "title": "Daily trend by Usage Type", "visualType": "column", "pageId": "p2",
         "queryRef": {"queryId": "qSystem"}},
        {"id": "t4", "title": "Billing Origin Details", "visualType": "table", "pageId": "p2",
         "queryRef": {"queryId": "qDbxTile"}},
        {"id": "t5", "title": "By parent", "visualType": "table", "pageId": "p1", "queryRef": {"queryId": "qParent"}},
    ],
}


def dash(d=DASH):
    return parse_dashboard(copy.deepcopy(d), ENT)


class ParseTests(unittest.TestCase):
    def test_usable_and_skipped_tiles(self):
        d = dash()
        self.assertEqual(list(d.tiles), ["users/daily-trend-by-users", "users/daily-trend-by-users-table"])
        skipped = dict(d.skipped)
        self.assertIn("allowed entities", skipped["Daily trend by Usage Type"])        # system table
        self.assertIn("database()", skipped["Billing Origin Details"])                  # Ingestion database
        self.assertIn("x_ResourceParent", skipped["By parent"])                         # column blanked by a rewrite
        self.assertEqual(len(d.skipped), 3)

    def test_query_is_standalone_and_passes_guard(self):
        q = dash().query("users/daily-trend-by-users")
        validate_kql(q, allowed_entities=ENT)
        self.assertLess(q.index("let CostsPlus ="), q.index("let CostsByDay ="))       # dependency order
        self.assertNotIn("database(", mask(q))                                          # rewrite applied
        self.assertNotIn("DatabricksSystemBillingUsage", q)                             # unused base not included
        self.assertIn(f"let numberOfMonths = {DEFAULT_INT};", q)                        # query-computed default
        self.assertIn("let numberOfDays = 28;", q)
        self.assertIn("let subscriptionNames = dynamic(null);", q)  # 'all': isempty() is false for dynamic([])
        self.assertIn("// trailing comment\n;", q)                                      # comment can't eat the ';'

    def test_params_only_those_tiles_use(self):
        d = dash()
        self.assertEqual(d.tiles["users/daily-trend-by-users"].params,
                         ("numberOfMonths", "numberOfDays", "maxGroupCount", "subscriptionNames"))
        self.assertNotIn("selectedCategory", d.params)  # only the skipped system-table tile used it

    def test_cycle_is_skipped(self):
        d = copy.deepcopy(DASH)
        d["queries"][1]["text"] = "CostsPlus | union CostsByDay"
        d["queries"][0]["text"] = "CostsByDay | where true"
        self.assertTrue(any("cycle" in r for _, r in parse_dashboard(d, ENT).skipped))

    def test_base_query_with_lets_becomes_view(self):
        d = copy.deepcopy(DASH)
        d["queries"][1]["text"] = "let n = numberOfDays;\nCostsPlus | where ChargePeriodStart >= ago(n * 1d)"
        q = parse_dashboard(d, ENT).query("users/daily-trend-by-users")
        self.assertIn("let CostsByDay = view () {", q)
        validate_kql(q, allowed_entities=ENT)

    def test_load_from_file_with_exclusions(self):
        from finops_nl.dashboard import exclude_path
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(DASH, f)
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(len(load_dashboard(f.name, ENT).tiles), 2)
        excl = exclude_path(f.name)
        self.assertTrue(excl.endswith(".exclude.json") and not excl.endswith(".json.exclude.json"))
        with open(excl, "w") as e:
            json.dump({"users/daily-trend-by-users": "Failed to resolve scalar expression named 'Creator'"}, e)
        self.addCleanup(os.unlink, excl)
        d = load_dashboard(f.name, ENT)
        self.assertEqual(list(d.tiles), ["users/daily-trend-by-users-table"])
        self.assertIn("Creator", dict(d.skipped)["users/daily-trend-by-users"])
        self.assertNotIn("maxGroupCount", d.params)  # only the excluded tile used it
        self.assertEqual(len(load_dashboard(f.name, ENT, use_exclusions=False).tiles), 2)


class OverrideTests(unittest.TestCase):
    T = "users/daily-trend-by-users"

    def test_overrides(self):
        q = dash().query(self.T, {"numberOfDays": 14, "maxGroupCount": "5", "subscriptionNames": ["Prod A"]})
        self.assertIn("let numberOfDays = 14;", q)
        self.assertIn("let maxGroupCount = 5;", q)
        self.assertIn('let subscriptionNames = dynamic(["Prod A"]);', q)

    def test_string_values_cannot_break_out(self):
        evil = 'x"]); Costs_v1_2 | take 1000000 //'
        q = dash().query(self.T, {"subscriptionNames": [evil, "back\\slash"]})
        validate_kql(q, allowed_entities=ENT)
        first_let = mask(q.split("\n", 4)[3])  # the subscriptionNames line, strings blanked
        self.assertEqual(first_let, 'let subscriptionNames = dynamic(["", ""]);')

    def test_invalid_overrides(self):
        d = dash()
        for bad in ({"numberOfDays": 0}, {"numberOfDays": 10_000}, {"numberOfDays": True},
                    {"subscriptionNames": "Prod"}, {"subscriptionNames": ["a\nb"]},
                    {"subscriptionNames": ["x"] * 51}, {"selectedCategory": ["x"]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                d.query(self.T, bad)


class FakeAdx:
    def __init__(self):
        self.calls = []

    def query(self, kql, **_):
        from finops_nl.adx import QueryResult
        self.calls.append(kql)
        return QueryResult(["Creator", "EffectiveCost"], [["alice", 10.0]], 1, False, 3, "r1")


class ToolTests(unittest.TestCase):
    S = Settings(adx_cluster_uri="https://x", adx_database="Hub")

    def runner(self, d="default"):
        self.adx = FakeAdx()
        return ToolRunner(self.S, self.adx, {"schema": "S"}, dashboard=dash() if d == "default" else d)

    def test_definition(self):
        defs = {t["name"]: t for t in self.runner().definitions()}
        tool = defs["run_dashboard_query"]
        self.assertEqual(tool["input_schema"]["properties"]["tile"]["enum"],
                         ["users/daily-trend-by-users", "users/daily-trend-by-users-table"])
        self.assertIn("Daily Cost by Individual Users", tool["description"])
        self.assertEqual(tool["input_schema"]["properties"]["parameters"]["properties"]["subscriptionNames"],
                         {"type": "array", "items": {"type": "string"}})

    def test_no_dashboard_no_tool(self):
        self.assertNotIn("run_dashboard_query", [t["name"] for t in self.runner(None).definitions()])

    def test_run(self):
        out = asyncio.run(self.runner().run("run_dashboard_query",
                                            {"tile": "users/daily-trend-by-users", "parameters": {"numberOfDays": 7}}))
        self.assertFalse(out.is_error, out.content)
        self.assertIn("let numberOfDays = 7;", self.adx.calls[0])
        self.assertEqual(out.kql, self.adx.calls[0])
        self.assertIn("alice", out.content)

    def test_missing_tag_column_hides_tile_for_the_session(self):
        unavailable = set()
        self.adx = FakeAdx()
        self.adx.query = lambda kql, **_: (_ for _ in ()).throw(AdxError(
            "Semantic error: SEM0100: 'where' operator: Failed to resolve scalar expression named 'Creator'"))
        r = ToolRunner(self.S, self.adx, {}, dashboard=dash(), unavailable_tiles=unavailable)
        out = asyncio.run(r.run("run_dashboard_query", {"tile": "users/daily-trend-by-users"}))
        self.assertTrue(out.is_error)
        self.assertIn("needs 'Creator'", out.content)
        self.assertIn("do not substitute your own definition", out.content)
        self.assertEqual(unavailable, {"users/daily-trend-by-users"})
        tool = [t for t in r.definitions() if t["name"] == "run_dashboard_query"][0]
        self.assertEqual(tool["input_schema"]["properties"]["tile"]["enum"], ["users/daily-trend-by-users-table"])
        self.assertNotIn("users/daily-trend-by-users:", tool["description"])
        again = asyncio.run(r.run("run_dashboard_query", {"tile": "users/daily-trend-by-users"}))
        self.assertIn("Unknown tile", again.content)
        unavailable.add("users/daily-trend-by-users-table")  # nothing left: no tool at all
        self.assertNotIn("run_dashboard_query", [t["name"] for t in r.definitions()])

    def test_other_adx_errors_keep_the_tile(self):
        unavailable = set()
        self.adx = FakeAdx()
        self.adx.query = lambda kql, **_: (_ for _ in ()).throw(AdxError("Query timed out"))
        r = ToolRunner(self.S, self.adx, {}, dashboard=dash(), unavailable_tiles=unavailable)
        out = asyncio.run(r.run("run_dashboard_query", {"tile": "users/daily-trend-by-users"}))
        self.assertIn("timed out", out.content)
        self.assertEqual(unavailable, set())

    def test_scope_filters_cost_rows_before_the_tile(self):
        s = [{"column": "ResourceName", "values": ["stor-a", 'x") | take 1 //']}]
        r = self.runner()
        out = asyncio.run(r.run("run_dashboard_query", {"tile": "users/daily-trend-by-users", "scope": s}))
        self.assertFalse(out.is_error, out.content)
        q = self.adx.calls[0]
        self.assertIn('(Costs_v1_2() | where ResourceName in~ ("stor-a", "x\\") | take 1 //"))', q)  # escaped
        self.assertNotIn("Costs_v1_2\n", q)  # every read of the table is scoped
        validate_kql(q, allowed_entities=ENT)
        bad = asyncio.run(r.run("run_dashboard_query", {"tile": "users/daily-trend-by-users",
                                                       "scope": [{"column": "Tags", "values": ["x"]}]}))
        self.assertTrue(bad.is_error)
        self.assertIn("scope column must be one of", bad.content)
        empty = asyncio.run(r.run("run_dashboard_query", {"tile": "users/daily-trend-by-users",
                                                         "scope": [{"column": "ResourceName", "values": []}]}))
        self.assertTrue(empty.is_error)

    def test_same_tile_run_twice_in_a_turn_is_not_rerun(self):
        r = self.runner()
        args = {"tile": "users/daily-trend-by-users", "parameters": {"numberOfDays": 7},
                "scope": [{"column": "ResourceName", "values": ["a"]}]}
        first = asyncio.run(r.run("run_dashboard_query", args))
        again = asyncio.run(r.run("run_dashboard_query", json.loads(json.dumps(args))))
        self.assertFalse(first.repeat)
        self.assertTrue(again.repeat)
        self.assertFalse(again.is_error)
        self.assertIn("Do not run it again", again.content)
        self.assertIsNone(again.result)  # no rows: no second chart, no UI step
        self.assertEqual(len(self.adx.calls), 1)
        other = asyncio.run(r.run("run_dashboard_query", {**args, "parameters": {"numberOfDays": 14}}))
        self.assertFalse(other.repeat)  # a different window is a new query
        self.assertEqual(len(self.adx.calls), 2)

    def test_failed_tile_run_can_be_retried(self):
        r = self.runner()
        self.adx.query = lambda kql, **_: (_ for _ in ()).throw(AdxError("Query timed out"))
        asyncio.run(r.run("run_dashboard_query", {"tile": "users/daily-trend-by-users"}))
        self.adx.query = FakeAdx().query
        retry = asyncio.run(r.run("run_dashboard_query", {"tile": "users/daily-trend-by-users"}))
        self.assertFalse(retry.repeat)

    def test_fixed_tile_parameter_uses_the_default(self):
        with mock.patch("finops_nl.tools.FIXED_TILE_PARAMS", {"users/": ("maxGroupCount",)}):
            r = self.runner()
            out = asyncio.run(r.run("run_dashboard_query", {"tile": "users/daily-trend-by-users",
                                                           "parameters": {"maxGroupCount": 50}}))
            self.assertFalse(out.is_error, out.content)
            self.assertIn("let maxGroupCount = 9;", self.adx.calls[0])  # the dashboard default, not 50
            self.assertIn("default maxGroupCount", out.content)
            # the dropped value doesn't make it a different run
            again = asyncio.run(r.run("run_dashboard_query", {"tile": "users/daily-trend-by-users"}))
            self.assertTrue(again.repeat)

    def test_tile_source_label_for_the_ui(self):
        r = self.runner()
        out = asyncio.run(r.run("run_dashboard_query", {"tile": "users/daily-trend-by-users",
                                                       "scope": [{"column": "ResourceName", "values": ["a", "b"]}]}))
        self.assertEqual(out.source, "Dashboard tile: Users / Daily Trend By Users (scope: ResourceName = a, b)")
        plain = asyncio.run(r.run("run_dashboard_query", {"tile": "users/daily-trend-by-users"}))
        self.assertEqual(plain.source, "Dashboard tile: Users / Daily Trend By Users")
        own = asyncio.run(r.run("run_kql", {"kql": "Costs_v1_2() | take 1"}))
        self.assertEqual(own.source, "")  # the app labels it "KQL (written by the assistant)"

    def test_unused_parameters_are_dropped_not_rejected(self):
        r = self.runner()
        out = asyncio.run(r.run("run_dashboard_query", {"tile": "users/daily-trend-by-users-table",
                                                       "parameters": {"numberOfDays": 14, "bogus": 1}}))
        self.assertFalse(out.is_error, out.content)
        self.assertIn("Ignored parameters this tile doesn't use: bogus", out.content)
        self.assertIn("let numberOfDays = 14;", self.adx.calls[0])

    def test_failed_tile_call_keeps_its_tile_label(self):
        out = asyncio.run(self.runner().run("run_dashboard_query", {"tile": "users/daily-trend-by-users",
                                                                   "parameters": {"numberOfDays": 0}}))
        self.assertTrue(out.is_error)
        self.assertEqual(out.source, "Dashboard tile: Users / Daily Trend By Users")  # not "written by the assistant"

    def test_scope_by_resource_type(self):
        out = asyncio.run(self.runner().run("run_dashboard_query", {
            "tile": "users/daily-trend-by-users", "scope": [{"column": "ResourceType", "values": ["Storage account"]}]}))
        self.assertFalse(out.is_error, out.content)
        self.assertIn('ResourceType in~ ("Storage account")', self.adx.calls[0])

    def test_scope_in_definition(self):
        tool = [t for t in self.runner().definitions() if t["name"] == "run_dashboard_query"][0]
        self.assertIn("ResourceName", tool["input_schema"]["properties"]["scope"]["items"]["properties"]["column"]["enum"])
        self.assertIn("`scope` filters cost rows before the tile's logic", tool["description"])

    def test_errors_reach_the_model(self):
        r = self.runner()
        bad_tile = asyncio.run(r.run("run_dashboard_query", {"tile": "nope"}))
        bad_param = asyncio.run(r.run("run_dashboard_query",
                                      {"tile": "users/daily-trend-by-users", "parameters": {"numberOfDays": -1}}))
        self.assertTrue(bad_tile.is_error and bad_param.is_error)
        self.assertIn("integer from 1", bad_param.content)
        self.assertEqual(self.adx.calls, [])


if __name__ == "__main__":
    unittest.main()

import json
import sys
import time
import unittest
from types import ModuleType
from types import SimpleNamespace as NS
from unittest import mock

from finops_nl import admin
from finops_nl.admin import Prices, filter_turns, load_turns, parse_record, render_page, summarize
from finops_nl.authorization import is_admin_user
from finops_nl.config import Settings
from finops_nl.logging_utils import RECENT_TURNS, log_event


def rec(**kw):
    base = {"ts": time.time(), "event": "agent_turn", "user": "a@x.com", "question": "cost by user?", "model": "gpt-5",
            "iterations": 2, "queries": 1, "hit_limit": False, "ms": 20000, "llm_ms": 19000, "input_tokens": 10000,
            "cached_tokens": 8000, "output_tokens": 1000, "reasoning_tokens": 500, "answer_chars": 300,
            "query_details": [{"tool": "run_dashboard_query", "tile": "users/x", "ok": True, "rows": 3, "ms": 120}],
            "other_tools": {}}
    return {**base, **kw}


S = Settings(adx_cluster_uri="x", adx_database="Hub")


class AdminDataTests(unittest.TestCase):
    def test_parse_record(self):
        self.assertEqual(parse_record(json.dumps(rec()))["user"], "a@x.com")
        for bad in (None, "", "not json", json.dumps({"event": "question"}), "[1]"):
            self.assertIsNone(parse_record(bad))

    def test_cost(self):
        p = Prices(input=1.25, cached=0.125, output=10)
        self.assertAlmostEqual(p.cost(rec()), (2000 * 1.25 + 8000 * 0.125 + 1000 * 10) / 1e6)
        self.assertFalse(Prices().enabled)
        self.assertEqual(Prices(1, 1, 1).cost({}), 0)  # old records without token fields

    def test_filter_and_summary(self):
        bad_q = {"tool": "run_kql", "kql": "x", "ok": False, "error": "SEM0100"}
        turns = [rec(), rec(user="b@x.com", hit_limit=True, ms=60000, query_details=[bad_q, bad_q]),
                 {"ts": 1, "event": "agent_turn", "iterations": 7, "queries": 8, "hit_limit": False}]  # old format
        self.assertEqual(len(filter_turns(turns, user="B@X")), 1)
        self.assertEqual([t.get("user") for t in filter_turns(turns, problems=True)], ["b@x.com"])
        s = summarize(turns, Prices())
        self.assertEqual(s["questions"], 3)
        self.assertEqual(s["hit_limit"], 1)
        self.assertAlmostEqual(s["failed_pct"], 100 * 2 / 3)  # 2 of 3 detailed queries failed
        self.assertAlmostEqual(s["median_s"], 40.0)          # old record has no ms: not counted
        self.assertIsNone(s["cost"])
        self.assertEqual(summarize([], Prices()), {})

    def test_memory_source_when_no_workspace(self):
        RECENT_TURNS.clear()
        log_event("agent_turn", **{k: v for k, v in rec().items() if k not in ("ts", "event")})
        log_event("question", text="ignored")
        turns, source = load_turns(S, 7)
        self.assertEqual((len(turns), source), (1, "this process (local)"))

    def test_log_analytics_source(self):
        sent = {}

        class Client:
            def __init__(self, cred):
                sent["cred"] = cred

            def query_workspace(self, ws, query, timespan):
                sent.update(ws=ws, query=query, timespan=timespan)
                rows = [[json.dumps(rec()), "rev-1"], ["garbage", "rev-1"]]
                return NS(tables=[NS(columns=["Log", "Rev"], rows=rows)])

        mq = ModuleType("azure.monitor.query")
        mq.LogsQueryClient = Client
        ai = ModuleType("azure.identity")
        ai.DefaultAzureCredential = lambda: "default-cred"
        fakes = {"azure": ModuleType("azure"), "azure.monitor": ModuleType("azure.monitor"),
                 "azure.monitor.query": mq, "azure.identity": ai}
        s = Settings(adx_cluster_uri="x", adx_database="Hub", admin_log_workspace_id="ws-1",
                     container_app_name="finops-nl")
        with mock.patch.dict(sys.modules, fakes):
            turns, source = load_turns(s, 30)
        self.assertEqual(source, "Log Analytics")
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["revision"], "rev-1")
        self.assertEqual((sent["ws"], sent["cred"], sent["timespan"].days), ("ws-1", "default-cred", 30))
        self.assertIn("App == 'finops-nl'", sent["query"])
        self.assertIn("ContainerAppConsoleLogs_CL", sent["query"])


class AdminAccessTests(unittest.TestCase):
    def test_allows_configured_sign_in_name(self):
        user = NS(identifier="Admin@Contoso.com", metadata={})
        self.assertTrue(is_admin_user(user, ("admin@contoso.com",), ()))

    def test_allows_configured_group_id(self):
        user = NS(identifier="member@contoso.com", metadata={"groups": ["AAAAAAAA-0000-4000-8000-00000000000A"]})
        self.assertTrue(is_admin_user(user, (), ("aaaaaaaa-0000-4000-8000-00000000000a",)))

    def test_rejects_unconfigured_user_or_group(self):
        user = NS(identifier="member@contoso.com", metadata={"groups": ["other-group"]})
        self.assertFalse(is_admin_user(user, (), ("aaaaaaaa-0000-4000-8000-00000000000a",)))
        self.assertFalse(is_admin_user(None, (), ()))


class FaviconTests(unittest.TestCase):
    def test_every_rendered_page_links_the_favicon(self):
        from finops_nl.admin import FAVICON, message_page
        page = render_page([], days=7, user="", problems=False, source="test", prices=Prices())
        self.assertIn(FAVICON, page)
        self.assertIn(FAVICON, message_page("Not authorized", "<p>x</p>"))
        self.assertIn('href="/favicon"', FAVICON)


class AdminRenderTests(unittest.TestCase):
    def page(self, turns, prices=Prices(), **kw):
        args = dict(days=7, user="", problems=False, source="test", prices=prices)
        return render_page(turns, **{**args, **kw})

    def test_escapes_user_content(self):
        out = self.page([rec(question="<script>alert(1)</script>", user='"><img>',
                             query_details=[{"tool": "run_kql", "kql": "<b>", "ok": False, "error": "<i>bad</i>"}])])
        self.assertNotIn("<script>alert", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn('"><img>', out)
        self.assertIn("&lt;i&gt;bad&lt;/i&gt;", out)
        self.assertIn("1 failed", out)

    def test_cost_column_only_with_prices(self):
        self.assertNotIn("Est. cost", self.page([rec()]))
        out = self.page([rec()], prices=Prices(1.25, 0.125, 10))
        self.assertIn("Est. cost", out)
        self.assertIn("<th>Cost</th>", out)

    def test_filters_and_empty_state(self):
        out = self.page([], days=30, user="bob", problems=True, error="Could not read logs: forbidden")
        self.assertIn("No questions in this period", out)
        self.assertIn("value='30' selected", out)
        self.assertIn('value="bob"', out)
        self.assertIn("checked", out)
        self.assertIn("Could not read logs: forbidden", out)

    def test_old_records_render(self):
        out = self.page([{"ts": 1, "event": "agent_turn", "iterations": 7, "queries": 8, "hit_limit": False}])
        self.assertIn("(not recorded)", out)


if __name__ == "__main__":
    unittest.main()

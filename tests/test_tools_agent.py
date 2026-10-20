import asyncio
import json
import sys
import unittest
from types import ModuleType
from types import SimpleNamespace as NS
from unittest import mock

from finops_nl.adx import AdxError, QueryResult
from finops_nl.agent import FOUNDRY_TOKEN_SCOPE, Agent, OpenAILLM, make_llm, to_openai_messages, trim_history
from finops_nl.config import Settings
from finops_nl.tools import ToolRunner, format_for_model

REFS = {"schema": "SCHEMA TEXT", "query_catalog": "CAT"}
S = Settings(adx_cluster_uri="https://x.kusto.windows.net", adx_database="Hub", llm_rows=3, max_tool_iterations=3)


class FakeAdx:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    def query(self, kql, **_):
        self.calls.append(kql)
        if self.error:
            raise AdxError(self.error)
        return self.result


def res(n=2):
    return QueryResult(["Svc", "Cost"], [[f"s{i}", float(i)] for i in range(n)], n, False, 5, "r1")


def run(coro):
    return asyncio.run(coro)


class ToolTests(unittest.TestCase):
    def test_run_kql_ok(self):
        adx = FakeAdx(res())
        out = run(ToolRunner(S, adx, REFS).run("run_kql", {"kql": "Costs_v1_2() | take 2"}))
        self.assertFalse(out.is_error)
        self.assertEqual(json.loads(out.content)["row_count"], 2)
        self.assertIn("| Svc | Cost |", out.preview)
        self.assertEqual(len(adx.calls), 1)

    def test_guard_rejection_never_reaches_adx(self):
        adx = FakeAdx(res())
        out = run(ToolRunner(S, adx, REFS).run("run_kql", {"kql": ".drop table X"}))
        self.assertTrue(out.is_error)
        self.assertEqual(adx.calls, [])

    def test_adx_error_is_returned_to_model(self):
        out = run(ToolRunner(S, FakeAdx(error="Semantic error: 'Foo' not found"), REFS).run("run_kql", {"kql": "Costs_v1_2() | project Foo"}))
        self.assertTrue(out.is_error)
        self.assertIn("Foo", out.content)

    def test_describe_entity_allowlist(self):
        r = ToolRunner(S, FakeAdx(res()), REFS)
        self.assertTrue(run(r.run("describe_entity", {"name": "Secrets()"})).is_error)
        ok = run(r.run("describe_entity", {"name": "Costs_v1_2()"}))
        self.assertFalse(ok.is_error)
        self.assertEqual(ok.content, "s0:0.0, s1:1.0")  # compact name:type list, every column

    def test_session_profile(self):
        from finops_nl.tools import session_profile
        cols = ["SubAccountName", "BillingCurrency", "Cost", "Rows"]
        rows = [["Payments-Prod", "EUR", 90.0, 10], ["Data-Dev", "EUR", 5.0, 4], ["Payments-Prod", "USD", 1.0, 2], ["", "EUR", 0.0, 1]]
        p = run(session_profile(FakeAdx(QueryResult(cols, rows, 4, False))))
        self.assertEqual(p.currency, "EUR")                                   # most rows
        self.assertEqual(p.subscriptions, [("Payments-Prod", 91.0), ("Data-Dev", 5.0)])  # summed, costliest first
        self.assertTrue(p.complete)
        self.assertFalse(run(session_profile(FakeAdx(QueryResult(cols, rows, 4, True)))).complete)
        failed = run(session_profile(FakeAdx(error="boom")))
        self.assertEqual((failed.currency, failed.subscriptions, failed.complete), (None, [], False))

    def test_match_names(self):
        from finops_nl.tools import match_names
        subs = [("Payments-Prod-001", 90.0), ("Payments-Dev", 10.0), ("Data Platform", 50.0), ("Azure subscription", 5.0)]
        self.assertEqual(match_names("payments prod", subs)[0], ["Payments-Prod-001", "similar", 90.0])
        self.assertEqual(match_names("PAYMENTS-PROD-001", subs)[0][1], "exact")
        self.assertEqual(match_names("data platform", subs)[0], ["Data Platform", "exact", 50.0])
        self.assertEqual(match_names("dataplatform", subs)[0][1], "similar")
        self.assertEqual(match_names("azure sub", subs)[0], ["Azure subscription", "contains", 5.0])
        self.assertEqual([m[0] for m in match_names("payments", subs)], ["Payments-Prod-001", "Payments-Dev"])
        self.assertEqual(match_names("hr", subs), [])

    def test_find_names_uses_session_subscriptions_without_a_query(self):
        from finops_nl.tools import SessionProfile
        adx = FakeAdx(res())
        prof = SessionProfile("USD", [("Payments-Prod", 9.0), ("Data-Dev", 1.0)], complete=True)
        r = ToolRunner(S, adx, REFS, profile=prof)
        hit = run(r.run("find_names", {"kind": "subscription", "text": "payments"}))
        self.assertIn("Payments-Prod", hit.content)
        miss = run(r.run("find_names", {"kind": "subscription", "text": "hr"}))
        self.assertIn("No subscription matches", miss.content)
        self.assertIn("Data-Dev", miss.content)
        self.assertEqual(adx.calls, [])                                        # all in memory
        run(r.run("find_names", {"kind": "resource", "text": "redis"}))         # resources still query ADX
        self.assertEqual(len(adx.calls), 1)
        partial = ToolRunner(S, FakeAdx(res()), REFS, profile=SessionProfile("USD", [("A", 1.0)], complete=False))
        run(partial.run("find_names", {"kind": "subscription", "text": "zzz"}))  # truncated list: ask ADX
        self.assertEqual(len(partial._adx.calls), 1)

    def test_get_reference_and_unknown_tool(self):
        r = ToolRunner(S, FakeAdx(res()), REFS)
        self.assertEqual(run(r.run("get_reference", {"topic": "schema"})).content, "SCHEMA TEXT")
        self.assertTrue(run(r.run("get_reference", {"topic": "nope"})).is_error)
        self.assertTrue(run(r.run("nope", {})).is_error)

    def test_reference_sections(self):
        from finops_nl.tools import load_references, split_sections
        refs = load_references(S)
        r = ToolRunner(S, FakeAdx(res()), refs)
        desc = [t for t in r.definitions() if t["name"] == "get_reference"][0]["description"]
        self.assertIn("query_catalog sections", desc)
        self.assertIn("Top services by cost", desc)
        self.assertIn("Cost metrics", desc)  # a schema section
        one = run(r.run("get_reference", {"topic": "query_catalog", "sections": ["top services by cost"]}))
        self.assertFalse(one.is_error)
        self.assertTrue(one.content.startswith("## Top services by cost"))
        self.assertIn("```kusto", one.content)
        self.assertLess(len(one.content), len(refs["query_catalog"]) / 5)
        two = run(r.run("get_reference", {"topic": "schema", "sections": ["## Time", "Cost metrics", "Time"]}))
        self.assertEqual(two.content.count("## Time"), 1)
        self.assertIn("### When BilledCost and EffectiveCost diverge", two.content)  # subsections kept
        bad = run(r.run("get_reference", {"topic": "schema", "sections": ["Nope"]}))
        self.assertTrue(bad.is_error)
        self.assertIn("Cost metrics", bad.content)
        whole = run(r.run("get_reference", {"topic": "weekly_report", "sections": ["Q1: Weekly cost total"]}))
        self.assertEqual(whole.content, refs["weekly_report"])  # not sectioned: always whole
        _, sections = split_sections(refs["query_catalog"])
        self.assertEqual(len(sections), 16)
        self.assertEqual(sum(v.count("```kusto") for v in sections.values()), refs["query_catalog"].count("```kusto"))

    def test_find_names_resolves_and_falls_back(self):
        from finops_nl.tools import find_names_kql
        q = find_names_kql("SubAccountName", 'Pay "ments" Prod')
        self.assertIn('let q = "pay \\"ments\\" prod";', q)               # escaped, lower-cased
        self.assertIn("by Value = tostring(SubAccountName)", q)
        self.assertNotIn("ResourceType", q)
        self.assertIn("Type = take_any(ResourceType)", find_names_kql("ResourceName", "x"))
        self.assertIn("by Value = tostring(ResourceType)", find_names_kql("ResourceType", "storage"))

    def test_find_names_project_reads_the_x_tag_first(self):
        from finops_nl.tools import NAME_KINDS, find_names_kql
        q = find_names_kql(NAME_KINDS["project"], "billing")
        self.assertIn("coalesce(tostring(Tags['x_project']), tostring(Tags['x_Project']), "
                      "tostring(Tags['project']), tostring(Tags['Project']))", q)
        self.assertEqual(NAME_KINDS["component"].count("Tags['"), 4)
        self.assertIn("Tags['x_Application']", NAME_KINDS["application"])
        from finops_nl.guard import validate_kql
        validate_kql(q, allowed_entities=S.allowed_entities)

        hit = FakeAdx(QueryResult(["Value", "Match", "Cost"], [["Azure subscription", "contains", 50.8]], 1, False))
        out = run(ToolRunner(S, hit, REFS).run("find_names", {"kind": "subscription", "text": "azure sub"}))
        self.assertFalse(out.is_error)
        self.assertIn("use these exact values", out.content)
        self.assertEqual(len(hit.calls), 1)

        class Seq(FakeAdx):
            def __init__(self, results):
                super().__init__()
                self.results = list(results)

            def query(self, kql, **_):
                self.calls.append(kql)
                return self.results.pop(0)
        miss = Seq([QueryResult(["Value", "Match", "Cost"], [], 0, False),
                    QueryResult(["Value", "Cost"], [["Prod", 9.0]], 1, False)])
        out = run(ToolRunner(S, miss, REFS).run("find_names", {"kind": "subscription", "text": "payments"}))
        self.assertIn("No subscription matches", out.content)
        self.assertIn("Prod", out.content)
        self.assertEqual(len(miss.calls), 2)
        for bad in ({"kind": "tag", "text": "x"}, {"kind": "subscription", "text": ""}, {"kind": "subscription", "text": "a\nb"}):
            self.assertTrue(run(ToolRunner(S, FakeAdx(res()), REFS).run("find_names", bad)).is_error)

    def test_result_shrinks_for_model(self):
        big = QueryResult(["a"], [["x" * 50] for _ in range(400)], 400, False)
        text = format_for_model(big, max_rows=400, max_chars=3000)
        data = json.loads(text)
        self.assertLessEqual(len(text), 3500)
        self.assertLess(data["rows_shown"], 400)
        self.assertIn("note", data)


def block(**kw):
    return NS(**kw)


class FakeLLM:
    def __init__(self, scripted):
        self.scripted, self.seen = list(scripted), []

    async def create(self, **kw):
        self.seen.append(kw)
        return self.scripted.pop(0)


def tool_use(id_, kql):
    return NS(content=[block(type="tool_use", id=id_, name="run_kql", input={"kql": kql})])


def text(t):
    return NS(content=[block(type="text", text=t)])


class AgentTests(unittest.TestCase):
    def test_thinking_blocks_go_back_unchanged_and_tools_stay_fixed(self):
        class Thinking:
            type = "thinking"

            def model_dump(self, **_):
                return {"type": "thinking", "thinking": "", "signature": "sig-1"}
        first = NS(content=[Thinking(), block(type="tool_use", id="t1", name="run_kql",
                                              input={"kql": "Costs_v1_2() | take 2"})])
        llm = FakeLLM([first, text("You spent 1.")])
        runner = ToolRunner(S, FakeAdx(res()), REFS)
        calls = []
        orig = runner.definitions
        runner.definitions = lambda: calls.append(1) or orig()
        hist = []
        turn = run(Agent(S, llm, runner, system=[{"type": "text", "text": "sys"}]).ask(hist, "how much?"))
        self.assertEqual(turn.answer, "You spent 1.")
        replayed = llm.seen[1]["messages"][1]["content"]  # the first assistant message, sent back in round 2
        self.assertEqual(replayed[0], {"type": "thinking", "thinking": "", "signature": "sig-1"})
        self.assertEqual(len(calls), 1)  # tool list built once per turn
        self.assertIs(llm.seen[0]["tools"], llm.seen[1]["tools"])
        self.assertNotIn("thinking", str(hist))  # stored history keeps only question and answer

    def test_tool_then_answer(self):
        adx = FakeAdx(res())
        llm = FakeLLM([tool_use("t1", "Costs_v1_2() | take 2"), text("You spent 1.")])
        events = []

        async def on_event(kind, o):
            events.append((kind, o.name))

        hist = []
        turn = run(Agent(S, llm, ToolRunner(S, adx, REFS), system=[{"type": "text", "text": "sys"}]).ask(hist, "how much?", on_event))
        self.assertEqual(turn.answer, "You spent 1.")
        self.assertEqual(turn.kql, ["Costs_v1_2() | take 2"])
        self.assertEqual(events, [("tool_result", "run_kql")])
        # history keeps the question and answer, plus a note of the query; tool results are dropped
        self.assertEqual([m["role"] for m in hist], ["user", "assistant"])
        self.assertEqual(hist[1]["content"][0], {"type": "text", "text": "You spent 1."})
        self.assertIn("run_kql: Costs_v1_2() | take 2", hist[1]["content"][1]["text"])

    def test_turn_record_for_admin_page(self):
        from finops_nl.logging_utils import RECENT_TURNS
        llm = FakeLLM([tool_use("a", "search 'x'"), tool_use("b", "Costs_v1_2() | take 1"), text("done")])
        run(Agent(S, llm, ToolRunner(S, FakeAdx(res()), REFS, user="ann@x.com"), system=[]).ask([], "what?"))
        r = RECENT_TURNS[-1]
        self.assertEqual((r["user"], r["question"], r["iterations"], r["queries"]), ("ann@x.com", "what?", 3, 2))
        bad, good = r["query_details"]
        self.assertEqual((bad["ok"], good["ok"], good["rows"]), (False, True, 2))
        self.assertIn("not allowed", bad["error"])
        self.assertEqual(good["kql"], "Costs_v1_2() | take 1")

    def test_follow_up_sees_compacted_history(self):
        llm = FakeLLM([tool_use("t1", "Costs_v1_2() | take 2"), text("A1"), text("A2")])
        agent = Agent(S, llm, ToolRunner(S, FakeAdx(res()), REFS), system=[])
        hist = []
        run(agent.ask(hist, "q1"))
        run(agent.ask(hist, "q2"))
        sent = llm.seen[-1]["messages"][:3]  # the agent appends to this list after the call
        self.assertEqual([m["role"] for m in sent], ["user", "assistant", "user"])
        self.assertEqual(sent[2]["content"], "q2")
        self.assertNotIn("tool_result", json.dumps(sent))
        self.assertEqual(len(hist), 4)

    def test_usage_is_summed_and_budget_stops_the_loop(self):
        def with_usage(r, **u):
            r.usage = {"input": 0, "cached": 0, "output": 0, "reasoning": 0, **u}
            return r
        s = Settings(adx_cluster_uri="x", adx_database="Hub", max_tool_iterations=5, max_turn_tokens=1000)
        llm = FakeLLM([with_usage(tool_use(str(i), "Costs_v1_2() | take 1"), input=700, cached=600, output=100)
                       for i in range(5)])
        hist = []
        turn = run(Agent(s, llm, ToolRunner(s, FakeAdx(res()), REFS), system=[]).ask(hist, "q"))
        self.assertTrue(turn.hit_limit)
        self.assertEqual(turn.iterations, 2)  # 800 tokens after one call, 1600 after two: stop
        self.assertEqual(turn.usage, {"input": 1400, "cached": 1200, "output": 200, "reasoning": 0})
        self.assertEqual([m["role"] for m in hist], ["user", "assistant"])

    def test_anthropic_usage_counts_cache_reads_as_input(self):
        from finops_nl.agent import usage_of
        u = NS(input_tokens=100, cache_read_input_tokens=900, cache_creation_input_tokens=0, output_tokens=50)
        self.assertEqual(usage_of(NS(usage=u)), {"input": 1000, "cached": 900, "output": 50, "reasoning": 0})
        self.assertEqual(usage_of(NS(content=[])), {})

    def test_on_text_is_passed_only_when_streaming(self):
        llm = FakeLLM([text("a"), text("b")])
        agent = Agent(S, llm, ToolRunner(S, FakeAdx(res()), REFS), system=[])
        run(agent.ask([], "q"))

        async def on_text(t):
            pass
        run(agent.ask([], "q", on_text=on_text))
        self.assertNotIn("on_text", llm.seen[0])
        self.assertIs(llm.seen[1]["on_text"], on_text)

    def test_system_prompt_subscriptions(self):
        from finops_nl.agent import build_system_prompt, subscriptions_line
        self.assertIn('one subscription, "Azure subscription"', subscriptions_line([("Azure subscription", 5.0)]))
        many = subscriptions_line([("A", 2.0), ("B", 1.0)])
        self.assertIn('"A", "B"', many)
        self.assertIn("find_names", subscriptions_line([(f"s{i}", 1.0) for i in range(51)]))
        self.assertIn("not loaded", subscriptions_line([]))
        text = build_system_prompt(subscriptions=[("A", 2.0), ("B", 1.0)])[0]["text"]
        self.assertIn('"A", "B"', text)
        self.assertNotIn("{subscriptions}", text)

    def test_system_prompt_has_currency(self):
        from finops_nl.agent import build_system_prompt
        self.assertIn("**EUR**", build_system_prompt(currency="EUR")[0]["text"])
        self.assertIn("**unknown**", build_system_prompt()[0]["text"])

    def test_model_can_recover_from_rejection(self):
        adx = FakeAdx(res())
        llm = FakeLLM([tool_use("a", "search 'x'"), tool_use("b", "Costs_v1_2() | take 1"), text("done")])
        turn = run(Agent(S, llm, ToolRunner(S, adx, REFS), system=[]).ask([], "q"))
        self.assertEqual(turn.answer, "done")
        self.assertTrue(turn.outcomes[0].is_error)
        self.assertFalse(turn.outcomes[1].is_error)

    def test_iteration_limit_keeps_history_valid(self):
        adx = FakeAdx(res())
        llm = FakeLLM([tool_use(str(i), "Costs_v1_2() | take 1") for i in range(3)])
        hist = []
        turn = run(Agent(S, llm, ToolRunner(S, adx, REFS), system=[]).ask(hist, "q"))
        self.assertTrue(turn.hit_limit)
        self.assertEqual([m["role"] for m in hist], ["user", "assistant"])
        self.assertEqual(hist[-1]["content"][0]["type"], "text")
        self.assertIn("allowed number of steps", hist[-1]["content"][0]["text"])

    def test_trim_history_does_not_split_tool_pairs(self):
        msgs = []
        for i in range(5):
            msgs += [
                {"role": "user", "content": f"q{i}"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": str(i), "name": "run_kql", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": str(i), "content": "x"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "a"}]},
            ]
        out = trim_history(msgs, 2)
        self.assertEqual(out[0]["content"], "q3")
        self.assertEqual(len(out), 8)



class MakeLlmTests(unittest.TestCase):
    """make_llm picks the client; fake SDK modules so the test needs neither anthropic nor azure-identity."""

    def setUp(self):
        self.calls = {}
        calls = self.calls
        anthropic = ModuleType("anthropic")
        anthropic.AsyncAnthropic = lambda **kw: calls.setdefault("anthropic", kw) or "client"
        anthropic.AsyncAnthropicFoundry = lambda **kw: calls.setdefault("foundry", kw) or "client"
        openai = ModuleType("openai")
        openai.AsyncOpenAI = lambda **kw: calls.setdefault("openai", kw) or "client"
        openai.AsyncAzureOpenAI = lambda **kw: calls.setdefault("azure_openai", kw) or "client"
        aio = ModuleType("azure.identity.aio")
        aio.DefaultAzureCredential = lambda: "cred"
        aio.get_bearer_token_provider = lambda cred, scope: calls.setdefault("token", (cred, scope)) and "provider"
        fakes = {"anthropic": anthropic, "openai": openai, "azure": ModuleType("azure"), "azure.identity": ModuleType("azure.identity"),
                 "azure.identity.aio": aio}
        patcher = mock.patch.dict(sys.modules, fakes)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _settings(self, **kw):
        return Settings(adx_cluster_uri="https://x", adx_database="Hub", **kw)

    def test_anthropic_default(self):
        make_llm(self._settings())
        self.assertIn("anthropic", self.calls)
        self.assertNotIn("foundry", self.calls)

    def test_foundry_entra_uses_token_not_key(self):
        make_llm(self._settings(llm_provider="foundry", foundry_resource="res"))
        self.assertEqual(self.calls["token"], ("cred", FOUNDRY_TOKEN_SCOPE))
        self.assertEqual(self.calls["foundry"], {"resource": "res", "azure_ad_token_provider": "provider"})

    def test_foundry_key(self):
        make_llm(self._settings(llm_provider="foundry", foundry_resource="res", foundry_auth="key", foundry_api_key="k"))
        self.assertEqual(self.calls["foundry"], {"resource": "res", "api_key": "k"})
        self.assertNotIn("token", self.calls)

    def test_no_temperature_sent(self):
        client = NS(messages=NS(create=None))
        sent = {}

        async def create(**kw):
            sent.update(kw)
        client.messages.create = create
        from finops_nl.agent import AnthropicLLM
        asyncio.run(AnthropicLLM(client=client).create(model="m", system=[], tools=[], messages=[], max_tokens=5))
        self.assertNotIn("temperature", sent)

    def test_anthropic_request_settings(self):
        sent = {}

        async def create(**kw):
            sent.update(kw)
        client = NS(messages=NS(create=create), beta=NS(messages=NS(create=create)))
        from finops_nl.agent import AnthropicLLM
        asyncio.run(AnthropicLLM(client=client).create(model="m", system=[], tools=[], messages=[], max_tokens=2000))
        self.assertEqual(sent["max_tokens"], AnthropicLLM.MIN_MAX_TOKENS)  # room for thinking
        self.assertNotIn("output_config", sent)
        self.assertNotIn("fallbacks", sent)
        sent.clear()
        asyncio.run(AnthropicLLM(client=client, effort="medium", fallbacks=True).create(
            model="m", system=[], tools=[], messages=[], max_tokens=2000))
        self.assertEqual(sent["output_config"], {"effort": "medium"})
        self.assertEqual((sent["fallbacks"], sent["betas"]), ("default", [AnthropicLLM.FALLBACK_BETA]))
    def test_openai_reasoning_effort_and_usage(self):
        client = FakeOpenAI([oa_resp(text="hi")])
        client.replies[0].usage = NS(prompt_tokens=1200, completion_tokens=300,
                                     prompt_tokens_details=NS(cached_tokens=1024),
                                     completion_tokens_details=NS(reasoning_tokens=250))
        out = asyncio.run(OpenAILLM(client=client, reasoning_effort="low").create(
            model="m", system=[], tools=[], messages=[], max_tokens=5))
        self.assertEqual(client.sent[0]["reasoning_effort"], "low")
        self.assertEqual(out.usage, {"input": 1200, "cached": 1024, "output": 300, "reasoning": 250})
        client = FakeOpenAI([oa_resp(text="hi")])
        asyncio.run(OpenAILLM(client=client).create(model="m", system=[], tools=[], messages=[], max_tokens=5))
        self.assertNotIn("reasoning_effort", client.sent[0])

    def test_openai_streaming_assembles_text_tool_calls_and_usage(self):
        def chunk(content=None, calls=None, usage=None):
            delta = NS(content=content, tool_calls=calls)
            return NS(choices=[] if usage else [NS(delta=delta)], usage=usage)

        def call(index, id=None, name=None, args=None):
            return NS(index=index, id=id, function=NS(name=name, arguments=args))

        chunks = [chunk("Let me "), chunk("check."),
                  chunk(calls=[call(0, "c1", "run_kql", '{"kql": "Costs')]),
                  chunk(calls=[call(0, args='_v1_2() | take 1"}')]),
                  chunk(calls=[call(1, "c2", "get_reference", '{"topic": "schema"}')]),
                  chunk(usage=NS(prompt_tokens=50, completion_tokens=20, prompt_tokens_details=None,
                                 completion_tokens_details=NS(reasoning_tokens=5)))]

        async def agen():
            for c in chunks:
                yield c

        sent = {}

        async def create(**kw):
            sent.update(kw)
            return agen()
        client = NS(chat=NS(completions=NS(create=create)))
        deltas = []

        async def on_text(t):
            deltas.append(t)
        out = asyncio.run(OpenAILLM(client=client).create(model="m", system=[], tools=[], messages=[],
                                                          max_tokens=5, on_text=on_text))
        self.assertTrue(sent["stream"])
        self.assertEqual(sent["stream_options"], {"include_usage": True})
        self.assertEqual(deltas, ["Let me ", "check."])
        self.assertEqual(out.content[0].text, "Let me check.")
        self.assertEqual([(b.id, b.name, b.input) for b in out.content[1:]],
                         [("c1", "run_kql", {"kql": "Costs_v1_2() | take 1"}), ("c2", "get_reference", {"topic": "schema"})])
        self.assertEqual(out.usage, {"input": 50, "cached": 0, "output": 20, "reasoning": 5})

    def test_anthropic_streaming(self):
        from finops_nl.agent import AnthropicLLM
        final = NS(content=[NS(type="text", text="Hi there")])

        class Stream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get_final_message(self):
                return final

        async def text_stream():
            for t in ("Hi", " there"):
                yield t
        stream = Stream()
        stream.text_stream = text_stream()
        client = NS(messages=NS(stream=lambda **kw: stream, create=None))
        deltas = []

        async def on_text(t):
            deltas.append(t)
        out = asyncio.run(AnthropicLLM(client=client).create(model="m", system=[], tools=[], messages=[],
                                                             max_tokens=5, on_text=on_text))
        self.assertIs(out, final)
        self.assertEqual(deltas, ["Hi", " there"])

    def test_azure_openai_entra_uses_token_not_key(self):
        s = self._settings(llm_provider="azure_openai", azure_openai_endpoint="https://r.openai.azure.com",
                           azure_openai_deployment="gpt-dep", azure_openai_api_key="leftover")
        llm = make_llm(s)
        self.assertIsInstance(llm, OpenAILLM)
        self.assertEqual(self.calls["token"], ("cred", FOUNDRY_TOKEN_SCOPE))
        kw = self.calls["azure_openai"]
        self.assertEqual((kw["azure_endpoint"], kw["azure_ad_token_provider"]), ("https://r.openai.azure.com", "provider"))
        self.assertNotIn("api_key", kw)
        self.assertEqual(s.model, "gpt-dep")
        self.assertNotIn("openai", self.calls)

    def test_azure_openai_key(self):
        make_llm(self._settings(llm_provider="azure_openai", azure_openai_endpoint="https://r.openai.azure.com",
                                azure_openai_deployment="d", azure_openai_auth="key", azure_openai_api_key="k"))
        kw = self.calls["azure_openai"]
        self.assertEqual(kw["api_key"], "k")
        self.assertIn("api_version", kw)
        self.assertNotIn("token", self.calls)

    def test_openai(self):
        s = self._settings(llm_provider="openai", openai_model="gpt-x", anthropic_model="claude-y")
        self.assertIsInstance(make_llm(s), OpenAILLM)
        self.assertNotIn("anthropic", self.calls)
        self.assertEqual(s.model, "gpt-x")
        self.assertEqual(self._settings().model, "claude-sonnet-5-5")


def oa_resp(text=None, calls=()):
    tool_calls = [NS(id=i, function=NS(name=n, arguments=a)) for i, n, a in calls] or None
    return NS(choices=[NS(message=NS(content=text, tool_calls=tool_calls))])


class FakeOpenAI:
    def __init__(self, replies):
        self.replies, self.sent = list(replies), []
        self.chat = NS(completions=NS(create=self._create))

    async def _create(self, **kw):
        self.sent.append(kw)
        return self.replies.pop(0)


class OpenAIAdapterTests(unittest.TestCase):
    def test_message_conversion(self):
        msgs = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "checking"},
                {"type": "tool_use", "id": "c1", "name": "run_kql", "input": {"kql": "T | take 1"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": "bad", "is_error": True}]},
            {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        ]
        out = to_openai_messages([{"type": "text", "text": "SYS", "cache_control": {}}], msgs)
        self.assertEqual(out[0], {"role": "system", "content": "SYS"})
        self.assertEqual(out[1], {"role": "user", "content": "q"})
        self.assertEqual(out[2]["content"], "checking")
        self.assertEqual(out[2]["tool_calls"][0]["function"],
                         {"name": "run_kql", "arguments": json.dumps({"kql": "T | take 1"})})
        self.assertEqual(out[3], {"role": "tool", "tool_call_id": "c1", "content": "ERROR: bad"})
        self.assertEqual(out[4], {"role": "assistant", "content": "done"})

    def test_agent_loop_through_adapter(self):
        client = FakeOpenAI([
            oa_resp(calls=[("c1", "run_kql", '{"kql": "Costs_v1_2() | take 2"}')]),
            oa_resp(text="Total is 1."),
        ])
        adx = FakeAdx(res())
        s = Settings(adx_cluster_uri="https://x", adx_database="Hub", llm_provider="openai", openai_model="gpt-x")
        history = []
        turn = run(Agent(s, OpenAILLM(client=client), ToolRunner(s, adx, REFS), system=[]).ask(history, "cost?"))
        self.assertEqual(turn.answer, "Total is 1.")
        self.assertEqual(adx.calls, ["Costs_v1_2() | take 2"])
        first, second = client.sent
        self.assertEqual(first["model"], "gpt-x")
        self.assertIn("run_kql", [t["function"]["name"] for t in first["tools"]])
        self.assertGreaterEqual(first["max_completion_tokens"], OpenAILLM.MIN_COMPLETION_TOKENS)
        self.assertNotIn("temperature", first)
        self.assertEqual(second["messages"][-1]["role"], "tool")
        self.assertEqual(second["messages"][-1]["tool_call_id"], "c1")

    def test_bad_tool_arguments_become_empty_input(self):
        client = FakeOpenAI([oa_resp(calls=[("c1", "run_kql", "{not json")])])
        out = run(OpenAILLM(client=client).create(model="m", system=[], tools=[], messages=[], max_tokens=5))
        self.assertEqual(out.content[0].input, {})

class AdxRequestPropertiesTests(unittest.TestCase):
    def test_servertimeout_accepted_by_sdk(self):
        """The Kusto SDK adds servertimeout to a timedelta; a string option broke every query."""
        try:
            from azure.kusto.data.client_base import ExecuteRequestParams
            from azure.kusto.data.client_details import ClientDetails
        except ImportError:
            self.skipTest("azure-kusto-data not installed")
        import datetime as dt
        from finops_nl.adx import AdxClient

        captured = {}

        class Client:
            def execute(self, db, kql, props):
                captured["params"] = ExecuteRequestParams._from_query(
                    kql, db, props, {}, dt.timedelta(minutes=4), dt.timedelta(minutes=10),
                    dt.timedelta(seconds=30), ClientDetails(None, None))
                return NS(primary_results=[])

        adx = AdxClient(Settings(adx_cluster_uri="https://x", adx_database="Hub", query_timeout_s=60))
        adx._client = Client()
        adx.query("T | take 1")
        self.assertEqual(captured["params"].timeout, dt.timedelta(seconds=90))
        self.assertIn('"servertimeout": "0:01:00"', captured["params"].json_payload["properties"])


if __name__ == "__main__":
    unittest.main()

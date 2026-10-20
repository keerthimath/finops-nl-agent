"""A2A: caller authentication (delegated user tokens only), per-user conversations, and the endpoint end to end.

The SDK tests skip when a2a-sdk / FastAPI aren't installed; the security checks above them always run.
"""
import time
import unittest
from types import SimpleNamespace as NS

from finops_nl.a2a_core import (APP_ONLY_REFUSED, A2AAuthError, Conversations, Principal, TokenValidator,
                                bearer_token, check_claims, query_results)
from finops_nl.adx import QueryResult
from finops_nl.config import Settings
from finops_nl.tools import ToolOutcome

TENANT, CLIENT = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"


def user_claims(**kw):
    return {"tid": TENANT, "scp": "access_as_user", "preferred_username": "Ann@Contoso.com", **kw}


class ClaimTests(unittest.TestCase):
    def test_delegated_token_gives_lowercase_identity(self):
        self.assertEqual(check_claims(user_claims(), TENANT), "ann@contoso.com")

    def test_v1_token_uses_upn(self):
        claims = user_claims(preferred_username=None, upn="Bob@Contoso.com")
        self.assertEqual(check_claims(claims, TENANT), "bob@contoso.com")

    def test_microsoft_account_guest_uses_email(self):
        claims = user_claims(preferred_username=None, email="Kim@Example.com", unique_name="live.com#Kim@Example.com")
        self.assertEqual(check_claims(claims, TENANT), "kim@example.com")

    def test_app_only_token_is_refused(self):
        claims = {"tid": TENANT, "roles": ["Costs.Read"], "appid": "x"}  # client credentials: no scp, no user
        with self.assertRaisesRegex(A2AAuthError, "App-only"):
            check_claims(claims, TENANT)
        self.assertIn("delegated", APP_ONLY_REFUSED)

    def test_other_tenant_and_missing_scope_and_identity(self):
        for claims in (user_claims(tid="33333333-0000-0000-0000-000000000000"), user_claims(scp="User.Read"),
                       user_claims(preferred_username="")):
            with self.assertRaises(A2AAuthError):
                check_claims(claims, TENANT)

    def test_bearer_header(self):
        self.assertEqual(bearer_token("Bearer abc.def"), "abc.def")
        for bad in (None, "", "Basic abc", "Bearer  "):
            with self.assertRaises(A2AAuthError):
                bearer_token(bad)


try:
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
except ImportError:  # pragma: no cover
    jwt = None


@unittest.skipIf(jwt is None, "PyJWT / cryptography not installed")
class TokenValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = cls.key.public_key()
        cls.validator = TokenValidator(TENANT, CLIENT, jwks_client=NS(get_signing_key_from_jwt=lambda _t: NS(key=public)))

    def token(self, key=None, alg="RS256", **claims):
        now = int(time.time())
        body = {"iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "aud": CLIENT, "iat": now,
                "nbf": now, "exp": now + 3600, **user_claims(), **claims}
        return jwt.encode(body, key or self.key, algorithm=alg)

    def test_valid_v2_and_v1_tokens(self):
        self.assertEqual(self.validator.validate(self.token()).identity, "ann@contoso.com")
        v1 = self.token(iss=f"https://sts.windows.net/{TENANT}/", aud=f"api://{CLIENT}")
        p = self.validator.validate(v1)
        self.assertEqual((p.identity, p.token), ("ann@contoso.com", v1))

    def test_rejects_forged_expired_and_foreign_tokens(self):
        cases = {
            "wrong signing key": self.token(key=self.other_key),
            "expired": self.token(exp=int(time.time()) - 3600),
            "other audience": self.token(aud="api://some-other-app"),
            "other issuer": self.token(iss="https://login.microsoftonline.com/common/v2.0"),
            "app-only": self.token(scp=None, roles=["Costs.Read"]),
            "HS256 with a guessed secret": jwt.encode({**user_claims(), "aud": CLIENT}, "secret" * 6, algorithm="HS256"),
            "not a jwt": "abc",
        }
        for name, tok in cases.items():
            with self.subTest(name), self.assertRaises(A2AAuthError):
                self.validator.validate(tok)


class ConversationTests(unittest.TestCase):
    def test_context_ids_are_per_user(self):
        convs = Conversations()
        convs.get("ann@x.com", "ctx1").history.append("ann's turn")
        self.assertEqual(convs.get("bob@x.com", "ctx1").history, [])  # same contextId, other user: nothing
        self.assertEqual(convs.get("ANN@x.com", "ctx1").history, ["ann's turn"])

    def test_least_recently_used_and_idle_conversations_go(self):
        now = [0.0]
        convs = Conversations(max_items=2, ttl_s=100, clock=lambda: now[0])
        convs.get("a", "1").history.append(1)
        convs.get("a", "2")
        convs.get("a", "1")
        convs.get("a", "3")  # evicts ("a", "2"), the least recently used
        self.assertEqual(len(convs), 2)
        self.assertEqual(convs.get("a", "1").history, [1])
        now[0] = 500
        self.assertEqual(convs.get("a", "1").history, [])  # idle past the TTL


class QueryResultsTests(unittest.TestCase):
    def test_successful_queries_only_with_capped_rows(self):
        r = QueryResult(["Svc", "Cost"], [[f"s{i}", float(i)] for i in range(5)], 5, False)
        turn = NS(outcomes=[
            ToolOutcome("run_kql", "{}", kql="Costs_v1_2() | take 5", result=r),
            ToolOutcome("run_kql", "bad", is_error=True, kql="x"),
            ToolOutcome("run_dashboard_query", "{}", result=r, source="Dashboard tile: Top", repeat=True),
            ToolOutcome("get_reference", "text"),
            ToolOutcome("run_dashboard_query", "{}", kql="tile kql", result=r, source="Dashboard tile: Top"),
        ])
        out = query_results(turn, max_rows=3)["queries"]
        self.assertEqual([q["source"] for q in out], ["KQL (written by the assistant)", "Dashboard tile: Top"])
        self.assertEqual(len(out[0]["rows"]), 3)
        self.assertTrue(out[0]["truncated"])
        self.assertEqual(out[0]["columns"], ["Svc", "Cost"])


class SettingsTests(unittest.TestCase):
    def test_off_by_default_and_needs_https_url_with_obo(self):
        base = {"ADX_CLUSTER_URI": "https://x", "AUTH_MODE": "obo", "ENTRA_TENANT_ID": TENANT,
                "ENTRA_CLIENT_ID": CLIENT, "ENTRA_CLIENT_SECRET": "s"}
        self.assertFalse(Settings.from_env(base).a2a_enabled)
        with self.assertRaisesRegex(ValueError, "CHAINLIT_URL"):
            Settings.from_env({**base, "A2A_ENABLED": "true"}).validate()
        s = Settings.from_env({**base, "A2A_ENABLED": "true", "CHAINLIT_URL": "https://app.example/"})
        s.validate()
        self.assertEqual(s.public_url, "https://app.example")


try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from finops_nl.a2a_server import A2A_PATH, FinOpsExecutor, add_routes
except ImportError:  # pragma: no cover
    FastAPI = None


class FakeAdx:
    """The profile query gets a currency + subscription; everything else gets a two-row cost table."""

    def __init__(self):
        self.calls = []

    def query(self, kql, **_):
        self.calls.append(kql)
        if "BillingCurrency" in kql:
            return QueryResult(["SubAccountName", "BillingCurrency", "Cost", "Rows"], [["Prod", "USD", 10.0, 5]], 1, False)
        return QueryResult(["Svc", "Cost"], [["Storage", 7.5], ["Compute", 2.5]], 2, False)


class ScriptedLLM:
    """Each question: one run_kql call, then an answer that echoes the question."""

    def __init__(self):
        self.seen = []

    async def create(self, *, messages, **_):
        self.seen.append(messages)
        last = messages[-1]["content"]
        if isinstance(last, str):
            return NS(content=[NS(type="tool_use", id=f"t{len(self.seen)}", name="run_kql",
                                  input={"kql": "Costs_v1_2() | summarize Cost = sum(EffectiveCost) by Svc"})])
        question = next(m["content"] for m in reversed(messages) if m["role"] == "user" and isinstance(m["content"], str))
        return NS(content=[NS(type="text", text=f"Answer to: {question}")])


class FakeValidator:
    """Token "ann" / "bob" = that user; anything else is rejected."""

    def validate(self, token):
        if token not in ("ann", "bob"):
            raise A2AAuthError("Invalid token: bad signature")
        return Principal(f"{token}@contoso.com", token)


@unittest.skipIf(FastAPI is None, "a2a-sdk[http-server] / fastapi not installed")
class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(adx_cluster_uri="https://x", adx_database="Hub", auth_mode="obo",
                                 entra_tenant_id=TENANT, entra_client_id=CLIENT, entra_client_secret="s",
                                 a2a_enabled=True, public_url="https://app.example")
        self.adx, self.llm = FakeAdx(), ScriptedLLM()
        self.principals = []

        def adx_for(principal):
            self.principals.append(principal)
            return self.adx

        app = FastAPI()

        @app.get("/{full_path:path}")  # stands in for Chainlit's catch-all UI route
        async def ui(full_path: str):
            return {"ui": full_path}

        self.executor = FinOpsExecutor(self.settings, self.llm, {"schema": "S", "query_catalog": "C"}, None, None,
                                       adx_factory=adx_for)
        add_routes(app, self.settings, self.executor, validator=FakeValidator())
        self.client = TestClient(app)

    def send(self, text, token="ann", context_id=None, method="SendMessage", accept=("text/markdown", "application/json")):
        msg = {"messageId": f"m{time.time_ns()}", "role": "ROLE_USER", "parts": [{"text": text}]}
        if context_id:
            msg["contextId"] = context_id
        headers = {"A2A-Version": "1.0"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return self.client.post(A2A_PATH, headers=headers,
                                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {
                                    "message": msg, "configuration": {"acceptedOutputModes": list(accept)}}})

    def test_card_is_public_and_ahead_of_the_ui_route(self):
        card = self.client.get("/.well-known/agent-card.json").json()
        self.assertEqual(card["name"], "FinOps Assistant")
        self.assertEqual(card["supportedInterfaces"][0]["url"], "https://app.example/a2a")
        self.assertIn(f"api://{CLIENT}/access_as_user", str(card["securityRequirements"]))
        self.assertEqual(self.client.get("/a2a/.well-known/agent-card.json").json(), card)  # next to the endpoint
        self.assertEqual(self.client.get("/anything").json(), {"ui": "anything"})

    def test_no_or_bad_token_is_401_and_nothing_runs(self):
        for token in (None, "forged"):
            r = self.send("What did we spend?", token=token)
            self.assertEqual(r.status_code, 401)
            self.assertIn("Bearer", r.headers["www-authenticate"])
        self.assertEqual((self.adx.calls, self.llm.seen), ([], []))

    def test_answer_rows_and_user_token_reach_adx(self):
        r = self.send("What did we spend last month?")
        self.assertEqual(r.status_code, 200, r.text)
        task = r.json()["result"]["task"]
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED")
        arts = {a["name"]: a for a in task["artifacts"]}
        self.assertEqual(arts["answer"]["parts"][0]["text"], "Answer to: What did we spend last month?")
        rows = arts["query_results"]["parts"][0]["data"]["queries"][0]
        self.assertEqual(rows["rows"], [["Storage", 7.5], ["Compute", 2.5]])
        self.assertEqual({p.token for p in self.principals}, {"ann"})  # the OBO assertion is the caller's token

    def test_text_only_callers_get_no_data_part(self):
        for accept in (("text/plain",), ()):  # e.g. Foundry's A2A tool rejects any non-text part
            task = self.send("Spend?", accept=accept).json()["result"]["task"]
            self.assertEqual([a["name"] for a in task["artifacts"]], ["answer"])

    def test_follow_up_shares_history_only_within_the_users_context(self):
        first = self.send("Spend by service?").json()["result"]["task"]
        ctx = first["contextId"]
        self.send("And last week?", context_id=ctx)
        self.assertIn("Spend by service?", str(self.llm.seen[-1]))  # ann's follow-up sees her first question
        self.send("Hello?", token="bob", context_id=ctx)
        self.assertNotIn("Spend by service?", str(self.llm.seen[-1]))  # bob reusing her contextId sees nothing

    def test_tasks_are_private_to_their_user(self):
        task_id = self.send("Spend?").json()["result"]["task"]["id"]

        def get(token):
            return self.client.post(A2A_PATH, headers={"A2A-Version": "1.0", "Authorization": f"Bearer {token}"},
                                    json={"jsonrpc": "2.0", "id": 2, "method": "GetTask", "params": {"id": task_id}})
        self.assertEqual(get("ann").json()["result"]["id"], task_id)
        self.assertIn("error", get("bob").json())

    def test_streaming_reports_queries_then_the_answer(self):
        r = self.send("Spend?", method="SendStreamingMessage")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Ran: KQL (written by the assistant)", r.text)
        self.assertIn("Answer to: Spend?", r.text)
        self.assertIn("TASK_STATE_COMPLETED", r.text)

    def test_v0_3_clients_work_on_the_same_endpoint(self):
        msg = {"messageId": "m03", "role": "user", "kind": "message", "parts": [{"kind": "text", "text": "Spend?"}]}
        r = self.client.post(A2A_PATH, headers={"Authorization": "Bearer ann"},
                             json={"jsonrpc": "2.0", "id": 3, "method": "message/send", "params": {"message": msg}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["result"]["status"]["state"], "completed")


if __name__ == "__main__":
    unittest.main()

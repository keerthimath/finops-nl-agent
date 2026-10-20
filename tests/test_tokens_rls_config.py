import asyncio
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from finops_nl.auth import JWT_BEARER, ClientAuth  # noqa: E402
from finops_nl.config import Settings  # noqa: E402
from finops_nl.session_tokens import AuthExpired, TokenSet, TokenStore, jwt_claims  # noqa: E402
import gen_rls  # noqa: E402


def run(c):
    return asyncio.run(c)


class TokenStoreTests(unittest.TestCase):
    def test_fresh_token_returned(self):
        s = TokenStore()
        s.put("A@x.com", TokenSet("tok", "ref", time.time() + 3600))
        self.assertEqual(run(s.get_access_token("a@x.com")), "tok")

    def test_refresh_on_expiry(self):
        s = TokenStore()
        s.put("a@x.com", TokenSet("old", "ref", time.time() - 10))

        async def refresher(r):
            self.assertEqual(r, "ref")
            return TokenSet("new", None, time.time() + 3600)

        self.assertEqual(run(s.get_access_token("a@x.com", refresher)), "new")
        self.assertEqual(run(s.get_access_token("a@x.com", refresher)), "new")  # refresh token retained

    def test_expired_without_refresh_raises_and_drops(self):
        s = TokenStore()
        s.put("a@x.com", TokenSet("old", None, time.time() - 10))
        with self.assertRaises(AuthExpired):
            run(s.get_access_token("a@x.com"))
        with self.assertRaises(AuthExpired):
            run(s.get_access_token("a@x.com"))

    def test_refresh_failure_raises(self):
        s = TokenStore()
        s.put("a@x.com", TokenSet("old", "ref", time.time() - 10))

        async def boom(_):
            raise ValueError("nope")

        with self.assertRaises(AuthExpired):
            run(s.get_access_token("a@x.com", boom))

    def test_jwt_claims(self):
        import base64, json
        p = base64.urlsafe_b64encode(json.dumps({"preferred_username": "u@x.com"}).encode()).decode().rstrip("=")
        self.assertEqual(jwt_claims(f"h.{p}.s")["preferred_username"], "u@x.com")
        self.assertEqual(jwt_claims("garbage"), {})


class RlsTests(unittest.TestCase):
    CFG = {
        "table": "Costs_final_v1_2",
        "allow_all": ["aadgroup=g1;t1"],
        "teams": [
            {"name": "Team A", "principal": "aadgroup=g2;t1", "subscriptions": ["AAA-1", "bbb-2"]},
            {"name": "team-b", "principal": "aadgroup=g3;t1", "subscriptions": ["ccc-3"]},
        ],
    }

    def test_generates_expected_kql(self):
        out = gen_rls.generate(self.CFG)
        self.assertIn("current_principal_is_member_of('aadgroup=g1;t1')", out)
        self.assertIn('dynamic(["aaa-1", "bbb-2"])', out)  # lower-cased
        self.assertIn("array_concat(sub_team_a, sub_team_b)", out)
        self.assertIn("set_has_element(allowed, tolower(SubAccountId))", out)
        self.assertIn('policy row_level_security enable "FinopsRls_Costs()"', out)
        self.assertIn(".add database ['Hub'] viewers ('aadgroup=g2;t1')", out)

    def test_subscription_accepts_multiple_principals_and_deduplicates_grants(self):
        cfg = {**self.CFG, "teams": [
            {"name": "Subscription A", "principals": ["aaduser=u1;t1", "aaduser=u2;t1"],
             "subscriptions": ["/subscriptions/sub-a"]},
            {"name": "Subscription B", "principals": ["aaduser=u1;t1"],
             "subscriptions": ["/subscriptions/sub-b"]},
        ]}

        plan = gen_rls.plan_from_config(cfg)
        out = gen_rls.generate(cfg)

        self.assertIn("current_principal_is_member_of('aaduser=u1;t1', 'aaduser=u2;t1')", out)
        self.assertEqual([command for _, command in gen_rls.grant_commands(plan)].count(
            ".add database ['Hub'] viewers ('aaduser=u1;t1')"), 1)
        self.assertEqual(len(gen_rls.grant_commands(plan)), 2)

    def test_requires_allow_all(self):
        with self.assertRaises(ValueError):
            gen_rls.generate({**self.CFG, "allow_all": []})

    def test_rejects_injection(self):
        bad = {**self.CFG, "teams": [{"name": "x", "principal": "aadgroup=g;t", "subscriptions": ['a"]), dynamic(["']}]}
        with self.assertRaises(ValueError):
            gen_rls.generate(bad)
        with self.assertRaises(ValueError):
            gen_rls.generate({**self.CFG, "table": "T; .drop table X"})
        with self.assertRaises(ValueError):
            gen_rls.generate({**self.CFG, "allow_all": ["aadgroup=g'); .drop"]})

    def test_duplicate_team(self):
        t = self.CFG["teams"][0]
        with self.assertRaises(ValueError):
            gen_rls.generate({**self.CFG, "teams": [t, {**t, "name": "team a"}]})


class ConfigTests(unittest.TestCase):
    def test_defaults_and_validation(self):
        s = Settings.from_env({"ADX_CLUSTER_URI": "https://c.kusto.windows.net/", "ALLOWED_ENTITIES": "A(), B()"})
        self.assertEqual(s.adx_cluster_uri, "https://c.kusto.windows.net")
        self.assertEqual((s.max_tool_iterations, s.llm_rows, s.max_turn_tokens, s.openai_reasoning_effort),
                         (8, 30, 150_000, "medium"))
        self.assertEqual(Settings.from_env({"OPENAI_REASONING_EFFORT": " Medium "}).openai_reasoning_effort, "medium")
        self.assertEqual(s.allowed_entities, ("A()", "B()"))
        s.validate()
        with self.assertRaises(ValueError):
            Settings.from_env({"ADX_CLUSTER_URI": "x", "AUTH_MODE": "obo"}).validate()
        with self.assertRaises(ValueError):
            Settings.from_env({"AUTH_MODE": "bogus"})

    def test_llm_provider(self):
        base = {"ADX_CLUSTER_URI": "https://c.kusto.windows.net"}
        s = Settings.from_env(base)
        self.assertEqual((s.llm_provider, s.foundry_auth), ("anthropic", "entra"))
        with self.assertRaises(ValueError):
            Settings.from_env({**base, "LLM_PROVIDER": "foundry"}).validate()
        s = Settings.from_env({**base, "LLM_PROVIDER": "Foundry", "ANTHROPIC_FOUNDRY_RESOURCE": "res"})
        s.validate()
        self.assertEqual((s.llm_provider, s.foundry_resource), ("foundry", "res"))
        self.assertEqual(Settings.from_env({**base, "LLM_PROVIDER": "OpenAI"}).llm_provider, "openai")
        az = {**base, "LLM_PROVIDER": "azure_openai",
              "AZURE_OPENAI_ENDPOINT": "https://r.openai.azure.com/openai/deployments/d/chat/completions?api-version=x"}
        with self.assertRaises(ValueError):
            Settings.from_env(az).validate()  # no deployment
        s = Settings.from_env({**az, "AZURE_OPENAI_DEPLOYMENT": "d"})
        s.validate()  # entra by default: no key needed
        self.assertEqual((s.azure_openai_endpoint, s.model, s.azure_openai_auth), ("https://r.openai.azure.com", "d", "entra"))
        self.assertEqual(Settings.from_env({**az, "AZURE_OPENAI_ENDPOINT": "r.openai.azure.com/"}).azure_openai_endpoint,
                         "https://r.openai.azure.com")
        with self.assertRaises(ValueError):
            Settings.from_env({**az, "AZURE_OPENAI_DEPLOYMENT": "d", "AZURE_OPENAI_AUTH": "key"}).validate()
        with self.assertRaises(ValueError):
            Settings.from_env({**az, "AZURE_OPENAI_AUTH": "password"})
        with self.assertRaises(ValueError):
            Settings.from_env({**base, "LLM_PROVIDER": "gemini"})
        with self.assertRaises(ValueError):
            Settings.from_env({**base, "FOUNDRY_AUTH": "password"})
        key_mode = {**base, "LLM_PROVIDER": "foundry", "ANTHROPIC_FOUNDRY_RESOURCE": "res", "FOUNDRY_AUTH": "key"}
        with self.assertRaises(ValueError):
            Settings.from_env(key_mode).validate()
        Settings.from_env({**key_mode, "ANTHROPIC_FOUNDRY_API_KEY": "k"}).validate()

    def test_entra_credential(self):
        obo = {"ADX_CLUSTER_URI": "https://c.kusto.windows.net", "AUTH_MODE": "obo",
               "ENTRA_TENANT_ID": "t", "ENTRA_CLIENT_ID": "c"}
        self.assertEqual(Settings.from_env(obo).entra_credential, "secret")
        with self.assertRaises(ValueError):
            Settings.from_env(obo).validate()  # secret mode without a secret
        Settings.from_env({**obo, "ENTRA_CLIENT_SECRET": "s"}).validate()
        mi = {**obo, "ENTRA_CREDENTIAL": "Managed_Identity"}
        with self.assertRaises(ValueError):
            Settings.from_env(mi).validate()  # no managed identity client id
        Settings.from_env({**mi, "ENTRA_MI_CLIENT_ID": "m"}).validate()
        with self.assertRaises(ValueError):
            Settings.from_env({**obo, "ENTRA_CREDENTIAL": "certificate"})


class ClientAuthTests(unittest.TestCase):
    def test_secret(self):
        s = Settings.from_env({"ADX_CLUSTER_URI": "x", "ENTRA_CLIENT_SECRET": "s"})
        auth = ClientAuth(s)
        self.assertEqual(auth.msal_credential(), "s")
        self.assertEqual(auth.form(), {"client_secret": "s"})

    def test_managed_identity_sends_no_secret(self):
        s = Settings.from_env({"ADX_CLUSTER_URI": "x", "ENTRA_CREDENTIAL": "managed_identity",
                               "ENTRA_MI_CLIENT_ID": "m", "ENTRA_CLIENT_SECRET": "leftover"})
        calls = []

        def assertion():
            calls.append(1)
            return f"mi-token-{len(calls)}"

        auth = ClientAuth(s, assertion=assertion)
        self.assertIs(auth.msal_credential()["client_assertion"], assertion)
        self.assertEqual(auth.form(), {"client_assertion": "mi-token-1", "client_assertion_type": JWT_BEARER})
        self.assertEqual(auth.form()["client_assertion"], "mi-token-2")  # fetched per request, never stale


if __name__ == "__main__":
    unittest.main()

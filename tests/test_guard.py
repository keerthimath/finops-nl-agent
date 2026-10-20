import unittest

from finops_nl.guard import GuardError, bracket_names, mask, validate_kql

ENT = ("Costs_v1_2()",)


def ok(q):
    return validate_kql(q, allowed_entities=ENT)


class GuardTests(unittest.TestCase):
    def test_accepts_normal_query(self):
        q = "Costs_v1_2()\n| where ChargePeriodStart >= ago(30d)\n| summarize sum(EffectiveCost) by SubAccountName"
        self.assertEqual(ok(q), q)

    def test_accepts_let_and_join(self):
        ok("let a = Costs_v1_2() | summarize x=sum(EffectiveCost) by ServiceName;\na | top 5 by x desc")

    def test_rejects_month_year_week_timespans_with_a_fix(self):
        for q in ("Costs_v1_2() | where ChargePeriodStart >= startofmonth(ago(1mo))",
                  "Costs_v1_2() | where ChargePeriodStart > ago(1y)",
                  "Costs_v1_2() | where ChargePeriodStart > ago(2w)",
                  "Costs_v1_2() | where ChargePeriodStart > ago(3 months)"):
            with self.subTest(q=q), self.assertRaises(GuardError) as ctx:
                validate_kql(q, allowed_entities=ENT)
            self.assertIn("startofmonth(now(), -1)", str(ctx.exception))
        for ok in ("Costs_v1_2() | where ChargePeriodStart > ago(30d) | where SkuTerm == '1y'",   # inside a string
                   "Costs_v1_2() | where ChargePeriodStart >= startofmonth(now(), -1) // not 1mo",  # in a comment
                   "Costs_v1_2() | extend Top5y = 1 | summarize by Top5y",                          # identifier
                   "Costs_v1_2() | where ChargePeriodStart > ago(12h) and x > 1m"):
            with self.subTest(ok=ok):
                validate_kql(ok, allowed_entities=ENT)

    def test_blocks_management_commands(self):
        for q in (".drop table Costs_final_v1_2", "  .show tables", ".set-or-append T <| Costs_v1_2()"):
            with self.assertRaises(GuardError):
                ok(q)

    def test_blocks_set_statements(self):
        with self.assertRaises(GuardError):
            ok("set notruncation;\nCosts_v1_2() | take 10")

    def test_blocks_dangerous_functions(self):
        for q in (
            "externaldata(a:string)['https://x'] | union Costs_v1_2()",
            "Costs_v1_2() | evaluate http_request('https://x')",
            "Costs_v1_2() | extend x = http_request_post('https://x')",
            "Costs_v1_2() | evaluate python(typeof(*), 'x')",
            "Costs_v1_2() | evaluate sql_request('a','b')",
        ):
            with self.assertRaises(GuardError, msg=q):
                ok(q)

    def test_allows_safe_plugins(self):
        ok("Costs_v1_2() | summarize c=sum(EffectiveCost) by ServiceName, SubAccountName | evaluate pivot(SubAccountName, sum(c))")

    def test_blocks_cross_scope(self):
        for q in ("database('Ingestion').Costs_final_v1_2 | take 1", "cluster('x').database('y').T | take 1"):
            with self.assertRaises(GuardError):
                validate_kql(q + " | where 1==1 // Costs_v1_2()", allowed_entities=ENT)

    def test_blocks_search_find_union_star(self):
        for q in (
            "search 'foo'",
            "Costs_v1_2() | take 1 | find 'x'",
            "union * | take 5",
            "union withsource=T * | take 5",
        ):
            with self.assertRaises(GuardError, msg=q):
                validate_kql(q + "\n// Costs_v1_2()", allowed_entities=ENT)

    def test_comments_cannot_hide_or_trigger(self):
        # denied word inside a comment is fine
        ok("Costs_v1_2() // externaldata is not used here\n| take 5")
        # but a comment cannot satisfy the allowed-entity requirement
        with self.assertRaises(GuardError):
            ok("OtherTable | take 5 // Costs_v1_2()")

    def test_string_literals_are_masked(self):
        ok("Costs_v1_2() | where ServiceName == 'externaldata' or ResourceName == \"database(x)\" | take 5")
        ok("Costs_v1_2() | where ResourceName == @'a''b; .drop' | take 5")

    def test_requires_allowed_entity(self):
        with self.assertRaises(GuardError):
            ok("print 1")

    def test_ends_with_tabular_expression(self):
        with self.assertRaises(GuardError):
            ok("let a = Costs_v1_2();")

    def test_empty_long_unterminated(self):
        with self.assertRaises(GuardError):
            ok("   ")
        with self.assertRaises(GuardError):
            validate_kql("Costs_v1_2() | take 1 //" + "x" * 9000, allowed_entities=ENT, max_chars=8000)
        with self.assertRaises(GuardError):
            ok("Costs_v1_2() | where a == 'oops")


class BracketNameTests(unittest.TestCase):
    """`project` (a KQL operator) and `application` as names must be ['project'] / ['application'] (SYN0002)."""

    def test_names_are_bracketed_operators_are_not(self):
        cases = {
            "T | where project == 'a'": "T | where ['project'] == 'a'",
            "T | summarize Cost = sum(c) by project, application":
                "T | summarize Cost = sum(c) by ['project'], ['application']",
            "T | project project, Cost": "T | project ['project'], Cost",
            "T | project-away project": "T | project-away ['project']",
            "T | extend project = tostring(Tags.project)": "T | extend ['project'] = tostring(Tags['project'])",
            "T | where isnotempty(application)\n| project Cost": "T | where isnotempty(['application'])\n| project Cost",
            "T | join (U | project a) on a": "T | join (U | project a) on a",
        }
        for query, expected in cases.items():
            with self.subTest(query):
                self.assertEqual(bracket_names(query), expected)

    def test_strings_comments_brackets_and_lookalikes_are_untouched(self):
        for query in ("T | where ['project'] == 'project' // by project", "T | where x_project == x_application",
                      "T | where projectNames has 'a' and applicationName == @'project'", 'T | where a == "application"',
                      "let project = 1;\nT | project a"):
            with self.subTest(query):
                self.assertEqual(bracket_names(query), query)

    def test_validate_returns_the_bracketed_query(self):
        out = validate_kql("Costs_v1_2() | summarize sum(EffectiveCost) by project", allowed_entities=("Costs_v1_2()",))
        self.assertEqual(out, "Costs_v1_2() | summarize sum(EffectiveCost) by ['project']")

    def test_mask_keep_length(self):
        q = "T | where a == 'x' // c\n| take 1"
        self.assertEqual(len(mask(q, keep_length=True)), len(q))


if __name__ == "__main__":
    unittest.main()

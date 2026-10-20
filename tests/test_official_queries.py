"""Every KQL block in the Microsoft FinOps Toolkit catalog and weekly guide must pass the guard.

If a toolkit update adds a pattern the guard rejects, this fails and tells you which query.
"""
import re
import unittest
from importlib import resources

from finops_nl.config import Settings
from finops_nl.guard import GuardError, bracket_names, validate_kql

ENT = Settings(adx_cluster_uri="x", adx_database="Hub").allowed_entities


def blocks(name):
    text = resources.files("finops_nl").joinpath("prompts", name).read_text(encoding="utf-8")
    return re.findall(r"```kusto\n(.*?)```", text, re.S)


class OfficialQueryTests(unittest.TestCase):
    def check(self, name):
        found = blocks(name)
        self.assertGreater(len(found), 3, f"no queries parsed from {name}")
        checked = 0
        for q in found:
            body = [x for x in re.sub(r"//[^\n]*", "", q).split(";") if x.strip()]
            if body and body[-1].strip().startswith("let "):
                continue  # a documented fragment (e.g. date boundaries), not a runnable query
            checked += 1
            try:
                out = validate_kql(q, allowed_entities=ENT)
            except GuardError as exc:
                self.fail(f"{name}: rejected: {exc}\n---\n{q[:300]}")
            self.assertNotRegex(out, r"\n\s*\n")  # blank lines collapsed
            self.assertEqual(bracket_names(q), q)  # the toolkit's own queries need no bracketing
        self.assertGreaterEqual(checked, 3)

    def test_catalog(self):
        self.check("query_catalog.md")

    def test_weekly_report(self):
        self.check("weekly_report.md")

    def test_system_prompt_mentions_tools_that_exist(self):
        s = resources.files("finops_nl").joinpath("prompts", "system.md").read_text(encoding="utf-8")
        for tool in ("get_reference", "describe_entity", "run_kql", "find_names", "run_dashboard_query"):
            self.assertIn(tool, s)


if __name__ == "__main__":
    unittest.main()

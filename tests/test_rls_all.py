import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import rls_all  # noqa: E402

ADMIN = "aaduser=11111111-1111-1111-1111-111111111111;22222222-2222-2222-2222-222222222222"
TEAM = {"name": "a", "principals": ["aadgroup=33333333-3333-3333-3333-333333333333;22222222-2222-2222-2222-222222222222"],
        "subscriptions": ["/subscriptions/aaaa"]}


class RlsAllTests(unittest.TestCase):
    def write(self, d, name, table, fn, allow=(ADMIN,), teams=()):
        import yaml
        cfg = {"table_database": "Ingestion", "table": table, "function_name": fn, "allow_all": list(allow),
               "teams": list(teams)}
        Path(d, name).write_text(yaml.safe_dump(cfg))

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.write(self.d, "teams.yaml", "Costs_final_v1_2", "FinopsRls_Costs", teams=[TEAM])
        self.write(self.d, "teams_v1_0.yaml", "Costs_final_v1_0", "FinopsRls_Costs_v1_0", teams=[TEAM])
        Path(self.d, "teams.example.yaml").write_text("not: [valid")  # ignored

    def test_agreeing_configs(self):
        paths = rls_all.config_paths(self.d)
        self.assertEqual([os.path.basename(p) for p in paths], ["teams.yaml", "teams_v1_0.yaml"])
        self.assertEqual(rls_all.check(paths), [])

    def test_team_missing_from_one_config(self):
        self.write(self.d, "teams_v1_0.yaml", "Costs_final_v1_0", "FinopsRls_Costs_v1_0")  # no team
        problems = rls_all.check(rls_all.config_paths(self.d))
        self.assertTrue(any("differ" in p for p in problems))

    def test_duplicate_function_or_table(self):
        self.write(self.d, "teams_x.yaml", "Costs_final_v1_2", "FinopsRls_Other", teams=[TEAM])
        problems = rls_all.check(rls_all.config_paths(self.d))
        self.assertTrue(any("same table" in p for p in problems))

    def test_advice_prefers_groups_and_one_entry_per_team(self):
        user = "aaduser=44444444-4444-4444-4444-444444444444;22222222-2222-2222-2222-222222222222"
        same = dict(TEAM, name="b", subscriptions=["/subscriptions/bbbb"])
        solo = {"name": "c", "principals": [user], "subscriptions": ["/subscriptions/cccc"]}
        d = tempfile.mkdtemp()
        self.write(d, "teams.yaml", "Costs_final_v1_2", "FinopsRls_Costs", teams=[TEAM, same, solo])
        notes = rls_all.advice(rls_all.config_paths(d))
        self.assertEqual(len(notes), 2)
        self.assertIn("team 'c': 1 individual user", notes[0])
        self.assertIn("teams 'a', 'b' have the same principals", notes[1])
        self.assertEqual(rls_all.advice(rls_all.config_paths(self.d)), [])  # one group per team: nothing to say

    def test_invalid_config_and_none(self):
        self.write(self.d, "teams_bad.yaml", "T", "F", allow=())
        self.assertTrue(any("allow_all" in p for p in rls_all.check(rls_all.config_paths(self.d))))
        self.assertEqual(rls_all.check([]), ["no configs found (scripts/teams*.yaml)"])


if __name__ == "__main__":
    unittest.main()

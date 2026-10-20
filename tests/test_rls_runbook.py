import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import rls_runbook  # noqa: E402

CFG = """
table_database: Ingestion
table: Costs_final_v1_2
viewer_databases: [Hub, Ingestion]
allow_all: ["aadgroup=g1;t1"]
teams:
  - {name: A, principal: "aadgroup=g2;t1", subscriptions: ["/subscriptions/aaa"]}
  - {name: B, principal: "aadgroup=g3;t1", subscriptions: ["/subscriptions/bbb"]}
"""


class FakeRunner:
    """counts: scripted (rows, subs) results for each count query, in order."""

    def __init__(self, counts):
        self.counts, self.calls = list(counts), []

    def run(self, db, text):
        self.calls.append((db, text))
        if "summarize Rows=count()" in text:
            r, s = self.counts.pop(0)
            return [{"Rows": r, "Subs": s}]
        if "summarize by v=" in text:
            return [{"v": "/subscriptions/aaa"}, {"v": "/subscriptions/zzz"}]
        return []


def run_main(args, runner):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(CFG)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = rls_runbook.main([*args[:1], f.name, *args[1:]], runner_factory=lambda _c: runner)
    return code, buf.getvalue()


def cmds(runner):
    return [c for _, c in runner.calls]


class RunbookTests(unittest.TestCase):
    def test_apply_dry_run_executes_nothing(self):
        r = FakeRunner([])
        code, out = run_main(["apply", "--cluster", "https://x"], r)
        self.assertEqual(code, 0)
        self.assertEqual(r.calls, [])
        self.assertIn("DRY RUN", out)
        self.assertIn('enable "FinopsRls_Costs()"', out)

    def test_apply_success_order(self):
        r = FakeRunner([(100, 5), (100, 5), (100, 5)])  # plain, forced, after
        code, _ = run_main(["apply", "--cluster", "https://x", "--execute"], r)
        self.assertEqual(code, 0)
        c = cmds(r)
        fn = next(i for i, x in enumerate(c) if x.startswith(".create-or-alter function"))
        forced = next(i for i, x in enumerate(c) if x.startswith("set query_force_row_level_security"))
        enable = next(i for i, x in enumerate(c) if "row_level_security enable" in x)
        grants = [i for i, x in enumerate(c) if x.startswith(".add database")]
        self.assertTrue(fn < forced < enable < min(grants))
        self.assertEqual(len(grants), 4)  # 2 teams x 2 databases
        self.assertTrue(all(db == "Ingestion" for db, x in r.calls if "row_level_security" in x))

    def test_aborts_before_enable_if_function_hides_rows_from_you(self):
        r = FakeRunner([(100, 5), (0, 0)])
        code, out = run_main(["apply", "--cluster", "https://x", "--execute"], r)
        self.assertEqual(code, 1)
        self.assertFalse(any("row_level_security enable" in x for x in cmds(r)))
        self.assertFalse(any(x.startswith(".add database") for x in cmds(r)))
        self.assertIn("NOT enabled", out)

    def test_auto_rollback_if_rows_change_after_enable(self):
        r = FakeRunner([(100, 5), (100, 5), (0, 0)])
        code, out = run_main(["apply", "--cluster", "https://x", "--execute"], r)
        self.assertEqual(code, 1)
        c = cmds(r)
        self.assertTrue(any("row_level_security disable" in x for x in c))
        self.assertFalse(any(x.startswith(".add database") for x in c))
        self.assertIn("Rolling back", out)

    def test_rollback(self):
        r = FakeRunner([])
        code, _ = run_main(["rollback", "--cluster", "https://x"], r)
        self.assertEqual((code, r.calls), (0, []))
        code, _ = run_main(["rollback", "--cluster", "https://x", "--execute"], r)
        self.assertEqual(code, 0)
        self.assertEqual(cmds(r), ['.alter table Costs_final_v1_2 policy row_level_security disable "FinopsRls_Costs()"'])

    def test_preflight_flags_missing_subscriptions(self):
        r = FakeRunner([(100, 5)])
        code, out = run_main(["preflight", "--cluster", "https://x"], r)
        self.assertEqual(code, 1)  # /subscriptions/bbb is not in the data
        self.assertIn("MISSING  B: /subscriptions/bbb", out)
        self.assertIn("ok       A: /subscriptions/aaa", out)

    def test_requires_cluster(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code, _ = run_main(["verify", "--cluster", ""], FakeRunner([]))
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()

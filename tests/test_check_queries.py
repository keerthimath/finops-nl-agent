import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_queries  # noqa: E402
from finops_nl.adx import AdxError, QueryResult  # noqa: E402
from finops_nl.config import Settings  # noqa: E402


class FakeAdx:
    def __init__(self, fail_if=""):
        self.fail_if, self.calls = fail_if, []

    def query(self, kql, **_):
        self.calls.append(kql)
        if self.fail_if and self.fail_if in kql:
            raise AdxError("Request is invalid and cannot be processed: Semantic error: SEM0100: 'LastWeek'")
        return QueryResult(["a"], [[1]], 1, False, 5)


class CheckQueriesTests(unittest.TestCase):
    def test_collects_runnable_doc_queries_with_headings(self):
        q = check_queries.collect(Settings(adx_cluster_uri="x", adx_database="Hub"), None)
        sources = {s for s, _, _ in q}
        self.assertEqual(sources, {"catalog", "weekly"})  # no DASHBOARD_FILE
        labels = [label for s, label, _ in q if s == "weekly"]
        self.assertIn("Q2: Category summary (top movers)", labels)
        self.assertNotIn("Date boundaries", labels)  # fragment only
        self.assertEqual(len(check_queries.collect(Settings(adx_cluster_uri="x", adx_database="Hub"), "weekly")),
                         len(labels))

    def test_failures_are_reported(self):
        q = [("weekly", "Q2", "... LastWeek ..."), ("catalog", "Top services", "Costs_v1_2() | take 1")]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            failures = check_queries.check(FakeAdx(fail_if="LastWeek"), q)
        self.assertEqual(failures, [("weekly", "Q2", "Semantic error: SEM0100: 'LastWeek'")])
        self.assertIn("ok    catalog   Top services", out.getvalue())


class ExclusionTests(unittest.TestCase):
    def test_writes_only_adx_rejections(self):
        import json
        import os
        import tempfile
        d = tempfile.mkdtemp()
        path = os.path.join(d, "finops.json")
        failures = [("dashboard", "users/x", "SEM0100: 'Creator'"), ("dashboard", "a/b", "Failed to process network request"),
                    ("weekly", "Q6", "SEM0100: 'LastWeek'")]
        self.assertEqual(check_queries.write_exclusions(path, failures), {"users/x": "SEM0100: 'Creator'"})
        with open(os.path.join(d, "finops.exclude.json")) as f:
            self.assertEqual(json.load(f), {"users/x": "SEM0100: 'Creator'"})


if __name__ == "__main__":
    unittest.main()

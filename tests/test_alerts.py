import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import alerts  # noqa: E402

CFG = {"subscription": "s", "resource_group": "rg", "location": "eastus", "workspace_id": "/ws", "workspace_customer_id": "g",
       "identity_id": "/mi", "action_group_id": "/ag", "adx_cluster_uri": "https://c.eastus2.kusto.windows.net/"}


class AlertRuleTests(unittest.TestCase):
    def test_defined_rules_are_valid(self):
        self.assertEqual({r.name: alerts.validate(r) for r in alerts.RULES}, {r.name: [] for r in alerts.RULES})

    def test_limits(self):
        r = alerts.RULES[0]
        self.assertIn("12 hours", " ".join(alerts.validate(replace(r, frequency="P1D", stateful=True))))
        self.assertTrue(alerts.validate(replace(r, frequency="PT2M")))
        self.assertTrue(alerts.validate(replace(r, frequency="P2D")))
        self.assertTrue(alerts.validate(replace(r, name="Bad Name")))
        self.assertTrue(alerts.validate(replace(r, dims=())))
        self.assertTrue(alerts.validate(replace(r, query="Costs_v1_2() | count")))  # must go through adx()
        self.assertEqual(alerts.minutes("PT12H"), 720)

    def test_body(self):
        r = alerts.RULES[1]
        b = alerts.body(r, CFG)
        p = b["properties"]
        self.assertEqual(b["identity"], {"type": "UserAssigned", "userAssignedIdentities": {"/mi": {}}})
        self.assertEqual((p["scopes"], p["actions"]["actionGroups"]), (["/ws"], ["/ag"]))
        self.assertEqual((p["evaluationFrequency"], p["windowSize"], p["autoMitigate"]), ("P1D", "P1D", False))
        crit = p["criteria"]["allOf"][0]
        self.assertIn("adx('https://c.eastus2.kusto.windows.net/Ingestion').Costs_final_v1_2", crit["query"])
        self.assertNotIn("{costs}", crit["query"])
        self.assertEqual([d["name"] for d in crit["dimensions"]], list(r.dims))
        self.assertEqual((crit["timeAggregation"], crit["operator"], crit["threshold"]), ("Count", "GreaterThan", 0))
        self.assertIn("/resourceGroups/rg/providers/Microsoft.Insights/scheduledQueryRules/finops-daily-top-resource",
                      alerts.rule_url(r, CFG))


if __name__ == "__main__":
    unittest.main()

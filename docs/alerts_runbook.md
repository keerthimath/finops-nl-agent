# Runbook: cost alerts in Azure Monitor

Cost alerts and daily cost reports are **Azure Monitor log search alert rules**. Each rule runs a KQL query from a Log
Analytics workspace against the hub through `adx()`, as a user-assigned managed identity, and emails through an action
group (Azure sends the mail: no SMTP). Rule definitions: `scripts/alerts.py` (`RULES`). Environment ids:
`scripts/alerts.yaml` (git-ignored; copy `scripts/alerts.example.yaml`).

## Rules
| Rule | What | Runs | Kind |
| --- | --- | --- | --- |
| `finops-subscription-cost-trend` | Per subscription: last 7 complete days vs the 7 before; fires when up more than 20% **and** more than 5 (billing currency) | every 12 h | stateful: notifies once, resolves when the trend drops |
| `finops-daily-top-resource` | The highest-cost resource on the last complete day | daily | report: fires every day |
| `finops-month-top5-resources` | The 5 highest-cost resources this month (one notification) | daily | report: fires every day |

Days are UTC. Azure cost data lags 24-48 h, so "the last complete day" is 2 days back. Cost is `EffectiveCost` (amortized).

## Commands
```bash
az login
python scripts/alerts.py list                          # rules and their schedule
python scripts/alerts.py test <rule>                   # run the rule's query now, as you, through the workspace
python scripts/alerts.py apply <rule>                  # dry run: the rule body that would be PUT
python scripts/alerts.py apply <rule> --execute        # create or update it
python scripts/alerts.py status                        # alerts these rules fired in the last day
python scripts/alerts.py disable|enable [<rule> ...]   # stop / restart
```

## Adding a rule
1. Write the query and run it as `test` would: it must return **one row per alert**, and only when it should alert
   (filter in KQL; the rule fires when the query returns any row). String columns become the alert's **dimensions**;
   their values are what the email shows, so put the numbers in a readable string column (`Detail`, `Cost`).
2. Add a `Rule(...)` to `RULES` in `scripts/alerts.py`: name `finops-...`, display name, description, severity
   (0 critical .. 4 verbose), `frequency`, `stateful`, `dims`, and the query with `{costs}` for the cost table.
3. `python scripts/alerts.py test <rule>` - check the rows; `apply <rule>` - review the body; confirm with the user
   (each rule adds a small monthly cost), then `apply <rule> --execute`.
4. Verify: `python scripts/alerts.py status` after the first run (minutes after creation for most rules, up to the
   rule's frequency), and the email arrives.
5. Add a test case if you changed `scripts/alerts.py` logic; commit `scripts/alerts.py` (never `alerts.yaml`).

## Limits and gotchas (found while setting these up)
- `adx()` reads **tables only**: `adx('<cluster>/Hub').Costs_v1_2()` fails (`SEM0036`). Use
  `{costs}` = `adx('<cluster>/Ingestion').Costs_final_v1_2` (and the other `*_final_v1_2` tables).
- Stateful rules (`autoMitigate`, resolve when the condition clears) must run at least every 12 hours (`PT12H`).
  Daily reports are stateless (`stateful=False`) so they notify every day.
- Frequency is between 5 minutes and 1 day; a weekly schedule isn't possible with these rules.
- Change rules with `scripts/alerts.py` (or `az rest`), **not** `az monitor scheduled-query update`: that uses an older
  API version, drops the identity, and Azure rejects `adx()` ("The 'adx' pattern cannot be used with the current
  authentication scheme").
- Action group test notifications (`az monitor action-group test-notifications create`) are not available on a Free
  subscription ("Free subscription not supported"); re-fire a real rule instead (below).
- The rule's location must equal the workspace's region.
- `scripts/alerts.py` checks these (and the name, dimensions, severity and `adx()` use) before every command.

## Identity and access (one-time setup, done 2026-10-10)
The rules run as `finops-alerts-id`. It sees **all** cost data (user decision), so alert emails can contain any
subscription's numbers: keep the action group to people allowed to see that.
| Needs | Where | How |
| --- | --- | --- |
| RLS `allow_all` on all eight final tables | ADX `Ingestion` | `docs/rls_runbook.md`, "Rollout log: alert identity added to allow_all" |
| Database Viewer | ADX `Ingestion` | same log (`adx()` reads `Ingestion` tables; `Hub` not needed) |
| Log Analytics Reader | the workspace | `az role assignment create --assignee-object-id <principalId> --assignee-principal-type ServicePrincipal --role "Log Analytics Reader" --scope <workspace resource id>` |

## Setup log: 2026-10-10 (first three rules)
Identifiers are placeholders: `<sub>`, `<rg>` (resource group for the rules, identity and action group), `<workspace>`
(resource id), `<workspace-guid>`, `<cluster>`.
```bash
# 1. Providers (Microsoft.Insights was registered; this one wasn't)
az provider register -n Microsoft.AlertsManagement
# 2. Identity the rules run as
az identity create -g <rg> -n finops-alerts-id -l eastus
# 3. Access: Log Analytics Reader (below) + RLS allow_all and Viewer on Ingestion (docs/rls_runbook.md)
az role assignment create --assignee-object-id <principalId> --assignee-principal-type ServicePrincipal \
  --role "Log Analytics Reader" --scope <workspace>
# 4. Email action group (Azure sends a "you've been added" email to the address)
az monitor action-group create -g <rg> -n finops-alerts-email --short-name FinOpsAlert \
  --action email finops-owner <email> usecommonalertschema
# 5. Check the queries return what the emails should show
az monitor log-analytics query -w <workspace-guid> --analytics-query "<rule query>"
# 6. Rules: PUT each body (what `scripts/alerts.py apply --execute` sends)
az rest --method put --body @<rule>.json --url \
  "https://management.azure.com/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Insights/scheduledQueryRules/<rule>?api-version=2023-03-15-preview"
```
Result: the trend rule was first rejected at `P1D` ("Stateful rules cannot run in a frequency greater than 12 hours")
and created at `PT12H`. All three fired on their first run (the subscription was up 132% week over week; top resource
the ADX cluster at 6.07 USD/day), which proves the identity can read the data.

## Recipients
```bash
az monitor action-group show -g <rg> -n finops-alerts-email --query "emailReceivers[].{name:name, to:emailAddress, status:status}" -o table
az monitor action-group update -g <rg> -n finops-alerts-email --add-action email <receiver-name> <email> usecommonalertschema
az monitor action-group update -g <rg> -n finops-alerts-email --remove-action <receiver-name>
```
Mail comes from `azure-noreply@microsoft.com`. If a recipient sees nothing, check the alert's history first: an
`ActionsTriggered` entry ("Action group ... executed") means Azure sent it and the mailbox filtered or rejected it
(`az rest --method get --url "https://management.azure.com/subscriptions/<sub>/providers/Microsoft.AlertsManagement/alerts/<alert-id>/history?api-version=2019-05-05-preview"`).
Some mail providers silently drop these messages although Azure sent them; if one never arrives, try another address.

## Re-send alerts now (e.g. after setting up the email)
- Daily reports (stateless): run them every 5 minutes until they fire, then back to daily:
  ```bash
  python scripts/alerts.py apply finops-daily-top-resource finops-month-top5-resources --frequency PT5M --execute
  python scripts/alerts.py status                     # wait for new "Fired" entries
  python scripts/alerts.py apply finops-daily-top-resource finops-month-top5-resources --execute   # back to P1D
  ```
- A stateful rule that is still "Fired" won't notify again until it resolves. Reset it by deleting and re-creating it
  (identical definition): `az monitor scheduled-query delete -g <rg> -n <rule> --yes`, then
  `python scripts/alerts.py apply <rule> --execute` (if the PUT fails right after the delete, retry: the delete is
  still finishing).

## Stop and restart
```bash
python scripts/alerts.py disable [<rule> ...]   # stop: no runs, no emails; definitions, identity, action group kept
python scripts/alerts.py enable  [<rule> ...]   # restart
```
State: all three rules **disabled on 2026-10-10** at the user's request, after confirming emails arrive.

## Remove a rule or everything
```bash
az monitor scheduled-query delete -g <rg> -n <rule> --yes
# everything: delete the three rules, the action group (az monitor action-group delete -g <rg> -n finops-alerts-email),
# then remove finops-alerts-id from RLS (docs/rls_runbook.md, its "Undo") and delete the identity.
```

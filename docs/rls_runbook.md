# Runbook: enable subscription-level RLS on the FinOps Hub in ADX

Two ways to do this. Use the script (safety-checked) or paste the commands by hand (same steps, no checks).
**Do it on a non-prod cluster first.** RLS replaces access to the table for every principal, including dashboards
and ingestion readers.

## Before you start
| You need | Why |
| --- | --- |
| **Database Admin** on the database that holds the cost table (FinOps Hub: usually `Ingestion`) | Create the function, enable the policy, grant viewers |
| Object IDs of each team's Entra group, your admin group, and the ingestion/ADF identity | Go in `scripts/teams.yaml` |
| The exact `SubAccountId` strings for each team's subscriptions | The RLS function matches on them |
| A test user in one team, and one with Viewer but in no team | To prove scoping |

`allow_all` **must** include your FinOps admins, the hub ingestion/ADF identity, **and the identity you run this
as**. Otherwise you (or ingestion) lose access the moment RLS is enabled.

## Option A: the script
```bash
az login                                     # as a Database Admin
export ADX_CLUSTER_URI=https://<cluster>.<region>.kusto.windows.net
cp scripts/teams.example.yaml scripts/teams.yaml     # then edit it
pip install azure-kusto-data pyyaml

python scripts/rls_runbook.py preflight scripts/teams.yaml           # 1. read-only checks
python scripts/rls_runbook.py apply     scripts/teams.yaml           # 2. dry run: prints every command
python scripts/rls_runbook.py apply     scripts/teams.yaml --execute # 3. apply with safety checks
python scripts/rls_runbook.py verify    scripts/teams.yaml           # 4. show policy and what you see
python scripts/rls_runbook.py rollback  scripts/teams.yaml --execute # undo, if needed
```
**Protect every final table with `SubAccountId`, both versions.** The Hub functions union the `_v1_2` and `_v1_0`
tables (check with `.show function Costs_v1_2` in `Hub`). One config per table, each with its own function name:
| Config | `table` | `function_name` |
| --- | --- | --- |
| `scripts/teams.yaml` | `Costs_final_v1_2` | `FinopsRls_Costs` |
| `scripts/teams_v1_0.yaml` | `Costs_final_v1_0` | `FinopsRls_Costs_v1_0` |
| `scripts/teams_<Name>_<ver>.yaml` | `<Name>_final_<ver>` for Recommendations, Transactions, CommitmentDiscountUsage, `v1_2` and `v1_0` | `FinopsRls_<Name>_<ver>` |

Model teams as **one entry per team**: an Entra group plus every subscription the team may see (see
`scripts/teams.example.yaml`). Membership then lives in Entra, so people joining or leaving need no redeploy;
`rls_all.py check` notes individual-user principals and teams that repeat the same principals.
Keep `allow_all` and `teams` identical in all of them: `python scripts/rls_all.py check` verifies that, and
`python scripts/rls_all.py preflight|apply [--execute]|verify|rollback --execute` runs a step on every config in
turn (it checks first and stops at the first failure). Configs are git-ignored (`scripts/teams*.yaml`) because they
hold object ids. Rows with an empty `SubAccountId` (billing-account-level purchases, shared reservations,
recommendations) match no team, so only `allow_all` sees them.

In zsh, keep `--execute` as its own argument: a variable holding `apply --execute` is passed as one word and the
script rejects it (nothing runs).
What `apply --execute` does: creates the function, compares the rows you see with and without forced RLS
(`set query_force_row_level_security;`) and **aborts if they differ**, enables the policy, re-counts and
**auto-disables if your row count changed**, then grants Database Viewer to the team groups.
`--force` skips those checks; avoid it.

## Option B: by hand (ADX web UI or Kusto Explorer)
Run each block in the database shown. `<table>` is `Costs_final_v1_2` unless yours differs.

**1. Preflight (database `Ingestion`)**
```kusto
print current_principal_details()
.show table <table> policy update
.show table * policy update
.show table <table> policy restricted_view_access
.show materialized-views
.show table <table> policy row_level_security
<table> | summarize by SubAccountId
```
Stop if: an update policy references the table and does not use a managed identity; the restricted view access
policy is on; a materialized view uses the table (it needs its own RLS, or enable with
`with (allowMaterializedViewsWithoutRowLevelSecurity=true)`); or your team subscriptions are not in the last result.

**2. Create the function (database `Ingestion`)**: generate it with `python scripts/gen_rls.py scripts/teams.yaml`
and run the `.create-or-alter function` block. It does nothing until the policy is enabled.

**3. Dry-run it on yourself (database `Ingestion`)**: both counts must match.
```kusto
<table> | summarize Rows=count()
set query_force_row_level_security;
<table> | summarize Rows=count()
```

**4. Enable (database `Ingestion`)**
```kusto
.alter table <table> policy row_level_security enable "FinopsRls_Costs()"
```
Re-run the count immediately. If it dropped, roll back (step 7).

**5. Grant Database Viewer (run in each database in `viewer_databases`)**
```kusto
.add database ['Hub'] viewers ('aadgroup=<groupObjectId>;<tenantId>')
.add database ['Ingestion'] viewers ('aadgroup=<groupObjectId>;<tenantId>')
```

**6. Verify as other people**
- As a member of team A: `<table> | summarize by SubAccountId` returns only team A's subscriptions.
- As a Viewer in no team: returns no rows.
- As an admin in `allow_all`: returns everything.
- Open the FinOps dashboard and the chat app as a team member and confirm only their spend appears.

**7. Roll back**
```kusto
.alter table <table> policy row_level_security disable "FinopsRls_Costs()"
```

## Before adding teams (not needed while only admins have access)
RLS filters the eight final tables (see Option A). Before giving any team Database Viewer, close this, or team
members can read other teams' data directly in ADX (the chat app's guard only allows `Costs_v1_2()` and `Recommendations()`, so
the app itself is not affected):
- **Raw tables in `Ingestion`** (`Costs_raw` and the other `*_raw` tables) hold the same rows unfiltered. RLS can't
  be enabled on `Costs_raw` (it is the source of an update policy without a managed identity). Options: turn on
  the restricted view access policy on the raw tables (only admins / Unrestricted Viewers can then query them;
  confirm it doesn't affect ingestion in Microsoft's docs and on a test table first), or don't grant Viewer on
  `Ingestion` and check that the Hub functions still work for team members.
- Then add the teams to **all eight** configs and re-run `apply --execute` for each (it re-creates the functions
  and grants Viewer). Check the `SubAccountId` format in each table first (`<table> | distinct SubAccountId`): the
  team lists must match it.

## Common problems
| Symptom | Likely cause |
| --- | --- |
| Policy command fails mentioning an update policy | Table is referenced by an update policy without a managed identity |
| Policy command fails mentioning restricted view access | Turn that policy off first; the two can't be combined |
| Everyone sees nothing after enabling | Your admin/ingestion principals are not in `allow_all`, or the principal string format is wrong (`aadgroup=<objectId>;<tenantId>`) |
| A team sees nothing | `SubAccountId` strings in `teams.yaml` don't match the data (check case and the `/subscriptions/` prefix) |
| Team member can't query at all | Missing Database Viewer on `Hub` and/or `Ingestion` |
| Dashboards broke for everyone else | Dashboard identity not in `allow_all` or not in any team |

## Notes
- First run against a live cluster on 2026-10-10 (admins only; see "Rollout log"). The team path (team functions
  with subscriptions, Viewer grants, verifying as a team member) has not been exercised yet: treat the first team
  rollout as a rehearsal.
- RLS filters rows only. Anyone with Viewer can still call functions that read other tables, so keep the
  database limited to FinOps data.


## Rollout log: 2026-10-10, first rollout (admins only)
Every command run, in order. Identifiers are placeholders here: `<cluster>` is the ADX cluster URI, `<tenant>` the
Entra tenant id, `<admin>` the admin's user object id, `<engine>` the hub ingestion engine's app id. The exact log with
real values and full output is kept locally in `scripts/rls_rollout_2026-10-10.log` (git-ignored).

Decision: **admins only**. One subscription and one user today, so `allow_all` = the FinOps admin and the hub
ingestion engine, and `teams: []`. Anyone else who is later given Viewer sees no cost rows until added to a team.
Applied on the only cluster (no non-prod), with the script's safety checks.

**1. Discovery (read-only, signed in with `az login` as the admin)**
| Database | Command | Result |
| --- | --- | --- |
| Ingestion | `print current_principal_details()` | the admin, a guest user (`aaduser=<admin>;<tenant>`) |
| Ingestion | `.show databases` | `Ingestion`, `Hub` |
| Ingestion | `.show database Ingestion principals` / `.show database Hub principals` | admin: Cluster AllDatabasesAdmin; `finops-hub-engine` app (`aadapp=<engine>;<tenant>`): Cluster AllDatabasesAdmin |
| Ingestion | `Costs_final_v1_2 \| summarize Rows=count() by SubAccountId, SubAccountName` | 406 rows, 1 subscription |
| Ingestion | `.show table Costs_final_v1_2 policy update` | its own update policy (source `Costs_raw`): not a blocker |
| Ingestion | `.show table Costs_final_v1_2 policy restricted_view_access` / `... policy row_level_security` | both `null` |
| Ingestion | `.show materialized-views` | none |
| Ingestion | `.show table * policy update \| where Policy has 'Costs_final_v1_2'` | none (no other table reads it) |
| Hub | `.show function Costs_v1_2` | unions `Costs_final_v1_2` **and** `Costs_final_v1_0` |
| Ingestion | `.show tables` | 22 tables, incl. `*_raw` and `*_final_v1_0/v1_2` (see "Before adding teams") |
| Ingestion | `Costs_final_v1_0 \| summarize Rows=count(), Subs=dcount(SubAccountId)` and its three policies | 0 rows; update policy disabled; no RLS / restricted view |

**2. Config** (git-ignored): `scripts/teams.yaml` (`table: Costs_final_v1_2`, `function_name: FinopsRls_Costs`) and
`scripts/teams_v1_0.yaml` (`table: Costs_final_v1_0`, `function_name: FinopsRls_Costs_v1_0`), each with
`table_database: Ingestion`, `viewer_databases: [Hub, Ingestion]`, `teams: []` and
```yaml
allow_all:
  - "aaduser=<admin>;<tenant>"   # FinOps admin (runs this runbook)
  - "aadapp=<engine>;<tenant>"   # finops-hub ingestion engine
```
Before running, `scripts/rls_runbook.py` got a retry for dropped connections only (all its commands are idempotent),
so a network blip between enabling and re-counting can't skip the safety check.

**3. Costs_final_v1_2** (`export ADX_CLUSTER_URI=<cluster>`)
```bash
python scripts/rls_runbook.py preflight scripts/teams.yaml            # clean: 406 rows, 1 subscription
python scripts/rls_runbook.py apply     scripts/teams.yaml            # dry run: 4 commands, no grants
python scripts/rls_runbook.py apply     scripts/teams.yaml --execute
```
which ran, in database `Ingestion`:
```kusto
.create-or-alter function with (folder="security", docstring="Subscription scoping for FinOps Hub") FinopsRls_Costs() {
    let allow_all = current_principal_is_member_of('aaduser=<admin>;<tenant>', 'aadapp=<engine>;<tenant>');
    let allowed = dynamic([]);
    Costs_final_v1_2
    | where allow_all or set_has_element(allowed, tolower(SubAccountId))
}
Costs_final_v1_2 | summarize Rows=count(), Subs=dcount(SubAccountId)                                   // 406 / 1
set query_force_row_level_security; Costs_final_v1_2 | summarize Rows=count(), Subs=dcount(SubAccountId) // 406 / 1
.alter table Costs_final_v1_2 policy row_level_security enable "FinopsRls_Costs()"
Costs_final_v1_2 | summarize Rows=count(), Subs=dcount(SubAccountId)                                   // 406 / 1: ok
```

**4. Costs_final_v1_0**: the same three script runs with `scripts/teams_v1_0.yaml` (the first `--execute` attempt was
passed as one argument by zsh and rejected by argparse; nothing ran; re-run correctly). It ran the same commands with
`FinopsRls_Costs_v1_0` / `Costs_final_v1_0`: 0 rows before, with forced RLS, and after.

**5. Verify**
```bash
python scripts/rls_runbook.py verify scripts/teams.yaml        # policy IsEnabled=true, query FinopsRls_Costs(); 406 rows
python scripts/rls_runbook.py verify scripts/teams_v1_0.yaml   # policy IsEnabled=true, query FinopsRls_Costs_v1_0(); 0 rows
```
```kusto
// Hub, as the admin (the chat app's path): unchanged
Costs_v1_2() | summarize Rows=count(), Subs=dcount(SubAccountId), Cost=round(sum(EffectiveCost),2)   // 406 / 1 / 21.72
// Ingestion: what anyone outside allow_all gets (the policy's filter with allow_all = false)
let allow_all = false; let allowed = dynamic([]);
Costs_final_v1_2 | where allow_all or set_has_element(allowed, tolower(SubAccountId)) | count          // 0
.show function FinopsRls_Costs | project Name, Folder                                                  // security
```

**Still to check**
- The next hub ingestion run still lands new rows (RLS doesn't filter writes; the engine is in `allow_all`):
  `Costs_final_v1_2 | summarize max(ChargePeriodStart), count()` after the next scheduled run.
- Not proven with a second person: a Viewer outside `allow_all` seeing 0 rows (simulated above). Do it when the first
  non-admin user is added.

**Rollback** (both tables, ~seconds):
```bash
python scripts/rls_runbook.py rollback scripts/teams.yaml --execute
python scripts/rls_runbook.py rollback scripts/teams_v1_0.yaml --execute
```

## Rollout log: 2026-10-10, other final tables (admins only)
Same decision, identifiers and placeholders as the first rollout. Exact log: `scripts/rls_rollout_2026-10-10_other_tables.log`
(git-ignored).

**1. Discovery (read-only, database `Ingestion`)**, for each of `Recommendations_final_v1_2`, `Recommendations_final_v1_0`,
`Transactions_final_v1_2`, `Transactions_final_v1_0`, `CommitmentDiscountUsage_final_v1_2`, `CommitmentDiscountUsage_final_v1_0`:
```kusto
<table> | getschema | where ColumnName in ('SubAccountId','SubAccountName','BillingAccountId') | project ColumnName, ColumnType
<table> | summarize Rows=count(), Subs=dcount(column_ifexists('SubAccountId','')), Empty=countif(isempty(column_ifexists('SubAccountId','')))
.show table <table> policy row_level_security | project Policy
.show table <table> policy restricted_view_access | project Policy
.show table <table> policy update | project Policy
```
and once:
```kusto
.show table * policy update | where Policy has_any ('Recommendations_final','Transactions_final','CommitmentDiscountUsage_final') | project EntityName
.show materialized-views | project Name, SourceTable
```
Result: all six have `SubAccountId` (string), **0 rows**, no RLS or restricted view access; each has only its own
update policy (as the destination, from its `*_raw` table; enabled for `v1_2`, disabled for `v1_0`); no other update
policy reads them; no materialized views.

**2. Config**: six git-ignored files `scripts/teams_<Name>_<ver>.yaml` (`table: <Name>_final_<ver>`,
`function_name: FinopsRls_<Name>_<ver>`), otherwise identical to the first rollout's (`allow_all` = admin + engine,
`teams: []`).

**3. Apply**, for each config `scripts/teams_<Name>_<ver>.yaml`:
```bash
python scripts/rls_runbook.py preflight scripts/teams_<Name>_<ver>.yaml
python scripts/rls_runbook.py apply     scripts/teams_<Name>_<ver>.yaml            # dry run
python scripts/rls_runbook.py apply     scripts/teams_<Name>_<ver>.yaml --execute
```
Each `--execute` ran (database `Ingestion`) the same four commands as the cost tables: `.create-or-alter function
... FinopsRls_<Name>_<ver>()` (same body, its own table), the row count without and with
`set query_force_row_level_security;`, `.alter table <Name>_final_<ver> policy row_level_security enable
"FinopsRls_<Name>_<ver>()"`, and the re-count. All: 0 rows before, forced, and after; no grants (no teams).

Network: this machine's connection to the cluster drops often. Three preflights (`Recommendations_v1_2`,
`Transactions_v1_2`, `CommitmentDiscountUsage_v1_2`) failed on dropped connections after retries; they are
read-only and the same checks had just passed in step 1. The first `--execute` for `Recommendations_v1_2` failed at
its first step before creating anything (checked: `.show table Recommendations_final_v1_2 policy row_level_security`
= null, and `.show functions | where Name startswith 'FinopsRls_'` had no such function); it was re-run and succeeded.

**4. Verify**
```kusto
// Ingestion: all eight policies enabled, each with its own function
.show table * policy row_level_security | where Policy != 'null'
| extend p=parse_json(Policy) | project EntityName, Enabled=tostring(p.IsEnabled), Query=tostring(p.Query)
// Hub, as the admin: the functions still run (406 / 0 / 0 / 0)
Costs_v1_2() | count
Recommendations() | count
Transactions_v1_2() | count
CommitmentDiscountUsage_v1_2() | count
```

**Rollback**: `python scripts/rls_runbook.py rollback scripts/teams_<Name>_<ver>.yaml --execute` per table.

## Rollout log: 2026-10-10, alert identity added to allow_all
Why: Azure Monitor alert rules (see `docs/alerts_runbook.md`) query the hub with no user, as the user-assigned managed
identity `finops-alerts-id`. Decision (user): it may see **all data**, so it goes in `allow_all`. `<alerts>` = its client
id (`az identity show -g <rg> -n finops-alerts-id --query clientId -o tsv`). Exact log:
`scripts/rls_rollout_2026-10-10_alerts_identity.log` (git-ignored).

**1. Config**: added `- "aadapp=<alerts>;<tenant>"` to `allow_all` in all eight `scripts/teams*.yaml`.
```bash
python scripts/rls_all.py check              # 8 configs agree
python scripts/rls_all.py apply              # dry run: 8 functions with the new principal
python scripts/rls_all.py apply --execute    # all 8 ok (Costs_final_v1_2: 477 rows still visible to the admin)
```
**2. Database Viewer on `Ingestion` only** (alert rules read `Ingestion.Costs_final_v1_2` through `adx()`, which can't
call the `Hub` functions):
```kusto
.add database ['Ingestion'] viewers ('aadapp=<alerts>;<tenant>') 'finops-alerts-id (Azure Monitor alert rules)'
```
**3. Verify**
```kusto
.show database Ingestion principals | where PrincipalFQN has '<alerts>' | project Role, PrincipalDisplayName   // Database Ingestion Viewer
.show functions | where Name startswith 'FinopsRls_' | summarize Functions=count(), WithAlerts=countif(Body has '<alerts>')  // 8 / 8
.show table * policy row_level_security | where Policy != 'null' | extend p=parse_json(Policy)
| summarize Tables=count(), Enabled=countif(tostring(p.IsEnabled)=='true')                                    // 8 / 8
```
Proven end to end: two of the alert rules fired on their first run (they could read the data).

Note: `Costs_final_v1_2` grew from 406 rows (first rollout) to 477, so hub ingestion keeps landing data with RLS on.

**Undo**: remove the line from all configs, `python scripts/rls_all.py apply --execute`, then
`.drop database ['Ingestion'] viewers ('aadapp=<alerts>;<tenant>')`.

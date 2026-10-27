# CLAUDE.md

Natural-language cost Q&A over Azure FinOps Hub in ADX. One Chainlit app; Claude writes KQL via tools.

## Commands
- Tests: `python -m unittest discover -s tests -v` (PYTHONPATH=src if not installed)
- Run locally: `chainlit run src/finops_nl/app.py -w` (AUTH_MODE=dev, needs `az login`)
- Evals (real LLM + ADX): `python evals/run_evals.py`
- RLS KQL: `python scripts/gen_rls.py scripts/teams.yaml`
- Cost alerts (Azure Monitor): `python scripts/alerts.py list|test|apply [--execute]|status` (`docs/alerts_runbook.md`)
- RLS on all protected tables: `python scripts/rls_all.py list|check|preflight|apply [--execute]|verify|rollback --execute`
  (needs `az login` as a Database Admin and `ADX_CLUSTER_URI`)

## Rules
- The security boundary is ADX (user identity via OBO + RLS + Viewer role). Never add a shared-credential path
  that bypasses it for deployed environments. `AUTH_MODE=dev` is local only.
- Never loosen `guard.py` without adding a test in `tests/test_guard.py`.
- `app.py` is loaded by Chainlit as a plain file: use absolute imports (`from finops_nl.x import ...`) there.
- A2A (`docs/a2a.md`) accepts only the user's delegated token (scope `access_as_user`), then OBO + RLS. Never accept
  app-only / client-credentials tokens or add a service identity for agents. Loosening `a2a_core.py` checks needs a
  test in `tests/test_a2a.py`.
- Keep Azure/Anthropic/Chainlit/a2a-sdk imports lazy or confined to `app.py`, `adx.py`, `auth.py`, `entra_provider.py`,
  `a2a_server.py`
  so unit tests run without them.
- Run ONE replica until `TokenStore` is moved to shared storage.
- Wrong answer reported -> add a case to `evals/golden.yaml`, then fix prompts/schema/examples.
- `query_catalog.md` / `weekly_report.md` / `schema.md` are verbatim toolkit files; update them by replacing the files, and `tests/test_official_queries.py` will tell you if the guard rejects anything new.
- `schema.md` is the toolkit's `schema-reference.md` verbatim; keep it in sync with your hub version (FinOps hubs v12+ for `Costs_v1_2()`).
- RLS changes go through `scripts/gen_rls.py` builders (used by `rls_runbook.py` too); keep `docs/rls_runbook.md` in sync.

## RLS operations (adding a team or admin, disabling, re-enabling)
State of record: `docs/rls_runbook.md` (Option A table of configs, "Before adding teams", and the dated rollout logs).
All eight final tables in `Ingestion` are protected, each by its own function and its own config
`scripts/teams*.yaml`. The configs are git-ignored (they hold object ids): if they're missing, rebuild them from the
runbook's table, the latest rollout log, and `scripts/teams.example.yaml`, then ask the user for the ids.

A **team** = one Entra group + the subscriptions its members may see (in the app, the dashboard and ADX). An **admin**
(in `allow_all`) sees everything. Sequence with the user when they ask to add a team:
0. **Once, before the first team:** check raw retention (runbook "Before adding teams"). At the default 0 days the
   `*_raw` tables hold no data, so no extra step; if it's above 0, agree with the user how to protect them
   (restricted view access) and test a full ingestion run. Team members need Viewer on both `Hub` and `Ingestion`
   (verified 2026-10-24: `Hub` functions read `Ingestion` as the caller).
1. **User:** creates the Entra group, adds members, and says which subscriptions it owns. Offer the exact values from
   `Costs_v1_2() | distinct SubAccountId, SubAccountName` (in `Hub`).
2. **Agent:** adds the team to all configs, runs `check`, `preflight` and the `apply` dry run, and shows the user the
   exact commands (steps 2-3 below).
3. **User:** confirms. **Agent:** `rls_all.py apply --execute` (rewrites the policies to include the team and grants the
   group Database Viewer on `Hub` and `Ingestion`).
4. **User:** assigns the group to the app's Enterprise application in Entra (*Users and groups*; *Assignment required*
   on), so members can sign in.
5. **Verify together:** a member sees only their subscriptions (`Costs_v1_2() | summarize by SubAccountId`, or "cost by
   subscription" in the app); a non-member sees nothing; the agent runs step 4 below.
6. **Agent:** logs every command in a new runbook rollout section (step 5 below) and commits when asked.
Adding an admin is the same flow with the principal in `allow_all` instead of `teams`. Removing a team: see Gotchas.

Procedure for every RLS change (it changes who can see cost data, so confirm with the user before any `--execute`):
1. Read the runbook's latest rollout log and "Before adding teams". Before the first team gets access, the raw tables
   (`Costs_raw` etc.) must be closed (see that section) - raise it with the user, don't skip it.
2. Edit **all** configs identically (`allow_all`, `teams`). `python scripts/rls_all.py check` must pass.
   - Principals: `aaduser=<objectId>;<tenantId>`, `aadgroup=<objectId>;<tenantId>`, `aadapp=<appId>;<tenantId>`.
     Group id: `az ad group show -g "<name>" --query id -o tsv`. Tenant: `az account show --query tenantId -o tsv`.
   - `allow_all` must keep the admin who runs this, the hub ingestion engine app, and `finops-alerts-id` (the Azure
     Monitor alert rules run as it), or they lose access.
   - Team subscriptions must match `<table> | distinct SubAccountId` exactly per table (case-insensitive).
3. `rls_all.py preflight`, then `rls_all.py apply` (dry run); show the user the generated commands; after they confirm,
   `rls_all.py apply --execute`. Never use `--force`.
4. Verify: `rls_all.py verify`; in `Ingestion`:
   `.show table * policy row_level_security | where Policy != 'null' | extend p=parse_json(Policy) | project EntityName, Enabled=tostring(p.IsEnabled), Query=tostring(p.Query)`
   (8 rows, all `true`); in `Hub`: `Costs_v1_2() | count` still returns the admin's full count. A team change is only
   proven when a team member checks `Costs_v1_2() | summarize by SubAccountId` themselves.
5. Document: add a "Rollout log: <date>, <change>" section to `docs/rls_runbook.md` with every command run, in order,
   with results, ids replaced by placeholders (`<admin>`, `<engine>`, `<tenant>`, `<cluster>`, `<group>`). Save the exact
   output to `scripts/local/rollout-logs/rls_rollout_<date>_<change>.log` (git-ignored); back up configs before a
   change to `scripts/local/config-backups/<date>-<change>/`. Never commit real ids.

Disable (all tables): `rls_all.py rollback --execute`. Re-enable: `rls_all.py apply --execute`. One table only:
`python scripts/rls_runbook.py rollback|apply scripts/teams_<Name>_<ver>.yaml --execute`.

Gotchas:
- The connection to the cluster can drop. `rls_runbook.py` retries network errors; if a step still fails, check the
  table's real state (`.show table <t> policy row_level_security`, `.show functions | where Name startswith 'FinopsRls_'`)
  before re-running (all steps are idempotent).
- In zsh, `--execute` must be its own argument (a variable holding `apply --execute` is one word and is rejected).
- Rows with an empty `SubAccountId` match no team: only `allow_all` sees them.
- Removing a team: drop it from all configs and `apply --execute` (their rows disappear), but the script never revokes
  access it granted: also run `.drop database ['Hub'] viewers ('<principal>')` and the same in `Ingestion`, and log it.

## Cost alerts (Azure Monitor)
Runbook: `docs/alerts_runbook.md`. Rules are defined in `scripts/alerts.py` (`RULES`); environment ids in
`scripts/alerts.yaml` (git-ignored; rebuild from `scripts/alerts.example.yaml` and ask the user if missing).
To add or change a rule:
1. Write the query against `{costs}` (`adx('<cluster>/Ingestion').Costs_final_v1_2`: `adx()` can't call `Hub`
   functions). One row per alert, only when it should alert; put readable numbers in string columns (the email shows
   the dimension values). Days are UTC; the last complete day is 2 days back.
2. Add a `Rule(...)`, then `alerts.py test <rule>` and `alerts.py apply <rule>` (dry run). Tell the user what it will
   email and that it adds a small monthly cost; after they confirm, `apply <rule> --execute`.
3. Verify with `alerts.py status` after its first run. Stateful (auto-resolving) rules need `frequency <= PT12H`;
   daily reports are stateless. Log new one-time setup steps in the runbook with placeholders, never real ids.
The rules run as `finops-alerts-id`, which is in RLS `allow_all` (sees all cost data): keep alert recipients to people
allowed to see that. A rule that needs another table needs no RLS change; a new identity does (RLS procedure above).

# Architecture

## Goal
Users ask cost questions in natural language and get answers from FinOps Toolkit data in ADX, where
one team must never see another team's cost (scoped by subscription).

## Request flow
1. User signs in via Entra (custom Chainlit OAuth provider, `entra_provider.py`). The app receives an
   access token for its own API (`api://<client-id>/access_as_user`) plus a refresh token and keeps both
   **server-side** in memory (`session_tokens.py`), never in a cookie.
2. User asks a question. `agent.py` sends it to Claude with three tools.
3. Claude fetches reference material with `get_reference` when needed (schema, query catalog, weekly report workflow), then calls `run_kql`. `tools.py` runs `guard.validate_kql`, then `adx.py`.
4. `auth.py` exchanges the user's API token for an ADX token on-behalf-of the same user.
5. ADX authenticates the real user, applies the row-level-security policy, enforces `request_readonly`,
   the server timeout and row cap, and returns rows. Claude summarizes; the UI shows the KQL it ran.
6. Optional A2A entry (`A2A_ENABLED`, `docs/a2a.md`): another agent posts the question to `/a2a` with the user's
   delegated token for this app's API; it is validated and used as the OBO assertion in step 4, so the same RLS
   applies. App-only tokens are refused.

## Why these choices
- **ADX RLS + user identity instead of an app-side filter.** A shared credential plus an application-code
  filter means one bug leaks every team's data. With OBO the database enforces scope, so a wrong or
  malicious query still can't cross teams.
- **Free-form KQL instead of per-question functions.** Far less code to maintain; accuracy comes from
  the schema doc, few-shot examples, and evals instead of Python per question type. If a few questions
  dominate usage and need exact semantics, add curated ADX functions and list them in `ALLOWED_ENTITIES`.
- **Custom OAuth provider instead of Container Apps built-in auth.** Built-in auth does not provide the
  user's access token to the app unless you enable the (preview, blob-backed) token store, and Chainlit's
  built-in Azure provider returns a Microsoft Graph token that can't be exchanged for ADX.
- **One replica.** Tokens are in process memory. To scale out, replace `TokenStore` with Redis or similar.
- **No MCP server, no Databricks.** The app calls ADX directly with the Kusto SDK.

## Guardrails (defense in depth)
| Layer | Control |
| --- | --- |
| ADX | Database Viewer role only; RLS on the cost table; `request_readonly`; server timeout; row cap |
| Guard | Rejects `.` commands, `set`/`restrict`/`pattern`, `externaldata`, `http_request`, SQL/other plugins, `cluster()`/`database()`, `search`/`find`/`union *`; requires an allowed entity; masks comments and strings before checking |
| Tools | `describe_entity` only for allowlisted entities; results capped before reaching the model |
| Prompt | Read-only, never invent numbers, never work around access limits |
| Ops | JSON log per question/query/rejection/error to Log Analytics; golden-question evals |

The guard is a denylist and cannot prove a query safe. It is not the boundary.

## Verify before production
- RLS target: FinOps Hub keeps cost tables in the Ingestion database and exposes functions in the Hub
  database. Confirm which table to protect, that users have the access they need on both databases, and
  that RLS applies when queried through `Costs_v1_2()`.
- `SubAccountId` is documented as the subscription ARM path, not a bare GUID. Query a sample (`Costs_v1_2() | distinct SubAccountId | take 5`) and put those exact strings in `teams.yaml` (matching is case-insensitive). Also confirm your tag keys.
- The ingestion/ADF identity and FinOps admins are in `allow_all`, or ingestion and dashboards will break.
- `ALLOWED_ENTITIES` (default `Costs_v1_2()` and `Recommendations()`) match your hub version (v12+ for `Costs_v1_2()`); `prompts/schema.md` is the toolkit's reference; compare it with `describe_entity` output for your hub version.
- The toolkit queries hit `Recommendations()` too, so it must be readable by your users (and covered by your RLS plan, since it is not part of the cost table).
- Weekly report: the model runs its 7 queries on the user's behalf, so each person gets a report scoped to their own subscriptions. A org-wide report needs an admin identity and is not built here.
- Chainlit version: re-check `entra_provider.py` against the `OAuthProvider` interface after upgrades.
- Admin consent for the delegated Azure Data Explorer permission, or OBO fails with a consent error.
- `az bicep build` and `what-if` on `infra/main.bicep`.
- `ENTRA_CREDENTIAL=managed_identity`: the federated credential exists on the app registration (subject = the
  managed identity's principal id), and sign-in plus OBO both succeed. Error `AADSTS700213`/`AADSTS70021` means
  the issuer, subject or audience doesn't match.

## ADX permissions needed to enable RLS
Step-by-step commands: `docs/rls_runbook.md`.
- To run the generated KQL: **Database Admin** on the database that holds the table (the safe assumption;
  Microsoft documents Database Admin for the materialized-view RLS command and Table Admin for other table
  policy commands). It also covers creating the function and `.add database ... viewers`.
- To inspect (`.show ... policy row_level_security`): Database User, Viewer, or Monitor.
- No Azure control-plane role (Contributor on the cluster) is needed; these are data-plane commands.
- RLS cannot be enabled if the table is referenced by an update policy that does not use a managed identity,
  or has a restricted view access policy. Views materialized over the table need their own RLS policy.
- Keep the RLS function in the same database as the table.

## Ideas for later
- Scheduled weekly anomaly report reusing `Agent` and `ToolRunner` (a Container Apps job).
- Log question, KQL, row count, and feedback into an ADX table for quality tracking.
- Redis-backed token store and multiple replicas.
- Curated ADX functions for high-traffic questions.

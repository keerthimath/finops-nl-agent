# A2A (Agent2Agent) endpoint

Other agents can ask this agent cost questions **for a signed-in user** over the
[A2A protocol](https://a2a-protocol.org) (1.0, with 0.3 clients accepted on the same endpoint). They get the same
answer the chat would give that user, as Markdown plus the rows behind it.

| | |
| --- | --- |
| Agent card | `GET /.well-known/agent-card.json`, also at `/a2a/.well-known/agent-card.json` (public: name, skills, auth requirements; no data) |
| Endpoint | `POST /a2a`, JSON-RPC: `SendMessage`, `SendStreamingMessage` (SSE), `GetTask`, `ListTasks`, `CancelTask` (0.3: `message/send`, `message/stream`, `tasks/get`, ...) |
| Code | `src/finops_nl/a2a_server.py` (SDK glue), `src/finops_nl/a2a_core.py` (auth, conversations), `tests/test_a2a.py` |
| Switch | `A2A_ENABLED=true` (Bicep: `a2aEnabled`). Off by default. The card advertises `CHAINLIT_URL`. |

## Security: the same boundary as the chat
1. The caller sends the **user's delegated access token for this app's API**
   (`Authorization: Bearer <token>`, scope `api://<client-id>/access_as_user`).
2. `a2a_core.TokenValidator` checks the Entra signature, lifetime, issuer (this tenant), audience (this app) and the
   `access_as_user` scope. **App-only (client-credentials) tokens are refused**: there is no shared-credential path.
3. The token is exchanged on-behalf-of for an ADX token (`auth.OboExchanger`), so ADX sees the real user and RLS
   applies, exactly as in the chat. The guard, row cap and timeout apply too.

Tasks and conversations belong to the caller's identity: `GetTask` with another user's token returns "not found",
and reusing another user's `contextId` starts an empty conversation.

`AUTH_MODE=dev` needs no token and uses your `az login` identity, like the chat. Local only.

## Answers
A task completes with two artifacts:
- `answer`: Markdown (`text/markdown`), the same text the chat shows.
- `query_results` (only if queries ran and the request's `acceptedOutputModes` includes `application/json`;
  Foundry's A2A tool accepts text only): `application/json`,
  `{"queries": [{"source", "kql", "columns", "rows", "row_count", "truncated"}]}`, up to 100 rows per query.

With `SendStreamingMessage` the caller also gets a `working` status per query that runs ("Ran: Dashboard tile: ..."
or "Ran: KQL (written by the assistant)"). Send follow-ups with the same `contextId`; the conversation keeps the
same history the chat would (last 8 questions) and expires after 4 hours idle.

## Letting an agent call it (Entra)
The calling agent needs a token for **this** app's API, for its user:
- **An agent with its own backend** (the usual case): its app registration gets the delegated API permission
  `access_as_user` on this app (API permissions -> My APIs), an admin grants consent, and it exchanges its user's
  token on-behalf-of for `api://<client-id>/access_as_user`.
- **A client that signs the user in itself**: its app registration gets the same permission, and it requests that
  scope at sign-in.

Admin consent decides which applications may call: an app without the permission can't get a token this endpoint
accepts.

### Try it with the Azure CLI
Allow the Azure CLI as a client of this API once (app registration -> Expose an API -> Add a client application:
`04b07795-8ddb-461a-bbee-02f9e1bf7b46`, scope `access_as_user`). Then:
```bash
APP=https://<app-fqdn>
TOKEN=$(az account get-access-token --scope api://<client-id>/access_as_user --query accessToken -o tsv)
curl -s $APP/.well-known/agent-card.json | jq .name
curl -s $APP/a2a -H "Authorization: Bearer $TOKEN" -H "A2A-Version: 1.0" -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0", "id": 1, "method": "SendMessage",
  "params": {"message": {"messageId": "m1", "role": "ROLE_USER",
                         "parts": [{"text": "What did we spend last month, by subscription?"}]}}}' \
  | jq '.result.task.artifacts[] | select(.name == "answer") | .parts[0].text'
```
For streaming, use `"method": "SendStreamingMessage"` and `curl -N`. Without a token (or with an app-only token)
the endpoint returns 401 and nothing runs. Remove the Azure CLI client application afterwards if you don't need it.

## Calling it from a Microsoft Foundry agent (verified 2026-10-15)
Foundry's A2A tool must send the **user's** token, so the connection uses **OAuth identity passthrough with custom
OAuth**. Its other options don't work here: agent identity / project managed identity send app-only tokens (refused,
401), and "user Entra token" fails inside Foundry (its on-behalf-of request to this API is rejected).
1. Entra: register a client app (e.g. `finops-foundry-client`, single tenant). API permissions: add the delegated
   `access_as_user` of this app's registration, then **Grant admin consent** (check the row says *Granted*; without
   it the consent link loops). Add a client secret.
2. Foundry project -> Tools -> Connect tool -> Custom -> Agent2Agent (A2A):
   - Endpoint: the app's root URL (`https://<app-fqdn>`, no `/a2a`); the card is found at `/.well-known/agent-card.json`.
   - Authentication: OAuth identity passthrough, custom OAuth. Client id and secret from step 1; authorization URL
     `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/authorize`; token and refresh URL `.../oauth2/v2.0/token`.
   - Scopes: `api://<client-id-of-this-app>/access_as_user` and `offline_access` as **separate** entries. Check the
     saved connection lists both: the portal can drop one, and a custom OAuth connection can't be edited (delete and
     recreate it; the redirect URL then changes).
3. Entra: add the redirect URL Foundry shows for the connection to the client app (Authentication -> Web).
4. Agent: add the A2A tool with that connection (leave "send credentials for agent card" off: the card is public).
   Instructions: tell it to call the FinOps tool for any cost question, in the same turn. gpt-5-mini at reasoning
   effort `low` often replies "one moment" without calling the tool; `medium` works.
5. First question per user: the reply contains a consent link; sign in and accept, then ask again. Users of the
   Foundry agent need the Foundry Agent Consumer role (or Foundry User) on the project.

Foundry accepts text answers only ("Only text modality is supported"): it doesn't send `acceptedOutputModes` with
`application/json`, so it gets just the `answer` artifact. App logs show its calls as `"channel": "a2a"` under the
user's sign-in name.

## Limits
- One replica: tasks and conversations are in memory (like `TokenStore`).
- No push notifications; callers poll `GetTask` or stream.
- The user's token must be valid when the first query runs; ADX tokens from the exchange are then cached per user.
- Logs: `question` and `agent_error` events carry `"channel": "a2a"`; refused calls log `a2a_denied` (reason only,
  never the token). Turns appear on `/admin` like chat turns.

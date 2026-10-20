# Runbook: deploy to Azure Container Apps

Step by step, with a check after each step. Run everything in **one terminal**: the variables only live there.
Do `docs/entra_setup.md` first. Do `docs/rls_runbook.md` before anyone else signs in.

## Before you start
| You need | Why |
| --- | --- |
| `az login` to the target subscription, Docker running | Build, push, deploy |
| **Owner** or **User Access Administrator** on the resource group | The template creates role assignments (AcrPull, Foundry) |
| **Owner** of the Entra app registration, or Application Administrator | Steps 6.1 and 6.2 |
| App registration client ID, `CHAINLIT_AUTH_SECRET`, ADX cluster URI, LLM key or Foundry resource | Deploy parameters |

Generate `CHAINLIT_AUTH_SECRET` once per environment: `chainlit create-secret` (or `openssl rand -base64 48`).

## 1. Set variables
```bash
cd <repo root>
RG=<resource-group>
LOC=<region>                     # e.g. eastus; must match the environment's region if you reuse one (step 4)
ACR=<registry-name>              # 5-50 lowercase letters/digits, globally unique
TAG=<version>                    # new tag per release, e.g. 0.1.2
```
**Check:** `echo $RG $LOC $ACR $TAG` prints four values.

## 2. Registry (first time only)
```bash
az group create -n $RG -l $LOC
az acr create -g $RG -n $ACR --sku Basic
```
**Check:** `az acr show -n $ACR --query loginServer -o tsv` prints `<acr>.azurecr.io`.
The registry must be in `$RG`.

## 3. Build and push the image
Optional: to ship dashboard queries, copy the export to `dashboards/` first (`dashboards/README.md`) and check it
with `PYTHONPATH=src python -m finops_nl.dashboard dashboards/<name>.json`. Then run
`python scripts/check_queries.py --only dashboard --write-exclude` (see `dashboards/README.md`) so tiles that fail
on your hub are left out, and add `dashboardFile=/app/dashboards/<name>.json` to `PARAMS` in step 4.
```bash
az acr login -n $ACR
docker buildx build --platform linux/amd64 -t $ACR.azurecr.io/finops-nl-agent:$TAG --push .
```
**Check:** `az acr repository show-tags -n $ACR --repository finops-nl-agent -o table` lists `$TAG`.

- `--platform linux/amd64` is required on Apple Silicon. Container Apps runs amd64.
- `az acr build -r $ACR -t finops-nl-agent:$TAG .` also works, but not on free/trial subscriptions
  (`TasksOperationsNotAllowed`).

## 4. Deploy
Load the deployment settings, including the admin allow-lists, from `.env`. Secrets entered below are not echoed or added to shell history:
```bash
set -a; source .env; set +a
read -s CHAINLIT_SECRET          # paste, Enter
read -s LLM_KEY                  # paste the LLM key, Enter (skip for Foundry / Azure OpenAI with entra)

PARAMS=(
  image=$ACR.azurecr.io/finops-nl-agent:$TAG
  acrName=$ACR
  location=$LOC
  entraClientId=<app-registration-client-id>
  chainlitAuthSecret="$CHAINLIT_SECRET"
  adxClusterUri=https://<cluster>.<region>.kusto.windows.net
  adxDatabase=Hub
  adminUsers="$ADMIN_USERS"
  adminGroupIds="$ADMIN_GROUP_IDS"
  # logWorkspaceResourceId=<workspace resource id>   # only when reusing an environment (see below)
  # LLM: pick one block
  llmProvider=openai openaiApiKey="$LLM_KEY" openaiModel=gpt-5
  # llmProvider=anthropic anthropicApiKey="$LLM_KEY" anthropicModel=claude-sonnet-5-5
  # llmProvider=foundry foundryResourceName=<name> foundryResourceGroup=<rg> foundrySubscriptionId=<sub> anthropicModel=<deployment>
  # Azure OpenAI deployment in Foundry, no key (recommended): the app identity gets Cognitive Services User on it
  # llmProvider=azure_openai azureOpenAIEndpoint=https://<resource>.openai.azure.com azureOpenAIDeployment=<deployment> foundryResourceName=<resource> foundryResourceGroup=<rg> foundrySubscriptionId=<sub>
  # Azure OpenAI with a key instead
  # llmProvider=azure_openai azureOpenAIEndpoint=https://<resource>.openai.azure.com azureOpenAIDeployment=<deployment> azureOpenAIAuth=key azureOpenAIApiKey="$LLM_KEY"
  # For gpt-4o / gpt-4.1 deployments (not reasoning models) also add: openaiReasoningEffort=''
)
```
If the subscription allows only one Container Apps environment and one already exists
(`MaxNumberOfGlobalEnvironmentsInSubExceeded`), reuse it, and set `LOC` to its region:
```bash
az containerapp env list --query "[].{name:name, rg:resourceGroup, location:location}" -o table
PARAMS+=(existingEnvironmentName=<name> existingEnvironmentResourceGroup=<rg>)
# For /admin history: the workspace that environment logs to
az monitor log-analytics workspace list --query "[?customerId=='$(az containerapp env show -g <rg> -n <name> \
  --query properties.appLogsConfiguration.logAnalyticsConfiguration.customerId -o tsv)'].id" -o tsv
PARAMS+=(logWorkspaceResourceId=<that id>)
```
Preview, then deploy:
```bash
az deployment group what-if -g $RG -f infra/main.bicep -p "${PARAMS[@]}"
az deployment group create  -g $RG -n finops-nl -f infra/main.bicep -p "${PARAMS[@]}"
```
**Check:** the deployment ends with `"provisioningState": "Succeeded"`.

- **Rebuild `PARAMS` after changing `TAG`.** The array copies the value when you create it.
- **First deploy can fail the image pull** while the AcrPull role assignment propagates. Wait 1-2 minutes and rerun `create`.
- **Claude on Foundry** can't be bought on a free subscription (Marketplace plan). Use another subscription or
  another `llmProvider`.
- **Azure OpenAI resource name:** the `<resource>` part of the endpoint. Find its resource group and subscription with
  `az cognitiveservices account list --query "[].{name:name, rg:resourceGroup}" -o table` (names keep their capitals).

## 5. Check the app is running
```bash
az containerapp revision list -g $RG -n finops-nl \
  --query "[].{name:name, image:properties.template.containers[0].image, health:properties.healthState, running:properties.runningState}" -o table
az containerapp logs show -g $RG -n finops-nl --type console --tail 30 --format text
```
**Check:** the newest revision shows your `$TAG`, `Healthy` and `Running`. The log has
`Your app is available at http://0.0.0.0:8000`.

"Activation failed" / "0/1 replicas ready" means the container crashed. The console log shows the Python error.
A config error such as `LLM_PROVIDER must be ...` usually means the running image is older than the code: check the tag.

## 6. Link the app registration (first time only)
```bash
az deployment group show -g $RG -n finops-nl --query properties.outputs -o json
```
### 6.1 Federated credential (lets the app sign in to Entra without a client secret)
Run the `federatedCredentialCommand` output as a command.
Or in the portal: app registration → **Certificates & secrets → Federated credentials → Add credential** →
*Managed Identity* → `finops-nl-id`.

**Check:** `az ad app federated-credential list --id <client-id> -o table` shows one entry with audience
`api://AzureADTokenExchange`.

### 6.2 Redirect URI
Portal: app registration → **Authentication → Web → Add URI** → paste the `redirectUri` output
(`https://<fqdn>/auth/oauth/entra-obo/callback`). Keep the localhost entry. **Save**.

**Check:** `az ad app show --id <client-id> --query web.redirectUris` includes the `https://` URI.

## 7. Sign in and ask a question
Open `https://<fqdn>` in a private window, sign in, and ask "what is the overall spend?".

**Check:** an answer with a KQL block. In the logs, `kql_ok` events.
Then open `https://<fqdn>/admin` (the `adminUrl` output): your question is listed with its rounds, queries,
time and tokens. "Could not read logs" there means the Log Analytics Reader role hasn't propagated yet (a few
minutes) or `logWorkspaceResourceId` wasn't passed for a reused environment.

| Error | Fix |
| --- | --- |
| `AADSTS50011`, redirect URI starts with `http://` | `CHAINLIT_URL` is missing. The template sets it; for a hand-patched app: `az containerapp update -g $RG -n finops-nl --set-env-vars CHAINLIT_URL=https://<fqdn>` |
| `AADSTS50011`, `https://` URI | Step 6.2: the URI isn't registered exactly (scheme, path, no trailing slash) |
| `AADSTS700213` / `AADSTS70021` | Step 6.1: the federated credential is missing, or its subject isn't the identity's principal ID |
| `AADSTS65001` (consent) | Admin consent for the Azure Data Explorer delegated permission (`docs/entra_setup.md` step 5) |
| Signed in, queries fail with access denied | The user needs **Viewer** on the ADX database |

## 8. Before inviting others
- Enable RLS: `docs/rls_runbook.md` (non-prod first). Until then each user sees whatever their own ADX permissions allow.
- Run the evals against real data with the deployed `LLM_PROVIDER`: `python evals/run_evals.py`.
- Enterprise application: *Assignment required* = Yes, with the team groups assigned.

## Releasing a new version
Set a new `TAG`, repeat step 3, then either rerun step 4 (rebuild `PARAMS`), or swap only the image:
```bash
az containerapp update -g $RG -n finops-nl --image $ACR.azurecr.io/finops-nl-agent:$TAG
```
Then repeat the step 5 check. If you swap only the image, pass the same tag next time you run step 4,
or the deploy rolls the app back.

## Switching the LLM provider on a running app
Use this to move a deployed app to an **Azure OpenAI deployment in Foundry** without a full redeploy (for example from
`openai` with an API key). The image must be `0.1.7` or later (`LLM_PROVIDER=azure_openai` support); if it's older,
do "Releasing a new version" first.

1. **Load the values from your `.env`** (endpoint, deployment, reasoning effort). Nothing is printed. Quote values
   that contain `?`, `&` or `()` (e.g. `AZURE_OPENAI_ENDPOINT='https://...?api-version=...'`), or zsh/bash `source` fails:
   ```bash
   set -a; source .env; set +a
   AOAI_NAME=$(echo "$AZURE_OPENAI_ENDPOINT" | sed -E 's#^https?://##; s#[./].*##')
   # Azure keeps the name's original capitals (e.g. MyOpenAI-Resource): match without case
   AOAI_ID=$(az cognitiveservices account list --query "[].id" -o tsv | grep -i "/accounts/$AOAI_NAME$" | head -1)
   echo "$AOAI_NAME -> $AOAI_ID"
   ```
   **Check:** a resource id is printed. Empty means the name didn't match or the resource is in another subscription
   (`az account set -s <sub>` and retry).

2. **Pick how the app signs in to it.**

   *Managed identity, no key (recommended):* give the app's identity access to the resource, then switch.
   ```bash
   PRINCIPAL=$(az identity show -g $RG -n finops-nl-id --query principalId -o tsv)
   az role assignment create --assignee-object-id $PRINCIPAL --assignee-principal-type ServicePrincipal \
     --role "Cognitive Services User" --scope "$AOAI_ID"
   az containerapp update -g $RG -n finops-nl --set-env-vars \
     LLM_PROVIDER=azure_openai AZURE_OPENAI_AUTH=entra \
     AZURE_OPENAI_ENDPOINT="$AZURE_OPENAI_ENDPOINT" AZURE_OPENAI_DEPLOYMENT="$AZURE_OPENAI_DEPLOYMENT" \
     OPENAI_REASONING_EFFORT="$OPENAI_REASONING_EFFORT"
   ```
   The app finds its identity through `AZURE_CLIENT_ID` (set by the template; check with
   `az containerapp show -g $RG -n finops-nl --query "properties.template.containers[0].env[?name=='AZURE_CLIENT_ID']"`).
   The role can take a few minutes to apply.

   *Key:* store it as a Container App secret, then switch.
   ```bash
   az containerapp secret set -g $RG -n finops-nl --secrets azure-openai-api-key="$AZURE_OPENAI_API_KEY"
   az containerapp update -g $RG -n finops-nl --set-env-vars \
     LLM_PROVIDER=azure_openai AZURE_OPENAI_AUTH=key AZURE_OPENAI_API_KEY=secretref:azure-openai-api-key \
     AZURE_OPENAI_ENDPOINT="$AZURE_OPENAI_ENDPOINT" AZURE_OPENAI_DEPLOYMENT="$AZURE_OPENAI_DEPLOYMENT" \
     OPENAI_REASONING_EFFORT="$OPENAI_REASONING_EFFORT"
   ```

3. **Check:** ask a question in the app, then
   ```bash
   az containerapp logs show -g $RG -n finops-nl --type console --tail 40 --format text | grep -E "azure.com|agent_turn|Error"
   ```
   A `POST https://<resource>...azure.com/openai/deployments/<deployment>/chat/completions ... 200 OK` and an
   `agent_turn` line mean it works. `/admin` shows the deployment name in each question's details.

| Error in the log | Fix |
| --- | --- |
| `401` / `PermissionDenied` (entra) | The role hasn't applied yet (wait a few minutes), or it was assigned on a different resource |
| `401` (key) | Wrong key, or the secret wasn't updated: re-run `secret set`, then restart the revision |
| `404` / `DeploymentNotFound` | `AZURE_OPENAI_DEPLOYMENT` must be the deployment name, not the model name |
| `Unrecognized request argument supplied: reasoning_effort` | Not a reasoning model (gpt-4o / gpt-4.1): set `OPENAI_REASONING_EFFORT=` (empty) |
| `LLM_PROVIDER must be ...` at startup | The image is older than 0.1.7 |

**Revert:** `az containerapp update -g $RG -n finops-nl --set-env-vars LLM_PROVIDER=openai` (the OpenAI key secret is
still there). Once the new provider works, remove the old key: `az containerapp secret remove -g $RG -n finops-nl
--secret-names openai-api-key` after removing `OPENAI_API_KEY` with `--remove-env-vars OPENAI_API_KEY`, and revoke it
at platform.openai.com.

**Next Bicep deploy:** pass the matching `llmProvider=azure_openai ...` line from step 4, or it switches the app back.

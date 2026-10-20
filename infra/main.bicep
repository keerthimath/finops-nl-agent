// One Container App (Chainlit) + Log Analytics. NOT validated with `az bicep build` in the authoring
// environment - run `az bicep build -f infra/main.bicep` and `az deployment group what-if` first.
targetScope = 'resourceGroup'

param location string = resourceGroup().location
param namePrefix string = 'finops-nl'

@description('Reuse an existing Container Apps environment instead of creating one (e.g. a subscription limited to one). Set `location` to that environment\'s region.')
param existingEnvironmentName string = ''
@description('Resource group of existingEnvironmentName. Defaults to this deployment\'s resource group.')
param existingEnvironmentResourceGroup string = resourceGroup().name
@description('With existingEnvironmentName: resource id of the Log Analytics workspace that environment logs to, so /admin can read it. Empty = /admin shows only the running process.')
param logWorkspaceResourceId string = ''
@description('Comma-separated sign-in names (emails) allowed on /admin.')
param adminUsers string = ''
@description('Comma-separated Entra group object IDs allowed on /admin. The app registration must emit a groups claim.')
param adminGroupIds string = ''
@description('Optional USD per million tokens, for the /admin cost estimate (input, cached input, output).')
param priceInputPerMTok string = ''
param priceCachedPerMTok string = ''
param priceOutputPerMTok string = ''
@description('A2A endpoint for other agents (docs/a2a.md): they call with the user\'s delegated token; RLS applies.')
param a2aEnabled bool = false

@description('Full image reference, e.g. myacr.azurecr.io/finops-nl-agent:0.1.0')
param image string
@description('Name of an existing Azure Container Registry in this resource group')
param acrName string

param tenantId string = tenant().tenantId
@description('Entra app registration (client) id used for sign-in and on-behalf-of')
param entraClientId string
@allowed([ 'managed_identity', 'secret' ])
@description('managed_identity = no client secret: the app registration trusts this deployment\'s managed identity through a federated credential (create it after deploying; see the federatedCredentialCommand output).')
param entraCredential string = 'managed_identity'
@secure()
@description('Only for entraCredential = secret.')
param entraClientSecret string = ''
@allowed([ 'foundry', 'anthropic', 'openai', 'azure_openai' ])
param llmProvider string = 'foundry'
@description('Microsoft Foundry resource (Microsoft.CognitiveServices/accounts) with a Claude deployment (llmProvider = foundry), or the resource holding the Azure OpenAI deployment (llmProvider = azure_openai with azureOpenAIAuth = entra): the app identity gets Cognitive Services User on it.')
param foundryResourceName string = ''
@description('Subscription of the Foundry resource. Defaults to this deployment\'s subscription.')
param foundrySubscriptionId string = subscription().subscriptionId
@description('Resource group of the Foundry resource. Defaults to this deployment\'s resource group.')
param foundryResourceGroup string = resourceGroup().name
@secure()
@description('Only for llmProvider = anthropic. Foundry uses the managed identity (Entra ID), no key.')
param anthropicApiKey string = ''
@secure()
@description('Only for llmProvider = openai.')
param openaiApiKey string = ''
@description('Only for llmProvider = openai.')
param openaiModel string = 'gpt-5'
@description('llmProvider = openai or azure_openai. low keeps latency and cost down; empty = provider default (and for non-reasoning models such as gpt-4o).')
param openaiReasoningEffort string = 'medium'
@description('Only for llmProvider = azure_openai: https://<resource>.openai.azure.com (or the resource endpoint Foundry shows).')
param azureOpenAIEndpoint string = ''
@description('Only for llmProvider = azure_openai: the deployment name.')
param azureOpenAIDeployment string = ''
@allowed([ 'entra', 'key' ])
@description('Only for llmProvider = azure_openai: entra = the app identity (no key stored; set foundryResourceName etc. for the role); key = azureOpenAIApiKey.')
param azureOpenAIAuth string = 'entra'
@secure()
@description('Only for llmProvider = azure_openai with azureOpenAIAuth = key.')
param azureOpenAIApiKey string = ''
@secure()
@description('Output of `chainlit create-secret`')
param chainlitAuthSecret string

param adxClusterUri string
param adxDatabase string = 'Hub'
param allowedEntities string = 'Costs_v1_2(),Recommendations()'
@description('Optional: path inside the image of an ADX dashboard export, e.g. /app/dashboards/finops.json (see dashboards/README.md)')
param dashboardFile string = ''
@description('Model id, or the Foundry deployment name when llmProvider = foundry')
param anthropicModel string = 'claude-sonnet-5-5'

var useFoundry = llmProvider == 'foundry'
var useOpenAI = llmProvider == 'openai'
var useAzureOpenAI = llmProvider == 'azure_openai'
var azureOpenAIKey = useAzureOpenAI && azureOpenAIAuth == 'key'
var useEntraSecret = entraCredential == 'secret'

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: acrName
}

var createEnvironment = empty(existingEnvironmentName)

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = if (createEnvironment) {
  name: '${namePrefix}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = if (createEnvironment) {
  name: '${namePrefix}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs!.properties.customerId
        sharedKey: logs!.listKeys().primarySharedKey
      }
    }
  }
}

resource existingEnv 'Microsoft.App/managedEnvironments@2024-03-01' existing = if (!createEnvironment) {
  name: existingEnvironmentName
  scope: resourceGroup(existingEnvironmentResourceGroup)
}

var environmentId = createEnvironment ? env.id : existingEnv.id

// Workspace /admin reads: the one created here, or the reused environment's (logWorkspaceResourceId).
var useExistingWorkspace = !createEnvironment && !empty(logWorkspaceResourceId)
// Placeholder when empty: ARM can evaluate both branches of a conditional, so indexing must never go out of range.
var wsParts = split(empty(logWorkspaceResourceId) ? '/subscriptions/none/resourceGroups/none/providers/none/workspaces/none' : logWorkspaceResourceId, '/')
resource existingWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = if (useExistingWorkspace) {
  name: last(wsParts)
  scope: resourceGroup(wsParts[2], wsParts[4])
}
var adminWorkspaceId = createEnvironment ? logs!.properties.customerId : (useExistingWorkspace ? existingWorkspace!.properties.customerId : '')
// Public URL of the app. Ingress terminates TLS, so Chainlit sees http and would send an http OAuth redirect URI.
var publicUrl = 'https://${namePrefix}.${createEnvironment ? env!.properties.defaultDomain : existingEnv!.properties.defaultDomain}'

resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-id'
  location: location
}

module workspaceReader 'workspace-reader.bicep' = if (createEnvironment || useExistingWorkspace) {
  name: '${namePrefix}-workspace-reader'
  scope: resourceGroup(useExistingWorkspace ? wsParts[2] : subscription().subscriptionId, useExistingWorkspace ? wsParts[4] : resourceGroup().name)
  params: {
    workspaceName: useExistingWorkspace ? last(wsParts) : '${namePrefix}-logs'
    principalId: uami.properties.principalId
  }
  dependsOn: [ logs ]
}

// Cognitive Services User on the Foundry / Azure OpenAI resource, which may be in another resource group or subscription.
module foundryRole 'foundry-role.bicep' = if (useFoundry || (useAzureOpenAI && !azureOpenAIKey)) {
  name: '${namePrefix}-foundry-role'
  scope: resourceGroup(foundrySubscriptionId, foundryResourceGroup)
  params: {
    foundryResourceName: foundryResourceName
    principalId: uami.properties.principalId
  }
}

// AcrPull
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, uami.id, 'acrpull')
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: namePrefix
  location: location  // must match the environment's region
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${uami.id}': {} }
  }
  dependsOn: [ acrPull ]
  properties: {
    managedEnvironmentId: environmentId
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
      }
      registries: [
        { server: acr.properties.loginServer, identity: uami.id }
      ]
      secrets: concat([
        { name: 'chainlit-auth-secret', value: chainlitAuthSecret }
      ], useEntraSecret ? [
        { name: 'entra-client-secret', value: entraClientSecret }
      ] : [], llmProvider == 'anthropic' ? [
        { name: 'anthropic-api-key', value: anthropicApiKey }
      ] : [], useOpenAI ? [
        { name: 'openai-api-key', value: openaiApiKey }
      ] : [], azureOpenAIKey ? [
        { name: 'azure-openai-api-key', value: azureOpenAIApiKey }
      ] : [])
    }
    template: {
      containers: [
        {
          name: 'app'
          image: image
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: concat([
            { name: 'AUTH_MODE', value: 'obo' }
            { name: 'CHAINLIT_URL', value: publicUrl }
            { name: 'ADX_CLUSTER_URI', value: adxClusterUri }
            { name: 'ADX_DATABASE', value: adxDatabase }
            { name: 'ALLOWED_ENTITIES', value: allowedEntities }
            { name: 'DASHBOARD_FILE', value: dashboardFile }
            // DefaultAzureCredential (Foundry, /admin's Log Analytics reads) picks the user-assigned identity by id.
            { name: 'AZURE_CLIENT_ID', value: uami.properties.clientId }
            { name: 'ADMIN_USERS', value: adminUsers }
            { name: 'ADMIN_GROUP_IDS', value: adminGroupIds }
            { name: 'ADMIN_LOG_WORKSPACE_ID', value: adminWorkspaceId }
            { name: 'PRICE_INPUT_PER_MTOK', value: priceInputPerMTok }
            { name: 'PRICE_CACHED_PER_MTOK', value: priceCachedPerMTok }
            { name: 'PRICE_OUTPUT_PER_MTOK', value: priceOutputPerMTok }
            { name: 'A2A_ENABLED', value: a2aEnabled ? 'true' : 'false' }
            { name: 'ANTHROPIC_MODEL', value: anthropicModel }
            { name: 'ENTRA_TENANT_ID', value: tenantId }
            { name: 'ENTRA_CLIENT_ID', value: entraClientId }
            { name: 'CHAINLIT_AUTH_SECRET', secretRef: 'chainlit-auth-secret' }
          ], useEntraSecret ? [
            { name: 'ENTRA_CREDENTIAL', value: 'secret' }
            { name: 'ENTRA_CLIENT_SECRET', secretRef: 'entra-client-secret' }
          ] : [
            { name: 'ENTRA_CREDENTIAL', value: 'managed_identity' }
            { name: 'ENTRA_MI_CLIENT_ID', value: uami.properties.clientId }
          ], useFoundry ? [
            { name: 'LLM_PROVIDER', value: 'foundry' }
            { name: 'FOUNDRY_AUTH', value: 'entra' }
            { name: 'ANTHROPIC_FOUNDRY_RESOURCE', value: foundryResourceName }
          ] : useOpenAI ? [
            { name: 'LLM_PROVIDER', value: 'openai' }
            { name: 'OPENAI_MODEL', value: openaiModel }
            { name: 'OPENAI_REASONING_EFFORT', value: openaiReasoningEffort }
            { name: 'OPENAI_API_KEY', secretRef: 'openai-api-key' }
          ] : useAzureOpenAI ? concat([
            { name: 'LLM_PROVIDER', value: 'azure_openai' }
            { name: 'AZURE_OPENAI_ENDPOINT', value: azureOpenAIEndpoint }
            { name: 'AZURE_OPENAI_DEPLOYMENT', value: azureOpenAIDeployment }
            { name: 'AZURE_OPENAI_AUTH', value: azureOpenAIAuth }
            { name: 'OPENAI_REASONING_EFFORT', value: openaiReasoningEffort }
          ], azureOpenAIKey ? [
            { name: 'AZURE_OPENAI_API_KEY', secretRef: 'azure-openai-api-key' }
          ] : []) : [
            { name: 'LLM_PROVIDER', value: 'anthropic' }
            { name: 'ANTHROPIC_API_KEY', secretRef: 'anthropic-api-key' }
          ])
        }
      ]
      // One replica on purpose: user tokens are held in process memory (see session_tokens.py).
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}

output fqdn string = app.properties.configuration.ingress.fqdn
output redirectUri string = 'https://${app.properties.configuration.ingress.fqdn}/auth/oauth/entra-obo/callback'
// Federated credential on the app registration (entraCredential = managed_identity). Run once after the first
// deploy; Bicep can't create it without the Microsoft Graph extension.
output federatedCredentialCommand string = 'az ad app federated-credential create --id ${entraClientId} --parameters \'{"name":"${namePrefix}-container-app","issuer":"${environment().authentication.loginEndpoint}${tenantId}/v2.0","subject":"${uami.properties.principalId}","audiences":["api://AzureADTokenExchange"]}\''
output adminUrl string = 'https://${app.properties.configuration.ingress.fqdn}/admin'

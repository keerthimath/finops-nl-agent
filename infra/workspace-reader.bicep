// Log Analytics Reader on the workspace: lets the app's identity read its own per-question records for /admin.
// A module so the workspace can live in another resource group or subscription (a reused environment).
param workspaceName string
param principalId string

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: workspaceName
}

resource reader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: workspace
  name: guid(workspace.id, principalId, 'loganalyticsreader')
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '73c42c96-874c-492b-b04d-ab87d138a893')
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}

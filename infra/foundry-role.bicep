// Cognitive Services User on the Foundry resource: lets the app's identity call the Claude deployment.
// A module so the Foundry resource can live in another resource group or subscription.
param foundryResourceName string
param principalId string

resource foundry 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: foundryResourceName
}

resource foundryUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundry
  name: guid(foundry.id, principalId, 'cognitiveservicesuser')
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'a97b65f3-24c7-4388-baec-2e87135dc908')
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}

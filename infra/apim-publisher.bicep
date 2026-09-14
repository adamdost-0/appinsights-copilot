targetScope = 'resourceGroup'

param dcrName string
param principalId string
param apimResourceId string

resource rule 'Microsoft.Insights/dataCollectionRules@2024-03-11' existing = {
  name: dcrName
}

resource publisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  // Stable across MI recreation; replacing a stale principal requires explicit role cleanup.
  name: guid(rule.id, apimResourceId, 'copilot-otel-apim-publisher')
  scope: rule
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '3913510d-42f4-4e42-8a64-420c390055eb')
  }
}

output dcrResourceId string = rule.id
output roleAssignmentId string = publisher.id

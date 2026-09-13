targetScope = 'subscription'

param location string = 'eastus'

@minLength(36)
@maxLength(36)
@description('Ownership receipt UUID. Use scripts.deploy to generate and retain it.')
param ownershipMarker string

param workspaceName string = 'law-copilot-otel-${uniqueString(subscription().id, 'rg-copilot-otel-audit')}'
param applicationInsightsName string = 'ai-copilot-otel-${uniqueString(subscription().id, 'rg-copilot-otel-audit')}'

var resourceGroupName = 'rg-copilot-otel-audit'
var tags = {
  solution: 'copilot-otel-audit'
  'ownership-marker': ownershipMarker
}

resource group 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module resources './resources.bicep' = {
  name: 'copilot-otel-audit-resources'
  scope: group
  params: {
    location: location
    tags: tags
    workspaceName: workspaceName
    applicationInsightsName: applicationInsightsName
  }
}

output applicationInsightsResourceId string = resources.outputs.applicationInsightsResourceId
output workspaceResourceId string = resources.outputs.workspaceResourceId
output workspaceCustomerId string = resources.outputs.workspaceCustomerId

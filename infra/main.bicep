targetScope = 'subscription'

param location string = 'eastus'

@minLength(36)
@maxLength(36)
param ownershipMarker string

@description('Object ID of the identity that publishes Copilot OTLP telemetry.')
@minLength(36)
@maxLength(36)
param principalId string

@allowed([
  'User'
  'ServicePrincipal'
  'Group'
])
param principalType string = 'User'

@description('Object ID of the operator that queries workspace logs and native metrics.')
@minLength(36)
@maxLength(36)
param operatorPrincipalId string = principalId

@allowed([
  'User'
  'ServicePrincipal'
  'Group'
])
param operatorPrincipalType string = principalType

var tags = {
  solution: 'copilot-otel-v1'
  'ownership-marker': ownershipMarker
}

resource group 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-copilot-otel-v1'
  location: location
  tags: tags
}

module resources './resources.bicep' = {
  name: 'copilot-otel-v1-resources'
  scope: group
  params: {
    location: location
    tags: tags
    principalId: principalId
    principalType: principalType
    operatorPrincipalId: operatorPrincipalId
    operatorPrincipalType: operatorPrincipalType
  }
}

output nativeState object = resources.outputs.nativeState

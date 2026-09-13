targetScope = 'subscription'

param location string = 'eastus'

@minLength(36)
@maxLength(36)
param ownershipMarker string

@minLength(36)
@maxLength(36)
param principalId string

@allowed([
  'User'
  'ServicePrincipal'
  'Group'
])
param principalType string = 'User'

var tags = {
  solution: 'copilot-native-otel'
  'ownership-marker': ownershipMarker
}

resource group 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-copilot-native-otel'
  location: location
  tags: tags
}

module resources './native-resources.bicep' = {
  name: 'copilot-native-otel-resources'
  scope: group
  params: {
    location: location
    tags: tags
    principalId: principalId
    principalType: principalType
  }
}

output nativeState object = resources.outputs.nativeState

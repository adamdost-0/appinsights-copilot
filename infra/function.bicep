targetScope = 'subscription'

param location string = 'eastus'

@minLength(36)
@maxLength(36)
param ownershipMarker string

@description('Approved client AND deployment-agent public IPv4 CIDRs. Never use 0.0.0.0/0. Validate with the runbook before deployment.')
@minLength(1)
@maxLength(100)
param allowedIPv4Cidrs array

@description('Existing DCR in this subscription; no changes to the native resource contract.')
param dcrResourceGroupName string
param dcrName string

@description('Exact HTTPS trace URL from verified nativeState; validate all endpoints with the runbook.')
@minLength(9)
param tracesEndpoint string
@description('Exact HTTPS logs URL from verified nativeState.')
@minLength(9)
param logsEndpoint string
@description('Exact HTTPS metrics URL from verified nativeState.')
@minLength(9)
param metricsEndpoint string

var tags = {
  solution: 'copilot-otel-relay'
  'ownership-marker': ownershipMarker
}

resource group 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-copilot-otel-relay'
  location: location
  tags: tags
}

module resources './function-resources.bicep' = {
  name: 'copilot-otel-relay-resources'
  scope: group
  params: {
    location: location
    tags: tags
    allowedIPv4Cidrs: allowedIPv4Cidrs
    tracesEndpoint: tracesEndpoint
    logsEndpoint: logsEndpoint
    metricsEndpoint: metricsEndpoint
  }
}

module publisher './function-publisher.bicep' = {
  name: 'copilot-otel-relay-publisher'
  scope: resourceGroup(dcrResourceGroupName)
  params: {
    dcrName: dcrName
    principalId: resources.outputs.principalId
  }
}

output relayState object = {
  subscription_id: subscription().subscriptionId
  resource_group: group.name
  location: location
  ownership_marker: ownershipMarker
  function_app_name: resources.outputs.functionAppName
  function_app_resource_id: resources.outputs.functionAppResourceId
  principal_id: resources.outputs.principalId
  storage_resource_id: resources.outputs.storageResourceId
  traces_endpoint: '${resources.outputs.baseEndpoint}/v1/traces'
  logs_endpoint: '${resources.outputs.baseEndpoint}/v1/logs'
  metrics_endpoint: '${resources.outputs.baseEndpoint}/v1/metrics'
  dcr_resource_id: publisher.outputs.dcrResourceId
  publisher_role_assignment_id: publisher.outputs.roleAssignmentId
}

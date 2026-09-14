targetScope = 'subscription'

param location string = 'eastus'

@description('Fresh gateway ownership UUID. Validate native receipt and readbacks with the offline APIM preflight before deployment.')
@minLength(36)
@maxLength(36)
param ownershipMarker string

@allowed([
  'Developer'
])
param skuName string = 'Developer'

@description('False installs deny baseline and operation policies only. Set true only after separate policy/readback review; this is not runtime acceptance.')
param activateGateway bool = false

@minLength(3)
@maxLength(100)
param publisherEmail string
@minLength(1)
@maxLength(100)
param publisherName string

@description('Existing receipt-owned DCR in this subscription. Only an additive publisher assignment is deployed there.')
param dcrResourceGroupName string
param dcrName string

@description('Complete verified nativeState logs endpoint, not a reconstructed host.')
@minLength(9)
param logsEndpoint string
@description('Complete verified nativeState traces endpoint.')
@minLength(9)
param tracesEndpoint string
@description('Complete verified nativeState metrics endpoint; its host may differ from logs.')
@minLength(9)
param metricsEndpoint string

var tags = {
  solution: 'copilot-otel-apim'
  'ownership-marker': ownershipMarker
}

resource group 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-copilot-otel-apim'
  location: location
  tags: tags
}

var apimName = 'apim-copilot-otel-${uniqueString(group.id)}'
var apimResourceId = resourceId(subscription().subscriptionId, group.name, 'Microsoft.ApiManagement/service', apimName)

module service './apim-service.bicep' = {
  name: 'apim-service'
  scope: group
  params: {
    location: location
    tags: tags
    apimName: apimName
    skuName: skuName
    publisherEmail: publisherEmail
    publisherName: publisherName
  }
}

// Reapply deny on updates too. Never deploy the later modules independently.
module baseline './apim-baseline.bicep' = {
  name: 'apim-baseline'
  scope: group
  params: {
    serviceName: service.outputs.serviceName
  }
}

module operations './apim-operations.bicep' = {
  name: 'apim-operations'
  scope: group
  params: {
    serviceName: service.outputs.serviceName
    logsEndpoint: logsEndpoint
    tracesEndpoint: tracesEndpoint
    metricsEndpoint: metricsEndpoint
  }
  dependsOn: [
    baseline
  ]
}

module publisher './apim-publisher.bicep' = {
  name: 'apim-publisher'
  scope: resourceGroup(dcrResourceGroupName)
  params: {
    dcrName: dcrName
    principalId: service.outputs.principalId
    apimResourceId: apimResourceId
  }
}

module activate './apim-activate.bicep' = if (activateGateway) {
  name: 'apim-activate'
  scope: group
  params: {
    serviceName: service.outputs.serviceName
  }
  dependsOn: [
    operations
    publisher
  ]
}

output apimState object = {
  subscription_id: subscription().subscriptionId
  resource_group: group.name
  location: location
  ownership_marker: ownershipMarker
  apim_name: service.outputs.serviceName
  apim_resource_id: service.outputs.serviceResourceId
  principal_id: service.outputs.principalId
  api_resource_id: baseline.outputs.apiResourceId
  api_subscription_resource_id: activateGateway ? activate!.outputs.apiSubscriptionResourceId : ''
  activation_requested: activateGateway
  publisher_role_assignment_id: publisher.outputs.roleAssignmentId
  dcr_resource_id: publisher.outputs.dcrResourceId
  logs_endpoint: '${service.outputs.gatewayUrl}/otlp/v1/logs'
  traces_endpoint: '${service.outputs.gatewayUrl}/otlp/v1/traces'
  metrics_endpoint: '${service.outputs.gatewayUrl}/otlp/v1/metrics'
}

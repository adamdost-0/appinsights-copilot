targetScope = 'resourceGroup'

param location string
param tags object
param apimName string
@allowed([
  'Developer'
])
param skuName string = 'Developer'
param publisherEmail string
param publisherName string

resource service 'Microsoft.ApiManagement/service@2024-05-01' = {
  name: apimName
  location: location
  tags: tags
  sku: {
    name: skuName
    capacity: 1
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    publisherEmail: publisherEmail
    publisherName: publisherName
    customProperties: {
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Protocols.Tls10': 'False'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Protocols.Tls11': 'False'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Protocols.Ssl30': 'False'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Backend.Protocols.Tls10': 'False'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Backend.Protocols.Tls11': 'False'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Backend.Protocols.Ssl30': 'False'
    }
  }
}

resource deny 'Microsoft.ApiManagement/service/policies@2024-05-01' = {
  parent: service
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('./policies/apim-deny.xml')
  }
}

output serviceName string = service.name
output serviceResourceId string = service.id
output principalId string = service.identity.principalId
output gatewayUrl string = service.properties.gatewayUrl

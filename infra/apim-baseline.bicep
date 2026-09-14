targetScope = 'resourceGroup'

param serviceName string

resource service 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: serviceName
}

resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: service
  name: 'copilot-otel'
  properties: {
    displayName: 'Copilot OTLP authenticated gateway'
    apiType: 'http'
    path: 'otlp'
    protocols: [
      'https'
    ]
    subscriptionRequired: true
    subscriptionKeyParameterNames: {
      header: 'X-Copilot-Telemetry-Key'
      query: 'subscription-key'
    }
  }
}

resource deny 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: api
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('./policies/apim-deny.xml')
  }
}

output apiResourceId string = api.id

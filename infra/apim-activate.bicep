targetScope = 'resourceGroup'

param serviceName string

resource service 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: serviceName
}
resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' existing = {
  parent: service
  name: 'copilot-otel'
}

var clientSubscriptionId = 'copilot-otel-client'

resource policy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: api
  name: 'policy'
  properties: {
    format: 'xml'
    value: replace(loadTextContent('./policies/apim-api.xml'), '__SUBSCRIPTION_ID__', clientSubscriptionId)
  }
}

resource clientSubscription 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = {
  parent: service
  name: clientSubscriptionId
  properties: {
    displayName: 'Copilot OTLP evaluation'
    scope: api.id
    state: 'active'
    allowTracing: false
  }
  dependsOn: [
    policy
  ]
}

output apiSubscriptionResourceId string = clientSubscription.id

targetScope = 'resourceGroup'

param serviceName string
param logsEndpoint string
param tracesEndpoint string
param metricsEndpoint string

resource service 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: serviceName
}
resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' existing = {
  parent: service
  name: 'copilot-otel'
}

var signals = [
  'logs'
  'traces'
  'metrics'
]
var endpoints = [
  logsEndpoint
  tracesEndpoint
  metricsEndpoint
]
var operationPolicy = loadTextContent('./policies/apim-operation.xml')

resource operations 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = [for signal in signals: {
  parent: api
  name: 'post-${signal}'
  properties: {
    displayName: 'Export OTLP ${signal}'
    method: 'POST'
    urlTemplate: '/v1/${signal}'
  }
}]

resource policies 'Microsoft.ApiManagement/service/apis/operations/policies@2024-05-01' = [for (signal, index) in signals: {
  parent: operations[index]
  name: 'policy'
  properties: {
    format: 'xml'
    value: replace(operationPolicy, '__ENDPOINT_BASE64__', base64(endpoints[index]))
  }
}]

targetScope = 'resourceGroup'

param location string = resourceGroup().location
param tags object
param principalId string
param operatorPrincipalId string = principalId

@allowed([
  'User'
  'ServicePrincipal'
  'Group'
])
param principalType string = 'User'

@allowed([
  'User'
  'ServicePrincipal'
  'Group'
])
param operatorPrincipalType string = principalType

var suffix = uniqueString(resourceGroup().id)
var metricStreams = ['Custom-Metrics-Otel']
var logStreams = ['Microsoft-OTel-Logs']
var traceStreams = [
  'Microsoft-OTel-Traces-Spans'
  'Microsoft-OTel-Traces-Events'
  'Microsoft-OTel-Traces-Resources'
]

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'law-copilot-otel-v1-${suffix}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    workspaceCapping: {
      dailyQuotaGb: 1
    }
    features: {
      enableLogAccessUsingOnlyResourcePermissions: false
    }
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

resource metrics 'Microsoft.Monitor/accounts@2025-10-03' = {
  name: 'amw-copilot-otel-v1-${suffix}'
  location: location
  tags: tags
  properties: {
    publicNetworkAccess: 'Enabled'
  }
}

resource endpoint 'Microsoft.Insights/dataCollectionEndpoints@2024-03-11' = {
  name: 'dce-copilot-otel-v1-${suffix}'
  location: location
  tags: tags
  properties: {
    networkAcls: {
      publicNetworkAccess: 'Enabled'
    }
  }
}

resource rule 'Microsoft.Insights/dataCollectionRules@2024-03-11' = {
  name: 'dcr-copilot-otel-v1-${suffix}'
  location: location
  tags: tags
  properties: {
    dataCollectionEndpointId: endpoint.id
    directDataSources: {
      otelMetrics: [
        {
          name: 'otelMetricsDirect'
          streams: metricStreams
          enrichWithResourceAttributes: ['*']
        }
      ]
      otelLogs: [
        {
          name: 'otelLogsDirect'
          streams: logStreams
          enrichWithResourceAttributes: ['*']
        }
      ]
      otelTraces: [
        {
          name: 'otelTracesDirect'
          streams: traceStreams
          enrichWithResourceAttributes: ['*']
        }
      ]
    }
    destinations: {
      logAnalytics: [
        {
          name: 'nativeLogs'
          workspaceResourceId: logs.id
        }
      ]
      monitoringAccounts: [
        {
          name: 'nativeMetrics'
          accountResourceId: metrics.id
        }
      ]
    }
    dataFlows: [
      {
        streams: metricStreams
        destinations: ['nativeMetrics']
      }
      {
        streams: concat(logStreams, traceStreams)
        destinations: ['nativeLogs']
      }
    ]
  }
}

resource publisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(rule.id, principalId, 'copilot-otel-v1-publisher')
  scope: rule
  properties: {
    principalId: principalId
    principalType: principalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '3913510d-42f4-4e42-8a64-420c390055eb')
  }
}

resource metricReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(metrics.id, operatorPrincipalId, 'copilot-otel-v1-metrics-reader')
  scope: metrics
  properties: {
    principalId: operatorPrincipalId
    principalType: operatorPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b0d8363b-8ddd-447d-831f-62ca05bff136')
  }
}

resource logReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(logs.id, operatorPrincipalId, 'copilot-otel-v1-logs-reader')
  scope: logs
  properties: {
    principalId: operatorPrincipalId
    principalType: operatorPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '73c42c96-874c-492b-b04d-ab87d138a893')
  }
}

output nativeState object = {
  subscription_id: subscription().subscriptionId
  resource_group: resourceGroup().name
  location: location
  ownership_marker: tags['ownership-marker']
  workspace_resource_id: logs.id
  workspace_customer_id: logs.properties.customerId
  azure_monitor_workspace_resource_id: metrics.id
  metrics_query_endpoint: metrics.properties.metrics.prometheusQueryEndpoint
  dce_resource_id: endpoint.id
  dcr_resource_id: rule.id
  dcr_immutable_id: rule.properties.immutableId
  // Internal OTel stream names differ from the public OTLP trace/log URL routes.
  traces_endpoint: '${endpoint.properties.logsIngestion.endpoint}/datacollectionRules/${rule.properties.immutableId}/streams/Microsoft-OTLP-Traces/otlp/v1/traces'
  metrics_endpoint: '${endpoint.properties.metricsIngestion.endpoint}/datacollectionRules/${rule.properties.immutableId}/streams/Custom-Metrics-Otel/otlp/v1/metrics'
  logs_endpoint: '${endpoint.properties.logsIngestion.endpoint}/datacollectionRules/${rule.properties.immutableId}/streams/Microsoft-OTLP-Logs/otlp/v1/logs'
}

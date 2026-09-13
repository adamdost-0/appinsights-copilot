targetScope = 'resourceGroup'

param location string = resourceGroup().location
param applicationInsightsResourceId string
param ownershipMarker string
param workbookData string

resource workbook 'Microsoft.Insights/workbooks@2023-06-01' = {
  name: guid(resourceGroup().id, 'copilot-native-session-explorer')
  location: location
  kind: 'shared'
  tags: {
    solution: 'copilot-native-otel'
    'ownership-marker': ownershipMarker
    purpose: 'native-session-visualization'
  }
  properties: {
    displayName: 'Copilot CLI - Native Session Explorer'
    description: 'Native Copilot conversations, tokens, models, latency, tools, traces and events. Synthetic verification data; no collector.'
    category: 'workbook'
    sourceId: applicationInsightsResourceId
    version: 'Notebook/1.0'
    serializedData: workbookData
  }
}

output workbookResourceId string = workbook.id

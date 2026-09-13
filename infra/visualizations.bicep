targetScope = 'resourceGroup'

param location string = resourceGroup().location
param workspaceResourceId string
param ownershipMarker string
param workbookData string

resource workbook 'Microsoft.Insights/workbooks@2023-06-01' = {
  name: guid(resourceGroup().id, 'copilot-otel-v1-session-explorer')
  location: location
  kind: 'shared'
  tags: {
    solution: 'copilot-otel-v1'
    'ownership-marker': ownershipMarker
    purpose: 'session-visualization'
  }
  properties: {
    displayName: 'Copilot CLI - Session Explorer'
    description: 'Native Copilot span summaries in Log Analytics: conversations, tokens, models, latency, tools, traces and events. No prompt or tool content; not metrics.'
    category: 'workbook'
    sourceId: workspaceResourceId
    version: 'Notebook/1.0'
    serializedData: workbookData
  }
}

output workbookResourceId string = workbook.id

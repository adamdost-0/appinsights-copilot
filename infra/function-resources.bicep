targetScope = 'resourceGroup'

param location string
param tags object
@minLength(1)
@maxLength(100)
param allowedIPv4Cidrs array
@minLength(9)
param tracesEndpoint string
@minLength(9)
param logsEndpoint string
@minLength(9)
param metricsEndpoint string

var suffix = uniqueString(resourceGroup().id)
var restrictions = [for (cidr, index) in allowedIPv4Cidrs: {
  ipAddress: cidr
  action: 'Allow'
  priority: 100 + index
  name: 'approved-ipv4-${index}'
  description: 'Approved client and deployment egress only'
}]

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'stotelrelay${suffix}'
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource deploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'deployments'
  properties: {
    publicAccess: 'None'
  }
}

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: 'plan-copilot-otel-relay-${suffix}'
  location: location
  tags: tags
  kind: 'functionapp'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

resource app 'Microsoft.Web/sites@2024-04-01' = {
  name: 'func-copilot-otel-relay-${suffix}'
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    publicNetworkAccess: 'Enabled'
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${deploymentContainer.name}'
          authentication: {
            type: 'SystemAssignedIdentity'
          }
        }
      }
      runtime: {
        name: 'node'
        version: '22'
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 40
        instanceMemoryMB: 512
        triggers: {
          http: {
            perInstanceConcurrency: 4
          }
        }
      }
    }
    siteConfig: {
      minTlsVersion: '1.2'
      scmMinTlsVersion: '1.2'
      ftpsState: 'Disabled'
      ipSecurityRestrictions: restrictions
      ipSecurityRestrictionsDefaultAction: 'Deny'
      scmIpSecurityRestrictions: restrictions
      scmIpSecurityRestrictionsDefaultAction: 'Deny'
      scmIpSecurityRestrictionsUseMain: true
      appSettings: [
        {
          name: 'FUNCTIONS_REQUEST_BODY_SIZE_LIMIT'
          value: '4194304'
        }
        {
          name: 'AzureWebJobsStorage__accountName'
          value: storage.name
        }
        {
          name: 'AzureWebJobsStorage__credential'
          value: 'managedidentity'
        }
        {
          name: 'OTLP_TRACES_ENDPOINT'
          value: tracesEndpoint
        }
        {
          name: 'OTLP_LOGS_ENDPOINT'
          value: logsEndpoint
        }
        {
          name: 'OTLP_METRICS_ENDPOINT'
          value: metricsEndpoint
        }
      ]
    }
  }
}

// The HTTP-only host needs blob ownership, also covering its deployment container.
resource hostStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, app.id, 'host-blob-owner')
  scope: storage
  properties: {
    principalId: app.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b')
  }
}

resource scmBasicAuth 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = {
  parent: app
  name: 'scm'
  properties: {
    allow: false
  }
}

resource ftpBasicAuth 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = {
  parent: app
  name: 'ftp'
  properties: {
    allow: false
  }
}

output functionAppName string = app.name
output functionAppResourceId string = app.id
output principalId string = app.identity.principalId
output storageResourceId string = storage.id
output baseEndpoint string = 'https://${app.properties.defaultHostName}'

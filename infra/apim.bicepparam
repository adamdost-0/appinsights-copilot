using './apim.bicep'

// Nonsecret shape only. Generate private, provenance-checked parameters with apim-preflight.mjs.
param location = 'eastus'
param ownershipMarker = '00000000-0000-4000-8000-000000000000'
param skuName = 'Developer'
param activateGateway = false
param publisherEmail = 'operator@example.invalid'
param publisherName = 'Synthetic evaluation'
param dcrResourceGroupName = 'rg-copilot-otel-v1'
param dcrName = 'replace-from-verified-native-receipt'
param logsEndpoint = 'https://replace-from-verified-native-receipt.invalid/logs'
param tracesEndpoint = 'https://replace-from-verified-native-receipt.invalid/traces'
param metricsEndpoint = 'https://replace-from-verified-native-receipt.invalid/metrics'

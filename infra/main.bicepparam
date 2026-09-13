using './main.bicep'

param location = 'eastus'
// Nonsecret example only. The deployment CLI generates a fresh ownership receipt.
param ownershipMarker = '00000000-0000-4000-8000-000000000000'
// Replace with the publisher's Entra object ID, not an application/client ID.
param principalId = '00000000-0000-4000-8000-000000000001'
param principalType = 'User'
// The default fixture operator is also the publisher. Override for separate query access.
param operatorPrincipalId = principalId
param operatorPrincipalType = principalType

import functions from '@azure/functions';
import { ManagedIdentityCredential } from '@azure/identity';
import { createRelay } from '../relay.js';
import { sendHttps } from '../transport.js';

const { app } = functions;
app.setup({ enableHttpStream: true });

// Reuse the official credential and its token cache for the worker lifetime.
const credential = process.env.AZURE_CLIENT_ID
  ? new ManagedIdentityCredential({ clientId: process.env.AZURE_CLIENT_ID })
  : new ManagedIdentityCredential();

export const handler = createRelay({
  environment: process.env,
  credential,
  send: sendHttps,
});

app.http('otlp', {
  route: 'v1/{signal}',
  authLevel: 'anonymous',
  handler,
});

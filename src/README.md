# Native OTLP HTTPS relay

Deploy this directory as the root of a **Node.js 22, Azure Functions runtime v4**
application. `functions/otlp.js` registers an anonymous HTTP trigger at
`/v1/{traces,logs,metrics}` (no `/api` prefix). Require HTTPS in the Function App.
This endpoint intentionally has no per-client Azure authentication: anyone who
can reach it can submit telemetry and consume ingestion capacity. Apply the
deployment's access restrictions and cost controls; this code is not an
authentication or rate-limiting service.

## Server configuration

Set all three endpoint settings and the request-size limit before accepting requests:

| Setting | Required value |
| --- | --- |
| `OTLP_TRACES_ENDPOINT` | `https://<dce>.ingest.monitor.azure.com/datacollectionRules/dcr-<32 lowercase hex>/streams/Microsoft-OTLP-Traces/otlp/v1/traces` |
| `OTLP_LOGS_ENDPOINT` | Same native route with `Microsoft-OTLP-Logs` and `/logs` |
| `OTLP_METRICS_ENDPOINT` | Same native route with `Custom-Metrics-<stream>` and `/metrics` |
| `AZURE_CLIENT_ID` | Runtime supports an optional user-assigned identity ID; omit for this deployment, whose infrastructure and role grants support system-assigned identity only |
| `FUNCTIONS_REQUEST_BODY_SIZE_LIMIT` | `4194304` |

The repository deploys **Flex Consumption**, where `functionAppConfig.runtime`
sets `name: node` and `version: 22`. Do not set `FUNCTIONS_WORKER_RUNTIME` or
`FUNCTIONS_EXTENSION_VERSION` on Flex; those app settings are for other hosting
plans and are not part of this deployment contract.

Endpoints may have different Azure Monitor ingestion hostnames, but must all
use the same immutable DCR ID. Explicit port 443 is allowed; credentials,
queries, fragments, encoded paths, other ports and alternate hosts are not.
Configuration is captured once per worker and cannot be overridden by the
request. Grant the managed identity the appropriate ingestion role on that
DCR. A single reusable `ManagedIdentityCredential` requests
`https://monitor.azure.com/.default`; no client token is forwarded.

The HTTP extension does not expose a supported `host.json` request-size property.
Set `FUNCTIONS_REQUEST_BODY_SIZE_LIMIT=4194304` as above for host-level rejection;
the handler independently enforces the immutable **4 MiB** limit, even if that
setting is omitted or increased. Streaming requires Functions host 4.34.1 or
newer. `host.json` contains HTTP throttling settings; the deployed Flex
`functionAppConfig.scaleAndConcurrency` explicitly sets HTTP concurrency to
**4 per instance** and maximum instances to **40**. These are not tenant quotas
or cost ceilings. Host logging disables automatic raw HTTP/dependency
telemetry, keeping only explicitly redacted relay error codes/statuses. Do not
enable Azure SDK verbose logging, request-body logging, or platform access logs
containing client query strings/headers.

## Behavior and limits

Only POST and `application/x-protobuf` are accepted. Bodies remain opaque OTLP:
the relay does not parse, alter, or independently validate protobuf semantics.
Native Azure ingestion remains the semantic validator. Empty bodies are rejected.
`identity` or absent encoding forwards raw bytes. `gzip` is checked for a valid
gzip stream and bounded to 4 MiB both compressed and expanded, then forwarded
**byte-for-byte**, not recompressed. Invalid/multiple encodings are rejected.

The 30-second deadline includes reading the request, identity acquisition,
upstream response headers and body. A native `https.request` transport avoids
automatic redirects and decompression. It makes one ingestion attempt, with no
relay retry or durable queue. The identity SDK retains its own token caching and
identity-endpoint behavior.

Upstream status, body, `content-type`, `content-encoding`, and `retry-after` are
preserved; client headers, redirect locations, cookies, and other upstream
headers are not. Response bodies are also limited to 4 MiB (oversize is 502).
Token/network failures are 502, invalid server configuration is 503, and deadline
expiry is 504. There is no false-success fallback. Timeout/network failure can
still follow successful Azure acceptance; client retries may duplicate telemetry.

## Local validation and synthetic Logs

```sh
npm --prefix src ci
npm --prefix src test
npm --prefix src run check
node src/tools/generate-logs-fixture.js --run-id UUID --output /private/new-logs.pb
```

Use a fresh UUID for each live proof. The fixture uses the pinned official
OpenTelemetry OTLP protobuf serializer, with nonempty INFO log body
`SYNTHETIC_RELAY_<UUID>`, resource `service.name=github-copilot`, string attribute
`run.id=<UUID>`, boolean `synthetic=true`, and current timestamps. Tests use the
official JSON and protobuf serializers to check OTLP value types and encoding.
The tool writes only the requested new file, mode 0600; it refuses overwrites
and symlinks and prints no payload or output path. Supply an existing private
parent directory. Fixture tools and their dev dependency are not runtime code.

For a deployment ZIP, put `host.json`, `package.json`, `package-lock.json`,
`relay.js`, `transport.js`, `functions/`, and production `node_modules/` at the
ZIP root (install with `npm ci --omit=dev --ignore-scripts` in a staging copy).
Do not nest the ZIP under `src/`. Exclude fixtures, tests, tools, and local
settings. `npm pack --dry-run` checks the runtime source allowlist but is not
itself a deployment ZIP and intentionally does not contain dependencies.
Normalize only staged runtime entry permissions to files `0644` and directories
`0755` before archiving; a private `umask 077` otherwise produced a live host
503. Keep the staging parent `0700` and the ZIP `0600`, as detailed in the
[packaging runbook](../docs/relay-deployment.md#package-and-deploy-code-with-azure-cli).

Local tests do not establish Azure ingestion. Live proof requires an Azure CLI
deployment, an actual POST, and a query finding the unique marker in the target
workspace. See the [deployment runbook](../docs/relay-deployment.md) and
[measured log/trace evidence](../docs/evidence/function-relay.md); native metrics
ingestion through the relay is not yet verified.

For real isolated CLI verification, `tools/smoke-copilot.mjs` accepts optional
`--host-name` and `--user-id` labels. These become percent-encoded
`host.name`/`user.id` resource attributes; neither is automatically captured by
the helper. Their declared values are recorded only in the private run manifest.
See [user attribution verification](../docs/verification.md#user-attribution).
They are client assertions, not authenticated device or employee identity.

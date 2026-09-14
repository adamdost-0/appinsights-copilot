# Native OTLP HTTPS relay

This directory also contains **local-only APIM preflight and synthetic test
tools**. They are not Function runtime code and must not be added to the
deployment ZIP. APIM uses native gateway policies rather than a second Function.
See the [independent APIM runbook](../docs/apim-deployment.md) for Azure CLI
deployment and [Administrators.md](../Administrators.md) for ordinary client
configuration.

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
See [administrator attribution guidance](../Administrators.md#opt-in-user-attribution)
and [recorded relay evidence](../docs/evidence/function-relay.md).
They are client assertions, not authenticated device or employee identity.

## APIM-authenticated synthetic CLI smoke

`tools/smoke-copilot.mjs` defaults to `--transport function-relay`, preserving
the existing no-client-auth recipe. Select APIM explicitly and provide the exact
HTTPS base ending in `/otlp`:

```sh
node src/tools/smoke-copilot.mjs \
  --transport apim-gateway \
  --endpoint https://approved-gateway.azure-api.net/otlp \
  --run-id UUID --output .local/apim-cli-unique-run
```

Load `APIM_SUBSCRIPTION_KEY` privately into the parent environment before launch;
there is intentionally no key CLI argument. The helper validates the credential,
passes only its encoded `X-Copilot-Telemetry-Key` OTLP header to the isolated
child, and declares that header secret to Copilot. The source key variable and
parent Azure credentials/configuration are not passed through. Do not reuse
the Function endpoint for APIM or add an APIM key to Function mode.

APIM manifests record `transport: apim-gateway` and
`client_auth: apim-subscription-key`, never the credential. Diagnostics redact
exact and encoded secret values as well as credential header forms. Keys and
headers must never appear in tool arguments, environment dumps, Markdown or
ordinary logs. Redaction is defense in depth, not a host-administrator or
malicious-plugin boundary.

Both modes keep content capture off and use temporary HOME/CWD outside ancestor
Git contexts. Output must be a new private directory under this worktree's
`.local/`; symlink and overwrite guards fail closed. The helper records
`awaiting_backend_verification`, not ingestion success. Verify fresh LAW
spans/events and privacy independently; native AMW metrics need separate
queries. The [APIM E2E runbook](../docs/apim-deployment.md#backend-evidence-and-negative-controls)
also requires synthetic Logs and authentication/revocation denial controls.

## APIM HTTP probes and offline parameter preflight

Generate parameters only from independently reviewed receipt/readback input:

```sh
node src/tools/apim-preflight.mjs \
  --input .local/apim-input.json --output .local/apim-parameters.json
```

This is a bounded offline Node helper, not a deployment wrapper. It validates
the native resource/endpoint/stream bindings and writes a new private parameter
file. Actual Azure reads, review, validation, what-if and deployment remain
explicit [Azure CLI commands](../docs/apim-deployment.md).

For gateway HTTP acceptance use a privately loaded `APIM_SUBSCRIPTION_KEY`:

```sh
node src/tools/probe-apim.mjs \
  --endpoint https://approved-gateway.azure-api.net/otlp \
  --output .local/apim-probes-unique-run \
  --include-other-routes --include-boundary
```

The probe runner sends bounded sequential synthetic requests, never redirects
or retries automatically, and keeps response bodies/credentials out of evidence.
It checks valid before/after controls, missing/random credentials, route/method,
query, media-type, compression, empty-body, oversized fixed-length, and unsupported
chunked-framing cases. `--include-other-routes` extends credential denial across
traces/metrics; `--include-boundary` explicitly sends one exactly-4-MiB valid
protobuf batch of bounded log records. Omit the latter for routine small probes.
Read actual returned statuses; framing rejection is not streamed-size proof.

`APIM_NEGATIVE_KEY` optionally supplies a real other/broad-scope subscription
credential. `APIM_RETIRED_KEY` optionally supplies a revoked credential after
rotation. Random-key testing does not prove either property. Real keys never
enter query-string probes: those use a fabricated query value alongside the valid
header. Load and unset all secret variables privately; do not put them in argv.

Output contains fresh case UUIDs/timestamps, expected/observed HTTP outcomes,
synthetic fixture references, and explicit skipped gates. A successful HTTP suite
still records `azure_ingestion_proven: false` and pending backend verification.
Correlate positive/denied markers with LAW and inspect privacy separately; the
empty body has no transmitted marker. Actual CLI spans/events and native
metrics remain independent tests. Do not treat these generated Logs as actual
CLI telemetry. Rate/quota and higher-volume testing need separately bounded
authorization rather than an automatic flood test.

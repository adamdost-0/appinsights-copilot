# Security, privacy, and data handling

[Administrator guide](../Administrators.md) | [Evidence requirements](../AGENTS.md#telemetry-evidence)

## Synthetic-only scope

Tests must never use normal-user source code, conversation history, personal
Copilot configuration, private repositories, or existing work sessions.
The harness creates isolated temporary homes and working directories, passes
only the required environment, disables custom instructions and built-in MCPs,
and restricts tools to the selected synthetic scenario. It does not copy the
normal user's profile. These are scope controls, not an operating-system sandbox.

The relay CLI harness executes in system-temporary home/work directories
(normally `/tmp/copilot-relay-*`), while retaining redacted evidence under
private `.local`. Verify Node's `os.tmpdir()` and its ancestors are outside any
checkout; the helper does not enforce this for a custom temp-directory setting.
Its initial checkout-local temporary directory allowed
ancestor Git repository/branch metadata to appear in telemetry; the parent
corrected that harness. Final acceptance must use its corrected execution path.
No project content was reported read, but private directory permissions alone
do not isolate Git context. Do not reuse the initial UUID as proof of the
corrected execution isolation.

`metadata-only` is the default privacy posture.
`full-content` and `delegated` are **explicit opt-ins to content capture for
fabricated, isolated examples only**. Delegated testing also enables content
capture; its name must not be mistaken for a metadata-only mode.
Capture-on can include prompts, responses, system instructions, tool definitions,
arguments, and results. It is not approval to export production/user content.
Earlier explicitly approved synthetic capture-on records remain in LAW under
its retention policy. Keeping capture off in the final relay and identity
tests does not remove those earlier records or prove a content-free workspace.

[Normal metadata-only onboarding](../Administrators.md) is a separate, organizationally
approved operational recipe. It is not executed as a test and does not authorize
content capture. Approval for ordinary use never permits normal-user code or
history to be included in the synthetic evidence.

## Metadata-only policy

Set `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false`.
Native CLI telemetry can still contain tool **name/type metadata** in
`gen_ai.tool.definitions`. The v1 default permits only entries with the exact
`name` and `type` fields: `type` must be `function`, and `name` must be a bounded
tool identifier matching `[A-Za-z_][A-Za-z0-9_.-]{0,127}`. Descriptions, schemas,
arguments, results, or nested content are not allowed. Empty telemetry is not a
privacy pass.

| Field | Metadata-only requirement |
| --- | --- |
| `gen_ai.input.messages` | Absent |
| `gen_ai.output.messages` | Absent |
| `gen_ai.system_instructions` | Absent |
| `gen_ai.tool.call.arguments` | Absent |
| `gen_ai.tool.call.result` | Absent |
| `gen_ai.tool.definitions` | Absent or exact name/type-only metadata; no schema/content |

The historical direct-route synthetic verifier enforces this defined policy on returned
telemetry; its metadata-only run [passed](evidence/v1.md) with two allowed
tool-metadata fields and no forbidden content fields. It does not continuously
validate ordinary sessions. A stricter organizational policy
requiring all six keys to be absent is **different** and must not be reported
as passed just because the v1 name/type allowance passes.

The Python direct-route runner records `--privacy-policy metadata-only-v1` by default. To explicitly
evaluate the stronger policy, use `--privacy-policy strict-absence` on a new
metadata-only smoke run and verify its own UUID. This changes validation, not
what the CLI exports, and may fail on otherwise permitted tool metadata. Do not
change a completed run's manifest to switch policies.
The Node relay smoke helper has no `--privacy-policy` option and does not
perform backend privacy validation. Its manifest remains
`awaiting_backend_verification`; apply the policy independently to queried
attributes. The [measured relay inspection](evidence/function-relay.md)
permitted two exact name/type-only fields, not strict six-key absence.

Neither the default relay nor the optional direct route is a privacy filter.
The relay forwards opaque binary OTLP; it does not inspect or redact content.
Backend validation
detects a violation **after data has been sent**; it cannot retract that data or
prove what was absent from memory or pre-ingestion traffic. On any content
failure, stop tests, keep diagnostics private, investigate the exact installed
CLI, and use the approved data-remediation process. Do not weaken validation or
enable content capture to make a metadata-only check succeed.

Metadata itself can disclose identities, model/tool names, timing, infrastructure
details, or resource attributes. Treat telemetry as organizational data even
when message capture is disabled. Named-field checks are not a universal secret
scanner, DLP system, or permission to add arbitrary resource attributes.

## Identity and access

| Identity | Minimum telemetry role and scope |
| --- | --- |
| Default relay client | Approved public IPv4 egress; no Azure token or Function key |
| Relay system-assigned identity | Monitoring Metrics Publisher on the explicit DCR; Storage Blob Data Owner on relay-only host/deployment storage |
| Optional direct sender | Monitoring Metrics Publisher on the explicit DCR |
| Metrics auditor | Monitoring Data Reader on the Azure Monitor workspace |
| Trace/event auditor | Log Analytics Reader on the Log Analytics workspace |
| Deployer | Authorized resource deployment and scoped role-assignment permissions |
| Workbook viewer | Workbook read access plus authorized access to its LAW data |

Separate publishing and auditing identities in an enterprise design. A sender
does not need read access just to export. The example's combined operator roles
are for bounded evaluation, not a least-privilege enterprise identity blueprint.
Workbook filters are not an authorization boundary.

The default relay deliberately uses anonymous HTTP triggers behind platform
ingress restrictions. The nonempty approved IPv4 allowlist and default-deny
app/SCM rules are applied on first creation, with HTTPS-only and TLS 1.2+.
There is no public anonymous deployment window. Every caller sharing an
allowlisted NAT/network can publish telemetry; the relay does not authenticate
individual clients, validate claimed user identity, or provide a rate-limiting
or durable-delivery guarantee. Network admission can be too broad for enterprise
requirements; do not treat it as equivalent to per-user Entra authorization.

The Function uses `ManagedIdentityCredential` with its system identity; callers
must not supply Azure credentials for relay forwarding. Storage shared-key
access and public blob access are disabled; storage is publicly reachable but
identity-authorized, not private-endpoint protected. Only approved native HTTPS
endpoints may be configured because the relay sends its managed-identity token
upstream. Do not copy tokens or payload bodies into logs/diagnostics.

The DCE requires an Entra bearer token for audience `https://monitor.azure.com`
even though its HTTPS endpoint is publicly reachable. GitHub tokens authorize
Copilot inference, not Azure ingestion. Application Insights connection strings
are not Azure OTLP credentials and are not used by this solution.

For the **optional direct authenticated route**, acquire short-lived tokens
using an approved Azure CLI session and pass them
in memory through the OTLP header. Do not echo them, enable shell tracing,
save them in environment files, put them in prompts, or publish process
environments. Privileged host users and processes with inspection access can
read environments; file permissions and redaction do not defeat host admins.

**Optional direct-route static environment tokens do not renew.** A wrapper can acquire a token from
an existing authorized Azure CLI login without prompting on every run; it does
not remove authentication, refresh an already-running CLI, or establish an
enterprise session-lifetime mechanism. Long-running use needs separately
designed identity, refresh, revocation, and expiry handling.

The relay removes this client token lifecycle by using managed identity for
upstream authentication; that is not removal of DCE authentication or proof of
lossless delivery. Recreated system identities require new scoped RBAC and
explicit stale-role cleanup.

## Private artifacts and publication

Keep `.local/relay-azure.json`, relay parameters, deployment ZIPs/hashes/readbacks,
CLI inventory/approval artifacts, `.local/native-deployment.json`, `.local/native-azure.json`,
`.local/native-workspace-children.json`, `.local/native-teardown.json`, run
manifests, CLI diagnostics, raw backend responses, workbook definitions/readbacks,
and browser evidence ignored and private. Resource identifiers and ownership markers
are sensitive operational metadata even when they are not credentials.
Private artifact directories use mode `0700` and files `0600`; do not loosen
their permissions or use symlinks to bypass receipt checks. The only packaging
exception is the explicit staged runtime entries (files `0644`, directories
`0755`) inside a private staging parent; see
[archive permissions](relay-deployment.md#package-and-deploy-code-with-azure-cli).

The workspace-child baseline records exact saved-search identities and content
hashes for ownership-checked cleanup. It is not a license to delete arbitrary
default-looking resources. Do not reset, edit, or remove it to adopt an existing
workspace or suppress a drift refusal.

Only manually reviewed sanitized summaries belong in
[relay evidence](evidence/function-relay.md); preserve
[v1 evidence](evidence/v1.md) as historical direct-auth evidence.
Do not publish raw telemetry or credential-bearing
diagnostics to explain a failure. The repository remains private; documentation
does not authorize a visibility change or grant repository access.

## Retention, cost, and cleanup

Log Analytics is configured for **30-day retention and a 1 GB/day workspace
cap**. Verify actual `OTelSpans`, `OTelEvents`, `OTelResources` and `OTelLogs` analytics/total
retention after deployment rather than inferring table behavior solely from
the workspace setting. Retention settings are not a secure-erasure guarantee.

The daily cap can interrupt observations and is **not a hard cost ceiling**.
Flex Consumption execution and relay storage capacity/transactions are
additional billable resources. A scaling limit does not cap total spending.
Azure Monitor workspace metric ingestion, retention, and billing are separate.
Unique run/conversation labels increase metric cardinality; bound this to the
evaluation and design a label policy before wider use. Copilot inference and
other Azure operations can incur additional charges. Use approved budgets and
alerts, not the LAW cap alone.

Nothing is automatically deleted after verification. Follow separately
approved lifecycle procedures and retain only the evidence your
policy requires. Resource deletion does not establish immediate removal from
every retained service copy or backup.

The relay group is separate from the v1 monitoring group and uses a fresh
ownership marker. Its publishing assignment lives on the **existing DCR** and
must be reviewed/removed explicitly before native teardown; group deletion
alone leaves that external assignment. Preserve the
[resource lifecycle constraints](../AGENTS.md#resource-lifecycle-constraints)
without changing historical ownership guards or saved-search baselines.

## Audit limitations

Telemetry is client-generated and can be disabled, forged, dropped, buffered,
sampled, or incomplete. Entra authenticates an authorized sender, not every
reported action. Required signal checks do not prove complete capture of all
commands, files, tools, optional features, or model decisions. This solution is
operational observability, **not tamper-proof or complete cybersecurity auditing**.
Native OTLP is preview, has no SLA, and is not recommended for production.

References: [Log Analytics retention](https://learn.microsoft.com/en-us/azure/azure-monitor/logs/data-retention-configure),
[daily cap limitations](https://learn.microsoft.com/en-us/azure/azure-monitor/logs/daily-cap),
[Azure Monitor workspace overview](https://learn.microsoft.com/en-us/azure/azure-monitor/metrics/azure-monitor-workspace-overview).

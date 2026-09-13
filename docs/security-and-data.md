# Security, privacy, and data handling

[Administrator guide](../README.md) | [Verification](verification.md)

## Synthetic-only scope

Tests must never use normal-user source code, conversation history, personal
Copilot configuration, private repositories, or existing work sessions.
The harness creates isolated temporary homes and working directories, passes
only the required environment, disables custom instructions and built-in MCPs,
and restricts tools to the selected synthetic scenario. It does not copy the
normal user's profile. These are scope controls, not an operating-system sandbox.

`metadata-only` is the default privacy posture.
`full-content` and `delegated` are **explicit opt-ins to content capture for
fabricated, isolated examples only**. Delegated testing also enables content
capture; its name must not be mistaken for a metadata-only mode.
Capture-on can include prompts, responses, system instructions, tool definitions,
arguments, and results. It is not approval to export production/user content.

[Normal metadata-only onboarding](usage.md) is a separate, organizationally
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

The v1 synthetic runtime/verifier enforces this defined policy on returned
telemetry; the fresh metadata-only run [passed](evidence/v1.md) with two allowed
tool-metadata fields and no forbidden content fields. It does not continuously
validate ordinary sessions. A stricter organizational policy
requiring all six keys to be absent is **different** and must not be reported
as passed just because the v1 name/type allowance passes.

The runner records `--privacy-policy metadata-only-v1` by default. To explicitly
evaluate the stronger policy, use `--privacy-policy strict-absence` on a new
metadata-only smoke run and verify its own UUID. This changes validation, not
what the CLI exports, and may fail on otherwise permitted tool metadata. Do not
change a completed run's manifest to switch policies.

There is no intermediary privacy filter on the direct route. Backend validation
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
| Sender | Monitoring Metrics Publisher on the explicit DCR |
| Metrics auditor | Monitoring Data Reader on the Azure Monitor workspace |
| Trace/event auditor | Log Analytics Reader on the Log Analytics workspace |
| Deployer | Authorized resource deployment and scoped role-assignment permissions |
| Workbook viewer | Workbook read access plus authorized access to its LAW data |

Separate publishing and auditing identities in an enterprise design. A sender
does not need read access just to export. The example's combined operator roles
are for bounded evaluation, not a least-privilege enterprise identity blueprint.
Workbook filters are not an authorization boundary.

The DCE requires an Entra bearer token for audience `https://monitor.azure.com`
even though its HTTPS endpoint is publicly reachable. GitHub tokens authorize
Copilot inference, not Azure ingestion. Application Insights connection strings
are not Azure OTLP credentials and are not used by this solution.

Acquire short-lived tokens using an approved Azure CLI session and pass them
in memory through the OTLP header. Do not echo them, enable shell tracing,
save them in environment files, put them in prompts, or publish process
environments. Privileged host users and processes with inspection access can
read environments; file permissions and redaction do not defeat host admins.

**Static environment tokens do not renew.** A wrapper can acquire a token from
an existing authorized Azure CLI login without prompting on every run; it does
not remove authentication, refresh an already-running CLI, or establish an
enterprise session-lifetime mechanism. Long-running use needs separately
designed identity, refresh, revocation, and expiry handling.

## Private artifacts and publication

Keep `.local/native-deployment.json`, `.local/native-azure.json`,
`.local/native-workspace-children.json`, `.local/native-teardown.json`, run
manifests, CLI diagnostics, raw backend responses, workbook definitions/readbacks,
and browser evidence ignored and private. Resource identifiers and ownership markers
are sensitive operational metadata even when they are not credentials.
Private directories use mode `0700` and files `0600`; do not loosen permissions
or use symlinks to bypass receipt checks.

The workspace-child baseline records exact saved-search identities and content
hashes for ownership-checked cleanup. It is not a license to delete arbitrary
default-looking resources. Do not reset, edit, or remove it to adopt an existing
workspace or suppress a drift refusal.

Only manually reviewed sanitized summaries belong in
[v1 evidence](evidence/v1.md). Do not publish raw telemetry or credential-bearing
diagnostics to explain a failure. The repository remains private; a LinkedIn
draft neither changes visibility nor gives readers repository access.

## Retention, cost, and cleanup

Log Analytics is configured for **30-day retention and a 1 GB/day workspace
cap**. Verify actual `OTelSpans`, `OTelEvents`, and `OTelResources` analytics/total
retention after deployment rather than inferring table behavior solely from
the workspace setting. Retention settings are not a secure-erasure guarantee.

The daily cap can interrupt observations and is **not a hard cost ceiling**.
Azure Monitor workspace metric ingestion, retention, and billing are separate.
Unique run/conversation labels increase metric cardinality; bound this to the
evaluation and design a label policy before wider use. Copilot inference and
other Azure operations can incur additional charges. Use approved budgets and
alerts, not the LAW cap alone.

Nothing is automatically deleted after verification. Follow
[cleanup](deployment.md#cleanup-and-rebuild) and retain only the evidence your
policy requires. Resource deletion does not establish immediate removal from
every retained service copy or backup.

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

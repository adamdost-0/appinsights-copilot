# Security and data handling

The synthetic-only consent below applies to both experiments. Collector
transforms and connection-string controls describe the **historical** path;
the [native route](native-otlp.md) instead uses Entra headers and no collector.
Its [measured content-off result](evidence/native-example.md#content-and-privacy)
distinguishes retained tool name/type metadata from a prompt or argument leak.

## Approved capture scope

Full-content capture is approved **only for fabricated inputs in isolated synthetic sessions**. The harness creates a temporary `HOME`, `COPILOT_HOME`, and workdir, uses a synthetic fixture/marker, suppresses normal custom instructions and built-in MCPs, and restricts tools. It must not load normal projects, personal configuration, private repositories, plugins, skills, or custom MCPs. Do not broaden this consent to another user's data or production sessions.

`metadata-only` requests disabled message capture; `full-content` and `delegated` enable it. The observed CLI does not fully honor the content-off contract: see the divergence below. Capture-on can include prompts, responses, tool arguments/results, system instructions, or tool definitions/metadata depending on the installed CLI. Even metadata can disclose model names, timings, identifiers, paths, or environment context. A synthetic prompt is not proof that the surrounding capture contains no sensitive information.

These controls reduce accidental exposure; they are not an OS security sandbox or a guarantee that generated tool requests are safe. Optional sandbox/hooks/MCP/skills/compaction coverage must remain explicitly unverified unless safely exercised and observed. The full-content workflow is not permission to inspect private host files.

## Observed content-off divergence and collector boundary

The coordinator found nonempty `gen_ai.tool.definitions` on chat/invocation spans in the raw CLI 1.0.84-5 metadata-only file diagnostic, despite explicit `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false`. The reported field length was 841; the checked capture had no other known gated fields or actual authentication token. This is an upstream privacy-control discrepancy, not a metadata-only privacy pass. Raw diagnostic evidence remains private.

The implemented defense-in-depth mitigation is a collector OTTL transform that removes six known gated attributes from spans and span events for metadata-only/default traffic, preserving them only for explicitly labeled `full-content` or `delegated` synthetic scenarios: `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`, `gen_ai.tool.definitions`, `gen_ai.tool.call.arguments`, and `gen_ai.tool.call.result`. It runs **before both Azure and local evidence trace exporters**. The 2026-09-13 metadata-only proof confirmed absence of all six fields in post-transform source and matching Azure records, without establishing native CLI compliance.

Only the resource attribute `copilot.audit.scenario` selects the explicit content-on exemptions. Missing/unknown values remain filtered; span/event-local values and a `copilot.scenario` alias cannot opt out. Processing errors propagate instead of silently bypassing the transform. This exact-key filter does not sanitize resource attributes, metrics, arbitrary keys, or all possible secrets. Configuration changes require an explicit collector restart.

With that transform active, the verifier's source is **post-transform collector evidence**, not raw CLI output. A metadata-only pass would establish absence of the checked fields at the downstream transport boundary and in matching Azure records, not native CLI compliance or absence from upstream process memory/raw diagnostics. Keep strict verifier absence checks; do not permit tool definitions just to make the scenario pass. Scenario labels are not authenticated consent, and six known fields are not a general-purpose secret scrubber.

## Trust boundaries and authentication

| Boundary | Security implication |
| --- | --- |
| CLI to local collector | OTLP HTTP/protobuf on host loopback 4318; no remote listener should be exposed. Loopback does not authenticate other local processes. |
| Collector health | Loopback 13133 reports local readiness, not ingestion or identity. |
| Collector to Application Insights | Uses a sensitive connection string with local authentication enabled (`DisableLocalAuth=false`). Telemetry is not identity-bound or tamperproof; an authorized ingestion key is not proof of who produced an event. |
| Azure query | Public endpoints still require authenticated, authorized data-plane RBAC. Workspace resource-permission access is disabled; provision suitable workspace access separately. |
| Docker and host | Docker administrators and privileged host users can inspect container environment/mounted evidence. Container hardening and file modes do not defend against those administrators. |

The collector is pinned to an immutable Contrib v0.160.0 image digest. Its Azure Monitor exporter is beta. Pinning improves repeatability, not perpetual security: dependency updates require deliberate config validation and repeat verification. Native Azure OTLP preview uses a different architecture; do not assume its identity, network, retention, or metric-store properties apply here.

## Secrets and private artifacts

All `.local/` runtime state is ignored and private. Runtime files are created with `0600` and private directories with `0700`. The pinned file exporter's rotation can create `0644` files inside the protected `0700` directory; status fails on rotation and stop restores evidence file modes to `0600`. Do not treat the directory protection as optional or copy rotated files to shared locations without checking permissions. Do not chmod shared directories world-writable, commit runtime files, or copy them into public build artifacts.

The Application Insights connection string is stored only in `.local/collector.env`, not in Bicep outputs, the safe Azure receipt, CLI configuration, prompts, or documentation. The collector needs it at runtime; avoid printing its container environment.

GitHub authentication requires a separately authorized `gh auth login` or a supported token environment variable; the harness internally reads `gh auth token` when no supported variable is set. It passes the token only in the child environment and never saves it. Never write a GitHub token into `.env`, `.local/collector.env`, or any collector configuration. Do not echo tokens, enable shell tracing around credentials, put tokens in command arguments, or paste usernames/token output into evidence. Redaction is defense in depth, not proof that an arbitrary raw payload is safe.

`.local/azure.json` contains resource identifiers and an ownership marker, not credentials; these can still reveal organizational metadata and need not be public. `.local/collector.json` points to private JSON-lines evidence. Run manifests, CLI stdout/stderr, source evidence, and raw query responses remain private. Only fabricated fixtures and manually reviewed, sanitized evidence belong in version control.

## Retention, cost, and deletion

The workspace is configured for `PerGB2018`, 30-day workspace retention, and a 1 GB daily workspace quota. **This does not establish that every Application Insights table deletes data after 30 days.** Table-specific analytics/total retention and Application Insights defaults can differ. Read actual workspace and table settings, plus applicable Application Insights/workspace caps, before accepting a retention policy.

Deployment readback verifies only the actual workspace cap/retention. The deployer does not set or query individual table retention or the separate Application Insights cap. Separate operator ARM reads on 2026-09-13 confirmed `AppDependencies`, `AppMetrics`, and `AppTraces` each have **90-day analytics and total retention**, despite the workspace's 30-day setting. Separate installed CLI billing inspection confirmed Application Insights `Basic`, a **100 GB** cap, and a **90%** warning threshold. The workspace's **1 GB** cap is lower and effective for ingestion; neither cap guarantees a cost ceiling. These observed settings are recorded privately in `.local/azure-retention.json`, not established by deployment success alone.

Caps can overshoot and are not hard budgets; they can also interrupt observation. Azure ingestion/storage and Copilot inference can incur charges. Review cost alerts and actual usage separately. Do not claim a spending guarantee from the configured daily quota.

The local file exporter rotates at 10 MiB with two backups and a one-day age setting. Rotation/age cleanup is not a secure erasure guarantee or a complete retention policy; asynchronous cleanup can temporarily exceed nominal size. Lifecycle commands fail readiness at 8 MiB or detected rotation so a single-file verifier does not silently claim completeness.

No resources are automatically cleaned up after success. Collector stop retains evidence. The Azure teardown command is audit-only and refuses deletion of every existing group, including with `--confirm`; its parent ownership checks do not discover hidden provider/proxy resources or other users' private artifacts. Ancillary child APIs are not queried. Only verified already-absent scope yields a successful no-op. An administrator must inspect the exact scope and approve any manual cleanup separately; a passing deployment/reuse audit is not deletion authorization.

The teardown audit preserves cloud resources and all local files, including the connection string. Privately archive only what is required, then remove selected owned artifacts using an approved disposal process. A deleted resource or file is not proof that backups or all retained service data have been erased.

## What this evidence cannot prove

The collector file copy enables comparison of real source attributes, span relationships, events, and metric snapshots with Azure conversion. It is neither immutable nor complete proof of every command, file access, tool action, or model decision. Export failures, termination, unsupported features, missing events, truncation, and histogram/quantile conversion limit fidelity. In particular, large system instructions/tool definitions may be truncated; full-content mode does not promise lossless storage of the entire model context. Required synthetic-marker proof is separate and must not be waived because other large fields have documented conversion loss. Do not advertise this repository as a lossless or tamperproof cybersecurity audit.

See [Azure retention](https://learn.microsoft.com/en-us/azure/azure-monitor/logs/data-retention-configure), [daily cap limitations](https://learn.microsoft.com/en-us/azure/azure-monitor/logs/daily-cap), and the [pinned exporter documentation](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/v0.160.0/exporter/azuremonitorexporter).

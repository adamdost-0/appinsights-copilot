# Repository context for Copilot CLI agents

## Architecture and administrator configuration

The default path is Copilot CLI -> IP-restricted Node 22 Azure Function ->
managed-identity-authenticated native DCE/DCR -> Log Analytics for
traces/events/resources/logs and Azure Monitor workspace for metrics.
There is no Application Insights component or workstation collector.
The Function moves Azure authentication off clients; it does not make the
DCE anonymous. GitHub authentication for inference remains separate.

The alternative APIM gateway uses a dedicated API-scoped client key and APIM
managed identity on the same native DCR. Follow `docs/apim-deployment.md`;
do not route APIM through or modify the existing Function. APIM owns
`rg-copilot-otel-apim`, a fresh marker and private `.local/apim*` artifacts.
Only its additive DCR publisher assignment crosses groups. Shared native
destinations mean independent gateway lifecycle, not isolated backend quota.
Do not describe an APIM key as an Entra audience/scope token or employee identity.

[Administrators.md](Administrators.md) is the canonical local-host environment
guide, including optional host/user labels and user-context/GPO caveats.
Do not duplicate exporter configuration in separate onboarding documents.

## Deployment workflow

- Follow the Azure CLI commands in `docs/deployment.md` and the Function relay
  runbook linked from the README. Read the complete relevant runbook before
  changing Azure resources.
- Do not execute the Python deployment wrappers (`scripts.native_deploy`,
  `scripts.deploy`, `scripts.visualizations`, or `scripts.teardown`) to create,
  update, or delete Azure resources. Historical Python tests and synthetic
  verification utilities are not the deployment workflow.
- Use Bicep with `az deployment ... what-if` and `az deployment ... create`.
  Keep actual parameters, deployment outputs, access tokens, and telemetry
  evidence in ignored `.local/`, not in source or Markdown.
- Never create an unrestricted anonymous telemetry ingress endpoint. Apply
  the approved source-IP restriction or private networking before publishing
  an anonymous Function trigger, and verify the restriction afterward.
- Do not delete or adopt unrelated Azure resources. Check subscription,
  resource-group ownership tags, and complete inventory before mutations.
- An HTTP success or successful deployment is not ingestion proof. Correlate
  a fresh synthetic run identifier with rows in the destination Log Analytics
  workspace. Do not capture normal project/session contents as test fixtures.
- Keep current relay findings in `docs/evidence/function-relay.md` separate from
  historical direct-authenticated `docs/evidence/v1.md`. Logs/spans/events do not
  prove native AMW metrics; complete relay CLI signal acceptance remains open
  until fresh metric-series queries succeed.
- Keep synthetic HOME/CWD outside every repository's ancestor Git context.
  Host/user labels are explicit client assertions, not authenticated identity.
  Normalize deployment archive permissions only on staged runtime entries;
  keep private receipts, ZIPs and telemetry private.
- APIM built-in all-access keys must not bypass API-level subscription-ID
  admission. Require fixed endpoint provenance, credential stripping, actual
  body bounds, fail-closed provisioning, and no query-string credentials.
  Do not relax policies to pass a smoke test. APIM v1 rejects gzip rather than
  claiming the Function's decompression bound.
- APIM proof must use its own fresh HTTP probes and backend queries. Preserve
  Function/v1 evidence independently. Readbacks are not gateway-runtime proof;
  authenticated ingestion and key revocation need measured data-plane results.

## Telemetry evidence

Use the isolated tools described in [src/README.md](src/README.md), with a fresh
UUID and private output directory. `queries/verify_relay.kql` correlates
synthetic logs by run marker and actual CLI spans/events by resource and
trace/span identity. Bind `__RUN_ID__` to the validated test UUID, query the
receipt-owned LAW, and require matching rows; an HTTP 204 is not sufficient.
Use wrong-run and, where applicable, wrong-user negative controls.

Keep synthetic log-producer proof separate from actual CLI proof: CLI events
travel with traces, not a demonstrated standalone Logs exporter. Inspect
returned content attributes with capture disabled; the opaque relay is not
DLP. Never enable full-content capture without separate approval.
Test ingress denial only with an approved network experiment; same-host
allow-rule removal is not an external-host or live SCM-denial test.

The current [relay evidence](docs/evidence/function-relay.md) proves synthetic
logs, actual CLI spans/events and optional Linux host/user labels. Native
relay metrics, Windows/AD/GPO deployment, normal-user monitoring and
authenticated workbook rendering remain unverified. Do not convert these
open gates into success claims from historical direct-DCE evidence.

## Workbook context and agent tasks

The **Copilot CLI - Session Explorer** workbook is an operational view of
native telemetry in the owned LAW, not an Application Insights experience,
terminal replay, invoice, or complete cybersecurity audit. The relay changes
transport/authentication, not the span-based query contract. Retaining the
existing workbook does not require redeployment.

| Panel | Intended insight |
| --- | --- |
| Overview | Observed spans, conversations, chat/tool calls, failures and missing metadata |
| Sessions | Conversation/trace grouping, observed interval, models, calls and reported usage |
| Chat tokens | Input/output usage from chat spans by model, not aggregate agent spans |
| Latency | Chat-span mean/p95 duration and observed call counts over time |
| Tools | Observed tool calls, failures, durations and missing tool metadata |
| Spans | Trace/span/parent relationships, model/tool identity, timing and status |
| Events | Native span events correlated with selected traces and spans |

Preserve these contracts when modifying or deploying the workbook:

- `infra/visualizations.bicep` owns the deterministic workbook resource.
  Compose `queries/session_base.kql` with each `queries/session_*.kql` panel.
  `scripts/session_workbook.py` provides the existing definition and optional
  read-only live-query validation; it is not a Python deployment prerequisite.
- Associate both workbook source and panel datasource with the receipt-owned
  LAW. Native rows have empty `_ResourceId`; do not filter by DCR resource ID.
  Deduplicate resource IDs and left-join enrichment without applying a
  query-wide timespan that clips late-arriving resource records.
- Bound span/event time selection to at most 24 hours. Use base64-bound
  exact-match `RunId`, `ConversationId` and `TraceId` selectors; blank means
  all in the time window and populated selectors combine with AND.
  Invalid input must not broaden scope. Never interpolate raw input into KQL.
- Include ordinary `service.name=github-copilot` sessions without requiring
  synthetic run/scenario labels. Group by actual conversation, fall back to
  actual trace, and leave missing identity unknown rather than fabricating it.
- Count tokens only on chat spans; expose missing usage/durations rather than
  assuming zero. Session wall time is the observed interval, not the sum of
  nested durations. Do not project prompt/response/tool bodies by default.
- Span-based token/latency charts do not prove AMW histogram persistence.
  Query native metrics separately; never sum cumulative snapshots as
  independent consumption. Reported usage is not billing evidence.

For a workbook change, generate a reviewed Notebook definition and Bicep
parameters privately with Node from the checked-in KQL. Follow the private
shell and ownership gates in [deployment](docs/deployment.md).
Run all seven live queries and selected span/event drilldowns before ARM
validation. Require expected columns, meaningful nonempty results, no
partial errors, and selector isolation. The Tools panel needs approved
synthetic tool data within the query window. Current metadata-only relay
smoke has no tool calls; do not weaken that gate or enable content capture
without separate approval.

Use Azure CLI, not Python mutation wrappers:

```bash
az deployment group validate -g rg-copilot-otel-v1 -n copilot-otel-workbook \
  --template-file infra/visualizations.bicep --parameters @.local/workbook-cli-parameters.json
az deployment group what-if -g rg-copilot-otel-v1 -n copilot-otel-workbook \
  --template-file infra/visualizations.bicep --parameters @.local/workbook-cli-parameters.json
```

After reviewing only the intended owned workbook creation/update, apply with
`az deployment group create` using the same arguments. Read back the exact
serialized definition, LAW association, ownership tags and stable resource ID.
No ingestion-resource deletion or endpoint change belongs in a workbook update.
Keep definitions, query responses and readbacks under private `.local/`.

Remaining workbook work requires fresh relay data in the seven panels and an
authorized portal session: open the LAW Workbooks gallery, render each panel,
exercise time/conversation/trace selectors, and compare counts with backend
evidence. Record query, saved-definition, gallery and rendered-UI outcomes
independently. Historical direct-auth queries/readback passed; a resource GET
does not establish browser rendering. Never copy browser profiles or tokens
into automation to bypass sign-in.

## Resource lifecycle constraints

No documentation change authorizes Azure deletion. Before an approved lifecycle
operation, verify the explicit subscription, original ownership markers,
resource IDs, LAW customer ID, full child inventory and external consumers.
Preserve existing receipts and exact saved-search canonical-JSON hash
baselines. Missing state, drift, foreign children or incomplete inventory
means stop; tags and familiar resource names are not proof of ownership.

The relay has its own group and marker. Its publisher role lives on the
native DCR outside that group and needs explicit removal/review when retiring
the relay. Do not remove other publishers, silently reset baselines or force
deletion of AMW-managed resources. Retirement needs separately reviewed Azure
CLI commands and explicit approval; this file is not a teardown runbook.
APIM has the same external-role lifecycle constraint: remove/review only its
receipt-owned DCR assignment, never another publisher. No APIM update or
documentation change authorizes deleting either existing resource group.

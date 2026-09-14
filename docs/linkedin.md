# LinkedIn draft: restricted managed-identity relay

**Draft only: not posted. The repository remains private.**

---

Azure administrators: this example adds a small Azure Function in front of
Azure Monitor's native OTLP ingestion.

Copilot CLI sends binary OTLP/HTTP over HTTPS **without a client Azure token or
Function key**, from explicitly allowed IPv4 networks. The Node 22 Function
uses managed identity to authenticate to the existing DCE/DCR. App and
deployment ingress default to deny; this is not an anonymous endpoint open to
the internet. Optional direct export with an Entra bearer token remains available.

Log Analytics holds native traces, events and logs; an Azure Monitor workspace
holds histogram metrics. A seven-panel LAW workbook explores sessions, reported
chat tokens, latency, tools and traces. Its charts use span data, not native
metric proof. No Application Insights component, collector, VM or container is
required.

Deployment is now documented **Azure CLI + Bicep commands in Markdown**:
native resources, the separate relay group, production ZIP deployment, workbook
updates and reviewed cleanup. Python smoke/query utilities remain optional
tests, not deployment prerequisites. The relay's DCR role assignment needs
explicit review on teardown because its scope is outside the relay group.

The final corrected-isolation relay run persisted one synthetic log, two actual
CLI spans and three correlated events, with message content capture off.
The initial harness allowed ancestor Git repository/branch metadata into
telemetry; the accepted replacement executed outside the checkout.
Platform denial was
also tested by removing the sole allowed source rule while keeping default
Deny, observing HTTP 403, and restoring the identical rule; no allow-all window
was introduced. Relay native metrics remain unverified, so complete CLI
signal acceptance is still open. Handler rejection tests alone
would not establish network denial.

The historical direct-authenticated v1 synthetic evaluation independently
passed native span/event/metric checks and workbook live queries. **Those
historical metrics are not relay metric proof.** Authenticated portal rendering
and normal-user monitoring are not claimed.

Important limits: native Azure OTLP is **preview, without an SLA and not
recommended for production**. Allowlisted NAT peers can all submit data;
network admission is not per-user authentication. Metadata-only is the default,
but the relay is not a privacy filter or complete/tamper-proof security audit.
Functions/storage, metrics and inference cost money; a LAW cap is not a total
solution spending ceiling.

The repository is private; this draft does not promise public access.

#AzureMonitor #AzureFunctions #ManagedIdentity #GitHubCopilot #OpenTelemetry

---

Before publication, review [historical v1 evidence](evidence/v1.md) and the
parent-maintained [relay evidence](evidence/function-relay.md). Update pending
checks only from measured outcomes. Preserve preview, privacy,
ingress and repository-access qualifications. Never attach receipts, raw
payloads, credentials, private IDs or unreviewed screenshots. Publication and
visibility changes require separate authorization.

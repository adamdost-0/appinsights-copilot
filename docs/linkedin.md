# LinkedIn v1 draft

**Draft only: not posted. The repository remains private.**

---

Azure Administrators: GitHub Copilot CLI can be monitored without deploying a
collector, VM, container, or Application Insights component.

This v1 administrator example configures the CLI to send binary OTLP/HTTP
protobuf directly over HTTPS, with a required Microsoft Entra bearer token, to
manually provisioned Azure Monitor DCE/DCR resources:

- Log Analytics stores native spans, span events, and resource metadata.
- An Azure Monitor workspace stores native histogram metrics.
- A seven-panel Log Analytics workbook explores sessions, chat token usage,
  latency, tools, spans, and events; AMW metric verification is separate.

The guide covers an Azure CLI rebuild, scoped publishing/query roles,
metadata-only defaults, explicitly selected synthetic content tests, bounded
verification, private evidence, retention, and cleanup. A separate onboarding
recipe covers organizationally approved metadata-only normal sessions; release
proof remains synthetic-only.

All three fresh synthetic scenarios passed native backend verification: 15
spans, 13 events, and 26 required histogram series. The saved workbook and all
seven live panel queries also passed. A separate isolated synthetic session
confirmed that conversation-based navigation works without experimental
run/scenario labels. Authenticated portal rendering is not verified; no
screenshot or real-user monitoring result is claimed.

Important boundaries: Azure native OTLP ingestion remains preview, without an
SLA and not recommended for production. "v1" labels the example, not service GA.
Client telemetry is useful for observability, not a tamper-proof or complete
security audit. A LAW daily cap is not a total solution cost ceiling.

The repository is currently private; this post does not promise public access.

#AzureMonitor #LogAnalytics #GitHubCopilot #OpenTelemetry #AzureAdministrators

---

Before publication, review [fresh v1 evidence](evidence/v1.md) and accurately
update the verification sentence from measured outcomes. Preserve preview,
privacy, and repository-access qualifications. Do not attach private receipts,
run identifiers, raw payloads, credentials, or unreviewed screenshots.
Publishing or changing repository visibility requires separate authorization.

# Function relay: live ingestion evidence

The restricted Azure Function was deployed with Azure CLI and accepted
**binary OTLP/HTTP without an Azure bearer header or Function key from the
client**. Its system-assigned managed identity authenticated to the existing
DCE/DCR. Matching data was queried from the actual Log Analytics workspace.
No Python deployment wrapper was executed.

## Accepted run

Final synthetic run: `18e8773a-3306-4c45-aabc-f78f5ec570fc`, September 13, 2026 UTC.

| Check | Observed result |
| --- | --- |
| Standalone synthetic OTLP Logs POST to Function `/v1/logs` | HTTP 204; one matching `OTelLogs` row |
| Actual isolated Copilot CLI, content capture off | Two `OTelSpans` and three `OTelEvents` |
| Actual conversation | `6bdb849f-7c49-4b1e-904d-f752f503f5e6` |
| Client authentication | No Azure authorization header, Function key, or key query parameter |
| Backend correlation | Fresh UUID and `github-copilot` service matched; unrelated UUID returned zero rows |
| Metadata check | Two name/type-only tool-definition fields; no forbidden content fields found in inspected span/event attributes |
| Unsupported requests | GET 405; JSON content type 415; unknown signal 404 |
| Compressed OTLP Logs | Separate gzip fixture returned 204 and one matching `OTelLogs` row |
| Network restriction | Removing the sole approved IP allow rule produced 403; restoring it returned handler 405 |
| Current ingress | App and SCM default Deny, approved test-host IPv4 `/32` only |
| Repeat Azure CLI apply | Same Function identity, endpoints, and DCR assignment; route remained responsive |
| Local checks at this stage | 60 Node tests and 233 existing Python regression tests passed; later user-label follow-up increased the Node suite to 64 (below) |

The Logs fixture and actual CLI output are distinct signals. The fixture uses
`Attributes["run.id"]` and body `SYNTHETIC_RELAY_<UUID>`. Actual CLI telemetry
uses resource attribute `copilot.run.id`; span events are joined by trace/span
identity. [The verification query](../../queries/verify_relay.kql) covers both.
The CLI's temporary HOME and working directory were outside the repository;
no normal project/session was used as an inference fixture.

The live Function uses Linux Flex Consumption, Node 22, Functions v4,
HTTPS-only, and TLS 1.2 minimum. Its identity has Monitoring Metrics Publisher
on the existing DCR and Storage Blob Data Owner on its dedicated host storage.
The Function has no workspace-query permission or client-supplied upstream URL.

## Corrections established during the live exercise

The initial ZIP inherited the private shell's `umask 077`: files were 0600 and
directories 0700. Kudu reported a ZIP permission warning, trigger
synchronization failed, the host returned 503, and Azure CLI exited nonzero.
Repackaging the same runtime with files 0644 and directories 0755 made the
same Azure CLI deployment command succeed. No identity grant or ingress
restriction was relaxed. Private artifacts remain under ignored `.local/`.
The [deployment runbook](../relay-deployment.md) includes this packaging step.

A CLI query with leading `//` comments returned an empty result despite
known ingested rows; removing those comments returned the six matching rows.
The relay query is comment-free for `az monitor log-analytics query`.
An attempted `/* ... */` replacement failed with Kusto `SYN0002`; it is not
a supported workaround.

An earlier synthetic CLI run used an empty directory underneath the
repository, which retained ancestor Git metadata. The smoke helper was
corrected to use a system temporary directory, a regression was added, and
the final run above was repeated and checked for inherited Git attributes.
Earlier attempts are retained privately, not substituted for final acceptance.

## Hostname follow-up

On September 14, 2026, run `d99ec234-b3d6-469c-bafe-3b6bd8a90f68` explicitly
added the local runtime's hostname through `OTEL_RESOURCE_ATTRIBUTES`.
The actual hostname is retained privately, not published in this repository.
Log Analytics returned **two actual CLI spans and three correlated span
events**, all resolving to that exact `host.name` resource attribute and
fresh run UUID. Content capture remained off; the client supplied no Azure
credentials. No Azure deployment or Function runtime change was required.

The first attempt, `133213aa-ab80-4abb-af2e-cd3200e9d48f`, completed inference
but produced no matching rows within the bounded verification window.
It remains unverified. A relay end-to-end preflight returned 204 before the
fresh successful run; this does not conclusively diagnose the first failure.
Private evidence is `.local/relay-host-proof.json` and the associated
manifest/query files. Hostname is client-asserted attribution, not proof of
device or user identity. Native metrics hostname labels were not verified.

## User attribution follow-up

On September 14, 2026, run `96ddd448-ac53-4e3d-ac23-7f3988bd8934` explicitly
added `user.id` from the current local OS username alongside `host.name`.
Log Analytics returned **two native CLI spans and three correlated events**
with both exact values and the fresh run UUID. The username and hostname
remain in private evidence, not this document. A wrong-user filter returned
zero rows. The actual conversation was
`661acbe7-1115-4a13-b9e8-ca261840e9c5`.

The test used the existing Function/DCE without redeployment, kept content
capture off, and supplied no client Azure credentials. It followed a successful
end-to-end relay preflight. The smoke helper's optional `--user-id` validates
and percent-encodes the supplied value; the full Node suite passed **64 tests**.
See [the client recipe](../usage.md#opt-in-user-attribution) and
[repeatable verification](../verification.md#user-attribution).

This proves transport of the declared OS-account label, not an authenticated
GitHub/Entra identity or attribution of a human behind a shared/service account.
Native metric-series user labels were not verified. Private proof:
`.local/relay-user-proof.json`, native span/query results, and the run manifest.

## Acceptance boundaries

- This proves Function-to-DCE delivery and persisted native logs, spans, and
  span events. An HTTP success alone was not accepted as proof.
- The metrics forwarding route is implemented and unit-tested, but the
  additional native token-histogram query for the initial relay CLI run
  returned no series. **Native metrics ingestion through this relay is not
  claimed as verified.** Earlier direct-DCE v1 metric proof is not relay proof.
- The network test used the same test host with its allow rule temporarily
  removed. It was not a request from a separately provisioned external host
  or a live SCM-denial test; SCM restrictions were read back.
- Complete relay CLI signal acceptance remains open while metrics are
  unverified; these log/span/event results do not authorize ordinary-user rollout.
- No normal-user capture, authenticated Azure portal rendering, Windows/AD/GPO rollout, durable
  buffering, availability SLA, or complete cybersecurity-audit coverage was
  established. IP restrictions authorize a network source, not an employee.
- Azure Monitor native OTLP remains preview. This relay does not change that
  status, make telemetry tamper-proof, or filter malicious sensitive payloads.
- Existing v1 workspaces and workbook were retained. No Application Insights,
  local Collector, AMA, APIM, or developer-side Azure login was introduced.

Actual endpoints, subscription identifiers, source IP, deployment receipts,
request traces, ZIPs, and raw query results remain in ignored `.local/`.
The repository remains private; no public release or LinkedIn post is implied.

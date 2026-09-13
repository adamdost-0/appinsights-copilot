# Troubleshooting direct OTLP

[Administrator guide](../README.md) | [Verification](verification.md)

Diagnose **local prerequisites -> identity -> exact exporter configuration ->
completed CLI run -> Azure routing -> backend queries -> workbook**.
Preserve nonzero exits, bounded timeouts, and private evidence. Do not rerun
inference or recreate resources blindly.

| Symptom | Check and action |
| --- | --- |
| Preflight blocked | Resolve the named Python/tool/authentication check. Run `copilot help monitoring` for the installed version. Do not disguise a blocked result as a warning or install a different telemetry architecture. |
| Deployment authorization/policy failure | Confirm the explicit subscription, public `AzureCloud`, registered providers, region policy, and permissions to deploy and assign roles. Do not weaken policy or silently switch subscriptions. |
| Ownership refusal | Retain `.local/native-deployment.json` and `.local/native-azure.json`. Check exact group, location, marker, and inventory. Do not forge receipts, adopt same-named resources, or remove unrelated resources to pass. |
| Missing or changed workspace-child baseline | Preserve `.local/native-workspace-children.json` with the other lifecycle receipts. Existing deployments without it cannot be automatically adopted. Saved-search IDs and full-entry hashes must match exactly; new, modified, or missing searches fail. Require authorized private inventory review, not an automatic baseline reset or name-prefix exception. |
| Partial deployment | Preserve the ownership seed and private `native-deployment-result.json`. If ARM succeeded but both operational state and baseline are absent after failed readback, use [reviewed first-apply recovery](deployment.md#recover-an-interrupted-first-apply). Never copy candidate outputs into operational state; missing receipts are not proof Azure is empty. |
| HTTP 401 | Verify a nonexpired Entra token for `https://monitor.azure.com` and `OTEL_EXPORTER_OTLP_HEADERS` with `Authorization=Bearer%20<token>`. GitHub sign-in is unrelated. A parent-shell update cannot refresh a running CLI. |
| HTTP 403 on ingestion | The identity that obtained the token needs Monitoring Metrics Publisher on the exact DCR. Allow normal RBAC propagation and inspect policy; public endpoint reachability does not imply anonymous ingestion. |
| HTTP 403 on queries | Check Monitoring Data Reader at AMW and Log Analytics Reader at LAW for the querying identity. A publishing role is not a read role. |
| Wrong protocol or path | Use HTTPS binary OTLP/HTTP protobuf and the receipt's full per-signal URLs. No gRPC, JSON OTLP, duplicate `/v1/traces`, or guessed stream names. The traces URL uses the logs-ingestion DCE domain but ends in `/otlp/v1/traces`, not `/logs`. |
| No telemetry despite CLI success | Check OTEL enabled, exporter `otlp-http`, exact endpoint variables and header, CLI diagnostics, DCR routing, retention/cap, and the completed run manifest. Success of inference alone is not export success. |
| HTTP 503 after fresh deployment | ARM success can precede ingestion data-plane readiness. Allow propagation, then run a new bounded synthetic CLI session with a new UUID and require complete backend proof. Do not mark a metrics-only run passed or use an authenticated empty-payload HTTP 400 as a health check. |
| Missing events | Span events are exported with traces and stored in `OTelEvents`. No standalone logs exporter is demonstrated. Never send trace or metric payloads to a logs URL. |
| Metadata-only failure | Tool definitions may contain only exact name/type metadata. Prompt/response/system content, arguments/results, schemas, or extra definition fields fail the policy. Stop; do not relabel the scenario, suppress the failure, or test with real data. |
| Missing native tables or delayed data | Check the correct LAW, fixed run time window, DCR destinations, and table/schema availability. The verifier may poll retryable missing data within its deadline. Empty results do not establish privacy or intake. |
| Empty `_ResourceId` on native rows | Expected for this no-Application-Insights path: rows are workspace-scoped, not DCR-associated. Use the exact LAW with LAW reader permissions and service/run correlation; a DCR resource-ID filter would discard valid rows. |
| Histogram query failure | Use native histogram `histogram_count`/`histogram_sum` against dotted base metric names, correct labels and AMW endpoint. Do not query invented `_count`/`_sum` series or sum cumulative snapshots. Environment overrides are not proof of CLI delta/exponential support. |
| Verification timeout or partial result | Read the private result reasons. Authentication, malformed responses, partial results, absent required series, and wrong identities must not become empty success. Investigate before extending the timeout. |
| Tool/delegation not observed | A prompt asking for a tool or child agent is not proof it happened. Require observed operation spans and valid parent relationships. Report unsupported/failed coverage; never inject synthetic telemetry as CLI proof. |
| Workbook empty | Set a time range containing the run; check LAW association, viewer permissions, selected IDs, and all seven live query results. Blank selectors mean all; multiple selectors combine with AND. |
| Workbook portal redirects to sign-in | Azure CLI authentication is not browser authentication. Use an authorized browser normally. Do not copy a personal profile or tokens into automation, and do not mark rendering verified from API readback. |

The verifier and workbook are independent evidence surfaces: a span-based token
chart does not prove AMW histogram ingestion. Positive metrics do not establish
lossless trace export or authenticated UI rendering. Record the precise failure
stage in [v1 evidence](evidence/v1.md), keeping raw data private.

For safe cleanup and a new deployment, use the
[ownership-checked lifecycle](deployment.md#cleanup-and-rebuild); never use a
generic broad delete to bypass an ownership refusal.

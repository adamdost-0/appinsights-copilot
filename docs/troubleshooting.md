# Troubleshooting relay and optional direct OTLP

[Administrator guide](../Administrators.md) | [Evidence requirements](../AGENTS.md#telemetry-evidence)

Diagnose **local prerequisites -> platform ingress -> identity -> exact exporter configuration ->
completed CLI run -> Azure routing -> backend queries -> workbook**.
Preserve nonzero exits, bounded timeouts, and private evidence. Do not rerun
inference or recreate resources blindly.

| Symptom | Check and action |
| --- | --- |
| Relay HTTP 403 before the handler | Check actual public IPv4 egress against approved app rules and Deny unmatched. NAT/VPN changes affect admission. Do not add Function keys or bearer tokens or open `/0`; approve an exact CIDR change if needed. |
| Azure CLI code deployment denied | The deployment host must be allowed by SCM rules and have deployment rights. Use current Azure CLI `config-zip` (Flex OneDeploy) with `--build-remote false`; do not switch to legacy `/api/zipdeploy`, Python deploy scripts, `func publish`, basic auth, or allow-all. |
| OneDeploy returns 202, then CLI exits with host-key status-check failure | Upload acceptance is not readiness or confirmed failure of the upload. Preserve the deployment identifier and nonzero CLI result; inspect that deployment's actual status, site/config and route behavior. Do not blindly redeploy or weaken auth/ingress. Successful code deployment requires a real no-client-auth request and matching persisted backend data. |
| Kudu `Zip permission validation failed`, host 503 | A private `umask 077` can put files 600/directories 700 inside the ZIP. Normalize only the allowlisted staging runtime entries to files 644/directories 755, rebuild a fresh ZIP, then use the same Azure CLI deployment. Keep `.local`/staging parent 700 and ZIP/evidence 600. Do not chmod the repository or private artifact tree, or widen IAM/network access. |
| `functionapp show` fields appear null | In the observed CLI 2.89.0 output, state/HTTPS/runtime are under `.properties`, while `functionapp config show` is flattened. Inspect the actual response shape before forming JMESPath/jq assertions; wrong-path null is not valid configuration evidence. |
| Archive tool missing | Both `zip` and `unzip` are prerequisites for the default package/inspection workflow. Use approved system or private local tool installation and the operator's PATH; do not substitute Python deployment. Local Core Tools packaging is an alternative to `zip`, not permission to skip actual archive inspection. |
| CLI query returns `[]` despite known ingested rows | In the observed `az monitor log-analytics query` path, a leading `//` comment in multiline input caused an empty result; the comment-free template was verified successfully. Use comment-free CLI KQL. Do not substitute `/* ... */`, which failed with Kusto `SYN0002`; supported `//` comments can instead remain intact in an `az rest` JSON query body. Do not infer missing telemetry or silently strip arbitrary query text. |
| Flex ARM rejects configuration | Verify region/runtime support and current site API. Runtime is `functionAppConfig.runtime` Node22, deployment storage is blob/SystemAssignedIdentity. Do not set legacy `linuxFxVersion`, `FUNCTIONS_WORKER_RUNTIME`, `FUNCTIONS_EXTENSION_VERSION` or `WEBSITE_RUN_FROM_PACKAGE`. |
| Function host/storage startup failure | Check system identity and Storage Blob Data Owner on the relay account, identity-based `AzureWebJobsStorage__*`, container URI and RBAC propagation. Do not introduce account keys; additional trigger types need separate role review. |
| Relay 5xx or upstream 401/403 | Check MI acquisition, approved upstream HTTPS URLs, Monitor audience and the **relay principal's** publisher role on the exact DCR. Caller Azure credentials do not fix the relay's identity. Keep error diagnostics bounded and payload/token-free. |
| Relay 404 / wrong route | Verify `host.json` route prefix and registered v4 HTTP routes `/v1/traces`, `/v1/logs`, `/v1/metrics`; inspect package root/manifest entrypoint and prod dependencies. |
| Relay HTTP success but no log row | Query actual LAW/native logs schema, fresh marker/service/time window, DCR logs route and OTLP partial success. Logs evidence is not CLI span events or metrics proof. |
| No off-network test host | Use only an approved fail-closed same-host test: remove its allow rule while retaining Deny, observe 403, then restore the exact rule and read back. Preserve the [evidence boundaries](../AGENTS.md#telemetry-evidence). If neither method is authorized, report live denial unverified. Spoofed headers and SCM configuration readback are not live SCM denial proof. |
| Optional Python test preflight blocked | Python is needed only for existing smoke/query utilities, not deployment. Resolve the named test/tool/authentication check. Run `copilot help monitoring` for the installed version. Do not disguise a blocked result as a warning. |
| Deployment authorization/policy failure | Confirm the explicit subscription, public `AzureCloud`, registered providers, region policy, and permissions to deploy and assign roles. Do not weaken policy or silently switch subscriptions. |
| Ownership refusal | Retain `.local/native-deployment.json` and `.local/native-azure.json`. Check exact group, location, marker, and inventory. Do not forge receipts, adopt same-named resources, or remove unrelated resources to pass. |
| Missing or changed workspace-child baseline | Preserve `.local/native-workspace-children.json` with the other lifecycle receipts. Existing deployments without it cannot be automatically adopted. Saved-search IDs and full-entry hashes must match exactly; new, modified, or missing searches fail. Require authorized private inventory review, not an automatic baseline reset or name-prefix exception. |
| Partial deployment | Preserve original parameters/marker and the CLI result `.local/native-cli-result.json` (plus any historical receipts). If ARM succeeded but operational state or inventory is absent after failed readback, use [reviewed first-apply recovery](deployment.md#recover-an-interrupted-first-apply). Install a candidate only after independent ownership/resource/inventory validation; never overwrite an existing receipt. Missing receipts are not proof Azure is empty. |
| Optional direct HTTP 401 | Verify a nonexpired Entra token for `https://monitor.azure.com` and `OTEL_EXPORTER_OTLP_HEADERS` with `Authorization=Bearer%20<token>`. GitHub sign-in is unrelated. A parent-shell update cannot refresh a running CLI. Relay clients do not need this header. |
| DCE HTTP 403 on ingestion | The identity that obtained the token needs Monitoring Metrics Publisher on the exact DCR: relay MI for relay requests, client identity for optional direct export. Allow normal RBAC propagation; public DCE reachability never implies anonymous ingestion. |
| HTTP 403 on queries | Check Monitoring Data Reader at AMW and Log Analytics Reader at LAW for the querying identity. A publishing role is not a read role. |
| Wrong protocol or path | Use HTTPS binary OTLP/HTTP protobuf and the receipt's full per-signal URLs. No gRPC, JSON OTLP, duplicate `/v1/traces`, or guessed stream names. The traces URL uses the logs-ingestion DCE domain but ends in `/otlp/v1/traces`, not `/logs`. |
| No telemetry despite CLI success | Check OTEL enabled, exporter `otlp-http`, exact relay or native URLs, ingress, CLI diagnostics, DCR routing, retention/cap, and completed run. The bearer header is needed only for direct DCE export. Success of inference alone is not export success. |
| HTTP 503 after fresh deployment | ARM success can precede ingestion data-plane readiness. Allow propagation, then run a new bounded synthetic CLI session with a new UUID and require complete backend proof. Do not mark a metrics-only run passed or use an authenticated empty-payload HTTP 400 as a health check. |
| Missing events | Span events are exported with traces and stored in `OTelEvents`. The relay's synthetic `/v1/logs` test does not establish a standalone CLI logs exporter. Never send trace or metric payloads to a logs URL. |
| Metadata-only failure | Tool definitions may contain only exact name/type metadata. Prompt/response/system content, arguments/results, schemas, or extra definition fields fail the policy. Stop; do not relabel the scenario, suppress the failure, or test with real data. |
| Missing native tables or delayed data | Check the correct LAW, fixed run time window, DCR destinations, and table/schema availability. The verifier may poll retryable missing data within its deadline. Empty results do not establish privacy or intake. |
| Empty `_ResourceId` on native rows | Expected for this no-Application-Insights path: rows are workspace-scoped, not DCR-associated. Use the exact LAW with LAW reader permissions and service/run correlation; a DCR resource-ID filter would discard valid rows. |
| Histogram query failure | Relay native metrics remain unverified: the measured query returned no series. Use native histogram `histogram_count`/`histogram_sum` against dotted base metric names, correct labels and AMW endpoint. Do not substitute historical direct metrics, invent `_count`/`_sum` series, or sum cumulative snapshots. Environment overrides are not proof of CLI delta/exponential support. |
| Verification timeout or partial result | Read the private result reasons. Authentication, malformed responses, partial results, absent required series, and wrong identities must not become empty success. Investigate before extending the timeout. |
| Tool/delegation not observed | A prompt asking for a tool or child agent is not proof it happened. Require observed operation spans and valid parent relationships. Report unsupported/failed coverage; never inject synthetic telemetry as CLI proof. |
| Workbook empty | Set a time range containing the run; check LAW association, viewer permissions, selected IDs, and all seven live query results. Blank selectors mean all; multiple selectors combine with AND. |
| Workbook portal redirects to sign-in | Azure CLI authentication is not browser authentication. Use an authorized browser normally. Do not copy a personal profile or tokens into automation, and do not mark rendering verified from API readback. |

The verifier and workbook are independent evidence surfaces: a span-based token
chart does not prove AMW histogram ingestion. Positive metrics do not establish
lossless trace export or authenticated UI rendering. Record the precise failure
stage in a new sanitized relay evidence record only after actual measurement,
keeping raw data private. [v1 evidence](evidence/v1.md) remains historical direct-auth proof.

For safe retirement and a new deployment, follow the
[resource lifecycle constraints](../AGENTS.md#resource-lifecycle-constraints)
with separately approved Azure CLI commands; explicitly remove/review the relay's
external DCR role before native teardown. Never use a
generic broad delete to bypass an ownership refusal.

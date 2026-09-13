# Local verification evidence

**2026-09-13: live Azure source/backend verification PASSED for metadata-only, full-content, and delegated synthetic scenarios. Deployment/repeat apply and earlier local source checks also passed.**

The `eastus` sample is available for inspection: group `rg-copilot-otel-audit`, workspace `law-copilot-otel-qiwrhpw7rm2q4`, and Application Insights `ai-copilot-otel-qiwrhpw7rm2q4`. Real Log Analytics queries matched **77 rows: 15 dependencies, 13 traces, and 49 metrics** across the three Azure-mode scenarios. The collector is stopped with confirmed clean shutdown.

This sanitized record summarizes confirmed coordinator observations, without fabricated backend rows or manifest relabeling. Raw capture, credentials, subscription names/IDs, full resource IDs, and personal identifiers are not published.

## Environment and validation

| Component | Observed version/state |
| --- | --- |
| Copilot CLI | `1.0.84-5` |
| Azure CLI / Bicep | `2.89.0` / `0.42.1` |
| Docker client and server | `29.7.1` |
| Python | `3.12.3`; documented prerequisite Python 3.12+ |
| Collector | Contrib `0.160.0`, exact immutable digest in [`collector/image.txt`](../../collector/image.txt) |
| GitHub authentication | Existing authorized login usable; token read internally, passed only in child environment, never stored |
| Azure authentication | Authorized login completed; preflight now passes. Initial unauthenticated/20-second managed-identity timeout blocker is resolved |

Commands below identify the repeatable checks, not cloud verification:

| Check | Confirmed result |
| --- | --- |
| `python3 -m scripts.preflight` | Passed after authorized Azure login; earlier nonzero authentication blocker resolved |
| `az bicep build --file infra/main.bicep --stdout` | Exit 0 |
| `az bicep build --file infra/resources.bicep --stdout` | Exit 0 |
| `az bicep build-params --file infra/main.bicepparam --stdout` | Exit 0; example parameters are not an ownership receipt |
| `python3 -m scripts.collector validate` | Pinned Azure configuration passes with synthetic connection string; no ingestion proof |
| `python3 -m scripts.collector validate --local-only` | Local-only configuration passes |
| `COLLECTOR_DOCKER_TEST=1 python3 -m unittest discover -s tests -p test_collector.py -v` | **43 passed, no skips**; actual protobuf, privacy, rotation, collision, and clean-shutdown probes included |
| `COLLECTOR_DOCKER_TEST=1 python3 -m unittest discover -s tests -q` | Final current-code run: **209 passed, no skips**. Offline/mocked boundaries plus actual Docker probes, separate from live Azure proof |
| Deployment compatibility regression tests | 30 passed after replacing unsupported Azure CLI location-list subscription flag with an explicitly scoped ARM request |
| `python3 -m compileall -q scripts tests` | Passed |
| Production source-parser integration | All three real collector runs return `source-validated` with `azure_ingestion_proven: false` using `parse_source` and `check_source_evidence` |

## Live Azure control-plane evidence

| Stage | Confirmed result |
| --- | --- |
| Initial live what-if | 3 `Create` changes |
| First apply and readback | Resource group, Log Analytics workspace, and linked Application Insights deployed in `eastus`; actual workspace retention 30 days and daily quota 1 GB; agreed public ingestion/query and local-auth settings verified |
| Second apply | Passed with stable resource IDs; preview contained 1 `Modify` and 2 `NoChange` entries, reflecting provider normalization rather than duplicate resources |

The selected subscription was explicitly authorized; its name and ID remain private. Azure CLI 2.89.0 does not accept `--subscription` for `az account list-locations`. Deployment now queries `/subscriptions/<subscription-id>/locations?api-version=2022-12-01` through an explicit ARM GET instead.

Control-plane success alone does not establish telemetry delivery, per-table retention, or the separate Application Insights daily cap. The separate telemetry proof below uses three fresh Azure-mode scenarios; existing local-only manifests were not relabeled or used as Azure proof.

### Observed retention and caps

Separate ARM reads of `workspaces/tables` at API version `2023-09-01` confirmed the following actual settings; these are not inferred from the template:

| Scope | `retentionInDays` | `totalRetentionInDays` |
| --- | --- | --- |
| Workspace | 30 | Not asserted |
| `AppDependencies` | 90 | 90 |
| `AppMetrics` | 90 | 90 |
| `AppTraces` | 90 | 90 |

The workspace daily cap is **1 GB**. Separate inspection using the installed `az monitor app-insights component billing show --app NAME --resource-group RG --subscription ID` succeeded without installing dependencies: `currentBillingFeatures` is `Basic`, `dataVolumeCap.cap` is **100 GB**, and its warning threshold is **90%**. The workspace's 1 GB cap is the lower effective ingestion cap; neither cap is a hard cost ceiling.

These reads are retained privately in `.local/azure-retention.json`; they are additional sample inspection, not `scripts.deploy` readback, which covers only workspace retention/cap. The observed **90-day table retention despite a 30-day workspace setting** demonstrates why workspace retention alone cannot establish a 30-day data deletion policy. An earlier guessed billing REST route returned 404; the successful installed CLI billing query resolved that diagnostic without changing resources.

## Live Azure telemetry proof

Each final `query_azure` verification used the real Log Analytics API and an unchanged Azure-mode manifest, returning `status: passed`, `azure_ingestion_proven: true`, and `proof_scope: post-transform-collector-to-azure`.

After the final diagnostic fix, production `verify()` was rerun against all three Azure runs without injection; all passed again with the unchanged counts below. Private `.local/azure-validation.json` records `azure_verified: true` and sanitized per-run results; the original collector stop recorded `clean_shutdown: true`.

| Scenario | Azure rows | Matched spans | Matched events | Matched required metric series | Source metric points | Optional content truncations |
| --- | --- | --- | --- | --- | --- | --- |
| Metadata-only | 15 | 2 | 3 | 5 | 10 | 0 |
| Full-content | 22 | 4 | 4 | 7 | 14 | 3 |
| Delegated | 40 | 9 | 6 | 14 | 25 | 5 |

The **77 Azure rows** comprise **15 `AppDependencies`, 13 `AppTraces`, and 49 `AppMetrics` rows**. The 26 matched required metric series are a subset, not a count of all metric rows; source point counts are not token totals. All three final verifications completed in one query attempt once buffered data was available. Initial table-readiness/transient outcomes were not counted as proof.

Metadata-only passed strict six-key absence after the collector transform and in Azure. Content-on input/output messages and same-tool arguments/results preserved the mandatory synthetic markers; delegation passed strict child parenting. For a source root with an empty parent only, Azure's observed `ParentId=OperationId` 32-hex normalization is accepted; child parents remain strict.

Observed optional system-instruction truncation was **8192 UTF-8 bytes**, not characters: full-content retained 8180 characters/8192 bytes, and delegated retained 8190 characters/8192 bytes as exact source prefixes. The verifier counts this loss only for oversized optional system instructions/tool definitions, never as an excuse for missing mandatory marker fields. Native CLI privacy remains unverified/known defective, and transport is not lossless.

## Real local collector capture

The three real CLI scenarios ran in `collector_mode: local-only` using the pinned image. The collector stopped with `clean_shutdown: true`; source evidence is the immutable **post-transform collector file**. Private `.local/local-validation.json` maps run identifiers, and `.local/runs/<run-id>/manifest.json` points to evidence.

| Scenario | Spans | Metric families | Observed local evidence |
| --- | --- | --- | --- |
| Metadata-only | 2 | 8 | Zero occurrences of the six known gated content attributes after transform |
| Full-content | 4 | 11 | `execute_tool view`; six content keys observed |
| Delegated | 9 | 11 | `execute_tool task`, `execute_tool view`, nested `invoke_agent explore` |

All three captures identify resource `service.name=github-copilot`, resource `service.version=1.0.84-5`, instrumentation `scope.name=github.copilot`, and scope version `1.0.84-5`. The observed event families are `github.copilot.mcp.server.lifecycle`, `github.copilot.session.usage_info`, and `github.copilot.user.message`. These are metric-family/span observations, not metric-series counts, token totals, or Azure row counts.

The actual authentication token was checked and found absent from **all three collector captures**. This is not a blanket guarantee that every possible secret is absent. Sessions used temporary `HOME`, `COPILOT_HOME`, XDG config/cache, and fixture-only workdirs, copied no user configuration, and bounded tool access; normal configuration and private repositories were not exposed.

Production source checks passed for all three captures: identity/version, required metric series, parenting including delegation, marker-bearing input/output and same-tool arguments/result for content-on, and six-key absence for metadata-only. Identical duplicate `gen_ai.response.model` attributes from the actual CLI are accepted only when their typed values are identical; conflicts fail. No fabricated Azure rows were injected and manifests remain `collector_mode: local-only`.

Results are persisted privately at `.local/runs/<run-id>/local-checks.json`: `status: source-validated`, `proof_scope: post-transform-collector-source-only`, and `azure_ingestion_proven: false`. The local workflow is `collector start --local-only`, chosen `run_smoke --scenario ... --timeout-seconds 180` invocations with status checks, then `collector stop` and source-only checks. Preserve each printed UUID and require clean shutdown. See [verification instructions](../verification.md) for the complete sequence. These results do not establish Azure conversion or ingestion.

## Upstream privacy finding and mitigation

The earlier direct CLI FILE metadata-only diagnostic exited 0 but **failed native content-off privacy**: despite explicit capture=false, chat/invocation spans contained nonempty `gen_ai.tool.definitions`. No other known gated field or the actual token was found in that checked raw capture. The discrepancy remains in private `.local/diagnostics/` evidence.

Both collector configurations now delete six exact known gated keys from span/event attributes before trace export for metadata-only/default resource scenarios. Only explicit `copilot.audit.scenario=full-content` or `delegated` preserves them. Real local metadata output confirms this downstream filtering, **not native CLI compliance**. The filter is not general secret redaction and does not sanitize arbitrary resource attributes or metrics; see [the security boundary](../security-and-data.md#observed-content-off-divergence-and-collector-boundary).

Earlier direct FILE full-content/delegated diagnostics also exited 0, but their ordinary fixture paths did not carry the marker in tool arguments. They do not pass the newer strict content contract by implication. New fixtures use `<marker>.txt`; actual input/output messages and same-tool arguments/result must still meet the verifier's mandatory marker checks.

Azure-verifier results carry `proof_scope: post-transform-collector-to-azure`; local checks carry `proof_scope: post-transform-collector-source-only`. Both retain `native_cli_content_privacy_verified: false`. Strict metadata absence remains required. Only an exact source prefix at the 8192-UTF-8-byte exporter limit is permitted for oversized optional system instructions/tool definitions and counted in backend comparison; mandatory marker fields are not exempt.

## Remaining limits

Azure what-if, apply/readback, repeat deployment, the three bounded telemetry scenarios, and the explicit retention reads above are confirmed. Full resource IDs remain private. Automated teardown refuses every existing group; only an authenticated, receipt-validated already-absent group can yield a no-op. Resources remain available for inspection with the collector stopped; other retention/cost settings and optional coverage are not inferred from ingestion success.

Sandbox, hooks, custom MCPs, skills, compaction, exception generation, and other optional features were not intentionally exercised or verified. The observed MCP lifecycle event does not establish custom MCP coverage. This is bounded synthetic observability evidence, not exhaustive or tamperproof auditing.

The current 209-test suite, compilation checks, repeated real Azure verification, and cloud configuration inspection are complete. Prior local proof remains source-only; later Azure proof does not retroactively change those runs. Preserve failures and proof boundaries when updating this record; never publish raw telemetry or credentials.

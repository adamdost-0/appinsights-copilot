# Verify actual CLI telemetry, not just health

**No cloud result is assumed.** A ready collector, zero CLI exit code, standalone synthetic OTLP generator, or unit-test fixture is insufficient proof. End-to-end verification requires real CLI source telemetry and matching Azure records from the same run. Current observations are in [the evidence record](evidence/local-example.md).

## Offline and configuration checks

Run from the repository root with Python 3.12+ (tested on 3.12.3):

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
az bicep build --file infra/main.bicep --stdout > /dev/null
az bicep build --file infra/resources.bicep --stdout > /dev/null
az bicep build-params --file infra/main.bicepparam --stdout > /dev/null
python3 -m scripts.collector validate
python3 -m scripts.collector validate --local-only
python3 -m scripts.run_smoke --help
python3 -m scripts.verify_ingestion --help
copilot help monitoring
```

To include actual local Docker probes rather than skip those opt-in tests:

```bash
COLLECTOR_DOCKER_TEST=1 python3 -m unittest discover -s tests -v
```

The final current-code regression run, `COLLECTOR_DOCKER_TEST=1 python3 -m unittest discover -s tests -q`, passed **209 tests with no skips**. The suite combines offline/mocked external boundaries with real Docker protobuf, privacy, rotation, collision, and clean-shutdown probes; it is separate from the live Azure proof. Production `verify()` also passed again for all three real Azure-mode runs after the final fix, without injected rows. Actual CLI/backend observations are recorded separately. Compilation proves syntax, not permissions or region support. Image validation proves the pinned collector accepts configuration, not ingestion.

## Execute bounded synthetic scenarios

Authorize the [data scope](security-and-data.md) first. The harness builds a narrow allowlisted child environment with fresh temporary `HOME`, `COPILOT_HOME`, XDG config/cache, and workdir outside the repository. It copies no normal user configuration and leaves the normal Copilot profile untouched. It disables custom instructions and built-in MCPs, uses `--disallow-temp-dir`, and removes inherited OTEL configuration.

Available tools are empty for metadata-only, `view` only for full-content, and `task` plus `view` for delegated; the latter uses separate arguments, `--available-tools task view`, not a comma-separated tool name. Shell, write, and URL tools are explicitly denied. The scenarios do not exercise private repositories, installed plugins, skills, or custom MCPs. On timeout, `run_session` terminates the owned subprocess group, checks for surviving group members even after subprocess communication completes, and kills remaining members after the grace period; timeout remains a failure.

The subprocess uses `COPILOT_OTEL_ENABLED=true`, `COPILOT_OTEL_EXPORTER_TYPE=otlp-http`, explicit `http/protobuf`, and endpoint `http://127.0.0.1:4318`. Every run adds string resource attributes `copilot.run.id` and `copilot.audit.scenario`. Content capture is controlled by `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`. Inherited file-exporter and TLS-only flags are removed: TLS options can silently disable an HTTP exporter.

Authenticate separately with `gh auth login` or provide a supported token environment variable. Authentication is passed in-memory through the child's supported token environment. The harness first checks `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, and `GITHUB_TOKEN`; if none is set it internally reads the authenticated GitHub CLI token. Do **not** run `gh auth token` to print or save it, and never put a GitHub token in `.env` or collector configuration.

For a single Azure-mode scenario, start the collector after deployment and privacy-filter validation, run the CLI, and then **stop the collector before verification**:

```bash
python3 -m scripts.collector start
python3 -m scripts.collector status
python3 -m scripts.run_smoke --scenario metadata-only --timeout-seconds 180
python3 -m scripts.collector status
python3 -m scripts.collector stop
```

Record the actual UUID printed by `run_smoke` before stopping. Do not invent one or substitute a fixture UUID. The manifest is `.local/runs/UUID/manifest.json` and contains `cli_version`, `run_id`, `scenario`, `capture_content`, `marker`, `started_at`, `finished_at`, `exit_code`, `evidence_path`, and `collector_mode`. Inspect its status and CLI outcome privately. A timeout/nonzero failure remains a failure, even if some spans arrived.

Azure verification requires an explicit `collector_mode: azure`; missing mode or local-only mode is rejected before any query. Do not relabel a diagnostic manifest to pass this check. Both `full-content` and `delegated` require `capture_content: true`.

Require `clean_shutdown: true` from stop before treating the capture as flushed. The lifecycle checks exited state, exit code 0, and absence of OOM; a successful Docker stop command alone is insufficient because it may have escalated to SIGKILL. Abnormal shutdown retains state/evidence for diagnosis and must not be upgraded to a clean capture. Ensure no CLI, collector, or other process can write the source while verifying. Clean shutdown is not an fsync or Azure-persistence guarantee. With the source complete and immutable, use the exact run UUID:

```bash
RUN_ID='replace-with-the-actual-completed-run-uuid'
python3 -m scripts.verify_ingestion --run-id "$RUN_ID" --timeout-seconds 600
```

For additional authorized scenarios, repeat **start -> status/run/status -> stop -> verify** separately. Alternatively, run the chosen scenarios sequentially under one collector, retain every printed UUID, then stop once and verify each UUID against the same now-immutable source. These are alternative smoke invocations to execute while that collector is running:

```bash
python3 -m scripts.run_smoke --scenario full-content --timeout-seconds 180
python3 -m scripts.run_smoke --scenario delegated --timeout-seconds 180
```

These last two commands each produce a **different** UUID. Verify each individually; do not reuse the metadata-only UUID. In `full-content`, the CLI is asked to read only a fabricated fixture with `view`. In `delegated`, one bounded task is asked to read the synthetic fixture; this scenario also enables content capture. Tool or delegation requests are not proof they happened: check observed execution spans and parent/child relationships. If unsupported or not exercised, report that, not success.

The current fixture filename is `<marker>.txt`, where the marker is `SYNTHETIC_AUDIT_<run-uuid>`. This makes the real `view` path argument carry the exact marker while the file content supplies it in the result. Do not rename the fixture to `fixture.txt` or inject artificial telemetry to satisfy the check. Earlier raw FILE diagnostics used an ordinary filename and do not establish the current strict input/output/tool-arguments/result proof.

For `collector start --local-only`, likewise stop after the chosen scenarios and require `clean_shutdown: true`, then inspect the immutable file evidence selected by each manifest and report **local-only** observations. The Azure verifier is not a local-only success checker. Do not run it to turn a diagnostic into an ingestion claim or assume metadata absence from an empty source.

### Immutable source and rotation recovery

The verifier detects source changes and recognized rotation siblings and fails rather than silently reading only one segment. Do not run it against a live append-only file. Do not restart a collector writing that file until all associated verifications finish.

If rotation occurred, stop all writers and retain every segment, including the active file. Deliberately consolidate **all** segments in chronological order into a new complete JSON-lines file in a private `0700` directory under `.local/`, with file mode `0600`. Update each affected manifest's `evidence_path` explicitly to that new absolute, non-symlink path; preserve the original segments and provenance privately. The new file must have no rotation siblings and remain unchanged through verification. Never discard segments or malformed/truncated lines just to pass. If segments were lost or completeness cannot be established, mark the run incomplete and obtain a fresh bounded run instead.

### Reproduce source-only checks without Azure

After clean shutdown and the completeness/immutability checks above, this helper runs the production parser and source checker against a chosen manifest. It prints only status, aggregate counts, and proof boundaries, and exits nonzero unless the source validates. It does not enforce collector lifecycle itself, query Azure, fabricate backend rows, change the manifest, or relabel local-only mode.

```bash
RUN_ID='replace-with-the-actual-completed-run-uuid'
python3 - "$RUN_ID" <<'PY'
import json
from pathlib import Path
import sys
from uuid import UUID
from scripts.verify_ingestion import (
    parse_source, check_source_evidence, trusted_path,
)

run_id = str(UUID(sys.argv[1]))
folder = Path.cwd() / ".local" / "runs" / run_id
manifest = json.loads(trusted_path(str(folder / "manifest.json")).read_text())
evidence = trusted_path(manifest["evidence_path"]).read_text(encoding="utf-8")
result = check_source_evidence(manifest, parse_source(evidence, run_id))
print(json.dumps({key: result[key] for key in (
    "status", "counts", "proof_scope", "azure_ingestion_proven",
    "native_cli_content_privacy_verified",
)}, indent=2))
sys.exit(0 if result["status"] == "source-validated" else 1)
PY
```

Successful local output is `status: source-validated`, `proof_scope: post-transform-collector-source-only`, and `azure_ingestion_proven: false`, not `passed` or Azure proof. The confirmed runs' private `local-checks.json` files retain their results; the helper above only prints a summary. The end-to-end `scripts.verify_ingestion` CLI still rejects local-only manifests by design.

## Required source/backend comparison

| Evidence | Expected mapping and check |
| --- | --- |
| CLI INTERNAL/CLIENT invocation, chat, tool spans | `AppDependencies`; compare names, trace/span IDs, parent IDs, run/resource/span attributes |
| Ordinary span events, including lifecycle events | `AppTraces`; compare event name, properties, trace and parent-span correlation |
| Token usage and operation-duration metrics | `AppMetrics`; every required series, identified by name plus resource/data-point dimensions, must match a positive sum/count snapshot |
| Exception span events, if actually emitted | `AppExceptions`; do not require exceptions from a scenario that never raised one |
| Metadata-only | Positive nonempty post-transform collector/backend evidence with gated content fields absent on both sides; not a native CLI privacy claim |
| Content-on (full-content and delegated) | Marker-bearing input and output messages, plus marker-bearing arguments and result on the same observed tool span; captured properties must survive source-to-Azure comparison |
| Delegation | Actual child-agent invocation and valid parent relationships in source and backend |

Relevant semantic names include `gen_ai.operation.name` values `invoke_agent`, `chat`, and `execute_tool`; metrics include `gen_ai.client.token.usage` and `gen_ai.client.operation.duration`. Inspect the installed CLI's monitoring reference for the exact version; do not infer optional signals merely from names listed in documentation.

Classify agent invocations by `gen_ai.operation.name=invoke_agent` or an appropriate span-name prefix, not exact equality with the bare span name `invoke_agent`: the actual local delegated diagnostic emitted `invoke_agent explore`. Matching a shared trace ID is useful but does not replace parent-span validation or matching Azure evidence.

Live Azure ingestion normalized an empty source root parent to a 32-hex `ParentId` equal to `OperationId`. The verifier accepts this only for source roots; it does not relax child-span or event parenting. Arbitrary root parents or mismatched child parents still fail.

The complete shared JSON-lines source is validated before selecting the requested run's resource groups; malformed unrelated groups are not silently skipped. Selected telemetry must identify `service.name=github-copilot` and instrumentation `scope.name=github.copilot`. Resource `service.version` must normalize to the manifest's actual CLI version; instrumentation `scope.version` is not the CLI version. Accepted CLI version forms are bare semver, an optional `v` prefix, and an optional `GitHub Copilot CLI ` label. An unfamiliar prefix fails explicitly rather than guessing; retain sanitized version output for review.

The actual CLI emitted identical duplicate `gen_ai.response.model` attributes. Parsing accepts duplicates only when the typed values are identical; conflicting duplicates fail rather than silently selecting one value.

The verifier reads the manifest's immutable private source file, selects `copilot.run.id`, and queries the recorded workspace using an explicit Azure subscription and Log Analytics audience. It uses the single checked-in template [`queries/verify_ingestion.kql`](../queries/verify_ingestion.kql), not separate per-signal query files. Queries are bounded by the run time window and Application Insights resource ID. It parses named response columns, rejects partial/malformed results, and polls missing data or retryable responses only until the deadline. Authentication/authorization errors are not converted into empty success.

**Privacy boundary:** the raw metadata-only CLI diagnostic leaked `gen_ai.tool.definitions` despite capture=false. The implemented collector transform removes the six known gated fields before both trace exporters for metadata-only/default scenarios. The 2026-09-13 live metadata-only proof confirmed their absence in post-transform source and matching Azure records. The verifier source is post-transform evidence, so this pass is not native CLI content-off compliance. Preserve strict absence checks and retain the upstream discrepancy separately; see [security and data handling](security-and-data.md#observed-content-off-divergence-and-collector-boundary).

Private `.local/runs/UUID/results.json` records the result and `.local/runs/UUID/raw-query.json` retains query evidence. Exit zero means the implemented verifier checks passed; `failed`, deadline expiry, or `unsupported` return nonzero. A transient pending state is not a completed pass. Review reasons and coverage before publishing a sanitized outcome. Do not publish raw responses or CLI stdout/stderr.

Azure-verifier results explicitly include `proof_scope: post-transform-collector-to-azure` and `native_cli_content_privacy_verified: false`, plus the upstream tool-definition leak limitation. Source-only checks instead use `proof_scope: post-transform-collector-source-only` and deny Azure proof. Preserve these fields and the limitation when reporting results, including a downstream pass. The source-side check still rejects gated tool definitions; filtering defines the boundary, not a relaxed verifier rule.

For content-on spans/events, the verifier permits one narrowly defined fidelity loss in oversized optional `gen_ai.system_instructions` or `gen_ai.tool.definitions`: the exact source prefix at the exporter's **8192 UTF-8 byte** limit, measured in bytes rather than characters. Live system-instruction values retained 8180 and 8190 characters while each occupied 8192 UTF-8 bytes. Accepted occurrences are recorded as `counts.optional_content_truncations`, explicitly reporting that content transport is not lossless. Missing fields, arbitrary shortening, or altered prefixes do not qualify. This exception never weakens metadata-only absence checks or required input/output/tool-arguments/result marker proof.

## Fidelity and coverage limits

The classic Azure exporter converts the OTLP model: histogram bucket boundaries/counts and summary quantiles are not retained as a lossless distribution, and attributes can be truncated or constrained. Large system instructions and tool definitions can exceed conversion limits; their loss/truncation is a documented fidelity limitation, not a promise of lossless full-prompt storage. Keep that distinction separate from mandatory marker-bearing input/output messages and same-tool arguments/result proof: a missing required marker is not waived as expected truncation. Report actual verifier outcomes and limitations rather than declaring all bytes preserved. Metrics need not carry `OperationId`; use run/resource attributes. Never add successive cumulative metric snapshots as if they were independent token usage. One matching series cannot hide a missing dimensioned series, including an output-token series.

The private file is a collector-side, post-transform source copy when the privacy filter is active, not untouched CLI output or an immutable record of everything the CLI did. Intentional privacy filtering removes data before fidelity comparison. Process termination, buffering, rotation, exporter failures, sampling or unsupported instrumentation can leave gaps. Health proves neither complete source capture nor backend delivery.

Sandbox, hooks, custom MCPs, skills, compaction, and other optional behaviors are **not covered unless safely exercised and observed**. Do not force unsafe activity or claim "all telemetry" from the three bounded scenarios. The synthetic scope intentionally excludes normal user content.

## Publish sanitized evidence

Use [the evidence record](evidence/local-example.md) to report date, actual versions, executed stage, observed outcome, private evidence location, and limitations. Add real aggregate counts only after the coordinator supplies measured results. Keep failed and blocked stages visible. Remove identifiers, captured text, usernames, credentials, private paths, and raw payloads from anything committed.

## Official CLI monitoring reference

For the installed version, `copilot help monitoring` is the authoritative GitHub-shipped monitoring reference; preflight saves its output privately in `.local/versions/copilot-monitoring.txt`. See also the [official GitHub CLI command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference) and [CLI reference index](https://docs.github.com/en/copilot/reference/copilot-cli-reference). A dedicated public monitoring article URL has not been confirmed; do not substitute an invented link for the version-specific help.

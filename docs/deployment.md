# Deployment and collector lifecycle

## Prerequisites and boundaries

The supported cloud is public `AzureCloud`. Install Python **3.12+** (tested on 3.12.3), Azure CLI/Bicep, Docker client plus a **local** running daemon capable of host bind mounts, and Copilot CLI. Version observations, not blanket compatibility guarantees, are in [the evidence record](evidence/local-example.md).

Azure authentication is a separate authorized operator action. The scripts never log in, register providers, grant roles, or silently select another subscription. An enabled account needs subscription-scope deployment and resource-group creation permissions plus the required resource permissions. `Microsoft.OperationalInsights` and `Microsoft.Insights` must already be registered. Query access additionally requires appropriate workspace data-plane RBAC (for example Log Analytics Reader at the intended workspace scope); public query access is not anonymous access. GitHub authentication alone cannot deploy or query Azure.

For synthetic Copilot inference, authenticate GitHub CLI separately with `gh auth login`, or supply a supported token through `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, or `GITHUB_TOKEN`. The harness reads an existing GitHub CLI token internally when no supported token variable is set. Never write a GitHub token into `.env`, `.local/collector.env`, or collector configuration. The harness leaves normal Copilot configuration untouched; do not copy a normal profile into its temporary home.

```bash
python3 -m scripts.preflight
python3 -m scripts.deploy --help
python3 -m scripts.collector --help
python3 -m scripts.teardown --help
```

Preflight checks versions and Azure readiness and saves CLI monitoring/environment/permissions help privately under `.local/versions/`. Resolve failures before cloud operations.

## Inspect and deploy

`infra/main.bicep` is subscription-scoped and creates the dedicated group `rg-copilot-otel-audit`; its module creates exactly two monitoring resources:

| Resource | Configuration |
| --- | --- |
| Log Analytics workspace | `Microsoft.OperationalInsights/workspaces@2023-09-01`, `PerGB2018`, workspace retention 30 days, daily quota 1 GB |
| Application Insights | `Microsoft.Insights/components@2020-02-02`, web application, linked workspace, `DisableLocalAuth=false` |

Public ingestion and query are enabled. Workspace access using only resource permissions is explicitly disabled. This design needs no DCR, DCE, hosted compute, or Grafana. Azure native OTLP ingestion is a **separate preview architecture** requiring additional resources and a different metrics store; do not substitute its endpoints for this classic exporter.

Compile locally, then use your actual canonical lowercase subscription UUID:

```bash
az bicep build --file infra/main.bicep --stdout > /dev/null
SUBSCRIPTION_ID='replace-with-your-actual-subscription-uuid'
python3 -m scripts.deploy --subscription "$SUBSCRIPTION_ID" --location eastus --what-if
```

`eastus` is the default, not a required region. Another region is accepted only if available in the selected subscription and supported by both resource providers. Check organizational policy and data residency before choosing. What-if performs account/provider/location checks and ARM validation; it is not a local-only command.

Azure CLI 2.89.0 does not support `--subscription` on `az account list-locations`. The deployer instead uses an explicit ARM GET at `/subscriptions/<subscription-id>/locations?api-version=2022-12-01` to preserve subscription scope; do not replace this with an implicitly selected-account lookup.

After reviewing the proposed changes and authorizing the cost:

```bash
python3 -m scripts.deploy --subscription "$SUBSCRIPTION_ID" --location eastus --apply
```

Apply repeats validation and what-if, verifies ownership before mutation, then checks safe outputs/inventory, saves the ownership receipt, and reads back workspace linkage, authentication/network settings, and actual workspace retention/cap. It withholds raw ARM responses and connection strings. What-if rejects deletion, ignored, unsupported, and unknown change types. An existing group without the matching local receipt is **not adopted**; repeat deployment is supported only for the verified receipt-owned group. Missing parent resources, extra generic inventory entries, mismatched ownership tags, changed location, and receipt mismatches are refusal conditions, not reasons to bypass safeguards. These are parent ownership checks: ancillary child APIs are not queried or required for deployment. Generic ARM listing does not expose every provider child/proxy resource, so a passing reuse audit is not an exhaustive ownership inventory or deletion authorization. See [the infrastructure contract](../infra/README.md) for precise checks and recovery limits.

A repeated apply need not show only `NoChange`: provider normalization can produce a `Modify` while resource IDs remain stable. The observed second apply succeeded with one `Modify` and two `NoChange` entries, not duplicate resources. Review the actual preview and readback; do not treat either a successful apply or stable IDs as ingestion proof.

The safe receipt `.local/azure.json` contains only:

```text
subscription_id
resource_group
application_insights_resource_id
workspace_resource_id
workspace_customer_id
deployment_name
ownership_marker
location
```

The connection string belongs **only** in `.local/collector.env`, mode `0600`, as the single unquoted assignment `APPLICATIONINSIGHTS_CONNECTION_STRING=...`. The deployer writes it; do not source the file or paste its contents into shell commands, Bicep parameters, committed configuration, or evidence.

The ownership receipt is retained if later service readback or secret-file writing fails. Its presence does not prove deployment completion or ingestion; an older `collector.env` may remain after failure. An earlier partial failure, before safe outputs/inventory and receipt persistence, can leave cloud resources without a receipt. The scripts then refuse automatic adoption or deletion. An administrator must inspect deployment history and actual resource IDs/tags and recover deliberately outside this CLI; never fabricate ownership evidence to bypass refusal.

Readback reports **only actual workspace** retention and daily cap. The template does not configure, and the deployer does not query or verify, the separate Application Insights cap or individual table retention. Workspace retention is not proof that all `App*` tables delete data after 30 days. Review actual per-table retention and applicable Application Insights limits separately in Azure. A daily cap is not a hard budget or guaranteed cost ceiling.

For this live sample, separate operator ARM reads confirmed **90-day analytics and total retention** on `AppDependencies`, `AppMetrics`, and `AppTraces`, while workspace retention is 30 days and its cap is 1 GB. Separate billing inspection confirmed Application Insights `Basic`, a **100 GB** cap, and **90%** warning threshold; the workspace cap is the lower effective ingestion limit, not a hard cost ceiling. These additional checks are not part of deployer readback. See [the dated configuration evidence](evidence/local-example.md#observed-retention-and-caps); do not infer a 30-day deletion policy from the workspace setting.

The installed CLI billing inspection command is:

```bash
az monitor app-insights component billing show \
  --app ai-copilot-otel-qiwrhpw7rm2q4 --resource-group rg-copilot-otel-audit \
  --subscription "$SUBSCRIPTION_ID"
```

Substitute the actual recorded component name for another deployment. This read succeeded for the sample without dependency installation; keep subscription and raw runtime details private.

## Validate and start the collector

`collector/image.txt` pins Contrib `0.160.0` by immutable digest. `collector/otelcol.yaml` uses `azure_monitor`, not `azuremonitor`, with `spaneventsenabled: true`. Do not replace the image with `latest`.

Both collector configurations implement the metadata-only/default OTTL privacy transform before trace export. Raw CLI capture=false leaked tool definitions; native content-off is not sufficient. The 2026-09-13 proof confirmed filtered metadata-only output in real collector capture and matching Azure records. Validate the selected configuration and restart an existing collector to load changes; configuration is not hot-reloaded. See [the exact boundary](security-and-data.md#observed-content-off-divergence-and-collector-boundary).

```bash
python3 -m scripts.collector validate
python3 -m scripts.collector start
python3 -m scripts.collector status
```

Validation uses a synthetic connection string and a network-disabled validation container; Docker may need to fetch the pinned image first. It checks the actual collector binary/configuration, not Azure connectivity. Normal start requires the private connection-string file. Host publications are loopback-only: OTLP HTTP/protobuf at `127.0.0.1:4318`, health at `127.0.0.1:13133/health`. Container listeners use container interfaces; do not expose the host ports publicly.

The owned container runs as the invoking UID/GID, read-only root filesystem, dropped capabilities, no-new-privileges, no restart policy, and a private evidence bind mount. Docker administrators can still inspect secrets. The health probe establishes local readiness only.

For a diagnostic without Azure ingestion, select the explicit local-only configuration:

```bash
python3 -m scripts.collector validate --local-only
python3 -m scripts.collector start --local-only
python3 -m scripts.collector status
```

Local-only mode uses `collector/otelcol-local.yaml`, with no Azure exporter and no credential injection, not even a synthetic key. It is a file-exporter diagnostic, **not a cloud success path**, and is never selected automatically. State, ownership labels, and command output bind the mode; missing/unknown modes are rejected. Stop an existing collector before switching modes, use fresh run UUIDs, and never relabel or overwrite a receipt. Both modes share the evidence file, so finish verification before restarting any writer.

`.local/collector.json` identifies the owned container and absolute `evidence_path`. The evidence is JSON-lines, not a single JSON document. Run `status` before and after each smoke. At the 8 MiB preflight bound or after rotation, commands refuse further smoke readiness. Stop, privately archive all evidence including rotated `otel-*.json` files, and explicitly clear the archived active files before another run. Rotation has 10 MiB segments and two backups; asynchronous cleanup means this is not a strict instantaneous disk ceiling. A single active file after rotation cannot prove complete capture.

The verification order is **start -> chosen CLI scenario(s), retaining each UUID -> stop -> verify each UUID**. Do not leave a CLI or collector writing the source during verification, and do not restart it against that file until verification finishes. If rotation occurred, follow [immutable-source recovery](verification.md#immutable-source-and-rotation-recovery): consolidate every retained segment into a new restricted immutable file and explicitly update affected manifest evidence paths. Never silently omit rotated segments.

## Stop and audit teardown

**Automated deletion of every existing Azure group is disabled**, even with a matching receipt and `--confirm`. The command below audits the recorded scope; it is not a delete command:

```bash
python3 -m scripts.collector stop
python3 -m scripts.teardown --subscription "$SUBSCRIPTION_ID" \
  --resource-group rg-copilot-otel-audit --confirm
```

`stop` stops and removes only the receipt-owned local container, preserving evidence. Require `clean_shutdown: true`: the script checks exited state, exit code 0, and no OOM; abnormal exit retains container/state/evidence for diagnosis. A successful Docker stop alone does not establish clean flushing. The teardown audit requires the explicit subscription, exact dedicated group, and matching receipt/account. For an existing group it performs parent ownership checks and then returns nonzero refusal, including when every check passes. `--confirm` confirms audit scope only; it never enables deletion. An already-absent group is the only successful no-op after receipt/account validation. No cloud delete request is issued.

Manual cleanup requires an administrator to inspect the exact recorded group in the Azure portal, assess provider-specific children/proxies and other users' private artifacts beyond generic inventory, confirm ownership and intended data loss, and perform cleanup separately. A passing deployment/reuse audit does not authorize that deletion. If the scope cannot be established, retain the resources and escalate; do not bypass the refusal with a broad shell delete command. See [the detailed audit limitations](../infra/README.md#parent-ownership-checks-and-teardown-refusal).

Nothing cleans up automatically on success. The audit preserves **all cloud resources and local files, including secrets**. After independently confirmed manual cleanup, archive needed evidence privately and remove only deliberately selected owned local artifacts when no longer needed. Resource deletion is not proof of immediate deletion from every retention/archive system.

## References

- [Create a workspace-based Application Insights resource](https://learn.microsoft.com/en-us/azure/azure-monitor/app/create-workspace-resource)
- [Configure Log Analytics retention](https://learn.microsoft.com/en-us/azure/azure-monitor/logs/data-retention-configure)
- [Log Analytics daily cap and limitations](https://learn.microsoft.com/en-us/azure/azure-monitor/logs/daily-cap)
- [Pinned v0.160.0 Azure Monitor exporter](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/v0.160.0/exporter/azuremonitorexporter)
- [Pinned v0.160.0 file exporter](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/v0.160.0/exporter/fileexporter)

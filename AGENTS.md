# Deployment workflow

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

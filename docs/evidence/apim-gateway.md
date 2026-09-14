# APIM gateway evaluation evidence

Measured on 2026-09-14 using Azure CLI/Bicep and isolated synthetic workloads.
This is separate from [Function evidence](function-relay.md) and
[historical direct-DCE evidence](v1.md). Actual resource identities, keys,
UUIDs, raw telemetry, query results, and deployment receipts remain private.

## Deployment and authentication

The separately owned Developer-tier APIM service was deployed deny-first, then
activated with a dedicated API-scoped subscription. Its system identity has
Monitoring Metrics Publisher on the exact existing native DCR. The existing
Function is not an APIM backend; LAW/AMW/DCE/DCR are deliberately shared.

Independent readback verified the service identity, ownership, TLS settings,
three exact HTTPS POST operations, fixed native destinations, subscription
scope/state/tracing, subscription-ID admission guard, and complete policy
contents. Original DCR permissions remained unchanged; only APIM's new
assignment was added. Reapply advanced its audit timestamp without changing
permissions.

Azure automatically created an Echo API, Starter/Unlimited sample products and
their subscriptions, plus a master subscription. Their policies/revisions were
inventoried: samples inherit the global deny; the telemetry API belongs to
neither product. No logger, diagnostic, named value, backend resource, or policy
fragment was present. Defaults were retained, not silently deleted.

The initial HTTP suite produced these results:

| Check | Observed result | LAW correlation |
| --- | --- | --- |
| Dedicated key, before/after controls | 204 | One exact synthetic log per control |
| Missing/random key on logs, traces and metrics routes | 401 | No matching synthetic markers |
| Actual service master key on telemetry API | 403 | No matching marker |
| Wrong method/path | 404 | No matching markers |
| JSON/gzip | 415 | No matching markers |
| Query-string credential attempt with a valid header | 400 | No matching marker |
| Unsupported chunked framing | 400 | No matching marker |
| Empty body | 400 | No marker can be carried |
| Declared 4 MiB + 1 | 413 from gateway | No matching marker |
| Exactly 4 MiB synthetic batch | 413 from backend | No matching marker; acceptance failed |

The size-boundary failure is retained, not counted as a successful positive
test. Two subsequent bounded native-path measurements established logs
acceptance at 1,048,576 bytes and backend rejection at 1,048,577 bytes. APIM's
declared and actual body guards were therefore lowered to **1 MiB on every
route**, without changing Function behavior. This is a conservative gateway
ceiling, not proof of the maximum native traces/metrics request sizes.

After the correction, the complete size/authentication suite, secondary-key
suite, and post-rotation suite passed **44 HTTP cases**, with matching LAW
positive counts and absent denied markers:

| Final check | Observed result | LAW correlation |
| --- | --- | --- |
| Exactly 1,048,576 bytes, 128-log batch | 204 | All 128 exact matching logs |
| 1,048,577 bytes | 413 from gateway, not backend | No matching marker |
| Secondary key before primary regeneration | 204 controls | One exact log per before/after control |
| Replacement primary after regeneration | 204 controls | One exact log per before/after control |
| Retired primary | 401 on first post-regeneration test | No matching marker |

The secondary key was unchanged by primary regeneration. No ordinary client
received either key. Negative absence is paired with successful controls, not
used alone as proof of admission enforcement. First-test revocation success
does not establish instantaneous propagation for every future rotation.

## Actual CLI and native metrics

A warm, isolated Copilot CLI 1.0.84-5 run with capture disabled produced **two
native spans and three correlated events** in the receipt-owned LAW. Resource
and trace/span joins preserved the exact fresh run identity. Content-related
attributes were metadata such as message source/interaction/turn identifiers
and message count; prompt, response and tool bodies were not present.
Two tool-definition fields contained only the permitted name/type metadata,
so the repository's metadata-only policy passed; strict six-field absence is
not claimed.

Native AMW queries independently verified positive, finite `histogram_count`
and `histogram_sum` results for all three required metadata-only metric families:
`gen_ai.client.token.usage`, `gen_ai.client.operation.duration`, and
`gen_ai.invoke_agent.duration`. Results carried the exact run, service and AMW
resource labels; wrong-run queries were empty. This is actual native histogram
persistence, not a conclusion drawn from span token/latency charts.
The same span/event, privacy and complete native metric checks passed again
after restarting the CLI with the replacement primary key and final 1 MiB
policy. The final run is distinct from earlier warm/cold runs.

The first CLI run overlapped the initial cold forwarding request, which took
about 8.2 seconds. Its diagnostic reported the one-second OTel shutdown disposal
deadline being exceeded, and that run had no correlated LAW rows or metric
series. A bounded second run with unchanged client configuration, after warm
HTTP controls, succeeded. The failed run is not overwritten or relabeled.
Inference success does not ensure telemetry flush, and no delivery guarantee is
claimed. Synthetic Logs producer proof is separate from actual CLI span events.

## Corrections found by live gates

- `rawxml` exports are not necessarily well-formed XML. JSON negotiation with
  `format=xml` enables exact canonical comparison.
- XML-encoded checked-in policies must be submitted with content format `xml`;
  `rawxml` retained encoded entities in C# expressions. Corrected readback
  matched the source exactly.
- APIM rejects unbraced single-statement C# control flow in policy expressions.
  The failed activation stayed deny-by-default and created no client
  subscription. Braced blocks resolved the provider validation error.
- What-if initially proposed re-enabling the legacy portal through an omitted
  default. Explicit security/transport settings preserved the observed service
  state. Provider representation differences were reviewed, not blanket-ignored.

These code corrections have focused RED/GREEN contract coverage. Follow the
[APIM runbook](../apim-deployment.md), including independent policy, ownership,
HTTP and backend checks, rather than treating ARM success as acceptance.
Final local regression passed 149 Node tests and 253 Python tests; native,
Function, APIM and workbook Bicep templates compiled. The existing native AMW
schema warning remains; compilation is not substituted for the live evidence.

## Remaining gates

The Developer tier and native OTLP preview are not production/SLA approval.
Rate/quota enforcement has policy readback but no live load/exhaustion test.
Managed streaming allocation bounds and total-request deadlines are not proved.
No ordinary users, Windows/AD/GPO secret distribution, authenticated workbook
rendering, or approved tool-activity histogram scenario were tested.
Host/user labels and subscription keys are not authenticated employee identity.
The shared workspace still accepts the separately authorized Function/direct
paths; APIM authentication is not proof of provenance for every workspace row.
Read-only post-test comparison preserved native and Function resource identities,
configuration, Function ingress, original DCR roles, all 39 saved-search hashes,
and the 686-table inventory. No fresh Function data-plane or off-network ingress
experiment was run as part of this APIM evaluation.

# Arena and ML evidence completion slice — 2026-09-14

Local implementation only. This build does not deploy, authorize trading,
produce missing market history, or establish a profitable strategy.

## Completed software

| Area | Change | Boundary |
| --- | --- | --- |
| ML accounting | Stream and verify the full captured journal prefix; retain accounting records only | Broken/legacy chains and torn tails still reject |
| Feature binding | Stream feature history and retain exact outcome decision IDs | Every record is checked; unavailable or late features remain inadmissible |
| Lane inventory | Disclose infrastructure journals separately; report each accounting lane and orphan fill file | Unknown journals are never silently skipped |
| ML Lab | Show evidence worklist, ownership, per-lane failures and infrastructure exclusions | Aggregate counts cannot certify per-plan admission or promotion |
| Arena intake | 13 reviewed candidates copied into new source-bound research IDs with immutable contracts and lineage | Original sources and past failures unchanged; no new backtest result |
| Lake diagnostics | Per-timeframe internal gaps distinguish absent rows from stored-unverified rows, with contiguous-run counts | Existing hourly recovery planner remains authoritative; raw-file presence does not prove repairability |
| Trusted HTTPS | Optional Compose override and read-only domain/DNS/trust preflight | No activation, certificate issuance, firewall or access-control change |

## Accounting limits

Journal/feature streaming captures one finite prefix at open. Later appends are
not part of that audit. All records in that prefix must verify before derived
evidence is published. Limits remain explicit: 2 GB streamed file, 8 MB record,
64 MB retained accounting/selected-feature payload, 128 MB materialized fills.
Exceeding any limit blocks rather than truncates. This removes the former
128 MB telemetry-journal barrier, not every possible operational capacity limit.

Only the exact infrastructure names `delta_product_specs.journal.jsonl` and
`shadow_portfolio.journal.jsonl` are excluded from paper outcome accounting;
both remain visible. Empty accounting inventory is incomplete, not green.

The worklist's feature, label and economic-validation items remain pending
until per-plan evidence is reviewed. These are prerequisites, not a second
automatic readiness engine. Existing immutable dataset/run validators remain
the admission authority.

## Reviewed candidate intake

`research/prereg/arena_intake_20260914.json` records reviewed source hashes and
claims. New IDs end in `__btcusd_1h_contract_v1`; the only source change is the
ID assignment. Contracts fix Delta India BTC, 1h close/next-open, delta_swing
costs, 1,440 training + 720 test bars, 48-bar maximum hold and funding excluded.
These are exploratory research contracts, not scalper or promotion claims.

The 13 original uncontracted imports are retained. Their results are not
retroactively certified. The new copies still require causal validation and
sufficient verified canonical history through the governed runner.

Further intake is explicit, never guessed from unknown code:

```bash
.venv/bin/python -m vnedge.research.candidate_intake SOURCE.py \
  --parent-sha256 REVIEWED_SOURCE_SHA256 \
  --strategy-id ai_NEW_RESEARCH_ID --claim 'A precise reviewed market claim.'
```

The helper writes the contract and lineage before the discoverable source.
Conflicting existing artifacts reject; this does not modify the live registry.

## Optional trusted HTTPS

An operator must first provide a public domain, configure DNS and authorize
the appropriate network access. Caddy's public certificate setup requires
working domain validation and persistent certificate storage; see the
[official automatic HTTPS requirements](https://caddyserver.com/docs/automatic-https).

The existing 8765 listener and allowlist remain unchanged. The override adds
80/443 with the same loopback binding default. Public binding and approved
client CIDRs remain explicit operator choices; application authentication is
not changed by this build.

Before activation:

```bash
.venv/bin/python -m vnedge.dashboard.tls_preflight \
  --domain YOUR_DOMAIN --expected-ip YOUR_VM_IP
```

When authorized, preserve the following Compose selection in the deployment
environment for this and subsequent releases:

```text
COMPOSE_FILE=docker-compose.yml:deploy/compose.trusted-tls.yml
DASHBOARD_DOMAIN=YOUR_DOMAIN
```

Validate the merged Compose/Caddy configuration on the Docker host before
recreating the TLS service. Once DNS, ports and certificates are operational,
repeat the preflight with `--verify-https`. It requires ordinary system trust,
does not follow redirects and does not disable certificate checks. Static
configuration tests are not proof of a successfully issued certificate.

## Still requires external evidence or a user choice

- Missing canonical history and unresolved proof gaps: recover only from
  completeness-verified raw data through the canonical owner, or accumulate
  future tape. Official candles cannot be relabeled as canonical trades.
- Authoritative settled-funding coverage: public funding-rate candles remain
  insufficient. No synthetic settlement receipt producer was enabled.
- Approved reconciled paper episodes with timely features, enough outcomes
  per registered split, and untouched/forward economic validation.
- Domain/DNS and network authorization for HTTPS activation.
- Chosen external notification destination and optional LLM provider/config.

No new paper/live lane, model activation, capital change or deployment is
included. A zero-label count or blocked research candidate can remain correct.

# Module lifecycle and producer ownership

Import reachability is not a deletion policy. VNEDGE contains normal runtime
imports, `python -m` operator commands, research evidence, and deliberately
retained kill-policy tripwires. Every cleanup must classify a surface before
removing it.

| Lifecycle | Meaning | Removal rule |
|---|---|---|
| `runtime` | Imported by a default or profiled service | Replace all callers and operational tests first |
| `operator_cli` | Explicit `python -m` maintenance, replay, ingest, or audit command | Remove its documented command and CLI tests together |
| `research` | Opt-in experiment that creates fresh evidence | Preserve pre-registration and burned-window records |
| `evidence_only` | Historical implementation/report needed to explain a kill | May not be imported by capital runtime; retain its policy tripwire |
| `retired` | No supported invocation and no evidence obligation | Delete module, tests, route, artifact reader, and documentation together |

## Current deployed producers

| Service | Lifecycle | Produces |
|---|---|---|
| `multi-lane-shadow` | `runtime` | runtime snapshots, lane telemetry, dashboard API |
| `pulse-recorder` | `runtime` | public trade lake and canonical closed candles |
| `dashboard-tls` | `runtime` | TLS edge only; no market artifacts |
| `research-loop` | `research` | opt-in evidence under `research/live_research` |

The default stack does not schedule the agent job runner, scanner tournament,
Pine/Lux research, ML training, or promotion jobs. A dashboard panel backed
only by one of those artifacts must say `not configured`/`evidence only`; it
must not imply that a producer is running.

## Permanent boundaries

- Killed HF/scanner code is `evidence_only`, never capital eligible.
- Settings mutations cannot place orders, promote, clear kills, or enable live.
- An unavailable artifact is not healthy telemetry.
- A new dashboard artifact must name its producer, schedule, freshness SLA, and
  missing-state behavior in the same change.
- Bulk deletion based only on an AST import graph is prohibited.

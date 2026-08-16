# Correction cockpit

`/app` is VNEDGE's read-only, measurement-first React cockpit. The classic
dashboard remains at `/` while parity work continues.

## Implemented

- **C1 — global truth chrome:** server-derived runtime mode, capital roster,
  latched kill state, feed quality, UTC clock, and persistent live-block banner.
- **C2 — lanes:** registry-derived `KILLED`, `RESEARCH_ONLY`, `eligible`, or
  `unknown` status; killed strategies are rendered `off` and never capitalized.
- **C3 — risk:** daily halt usage, journal availability/recovery degradation,
  gateway reject summaries, public-feed health, and explicit Delta private
  stream `not_implemented` state.
- **P0/P1 hardening:** public liveness alias `/healthz`, Compose edge health,
  control-room color tokens, low-decoration surfaces, and keyboard-focusable
  data tables.

The default screen is Pulse. Navigation is Pulse, Lanes, Risk, Journal, and
Research. Research links are evidence-only and the cockpit exposes no order or
strategy-promotion controls.

## Read-only API

| Route | Purpose |
|---|---|
| `GET /api/lanes` | Policy-labelled active roster and capital truth |
| `GET /api/risk/snapshot` | Kill, halt, journal, gateway, feed, and live blockers |
| `GET /api/pulse/{symbol}` | Coalesced Market Pulse snapshot |

Every route uses the existing dashboard authentication. `can_trade` is always
false on correction API payloads. The only dashboard POST route is credential
exchange at `/auth/session`; there is no order or promotion mutation route.

## Remaining phases

C4–C7 continue Pulse depth: complete forming-hour metrics, missing-hour strip
semantics, chart/AVWAP polish, constrained hour-close analysis, and coalesced
notifications. C8 removes contradictory wording from the classic cockpit after
`/app` reaches parity.

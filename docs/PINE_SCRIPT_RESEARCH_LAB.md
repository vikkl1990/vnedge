# Pine Script Research Lab

Status: read-only research surface.

Purpose: build a durable knowledge base for public/open-source TradingView
scripts and user-supplied Pine exports, then decide which ideas deserve a
causal VNEDGE port and multi-timeframe crypto replay.

## Why this exists

TradingView has a large public script catalog and an open-source discovery
mode, but catalog pages do not reliably expose Pine source in static HTML, and
many scripts are protected or invite-only. VNEDGE therefore treats each script
as a provenance-bound research record:

1. discover metadata and links,
2. ingest source only when it is public/open-source or supplied by the user,
3. hash the source and record license/provenance,
4. run AI review for crypto portability and repaint/lookahead risk,
5. port only clean mechanisms into VNEDGE strategy classes,
6. replay on all configured timeframes/venues with fees and slippage,
7. require untouched-window judgment before shadow/paper promotion.

No script review can trade or promote directly.

## Dashboard surface

New route:

- `/pine-research` serves the standalone Pine Research Lab page.
- `/pine-research/kb` serves the token-gated JSON knowledge base.

The endpoint falls back to a seed payload when the generated artifact is absent,
so the page remains useful during early deployment. The expected generated
artifact is:

```text
research/pine_scripts/pine_research_kb.json
```

`create_app(..., pine_research_path=...)` can point the dashboard at another
artifact path.

The production Compose dashboard mounts this directory read-only:

```text
./research/pine_scripts:/app/research/pine_scripts:ro
```

Publish or refresh the artifact with:

```bash
python -m vnedge.research.pine_script_research \
  --source-dir research/pine_scripts/sources \
  --output research/pine_scripts/pine_research_kb.json
```

Explicit user-supplied exports can be reviewed without scanning the directory:

```bash
python -m vnedge.research.pine_script_research \
  ~/Downloads/open_indicator.pine \
  --source-dir /path/that/does/not/exist \
  --output research/pine_scripts/pine_research_kb.json
```

Use `--no-defaults` for a clean artifact with only supplied/open-source files.

## Knowledge-base shape

Each record contains:

- `script_id`, title, URL, author, kind,
- source availability, license, line count, SHA-256 hash,
- detected features and risks,
- crypto portability verdict,
- crypto fit score,
- porting notes,
- AI uplift ideas,
- timeframe backtest cells,
- decision and promotion flags.

Promotion flags are always false at this layer.

## Verdict meanings

- `PORTABLE`: clean enough to port, still needs VNEDGE replay.
- `PORTABLE_WITH_CHANGES`: useful mechanism, but requires causal/execution fixes.
- `RESEARCH_ONLY`: useful as a feature or teaching overlay, not a standalone lane.
- `BLOCKED_NO_SOURCE`: metadata only; no lawful/causal port can be built yet.
- `BLOCKED_REPAINT_RISK`: MTF/lookahead/repaint risk must be resolved first.

## Current seed records

The initial page includes:

- TradingView public scripts catalog: discovery record, source blocked.
- Luxara Live Plan QTM: ported as `luxara_live_plan_qtm_v1`; research candidate
  only after same-data VM replay.
- Luxara Break & Bounce V27: ported as `luxara_break_bounce_v27_v1`; telemetry
  only because broad breakouts failed and the strict pulse is sparse.

## Next build

The next production-grade step is a crawler/reviewer job that publishes
`research/pine_scripts/pine_research_kb.json` in chunks:

- catalog/import batch,
- source-hash batch,
- AI review batch,
- port/backtest queue batch,
- dedupe against existing VNEDGE scanner families.

The crawler must not scrape or store protected/invite-only source. It should
only review metadata for those and mark them `BLOCKED_NO_SOURCE`.

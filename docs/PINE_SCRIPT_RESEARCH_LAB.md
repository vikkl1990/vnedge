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

Bulk TradingView catalog links can be added as metadata-only backlog:

```bash
python -m vnedge.research.pine_script_research \
  --catalog-url https://www.tradingview.com/scripts/ \
  --source-dir research/pine_scripts/sources \
  --output research/pine_scripts/pine_research_kb.json \
  --max-catalog-records 250
```

For large profile/tag pages, save the HTML once and import it in chunks:

```bash
python -m vnedge.research.pine_script_research \
  --catalog-html research/pine_scripts/catalog/tradingview_scripts_page_1.html \
  --source-dir research/pine_scripts/sources \
  --output research/pine_scripts/pine_research_kb.json \
  --no-defaults
```

Catalog rows are intentionally `BLOCKED_NO_SOURCE`. They are useful for
prioritization and AI clustering, but cannot be ported or backtested until
lawful public/open-source Pine source is supplied.

When a catalog URL and a source-backed Pine export look like the same script,
the publisher reconciles them into one source-backed record. The source record
wins, while `catalog_urls` and `catalog_script_ids` preserve discovery
provenance. This prevents the lab from double-counting a script after source is
supplied.

## Knowledge-base shape

Each record contains:

- `script_id`, title, URL, author, kind,
- source availability, license, line count, SHA-256 hash,
- detected features and risks,
- crypto portability verdict,
- crypto fit score,
- porting notes,
- AI uplift ideas,
- mechanism cluster, priority score, and next action,
- timeframe backtest cells,
- decision and promotion flags.

Promotion flags are always false at this layer.

`next_action` is the operator queue:

- `REQUEST_OPEN_SOURCE_EXPORT`: catalog hit only; source is still required.
- `PORT_CAUSAL_FEATURES_AND_REPLAY`: source exists and the mechanism can be
  ported into VNEDGE for causality tests and replay.
- `RUN_CAUSALITY_AUDIT`: source exists but repaint/lookahead risk is present.
- `DISTILL_FEATURES_ONLY`: useful as ML/features, not a standalone scanner.

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

## Chunked research loop

The production-grade loop publishes `research/pine_scripts/pine_research_kb.json`
in chunks:

- catalog/import batch,
- source-hash batch,
- AI review batch,
- port/backtest queue batch,
- dedupe against existing VNEDGE scanner families.

The crawler must not scrape or store protected/invite-only source. It should
only review metadata for those and mark them `BLOCKED_NO_SOURCE`.

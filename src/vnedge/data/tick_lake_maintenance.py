"""Tick-lake maintenance — shard compaction, retention, disk-pressure guard.

    python -m vnedge.data.tick_lake_maintenance --data-root data [--dry-run]

The tick recorders write small atomic per-flush Parquet shards
(``ticks/exchange=*/symbol=*/stream=*/YYYYMMDD/{first_ts}-{seq}.parquet``).
That layout is crash-safe and reader-friendly, but it accumulates thousands
of tiny files per day and the lake eats the disk. This module keeps the lake
bounded, in three passes:

1. RETENTION — day directories (and legacy single ``YYYYMMDD.parquet``
   files) older than a per-stream horizon are deleted. Defaults: book 21d,
   trades 60d, liquidations 120d (env: ``TICK_RETENTION_BOOK_DAYS`` /
   ``TICK_RETENTION_TRADES_DAYS`` / ``TICK_RETENTION_LIQUIDATIONS_DAYS``).
   Book is the bulk of the lake; trades/liquidations are far smaller and
   feed research for longer. Unknown stream types are never aged out —
   deleting a new stream must be a deliberate code change, not an accident.

2. COMPACTION — every CLOSED (non-today) day directory is merged into ONE
   ts-sorted snappy Parquet file, written atomically (temp + ``os.replace``)
   and verified by row count BEFORE the source shards are removed. The
   compacted file lives inside the same day directory, so every existing
   reader keeps working unchanged (``replay_backtester._load_stream_frame``
   globs ``<day>/*.parquet``). Today's directory is never touched — the
   recorders are still writing to it. Re-compaction is safe: a straggler
   shard flushed into an already-compacted day is simply merged on the next
   pass.

3. DISK-PRESSURE GUARD — if the data filesystem is above
   ``DISK_USAGE_HALT_PCT`` (default 85), log CRITICAL and delete the OLDEST
   book days beyond even the retention horizon, oldest first, down to a hard
   floor of the most recent ``PRESSURE_FLOOR_DAYS`` (7) days — never below.
   Pressure mode NEVER deletes trades, liquidations, or the hist archive; if
   the floor is reached and usage is still above the threshold, it
   alert-logs and stops. The floor is a constant, not configuration —
   changing it is a reviewed code change (same policy as
   ``ABSOLUTE_MAX_LEVERAGE``).

HARD EXCLUSION: ``exchange=binanceusdm_hist`` — the deliberate Binance
Vision backfill archive (see ``aggtrades_backfill.py``) — is never
compacted, never aged out, and never touched by pressure mode. Any other
``exchange=*_hist`` directory gets the same protection, so future archives
cannot be deleted by accident.

Crash-safety note (documented, accepted): shards are deleted only AFTER the
compacted file is verified and atomically published, so a crash can never
LOSE rows. A crash in the tiny window between publish and shard deletion
leaves both on disk; the next pass re-merges them, which can duplicate that
day's rows on the research tape. Rows are research data, not execution
state — losing none beats occasionally doubling some.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)

# Deliberate backfill archives — maintenance must NEVER touch these.
HIST_EXCHANGE = "binanceusdm_hist"

DEFAULT_RETENTION_DAYS: dict[str, int] = {
    "book": 21,
    "trades": 60,
    "liquidations": 120,
}
RETENTION_ENV: dict[str, str] = {
    "book": "TICK_RETENTION_BOOK_DAYS",
    "trades": "TICK_RETENTION_TRADES_DAYS",
    "liquidations": "TICK_RETENTION_LIQUIDATIONS_DAYS",
}
DISK_USAGE_HALT_PCT_ENV = "DISK_USAGE_HALT_PCT"
DEFAULT_DISK_USAGE_HALT_PCT = 85.0
# Hard floor for pressure mode: never delete book days younger than this.
# Constant on purpose — lowering it is a reviewed code change, not config.
PRESSURE_FLOOR_DAYS = 7
# A globally out-of-order day needs one in-memory Arrow sort before it can be
# published as a single ordered file.  Keep that recovery path bounded: the
# normal recorder produces far smaller closed-day inputs, while a larger day
# remains untouched for an operator-run offline repair instead of risking an
# OOM kill in the maintenance container.
MAX_FULL_SORT_INPUT_BYTES = 160 * 1024 * 1024


# --- small helpers ---------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        logger.warning("bad %s value %r — using default %d",
                       name, os.environ.get(name), default)
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        logger.warning("bad %s value %r — using default %s",
                       name, os.environ.get(name), default)
        return default


def retention_days_from_env() -> dict[str, int]:
    return {stream: _env_int(env, DEFAULT_RETENTION_DAYS[stream])
            for stream, env in RETENTION_ENV.items()}


def _is_excluded_exchange(exchange: str) -> bool:
    """True for backfill archives that maintenance must never touch."""
    return exchange == HIST_EXCHANGE or exchange.endswith("_hist")


def _is_day_name(name: str) -> bool:
    return len(name) == 8 and name.isdigit()


def _tree_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
    return total


def _delete_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _disk_usage_pct(path: Path) -> float:
    u = shutil.disk_usage(path)
    return u.used / u.total * 100.0


def _iter_stream_dirs(ticks_root: Path, *, include_excluded: bool = False):
    """Yield (exchange, stream_name, stream_dir) for every stream directory.

    Backfill archives (``_is_excluded_exchange``) are skipped unless
    ``include_excluded`` (used only for size reporting, never for deletion).
    """
    if not ticks_root.is_dir():
        return
    for ex_dir in sorted(ticks_root.glob("exchange=*")):
        exchange = ex_dir.name.split("=", 1)[1]
        if _is_excluded_exchange(exchange) and not include_excluded:
            continue
        for sym_dir in sorted(ex_dir.glob("symbol=*")):
            for stream_dir in sorted(sym_dir.glob("stream=*")):
                if stream_dir.is_dir():
                    yield exchange, stream_dir.name.split("=", 1)[1], stream_dir


def _day_entries(stream_dir: Path) -> list[tuple[str, Path]]:
    """All per-day entries in one stream dir, sorted oldest first: shard day
    directories (``YYYYMMDD/``) AND legacy single files (``YYYYMMDD.parquet``)."""
    entries: list[tuple[str, Path]] = []
    for child in stream_dir.iterdir():
        if child.is_dir() and _is_day_name(child.name):
            entries.append((child.name, child))
        elif child.is_file() and child.suffix == ".parquet" and _is_day_name(child.stem):
            entries.append((child.stem, child))
    entries.sort(key=lambda e: e[0])
    return entries


# --- 1. compaction ---------------------------------------------------------------

def compact_day(day_dir: Path, *, now: datetime | None = None,
                dry_run: bool = False) -> dict | None:
    """Merge all Parquet shards of one CLOSED day directory into a single
    ts-sorted, snappy-compressed file (``compacted-<day>.parquet`` inside the
    same directory, so shard-globbing readers need no change).

    Returns a summary dict, or None when there is nothing to do: today's
    (or a future) directory — recorders still writing — an already-compacted
    day, an empty directory, or a day that failed verification (logged,
    shards left untouched; never silent).

    Safety order: write temp -> verify row count -> atomic ``os.replace`` ->
    only then delete the input shards. Nothing is deleted unless the merged
    file is verifiably complete.
    """
    day_dir = Path(day_dir)
    day = day_dir.name
    if not _is_day_name(day):
        return None
    today = (now or datetime.now(UTC)).strftime("%Y%m%d")
    if day >= today:
        return None  # today (or clock-skew future): recorders still writing here
    out = day_dir / f"compacted-{day}.parquet"
    inputs = sorted(p for p in day_dir.glob("*.parquet") if p.is_file())
    if not inputs or inputs == [out]:
        return None  # empty or already compacted
    metadata: list[tuple[int, int, int, Path, pa.Schema]] = []
    for p in inputs:
        try:
            parquet = pq.ParquetFile(p)
            if "ts_ms" not in parquet.schema_arrow.names:
                logger.error("compact %s: no ts_ms column — day left as-is", day_dir)
                return None
            rows = int(parquet.metadata.num_rows)
            if rows == 0:
                metadata.append((0, 0, 0, p, parquet.schema_arrow))
                continue
            first = parquet.read_row_group(0, columns=["ts_ms"])["ts_ms"]
            last = parquet.read_row_group(
                parquet.num_row_groups - 1, columns=["ts_ms"]
            )["ts_ms"]
            metadata.append(
                (
                    int(pc.min(first).as_py()),
                    int(pc.max(last).as_py()),
                    rows,
                    p,
                    parquet.schema_arrow,
                )
            )
        except (OSError, ValueError, AttributeError, pa.ArrowException) as exc:
            logger.error("compact %s: unreadable shard %s (%s) — day left as-is",
                         day_dir, p.name, exc)
            return None
    metadata.sort(key=lambda item: (item[0], item[1], item[3].name))
    expected = sum(item[2] for item in metadata)
    if dry_run:
        return {"day": day, "shards": len(inputs), "rows": expected, "dry_run": True}
    # clear any temp left by a crash before publish (its rows still live in shards)
    for stale in day_dir.glob(".*.tmp"):
        stale.unlink(missing_ok=True)
    tmp = day_dir / f".{out.name}.tmp"
    writer: pq.ParquetWriter | None = None
    written_rows = 0
    previous_ts: int | None = None
    stream_failed = False

    try:
        unified_schema = pa.unify_schemas([item[4] for item in metadata])
    except pa.ArrowException as exc:
        logger.error("compact %s: incompatible shard schemas (%s) — shards kept",
                     day_dir, exc)
        return None

    def _align(table: pa.Table) -> pa.Table:
        """Add nullable legacy fields and order/cast every shard identically."""
        columns: list[pa.ChunkedArray | pa.Array] = []
        for field in unified_schema:
            if field.name not in table.column_names:
                columns.append(pa.nulls(table.num_rows, type=field.type))
                continue
            column = table[field.name]
            if column.type != field.type:
                column = pc.cast(column, target_type=field.type, safe=True)
            columns.append(column)
        return pa.Table.from_arrays(columns, schema=unified_schema)

    # Detect cross-shard overlap before opening the output.  Overlap is not a
    # reason to delete or deduplicate rows: same-millisecond trades are valid,
    # and legacy shards may not carry trade_id.  A bounded global Arrow sort
    # preserves the exact tape while restoring the ordering invariant.
    ordered_ranges = True
    range_end: int | None = None
    for minimum, maximum, rows, _path, _schema in metadata:
        if rows == 0:
            continue
        if range_end is not None and minimum < range_end:
            ordered_ranges = False
            break
        range_end = maximum

    input_bytes = sum(path.stat().st_size for *_head, path, _schema in metadata)
    if not ordered_ranges and input_bytes > MAX_FULL_SORT_INPUT_BYTES:
        logger.error(
            "compact %s: out-of-order input is %d bytes (limit %d); "
            "offline repair required — shards kept",
            day_dir,
            input_bytes,
            MAX_FULL_SORT_INPUT_BYTES,
        )
        return None
    try:
        if not ordered_ranges:
            tables = [_align(pq.read_table(path)) for *_head, path, _schema in metadata]
            combined = pa.concat_tables(tables)
            order = pc.sort_indices(combined, sort_keys=[("ts_ms", "ascending")])
            table = combined.take(order)
            # The sorted table no longer references the source chunks.  Release
            # them before Parquet encoding so large (10M+ row) days do not keep
            # both copies resident throughout the write.
            del tables, combined, order
            writer = pq.ParquetWriter(tmp, unified_schema, compression="snappy")
            writer.write_table(table, row_group_size=100_000)
            written_rows = table.num_rows
        else:
            for _minimum, _maximum, _rows, path, _schema in metadata:
                parquet = pq.ParquetFile(path)
                for batch in parquet.iter_batches(batch_size=100_000):
                    table = _align(pa.Table.from_batches([batch]))
                    order = pc.sort_indices(table, sort_keys=[("ts_ms", "ascending")])
                    table = table.take(order)
                    if table.num_rows:
                        first_ts = int(table["ts_ms"][0].as_py())
                        if previous_ts is not None and first_ts < previous_ts:
                            raise ValueError(
                                "out-of-order row groups require offline repair"
                            )
                        previous_ts = int(table["ts_ms"][table.num_rows - 1].as_py())
                    if writer is None:
                        writer = pq.ParquetWriter(
                            tmp, unified_schema, compression="snappy"
                        )
                    writer.write_table(table)
                    written_rows += table.num_rows
    except (OSError, ValueError, AttributeError, pa.ArrowException) as exc:
        stream_failed = True
        logger.error("compact %s: bounded stream failed (%s) — shards kept", day_dir, exc)
        return None
    finally:
        if writer is not None:
            writer.close()
        if stream_failed or written_rows != expected:
            tmp.unlink(missing_ok=True)
    if writer is None:
        return None
    written = pq.ParquetFile(tmp).metadata.num_rows
    if written != int(expected) or written_rows != int(expected):
        tmp.unlink(missing_ok=True)
        logger.error("compact %s: row-count mismatch (wrote %d, expected %d) — shards kept",
                     day_dir, written, expected)
        return None
    os.replace(tmp, out)  # atomic publish; readers never see a partial file
    for p in inputs:
        if p != out:
            p.unlink(missing_ok=True)
    logger.info("compacted %s: %d shards -> 1 file, %d rows", day_dir, len(inputs), expected)
    return {"day": day, "shards": len(inputs), "rows": expected, "dry_run": False}


# --- 2 + 3. full sweep -----------------------------------------------------------

def run_maintenance(
    data_root: Path | str,
    *,
    retention_days: Mapping[str, int] | None = None,
    halt_pct: float | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
    usage_pct: Callable[[Path], float] | None = None,
) -> dict:
    """One maintenance sweep: retention -> compaction -> disk-pressure guard.

    Returns a report dict (also consumed by the CLI printer). With
    ``dry_run=True`` nothing on disk is created, modified, or deleted — the
    report shows what WOULD happen.
    """
    root = Path(data_root)
    ticks_root = root / "ticks"
    now = now or datetime.now(UTC)
    today = now.strftime("%Y%m%d")
    retention = dict(retention_days) if retention_days is not None \
        else retention_days_from_env()
    halt = halt_pct if halt_pct is not None \
        else _env_float(DISK_USAGE_HALT_PCT_ENV, DEFAULT_DISK_USAGE_HALT_PCT)
    usage_fn = usage_pct or _disk_usage_pct

    streams: dict[str, dict] = {}

    def _stream(stream: str) -> dict:
        return streams.setdefault(stream, {
            "bytes_before": 0, "bytes_after": 0,
            "days_before": 0, "days_after": 0,
            "days_deleted": 0, "bytes_freed": 0,
            "days_compacted": 0, "shards_merged": 0,
        })

    for _ex, stream, stream_dir in _iter_stream_dirs(ticks_root):
        rec = _stream(stream)
        for _day, path in _day_entries(stream_dir):
            rec["days_before"] += 1
            rec["bytes_before"] += _tree_bytes(path)

    excluded_bytes = sum(
        _tree_bytes(stream_dir)
        for ex, _stream, stream_dir in _iter_stream_dirs(ticks_root, include_excluded=True)
        if _is_excluded_exchange(ex)
    )
    usage_before = usage_fn(root)

    # -- retention ------------------------------------------------------------
    retention_deleted: list[dict] = []
    retention_victims: set[Path] = set()  # so a dry run doesn't also "compact" them
    for _ex, stream, stream_dir in _iter_stream_dirs(ticks_root):
        horizon = retention.get(stream)
        if horizon is None:
            continue  # unknown stream type: never aged out implicitly
        cutoff = (now - timedelta(days=horizon)).strftime("%Y%m%d")
        for day, path in _day_entries(stream_dir):
            if day >= cutoff:
                continue
            size = _tree_bytes(path)
            rec = _stream(stream)
            rec["days_deleted"] += 1
            rec["bytes_freed"] += size
            retention_deleted.append({"stream": stream, "day": day,
                                      "path": str(path), "bytes": size})
            retention_victims.add(path)
            if not dry_run:
                _delete_path(path)
                logger.info("retention: deleted %s (%s > %dd old, %d bytes)",
                            path, stream, horizon, size)

    # -- compaction (closed days only; compact_day itself refuses today) -------
    for _ex, stream, stream_dir in _iter_stream_dirs(ticks_root):
        for day, path in _day_entries(stream_dir):
            if not path.is_dir() or day >= today or path in retention_victims:
                continue
            result = compact_day(path, now=now, dry_run=dry_run)
            if result is not None:
                rec = _stream(stream)
                rec["days_compacted"] += 1
                rec["shards_merged"] += result["shards"]

    # -- disk-pressure guard ----------------------------------------------------
    usage_now = usage_fn(root)
    pressure: dict = {"triggered": False, "deleted": [], "still_above": False,
                      "halt_pct": halt, "floor_days": PRESSURE_FLOOR_DAYS}
    if usage_now > halt:
        pressure["triggered"] = True
        logger.critical(
            "DISK PRESSURE: %s at %.1f%% used (halt threshold %.1f%%) — deleting "
            "oldest book days down to a %d-day floor; trades/liquidations/hist "
            "are never touched in pressure mode", root, usage_now, halt,
            PRESSURE_FLOOR_DAYS)
        floor_cutoff = (now - timedelta(days=PRESSURE_FLOOR_DAYS)).strftime("%Y%m%d")
        candidates: list[tuple[str, Path]] = []
        for _ex, stream, stream_dir in _iter_stream_dirs(ticks_root):
            if stream != "book":
                continue  # pressure mode deletes ONLY book data
            candidates.extend((day, path) for day, path in _day_entries(stream_dir)
                              if day < floor_cutoff and path not in retention_victims)
        candidates.sort(key=lambda e: e[0])  # oldest first
        for day, path in candidates:
            size = _tree_bytes(path)
            rec = _stream("book")
            rec["days_deleted"] += 1
            rec["bytes_freed"] += size
            pressure["deleted"].append({"day": day, "path": str(path), "bytes": size})
            if dry_run:
                continue  # deletions can't change usage in a dry run; list them all
            _delete_path(path)
            logger.warning("pressure: deleted book day %s (%d bytes)", path, size)
            usage_now = usage_fn(root)
            if usage_now <= halt:
                break
        # In a dry run deletions never happen, so post-delete usage is unknowable —
        # the unresolved verdict is only meaningful on a real sweep.
        if not dry_run:
            usage_now = usage_fn(root)
            if usage_now > halt:
                pressure["still_above"] = True
                logger.critical(
                    "DISK PRESSURE UNRESOLVED: %.1f%% used, still above %.1f%% after "
                    "trimming book data to the %d-day floor — stopping. Manual "
                    "intervention required; trades/liquidations/hist will NOT be "
                    "deleted automatically.", usage_now, halt, PRESSURE_FLOOR_DAYS)

    # -- after stats ------------------------------------------------------------
    for _ex, stream, stream_dir in _iter_stream_dirs(ticks_root):
        rec = _stream(stream)
        for _day, path in _day_entries(stream_dir):
            rec["days_after"] += 1
            rec["bytes_after"] += _tree_bytes(path)

    return {
        "generated_at": now.isoformat(),
        "data_root": str(root),
        "dry_run": dry_run,
        "retention_days": retention,
        "disk_usage_pct": {"before": usage_before, "after": usage_fn(root)},
        "streams": streams,
        "retention_deleted": retention_deleted,
        "pressure": pressure,
        "excluded_bytes": excluded_bytes,
        "excluded_exchanges": [
            ex for ex in sorted({e for e, _s, _d in
                                 _iter_stream_dirs(ticks_root, include_excluded=True)})
            if _is_excluded_exchange(ex)
        ],
    }


# --- CLI --------------------------------------------------------------------------

def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0 or unit == "TiB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024.0
    return f"{n:.1f}TiB"


def print_report(report: dict) -> None:
    tag = " [DRY-RUN — nothing was deleted]" if report["dry_run"] else ""
    du = report["disk_usage_pct"]
    print(f"tick-lake maintenance report {report['generated_at']}{tag}")
    print(f"  data root: {report['data_root']} | "
          f"disk usage {du['before']:.1f}% -> {du['after']:.1f}% "
          f"(halt {report['pressure']['halt_pct']:.0f}%)")
    header = (f"  {'stream':<14}{'before':>10}{'after':>10}{'days':>6}"
              f"{'compacted':>11}{'shards':>8}{'deleted':>9}{'freed':>10}")
    print(header)
    for stream, s in sorted(report["streams"].items()):
        print(f"  {stream:<14}{_fmt_bytes(s['bytes_before']):>10}"
              f"{_fmt_bytes(s['bytes_after']):>10}{s['days_after']:>6}"
              f"{s['days_compacted']:>11}{s['shards_merged']:>8}"
              f"{s['days_deleted']:>9}{_fmt_bytes(s['bytes_freed']):>10}")
    if report["excluded_exchanges"]:
        print(f"  excluded (never touched): "
              f"{', '.join(report['excluded_exchanges'])} "
              f"({_fmt_bytes(report['excluded_bytes'])})")
    p = report["pressure"]
    if not p["triggered"]:
        print("  pressure guard: not triggered")
    else:
        days = ", ".join(d["day"] for d in p["deleted"]) or "none eligible"
        print(f"  pressure guard: TRIGGERED — book days deleted: {days} "
              f"(floor {p['floor_days']}d)")
        if p["still_above"]:
            print("  pressure guard: STILL ABOVE THRESHOLD — manual intervention required")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(
        description="tick-lake maintenance: compaction + retention + disk guard")
    p.add_argument("--data-root", default="data")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would happen; delete/write nothing")
    p.add_argument("--interval-seconds", type=int,
                   default=_env_int("TICK_MAINTENANCE_INTERVAL_SECONDS", 0),
                   help="loop cadence; <=0 runs a single sweep and exits")
    p.add_argument(
        "--report",
        type=Path,
        default=Path("data/reports/tick_lake_maintenance.json"),
    )
    args = p.parse_args(argv)
    while True:
        started = time.time()
        report = run_maintenance(Path(args.data_root), dry_run=args.dry_run)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        tmp_report = args.report.with_suffix(args.report.suffix + ".tmp")
        tmp_report.write_text(json.dumps(report, default=str), encoding="utf-8")
        os.replace(tmp_report, args.report)
        print_report(report)
        logger.info("tick-lake maintenance sweep done in %.1fs", time.time() - started)
        if args.interval_seconds <= 0:
            break
        time.sleep(args.interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

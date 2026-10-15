import csv
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from .dq_common import (
    dq_arg_parser, open_context, ensure_dq_tables, iter_source_game_chunks,
    build_expected_game, fetch_target_games, game_hash, write_mismatches,
    REPORT_DIR, _selected_entity,
)

COMPARE_COLUMNS = [
    "start_date_time", "end_date_time", "player_user_name", "bet_amount", "valid_bet",
    "payout_amount", "progression_contribution_paid", "round_id", "table_room_id",
    "brand", "platform",
]


def diff_columns(expected: Dict[str, Any], actual: Dict[str, Any]) -> List[str]:
    cols = []
    for c in COMPARE_COLUMNS:
        if str(expected.get(c, "")) != str(actual.get(c, "")):
            cols.append(c)
    return cols


def main() -> int:
    parser = dq_arg_parser("Deep source-vs-target column checker. Designed for 4GB RAM / 1 CPU hosts.")
    parser.add_argument("--max-mismatches", type=int, default=1000)
    parser.add_argument("--csv", default=None, help="Optional mismatch CSV path. Default reports/dq_deep_<brand>_<dates>_<run>.csv")
    args = parser.parse_args()
    entity = _selected_entity(args)
    if entity != "game":
        raise SystemExit("This dq_deep_compare version currently supports --entity game_transaction. Wallet support should be added after wallet source key/date columns are finalized.")
    if args.chunk_size <= 0:
        raise SystemExit("--chunk-size must be greater than zero")

    adapter, config, from_dt, until_dt, src, tgt = open_context(args)
    run_id = f"dqdeep_{config.BRAND_KEY}_game_{args.date_from}_{args.date_to}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = Path(args.csv) if args.csv else REPORT_DIR / f"{run_id}.csv"
    started = time.perf_counter()
    checked = 0
    mismatch_count = 0
    missing_count = 0
    try:
        if not args.no_write_dq_tables:
            ensure_dq_tables(tgt, args.dq_schema)
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "business_key", "mismatch_type", "mismatch_columns",
                "source_hash", "target_hash", "source_sample", "target_sample"
            ])
            writer.writeheader()
            pending_db_rows: List[Dict[str, Any]] = []
            for chunk in iter_source_game_chunks(src, config, from_dt, until_dt, args.chunk_size):
                expected_rows = [build_expected_game(r, config) for r in chunk]
                expected_rows = [e for e in expected_rows if e.get("external_id")]
                targets = fetch_target_games(tgt, config, [e["external_id"] for e in expected_rows])
                for e in expected_rows:
                    checked += 1
                    t = targets.get(e["external_id"])
                    source_hash = game_hash(e)
                    if not t:
                        missing_count += 1
                        mismatch_count += 1
                        mismatch_type = "missing_in_target"
                        mismatch_columns = ["missing_target_row"]
                        target_hash = None
                        target_sample = None
                    else:
                        target_hash = game_hash(t)
                        mismatch_columns = diff_columns(e, t)
                        if not mismatch_columns and source_hash == target_hash:
                            continue
                        mismatch_count += 1
                        mismatch_type = "column_mismatch"
                        target_sample = t
                    out = {
                        "business_key": e.get("external_id"),
                        "mismatch_type": mismatch_type,
                        "mismatch_columns": ",".join(mismatch_columns),
                        "source_hash": source_hash,
                        "target_hash": target_hash or "",
                        "source_sample": str(e),
                        "target_sample": str(target_sample or ""),
                    }
                    writer.writerow(out)
                    pending_db_rows.append({
                        "run_id": run_id,
                        "brand": config.BRAND_KEY,
                        "entity": "game_transaction",
                        "business_key": e.get("external_id"),
                        "mismatch_type": mismatch_type,
                        "mismatch_columns": mismatch_columns,
                        "source_hash": source_hash,
                        "target_hash": target_hash,
                        "source_sample": e,
                        "target_sample": target_sample,
                    })
                    if len(pending_db_rows) >= 500 and not args.no_write_dq_tables:
                        write_mismatches(tgt, args.dq_schema, pending_db_rows)
                        pending_db_rows = []
                    if args.max_mismatches and mismatch_count >= args.max_mismatches:
                        break
                if args.max_mismatches and mismatch_count >= args.max_mismatches:
                    break
                if args.sleep_seconds > 0:
                    time.sleep(args.sleep_seconds)
            if pending_db_rows and not args.no_write_dq_tables:
                write_mismatches(tgt, args.dq_schema, pending_db_rows)
        duration = time.perf_counter() - started
        print("[DQ DEEP COMPARE SUMMARY]")
        print(f"run_id: {run_id}")
        print(f"checked_rows: {checked}")
        print(f"mismatch_rows: {mismatch_count}")
        print(f"missing_in_target: {missing_count}")
        print(f"csv: {csv_path}")
        print(f"duration_seconds: {duration:.3f}")
        print(f"duration_minutes: {duration/60.0:.3f}")
        return 0 if mismatch_count == 0 else 2
    finally:
        src.close(); tgt.close()


if __name__ == "__main__":
    raise SystemExit(main())

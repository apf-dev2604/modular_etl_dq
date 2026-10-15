import argparse
import hashlib
import json
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Sequence

from psycopg2.extras import RealDictCursor, execute_values, Json

from .db import connect, table_ref
from .dates import business_window_bounds, parse_date_arg
from .main import load_brand, _selected_entity

REPORT_DIR = Path(os.getenv("DQ_REPORT_DIR", "reports"))
TOTAL_AMOUNT_TOLERANCE = Decimal(os.getenv("DQ_TOTAL_AMOUNT_TOLERANCE", "0.01"))
ROW_AMOUNT_TOLERANCE = Decimal(os.getenv("DQ_ROW_AMOUNT_TOLERANCE", "0.65"))
DQ_STATUS_PASS = "PASS"
DQ_STATUS_PASS_WITH_ROUNDING_NOTE = "PASS_WITH_ROUNDING_NOTE"
DQ_STATUS_FAIL = "FAIL"
DQ_TRUST_RESULT_BY_STATUS = {
    DQ_STATUS_PASS: "READY_FOR_REPORTING",
    DQ_STATUS_PASS_WITH_ROUNDING_NOTE: "READY_FOR_REPORTING_WITH_NOTE",
    DQ_STATUS_FAIL: "NOT_READY",
}


def dq_trust_result(status: str) -> str:
    return DQ_TRUST_RESULT_BY_STATUS.get(status, "NOT_READY")


def norm_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def norm_decimal(value: Any) -> str:
    if value is None or str(value).strip() == "":
        value = "0"
    try:
        return format(Decimal(str(value).strip()), "f").rstrip("0").rstrip(".") or "0"
    except (InvalidOperation, ValueError):
        return str(value).strip()


def decimal_value(value: Any) -> Decimal:
    if value is None or str(value).strip() == "":
        return Decimal("0")
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return Decimal("0")


def decimal_abs_diff(left: Any, right: Any) -> Decimal:
    return abs(decimal_value(left) - decimal_value(right))


def exceeds_total_amount_tolerance(left: Any, right: Any) -> bool:
    return decimal_abs_diff(left, right) > TOTAL_AMOUNT_TOLERANCE


def norm_ts(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().replace("T", " ").replace("Z", "").replace("+00:00", "")
    if "." in text:
        text = text.split(".", 1)[0]
    return text[:19] if len(text) >= 19 else text


def md5_pipe(values: Sequence[Any]) -> str:
    clean = ["" if v is None else str(v) for v in values]
    return hashlib.md5("|".join(clean).encode("utf-8")).hexdigest()


def configure_session(conn, statement_timeout: str = "30min", lock_timeout: str = "5s", work_mem: str = "16MB") -> None:
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = %s", (statement_timeout,))
        cur.execute("SET lock_timeout = %s", (lock_timeout,))
        cur.execute("SET idle_in_transaction_session_timeout = '5min'")
        cur.execute("SET work_mem = %s", (work_mem,))
    conn.commit()


def quote_schema_sql(schema: str) -> str:
    # Schema names in this package are expected to be simple identifiers such as db_admin.
    # Keep quoting centralized so CREATE SCHEMA is safe if the value is overridden.
    return '"' + str(schema).replace('"', '""') + '"'


def open_context(args):
    adapter, config = load_brand(args.brand)
    from_dt, until_dt = business_window_bounds(
        args.date_from,
        args.date_to,
        config.BUSINESS_WINDOW_START_HOUR,
        config.BUSINESS_TZ,
        aware=getattr(args, "time_pht_aware", False),
    )
    src = connect(config.SOURCE_DB_NAME, env_prefix="SOURCE")
    tgt = connect(config.TARGET_DB_NAME, env_prefix="TARGET")
    configure_session(src, getattr(args, "statement_timeout", "30min"), getattr(args, "lock_timeout", "5s"), getattr(args, "work_mem", "16MB"))
    configure_session(tgt, getattr(args, "statement_timeout", "30min"), getattr(args, "lock_timeout", "5s"), getattr(args, "work_mem", "16MB"))
    return adapter, config, from_dt, until_dt, src, tgt


def ensure_dq_tables(conn, schema: str = "db_admin") -> None:
    ddl = f'''
    CREATE TABLE IF NOT EXISTS {table_ref(schema, "etl_dq_run_summary")} (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        run_id text NOT NULL,
        brand text NOT NULL,
        entity text NOT NULL,
        window_from timestamp NOT NULL,
        window_to timestamp NOT NULL,
        source_rows bigint,
        target_rows bigint,
        source_distinct_keys bigint,
        target_distinct_keys bigint,
        source_duplicate_keys bigint,
        target_duplicate_keys bigint,
        source_total_bet numeric(24,4),
        target_total_bet numeric(24,4),
        source_total_valid_bet numeric(24,4),
        target_total_valid_bet numeric(24,4),
        source_total_payout numeric(24,4),
        target_total_payout numeric(24,4),
        source_total_jackpot_contribution numeric(24,4),
        target_total_jackpot_contribution numeric(24,4),
        missing_in_target bigint,
        extra_in_target bigint,
        hash_mismatch_rows bigint,
        status text NOT NULL,
        trust_result text NULL,
        notes text NULL,
        duration_seconds numeric(18,3),
        duration_minutes numeric(18,3),
        created_at timestamptz DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS {table_ref(schema, "etl_dq_mismatch_detail")} (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        run_id text NOT NULL,
        brand text NOT NULL,
        entity text NOT NULL,
        business_key text NOT NULL,
        mismatch_type text NOT NULL,
        mismatch_columns text[] NULL,
        source_hash text NULL,
        target_hash text NULL,
        source_sample jsonb NULL,
        target_sample jsonb NULL,
        created_at timestamptz DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_etl_dq_summary_window
    ON {table_ref(schema, "etl_dq_run_summary")} (brand, entity, window_from, window_to);
    CREATE INDEX IF NOT EXISTS idx_etl_dq_mismatch_run
    ON {table_ref(schema, "etl_dq_mismatch_detail")} (run_id);
    ALTER TABLE {table_ref(schema, "etl_dq_run_summary")}
    ADD COLUMN IF NOT EXISTS trust_result text NULL;
    CREATE INDEX IF NOT EXISTS idx_etl_dq_mismatch_key
    ON {table_ref(schema, "etl_dq_mismatch_detail")} (business_key);
    '''
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()


def safe_numeric_expr(key: str) -> str:
    return f"CASE WHEN NULLIF(data->>'{key}','') ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN (data->>'{key}')::numeric ELSE 0 END"


def source_game_summary(src_conn, config, from_dt, until_dt) -> Dict[str, Any]:
    sql = f'''
    SELECT
        count(*) AS source_rows,
        count(DISTINCT data->>'TransactionID') AS source_distinct_keys,
        count(*) - count(DISTINCT data->>'TransactionID') AS source_duplicate_keys,
        coalesce(sum({safe_numeric_expr('TotalStakes')}),0) AS source_total_bet,
        coalesce(sum({safe_numeric_expr('TotalStakes')}),0) AS source_total_valid_bet,
        coalesce(sum({safe_numeric_expr('TotalWins')}),0) AS source_total_payout,
        coalesce(sum({safe_numeric_expr('PROGRESSIVE_CONTRIBUTION_PAID')}),0) AS source_total_jackpot_contribution
    FROM {table_ref(config.SOURCE_SCHEMA, config.SOURCE_TABLES["game_transactions"])}
    WHERE game_dt >= %s::timestamp
      AND game_dt < %s::timestamp
      AND data IS NOT NULL
    '''
    with src_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, (from_dt, until_dt))
        row = dict(cur.fetchone() or {})
    src_conn.rollback()
    return row


def target_game_summary(tgt_conn, config, from_dt, until_dt) -> Dict[str, Any]:
    sql = f'''
    SELECT
        count(*) AS target_rows,
        count(DISTINCT externalId) AS target_distinct_keys,
        count(*) - count(DISTINCT externalId) AS target_duplicate_keys,
        coalesce(sum(betAmount),0) AS target_total_bet,
        coalesce(sum(validBet),0) AS target_total_valid_bet,
        coalesce(sum(payoutAmount),0) AS target_total_payout,
        coalesce(sum(progressionContributionPaid),0) AS target_total_jackpot_contribution
    FROM {table_ref(config.TARGET_SCHEMA, config.GAME_TRANSACTION_TABLE)}
    WHERE brand = %s
      AND platform = %s
      AND startDateTime >= %s::timestamp
      AND startDateTime < %s::timestamp
    '''
    with tgt_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, (config.BRAND, config.PLATFORM, from_dt, until_dt))
        row = dict(cur.fetchone() or {})
    tgt_conn.rollback()
    return row


def iter_source_game_chunks(src_conn, config, from_dt, until_dt, chunk_size: int):
    after_dt = None
    after_id = ""
    table = table_ref(config.SOURCE_SCHEMA, config.SOURCE_TABLES["game_transactions"])
    while True:
        if after_dt is None:
            sql = f'''
            SELECT id::text AS source_id, game_dt, data
            FROM {table}
            WHERE game_dt >= %s::timestamp
              AND game_dt < %s::timestamp
              AND game_dt IS NOT NULL
              AND data IS NOT NULL
            ORDER BY game_dt, id
            LIMIT %s
            '''
            params = (from_dt, until_dt, chunk_size)
        else:
            sql = f'''
            SELECT id::text AS source_id, game_dt, data
            FROM {table}
            WHERE game_dt >= %s::timestamp
              AND game_dt < %s::timestamp
              AND (game_dt, id) > (%s::timestamp, %s)
              AND game_dt IS NOT NULL
              AND data IS NOT NULL
            ORDER BY game_dt, id
            LIMIT %s
            '''
            params = (from_dt, until_dt, after_dt, after_id, chunk_size)
        with src_conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
        src_conn.rollback()
        if not rows:
            break
        yield rows
        last = rows[-1]
        after_dt = last["game_dt"]
        after_id = str(last["source_id"])


def build_expected_game(row: Dict[str, Any], config) -> Dict[str, Any]:
    data = row.get("data") or {}
    if isinstance(data, str):
        data = json.loads(data)
    ext = norm_text(data.get("TransactionID") or row.get("source_id"))
    total_stakes = data.get("TotalStakes")
    return {
        "external_id": ext,
        "start_date_time": norm_ts(data.get("GameDate") or row.get("game_dt")),
        "end_date_time": norm_ts(data.get("UpdateDateTime") or data.get("GameDate") or row.get("game_dt")),
        "player_user_name": norm_text(data.get("PlayerAccount")),
        "bet_amount": norm_decimal(total_stakes),
        "valid_bet": norm_decimal(data.get("ValidBet") if data.get("ValidBet") is not None else total_stakes),
        "payout_amount": norm_decimal(data.get("TotalWins")),
        "progression_contribution_paid": norm_decimal(data.get("PROGRESSIVE_CONTRIBUTION_PAID")),
        "round_id": norm_text(data.get("SessionID")),
        "table_room_id": norm_text(data.get("Outlet")),
        "brand": config.BRAND,
        "platform": config.PLATFORM,
    }


GAME_MONETARY_FIELDS = ("bet_amount", "valid_bet", "payout_amount", "progression_contribution_paid")
GAME_IDENTITY_FIELDS = (
    "external_id", "start_date_time", "end_date_time", "player_user_name",
    "round_id", "table_room_id", "brand", "platform",
)


def game_rounding_only(expected: Dict[str, Any], actual: Dict[str, Any]) -> bool:
    """True only when a hash difference is exclusively monetary and within per-field tolerance.

    All identifiers, timestamps, and other nonmonetary values remain strict.
    """
    if any(norm_text(expected.get(k)) != norm_text(actual.get(k)) for k in GAME_IDENTITY_FIELDS):
        return False
    differences = [decimal_abs_diff(expected.get(k), actual.get(k)) for k in GAME_MONETARY_FIELDS]
    return any(x > 0 for x in differences) and all(x <= ROW_AMOUNT_TOLERANCE for x in differences)


def game_hash(d: Dict[str, Any]) -> str:
    return md5_pipe([
        d.get("external_id"), d.get("start_date_time"), d.get("end_date_time"),
        d.get("player_user_name"), d.get("bet_amount"), d.get("valid_bet"),
        d.get("payout_amount"), d.get("progression_contribution_paid"),
        d.get("round_id"), d.get("table_room_id"), d.get("brand"), d.get("platform"),
    ])


def fetch_target_games(tgt_conn, config, external_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    if not external_ids:
        return {}
    sql = f'''
    SELECT externalId AS external_id, startDateTime AS start_date_time, endDateTime AS end_date_time, playerUserName AS player_user_name,
           betAmount AS bet_amount, validBet AS valid_bet, payoutAmount AS payout_amount, progressionContributionPaid AS progression_contribution_paid,
           roundId AS round_id, tableRoomId AS table_room_id, brand, platform
    FROM {table_ref(config.TARGET_SCHEMA, config.GAME_TRANSACTION_TABLE)}
    WHERE externalId = ANY(%s)
    '''
    with tgt_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, (list(external_ids),))
        rows = [dict(r) for r in cur.fetchall()]
    tgt_conn.rollback()
    out = {}
    for r in rows:
        out[norm_text(r.get("external_id"))] = {
            "external_id": norm_text(r.get("external_id")),
            "start_date_time": norm_ts(r.get("start_date_time")),
            "end_date_time": norm_ts(r.get("end_date_time")),
            "player_user_name": norm_text(r.get("player_user_name")),
            "bet_amount": norm_decimal(r.get("bet_amount")),
            "valid_bet": norm_decimal(r.get("valid_bet")),
            "payout_amount": norm_decimal(r.get("payout_amount")),
            "progression_contribution_paid": norm_decimal(r.get("progression_contribution_paid")),
            "round_id": norm_text(r.get("round_id")),
            "table_room_id": norm_text(r.get("table_room_id")),
            "brand": norm_text(r.get("brand")),
            "platform": norm_text(r.get("platform")),
        }
    return out


def write_summary(tgt_conn, schema: str, row: Dict[str, Any]) -> None:
    cols = list(row.keys())
    col_sql = ", ".join(cols)
    sql = f'INSERT INTO {table_ref(schema, "etl_dq_run_summary")} ({col_sql}) VALUES %s'
    with tgt_conn.cursor() as cur:
        execute_values(cur, sql, [tuple(row[c] for c in cols)], page_size=1)
    tgt_conn.commit()


def write_mismatches(tgt_conn, schema: str, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    cols = ["run_id", "brand", "entity", "business_key", "mismatch_type", "mismatch_columns", "source_hash", "target_hash", "source_sample", "target_sample"]
    col_sql = ", ".join(cols)
    sql = f'INSERT INTO {table_ref(schema, "etl_dq_mismatch_detail")} ({col_sql}) VALUES %s'
    values = []
    for r in rows:
        values.append((
            r.get("run_id"), r.get("brand"), r.get("entity"), r.get("business_key"),
            r.get("mismatch_type"), r.get("mismatch_columns"), r.get("source_hash"), r.get("target_hash"),
            Json(r.get("source_sample")) if isinstance(r.get("source_sample"), dict) else r.get("source_sample"),
            Json(r.get("target_sample")) if isinstance(r.get("target_sample"), dict) else r.get("target_sample"),
        ))
    with tgt_conn.cursor() as cur:
        execute_values(cur, sql, values, page_size=500)
    tgt_conn.commit()


def dq_arg_parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--brand", required=True, choices=["inplayv1", "inplay", "instaplay", "1play"])
    p.add_argument("--entity", "--object", default="gameTransaction", choices=["gameTransaction", "GameTransaction", "game", "walletTransaction", "WalletTransaction", "wallet"])
    p.add_argument("--date-from", required=True, type=parse_date_arg)
    p.add_argument("--date-to", required=True, type=parse_date_arg)
    p.add_argument("--time-pht-aware", action="store_true")
    p.add_argument("--chunk-size", type=int, default=int(os.getenv("DQ_CHUNK_SIZE", "500")))
    p.add_argument("--sleep-seconds", type=float, default=float(os.getenv("DQ_SLEEP_SECONDS", "0.05")))
    p.add_argument("--statement-timeout", default=os.getenv("DQ_STATEMENT_TIMEOUT", "30min"))
    p.add_argument("--lock-timeout", default=os.getenv("DQ_LOCK_TIMEOUT", "5s"))
    p.add_argument("--work-mem", default=os.getenv("DQ_WORK_MEM", "16MB"))
    p.add_argument("--dq-schema", default=os.getenv("DQ_SCHEMA", "db_admin"))
    p.add_argument("--no-write-dq-tables", action="store_true")
    return p

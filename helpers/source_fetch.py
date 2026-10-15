import os
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from psycopg2.extras import RealDictCursor

from .db import table_ref
from .reports import trace


SOURCE_FETCH_STATS: Dict[str, Dict[str, Any]] = {}
PRINTED_SOURCE_SQL_LABELS = set()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except Exception:
        return default


def reset_source_fetch_summary(label: str) -> None:
    SOURCE_FETCH_STATS.pop(label, None)


def _record_source_fetch(label: str, table: str, exact_query: str, rows_count: int, limit: int, duration_seconds: float = 0.0) -> None:
    stats = SOURCE_FETCH_STATS.setdefault(label, {
        "table": table,
        "executedSelects": 0,
        "dataBatches": 0,
        "totalRowsFetched": 0,
        "lastRows": 0,
        "limit": limit,
        "firstExecutedQueryForPsql": "",
        "lastExecutedQueryForPsql": "",
        "lastDataQueryForPsql": "",
        "totalDurationSeconds": 0.0,
        "lastDurationSeconds": 0.0,
    })
    stats["table"] = table
    stats["executedSelects"] = int(stats.get("executedSelects", 0) or 0) + 1
    stats["lastRows"] = rows_count
    stats["limit"] = limit
    stats["lastDurationSeconds"] = float(duration_seconds or 0.0)
    stats["totalDurationSeconds"] = float(stats.get("totalDurationSeconds", 0.0) or 0.0) + float(duration_seconds or 0.0)
    if not stats.get("firstExecutedQueryForPsql"):
        stats["firstExecutedQueryForPsql"] = exact_query.strip()
    stats["lastExecutedQueryForPsql"] = exact_query.strip()
    if rows_count > 0:
        stats["dataBatches"] = int(stats.get("dataBatches", 0) or 0) + 1
        stats["totalRowsFetched"] = int(stats.get("totalRowsFetched", 0) or 0) + rows_count
        stats["lastDataQueryForPsql"] = exact_query.strip()


def print_source_fetch_summary(label: str, include_last_query: Optional[bool] = None) -> None:
    stats = SOURCE_FETCH_STATS.get(label)
    if not stats:
        return
    if include_last_query is None:
        include_last_query = _env_bool("MIGRATION_LOG_SOURCE_LAST_QUERY", False)
    trace(f"[SOURCE QUERY SUMMARY][{label}]")
    trace(f"  table               : {stats.get('table', '')}")
    trace(f"  executedSelects     : {stats.get('executedSelects', 0)}")
    trace(f"  dataBatches         : {stats.get('dataBatches', 0)}")
    trace(f"  totalRowsFetched    : {stats.get('totalRowsFetched', 0)}")
    trace(f"  lastRows            : {stats.get('lastRows', 0)}")
    total_seconds = float(stats.get('totalDurationSeconds', 0.0) or 0.0)
    last_seconds = float(stats.get('lastDurationSeconds', 0.0) or 0.0)
    trace(f"  limit               : {stats.get('limit', '')}")
    trace(f"  extractionSeconds   : {total_seconds:.3f}")
    trace(f"  extractionMinutes   : {total_seconds / 60.0:.3f}")
    trace(f"  lastSelectSeconds   : {last_seconds:.3f}")
    if include_last_query:
        first_query = stats.get('firstExecutedQueryForPsql') or stats.get('lastExecutedQueryForPsql') or ''
        if first_query:
            trace("  firstExecutedQueryForPsql:")
            for line in first_query.splitlines():
                trace(f"    {line}")


def parse_checkpoint(raw: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not raw:
        return (None, None)
    if "|" in raw:
        dt, row_id = raw.split("|", 1)
        return (dt or None, row_id or None)
    return (None, raw)


def format_checkpoint(dt_iso: Optional[str], row_id: str) -> str:
    return f"{dt_iso or ''}|{row_id}"


def get_checkpoint(conn, schema: str, table: str, platform: str) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute(
            f'SELECT last_source_id FROM {table_ref(schema, table)} WHERE platform=%s',
            (platform,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def set_checkpoint(conn, schema: str, table: str, platform: str, value: str, dry_run: bool) -> None:
    if dry_run:
        return
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {table_ref(schema, table)} (platform, last_source_id, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (platform) DO UPDATE SET
              last_source_id = EXCLUDED.last_source_id,
              updated_at = now()
            """,
            (platform, value),
        )


def render_source_query(cur, query: str, params: Iterable[Any]) -> str:
    params_list = list(params)
    try:
        return cur.mogrify(query, params_list).decode("utf-8")
    except Exception as exc:
        return f"{query}\n-- PARAMS: {params_list!r}\n-- mogrify failed: {exc}"


def print_source_query(cur, label: str, query: str, params: Iterable[Any]) -> str:
    exact = render_source_query(cur, query, params)
    # Always flash the first extraction SELECT once per phase.
    # This is intentionally one-time, not per batch, so operators can verify
    # source table/date key/window without flooding the terminal.
    if label not in PRINTED_SOURCE_SQL_LABELS:
        PRINTED_SOURCE_SQL_LABELS.add(label)
        trace(f"\n[EXTRACTION SQL][{label}] first_select_for_psql:")
        for line in exact.strip().splitlines():
            trace(f"  {line}")
        trace("")
    elif _env_bool("MIGRATION_LOG_SOURCE_SQL_EVERY_BATCH", False):
        trace(f"\n[EXTRACTION SQL][{label}] batch_select_for_psql:")
        for line in exact.strip().splitlines():
            trace(f"  {line}")
        trace("")
    return exact


def fetch_json_batch(
    conn,
    source_schema: str,
    table: str,
    date_expr: str,
    after_dt: Optional[str],
    after_id: Optional[str],
    limit: int,
    from_dt: Optional[str] = None,
    until_dt: Optional[str] = None,
    extra_conditions: Optional[List[str]] = None,
    extra_params: Optional[List[Any]] = None,
    label: Optional[str] = None,
) -> List[Dict[str, Any]]:
    anchor_id = after_id or ""
    conditions = ["data IS NOT NULL"]
    params: List[Any] = []

    if extra_conditions:
        conditions.extend(extra_conditions)
        params.extend(extra_params or [])

    dt_cast = "timestamptz" if _env_bool("ETL_TIME_PHT_AWARE", False) else "timestamp"

    if after_dt is not None:
        conditions.append(f"({date_expr}, id) > (%s::{dt_cast}, %s)")
        params.extend([after_dt, anchor_id])
    elif from_dt is not None:
        conditions.append(f"{date_expr} >= %s::{dt_cast}")
        params.append(from_dt)

    if until_dt is not None:
        conditions.append(f"{date_expr} < %s::{dt_cast}")
        params.append(until_dt)

    params.append(limit)
    query = f"""
        SELECT id, data
        FROM {table_ref(source_schema, table)}
        WHERE {' AND '.join(conditions)}
        ORDER BY {date_expr} ASC, id ASC
        LIMIT %s
    """
    effective_label = label or f"{table} batch"
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        exact_query = print_source_query(cur, effective_label, query, params)
        started = time.perf_counter()
        cur.execute(query, params)
        rows = cur.fetchall()
        duration_seconds = time.perf_counter() - started
        rows_count = len(rows)
        _record_source_fetch(effective_label, table, exact_query, rows_count, limit, duration_seconds)
        result_every = _env_int("MIGRATION_SOURCE_RESULT_EVERY", 0)
        stats = SOURCE_FETCH_STATS.get(effective_label, {})
        executed = int(stats.get("executedSelects", 0) or 0)
        if _env_bool("MIGRATION_LOG_SOURCE_RESULTS_EVERY_BATCH", False) or (_env_bool("MIGRATION_LOG_SOURCE_RESULTS", False) and result_every > 0 and executed % result_every == 0):
            trace(f"[SOURCE QUERY RESULT][{table}] rows={rows_count} limit={limit} executedSelects={executed}")
    conn.rollback()
    return rows

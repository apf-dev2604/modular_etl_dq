import time
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import asdict, is_dataclass

from psycopg2.extras import execute_values

from .db import table_ref
from .players import to_decimal_str
from .reports import write_phase_report, db_error_parts, trace
from .fact_quality import fact_only_enabled, columns_exist, target_present_count, batch_quality_log, compare_wallet_batch_columns

PRINTED_IMPORT_SQL_KEYS = set()


def _flash_import_sql_once(label: str, sql: str) -> None:
    if label in PRINTED_IMPORT_SQL_KEYS:
        return
    PRINTED_IMPORT_SQL_KEYS.add(label)
    trace(f"\n[IMPORT SQL][{label}] insert_upsert_template:")
    for line in sql.strip().splitlines():
        trace(f"  {line}")
    trace("")


def _as_mapping(value: Any) -> Dict[str, Any]:
    if is_dataclass(value):
        return asdict(value)
    return dict(value or {})


def ensure_wallet_dedupe_index(conn, config, dry_run: bool) -> None:
    if dry_run:
        return
    idx = f"ux_wallet_{config.BRAND_KEY.lower()}_reference"
    sql = f"""
    CREATE UNIQUE INDEX IF NOT EXISTS {idx}
    ON {table_ref(config.TARGET_SCHEMA, config.WALLET_TRANSACTION_TABLE)} (platform, referenceId)
    WHERE referenceId IS NOT NULL
    """
    with conn.cursor() as cur:
        cur.execute(sql)


def _existing_wallet(conn, config, kind: Any, reference_id: Any) -> Dict[str, Any]:
    if not reference_id:
        return {}
    sql = f'''
    SELECT id, createdDatetime, transactionType, platform, referenceId
    FROM {table_ref(config.TARGET_SCHEMA, config.WALLET_TRANSACTION_TABLE)}
    WHERE platform=%s AND transactionType=%s AND referenceId=%s
    LIMIT 1
    '''
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (config.WALLET_PLATFORM, str(kind).lower(), str(reference_id)))
            row = cur.fetchone()
            if not row:
                return {}
            return {
                "id": row[0], "createdDatetime": row[1],
                "transactionType": row[2], "platform": row[3], "referenceId": row[4],
            }
    except Exception:
        return {}


def insert_wallet_transactions(conn, config, rows: List[Dict[str, Any]], dry_run: bool, report_path: Optional[str] = None) -> Dict[str, int]:
    values: List[Tuple[Any, ...]] = []
    meta_rows: List[Dict[str, Any]] = []
    stats = {"insertAttemptRows": 0, "insertedRows": 0, "duplicateSkippedRows": 0, "skippedRows": 0, "missingRequiredRows": 0, "insertErrorRows": 0, "importDurationSeconds": 0.0, "columnDqCheckedRows": 0, "columnVarianceRows": 0, "columnVarianceColumns": 0, "columnVarianceColumnList": ""}
    seen = set()
    fact_only = fact_only_enabled()
    # Target wallet fact table intentionally does not contain duplicate soft-link columns
    # such as playerUserName/sourcePlayerId/sourceMemberId/memberId.
    optional_cols = []
    for raw_row in rows:
        row = _as_mapping(raw_row)
        # Fact-only no-FK rule: require source wallet identity only. player_id can be NULL.
        if not row.get("reference_id"):
            stats["skippedRows"] += 1
            stats["missingRequiredRows"] += 1
            if report_path:
                write_phase_report(report_path, issueType="wallet_missing_required_field", category="missingRequired", sourceTable=row.get("source_table"), sourceUsername=row.get("username"), sourceId=row.get("source_id"), referenceId=row.get("reference_id"), sourceDate=row.get("created"), targetWindowDateColumn="createdDatetime", nexusWindowDateColumn="createdDatetime", transactionType=row.get("kind"), action="skipped", reason="Missing reference_id")
            continue
        if (not fact_only) and (not row.get("player_id")):
            stats["skippedRows"] += 1
            stats["missingRequiredRows"] += 1
            if report_path:
                write_phase_report(report_path, issueType="wallet_missing_required_field", category="missingRequired", sourceTable=row.get("source_table"), sourceUsername=row.get("username"), sourceId=row.get("source_id"), referenceId=row.get("reference_id"), sourceDate=row.get("created"), targetWindowDateColumn="createdDatetime", nexusWindowDateColumn="createdDatetime", transactionType=row.get("kind"), action="skipped", reason="Missing player_id")
            continue
        key = (str(row.get("kind")).lower(), row.get("reference_id"))
        if key in seen:
            stats["skippedRows"] += 1
            stats["duplicateSkippedRows"] += 1
            if report_path:
                write_phase_report(report_path, issueType="duplicate_reference_id_in_batch", category="duplicateSkipped", sourceTable=row.get("source_table"), sourceUsername=row.get("username"), sourceId=row.get("source_id"), referenceId=row.get("reference_id"), sourceDate=row.get("created"), targetWindowDateColumn="createdDatetime", nexusWindowDateColumn="createdDatetime", transactionType=row.get("kind"), action="duplicate_key_ignored", reason="Duplicate wallet referenceId appeared more than once in this source batch")
            continue
        seen.add(key)
        status = str(row.get("status") or "").strip().lower()
        created = row.get("created")
        confirmed_dt = row.get("confirmed") if status == "confirmed" else None
        cancelled_dt = created if status == "cancelled" else None
        failed_dt = created if status == "failed" else None
        # Reporting wallet fact table: no playerId/FK insert and no duplicate soft-link columns.
        base_values = [
            str(row.get("kind")).lower(), config.WALLET_PLATFORM, row.get("payment_gateway") or "",
            row.get("domain") or config.DEFAULT_DOMAIN, to_decimal_str(row.get("amount")), status, None,
            created, confirmed_dt, cancelled_dt, failed_dt, row.get("reference_id"),
        ]
        values.append(tuple(base_values))
        meta_rows.append(row)
    stats["insertAttemptRows"] = len(values)
    cols = [
        'transactionType', 'platform', 'paymentGateway', 'domain', 'amount', 'status',
        'bettingPhase', 'createdDatetime', 'confirmedDatetime', 'cancelledDatetime', 'failedDatetime', 'referenceId'
    ]
    sql = f"""
    INSERT INTO {table_ref(config.TARGET_SCHEMA, config.WALLET_TRANSACTION_TABLE)} (
        {', '.join(cols)}
    ) VALUES %s
    ON CONFLICT (platform, referenceId)
    WHERE (referenceId IS NOT NULL)
    DO NOTHING
    RETURNING transactionType, referenceId
    """
    _flash_import_sql_once(f"{config.BRAND_KEY} walletTransaction", sql)
    if dry_run:
        stats["insertedRows"] = 0
        batch_quality_log("walletTransaction", len(rows), len(meta_rows), len(values), 0, 0, dry_run=True)
        return stats
    if not values:
        batch_quality_log("walletTransaction", len(rows), len(meta_rows), 0, 0, 0)
        return stats
    insert_started = time.perf_counter()
    try:
        with conn.cursor() as cur:
            inserted = execute_values(cur, sql, values, page_size=config.INSERT_PAGE_SIZE, fetch=True) or []
    except Exception as exc:
        stats["importDurationSeconds"] = time.perf_counter() - insert_started
        conn.rollback()
        parts = db_error_parts(exc)
        stats["insertErrorRows"] = len(values)
        if report_path:
            for row in meta_rows:
                write_phase_report(report_path, issueType="wallet_insert_error", category="databaseInsertError", sourceTable=row.get("source_table"), sourceUsername=row.get("username"), sourceId=row.get("source_id"), referenceId=row.get("reference_id"), sourceDate=row.get("created"), targetPlayerId=row.get("player_id"), targetWindowDateColumn="createdDatetime", nexusWindowDateColumn="createdDatetime", transactionType=row.get("kind"), platform=config.WALLET_PLATFORM, action="insert_error", reason="walletTransaction insert failed", **parts)
        batch_quality_log("walletTransaction", len(rows), len(meta_rows), len(values), 0, 0, failed=True, reason=str(exc))
        trace(f"[WALLET INSERT ERROR][{config.BRAND_KEY}] rows={len(values)} error={exc}")
        return stats

    stats["importDurationSeconds"] = time.perf_counter() - insert_started
    inserted_counts: Dict[Tuple[str, str], int] = {}
    for item in inserted:
        key = (str(item[0]).lower(), str(item[1]))
        inserted_counts[key] = inserted_counts.get(key, 0) + 1
    stats["insertedRows"] = len(inserted)
    for row in meta_rows:
        ref = str(row.get("reference_id"))
        kind = str(row.get("kind")).lower()
        key = (kind, ref)
        if inserted_counts.get(key, 0) > 0:
            inserted_counts[key] -= 1
            continue
        stats["duplicateSkippedRows"] += 1
        existing = _existing_wallet(conn, config, kind, ref)
        if report_path:
            write_phase_report(
                report_path,
                issueType="duplicate_reference_id_ignored",
                category="duplicateSkipped",
                sourceTable=row.get("source_table"),
                sourceUsername=row.get("username"),
                sourceId=row.get("source_id"),
                referenceId=ref,
                sourceDate=row.get("created"),
                targetId=existing.get("id"),
                targetPlayerId=existing.get("playerId"),
                targetDate=existing.get("createdDatetime"),
                targetWindowDateColumn="createdDatetime",
                nexusWindowDateColumn="createdDatetime",
                nexusWindowDateColumnValue=existing.get("createdDatetime"),
                nexusExternalId=existing.get("referenceId") or ref,
                nexusReferenceId=existing.get("referenceId") or ref,
                transactionType=kind,
                platform=existing.get("platform") or config.WALLET_PLATFORM,
                action="duplicate_key_ignored",
                reason="Duplicate wallet platform/referenceId; ON CONFLICT DO NOTHING ignored this source row.",
            )
    present = target_present_count(conn, config.TARGET_SCHEMA, config.WALLET_TRANSACTION_TABLE, ['platform','referenceId'], [(config.WALLET_PLATFORM, r.get("reference_id")) for r in meta_rows if r.get("reference_id")])
    dq = compare_wallet_batch_columns(conn, config, meta_rows, report_path or "")
    stats["columnDqCheckedRows"] = int(dq.get("checkedRows", 0) or 0)
    stats["columnVarianceRows"] = int(dq.get("varianceRows", 0) or 0)
    stats["columnVarianceColumns"] = int(dq.get("varianceColumns", 0) or 0)
    stats["columnVarianceColumnList"] = ",".join(sorted(dq.get("columns", set()) or []))
    batch_quality_log("walletTransaction", len(rows), len(meta_rows), len(values), stats["insertedRows"], present)
    if stats["columnDqCheckedRows"]:
        trace(f"[COLUMN DQ][walletTransaction] checkedRows={stats['columnDqCheckedRows']} varianceRows={stats['columnVarianceRows']} varianceColumns={stats['columnVarianceColumns']} columns={stats['columnVarianceColumnList'] or 'none'}")
    return stats

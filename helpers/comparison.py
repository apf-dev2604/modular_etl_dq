"""Source-vs-Nexus count comparison reporting.

This module is intentionally read-only. It does not change migration mappings or
insert behavior. Counts are derived from each brand config/adapter so table names,
schemas, source date expressions, and target tables remain brand-specific.
"""

from __future__ import annotations

import os
import csv
import re
import smtplib
import zipfile
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Dict, List, Optional, Tuple

from psycopg2.extras import RealDictCursor

from .db import table_ref
from .reports import short_text, trace, write_csv_row, get_phase_summaries, human_date_label


COMPARISON_FIELDS = [
    "brandKey",
    "brand",
    "phase",
    "sourceDatabase",
    "sourceSchema",
    "sourceTable",
    "sourceDateExpression",
    "sourceWindowDateColumn",
    "sourceCount",
    "nexusDatabase",
    "nexusSchema",
    "nexusTable",
    "nexusDateColumn",
    "nexusWindowDateColumn",
    "nexusFilter",
    "nexusCount",
    "delta",
    "migrationSourceRows",
    "migrationInsertedRows",
    "migrationUpdatedRows",
    "migrationDuplicateSkippedRows",
    "migrationSkippedRows",
    "migrationTotalErrorRows",
    "migrationAccountedRows",
    "migrationAccountingDelta",
    "varianceExplanation",
    "dateFromInclusive",
    "dateToExclusive",
    "status",
    "notes",
]


VARIANCE_DETAIL_FIELDS = [
    "brandKey", "brand", "phase", "sourceDatabase", "sourceSchema", "sourceTable",
    "sourceId", "sourceUsername", "sourcePlayerId", "sourcePlayerAccount", "businessKey", "sourceExternalId", "source_externalid", "sourceDate", "src_date",
    "sourceWindowDateColumn", "nexusWindowDateColumn", "nexusWindowDateColumnValue",
    "nexusDatabase", "nexusSchema", "nexusTable", "targetId", "targetPlayerId",
    "targetUsername", "targetBusinessKey", "nexusExternalId", "nexus_externalId", "nexusReferenceId", "targetDate", "targetTransactionType",
    "targetPlatform", "targetBrand", "reasonCode", "reason", "specificCauseCode",
    "specificCauseSeverity", "insertDisposition", "isTrueMissingFromFinal", "isExistingFinalRow",
    "migrationWindowFromInclusive", "migrationWindowToExclusive", "dateFromInclusive", "dateToExclusive",
]


def comparison_detail_path(comparison_csv_path: str) -> str:
    return comparison_csv_path.replace("_sourceNexusComparison_", "_sourceNexusVarianceDetails_")


def _in_window(value: Any, from_dt: Any, until_dt: Any) -> bool:
    if value is None:
        return False
    try:
        if from_dt is not None and value < from_dt:
            return False
        if until_dt is not None and value >= until_dt:
            return False
        return True
    except Exception:
        return False


def _chunked(values: List[str], size: int = 1000):
    for i in range(0, len(values), size):
        yield values[i:i + size]


def _source_rows_for_variance(src_conn, config, adapter, phase: str, from_dt, until_dt) -> List[Dict[str, Any]]:
    # Return source-side business keys for mismatch investigation.
    # This uses the same brand adapter mapping functions used by migration and does not join databases.
    if config.SOURCE_MODE != "table_batch":
        return []
    source_key = "players" if phase == "players" else phase
    table_name = config.SOURCE_TABLES[source_key]
    date_expr = _source_date_expr(adapter, source_key)
    conditions = ["data IS NOT NULL"]
    params: List[Any] = []
    if from_dt is not None:
        conditions.append(f"{date_expr} >= %s::timestamptz")
        params.append(from_dt)
    if until_dt is not None:
        conditions.append(f"{date_expr} < %s::timestamptz")
        params.append(until_dt)
    sql = f"SELECT id, data FROM {table_ref(config.SOURCE_SCHEMA, table_name)} WHERE {' AND '.join(conditions)}"
    out: List[Dict[str, Any]] = []
    with src_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        for row in cur.fetchall():
            try:
                if phase == "game_transactions":
                    mapped = adapter.map_game_transaction(row)
                    business_key = str((mapped or {}).get("external_id") or "").strip()
                    source_date = (mapped or {}).get("created")
                elif phase in ("deposits", "withdrawals"):
                    mapped = adapter.map_wallet(row, "deposit" if phase == "deposits" else "withdrawal")
                    business_key = str((mapped or {}).get("reference_id") or "").strip()
                    source_date = (mapped or {}).get("created")
                else:
                    mapped = adapter.map_player(row, {})
                    business_key = str((mapped or {}).get("external_id") or (mapped or {}).get("username") or "").strip()
                    source_date = (mapped or {}).get("registration_date")
            except Exception:
                mapped = None
                business_key = ""
                source_date = None
            mapped = mapped or {}
            out.append({
                "sourceId": row.get("id"),
                "sourceUsername": mapped.get("username") or mapped.get("player_account") or mapped.get("playerAccount") or "",
                "sourcePlayerId": mapped.get("external_id") or mapped.get("player_id") or mapped.get("playerId") or "",
                "sourcePlayerAccount": mapped.get("username") or mapped.get("player_account") or mapped.get("playerAccount") or "",
                "businessKey": business_key,
                "sourceDate": source_date,
                "sourceTable": table_name,
            })
    try:
        src_conn.rollback()
    except Exception:
        pass
    return out


def _target_rows_by_key(tgt_conn, config, phase: str, keys: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    keys = [str(k) for k in keys if k is not None and str(k).strip()]
    if not keys:
        return {}
    found: Dict[str, List[Dict[str, Any]]] = {}
    if phase == "game_transactions":
        table_name = config.GAME_TRANSACTION_TABLE
        sql_tmpl = f'''
            SELECT id, NULL::uuid AS player_id, playerUserName AS player_user_name, externalId AS external_id, startDateTime AS start_date_time, brand, platform
            FROM {table_ref(config.TARGET_SCHEMA, table_name)}
            WHERE externalId = ANY(%s)
        '''
    elif phase in ("deposits", "withdrawals"):
        table_name = config.WALLET_TRANSACTION_TABLE
        sql_tmpl = f'''
            SELECT id, NULL::uuid AS player_id, referenceId AS reference_id, createdDatetime AS created_datetime, transactionType AS transaction_type, platform
            FROM {table_ref(config.TARGET_SCHEMA, table_name)}
            WHERE referenceId = ANY(%s)
        '''
    else:
        table_name = config.PLAYER_TABLE
        sql_tmpl = f'''
            SELECT id, "userName", "externalId", "updatedAt", "brandName"
            FROM {table_ref(config.TARGET_SCHEMA, table_name)}
            WHERE externalId = ANY(%s) OR "userName" = ANY(%s)
        '''
    with tgt_conn.cursor() as cur:
        for chunk in _chunked(keys):
            if phase == "players":
                cur.execute(sql_tmpl, (chunk, chunk))
            else:
                cur.execute(sql_tmpl, (chunk,))
            for row in cur.fetchall():
                if phase == "game_transactions":
                    item = {"targetId": row[0], "targetPlayerId": row[1], "targetUsername": row[2], "businessKey": row[3], "targetDate": row[4], "targetBrand": row[5], "targetPlatform": row[6], "targetTransactionType": ""}
                elif phase in ("deposits", "withdrawals"):
                    item = {"targetId": row[0], "targetPlayerId": row[1], "targetUsername": "", "businessKey": row[2], "targetDate": row[3], "targetTransactionType": row[4], "targetPlatform": row[5], "targetBrand": ""}
                else:
                    item = {"targetId": row[0], "targetUsername": row[1], "businessKey": row[2] or row[1], "targetDate": row[3], "targetBrand": row[4], "targetPlayerId": "", "targetTransactionType": "", "targetPlatform": ""}
                found.setdefault(str(item["businessKey"]), []).append(item)
                if phase == "players" and item.get("targetUsername"):
                    found.setdefault(str(item["targetUsername"]), []).append(item)
    return found


def _date_relation(value: Any, from_dt: Any, until_dt: Any) -> str:
    if value is None:
        return "target_date_null"
    try:
        if from_dt is not None and value < from_dt:
            return "target_date_before_migration_window"
        if until_dt is not None and value >= until_dt:
            return "target_date_after_migration_window"
        return "target_date_inside_migration_window"
    except Exception:
        return "target_date_uncomparable"


def _reason_payload(code: str, reason: str, target: Dict[str, Any], *, severity: str = "warning", disposition: str = "not_newly_inserted", true_missing: bool = False, existing: bool = False) -> Tuple[str, str, Dict[str, Any], Dict[str, Any]]:
    return code, reason, target, {
        "specificCauseCode": code,
        "specificCauseSeverity": severity,
        "insertDisposition": disposition,
        "isTrueMissingFromFinal": "true" if true_missing else "false",
        "isExistingFinalRow": "true" if existing else "false",
    }


def _classify_target_match(phase: str, config, source: Dict[str, Any], matches: List[Dict[str, Any]], from_dt, until_dt) -> Tuple[str, str, Dict[str, Any], Dict[str, Any]]:
    """Classify why a source row is not counted as a matching final-table row.

    The output is intentionally specific. Generic outside-window cases are broken
    into before-window / after-window / null-date and whether the expected brand,
    platform, and transaction type matched.
    """
    if not source.get("businessKey"):
        return _reason_payload(
            "source_missing_business_key",
            "Source row has no stable business key/reference for target lookup; migration cannot prove whether it exists in the final table.",
            {}, severity="error", disposition="skipped_or_unverifiable_before_insert", true_missing=False, existing=False,
        )
    if not matches:
        return _reason_payload(
            "not_present_in_final_table",
            "Business key was not found in the configured *_final table. This is a true not-present-in-final condition unless the brand mapping uses a different business key.",
            {}, severity="error", disposition="not_found_in_final_table", true_missing=True, existing=False,
        )

    wanted_type = "deposit" if phase == "deposits" else "withdrawal" if phase == "withdrawals" else ""

    def date_ok(m):
        return _in_window(m.get("targetDate"), from_dt, until_dt)

    # First try to find a fully matching final-table row inside the same window.
    for m in matches:
        if phase in ("deposits", "withdrawals"):
            type_ok = str(m.get("targetTransactionType") or "").strip().lower() == wanted_type
            platform_ok = str(m.get("targetPlatform") or "").strip().lower() == str(config.WALLET_PLATFORM).strip().lower()
            if date_ok(m) and type_ok and platform_ok:
                return _reason_payload(
                    "exists_inside_nexus_window",
                    "Business key exists in Nexus inside the comparison window with expected transactionType/platform.",
                    m, severity="info", disposition="counted_in_final_window", true_missing=False, existing=True,
                )
        elif phase == "game_transactions":
            brand_ok = str(m.get("targetBrand") or "").strip().lower() == str(config.BRAND).strip().lower()
            platform_ok = str(m.get("targetPlatform") or "").strip().lower() == str(config.PLATFORM).strip().lower()
            if date_ok(m) and brand_ok and platform_ok:
                return _reason_payload(
                    "exists_inside_nexus_window",
                    "Business key exists in Nexus inside the comparison window with expected brand/platform.",
                    m, severity="info", disposition="counted_in_final_window", true_missing=False, existing=True,
                )
        else:
            brand_ok = str(m.get("targetBrand") or "").strip().lower() == str(config.BRAND).strip().lower()
            if date_ok(m) and brand_ok:
                return _reason_payload(
                    "exists_inside_nexus_window",
                    "Player exists in Nexus inside the comparison window with expected brand.",
                    m, severity="info", disposition="counted_in_final_window", true_missing=False, existing=True,
                )

    # Pick the most relevant existing final row for diagnostic reporting.
    preferred = None
    if phase in ("deposits", "withdrawals"):
        for m in matches:
            type_ok = str(m.get("targetTransactionType") or "").strip().lower() == wanted_type
            platform_ok = str(m.get("targetPlatform") or "").strip().lower() == str(config.WALLET_PLATFORM).strip().lower()
            if type_ok and platform_ok:
                preferred = m
                break
    elif phase == "game_transactions":
        for m in matches:
            brand_ok = str(m.get("targetBrand") or "").strip().lower() == str(config.BRAND).strip().lower()
            platform_ok = str(m.get("targetPlatform") or "").strip().lower() == str(config.PLATFORM).strip().lower()
            if brand_ok and platform_ok:
                preferred = m
                break
    else:
        for m in matches:
            brand_ok = str(m.get("targetBrand") or "").strip().lower() == str(config.BRAND).strip().lower()
            if brand_ok:
                preferred = m
                break
    m = preferred or matches[0]
    relation = _date_relation(m.get("targetDate"), from_dt, until_dt)

    if phase in ("deposits", "withdrawals"):
        type_ok = str(m.get("targetTransactionType") or "").strip().lower() == wanted_type
        platform_ok = str(m.get("targetPlatform") or "").strip().lower() == str(config.WALLET_PLATFORM).strip().lower()
        if not type_ok:
            return _reason_payload(
                f"final_key_exists_wrong_transaction_type_{relation}",
                f"Business key exists in walletTransaction_final, but transactionType is {m.get('targetTransactionType')!r}; expected {wanted_type!r}. It may also have target date relation: {relation}.",
                m, severity="warning", disposition="exists_in_final_but_not_counted_by_filter", true_missing=False, existing=True,
            )
        if not platform_ok:
            return _reason_payload(
                f"final_key_exists_wrong_platform_{relation}",
                f"Business key exists in walletTransaction_final, but platform is {m.get('targetPlatform')!r}; expected {config.WALLET_PLATFORM!r}. It may also have target date relation: {relation}.",
                m, severity="warning", disposition="exists_in_final_but_not_counted_by_filter", true_missing=False, existing=True,
            )
        if relation != "target_date_inside_migration_window":
            return _reason_payload(
                f"duplicate_reference_id_exists_expected_type_platform_{relation}",
                "Duplicate/business key exists in walletTransaction_final with expected transactionType/platform, but target createdDatetime is outside the same migration window. The migration insert was not a new insert when ON CONFLICT DO NOTHING applied; inspect phase migration_exception CSV for duplicate_key_ignored.",
                m, severity="warning", disposition="exists_in_final_but_not_counted_by_date_window", true_missing=False, existing=True,
            )

    if phase == "game_transactions":
        brand_ok = str(m.get("targetBrand") or "").strip().lower() == str(config.BRAND).strip().lower()
        platform_ok = str(m.get("targetPlatform") or "").strip().lower() == str(config.PLATFORM).strip().lower()
        if not brand_ok:
            return _reason_payload(
                f"final_key_exists_wrong_brand_{relation}",
                f"Business key exists in gameTransaction_final, but brand is {m.get('targetBrand')!r}; expected {config.BRAND!r}. It may also have target date relation: {relation}.",
                m, severity="warning", disposition="exists_in_final_but_not_counted_by_filter", true_missing=False, existing=True,
            )
        if not platform_ok:
            return _reason_payload(
                f"final_key_exists_wrong_platform_{relation}",
                f"Business key exists in gameTransaction_final, but platform is {m.get('targetPlatform')!r}; expected {config.PLATFORM!r}. It may also have target date relation: {relation}.",
                m, severity="warning", disposition="exists_in_final_but_not_counted_by_filter", true_missing=False, existing=True,
            )
        if relation != "target_date_inside_migration_window":
            return _reason_payload(
                f"duplicate_external_id_exists_expected_brand_platform_{relation}",
                "Duplicate/business key exists in gameTransaction_final with expected brand/platform, but target startDateTime is outside the same migration window. The migration insert was not a new insert when ON CONFLICT DO NOTHING applied; inspect phase migration_exception CSV for duplicate_key_ignored.",
                m, severity="warning", disposition="exists_in_final_but_not_counted_by_date_window", true_missing=False, existing=True,
            )

    if phase == "players":
        brand_ok = str(m.get("targetBrand") or "").strip().lower() == str(config.BRAND).strip().lower()
        if not brand_ok:
            return _reason_payload(
                f"player_exists_wrong_brand_{relation}",
                f"Player business key exists in playerDetails_final, but brandName is {m.get('targetBrand')!r}; expected {config.BRAND!r}. It may also have target date relation: {relation}.",
                m, severity="warning", disposition="exists_in_final_but_not_counted_by_filter", true_missing=False, existing=True,
            )
        if relation != "target_date_inside_migration_window":
            return _reason_payload(
                f"player_existing_row_updated_but_updatedAt_{relation}",
                "Player exists in playerDetails_final for this business key, but target updatedAt is outside the comparison window. This usually means it was an existing player row, not a newly inserted player for this window.",
                m, severity="info", disposition="existing_player_not_counted_by_target_date_window", true_missing=False, existing=True,
            )

    return _reason_payload(
        f"final_key_exists_but_not_counted_{relation}",
        "Business key exists in the configured final table, but did not satisfy the target comparison filter. Check targetDate, brand/platform, and type columns.",
        m, severity="warning", disposition="exists_in_final_but_not_counted_by_filter", true_missing=False, existing=True,
    )


def write_variance_detail_report(src_conn, tgt_conn, config, adapter, rows: List[CountRow], comparison_csv_path: str, from_dt, until_dt) -> str:
    # Create row-level reason CSV for phases with variance. This is connection-based, not a cross-db SQL join.
    detail_path = comparison_detail_path(comparison_csv_path)
    for count_row in rows:
        if count_row.phase == "grand_total" or int(count_row.delta or 0) == 0:
            continue
        phase = count_row.phase
        source_rows = _source_rows_for_variance(src_conn, config, adapter, phase, from_dt, until_dt)
        keys = [str(r.get("businessKey")) for r in source_rows if r.get("businessKey")]
        target_map = _target_rows_by_key(tgt_conn, config, phase, keys)
        for source in source_rows:
            key = str(source.get("businessKey") or "")
            reason_code, reason, target, specific = _classify_target_match(phase, config, source, target_map.get(key, []), from_dt, until_dt)
            if reason_code == "exists_inside_nexus_window":
                continue
            write_csv_row(detail_path, VARIANCE_DETAIL_FIELDS, {
                "brandKey": config.BRAND_KEY, "brand": config.BRAND, "phase": phase,
                "sourceDatabase": config.SOURCE_DB_NAME, "sourceSchema": config.SOURCE_SCHEMA,
                "sourceTable": source.get("sourceTable") or count_row.source_table,
                "sourceId": source.get("sourceId"), "sourceUsername": source.get("sourceUsername"),
                "sourcePlayerId": source.get("sourcePlayerId"), "sourcePlayerAccount": source.get("sourcePlayerAccount"),
                "businessKey": key, "sourceExternalId": key, "source_externalid": key, "sourceDate": source.get("sourceDate"), "src_date": source.get("sourceDate"),
                "sourceWindowDateColumn": human_date_label(count_row.source_date_expression),
                "nexusWindowDateColumn": human_date_label(count_row.nexus_date_column),
                "nexusWindowDateColumnValue": target.get("targetDate"),
                "nexusDatabase": config.TARGET_DB_NAME, "nexusSchema": config.TARGET_SCHEMA,
                "nexusTable": count_row.nexus_table, "targetId": target.get("targetId"),
                "targetPlayerId": target.get("targetPlayerId"), "targetUsername": target.get("targetUsername"),
                "targetBusinessKey": target.get("businessKey"), "nexusExternalId": target.get("businessKey"), "nexus_externalId": target.get("businessKey"), "nexusReferenceId": target.get("businessKey"), "targetDate": target.get("targetDate"),
                "targetTransactionType": target.get("targetTransactionType"), "targetPlatform": target.get("targetPlatform"),
                "targetBrand": target.get("targetBrand"), "reasonCode": reason_code, "reason": reason,
                "specificCauseCode": specific.get("specificCauseCode"), "specificCauseSeverity": specific.get("specificCauseSeverity"),
                "insertDisposition": specific.get("insertDisposition"), "isTrueMissingFromFinal": specific.get("isTrueMissingFromFinal"),
                "isExistingFinalRow": specific.get("isExistingFinalRow"),
                "migrationWindowFromInclusive": from_dt or "", "migrationWindowToExclusive": until_dt or "",
                "dateFromInclusive": from_dt or "", "dateToExclusive": until_dt or "",
            })
    return detail_path


@dataclass
class CountRow:
    phase: str
    source_database: str
    source_schema: str
    source_table: str
    source_date_expression: str
    source_count: int
    nexus_database: str
    nexus_schema: str
    nexus_table: str
    nexus_date_column: str
    nexus_filter: str
    nexus_count: int
    date_from: Optional[str]
    date_to: Optional[str]
    notes: str = ""

    @property
    def delta(self) -> int:
        return int(self.source_count or 0) - int(self.nexus_count or 0)

    @property
    def status(self) -> str:
        return "matched" if self.delta == 0 else "mismatch"


def _fetch_one_count(conn, sql: str, params: List[Any]) -> int:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return int(row[0] or 0) if row else 0


def _source_date_expr(adapter, source_key: str) -> str:
    if hasattr(adapter, "source_date_expr_for_table"):
        return adapter.source_date_expr_for_table(source_key, "data")
    return adapter.source_date_expr("data")


def _source_count_json(src_conn, config, adapter, source_key: str, from_dt, until_dt) -> Tuple[int, str, str, str]:
    table_name = config.SOURCE_TABLES[source_key]
    date_expr = _source_date_expr(adapter, source_key)
    conditions = ["data IS NOT NULL"]
    params: List[Any] = []
    if from_dt is not None:
        conditions.append(f"{date_expr} >= %s::timestamptz")
        params.append(from_dt)
    if until_dt is not None:
        conditions.append(f"{date_expr} < %s::timestamptz")
        params.append(until_dt)
    sql = f"SELECT count(1) FROM {table_ref(config.SOURCE_SCHEMA, table_name)} WHERE {' AND '.join(conditions)}"
    return _fetch_one_count(src_conn, sql, params), table_name, human_date_label(date_expr), ""


def _append_flat_window(conditions: List[str], params: List[Any], column_expr: str, from_dt, until_dt) -> None:
    if from_dt is not None:
        conditions.append(f"{column_expr} >= (%s::timestamptz AT TIME ZONE 'Asia/Manila')")
        params.append(from_dt)
    if until_dt is not None:
        conditions.append(f"{column_expr} < (%s::timestamptz AT TIME ZONE 'Asia/Manila')")
        params.append(until_dt)


def _source_count_flat(src_conn, config, phase: str, from_dt, until_dt) -> Tuple[int, str, str, str]:
    params: List[Any] = []
    conditions: List[str] = []
    notes = "flat RDBMS source count"

    if phase == "players":
        pd = table_ref(config.SOURCE_SCHEMA, config.SOURCE_TABLES["players"])
        pr = table_ref(config.SOURCE_SCHEMA, config.SOURCE_TABLES.get("player_registrations", config.SOURCE_TABLES["players"]))
        date_expr = 'COALESCE(pr."REGISTRATION_DATE", pd."START_DATE")'
        conditions = ["1=1"]
        _append_flat_window(conditions, params, date_expr, from_dt, until_dt)
        sql = f'''
            SELECT count(1)
            FROM {pd} pd
            LEFT JOIN {pr} pr ON pr."PLAYER_NAME" = pd."LOGIN_NAME"
            WHERE {' AND '.join(conditions)}
        '''
        return _fetch_one_count(src_conn, sql, params), config.SOURCE_TABLES["players"], human_date_label(date_expr), notes

    if phase == "game_transactions":
        table_name = config.SOURCE_TABLES["game_transactions"]
        date_expr = '"GAME_DATE"'
        conditions = ["1=1"]
        _append_flat_window(conditions, params, date_expr, from_dt, until_dt)
        sql = f"SELECT count(1) FROM {table_ref(config.SOURCE_SCHEMA, table_name)} WHERE {' AND '.join(conditions)}"
        return _fetch_one_count(src_conn, sql, params), table_name, human_date_label(date_expr), notes

    table_name = config.SOURCE_TABLES["deposits" if phase == "deposits" else "withdrawals"]
    date_expr = '"TRANSACTION_DATE"'
    wanted = "deposit" if phase == "deposits" else "withdrawal"
    conditions = ["LOWER(TRIM(COALESCE(\"TRANSACTION_TYPE\"::text, ''))) = %s"]
    params.append(wanted)
    _append_flat_window(conditions, params, date_expr, from_dt, until_dt)
    sql = f"SELECT count(1) FROM {table_ref(config.SOURCE_SCHEMA, table_name)} WHERE {' AND '.join(conditions)}"
    notes = f"flat wallet count filtered by TRANSACTION_TYPE={wanted}"
    return _fetch_one_count(src_conn, sql, params), table_name, human_date_label(date_expr), notes


def _target_count(tgt_conn, config, phase: str, from_dt, until_dt) -> Tuple[int, str, str, str]:
    params: List[Any] = []
    conditions: List[str] = []

    if phase == "players":
        table_name = config.PLAYER_TABLE
        date_col = '"updatedAt"'
        conditions = ['LOWER(TRIM(COALESCE("brandName", \'\'))) = LOWER(TRIM(%s))']
        params.append(config.BRAND)
    elif phase == "game_transactions":
        table_name = config.GAME_TRANSACTION_TABLE
        date_col = 'startDateTime'
        conditions = ['LOWER(TRIM(COALESCE("brand", \'\'))) = LOWER(TRIM(%s))']
        params.append(config.BRAND)
        conditions.append('LOWER(TRIM(COALESCE("platform", \'\'))) = LOWER(TRIM(%s))')
        params.append(config.PLATFORM)
    else:
        table_name = config.WALLET_TRANSACTION_TABLE
        date_col = 'createdDatetime'
        tx_type = "deposit" if phase == "deposits" else "withdrawal"
        conditions = ['LOWER(TRIM(COALESCE("platform", \'\'))) = LOWER(TRIM(%s))']
        params.append(config.WALLET_PLATFORM)
        conditions.append('LOWER(TRIM(COALESCE("transactionType", \'\'))) = %s')
        params.append(tx_type)

    if from_dt is not None:
        conditions.append(f"{date_col}::timestamptz >= %s::timestamptz")
        params.append(from_dt)
    if until_dt is not None:
        conditions.append(f"{date_col}::timestamptz < %s::timestamptz")
        params.append(until_dt)

    sql = f"SELECT count(1) FROM {table_ref(config.TARGET_SCHEMA, table_name)} WHERE {' AND '.join(conditions)}"
    filter_text = f"same migration window using target date column {human_date_label(date_col)}"
    if phase in ("deposits", "withdrawals"):
        filter_text += f"; transactionType={tx_type}; platform={config.WALLET_PLATFORM}"
    elif phase == "game_transactions":
        filter_text += f"; brand={config.BRAND}; platform={config.PLATFORM}"
    elif phase == "players":
        filter_text += f"; brandName={config.BRAND}"
    return _fetch_one_count(tgt_conn, sql, params), table_name, human_date_label(date_col), filter_text


def _phase_stats_for_row(row: CountRow, phase_summaries: Optional[Dict[str, Dict[str, Any]]]) -> Dict[str, Any]:
    if not phase_summaries or row.phase == "grand_total":
        return {}
    return dict(phase_summaries.get(row.phase) or {})


def _int_stat(stats: Dict[str, Any], key: str) -> int:
    try:
        return int(stats.get(key, 0) or 0)
    except Exception:
        return 0


def _variance_explanation(row: CountRow, stats: Dict[str, Any]) -> str:
    if row.phase == "grand_total":
        return "sum of per-phase sourceCount - nexusCount"
    if row.delta == 0:
        return "matched"
    duplicate_skipped = _int_stat(stats, "duplicateSkippedRows")
    inserted = _int_stat(stats, "insertedRows")
    updated = _int_stat(stats, "updatedRows")
    skipped = _int_stat(stats, "skippedRows")
    errors = _int_stat(stats, "totalErrorRows")
    if row.phase == "players" and updated and row.nexus_count == 0:
        return "players were updated as existing rows; target count uses updatedAt/source-window and may not count existing player rows in this business window"
    if row.phase in ("deposits", "withdrawals"):
        if duplicate_skipped:
            return "variance is likely duplicate referenceIds already present in target outside the target date/type/window filter; inspect phase CSV rows with duplicate_reference_id_ignored and targetDate"
        if inserted and row.nexus_count < inserted:
            return "migration reports inserted wallet rows but target window count is lower; likely target createdDatetime/transactionType/platform filter mismatch or date mapping issue; inspect phase CSV targetDate/sourceDate"
    if row.phase == "game_transactions" and duplicate_skipped:
        return "variance is likely duplicate externalIds already present in target outside the target date/window filter; inspect phase CSV duplicate rows"
    if skipped or errors:
        return "variance partly accounted by skipped/error rows in phase report"
    return "unaccounted variance; compare phase CSV sourceDate/targetDate and target filters"


def _write_count_row(path: str, config, row: CountRow, phase_summaries: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
    stats = _phase_stats_for_row(row, phase_summaries)
    migration_source = _int_stat(stats, "sourceRows")
    migration_inserted = _int_stat(stats, "insertedRows")
    migration_updated = _int_stat(stats, "updatedRows")
    migration_duplicate = _int_stat(stats, "duplicateSkippedRows")
    migration_skipped = _int_stat(stats, "skippedRows")
    migration_errors = _int_stat(stats, "totalErrorRows")
    migration_accounted = migration_inserted + migration_updated + migration_duplicate + migration_skipped + migration_errors
    migration_accounting_delta = (migration_source - migration_accounted) if stats else ""
    write_csv_row(path, COMPARISON_FIELDS, {
        "brandKey": config.BRAND_KEY,
        "brand": config.BRAND,
        "phase": row.phase,
        "sourceDatabase": row.source_database,
        "sourceSchema": row.source_schema,
        "sourceTable": row.source_table,
        "sourceDateExpression": human_date_label(row.source_date_expression),
        "sourceWindowDateColumn": human_date_label(row.source_date_expression),
        "sourceCount": row.source_count,
        "nexusDatabase": row.nexus_database,
        "nexusSchema": row.nexus_schema,
        "nexusTable": row.nexus_table,
        "nexusDateColumn": human_date_label(row.nexus_date_column),
        "nexusWindowDateColumn": human_date_label(row.nexus_date_column),
        "nexusFilter": short_text(row.nexus_filter, 1000),
        "nexusCount": row.nexus_count,
        "delta": row.delta,
        "migrationSourceRows": migration_source if stats else "",
        "migrationInsertedRows": migration_inserted if stats else "",
        "migrationUpdatedRows": migration_updated if stats else "",
        "migrationDuplicateSkippedRows": migration_duplicate if stats else "",
        "migrationSkippedRows": migration_skipped if stats else "",
        "migrationTotalErrorRows": migration_errors if stats else "",
        "migrationAccountedRows": migration_accounted if stats else "",
        "migrationAccountingDelta": migration_accounting_delta,
        "varianceExplanation": short_text(_variance_explanation(row, stats), 1000),
        "dateFromInclusive": row.date_from or "",
        "dateToExclusive": row.date_to or "",
        "status": row.status,
        "notes": row.notes,
    })


def comparison_has_variance(rows: List[CountRow]) -> bool:
    return any((r.phase != "grand_total" and int(r.delta or 0) != 0) for r in rows)


def _print_comparison(rows: List[CountRow], config, path: str, only_if_variance: bool = False) -> None:
    total_source = sum(r.source_count for r in rows if r.phase != "grand_total")
    total_nexus = sum(r.nexus_count for r in rows if r.phase != "grand_total")
    total_delta = total_source - total_nexus
    if only_if_variance and total_delta == 0 and not comparison_has_variance(rows):
        trace(f"[SOURCE VS NEXUS SUMMARY][{config.BRAND_KEY}] completed without variance")
        return
    trace(f"[SOURCE VS NEXUS SUMMARY][{config.BRAND_KEY}]")
    trace(f"  sourceTotal       : {total_source}")
    trace(f"  nexusTotal        : {total_nexus}")
    trace(f"  delta             : {total_delta}")
    trace(f"  comparisonCsv     : {path}")
    trace("  byPhase:")
    for r in rows:
        if r.phase == "grand_total":
            continue
        trace(f"    {r.phase}:")
        trace(f"      sourceCount   : {r.source_count}")
        trace(f"      nexusCount    : {r.nexus_count}")
        trace(f"      delta         : {r.delta}")
        trace(f"      status        : {r.status}")
        trace(f"      sourceTable   : {r.source_table}")
        stats = _phase_stats_for_row(r, get_phase_summaries())
        if r.delta != 0:
            trace(f"      migrationRows : source={_int_stat(stats, 'sourceRows')} inserted={_int_stat(stats, 'insertedRows')} updated={_int_stat(stats, 'updatedRows')} duplicateSkipped={_int_stat(stats, 'duplicateSkippedRows')} skipped={_int_stat(stats, 'skippedRows')} errors={_int_stat(stats, 'totalErrorRows')}")
            trace(f"      explanation   : {_variance_explanation(r, stats)}")
        trace(f"      nexusTable    : {r.nexus_table}")


def run_source_nexus_comparison(src_conn, tgt_conn, adapter, config, comparison_csv_path: str, from_dt, until_dt, quiet_if_matched: bool = False, phase_summaries: Optional[Dict[str, Dict[str, Any]]] = None, phases: Optional[List[str]] = None):
    """Write source-vs-target count CSV and print vertical summary.

    Returns (comparison_csv_path, has_variance, rows).
    """
    rows: List[CountRow] = []
    if phases is None:
        # Default comparison is fact-only. Player comparison must be requested by the caller.
        phases = ["game_transactions", "deposits", "withdrawals"]
    else:
        phases = list(phases)

    for phase in phases:
        if config.SOURCE_MODE == "flat_table_batch":
            source_count, source_table, source_expr, notes = _source_count_flat(src_conn, config, phase, from_dt, until_dt)
        else:
            source_key = "players" if phase == "players" else phase
            source_count, source_table, source_expr, notes = _source_count_json(src_conn, config, adapter, source_key, from_dt, until_dt)
        nexus_count, nexus_table, nexus_date_col, nexus_filter = _target_count(tgt_conn, config, phase, from_dt, until_dt)
        rows.append(CountRow(
            phase=phase,
            source_database=config.SOURCE_DB_NAME,
            source_schema=config.SOURCE_SCHEMA,
            source_table=source_table,
            source_date_expression=source_expr,
            source_count=source_count,
            nexus_database=config.TARGET_DB_NAME,
            nexus_schema=config.TARGET_SCHEMA,
            nexus_table=nexus_table,
            nexus_date_column=nexus_date_col,
            nexus_filter=nexus_filter,
            nexus_count=nexus_count,
            date_from=from_dt,
            date_to=until_dt,
            notes=notes,
        ))

    total_source = sum(r.source_count for r in rows)
    total_nexus = sum(r.nexus_count for r in rows)
    rows.append(CountRow(
        phase="grand_total",
        source_database=config.SOURCE_DB_NAME,
        source_schema=config.SOURCE_SCHEMA,
        source_table="all_configured_source_tables",
        source_date_expression="per-phase source window date column",
        source_count=total_source,
        nexus_database=config.TARGET_DB_NAME,
        nexus_schema=config.TARGET_SCHEMA,
        nexus_table="all_configured_nexus_tables",
        nexus_date_column="per-phase Nexus window date column",
        nexus_filter="per-phase brand/platform/type filters",
        nexus_count=total_nexus,
        date_from=from_dt,
        date_to=until_dt,
        notes="sourceCount - nexusCount = delta",
    ))

    if phase_summaries is None:
        phase_summaries = get_phase_summaries()
    for row in rows:
        _write_count_row(comparison_csv_path, config, row, phase_summaries)
    has_variance = comparison_has_variance(rows)
    if has_variance:
        detail_path = write_variance_detail_report(src_conn, tgt_conn, config, adapter, rows, comparison_csv_path, from_dt, until_dt)
        trace(f"[SOURCE VS NEXUS VARIANCE DETAILS][{config.BRAND_KEY}] csv={detail_path}")
        trace(f"[SOURCE VS NEXUS WINDOW][{config.BRAND_KEY}] from={from_dt} to={until_dt} note=same_effective_window_as_migration")
        _print_variance_reason_summary(detail_path, config)
    _print_comparison(rows, config, comparison_csv_path, only_if_variance=quiet_if_matched)
    return comparison_csv_path, has_variance, rows




def _read_variance_reason_counts(detail_path: str) -> Dict[str, Dict[str, Dict[str, int]]]:
    counts: Dict[str, Dict[str, Dict[str, int]]] = {}
    if not os.path.isfile(detail_path):
        return counts
    try:
        with open(detail_path, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                phase = (row.get("phase") or "unknown").strip() or "unknown"
                phase_counts = counts.setdefault(phase, {"reasonCode": {}, "specificCauseCode": {}, "specificCauseSeverity": {}, "insertDisposition": {}})
                for field in phase_counts:
                    value = (row.get(field) or "unknown").strip() or "unknown"
                    phase_counts[field][value] = phase_counts[field].get(value, 0) + 1
    except Exception as exc:
        trace(f"[SOURCE VS NEXUS VARIANCE DETAILS][WARN] unable to summarize detail csv={detail_path} error={exc}")
    return counts


def _print_variance_reason_summary(detail_path: str, config) -> None:
    counts = _read_variance_reason_counts(detail_path)
    if not counts:
        trace(f"[SOURCE VS NEXUS VARIANCE REASONS][{config.BRAND_KEY}] no row-level variance details found csv={detail_path}")
        return
    trace(f"[SOURCE VS NEXUS VARIANCE REASONS][{config.BRAND_KEY}]")
    trace(f"  varianceDetailsCsv : {detail_path}")
    trace("  note               : The same migration window is used for source and Nexus comparison; only the source/target date columns differ by phase.")
    trace("  meaning            : not_present_in_final_table is true missing from *_final. Duplicate/existing-key causes are broken down by target date, type, platform, and brand.")
    for phase in sorted(counts):
        phase_counts = counts[phase]
        trace(f"  {phase}:")
        reason_counts = phase_counts.get("reasonCode", {})
        specific_counts = phase_counts.get("specificCauseCode", {})
        disposition_counts = phase_counts.get("insertDisposition", {})
        not_present = reason_counts.get("not_present_in_final_table", 0) + specific_counts.get("not_present_in_final_table", 0)
        if not_present:
            trace(f"    trueNotPresentInFinalRows: {not_present}")
        trace("    byInsertDisposition:")
        for value, count in sorted(disposition_counts.items(), key=lambda kv: (-kv[1], kv[0])):
            trace(f"      {value}: {count}")
        trace("    bySpecificCause:")
        for value, count in sorted(specific_counts.items(), key=lambda kv: (-kv[1], kv[0])):
            trace(f"      {value}: {count}")
        trace("    byReasonCode:")
        for value, count in sorted(reason_counts.items(), key=lambda kv: (-kv[1], kv[0])):
            trace(f"      {value}: {count}")


def _variance_reason_body(detail_path: str, config) -> str:
    counts = _read_variance_reason_counts(detail_path)
    if not counts:
        return "No row-level variance reason details were generated. Check the local comparison CSV files."
    lines = [f"Source vs Nexus variance reasons for {config.BRAND_KEY} / {config.BRAND}:", ""]
    lines.append(f"Variance details CSV: {detail_path}")
    lines.append("")
    lines.append("The same migration window is used for source and Nexus comparison. Only the configured source and target date columns differ by phase.")
    lines.append("not_present_in_final_table = true missing final-table row.")
    lines.append("Duplicate/existing-key conditions are broken down by target date relation, type, platform, and brand.")
    lines.append("")
    for phase in sorted(counts):
        phase_counts = counts[phase]
        lines.append(f"{phase}:")
        for label, key in (("Insert disposition", "insertDisposition"), ("Specific cause", "specificCauseCode"), ("Reason code", "reasonCode")):
            lines.append(f"  {label}:")
            for value, count in sorted(phase_counts.get(key, {}).items(), key=lambda kv: (-kv[1], kv[0])):
                lines.append(f"    - {value}: {count}")
        lines.append("")
    return "\n".join(lines)


def _run_token_from_path(path: str) -> str:
    m = re.search(r"rundate_[0-9]+", os.path.basename(path))
    return m.group(0) if m else ""


def _collect_report_csv_files(path: str, config) -> List[str]:
    report_dir = os.path.dirname(path) or "."
    token = _run_token_from_path(path)
    brand_key = str(getattr(config, "BRAND_KEY", "") or "").lower()
    files: List[str] = []
    if os.path.isfile(path):
        files.append(path)
    detail_path = comparison_detail_path(path)
    if os.path.isfile(detail_path) and detail_path not in files:
        files.append(detail_path)
    try:
        for name in os.listdir(report_dir):
            if not name.lower().endswith(".csv"):
                continue
            full = os.path.join(report_dir, name)
            lname = name.lower()
            if token and token not in name:
                continue
            if brand_key and brand_key not in lname:
                continue
            if os.path.isfile(full) and full not in files:
                files.append(full)
    except Exception as exc:
        trace(f"[COMPARISON EMAIL][WARN] unable to scan report dir={report_dir} error={exc}")
    return sorted(files)


def _attachment_limit_bytes() -> int:
    raw = os.getenv("MIGRATION_EMAIL_MAX_ATTACHMENT_BYTES") or os.getenv("MIGRATION_EMAIL_MAX_BYTES") or "17825792"
    try:
        value = int(raw)
        return value if value > 0 else 17825792
    except Exception:
        return 17825792


def _prepare_email_attachments(csv_files: List[str], config, comparison_path: str) -> Tuple[List[Tuple[str, bytes, str, str]], bool, Optional[str], int]:
    """Return attachments, skipped flag, zip path, total raw bytes."""
    limit = _attachment_limit_bytes()
    existing = [p for p in csv_files if os.path.isfile(p)]
    total_raw = sum(os.path.getsize(p) for p in existing)
    if total_raw <= limit:
        attachments: List[Tuple[str, bytes, str, str]] = []
        for p in existing:
            with open(p, "rb") as handle:
                attachments.append((os.path.basename(p), handle.read(), "text", "csv"))
        return attachments, False, None, total_raw

    report_dir = os.path.dirname(comparison_path) or "."
    zip_name = f"{getattr(config, 'BRAND_KEY', 'brand')}_comparison_reports_{_run_token_from_path(comparison_path) or 'run'}.zip"
    zip_path = os.path.join(report_dir, zip_name)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in existing:
            zf.write(p, arcname=os.path.basename(p))
    zip_size = os.path.getsize(zip_path)
    if zip_size > limit:
        return [], True, zip_path, total_raw
    with open(zip_path, "rb") as handle:
        return [(os.path.basename(zip_path), handle.read(), "application", "zip")], False, zip_path, total_raw

def email_comparison_report(path: str, config, to_emails: Optional[str] = None, variance_only: bool = False, has_variance: bool = True) -> bool:
    """Email comparison/report CSVs when variance exists.

    Behavior:
    - If variance_only=True and no variance, skip email and log only.
    - If variance exists, include reason summary in body.
    - Attach all run CSV files for this brand/rundate.
    - If CSV bundle is over MIGRATION_EMAIL_MAX_ATTACHMENT_BYTES (default 17 MiB), zip it.
    - If zipped bundle is still over the limit, do not email; log/display local CSV location.
    """
    if variance_only and not has_variance:
        trace(f"[COMPARISON EMAIL][SKIP] completed without variance; no email sent. csv={path}")
        return False

    detail_path = comparison_detail_path(path)
    reason_body = _variance_reason_body(detail_path, config) if has_variance else "Completed without variance."
    if has_variance:
        _print_variance_reason_summary(detail_path, config)

    to_raw = to_emails or os.getenv("MIGRATION_REPORT_TO") or os.getenv("COMPARISON_REPORT_TO") or ""
    recipients = [x.strip() for x in to_raw.split(",") if x.strip()]
    if not recipients:
        trace("[COMPARISON EMAIL][SKIP] No recipients configured. Set MIGRATION_REPORT_TO or COMPARISON_REPORT_TO.")
        return False

    smtp_host = os.getenv("SMTP_HOST", "")
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    sender = os.getenv("SMTP_FROM", smtp_user)
    if not smtp_host or not smtp_user or not smtp_password or not sender:
        trace("[COMPARISON EMAIL][SKIP] SMTP env is incomplete. Required: SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SMTP_FROM or SMTP_USER.")
        return False

    csv_files = _collect_report_csv_files(path, config)
    attachments, too_big, zip_path, total_raw = _prepare_email_attachments(csv_files, config, path)
    limit = _attachment_limit_bytes()
    if too_big:
        trace(f"[COMPARISON EMAIL][SKIP] unable to send. File too big after zip. rawBytes={total_raw} limitBytes={limit} zip={zip_path}")
        trace(f"[COMPARISON EMAIL][LOCAL CSV] comparisonCsv={path}")
        if os.path.isfile(detail_path):
            trace(f"[COMPARISON EMAIL][LOCAL CSV] varianceDetailsCsv={detail_path}")
        trace(f"[COMPARISON EMAIL][LOCAL CSV] reportDir={os.path.dirname(path) or '.'}")
        return False

    msg = EmailMessage()
    msg["Subject"] = f"[{config.BRAND_KEY}] Source vs Nexus VARIANCE report" if has_variance else f"[{config.BRAND_KEY}] Source vs Nexus comparison report"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    attach_names = ", ".join(name for name, _, _, _ in attachments) or "none"
    msg.set_content(
        f"Source vs Nexus comparison report for {config.BRAND_KEY} / {config.BRAND}.\n\n"
        f"{reason_body}\n\n"
        f"Comparison CSV: {path}\n"
        f"Variance details CSV: {detail_path if os.path.isfile(detail_path) else 'not generated'}\n"
        f"Report CSV files included: {len(csv_files)}\n"
        f"Raw CSV bytes: {total_raw}\n"
        f"Attachment limit bytes: {limit}\n"
        f"Attachment mode: {'zip' if zip_path and attachments else 'csv'}\n"
        f"Attachments: {attach_names}\n"
    )
    for filename, payload, maintype, subtype in attachments:
        msg.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(msg)
    trace(f"[COMPARISON EMAIL][SENT] to={','.join(recipients)} attachments={len(attachments)} files={attach_names}")
    return True

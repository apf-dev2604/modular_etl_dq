import uuid
import time
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import asdict, is_dataclass

from psycopg2.extras import execute_values

from .db import table_ref
from .players import to_decimal_str
from .reports import write_phase_report, db_error_parts, trace
from .fact_quality import fact_only_enabled, columns_exist, target_present_count, batch_quality_log, compare_game_batch_columns

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


def normalize_game_type(raw: Optional[str]) -> str:
    if not raw:
        return "Slots"
    value = str(raw).strip().upper()
    if value in ("SLOTS", "SLOT"):
        return "Slots"
    if value in ("LIVE", "LIVE_CASINO", "CASINO"):
        return "Live"
    if value in ("SPORTS", "SPORT"):
        return "Sports"
    return str(raw).strip().title()


def get_or_create_game_provider(conn, config, provider_name: str, cache: Dict[str, uuid.UUID], dry_run: bool) -> uuid.UUID:
    name = (provider_name or "UNKNOWN").strip().upper() or "UNKNOWN"
    if name in cache:
        return cache[name]
    if dry_run:
        with conn.cursor() as cur:
            cur.execute(f'SELECT id FROM {table_ref(config.TARGET_SCHEMA, config.GAME_PROVIDER_TABLE)} WHERE "gameProvider"=%s', (name,))
            row = cur.fetchone()
        gid = row[0] if row else uuid.uuid4()
        cache[name] = gid
        return gid
    sql = f"""
    INSERT INTO {table_ref(config.TARGET_SCHEMA, config.GAME_PROVIDER_TABLE)} ("gameProvider", "isActive", "createdAt", "updatedAt")
    VALUES (%s, true, now(), now())
    ON CONFLICT ("gameProvider") DO UPDATE SET "updatedAt"=now()
    RETURNING id
    """
    with conn.cursor() as cur:
        cur.execute(sql, (name,))
        gid = cur.fetchone()[0]
    cache[name] = gid
    return gid


def get_or_create_game_type(conn, config, game_type: str, cache: Dict[str, uuid.UUID], dry_run: bool) -> uuid.UUID:
    value = normalize_game_type(game_type)
    if value in cache:
        return cache[value]
    if dry_run:
        with conn.cursor() as cur:
            cur.execute(f'SELECT id FROM {table_ref(config.TARGET_SCHEMA, config.GAME_TYPE_TABLE)} WHERE "gameType"=%s', (value,))
            row = cur.fetchone()
        gid = row[0] if row else uuid.uuid4()
        cache[value] = gid
        return gid
    sql = f"""
    INSERT INTO {table_ref(config.TARGET_SCHEMA, config.GAME_TYPE_TABLE)} ("gameType", "isActive", "createdAt", "updatedAt")
    VALUES (%s, true, now(), now())
    ON CONFLICT ("gameType") DO UPDATE SET "updatedAt"=now()
    RETURNING id
    """
    with conn.cursor() as cur:
        cur.execute(sql, (value,))
        gid = cur.fetchone()[0]
    cache[value] = gid
    return gid


def get_or_create_game_list(conn, config, game_name: str, provider_id: uuid.UUID, game_type_id: uuid.UUID, cache: Dict[Tuple[uuid.UUID, str], uuid.UUID], dry_run: bool) -> uuid.UUID:
    name = (game_name or "UNKNOWN").strip() or "UNKNOWN"
    key = (provider_id, name)
    if key in cache:
        return cache[key]
    if dry_run:
        with conn.cursor() as cur:
            cur.execute(
                f'SELECT id FROM {table_ref(config.TARGET_SCHEMA, config.GAME_LIST_TABLE)} WHERE "gameProviderId"=%s AND "gameName"=%s',
                (provider_id, name),
            )
            row = cur.fetchone()
        gid = row[0] if row else uuid.uuid4()
        cache[key] = gid
        return gid
    sql = f"""
    INSERT INTO {table_ref(config.TARGET_SCHEMA, config.GAME_LIST_TABLE)} (
        "gameTypeId", "gameProviderId", "gameName", "isProgressive", "isActive", "createdAt", "updatedAt", "brandName"
    ) VALUES (%s, %s, %s, false, true, now(), now(), %s)
    ON CONFLICT ("gameProviderId", "gameName") DO UPDATE SET
        "gameTypeId"=EXCLUDED."gameTypeId", "updatedAt"=now()
    RETURNING id
    """
    with conn.cursor() as cur:
        cur.execute(sql, (game_type_id, provider_id, name, config.BRAND))
        gid = cur.fetchone()[0]
    cache[key] = gid
    return gid


def _existing_game(conn, config, external_id: Any) -> Dict[str, Any]:
    if not external_id:
        return {}
    sql = f'''
    SELECT id, playerUserName, startDateTime, brand, platform, externalId
    FROM {table_ref(config.TARGET_SCHEMA, config.GAME_TRANSACTION_TABLE)}
    WHERE externalId=%s
    LIMIT 1
    '''
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (str(external_id),))
            row = cur.fetchone()
            if not row:
                return {}
            return {
                "id": row[0], "playerUserName": row[1],
                "startDateTime": row[2], "brand": row[3], "platform": row[4], "externalId": row[5],
            }
    except Exception:
        return {}


def insert_game_transactions(conn, config, rows: List[Dict[str, Any]], dry_run: bool, report_path: Optional[str] = None) -> Dict[str, int]:
    values: List[Tuple[Any, ...]] = []
    meta_rows: List[Dict[str, Any]] = []
    stats = {"insertAttemptRows": 0, "insertedRows": 0, "duplicateSkippedRows": 0, "skippedRows": 0, "missingRequiredRows": 0, "insertErrorRows": 0, "importDurationSeconds": 0.0, "columnDqCheckedRows": 0, "columnVarianceRows": 0, "columnVarianceColumns": 0, "columnVarianceColumnList": ""}
    provider_cache: Dict[str, uuid.UUID] = {}
    gametype_cache: Dict[str, uuid.UUID] = {}
    gamelist_cache: Dict[Tuple[uuid.UUID, str], uuid.UUID] = {}
    fact_only = fact_only_enabled()

    # Target table intentionally does not contain duplicate traceability columns
    # such as betId/betTransactionId/sourcePlayerId/sourceMemberId.
    # Keep source identity only in externalId and roundId.
    optional_cols = []

    for raw_row in rows:
        row = _as_mapping(raw_row)
        # Fact-only no-FK rule: do not require player_id and do not create/look up parent records.
        # Still require a source transaction identity and username because playerUserName is the soft link.
        required_missing = not row.get("external_id") or not row.get("username")
        if required_missing:
            stats["skippedRows"] += 1
            stats["missingRequiredRows"] += 1
            if report_path:
                write_phase_report(report_path, issueType="game_missing_required_field", category="missingRequired", sourceTable=config.SOURCE_TABLES.get("game_transactions"), sourceUsername=row.get("username"), sourceId=row.get("source_id"), referenceId=row.get("external_id"), sourceDate=row.get("created"), targetWindowDateColumn="startDateTime", nexusWindowDateColumn="startDateTime", action="skipped", reason="Missing external_id or username/playerUserName")
            continue
        # Reporting fact table: no providerId/gameId/gameTypeId/playerId insert.
        # No duplicate traceability columns are inserted; use externalId and roundId only.
        base_values = [
            row.get("created"), row.get("username"),
            row.get("table_room_id"), "0", to_decimal_str(row.get("bet_amount")), to_decimal_str(row.get("valid_bet", row.get("bet_amount"))),
            to_decimal_str(row.get("payout_amount")),
            to_decimal_str(row.get("pc1")), to_decimal_str(row.get("pc2")), to_decimal_str(row.get("pc3")), to_decimal_str(row.get("pc4")),
            to_decimal_str(row.get("pc5") if row.get("pc5") is not None else row.get("jackpot_contribution")),
            to_decimal_str(row.get("jw1")), to_decimal_str(row.get("jw2")), to_decimal_str(row.get("jw3")), to_decimal_str(row.get("jw4")),
            to_decimal_str(row.get("jw5") if row.get("jw5") is not None else row.get("jackpot_payout")),
            to_decimal_str(row.get("progression_contribution_paid")), to_decimal_str(row.get("seed_money_won")), int(row.get("seed_money_jackpot_over_1000") or 0),
            row.get("settled") or row.get("created"), row.get("external_id"), bool(row.get("parlay", False)),
            row.get("bet_details"), row.get("bet_timing"), config.BRAND, config.PLATFORM, row.get("round_id"),
        ]
        values.append(tuple(base_values))
        meta_rows.append(row)

    stats["insertAttemptRows"] = len(values)

    cols = [
        'startDateTime', 'playerUserName', 'tableRoomId',
        'sideBetAmount', 'betAmount', 'validBet', 'payoutAmount',
        'PC1', 'PC2', 'PC3', 'PC4', 'PC5', 'JW1', 'JW2', 'JW3', 'JW4', 'JW5',
        'progressionContributionPaid', 'seedMoneyWon', 'seedMoneyJackpotOver1000', 'endDateTime',
        'externalId', 'parlay', 'betDetails', 'betTiming', 'brand', 'platform', 'roundId'
    ]
    sql = f"""
    INSERT INTO {table_ref(config.TARGET_SCHEMA, config.GAME_TRANSACTION_TABLE)} (
        {', '.join(cols)}
    ) VALUES %s
    ON CONFLICT (externalId) DO NOTHING
    RETURNING externalId
    """
    _flash_import_sql_once(f"{config.BRAND_KEY} gameTransaction", sql)
    if dry_run:
        stats["insertedRows"] = 0
        batch_quality_log("gameTransaction", len(rows), len(meta_rows), len(values), 0, 0, dry_run=True)
        return stats
    if not values:
        batch_quality_log("gameTransaction", len(rows), len(meta_rows), 0, 0, 0)
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
                write_phase_report(report_path, issueType="game_insert_error", category="databaseInsertError", sourceTable=config.SOURCE_TABLES.get("game_transactions"), sourceUsername=row.get("username"), sourceId=row.get("source_id"), referenceId=row.get("external_id"), sourceDate=row.get("created"), targetPlayerId=row.get("player_id"), targetWindowDateColumn="startDateTime", nexusWindowDateColumn="startDateTime", action="insert_error", reason="gameTransaction insert failed", **parts)
        batch_quality_log("gameTransaction", len(rows), len(meta_rows), len(values), 0, 0, failed=True, reason=str(exc))
        trace(f"[GAME INSERT ERROR][{config.BRAND_KEY}] rows={len(values)} error={exc}")
        return stats

    stats["importDurationSeconds"] = time.perf_counter() - insert_started
    inserted_counts: Dict[str, int] = {}
    for item in inserted:
        key = str(item[0])
        inserted_counts[key] = inserted_counts.get(key, 0) + 1
    stats["insertedRows"] = len(inserted)
    for row in meta_rows:
        ext = str(row.get("external_id"))
        if inserted_counts.get(ext, 0) > 0:
            inserted_counts[ext] -= 1
            continue
        stats["duplicateSkippedRows"] += 1
        existing = _existing_game(conn, config, ext)
        if report_path:
            write_phase_report(
                report_path,
                issueType="duplicate_external_id_ignored",
                category="duplicateSkipped",
                sourceTable=config.SOURCE_TABLES.get("game_transactions"),
                sourceUsername=row.get("username"),
                sourceId=row.get("source_id"),
                referenceId=ext,
                sourceDate=row.get("created"),
                targetId=existing.get("id"),
                targetPlayerId=existing.get("playerId"),
                targetUsername=existing.get("playerUserName"),
                targetDate=existing.get("startDateTime"),
                targetWindowDateColumn="startDateTime",
                nexusWindowDateColumn="startDateTime",
                nexusWindowDateColumnValue=existing.get("startDateTime"),
                nexusExternalId=existing.get("externalId") or ext,
                nexusReferenceId=existing.get("externalId") or ext,
                platform=existing.get("platform") or config.PLATFORM,
                action="duplicate_key_ignored",
                reason="Duplicate gameTransaction externalId; ON CONFLICT DO NOTHING ignored this source row.",
            )
    present = target_present_count(conn, config.TARGET_SCHEMA, config.GAME_TRANSACTION_TABLE, ['externalId'], [(r.get("external_id"),) for r in meta_rows if r.get("external_id")])
    dq = compare_game_batch_columns(conn, config, meta_rows, report_path or "")
    stats["columnDqCheckedRows"] = int(dq.get("checkedRows", 0) or 0)
    stats["columnVarianceRows"] = int(dq.get("varianceRows", 0) or 0)
    stats["columnVarianceColumns"] = int(dq.get("varianceColumns", 0) or 0)
    stats["columnVarianceColumnList"] = ",".join(sorted(dq.get("columns", set()) or []))
    batch_quality_log("gameTransaction", len(rows), len(meta_rows), len(values), stats["insertedRows"], present)
    if stats["columnDqCheckedRows"]:
        trace(f"[COLUMN DQ][gameTransaction] checkedRows={stats['columnDqCheckedRows']} varianceRows={stats['columnVarianceRows']} varianceColumns={stats['columnVarianceColumns']} columns={stats['columnVarianceColumnList'] or 'none'}")
    return stats

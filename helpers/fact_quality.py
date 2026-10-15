import os
import traceback
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from .db import table_ref
from .reports import trace

_COLUMN_CACHE: Dict[Tuple[int, str, str], set] = {}


def _truthy(value: Any) -> bool:
    return str(value or '').strip().lower() in ('1','true','yes','y','on')


def fact_only_enabled() -> bool:
    # Unquoted target fact tables are reporting-fact-only by design.
    # Old FK/dimension IDs are no longer inserted, and old parent table upserts are not used.
    return True


def table_columns(conn, schema: str, table: str) -> set:
    key = (id(conn), schema, table)
    if key in _COLUMN_CACHE:
        return _COLUMN_CACHE[key]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            """,
            (schema, table.lower()),
        )
        cols = {r[0] for r in cur.fetchall()}
    _COLUMN_CACHE[key] = cols
    return cols


def column_exists(conn, schema: str, table: str, column: str) -> bool:
    return column.lower() in table_columns(conn, schema, table)


def columns_exist(conn, schema: str, table: str, columns: Sequence[str]) -> List[str]:
    cols = table_columns(conn, schema, table)
    return [c for c in columns if c.lower() in cols]


def nullable_status(conn, schema: str, table: str, columns: Sequence[str]) -> Dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, is_nullable
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
              AND column_name = ANY(%s)
            """,
            (schema, table.lower(), [c.lower() for c in columns]),
        )
        return {r[0]: r[1] for r in cur.fetchall()}


def _column_debug_rows(conn, schema: str, table: str) -> List[Tuple[str, str, str]]:
    """Return target column details for troubleshooting preflight failures.

    For unquoted PostgreSQL targets, mixed-case SQL tokens are stored in
    information_schema as lowercase names. Example: externalId -> externalid.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema, table.lower()),
        )
        return [(str(r[0]), str(r[1]), str(r[2])) for r in cur.fetchall()]


def _format_column_debug(rows: Sequence[Tuple[str, str, str]], limit: int = 80) -> str:
    if not rows:
        return '<no columns returned from information_schema>'
    parts = [f"{name}:{dtype}:nullable={nullable}" for name, dtype, nullable in rows[:limit]]
    if len(rows) > limit:
        parts.append(f"... truncated {len(rows) - limit} more")
    return ', '.join(parts)


def fact_preflight(conn, config) -> bool:
    """Small, cheap readiness check for no-FK fact-only loads.

    Important for the unquoted-target version:
    PostgreSQL folds unquoted identifiers to lowercase. Therefore the target
    table can be written in SQL as gameTransaction_prod/externalId, but
    information_schema will report gametransaction_prod/externalid. This
    preflight compares using lower-case canonical names and logs the actual
    column inventory when something is missing.
    """
    ok = True
    try:
        game_cols = table_columns(conn, config.TARGET_SCHEMA, config.GAME_TRANSACTION_TABLE)
        wallet_cols = table_columns(conn, config.TARGET_SCHEMA, config.WALLET_TRANSACTION_TABLE)
        trace(f"[FACT PREFLIGHT] target game table={table_ref(config.TARGET_SCHEMA, config.GAME_TRANSACTION_TABLE)} columns={len(game_cols)}")
        trace(f"[FACT PREFLIGHT] target wallet table={table_ref(config.TARGET_SCHEMA, config.WALLET_TRANSACTION_TABLE)} columns={len(wallet_cols)}")

        checks = (
            (config.GAME_TRANSACTION_TABLE, game_cols, ['externalId','playerUserName','startDateTime']),
            (config.WALLET_TRANSACTION_TABLE, wallet_cols, ['transactionType','platform','referenceId','amount','status','createdDatetime']),
        )

        for tbl_name, cols, required in checks:
            lower_cols = {str(c).lower() for c in cols}
            missing = [c for c in required if c.lower() not in lower_cols]
            if missing:
                ok = False
                actual_rows = _column_debug_rows(conn, config.TARGET_SCHEMA, tbl_name)
                trace(
                    f"FAILED: target table {tbl_name} missing required columns {missing}; "
                    f"required_lower={[c.lower() for c in required]}; "
                    f"actual_columns={sorted(lower_cols)}"
                )
                trace(
                    f"[FACT PREFLIGHT DEBUG] table={table_ref(config.TARGET_SCHEMA, tbl_name)} "
                    f"information_schema_table_name={tbl_name.lower()} "
                    f"column_details={_format_column_debug(actual_rows)}"
                )
                trace(
                    "[FACT PREFLIGHT HINT] If you recreated target tables without double quotes, "
                    "PostgreSQL stores names in lowercase. This patched preflight accepts that. "
                    "If this still fails, rerun the target DDL and verify with: "
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='<lowercase_table_name>';"
                )

        # FK parent ID columns were removed from the new unquoted fact tables.
        # No nullable FK preflight is required.
        if ok:
            trace('OK for Data Import | target table readiness passed')
    except Exception as exc:
        ok = False
        trace(f"FAILED: fact preflight error: {type(exc).__name__}: {exc}")
        trace(f"[FACT PREFLIGHT TRACEBACK] {traceback.format_exc()}")
    return ok



def env_bool(name: str, default: bool = False) -> bool:
    return _truthy(os.getenv(name, '1' if default else '0'))


def env_int(name: str, default: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == '':
        return default
    try:
        return int(str(raw).strip())
    except Exception:
        return default


def _norm(value: Any) -> str:
    # Normalize values for low-cost batch DQ comparison.
    if value is None:
        return ''
    text = str(value).strip()
    text = text.replace('T', ' ').replace('+00:00', '').replace('Z', '')
    if '.' in text and ':' in text:
        text = text.split('.', 1)[0]
    try:
        from decimal import Decimal
        return format(Decimal(text), 'f').rstrip('0').rstrip('.') or '0'
    except Exception:
        return text


def batch_column_dq_enabled() -> bool:
    return env_bool('MIGRATION_BATCH_COLUMN_DQ', True)


def batch_column_dq_limit(default: int = 500) -> int:
    return max(0, env_int('MIGRATION_BATCH_COLUMN_DQ_LIMIT', default))


def compare_game_batch_columns(conn, config, source_rows: Sequence[Dict[str, Any]], report_path: str = '') -> Dict[str, Any]:
    # Low-cost post-insert/upsert validation for the current game batch.
    result = {'checkedRows': 0, 'varianceRows': 0, 'varianceColumns': 0, 'columns': set()}
    if not batch_column_dq_enabled() or not source_rows:
        return result
    sample = [dict(r or {}) for r in source_rows if dict(r or {}).get('external_id')][:batch_column_dq_limit()]
    if not sample:
        return result
    keys = [str(r.get('external_id')) for r in sample]
    with conn.cursor() as cur:
        cur.execute(
            f'''
            SELECT externalId, startDateTime, playerUserName, betAmount, validBet, payoutAmount, brand, platform, roundId
            FROM {table_ref(config.TARGET_SCHEMA, config.GAME_TRANSACTION_TABLE)}
            WHERE externalId = ANY(%s)
            ''',
            (keys,),
        )
        targets = {str(r[0]): r for r in cur.fetchall()}
    for src in sample:
        ext = str(src.get('external_id'))
        tgt = targets.get(ext)
        if not tgt:
            result['varianceRows'] += 1
            result['varianceColumns'] += 1
            result['columns'].add('missing_target_row')
            continue
        comparisons = {
            'startDateTime': (src.get('created'), tgt[1]),
            'playerUserName': (src.get('username'), tgt[2]),
            'betAmount': (src.get('bet_amount'), tgt[3]),
            'validBet': (src.get('valid_bet', src.get('bet_amount')), tgt[4]),
            'payoutAmount': (src.get('payout_amount'), tgt[5]),
            'brand': (config.BRAND, tgt[6]),
            'platform': (config.PLATFORM, tgt[7]),
            'roundId': (src.get('round_id'), tgt[8]),
        }
        row_has_variance = False
        for col, (expected, actual) in comparisons.items():
            if _norm(expected) != _norm(actual):
                row_has_variance = True
                result['varianceColumns'] += 1
                result['columns'].add(col)
        if row_has_variance:
            result['varianceRows'] += 1
    result['checkedRows'] = len(sample)
    return result


def compare_wallet_batch_columns(conn, config, source_rows: Sequence[Dict[str, Any]], report_path: str = '') -> Dict[str, Any]:
    # Low-cost post-insert/upsert validation for the current wallet batch.
    result = {'checkedRows': 0, 'varianceRows': 0, 'varianceColumns': 0, 'columns': set()}
    if not batch_column_dq_enabled() or not source_rows:
        return result
    sample = [dict(r or {}) for r in source_rows if dict(r or {}).get('reference_id')][:batch_column_dq_limit()]
    if not sample:
        return result
    refs = [str(r.get('reference_id')) for r in sample]
    with conn.cursor() as cur:
        cur.execute(
            f'''
            SELECT referenceId, transactionType, amount, status, createdDatetime, platform
            FROM {table_ref(config.TARGET_SCHEMA, config.WALLET_TRANSACTION_TABLE)}
            WHERE platform = %s
              AND referenceId = ANY(%s)
            ''',
            (config.WALLET_PLATFORM, refs),
        )
        targets = {str(r[0]): r for r in cur.fetchall()}
    for src in sample:
        ref = str(src.get('reference_id'))
        tgt = targets.get(ref)
        if not tgt:
            result['varianceRows'] += 1
            result['varianceColumns'] += 1
            result['columns'].add('missing_target_row')
            continue
        comparisons = {
            'transactionType': (str(src.get('kind') or '').lower(), tgt[1]),
            'amount': (src.get('amount'), tgt[2]),
            'status': (str(src.get('status') or '').strip().lower(), tgt[3]),
            'createdDatetime': (src.get('created'), tgt[4]),
            'platform': (config.WALLET_PLATFORM, tgt[5]),
        }
        row_has_variance = False
        for col, (expected, actual) in comparisons.items():
            if _norm(expected) != _norm(actual):
                row_has_variance = True
                result['varianceColumns'] += 1
                result['columns'].add(col)
        if row_has_variance:
            result['varianceRows'] += 1
    result['checkedRows'] = len(sample)
    return result


def target_present_count(conn, schema: str, table: str, key_columns: Sequence[str], keys: Sequence[Tuple[Any, ...]]) -> int:
    if not keys:
        return 0
    # Keep lightweight: at most sample first 500 keys per batch.
    sample = list(keys[:500])
    with conn.cursor() as cur:
        if len(key_columns) == 1:
            cur.execute(
                f"SELECT count(*) FROM {table_ref(schema, table)} WHERE {key_columns[0]} = ANY(%s)",
                ([k[0] for k in sample],),
            )
            return int(cur.fetchone()[0] or 0)
        if len(key_columns) == 2:
            # Use VALUES for composite key matching.
            placeholders = ','.join(['(%s,%s)'] * len(sample))
            flat = []
            for a,b in sample:
                flat.extend([a,b])
            cur.execute(
                f"SELECT count(*) FROM {table_ref(schema, table)} t JOIN (VALUES {placeholders}) v(c1,c2) ON t.{key_columns[0]} = v.c1 AND t.{key_columns[1]} = v.c2",
                flat,
            )
            return int(cur.fetchone()[0] or 0)
    return 0


def batch_quality_log(phase: str, source_rows: int, transformed: int, insert_attempt: int, inserted: int, target_present: int, failed: bool=False, reason: str='', dry_run: bool=False) -> None:
    if failed:
        trace(f"FAILED: {phase} | {reason}")
        return
    if dry_run:
        trace(
            f"DRY RUN ONLY | phase={phase} source_rows={source_rows} transformed={transformed} "
            f"would_attempt={insert_attempt} inserted=0 target_present_for_batch_keys=not_checked"
        )
        return
    trace(
        f"OK for Data Import | phase={phase} source_rows={source_rows} transformed={transformed} "
        f"attempted={insert_attempt} inserted={inserted} target_present_for_batch_keys={target_present}"
    )

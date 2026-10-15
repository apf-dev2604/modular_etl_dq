from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv is not None:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    load_dotenv(PROJECT_ROOT / ".env")

import argparse
import os
import time
import importlib
from typing import Any, Dict, List, Optional

from .dates import business_window_bounds, parse_date_arg
from .context import MigrationContext
from .models import BrandConfig, NormalizedGameTransaction, NormalizedPlayer, NormalizedWalletTransaction
from .validators import validate_adapter, validate_brand_config
from .db import connect
from .players import build_player_map, lookup_player_id_by_username, upsert_player, username_key
from .reports import configure_logging, make_report_paths, trace, print_phase_summary, write_phase_report, set_phase_report_context, clear_phase_report_context, human_date_label
from .source_fetch import fetch_json_batch, format_checkpoint, get_checkpoint, parse_checkpoint, set_checkpoint, print_source_fetch_summary, reset_source_fetch_summary
from .game_transactions import insert_game_transactions
from .wallet_transactions import ensure_wallet_dedupe_index, insert_wallet_transactions
from .fact_quality import fact_preflight
from .reconciliation import run_detailed_reconciliation
from .data_quality import run_data_quality_checks
from .comparison import run_source_nexus_comparison, email_comparison_report

PUBLIC_BRANDS = {
    "inplay": "inplay",
    "inplayv1": "inplayv1",
    "instaplay": "instaplay",
    "1play": "oneplay",
}

def load_brand(brand_key: str):
    public_key = str(brand_key).strip().lower()
    if public_key not in PUBLIC_BRANDS:
        allowed = ", ".join(sorted(PUBLIC_BRANDS))
        raise SystemExit(f"Unsupported --brand {brand_key!r}. Allowed values: {allowed}")
    module_key = PUBLIC_BRANDS[public_key]
    adapter_module = importlib.import_module(f"helpers.brands.{module_key}.adapter")
    config_module = importlib.import_module(f"helpers.brands.{module_key}.config")
    config = BrandConfig.from_module(config_module)
    adapter = adapter_module.Adapter()
    validate_brand_config(config)
    validate_adapter(adapter, config.SOURCE_MODE)
    return adapter, config


def resolve_player_id(tgt_conn, config, player_map: Dict[str, Any], username: str) -> Optional[Any]:
    key = username_key(username)
    player_id = player_map.get(key)
    if player_id:
        return player_id
    player_id = lookup_player_id_by_username(tgt_conn, config, username)
    if player_id:
        player_map[key] = player_id
    return player_id


def source_date_expr(adapter, source_key: str) -> str:
    if hasattr(adapter, "source_date_expr_for_table"):
        return adapter.source_date_expr_for_table(source_key, "data")
    return adapter.source_date_expr("data")


def source_created_value(adapter, data: Dict[str, Any], source_key: str) -> Optional[str]:
    try:
        return adapter.source_created_value(data, source_key)
    except TypeError:
        return adapter.source_created_value(data)


def _window_text(value):
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value is not None else "")

def _set_phase_context(config, adapter, phase: str, source_key: str, from_dt, until_dt) -> None:
    try:
        expr = source_date_expr(adapter, source_key)
    except Exception:
        expr = ""
    date_label = human_date_label(expr)
    set_phase_report_context(
        phase=phase,
        sourceDatabase=config.SOURCE_DB_NAME,
        sourceSchema=config.SOURCE_SCHEMA,
        sourceTable=config.SOURCE_TABLES.get(source_key, ""),
        sourceDateField=date_label,
        sourceWindowDateColumn=date_label,
        sourceWindowFromInclusive=_window_text(from_dt),
        sourceWindowToExclusive=_window_text(until_dt),
    )

def fetch_detail_map(adapter, src_conn, from_dt=None, until_dt=None):
    try:
        return adapter.fetch_player_detail_map(src_conn, from_dt=from_dt, until_dt=until_dt)
    except TypeError:
        try:
            return adapter.fetch_player_detail_map(src_conn, date_from=from_dt, date_to=until_dt)
        except TypeError:
            return adapter.fetch_player_detail_map(src_conn)



def _blank_phase_stats() -> Dict[str, int]:
    return {
        "sourceRows": 0,
        "mappedRows": 0,
        "insertAttemptRows": 0,
        "insertedRows": 0,
        "updatedRows": 0,
        "duplicateSkippedRows": 0,
        "skippedRows": 0,
        "missingPlayerRows": 0,
        "missingUsernameRows": 0,
        "missingRequiredRows": 0,
        "mappingErrorRows": 0,
        "insertErrorRows": 0,
        "dataBatches": 0,
        "importDurationSeconds": 0.0,
        "importDurationMinutes": 0.0,
        "columnDqCheckedRows": 0,
        "columnVarianceRows": 0,
        "columnVarianceColumns": 0,
        "columnVarianceColumnList": "",
    }


def _add_insert_stats(phase_stats: Dict[str, int], insert_stats: Any) -> None:
    if isinstance(insert_stats, dict):
        for key in ("insertAttemptRows", "insertedRows", "duplicateSkippedRows", "skippedRows", "missingRequiredRows", "insertErrorRows", "columnDqCheckedRows", "columnVarianceRows", "columnVarianceColumns"):
            phase_stats[key] = int(phase_stats.get(key, 0) or 0) + int(insert_stats.get(key, 0) or 0)
        phase_stats["importDurationSeconds"] = float(phase_stats.get("importDurationSeconds", 0.0) or 0.0) + float(insert_stats.get("importDurationSeconds", 0.0) or 0.0)
        phase_stats["importDurationMinutes"] = float(phase_stats.get("importDurationSeconds", 0.0) or 0.0) / 60.0
        existing_cols = set(str(phase_stats.get("columnVarianceColumnList") or "").split(",")) if phase_stats.get("columnVarianceColumnList") else set()
        new_cols = set(str(insert_stats.get("columnVarianceColumnList") or "").split(",")) if insert_stats.get("columnVarianceColumnList") else set()
        all_cols = sorted(c for c in existing_cols.union(new_cols) if c)
        phase_stats["columnVarianceColumnList"] = ",".join(all_cols)
        return
    if isinstance(insert_stats, tuple):
        inserted = int(insert_stats[0] or 0)
        skipped = int(insert_stats[1] or 0) if len(insert_stats) > 1 else 0
        phase_stats["insertedRows"] += inserted
        phase_stats["skippedRows"] += skipped
        return




def _selected_entity(args) -> str:
    # User-facing selector. --entity is preferred; --only remains as hidden backward-compatible alias.
    value = getattr(args, "entity", None) or getattr(args, "only", None) or "all"
    normalized = str(value).strip()
    lookup = {
        "all": "all",
        "game": "game",
        "gametransaction": "game",
        "game_transactions": "game",
        "wallet": "wallet",
        "wallettransaction": "wallet",
        "wallet_transactions": "wallet",
    }
    return lookup.get(normalized.lower(), "all")

def _load_game(args) -> bool:
    return _selected_entity(args) in ("all", "game")

def _load_wallet(args) -> bool:
    return _selected_entity(args) in ("all", "wallet")

def _selected_fact_phases(args) -> List[str]:
    phases: List[str] = []
    if _load_game(args):
        phases.append("game_transactions")
    if _load_wallet(args):
        phases.extend(["deposits", "withdrawals"])
    return phases

def process_table_batch_brand(src_conn, tgt_conn, ctx: MigrationContext, args) -> int:
    adapter = ctx.adapter
    config = ctx.config
    paths = ctx.paths
    from_dt = ctx.from_dt
    until_dt = ctx.until_dt
    ensure_wallet_dedupe_index(tgt_conn, config, args.dry_run)
    detail_map = fetch_detail_map(adapter, src_conn, from_dt, until_dt)
    total = 0

    player_stats = _blank_phase_stats()
    phase = "players"
    reset_source_fetch_summary(f"{config.BRAND_KEY} players")
    _set_phase_context(config, adapter, "players", "players", from_dt, until_dt)
    after_dt, after_id = (None, None) if args.date_from or args.date_to else parse_checkpoint(get_checkpoint(tgt_conn, config.TARGET_SCHEMA, config.CHECKPOINT_TABLE, config.checkpoint_key(phase)) if args.resume else None)
    while True:
        rows = fetch_json_batch(src_conn, config.SOURCE_SCHEMA, config.SOURCE_TABLES["players"], source_date_expr(adapter, "players"), after_dt, after_id, args.batch_size, from_dt, until_dt, label=f"{config.BRAND_KEY} players")
        if not rows:
            break
        player_stats["sourceRows"] += len(rows)
        player_stats["dataBatches"] += 1
        for row in rows:
            try:
                if hasattr(adapter, "ensure_outlet_from_player_row"):
                    adapter.ensure_outlet_from_player_row(tgt_conn, row, args.dry_run)
                mapped = adapter.map_player(row, detail_map)
                if not mapped:
                    player_stats["skippedRows"] += 1
                    player_stats["mappingErrorRows"] += 1
                    write_phase_report(paths.players, issueType="player_mapping_empty", category="mappingOrValidationError", sourceTable=config.SOURCE_TABLES["players"], sourceId=row.get("id"), action="skipped", reason="adapter.map_player returned no mapped player")
                    continue
                player_stats["mappedRows"] += 1
                player_stats["insertAttemptRows"] += 1
                existed = lookup_player_id_by_username(tgt_conn, config, mapped.get("username") or "") if mapped.get("username") else None
                player_id = upsert_player(tgt_conn, config, NormalizedPlayer.from_mapping(mapped), args.dry_run)
                if existed:
                    player_stats["updatedRows"] += 1
                    write_phase_report(
                        paths.players,
                        issueType="player_existing_updated_not_inserted",
                        category="updatedExisting",
                        sourceTable=config.SOURCE_TABLES["players"],
                        sourceUsername=mapped.get("username"),
                        sourcePlayerId=mapped.get("external_id"),
                        sourcePlayerAccount=mapped.get("username"),
                        sourceId=row.get("id"),
                        referenceId=mapped.get("external_id"),
                        sourceDate=mapped.get("registration_date"),
                        targetId=player_id,
                        action="updated_existing_row",
                        reason="Player was not newly inserted because userName already existed in playerDetails_final; row was updated by ON CONFLICT DO UPDATE.",
                    )
                else:
                    player_stats["insertedRows"] += 1
            except Exception as exc:
                player_stats["insertErrorRows"] += 1
                write_phase_report(paths.players, issueType="player_upsert_error", category="databaseInsertError", sourceTable=config.SOURCE_TABLES["players"], sourceId=row.get("id"), action="insert_error", reason="playerDetails upsert failed", error=exc)
                try:
                    tgt_conn.rollback()
                except Exception:
                    pass
        last = rows[-1]
        last_data = adapter.as_dict(last.get("data"))
        after_id = str(last["id"])
        after_dt = source_created_value(adapter, last_data, "players")
        set_checkpoint(tgt_conn, config.TARGET_SCHEMA, config.CHECKPOINT_TABLE, config.checkpoint_key(phase), format_checkpoint(after_dt, after_id), args.dry_run)
        total += len(rows)
        if not args.dry_run:
            tgt_conn.commit()
    print_source_fetch_summary(f"{config.BRAND_KEY} players")
    print_phase_summary(config.BRAND_KEY, "players", player_stats, paths.players)

    player_map = build_player_map(tgt_conn, config)

    if _load_game(args):
        game_stats = _blank_phase_stats()
        phase = "game_transactions"
        reset_source_fetch_summary(f"{config.BRAND_KEY} game")
    _set_phase_context(config, adapter, "game_transactions", "game_transactions", from_dt, until_dt)
    after_dt, after_id = (None, None) if args.date_from or args.date_to else parse_checkpoint(get_checkpoint(tgt_conn, config.TARGET_SCHEMA, config.CHECKPOINT_TABLE, config.checkpoint_key(phase)) if args.resume else None)
    while True:
        rows = fetch_json_batch(src_conn, config.SOURCE_SCHEMA, config.SOURCE_TABLES["game_transactions"], source_date_expr(adapter, "game_transactions"), after_dt, after_id, args.batch_size, from_dt, until_dt, label=f"{config.BRAND_KEY} game")
        if not rows:
            break
        game_stats["sourceRows"] += len(rows)
        game_stats["dataBatches"] += 1
        mapped_rows: List[Dict[str, Any]] = []
        for row in rows:
            mapped = adapter.map_game_transaction(row)
            if not mapped:
                game_stats["skippedRows"] += 1
                game_stats["mappingErrorRows"] += 1
                write_phase_report(paths.game, issueType="game_mapping_empty", category="mappingOrValidationError", sourceTable=config.SOURCE_TABLES["game_transactions"], sourceId=row.get("id"), action="skipped", reason="adapter.map_game_transaction returned no mapped transaction")
                continue
            game_stats["mappedRows"] += 1
            username = mapped.get("username") or ""
            if not username:
                game_stats["skippedRows"] += 1
                game_stats["missingUsernameRows"] += 1
                write_phase_report(paths.game, issueType="missing_username", category="missingUsername", sourceTable=config.SOURCE_TABLES["game_transactions"], sourceId=mapped.get("source_id") or row.get("id"), referenceId=mapped.get("external_id"), sourceDate=mapped.get("created"), action="skipped", reason="Missing username after mapping")
                continue
            player_id = resolve_player_id(tgt_conn, config, player_map, username)
            if player_id:
                mapped["player_id"] = player_id
                mapped_rows.append(NormalizedGameTransaction.from_mapping(mapped))
            else:
                game_stats["skippedRows"] += 1
                game_stats["missingPlayerRows"] += 1
                write_phase_report(paths.game, issueType="player_not_in_player_map", category="missingTargetPlayer", sourceTable=config.SOURCE_TABLES["game_transactions"], sourceUsername=username, sourceId=mapped.get("source_id") or row.get("id"), referenceId=mapped.get("external_id"), sourceDate=mapped.get("created"), action="skipped", reason="No matching playerDetails_final row found by normalized username; transaction not inserted")
        insert_stats = insert_game_transactions(tgt_conn, config, mapped_rows, args.dry_run, paths.game)
        _add_insert_stats(game_stats, insert_stats)
        last = rows[-1]
        last_data = adapter.as_dict(last.get("data"))
        after_id = str(last["id"])
        after_dt = source_created_value(adapter, last_data, "game_transactions")
        set_checkpoint(tgt_conn, config.TARGET_SCHEMA, config.CHECKPOINT_TABLE, config.checkpoint_key(phase), format_checkpoint(after_dt, after_id), args.dry_run)
        total += len(rows)
        if not args.dry_run:
            tgt_conn.commit()
    print_source_fetch_summary(f"{config.BRAND_KEY} game")
    print_phase_summary(config.BRAND_KEY, "game_transactions", game_stats, paths.game)

    for kind, source_key, report_path in (("deposit", "deposits", paths.deposits), ("withdrawal", "withdrawals", paths.withdrawals)):
        wallet_stats = _blank_phase_stats()
        phase = source_key
        reset_source_fetch_summary(f"{config.BRAND_KEY} {kind}")
        _set_phase_context(config, adapter, source_key, source_key, from_dt, until_dt)
        after_dt, after_id = (None, None) if args.date_from or args.date_to else parse_checkpoint(get_checkpoint(tgt_conn, config.TARGET_SCHEMA, config.CHECKPOINT_TABLE, config.checkpoint_key(phase)) if args.resume else None)
        while True:
            rows = fetch_json_batch(src_conn, config.SOURCE_SCHEMA, config.SOURCE_TABLES[source_key], source_date_expr(adapter, source_key), after_dt, after_id, args.batch_size, from_dt, until_dt, label=f"{config.BRAND_KEY} {kind}")
            if not rows:
                break
            wallet_stats["sourceRows"] += len(rows)
            wallet_stats["dataBatches"] += 1
            mapped_rows = []
            for row in rows:
                mapped = adapter.map_wallet(row, kind)
                if not mapped:
                    wallet_stats["skippedRows"] += 1
                    wallet_stats["mappingErrorRows"] += 1
                    write_phase_report(report_path, issueType="wallet_mapping_empty", category="mappingOrValidationError", sourceTable=config.SOURCE_TABLES[source_key], sourceId=row.get("id"), transactionType=kind, action="skipped", reason="adapter.map_wallet returned no mapped wallet transaction")
                    continue
                wallet_stats["mappedRows"] += 1
                username = mapped.get("username") or ""
                if not username:
                    wallet_stats["skippedRows"] += 1
                    wallet_stats["missingUsernameRows"] += 1
                    write_phase_report(report_path, issueType="missing_username", category="missingUsername", sourceTable=config.SOURCE_TABLES[source_key], sourceId=mapped.get("source_id") or row.get("id"), referenceId=mapped.get("reference_id"), sourceDate=mapped.get("created"), transactionType=kind, action="skipped", reason="Missing username after mapping")
                    continue
                player_id = resolve_player_id(tgt_conn, config, player_map, username)
                if player_id:
                    mapped["player_id"] = player_id
                    mapped_rows.append(NormalizedWalletTransaction.from_mapping(mapped))
                else:
                    wallet_stats["skippedRows"] += 1
                    wallet_stats["missingPlayerRows"] += 1
                    write_phase_report(report_path, issueType="player_not_in_player_map", category="missingTargetPlayer", sourceTable=config.SOURCE_TABLES[source_key], sourceUsername=username, sourceId=mapped.get("source_id") or row.get("id"), referenceId=mapped.get("reference_id"), sourceDate=mapped.get("created"), transactionType=kind, action="skipped", reason="No matching playerDetails_final row found by normalized username; wallet transaction not inserted")
            insert_stats = insert_wallet_transactions(tgt_conn, config, mapped_rows, args.dry_run, report_path)
            _add_insert_stats(wallet_stats, insert_stats)
            last = rows[-1]
            last_data = adapter.as_dict(last.get("data"))
            after_id = str(last["id"])
            after_dt = source_created_value(adapter, last_data, source_key)
            set_checkpoint(tgt_conn, config.TARGET_SCHEMA, config.CHECKPOINT_TABLE, config.checkpoint_key(phase), format_checkpoint(after_dt, after_id), args.dry_run)
            total += len(rows)
            if not args.dry_run:
                tgt_conn.commit()
        print_source_fetch_summary(f"{config.BRAND_KEY} {kind}")
        print_phase_summary(config.BRAND_KEY, source_key, wallet_stats, report_path)
    return total

def process_flat_table_batch_brand(src_conn, tgt_conn, ctx: MigrationContext, args) -> int:
    adapter = ctx.adapter
    config = ctx.config
    paths = ctx.paths
    ensure_wallet_dedupe_index(tgt_conn, config, args.dry_run)
    detail_map = fetch_detail_map(adapter, src_conn, ctx.from_dt, ctx.until_dt)
    total = 0

    phase = "players"
    after_id = args.start_after_id or None
    if after_id is None and args.resume:
        raw = get_checkpoint(tgt_conn, config.TARGET_SCHEMA, config.CHECKPOINT_TABLE, config.checkpoint_key(phase))
        _, after_id = parse_checkpoint(raw)
    while True:
        rows = adapter.fetch_player_rows(src_conn, int(after_id or 0), args.batch_size, ctx.from_dt, ctx.until_dt)
        if not rows:
            break
        for row in rows:
            if hasattr(adapter, "ensure_outlet_from_player_row"):
                adapter.ensure_outlet_from_player_row(tgt_conn, row, args.dry_run)
            mapped = adapter.map_player(row, detail_map)
            if mapped:
                upsert_player(tgt_conn, config, NormalizedPlayer.from_mapping(mapped), args.dry_run)
        after_id = str(rows[-1].get("IDX") or rows[-1].get("id") or after_id or 0)
        set_checkpoint(tgt_conn, config.TARGET_SCHEMA, config.CHECKPOINT_TABLE, config.checkpoint_key(phase), after_id, args.dry_run)
        total += len(rows)
        if not args.dry_run:
            tgt_conn.commit()

    player_map = build_player_map(tgt_conn, config)

    phase = "game_transactions"
    after_id = args.start_after_id or None
    if after_id is None and args.resume:
        raw = get_checkpoint(tgt_conn, config.TARGET_SCHEMA, config.CHECKPOINT_TABLE, config.checkpoint_key(phase))
        _, after_id = parse_checkpoint(raw)
    while True:
        rows = adapter.fetch_game_rows(src_conn, int(after_id or 0), args.batch_size, ctx.from_dt, ctx.until_dt)
        if not rows:
            break
        mapped_rows = []
        for row in rows:
            mapped = adapter.map_game_transaction(row)
            if not mapped:
                continue
            player_id = resolve_player_id(tgt_conn, config, player_map, mapped.get("username") or "")
            if player_id:
                mapped["player_id"] = player_id
                mapped_rows.append(NormalizedGameTransaction.from_mapping(mapped))
        insert_game_transactions(tgt_conn, config, mapped_rows, args.dry_run, paths.game)
        after_id = str(rows[-1].get("IDX") or rows[-1].get("id") or after_id or 0)
        set_checkpoint(tgt_conn, config.TARGET_SCHEMA, config.CHECKPOINT_TABLE, config.checkpoint_key(phase), after_id, args.dry_run)
        total += len(rows)
        if not args.dry_run:
            tgt_conn.commit()

    phase = "wallet_transactions"
    after_id = args.start_after_id or None
    if after_id is None and args.resume:
        raw = get_checkpoint(tgt_conn, config.TARGET_SCHEMA, config.CHECKPOINT_TABLE, config.checkpoint_key(phase))
        _, after_id = parse_checkpoint(raw)
    while True:
        rows = adapter.fetch_wallet_rows(src_conn, int(after_id or 0), args.batch_size, ctx.from_dt, ctx.until_dt)
        if not rows:
            break
        mapped_rows = []
        for row in rows:
            mapped = adapter.map_wallet(row, "wallet")
            if not mapped:
                continue
            player_id = resolve_player_id(tgt_conn, config, player_map, mapped.get("username") or "")
            if player_id:
                mapped["player_id"] = player_id
                mapped_rows.append(NormalizedWalletTransaction.from_mapping(mapped))
        insert_wallet_transactions(tgt_conn, config, mapped_rows, args.dry_run, paths.deposits)
        after_id = str(rows[-1].get("IDX") or rows[-1].get("id") or after_id or 0)
        set_checkpoint(tgt_conn, config.TARGET_SCHEMA, config.CHECKPOINT_TABLE, config.checkpoint_key(phase), after_id, args.dry_run)
        total += len(rows)
        if not args.dry_run:
            tgt_conn.commit()
    return total


def process_member_driven_brand(src_conn, tgt_conn, ctx: MigrationContext, args) -> int:
    adapter = ctx.adapter
    config = ctx.config
    paths = ctx.paths
    from_dt = ctx.from_dt
    until_dt = ctx.until_dt
    ensure_wallet_dedupe_index(tgt_conn, config, args.dry_run)
    detail_cache = {}
    player_map = build_player_map(tgt_conn, config)
    processed = 0
    after_dt, after_id = (None, None) if args.date_from or args.date_to else parse_checkpoint(get_checkpoint(tgt_conn, config.TARGET_SCHEMA, config.CHECKPOINT_TABLE, config.checkpoint_key("players")) if args.resume else None)

    while True:
        rows = fetch_json_batch(src_conn, config.SOURCE_SCHEMA, config.SOURCE_TABLES["players"], source_date_expr(adapter, "players"), after_dt, after_id, args.batch_size, from_dt, until_dt, label=f"{config.BRAND_KEY} member batch")
        if not rows:
            break
        for row in rows:
            player = adapter.map_player(row, detail_cache, src_conn=src_conn)
            if not player:
                continue
            player_model = NormalizedPlayer.from_mapping(player)
            player_id = upsert_player(tgt_conn, config, player_model, args.dry_run)
            player_map[username_key(player_model.username)] = player_id
            member_id = player_model.external_id
            username = player_model.username

            game_rows = adapter.fetch_member_game_rows(src_conn, member_id, username, args.tx_limit, from_dt, until_dt)
            mapped_games = []
            for game_row in game_rows:
                mapped = adapter.map_game_transaction(game_row)
                if mapped:
                    mapped["player_id"] = player_id
                    mapped_games.append(NormalizedGameTransaction.from_mapping(mapped))
            insert_game_transactions(tgt_conn, config, mapped_games, args.dry_run, paths.game)

            wallet_rows = []
            for kind in ("deposit", "withdrawal"):
                rows_for_kind = adapter.fetch_member_wallet_rows(src_conn, kind, member_id, args.wallet_limit, from_dt, until_dt)
                for wallet_row in rows_for_kind:
                    mapped = adapter.map_wallet(wallet_row, kind)
                    if mapped:
                        mapped["player_id"] = player_id
                        wallet_rows.append(NormalizedWalletTransaction.from_mapping(mapped))
            insert_wallet_transactions(tgt_conn, config, wallet_rows, args.dry_run, paths.deposits)
            processed += 1
            if processed % args.commit_every == 0:
                if args.dry_run:
                    tgt_conn.rollback()
                else:
                    tgt_conn.commit()
        last = rows[-1]
        last_data = adapter.as_dict(last.get("data"))
        after_id = str(last["id"])
        after_dt = source_created_value(adapter, last_data, "players")
        set_checkpoint(tgt_conn, config.TARGET_SCHEMA, config.CHECKPOINT_TABLE, config.checkpoint_key("players"), format_checkpoint(after_dt, after_id), args.dry_run)
        if not args.dry_run:
            tgt_conn.commit()
    return processed



def process_table_batch_brand_fact_only(src_conn, tgt_conn, ctx: MigrationContext, args) -> int:
    """Fact-only no-FK path for table_batch brands: game + wallet only, no players/outlets/dimensions."""
    adapter = ctx.adapter
    config = ctx.config
    paths = ctx.paths
    from_dt = ctx.from_dt
    until_dt = ctx.until_dt
    ensure_wallet_dedupe_index(tgt_conn, config, args.dry_run)
    total = 0

    if _load_game(args):
        game_stats = _blank_phase_stats()
        phase = "game_transactions"
        reset_source_fetch_summary(f"{config.BRAND_KEY} game")
        _set_phase_context(config, adapter, "game_transactions", "game_transactions", from_dt, until_dt)
        after_dt, after_id = (None, None) if args.date_from or args.date_to else parse_checkpoint(get_checkpoint(tgt_conn, config.TARGET_SCHEMA, config.CHECKPOINT_TABLE, config.checkpoint_key(phase)) if args.resume else None)
        while True:
            rows = fetch_json_batch(src_conn, config.SOURCE_SCHEMA, config.SOURCE_TABLES["game_transactions"], source_date_expr(adapter, "game_transactions"), after_dt, after_id, args.batch_size, from_dt, until_dt, label=f"{config.BRAND_KEY} game")
            if not rows:
                break
            game_stats["sourceRows"] += len(rows)
            game_stats["dataBatches"] += 1
            mapped_rows = []
            for row in rows:
                mapped = adapter.map_game_transaction(row)
                if not mapped:
                    game_stats["skippedRows"] += 1
                    game_stats["mappingErrorRows"] += 1
                    write_phase_report(paths.game, issueType="game_mapping_empty", category="mappingOrValidationError", sourceTable=config.SOURCE_TABLES["game_transactions"], sourceId=row.get("id"), action="skipped", reason="adapter.map_game_transaction returned no mapped transaction")
                    continue
                game_stats["mappedRows"] += 1
                mapped["player_id"] = None
                mapped_rows.append(NormalizedGameTransaction.from_mapping(mapped))
            insert_stats = insert_game_transactions(tgt_conn, config, mapped_rows, args.dry_run, paths.game)
            _add_insert_stats(game_stats, insert_stats)
            last = rows[-1]
            last_data = adapter.as_dict(last.get("data"))
            after_id = str(last["id"])
            after_dt = source_created_value(adapter, last_data, "game_transactions")
            set_checkpoint(tgt_conn, config.TARGET_SCHEMA, config.CHECKPOINT_TABLE, config.checkpoint_key(phase), format_checkpoint(after_dt, after_id), args.dry_run)
            total += len(rows)
            if not args.dry_run:
                tgt_conn.commit()
        print_source_fetch_summary(f"{config.BRAND_KEY} game")
        print_phase_summary(config.BRAND_KEY, "game_transactions", game_stats, paths.game)
    else:
        trace(f"[SKIP] gameTransaction phase skipped by --entity={getattr(args, 'entity', 'all')}")

    if _load_wallet(args):
        for kind, source_key, report_path in (("deposit", "deposits", paths.deposits), ("withdrawal", "withdrawals", paths.withdrawals)):
            if source_key not in config.SOURCE_TABLES:
                trace(f"[SKIP][walletTransaction.{kind}] source table key {source_key!r} not configured for {config.BRAND_KEY}")
                continue
            wallet_stats = _blank_phase_stats()
            phase = source_key
            reset_source_fetch_summary(f"{config.BRAND_KEY} {kind}")
            _set_phase_context(config, adapter, source_key, source_key, from_dt, until_dt)
            after_dt, after_id = (None, None) if args.date_from or args.date_to else parse_checkpoint(get_checkpoint(tgt_conn, config.TARGET_SCHEMA, config.CHECKPOINT_TABLE, config.checkpoint_key(phase)) if args.resume else None)
            while True:
                rows = fetch_json_batch(src_conn, config.SOURCE_SCHEMA, config.SOURCE_TABLES[source_key], source_date_expr(adapter, source_key), after_dt, after_id, args.batch_size, from_dt, until_dt, label=f"{config.BRAND_KEY} {kind}")
                if not rows:
                    break
                wallet_stats["sourceRows"] += len(rows)
                wallet_stats["dataBatches"] += 1
                mapped_rows = []
                for row in rows:
                    mapped = adapter.map_wallet(row, kind)
                    if not mapped:
                        wallet_stats["skippedRows"] += 1
                        wallet_stats["mappingErrorRows"] += 1
                        write_phase_report(report_path, issueType="wallet_mapping_empty", category="mappingOrValidationError", sourceTable=config.SOURCE_TABLES[source_key], sourceId=row.get("id"), transactionType=kind, action="skipped", reason="adapter.map_wallet returned no mapped wallet transaction")
                        continue
                    wallet_stats["mappedRows"] += 1
                    mapped["player_id"] = None
                    mapped_rows.append(NormalizedWalletTransaction.from_mapping(mapped))
                insert_stats = insert_wallet_transactions(tgt_conn, config, mapped_rows, args.dry_run, report_path)
                _add_insert_stats(wallet_stats, insert_stats)
                last = rows[-1]
                last_data = adapter.as_dict(last.get("data"))
                after_id = str(last["id"])
                after_dt = source_created_value(adapter, last_data, source_key)
                set_checkpoint(tgt_conn, config.TARGET_SCHEMA, config.CHECKPOINT_TABLE, config.checkpoint_key(phase), format_checkpoint(after_dt, after_id), args.dry_run)
                total += len(rows)
                if not args.dry_run:
                    tgt_conn.commit()
            print_source_fetch_summary(f"{config.BRAND_KEY} {kind}")
            print_phase_summary(config.BRAND_KEY, source_key, wallet_stats, report_path)
    else:
        trace(f"[SKIP] walletTransaction phase skipped by --entity={getattr(args, 'entity', 'all')}")
    return total

def process_member_driven_brand_fact_only(src_conn, tgt_conn, ctx: MigrationContext, args) -> int:
    """Fact-only no-FK path for member-driven brands such as Instaplay/88Play."""
    adapter = ctx.adapter
    config = ctx.config
    paths = ctx.paths
    from_dt = ctx.from_dt
    until_dt = ctx.until_dt
    ensure_wallet_dedupe_index(tgt_conn, config, args.dry_run)
    processed = 0
    after_dt, after_id = (None, None) if args.date_from or args.date_to else parse_checkpoint(get_checkpoint(tgt_conn, config.TARGET_SCHEMA, config.CHECKPOINT_TABLE, config.checkpoint_key("players")) if args.resume else None)

    while True:
        rows = fetch_json_batch(src_conn, config.SOURCE_SCHEMA, config.SOURCE_TABLES["players"], source_date_expr(adapter, "players"), after_dt, after_id, args.batch_size, from_dt, until_dt, label=f"{config.BRAND_KEY} member batch")
        if not rows:
            break
        for row in rows:
            player = adapter.map_player(row, {}, src_conn=src_conn)
            if not player:
                continue
            member_id = player.get("external_id")
            username = player.get("username") or ""

            if _load_game(args):
                game_rows = adapter.fetch_member_game_rows(src_conn, member_id, username, args.tx_limit, from_dt, until_dt)
                mapped_games = []
                for game_row in game_rows:
                    mapped = adapter.map_game_transaction(game_row)
                    if mapped:
                        mapped["player_id"] = None
                        mapped_games.append(NormalizedGameTransaction.from_mapping(mapped))
                insert_game_transactions(tgt_conn, config, mapped_games, args.dry_run, paths.game)

            if _load_wallet(args):
                wallet_rows = []
                for kind in ("deposit", "withdrawal"):
                    rows_for_kind = adapter.fetch_member_wallet_rows(src_conn, kind, member_id, args.wallet_limit, from_dt, until_dt)
                    for wallet_row in rows_for_kind:
                        mapped = adapter.map_wallet(wallet_row, kind)
                        if mapped:
                            mapped["player_id"] = None
                            wallet_rows.append(NormalizedWalletTransaction.from_mapping(mapped))
                insert_wallet_transactions(tgt_conn, config, wallet_rows, args.dry_run, paths.deposits)
            processed += 1
            if processed % args.commit_every == 0:
                if args.dry_run:
                    tgt_conn.rollback()
                else:
                    tgt_conn.commit()
        last = rows[-1]
        last_data = adapter.as_dict(last.get("data"))
        after_id = str(last["id"])
        after_dt = source_created_value(adapter, last_data, "players")
        set_checkpoint(tgt_conn, config.TARGET_SCHEMA, config.CHECKPOINT_TABLE, config.checkpoint_key("players"), format_checkpoint(after_dt, after_id), args.dry_run)
        if not args.dry_run:
            tgt_conn.commit()
    return processed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--brand", required=True, help="Brand key: inplay, inplayv1, instaplay, or 1play")
    parser.add_argument("--migrate-all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--date-from", type=parse_date_arg, default=None)
    parser.add_argument("--date-to", type=parse_date_arg, default=None)
    parser.add_argument("--time-pht-aware", action="store_true", help="Treat --date-from/--date-to as Asia/Manila +08:00 business-window bounds. Default is timezone-unaware timestamp bounds.")
    parser.add_argument("--show-progress", action="store_true", help="Print lightweight batch/source-result progress so long queries do not look stuck.")
    parser.add_argument("--entity", "--object", choices=["all", "gameTransaction", "GameTransaction", "walletTransaction", "WalletTransaction", "game", "wallet"], default="all", help="Limit extraction/load to one entity. Default all. Use walletTransaction/WalletTransaction to run deposits+withdrawals only, or gameTransaction/GameTransaction to run game rows only.")
    parser.add_argument("--only", dest="entity", choices=["all", "gameTransaction", "GameTransaction", "walletTransaction", "WalletTransaction", "game", "wallet"], help=argparse.SUPPRESS)  # deprecated alias for --entity
    parser.add_argument("--batch-size", type=int, default=100, help="Low-memory default for 4GB hosts; increase only after observing free RAM")
    parser.add_argument("--commit-every", type=int, default=500, help="Low-memory default to avoid long transactions and memory pressure")
    parser.add_argument("--resume", action="store_true", default=True)
    parser.add_argument("--no-resume", dest="resume", action="store_false")
    parser.add_argument("--start-after-id", default=None)
    parser.add_argument("--tx-limit", type=int, default=50)
    parser.add_argument("--wallet-limit", type=int, default=50)
    parser.set_defaults(fact_only_no_fk=True)
    parser.add_argument("--fact-only-no-fk", dest="fact_only_no_fk", action="store_true", help=argparse.SUPPRESS)  # backward compatible; fact-only no-FK is already the default
    parser.add_argument("--add-playerDetails", "--addplayerDetails", dest="fact_only_no_fk", action="store_false", help="Legacy/full mode only: load playerDetails/outlet/dimension parent tables and enable FK-like player matching.")
    parser.add_argument("--no-fact-only-no-fk", dest="fact_only_no_fk", action="store_false", help=argparse.SUPPRESS)  # deprecated alias for --add-playerDetails
    parser.add_argument("--run-recon", action="store_true", help="Run reconciliation after migration. Off by default to protect small EC2 hosts.")
    parser.add_argument("--run-dq", action="store_true", help="Run data-quality checks after migration. Off by default to protect small EC2 hosts.")
    parser.add_argument("--recon-only", action="store_true", help="Run reconciliation only; do not migrate rows.")
    parser.add_argument("--dq-only", action="store_true", help="Run data-quality checks only; do not migrate rows.")
    parser.add_argument("--run-compare", action="store_true", help="Run source-vs-Nexus comparison after migration; writes CSV and shows variance only.")
    parser.add_argument("--compare-only", action="store_true", help="Run source-vs-Nexus comparison only; do not migrate rows.")
    parser.add_argument("--email-comparison", action="store_true", help="Email comparison CSV only when variance is found.")
    parser.add_argument("--comparison-email-to", default=None, help="Comma-separated comparison email recipients; overrides MIGRATION_REPORT_TO/COMPARISON_REPORT_TO.")
    args = parser.parse_args()

    if not args.migrate_all and not args.recon_only and not args.dq_only and not args.compare_only:
        raise SystemExit("Use --migrate-all, --recon-only, --dq-only, or --compare-only.")

    adapter, config = load_brand(args.brand)
    paths = make_report_paths(config.BRAND_KEY, args.date_from, args.date_to)
    configure_logging(paths.log)
    os.environ["ETL_TIME_PHT_AWARE"] = "1" if args.time_pht_aware else "0"
    if args.show_progress:
        os.environ["MIGRATION_LOG_SOURCE_RESULTS_EVERY_BATCH"] = "1"
    from_dt, until_dt = business_window_bounds(args.date_from, args.date_to, config.BUSINESS_WINDOW_START_HOUR, config.BUSINESS_TZ, aware=args.time_pht_aware)
    if args.time_pht_aware:
        trace(f"[DATE WINDOW][{config.BUSINESS_TZ_NAME} aware] source_from_inclusive={from_dt} source_to_exclusive={until_dt}")
    else:
        trace(f"[DATE WINDOW][timezone-unaware] source_from_inclusive={from_dt} source_to_exclusive={until_dt}")
    trace(f"[RESOURCE MODE] batch_size={args.batch_size} commit_every={args.commit_every} insert_page_size={config.INSERT_PAGE_SIZE}")
    ctx = MigrationContext(adapter=adapter, config=config, paths=paths, from_dt=from_dt, until_dt=until_dt, dry_run=args.dry_run)

    if args.fact_only_no_fk:
        os.environ["FACT_ONLY_NOFK"] = "1"
    else:
        # Explicit legacy mode only. Normal/reporting mode leaves FK/dimension IDs blank.
        os.environ["FACT_ONLY_NOFK"] = "0"

    if args.show_progress:
        trace("[RUNNING] opening source and target database connections...")
    src = connect(config.SOURCE_DB_NAME, env_prefix="SOURCE")
    tgt = connect(config.TARGET_DB_NAME, env_prefix="TARGET")
    run_started = time.perf_counter()
    try:
        total = 0
        if args.show_progress:
            trace("[RUNNING] checking target table readiness...")
        if args.fact_only_no_fk and not fact_preflight(tgt, config):
            raise SystemExit("FAILED: fact-only target table preflight failed. See log for details.")
        if args.show_progress:
            trace("[RUNNING] target table readiness complete; starting source extraction...")
        if args.migrate_all:
            if args.fact_only_no_fk:
                if config.SOURCE_MODE == "member_driven":
                    total = process_member_driven_brand_fact_only(src, tgt, ctx, args)
                elif config.SOURCE_MODE == "flat_table_batch":
                    total = process_flat_table_batch_brand(src, tgt, ctx, args)  # unchanged unless 1Play fact-only is separately requested
                else:
                    total = process_table_batch_brand_fact_only(src, tgt, ctx, args)
            elif config.SOURCE_MODE == "member_driven":
                total = process_member_driven_brand(src, tgt, ctx, args)
            elif config.SOURCE_MODE == "flat_table_batch":
                total = process_flat_table_batch_brand(src, tgt, ctx, args)
            else:
                total = process_table_batch_brand(src, tgt, ctx, args)
        if args.run_recon or args.recon_only:
            run_detailed_reconciliation(src, tgt, ctx.adapter, ctx.config, ctx.paths.reconciliation, ctx.from_dt, ctx.until_dt, include_players=(not args.fact_only_no_fk))
        if args.run_dq or args.dq_only:
            run_data_quality_checks(src, tgt, ctx.adapter, ctx.config, ctx.paths.data_quality, ctx.from_dt, ctx.until_dt, include_players=(not args.fact_only_no_fk))
        def _env_truthy(name: str, default: str = "false") -> bool:
            return str(os.getenv(name, default)).strip().lower() in ("1", "true", "yes", "y", "on")

        comparison_has_variance = False
        explicit_compare_requested = bool(args.run_compare or args.compare_only or args.email_comparison)
        auto_compare_requested = bool(
            args.migrate_all
            and not args.dry_run
            and _env_truthy("MIGRATION_AUTO_COMPARE_AFTER_RUN", "true")
        )
        run_compare_effective = explicit_compare_requested or auto_compare_requested
        email_comparison_effective = (
            args.email_comparison
            or (not args.dry_run and _env_truthy("MIGRATION_EMAIL_COMPARISON_ON_VARIANCE", "true"))
        )
        if run_compare_effective:
            comparison_phases = _selected_fact_phases(args) if args.fact_only_no_fk else None
            _, comparison_has_variance, _ = run_source_nexus_comparison(
                src,
                tgt,
                ctx.adapter,
                ctx.config,
                ctx.paths.comparison,
                ctx.from_dt,
                ctx.until_dt,
                quiet_if_matched=True,
                phases=comparison_phases,
            )
            if email_comparison_effective:
                try:
                    email_comparison_report(
                        ctx.paths.comparison, ctx.config, args.comparison_email_to, variance_only=True, has_variance=comparison_has_variance
                    )
                except Exception as exc:
                    trace(f"[COMPARISON EMAIL][ERROR] {exc}")
        if args.dry_run:
            tgt.rollback()
        else:
            tgt.commit()
        run_duration_seconds = time.perf_counter() - run_started
        trace(f"[RUN SUMMARY][{config.BRAND}] sourceProcessed={total} dryRun={args.dry_run} reconOnly={args.recon_only} dqOnly={args.dq_only} totalDurationSeconds={run_duration_seconds:.3f} totalDurationMinutes={run_duration_seconds / 60.0:.3f}")
        if args.fact_only_no_fk:
            trace(f"reports: game={paths.game} deposits={paths.deposits} withdrawals={paths.withdrawals} reconciliation={paths.reconciliation} comparison={paths.comparison}")
        else:
            trace(f"reports: players={paths.players} game={paths.game} deposits={paths.deposits} withdrawals={paths.withdrawals} reconciliation={paths.reconciliation} comparison={paths.comparison}")
    finally:
        try:
            src.close()
        finally:
            tgt.close()


if __name__ == "__main__":
    main()

import time
import os
import smtplib
from email.message import EmailMessage
from datetime import datetime
from typing import Any, Dict

from .dq_common import (
    dq_arg_parser, open_context, ensure_dq_tables, source_game_summary,
    target_game_summary, iter_source_game_chunks, build_expected_game,
    fetch_target_games, game_hash, write_summary, _selected_entity,
    TOTAL_AMOUNT_TOLERANCE, decimal_abs_diff, exceeds_total_amount_tolerance,
    DQ_STATUS_PASS, DQ_STATUS_PASS_WITH_ROUNDING_NOTE, DQ_STATUS_FAIL,
    dq_trust_result, game_rounding_only, ROW_AMOUNT_TOLERANCE,
    write_mismatches, GAME_IDENTITY_FIELDS, GAME_MONETARY_FIELDS, norm_text, decimal_abs_diff,
)


def log_current_query(task: str) -> None:
    print(f"[DQ CURRENT QUERY] {task}", flush=True)

def send_dq_failure_email(row: Dict[str, Any]) -> None:
    """Best-effort alert only for material FAIL; never alter DQ persistence/exit status."""
    if row.get("status") != DQ_STATUS_FAIL:
        return
    recipient_string = os.getenv("DQ_ALERT_TO", "")
    recipients = [r.strip() for r in recipient_string.split(",") if r.strip()]
    host = os.getenv("SMTP_HOST", "")
    username = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASSWORD", "")
    sender = os.getenv("SMTP_FROM", username)
    if not (recipients and host and sender):
        print("[DQ EMAIL][SKIP] Set DQ_ALERT_TO, SMTP_HOST and SMTP_FROM (or SMTP_USER).", flush=True)
        return
    msg = EmailMessage()
    msg["Subject"] = f"[DQ ALERT][FAIL] {row['brand']} {row['entity']} {row['window_from']}"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content("\n".join(f"{k}: {row.get(k)}" for k in (
        "run_id", "brand", "entity", "window_from", "window_to", "status",
        "trust_result", "missing_in_target", "hash_mismatch_rows", "notes",
    )))
    try:
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=30) as smtp:
            smtp.starttls()
            if username and password:
                smtp.login(username, password)
            smtp.send_message(msg)
        print(f"[DQ EMAIL][SENT] recipients={len(recipients)}", flush=True)
    except Exception as exc:
        print(f"[DQ EMAIL][ERROR] {type(exc).__name__}: {exc}", flush=True)


def main() -> int:
    parser = dq_arg_parser("Post-load DQ reconciliation summary for fact tables.")
    args = parser.parse_args()
    entity = _selected_entity(args)
    if entity != "game":
        raise SystemExit("This dq_reconcile version currently supports --entity game_transaction. Wallet support should be added after wallet source key/date columns are finalized.")
    if args.chunk_size <= 0:
        raise SystemExit("--chunk-size must be greater than zero")

    adapter, config, from_dt, until_dt, src, tgt = open_context(args)
    started = time.perf_counter()
    run_id = f"dqrecon_{config.BRAND_KEY}_game_{args.date_from}_{args.date_to}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    print(f"[DQ NUMERIC TOTAL TOLERANCE] {TOTAL_AMOUNT_TOLERANCE}")
    print(f"[DQ ROW AMOUNT TOLERANCE] {ROW_AMOUNT_TOLERANCE}")
    try:
        if not args.no_write_dq_tables:
            log_current_query(f"ensure DQ tables schema={args.dq_schema}")
            ensure_dq_tables(tgt, args.dq_schema)

        log_current_query(f"source game summary brand={config.BRAND_KEY} window={from_dt} -> {until_dt}")
        src_sum = source_game_summary(src, config, from_dt, until_dt)
        log_current_query(f"target game summary brand={config.BRAND_KEY} window={from_dt} -> {until_dt}")
        tgt_sum = target_game_summary(tgt, config, from_dt, until_dt)

        missing = 0
        hash_mismatch = 0
        rounding_hash_rows = 0
        checked = 0
        mismatch_details = []
        rounding_external_ids = []
        failed_external_ids = []
        missing_external_ids = []
        notes_id_limit = max(0, int(os.getenv("DQ_NOTES_ID_LIMIT", "100")))
        chunk_no = 0
        log_current_query(f"source game chunk scan chunk_size={args.chunk_size} window={from_dt} -> {until_dt}")
        for chunk in iter_source_game_chunks(src, config, from_dt, until_dt, args.chunk_size):
            chunk_no += 1
            expected = [build_expected_game(r, config) for r in chunk]
            expected = [e for e in expected if e.get("external_id")]
            log_current_query(f"target game lookup chunk={chunk_no} external_ids={len(expected)}")
            targets = fetch_target_games(tgt, config, [e["external_id"] for e in expected])
            for e in expected:
                checked += 1
                t = targets.get(e["external_id"])
                if not t:
                    missing += 1
                    missing_external_ids.append(e["external_id"])
                    mismatch_details.append({
                        "run_id": run_id, "brand": config.BRAND_KEY,
                        "entity": "game_transaction", "business_key": e["external_id"],
                        "mismatch_type": "MISSING_IN_TARGET", "mismatch_columns": [],
                        "source_hash": game_hash(e), "target_hash": None,
                        "source_sample": e, "target_sample": None,
                    })
                elif game_hash(e) != game_hash(t):
                    monetary_fields = [k for k in GAME_MONETARY_FIELDS
                                       if decimal_abs_diff(e.get(k), t.get(k)) > 0]
                    identity_fields = [k for k in GAME_IDENTITY_FIELDS
                                       if norm_text(e.get(k)) != norm_text(t.get(k))]
                    different_fields = identity_fields + monetary_fields
                    if game_rounding_only(e, t):
                        rounding_hash_rows += 1
                        rounding_external_ids.append(e["external_id"])
                        kind = "ROUNDING_ONLY"
                    else:
                        hash_mismatch += 1
                        failed_external_ids.append(e["external_id"])
                        kind = "HASH_MISMATCH"
                    mismatch_details.append({
                        "run_id": run_id, "brand": config.BRAND_KEY,
                        "entity": "game_transaction", "business_key": e["external_id"],
                        "mismatch_type": kind, "mismatch_columns": different_fields,
                        "source_hash": game_hash(e), "target_hash": game_hash(t),
                        "source_sample": e, "target_sample": t,
                    })
            if args.sleep_seconds > 0:
                time.sleep(args.sleep_seconds)

        duration = time.perf_counter() - started
        status = DQ_STATUS_PASS
        notes = []
        hard_failures = []
        rounding_notes = []
        if missing:
            hard_failures.append(f"missing_in_target={missing}")
        if hash_mismatch:
            hard_failures.append(f"hash_mismatch_rows={hash_mismatch}")
        if rounding_hash_rows:
            rounding_notes.append(f"rounding_only_hash_rows={rounding_hash_rows} row_amount_tolerance={ROW_AMOUNT_TOLERANCE}")
        if src_sum.get("source_rows") != tgt_sum.get("target_rows"):
            hard_failures.append(f"row_count_variance source={src_sum.get('source_rows')} target={tgt_sum.get('target_rows')}")
        if src_sum.get("source_distinct_keys") != tgt_sum.get("target_distinct_keys"):
            hard_failures.append(f"distinct_key_variance source={src_sum.get('source_distinct_keys')} target={tgt_sum.get('target_distinct_keys')}")
        amount_pairs = [
            ("total_bet", "source_total_bet", "target_total_bet"),
            ("total_valid_bet", "source_total_valid_bet", "target_total_valid_bet"),
            ("total_payout", "source_total_payout", "target_total_payout"),
            ("total_jackpot_contribution", "source_total_jackpot_contribution", "target_total_jackpot_contribution"),
        ]
        for label, sk, tk in amount_pairs:
            diff = decimal_abs_diff(src_sum.get(sk), tgt_sum.get(tk))
            if exceeds_total_amount_tolerance(src_sum.get(sk), tgt_sum.get(tk)):
                hard_failures.append(
                    f"{label}_variance source={src_sum.get(sk)} target={tgt_sum.get(tk)} "
                    f"diff={diff} tolerance={TOTAL_AMOUNT_TOLERANCE}"
                )
            elif diff > 0:
                rounding_notes.append(
                    f"{label}_rounding_note source={src_sum.get(sk)} target={tgt_sum.get(tk)} "
                    f"diff={diff} tolerance={TOTAL_AMOUNT_TOLERANCE}"
                )
        notes.extend(hard_failures)
        notes.extend(rounding_notes)
        def add_id_note(label, ids):
            if not ids:
                return
            selected = ids[:notes_id_limit]
            suffix = f" (+{len(ids) - len(selected)} more; see etl_dq_mismatch_detail)" if len(ids) > len(selected) else ""
            notes.append(f"{label}=[{', '.join(selected)}]{suffix}")
        add_id_note("rounding_external_ids", rounding_external_ids)
        add_id_note("hash_mismatch_external_ids", failed_external_ids)
        add_id_note("missing_external_ids", missing_external_ids)
        if hard_failures:
            status = DQ_STATUS_FAIL
        elif rounding_notes:
            status = DQ_STATUS_PASS_WITH_ROUNDING_NOTE
        trust_result = dq_trust_result(status)

        row: Dict[str, Any] = {
            "run_id": run_id,
            "brand": config.BRAND_KEY,
            "entity": "game_transaction",
            "window_from": from_dt,
            "window_to": until_dt,
            **src_sum,
            **tgt_sum,
            "missing_in_target": missing,
            # Cross-database extra rows are intentionally not computed in this lightweight checker.
            # Use a database-side staging approach if extra-in-target detection is required.
            "extra_in_target": 0,
            "hash_mismatch_rows": hash_mismatch,
            "status": status,
            "trust_result": trust_result,
            "notes": "; ".join(notes) if notes else "All configured DQ checks passed for this window.",
            "duration_seconds": round(duration, 3),
            "duration_minutes": round(duration / 60.0, 3),
        }
        if not args.no_write_dq_tables:
            write_summary(tgt, args.dq_schema, row)
            if mismatch_details:
                log_current_query(f"write DQ mismatch details count={len(mismatch_details)}")
                write_mismatches(tgt, args.dq_schema, mismatch_details)
            send_dq_failure_email(row)
        print("[DQ RECON SUMMARY]")
        print(f"status: {status}")
        print(f"trust_result: {trust_result}")
        print(f"checked_source_rows_for_hash: {checked}")
        for k in sorted(row):
            print(f"{k}: {row[k]}")
        return 0 if status in (DQ_STATUS_PASS, DQ_STATUS_PASS_WITH_ROUNDING_NOTE) else 2
    finally:
        src.close(); tgt.close()


if __name__ == "__main__":
    raise SystemExit(main())

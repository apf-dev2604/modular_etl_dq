import csv
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from .logger import setup_logger, event
from typing import Any, Dict, Iterable, List, Optional
from collections import Counter, defaultdict


PHASE_SUMMARIES: Dict[str, Dict[str, Any]] = {}
PHASE_REPORT_CONTEXT: Dict[str, Any] = {}

def set_phase_report_context(**fields: Any) -> None:
    PHASE_REPORT_CONTEXT.clear()
    PHASE_REPORT_CONTEXT.update({k: v for k, v in fields.items() if v is not None})

def clear_phase_report_context() -> None:
    PHASE_REPORT_CONTEXT.clear()

def get_phase_summaries() -> Dict[str, Dict[str, Any]]:
    return {k: dict(v) for k, v in PHASE_SUMMARIES.items()}


def configure_logging(path: str) -> None:
    setup_logger(path)


def trace(message: str, level: int = 20, **fields: Any) -> None:
    event(message, level=level, **fields)


def short_text(value: Any, limit: int = 300) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."


def human_date_label(value: Any) -> str:
    """Return a compact human label for a SQL date expression.

    This keeps reports readable by showing GameDate/transferDate/createddate
    instead of full expressions such as immutable_json_timestamp(NULLIF(...)).
    """
    text = short_text(value, 500)
    if not text:
        return ""
    # Prefer JSONB key labels like data->>'GameDate'.
    matches = re.findall(r"->>\s*'([^']+)'", text)
    if matches:
        return " or ".join(dict.fromkeys(matches))
    # Prefer quoted target/source column labels.
    qmatches = re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', text)
    if qmatches:
        return " or ".join(dict.fromkeys(qmatches))
    return text


def write_csv_row(path: str, fieldnames: List[str], row: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    exists = os.path.isfile(path)
    with open(path, "a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)


@dataclass
class ReportPaths:
    log: str
    players: str
    game: str
    deposits: str
    withdrawals: str
    reconciliation: str
    data_quality: str
    comparison: str


def make_report_paths(brand_key: str, date_from, date_to, timestamp: Optional[str] = None) -> ReportPaths:
    timestamp = timestamp or datetime.now().strftime("%Y%m%d%H%M%S")

    def part(value, fallback):
        return value.strftime("%Y%m%d") if value else fallback

    if date_from is None and date_to is None:
        suffix = f"full_rundate_{timestamp}"
        dq_suffix = suffix
    else:
        suffix = f"{part(date_from, '00000000')}_{part(date_to, '99999999')}-rundate_{timestamp}"
        dq_suffix = f"{part(date_from, '00000000')}-{part(date_to, '99999999')}-rundate_{timestamp}"

    base = brand_key.lower()
    return ReportPaths(
        log=f"logs/{base}_trace_{suffix}.log",
        players=f"reports/{base}_players_{suffix}.csv",
        game=f"reports/{base}_gameTransaction_{suffix}.csv",
        deposits=f"reports/{base}_deposits_{suffix}.csv",
        withdrawals=f"reports/{base}_withdrawals_{suffix}.csv",
        reconciliation=f"reports/{base}_reconciliation_{suffix}.csv",
        data_quality=f"reports/dataQuality_{base}_{dq_suffix}.csv",
        comparison=f"reports/{base}_sourceNexusComparison_{suffix}.csv",
    )


PHASE_FIELDS = [
    "issueType",
    "category",
    "phase",
    "sourceDatabase",
    "sourceSchema",
    "sourceTable",
    "sourceDateField",
    "sourceWindowDateColumn",
    "targetWindowDateColumn",
    "nexusWindowDateColumn",
    "sourceWindowFromInclusive",
    "sourceWindowToExclusive",
    "sourceUsername",
    "sourcePlayerId",
    "sourcePlayerAccount",
    "sourceId",
    "referenceId",
    "sourceExternalId",
    "sourceDate",
    "src_date",
    "targetId",
    "targetPlayerId",
    "targetUsername",
    "targetDate",
    "nexusWindowDateColumnValue",
    "nexusExternalId",
    "nexusReferenceId",
    "transactionType",
    "platform",
    "action",
    "reason",
    "sqlstate",
    "constraintName",
    "tableName",
    "columnName",
    "messageDetail",
    "error",
]


EXCEPTION_FIELDS = PHASE_FIELDS + [
    "exceptionCode",
    "exceptionSeverity",
    "exceptionMessage",
    "insertDisposition",
    "isTrueMissingFromFinal",
    "isExistingFinalRow",
    "sourceDateColumnLabel",
]


def exception_report_path(phase_report_path: str) -> str:
    """Return brand_phase_migration_exception_* path beside the phase report.

    Example:
      reports/inplayv1_deposits_20251103_20251104-rundate_20260617.csv
      -> reports/inplayv1_deposits_migration_exception_20251103_20251104-rundate_20260617.csv
    """
    if not phase_report_path:
        return phase_report_path
    directory = os.path.dirname(phase_report_path) or "."
    name = os.path.basename(phase_report_path)
    if name.lower().endswith(".csv"):
        name = name[:-4]
    parts = name.split("_", 2)
    if len(parts) >= 3:
        new_name = f"{parts[0]}_{parts[1]}_migration_exception_{parts[2]}.csv"
    else:
        new_name = f"{name}_migration_exception.csv"
    return os.path.join(directory, new_name)


def _exception_details(row: Dict[str, Any]) -> Dict[str, Any]:
    issue = str(row.get("issueType") or "").strip()
    category = str(row.get("category") or "").strip()
    action = str(row.get("action") or "").strip()
    reason = str(row.get("reason") or "").strip()
    target_date = str(row.get("targetDate") or "").strip()
    target_id = str(row.get("targetId") or "").strip()
    target_player_id = str(row.get("targetPlayerId") or "").strip()
    source_date_field = human_date_label(row.get("sourceDateField") or row.get("sourceWindowDateColumn") or "")

    code = issue or action or category or "unclassified_migration_exception"
    severity = "warning"
    disposition = "not_newly_inserted"
    is_true_missing = "false"
    is_existing_final = "false"

    if action == "insert_error" or "insert_error" in issue or category == "databaseInsertError":
        severity = "error"
        disposition = "insert_failed"
        code = issue or "database_insert_error"
        message = reason or "Insert failed due to database exception. Check sqlstate/constraintName/messageDetail/error."
    elif "missing_username" in issue or category == "missingUsername":
        severity = "error"
        disposition = "skipped_before_insert"
        code = issue or "missing_username"
        message = reason or "Skipped before insert because source username/player account was missing after mapping."
    elif "player_not_in_player_map" in issue or category == "missingTargetPlayer":
        severity = "error"
        disposition = "skipped_before_insert"
        code = issue or "missing_target_player"
        is_true_missing = "true"
        message = reason or "Skipped before insert because no matching playerDetails_final row was found."
    elif "missing_required" in issue or category == "missingRequired":
        severity = "error"
        disposition = "skipped_before_insert"
        code = issue or "missing_required_field"
        message = reason or "Skipped before insert because a required mapped field was missing."
    elif "mapping" in issue or category == "mappingOrValidationError":
        severity = "error"
        disposition = "skipped_before_insert"
        code = issue or "mapping_or_validation_error"
        message = reason or "Skipped before insert because adapter mapping returned empty/invalid data."
    elif action == "duplicate_key_ignored" or "duplicate" in issue.lower() or category in ("duplicateSkipped", "duplicateIgnored"):
        severity = "warning"
        disposition = "ignored_by_on_conflict_do_nothing"
        is_existing_final = "true" if (target_id or target_date or target_player_id) else "unknown"
        code = issue or "duplicate_key_ignored"
        if target_date:
            message = (
                "Not newly inserted because a configured duplicate/business key already exists in the final table. "
                "ON CONFLICT DO NOTHING ignored this source row. targetDate identifies the existing final-table row; compare targetDate to the source window."
            )
        else:
            message = (
                "Not newly inserted because a configured duplicate/business key already exists or was detected by ON CONFLICT DO NOTHING. "
                "No targetDate was available in the lookup."
            )
    elif action == "updated_existing_row" or category == "updatedExisting":
        severity = "info"
        disposition = "updated_existing_final_row"
        is_existing_final = "true"
        code = issue or "existing_row_updated_not_inserted"
        message = reason or "Not newly inserted because the final-table row already existed; migration updated the existing row."
    elif action == "skipped":
        severity = "warning"
        disposition = "skipped_before_insert"
        code = issue or category or "skipped_before_insert"
        message = reason or "Skipped before insert."
    else:
        message = reason or "Row was not newly inserted; review action/category/issueType."

    return {
        "exceptionCode": code,
        "exceptionSeverity": severity,
        "exceptionMessage": message,
        "insertDisposition": disposition,
        "isTrueMissingFromFinal": is_true_missing,
        "isExistingFinalRow": is_existing_final,
        "sourceDateColumnLabel": source_date_field,
    }


def write_exception_report(phase_report_path: str, row: Dict[str, Any]) -> None:
    if not phase_report_path:
        return
    details = _exception_details(row)
    out = dict(row)
    out.update(details)
    path = exception_report_path(phase_report_path)
    write_csv_row(path, EXCEPTION_FIELDS, {name: short_text(out.get(name), 700 if name in ("reason", "error", "messageDetail", "exceptionMessage") else 300) for name in EXCEPTION_FIELDS})


RECON_FIELDS = [
    "checkName",
    "recordType",
    "status",
    "sourceTable",
    "sourceId",
    "sourceUsername",
    "sourceExternalId",
    "targetTable",
    "targetId",
    "targetUsername",
    "targetExternalId",
    "referenceType",
    "referenceValue",
    "metric",
    "value",
    "reason",
    "notes",
    "timestamp",
]


def db_error_parts(exc: Any) -> Dict[str, Any]:
    diag = getattr(exc, "diag", None)
    return {
        "sqlstate": getattr(exc, "pgcode", "") or "",
        "constraintName": getattr(diag, "constraint_name", "") or "",
        "tableName": getattr(diag, "table_name", "") or "",
        "columnName": getattr(diag, "column_name", "") or "",
        "messageDetail": getattr(diag, "message_detail", "") or "",
        "error": str(exc or ""),
    }


def write_phase_report(path: str, **kwargs: Any) -> None:
    merged = dict(PHASE_REPORT_CONTEXT)
    merged.update(kwargs)
    # Keep report columns human-readable; do not expose full SQL timestamp expressions in CSV.
    date_label = human_date_label(merged.get("sourceDateField") or merged.get("sourceWindowDateColumn") or "")
    if date_label:
        merged["sourceDateField"] = date_label
        merged["sourceWindowDateColumn"] = date_label
    if not merged.get("targetWindowDateColumn"):
        merged["targetWindowDateColumn"] = merged.get("nexusWindowDateColumn") or ""
    if not merged.get("nexusWindowDateColumn"):
        merged["nexusWindowDateColumn"] = merged.get("targetWindowDateColumn") or ""
    if not merged.get("sourcePlayerAccount"):
        merged["sourcePlayerAccount"] = merged.get("sourceUsername") or merged.get("targetUsername") or ""
    if not merged.get("sourceExternalId"):
        merged["sourceExternalId"] = merged.get("referenceId") or merged.get("sourcePlayerId") or ""
    if not merged.get("src_date"):
        merged["src_date"] = merged.get("sourceDate") or ""
    if not merged.get("nexusWindowDateColumnValue"):
        merged["nexusWindowDateColumnValue"] = merged.get("targetDate") or ""
    if not merged.get("nexusExternalId"):
        merged["nexusExternalId"] = merged.get("targetBusinessKey") or merged.get("referenceId") or ""
    if not merged.get("nexusReferenceId"):
        merged["nexusReferenceId"] = merged.get("targetBusinessKey") or merged.get("referenceId") or ""
    row = {name: short_text(merged.get(name), 500 if name in ("reason", "error", "messageDetail") else 300) for name in PHASE_FIELDS}
    write_csv_row(path, PHASE_FIELDS, row)
    # Also write all non-new-insert accounting rows to a dedicated migration-exception CSV.
    # This file is intentionally redundant with the phase CSV but is easier to audit manually.
    write_exception_report(path, row)


def write_reconciliation(path: str, **kwargs: Any) -> None:
    row = {name: short_text(kwargs.get(name), 500) for name in RECON_FIELDS}
    row["timestamp"] = datetime.now(timezone.utc).isoformat()
    write_csv_row(path, RECON_FIELDS, row)


def _phase_csv_group_counts(path: Optional[str]) -> Dict[str, Counter]:
    grouped: Dict[str, Counter] = {
        "issueType": Counter(),
        "category": Counter(),
        "action": Counter(),
        "reason": Counter(),
        "exceptionCode": Counter(),
        "exceptionSeverity": Counter(),
        "insertDisposition": Counter(),
    }
    if not path:
        return grouped
    # Prefer the dedicated exception CSV because it has normalized exception fields.
    read_path = exception_report_path(path)
    if not os.path.isfile(read_path):
        read_path = path
    if not os.path.isfile(read_path):
        return grouped
    try:
        with open(read_path, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                for key in grouped:
                    value = short_text((row.get(key) or "blank").strip() or "blank", 180)
                    grouped[key][value] += 1
    except Exception as exc:
        trace(f"[PHASE ACCOUNTING GROUPS][WARN] unable to summarize csv={read_path} error={exc}")
    return grouped


def print_phase_accounting_groups(brand_key: str, phase: str, report_path: Optional[str], max_items: int = 20) -> None:
    grouped = _phase_csv_group_counts(report_path)
    total_rows = sum(grouped["action"].values()) if grouped.get("action") else 0
    if total_rows <= 0:
        trace(f"[PHASE ACCOUNTING GROUPS][{brand_key} {phase}] no row-level accounting rows csv={report_path or ''}")
        return
    trace(f"[PHASE ACCOUNTING GROUPS][{brand_key} {phase}]")
    trace(f"  csv                 : {report_path}")
    trace(f"  exceptionCsv        : {exception_report_path(report_path) if report_path else ''}")
    trace(f"  csvRows             : {total_rows}")
    for group_name in ["exceptionSeverity", "insertDisposition", "exceptionCode", "category", "action", "issueType", "reason"]:
        counter = grouped.get(group_name) or Counter()
        if not counter:
            continue
        trace(f"  by{group_name[:1].upper() + group_name[1:]}:")
        for value, count in counter.most_common(max_items):
            trace(f"    {value}: {count}")
        remaining = len(counter) - max_items
        if remaining > 0:
            trace(f"    ... {remaining} more {group_name} values in CSV")


def print_phase_summary(brand_key: str, phase: str, stats: Dict[str, Any], report_path: Optional[str] = None) -> None:
    total_errors = int(stats.get("missingPlayerRows", 0) or 0) + int(stats.get("missingUsernameRows", 0) or 0) + int(stats.get("missingRequiredRows", 0) or 0) + int(stats.get("mappingErrorRows", 0) or 0) + int(stats.get("insertErrorRows", 0) or 0)
    stats = dict(stats)
    stats["totalErrorRows"] = total_errors
    PHASE_SUMMARIES[str(phase)] = dict(stats)
    trace(f"[PHASE SUMMARY][{brand_key} {phase}]")
    for key in [
        "sourceRows", "mappedRows", "insertAttemptRows", "insertedRows", "updatedRows",
        "duplicateSkippedRows", "skippedRows", "missingPlayerRows", "missingUsernameRows",
        "missingRequiredRows", "mappingErrorRows", "insertErrorRows", "totalErrorRows", "dataBatches",
        "importDurationSeconds", "importDurationMinutes", "columnDqCheckedRows",
        "columnVarianceRows", "columnVarianceColumns", "columnVarianceColumnList",
    ]:
        trace(f"  {key:<22}: {stats.get(key, 0)}")
    # Display grouped row-level accounting/exception reasons from the phase CSV.
    # This is deliberately read-only and uses the CSV already written by the migration.
    print_phase_accounting_groups(brand_key, phase, report_path)

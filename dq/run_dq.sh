#!/usr/bin/env bash
# Run existing ETL synchronously, wait 10 minutes after SUCCESS, then run DQ.
# Edit the configuration below; no .bash_profile or cron.env.sh required.
# Usage (daily): run_etl_then_dq.sh YYYY-MM-DD YYYY-MM-DD
# Usage (manual override): run_etl_then_dq.sh YYYY-MM-DD YYYY-MM-DD -- /path/to/existing_etl_launcher [arguments ...]
set -Eeuo pipefail

# ======================== EDIT CONFIGURATION HERE ========================
PROJECT_DIR="~/scripts/modular_etl_dq"  # Root with helpers/ and existing .env
ENV_FILE="$PROJECT_DIR/.env"              # Existing DB/SMTP secrets; do NOT copy them here
PYTHON_BIN="~/scripts/.venv/bin/python"                      # Set to your working venv Python path if applicable
#ETL_LAUNCHER="$PROJECT_DIR/dq/run_daily_dqrun.sh" # Existing synchronous ETL launcher; REQUIRED for daily cron
DQ_BRAND="inplayv1"                      # Brand passed to DQ
DQ_ENTITY="gameTransaction"              # Entity passed to DQ
DQ_CHUNK_SIZE="10000"                    # Previously tested batch size
DQ_WAIT_SECONDS="600"                    # 600 seconds = 10 minutes AFTER ETL succeeds
DQ_ROW_AMOUNT_TOLERANCE="0.65"           # Allowed per-row monetary rounding variance
DQ_TOTAL_AMOUNT_TOLERANCE="0.01"         # Aggregate monetary-total rounding tolerance
DQ_LOCK_FILE="/tmp/nexus_etl_daily_dq.lock"  # Prevent overlapping pipeline runs
DQ_LOG_DIR="$PROJECT_DIR/logs/dq_automation" # Persistent ETL/DQ logs
# ========================================================================

usage() { echo "Usage: $0 YYYY-MM-DD YYYY-MM-DD [-- ETL_COMMAND [ARGS...]]" >&2; exit 64; }
(( $# >= 2 )) || usage
DATE_FROM="$1"; DATE_TO="$2"; shift 2
for day in "$DATE_FROM" "$DATE_TO"; do
  [[ "$day" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] && [[ "$(date -d "$day" +%F 2>/dev/null || true)" == "$day" ]] || usage
done
[[ "$DATE_FROM" < "$DATE_TO" ]] || usage
if (( $# > 0 )); then
  [[ "$1" == "--" ]] || usage
  shift
  (( $# > 0 )) || usage
  ETL_COMMAND=("$@")
else
  ETL_COMMAND=("$ETL_LAUNCHER")
fi

[[ -d "$PROJECT_DIR/helpers" ]] || { echo "Missing $PROJECT_DIR/helpers" >&2; exit 66; }
[[ "$DQ_CHUNK_SIZE" =~ ^[1-9][0-9]*$ && "$DQ_WAIT_SECONDS" =~ ^[0-9]+$ ]] || { echo 'Invalid chunk size or wait seconds' >&2; exit 64; }
[[ -f "$ENV_FILE" ]] || { echo "Missing .env file: $ENV_FILE" >&2; exit 66; }
[[ -r "$ENV_FILE" ]] || { echo "Cannot read .env file: $ENV_FILE" >&2; exit 66; }
[[ -x "${ETL_COMMAND[0]}" ]] || { echo "ETL command not executable: ${ETL_COMMAND[0]}" >&2; exit 66; }

# IMPORTANT: This expects a SHELL-COMPATIBLE .env containing KEY=VALUE assignments.
# If your .env uses dotenv-only syntax, do not source it; adapt loader to parse .env.
# set -a exports values to both child Python processes without logging secrets.
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
# This script's DQ controls intentionally override any same-named .env values.
export DQ_ROW_AMOUNT_TOLERANCE DQ_TOTAL_AMOUNT_TOLERANCE

mkdir -p "$DQ_LOG_DIR"
exec 9>"$DQ_LOCK_FILE"
if ! flock -n 9; then echo "[PIPELINE] Another ETL/DQ run is active" >&2; exit 75; fi
LOG_FILE="$DQ_LOG_DIR/pipeline_${DATE_FROM}_${DATE_TO}_$(date +%Y%m%dT%H%M%S).log"
exec >>"$LOG_FILE" 2>&1
printf '[%s] [PIPELINE] Starting window %s -> %s\n' "$(date -Is)" "$DATE_FROM" "$DATE_TO"
printf '[%s] [ETL] Running existing launcher\n' "$(date -Is)"
if "${ETL_COMMAND[@]}"; then
  printf '[%s] [ETL] Succeeded; all worker commits/checkpoints must be complete\n' "$(date -Is)"
else
  rc=$?
  printf '[%s] [ETL] Failed exit=%d; skipping DQ\n' "$(date -Is)" "$rc"
  exit "$rc"
fi
printf '[%s] [DQ] Waiting %s seconds after ETL completion\n' "$(date -Is)" "$DQ_WAIT_SECONDS"
sleep "$DQ_WAIT_SECONDS"
cd "$PROJECT_DIR"
printf '[%s] [DQ] Reconciling brand=%s entity=%s chunk=%s tolerance(row=%s,total=%s)\n' "$(date -Is)" "$DQ_BRAND" "$DQ_ENTITY" "$DQ_CHUNK_SIZE" "$DQ_ROW_AMOUNT_TOLERANCE" "$DQ_TOTAL_AMOUNT_TOLERANCE"
# Existing Python DQ code writes and commits its summary and mismatch details.
# Do not add a separate SQL insert/commit here.
if "$PYTHON_BIN" -m helpers.dq_reconcile \
    --brand "$DQ_BRAND" --entity "$DQ_ENTITY" \
    --date-from "$DATE_FROM" --date-to "$DATE_TO" \
    --chunk-size "$DQ_CHUNK_SIZE"; then
  printf '[%s] [DQ] Reportable result saved (PASS or PASS_WITH_ROUNDING_NOTE)\n' "$(date -Is)"
else
  rc=$?
  printf '[%s] [DQ] Nonreportable or error exit=%d; check DB and logs\n' "$(date -Is)" "$rc"
  exit "$rc"
fi

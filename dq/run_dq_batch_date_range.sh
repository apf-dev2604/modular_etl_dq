
#!/usr/bin/env bash
set -Eeuo pipefail

# ============================================================
# NEXUS ETL - DQ DATE RANGE RUNNER
#
# Purpose:
#   Run DQ reconciliation for one or multiple business days.
#   Each window starts at 06:00 and ends at 06:00 next day.
#   The ending date is EXCLUSIVE.
#
# This script does NOT execute ETL imports.
# ============================================================

# ================= CONFIGURATION ============================

# Project directory
PROJECT_DIR="/home/allanf/scripts/modular_etl_dq"

# Python virtual environment
PYTHON_BIN="/home/allanf/scripts/.venv/bin/python"

# Existing database and SMTP configuration
ENV_FILE="$PROJECT_DIR/.env"

# Default settings
DEFAULT_BRAND="inplayv1"
DEFAULT_ENTITY="gameTransaction"
DEFAULT_CHUNK_SIZE=10000

# DQ monetary tolerances
export DQ_ROW_AMOUNT_TOLERANCE="0.65"
export DQ_TOTAL_AMOUNT_TOLERANCE="0.01"

# Maximum number of external IDs shown in DQ notes
export DQ_NOTES_ID_LIMIT="100"

# Logs
LOG_DIR="$PROJECT_DIR/dq/logs"

# Prevent overlapping DQ executions
LOCK_FILE="/tmp/modular_etl_dq.lock"

# ============================================================
# COMMAND LINE ARGUMENTS
# ============================================================

BRAND="$DEFAULT_BRAND"
ENTITY="$DEFAULT_ENTITY"
CHUNK_SIZE="$DEFAULT_CHUNK_SIZE"
DATE_FROM=""
DATE_TO=""

usage() {
    cat <<EOF
Usage:
  $0 --brand BRAND --entity ENTITY \
     --date-from YYYY-MM-DD \
     --date-to YYYY-MM-DD \
     [--chunk-size N]

Example:
  $0 --brand inplayv1 \
     --entity gameTransaction \
     --date-from 2024-11-01 \
     --date-to 2024-11-02 \
     --chunk-size 10000

Date range:
  From: YYYY-MM-DD 06:00:00 (inclusive)
  To:   YYYY-MM-DD 06:00:00 (exclusive)
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --brand)
            [[ $# -ge 2 ]] || { usage; exit 64; }
            BRAND="$2"
            shift 2
            ;;
        --entity)
            [[ $# -ge 2 ]] || { usage; exit 64; }
            ENTITY="$2"
            shift 2
            ;;
        --date-from)
            [[ $# -ge 2 ]] || { usage; exit 64; }
            DATE_FROM="$2"
            shift 2
            ;;
        --date-to)
            [[ $# -ge 2 ]] || { usage; exit 64; }
            DATE_TO="$2"
            shift 2
            ;;
        --chunk-size)
            [[ $# -ge 2 ]] || { usage; exit 64; }
            CHUNK_SIZE="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "[ERROR] Unknown argument: $1"
            usage
            exit 64
            ;;
    esac
done

# ============================================================
# VALIDATION
# ============================================================

if [[ -z "$DATE_FROM" || -z "$DATE_TO" ]]; then
    echo "[ERROR] --date-from and --date-to are required."
    usage
    exit 64
fi

# Validate date format and actual calendar dates
for DAY in "$DATE_FROM" "$DATE_TO"; do
    if [[ ! "$DAY" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] ||
       [[ "$(date -d "$DAY" +%F 2>/dev/null)" != "$DAY" ]]; then
        echo "[ERROR] Invalid date: $DAY"
        exit 64
    fi
done

if [[ ! "$DATE_FROM" < "$DATE_TO" ]]; then
    echo "[ERROR] DATE_TO must be later than DATE_FROM."
    exit 64
fi

if [[ ! "$CHUNK_SIZE" =~ ^[0-9]+$ ]] ||
   (( 10#$CHUNK_SIZE <= 0 )); then
    echo "[ERROR] Chunk size must be greater than zero."
    exit 64
fi

# Current Python implementation supports gameTransaction only.
case "$ENTITY" in
    gameTransaction|game_transaction)
        ENTITY="gameTransaction"
        ;;
    walletTransaction|wallet_transaction)
        echo "[ERROR] Wallet DQ is not yet implemented in helpers.dq_reconcile."
        exit 64
        ;;
    *)
        echo "[ERROR] Unsupported entity: $ENTITY"
        exit 64
        ;;
esac

# ============================================================
# ENVIRONMENT
# ============================================================

if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "[ERROR] Python executable not found: $PYTHON_BIN"
    exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
    echo "[ERROR] Missing .env file: $ENV_FILE"
    exit 1
fi

mkdir -p "$LOG_DIR"

# Prevent two DQ range scripts from running simultaneously
exec 9>"$LOCK_FILE"

if ! flock -n 9; then
    echo "[ERROR] Another DQ process is already running."
    exit 1
fi

cd "$PROJECT_DIR"

# Load existing .env file.
# This requires a Bash-compatible .env file.
set -a
source "$ENV_FILE"
set +a

# ============================================================
# START DQ
# ============================================================

echo "======================================================"
echo "          DQ RECONCILIATION DATE RANGE"
echo "======================================================"
echo "Brand          : $BRAND"
echo "Entity         : $ENTITY"
echo "Date From      : $DATE_FROM 06:00:00"
echo "Date To        : $DATE_TO 06:00:00 (exclusive)"
echo "Chunk Size     : $CHUNK_SIZE"
echo "Row Tolerance  : $DQ_ROW_AMOUNT_TOLERANCE"
echo "Total Tolerance: $DQ_TOTAL_AMOUNT_TOLERANCE"
echo "======================================================"

CURRENT="$DATE_FROM"

PASS_COUNT=0
FAIL_COUNT=0
ERROR_COUNT=0

# ============================================================
# PROCESS ONE BUSINESS DAY AT A TIME
# ============================================================

while [[ "$CURRENT" < "$DATE_TO" ]]; do

    NEXT="$(date -d "$CURRENT + 1 day" +%F)"

    LOG_FILE="$LOG_DIR/dq_${BRAND}_${ENTITY}_${CURRENT}.log"

    echo
    echo "======================================================"
    echo "[DQ START] $(date '+%Y-%m-%d %H:%M:%S')"
    echo "[DQ WINDOW] $CURRENT 06:00:00 -> $NEXT 06:00:00"
    echo "[DQ LOG] $LOG_FILE"
    echo "======================================================"

    # Existing Python DQ handles:
    # - Source/target counts and totals
    # - Transaction hash comparison
    # - Monetary rounding tolerance
    # - DQ summary and mismatch detail writes
    # - Email alerts for material failures

    set +e

    "$PYTHON_BIN" -m helpers.dq_reconcile \
        --brand "$BRAND" \
        --entity "$ENTITY" \
        --date-from "$CURRENT" \
        --date-to "$NEXT" \
        --chunk-size "$CHUNK_SIZE" \
        2>&1 | tee "$LOG_FILE"

    RC=${PIPESTATUS[0]}

    set -e

    # Exit codes:
    # 0 = PASS or PASS_WITH_ROUNDING_NOTE
    # 2 = DQ FAIL
    # Others = execution errors

    case "$RC" in
        0)
            PASS_COUNT=$((PASS_COUNT + 1))
            echo "[DQ COMPLETED] $CURRENT"
            ;;
        2)
            FAIL_COUNT=$((FAIL_COUNT + 1))
            echo "[DQ FAILED] $CURRENT - Data mismatch"
            ;;
        *)
            ERROR_COUNT=$((ERROR_COUNT + 1))
            echo "[DQ ERROR] $CURRENT - Exit code $RC"
            echo "[DQ STOPPED] Subsequent dates not processed."
            exit "$RC"
            ;;
    esac

    # Python exits after each day, releasing its memory
    # and closing its database connections.
    CURRENT="$NEXT"

done

# ============================================================
# FINAL SUMMARY
# ============================================================

echo
echo "======================================================"
echo "                 DQ RANGE SUMMARY"
echo "======================================================"
echo "Brand                  : $BRAND"
echo "Entity                 : $ENTITY"
echo "Successful/DQ note     : $PASS_COUNT"
echo "DQ failed windows      : $FAIL_COUNT"
echo "Execution errors       : $ERROR_COUNT"
echo "======================================================"

if (( FAIL_COUNT > 0 )); then
    exit 2
fi

exit 0


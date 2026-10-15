#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# CONFIG - CHANGE THESE ONLY
# =============================================================================

PROJECT_DIR="/home/allanf/scripts/modular_etl_dq"

BRAND="inplayv1"

# Options:
#   all
#   gameTransaction
#   walletTransaction
ENTITY="gameTransaction"

START_DATE="2024-11-03"
END_DATE="2024-12-01"

BATCH_SIZE="1000"
COMMIT_EVERY="1000"

# 0 = live run
# 1 = dry run only
DRY_RUN="1"

# 0 = plain timestamp, example: 2024-11-01 06:00:00
# 1 = Asia/Manila aware, example: 2024-11-01 06:00:00+08:00
TIME_PHT_AWARE="0"

# Optional sleep between each date run
SLEEP_SECONDS="5"

# =============================================================================
# DO NOT CHANGE BELOW UNLESS NEEDED
# =============================================================================

cd "$PROJECT_DIR"

CURRENT_DATE="$START_DATE"

echo "============================================================"
echo "ETL date-loop started"
echo "Project dir : $PROJECT_DIR"
echo "Brand       : $BRAND"
echo "Entity      : $ENTITY"
echo "Start date  : $START_DATE"
echo "End date    : $END_DATE"
echo "Dry run     : $DRY_RUN"
echo "============================================================"

while [[ "$CURRENT_DATE" < "$END_DATE" ]]; do
    NEXT_DATE="$(date -d "$CURRENT_DATE + 1 day" +%F)"

    echo
    echo "============================================================"
    echo "Running ETL window"
    echo "From date : $CURRENT_DATE 06:00:00"
    echo "To date   : $NEXT_DATE 06:00:00"
    echo "Entity    : $ENTITY"
    echo "============================================================"

    CMD=(
        python -m helpers.main
        --brand "$BRAND"
        --migrate-all
        --date-from "$CURRENT_DATE"
        --date-to "$NEXT_DATE"
        --batch-size "$BATCH_SIZE"
        --commit-every "$COMMIT_EVERY"
        --entity "$ENTITY"
    )

    if [[ "$DRY_RUN" == "1" ]]; then
        CMD+=(--dry-run)
    fi

    if [[ "$TIME_PHT_AWARE" == "1" ]]; then
        CMD+=(--time-pht-aware)
    fi

    echo "Command:"
    printf ' %q' "${CMD[@]}"
    echo
    echo

    "${CMD[@]}"

    RC=$?

    if [[ "$RC" -ne 0 ]]; then
        echo
        echo "FAILED: $CURRENT_DATE to $NEXT_DATE"
        echo "Exit code: $RC"
        exit "$RC"
    fi

    echo
    echo "SUCCESS: $CURRENT_DATE to $NEXT_DATE"

    CURRENT_DATE="$NEXT_DATE"

    if [[ "$SLEEP_SECONDS" -gt 0 ]]; then
        echo "Sleeping ${SLEEP_SECONDS}s before next window..."
        sleep "$SLEEP_SECONDS"
    fi
done

echo
echo "============================================================"
echo "ETL date-loop completed successfully"
echo "From : $START_DATE"
echo "To   : $END_DATE"
echo "============================================================"

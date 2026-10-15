#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/home/allanf/scripts/modular_etl_dq"
BRAND="inplayv1"
ENTITY="gameTransaction"
START_DATE="2024-11-03"
END_DATE="2024-12-01"
CHUNK_SIZE="500"
SLEEP_SECONDS="0.05"
MAX_MISMATCHES="1000"

cd "$PROJECT_DIR"
CURRENT_DATE="$START_DATE"
while [[ "$CURRENT_DATE" < "$END_DATE" ]]; do
    NEXT_DATE="$(date -d "$CURRENT_DATE + 1 day" +%F)"
    echo "============================================================"
    echo "DQ window: $CURRENT_DATE 06:00:00 to $NEXT_DATE 06:00:00"
    echo "============================================================"
    python -m helpers.dq_reconcile \
      --brand "$BRAND" \
      --entity "$ENTITY" \
      --date-from "$CURRENT_DATE" \
      --date-to "$NEXT_DATE" \
      --chunk-size "$CHUNK_SIZE" \
      --dq-schema db_admin \
      --sleep-seconds "$SLEEP_SECONDS" || true

    python -m helpers.dq_deep_compare \
      --brand "$BRAND" \
      --entity "$ENTITY" \
      --date-from "$CURRENT_DATE" \
      --date-to "$NEXT_DATE" \
      --chunk-size "$CHUNK_SIZE" \
      --dq-schema db_admin \
      --sleep-seconds "$SLEEP_SECONDS" \
      --max-mismatches "$MAX_MISMATCHES" || true

    CURRENT_DATE="$NEXT_DATE"
done

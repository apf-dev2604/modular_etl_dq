#!/usr/bin/env bash
# Cron entrypoint: run the configured ETL, then DQ, for the prior 06:00-06:00 day.
# # Schedule AFTER 06:00 Asia/Manila. All operational settings are in run_etl_then_dq.sh.
# set -Eeuo pipefail
# export TZ="Asia/Manila"   # Determines which dates count as T-1
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATE_FROM="$(date -d yesterday +%F)"
DATE_TO="$(date +%F)"
exec "$SCRIPT_DIR/run_dq.sh" "$DATE_FROM" "$DATE_TO"
#

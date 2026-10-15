#!/bin/bash
set -euo pipefail

cd ~/scripts/nexus_etl

# Historical DQ window:
# >= 2024-11-01 06:00:00
# <  2024-11-02 06:00:00

export DQ_ROW_AMOUNT_TOLERANCE=0.65
export DQ_TOTAL_AMOUNT_TOLERANCE=0.01

~/scripts/.venv/bin/python -m helpers.dq_reconcile \
  --brand inplayv1 \
  --entity gameTransaction \
  --date-from 2024-11-01 \
  --date-to 2024-11-02 \
  --chunk-size 10000

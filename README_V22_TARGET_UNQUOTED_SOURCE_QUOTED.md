# v22 - Target unquoted, source quoted, operator visibility retained

## Purpose
This package is aligned with the recreated target fact-table design where target tables and columns are queried without double quotes.

Target tables:
- public.gameTransaction_prod
- public.walletTransaction_prod
- public.migration_checkpoint_prod

PostgreSQL stores these unquoted identifiers in lowercase internally, but the scripts can still write them as gameTransaction_prod/startDateTime/externalId without double quotes.

## Source tables remain quoted
Existing datalake/source tables are still mixed-case objects and remain quoted in source queries:
- public."GameTransactionInplayV1"
- public."DepositsInplayV1"
- public."WithdrawalsInplayV1"

Do not remove source-table quotes unless those source tables are recreated in lowercase.

## Operator visibility retained
The package retains the one-time task/query visibility:
- [EXTRACTION SQL] prints the first source SELECT once per phase.
- [IMPORT SQL] prints the target INSERT/ON CONFLICT template once per entity.
- [SOURCE QUERY SUMMARY] prints extraction duration and row counts.
- [PHASE SUMMARY] prints import duration and insert/upsert counts.
- [COLUMN DQ] prints lightweight batch data-quality result.

## Updated scripts relevant to this behavior
- helpers/db.py
- helpers/source_fetch.py
- helpers/game_transactions.py
- helpers/wallet_transactions.py
- helpers/fact_quality.py
- helpers/dq_common.py
- helpers/dq_reconcile.py
- helpers/dq_deep_compare.py

## DQ schema
Post-load DQ tables are under db_admin:
- db_admin.etl_dq_run_summary
- db_admin.etl_dq_mismatch_detail

DDL/grants:
- sql/create_db_admin_dq_tables.sql
- sql/grants_target_db.sql
- sql/grants_source_db.sql

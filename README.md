# Migration Engine Multi-Brand 

This package was rebuilt from the four current working scripts supplied in the chat. The uploaded scripts are included unchanged for audit under `scripts/`:

- `scripts/inplayv2.py`
- `scripts/inplayv1.py`
- `scripts/instaplay.py`
- `scripts/1play.py`

The files under `scripts/` are reference/audit copies only. The runtime engine does not import them directly. The runnable implementation is the shared engine plus the brand adapters under `helpers/brands/`.

Brand-specific business rules, source table names, source keys, and mappings are isolated in:

```text
helpers/brands/inplay/      # InplayV2
helpers/brands/inplayv1/    # InplayV1
helpers/brands/instaplay/   # Instaplay / 88Play
helpers/brands/oneplay/     # internal module for public --brand 1play
```

Use `--brand 1play` for 1Play. The internal Python module remains `oneplay` because Python package names beginning with a digit are unsafe for normal imports.

## Target routing

For this build, all brands write to target database `iestdbrds` and target schema `migration_repair` by default. Source database defaults to `iestdl`. Source schema remains configurable per brand through environment variables.

## PHT date window

When `--date-from` / `--date-to` are provided, the engine uses the same InplayV2-style Philippine business window:

```text
--date-from YYYY-MM-DD -> YYYY-MM-DD 06:00:00+08:00 inclusive
--date-to   YYYY-MM-DD -> YYYY-MM-DD 06:00:00+08:00 exclusive
```

JSONB brands compare those bounds as `timestamptz`. 1Play uses flat timestamp columns, so the same PHT bounds are converted back to Asia/Manila local timestamp before comparison.

## Low-memory defaults

The CLI defaults are intentionally conservative for a 4GB EC2 instance:

```text
--batch-size 100
--commit-every 500
--tx-limit 50
--wallet-limit 50
INSERT_PAGE_SIZE 100
reconciliation/data-quality OFF by default
```

Run reconciliation after a migration using `--run-recon`; run DQ after a migration using `--run-dq`. For completed migrations, use `--recon-only` and/or `--dq-only` so the engine does not reprocess rows.

## Examples

```bash
python3 main.py --brand inplay --migrate-all --date-from 2026-05-26 --date-to 2026-05-27 --dry-run
python3 main.py --brand inplayv1 --migrate-all --date-from 2026-05-26 --date-to 2026-05-27 --dry-run
python3 main.py --brand instaplay --migrate-all --date-from 2026-05-26 --date-to 2026-05-27 --dry-run
python3 main.py --brand 1play --migrate-all --date-from 2026-05-26 --date-to 2026-05-27 --dry-run

# post-run checks only
python3 main.py --brand inplay --recon-only --date-from 2026-05-26 --date-to 2026-05-27
python3 main.py --brand inplay --dq-only --date-from 2026-05-26 --date-to 2026-05-27
```

## Preserved mapping notes

- InplayV2: JSONB source tables, PHT 06:00 business window, `immutable_json_timestamp(...)` for JSONB source-date filtering, registration-only player creation, game/wallet rows skip if player is missing.
- InplayV1: JSONB source tables with `createddate`, `GameDate`, `transferDate`; registration-only player creation; game/wallet no ghost/shadow player creation.
- Instaplay/88Play: member-driven source flow; registration/member row drives child game and wallet fetches; mapping follows the supplied 88Play/Instaplay script.
- 1Play: flat RDBMS source tables; `LOGIN_NAME`/`PLAYER_ACCOUNT`/`PLAYER_NAME` based joining; game `TRANSACTION_ID` to externalId; wallet `TRANSACTION_ID` to referenceId.

## Environment file

This package includes `.env` and `.env.example` with DB routing, report paths, and comparison email settings.
Before running a migration or comparison, load it in the same shell:

```bash
set -a
source .env
set +a
```

Update these placeholders before live use:

```bash
export RDS_HOST="your-rds-host.amazonaws.com"
export RDS_USER="your_db_user"
export RDS_PASSWORD="your_db_password"
export SMTP_USER="your_sender_email"
export SMTP_PASSWORD="your_app_password"
export SMTP_FROM="your_sender_email"
```

## Variance reason screen display and email attachment behavior

When source-vs-Nexus comparison finds variance, the script now prints a per-phase reason summary on screen and in logs, writes a row-level variance-details CSV, and includes the same reason summary in the email body.

Generated comparison files:

- `reports/<brand>_sourceNexusComparison_<window>-rundate_<timestamp>.csv`
- `reports/<brand>_sourceNexusVarianceDetails_<window>-rundate_<timestamp>.csv`

Email behavior:

- If no variance exists, no email is sent and the log shows `completed without variance`.
- If variance exists, the email body includes the delta reason summary.
- All run CSV files for the same brand/rundate are included.
- If the CSV bundle exceeds `MIGRATION_EMAIL_MAX_ATTACHMENT_BYTES` (default `17825792`, about 17 MiB), the files are zipped and the zip is emailed.
- If the zip still exceeds the limit, no email is sent. The screen/log shows `unable to send. File too big` plus the local report directory and CSV paths.

Optional env:

```bash
export MIGRATION_EMAIL_MAX_ATTACHMENT_BYTES="17825792"
```


## Lindy phase-accounting CSV columns

Phase CSVs include sourceDatabase, sourceSchema, phase, sourceDateField, sourceWindowFromInclusive, sourceWindowToExclusive, sourceUsername, sourcePlayerId, and sourcePlayerAccount. Rows that are not newly inserted are written with a concrete action/reason where applicable: duplicate key ignored, missing username, missing target player, mapping/validation error, insert error, and player existing-row update.

The source-vs-Nexus variance report uses the same effective migration window for source and target comparison; only the phase-specific date columns differ. A delta is a true missing final-table row only when the variance-details CSV reasonCode is not_present_in_final_table.

## Lindy accounting exception CSVs

Each phase now writes a dedicated migration exception CSV beside the normal phase CSV:

`reports/<brand>_<phase>_migration_exception_<date-window>-rundate_<timestamp>.csv`

These files contain every source row that was not newly inserted, including duplicate-key ignores, existing-player updates, skipped rows, missing player/username, mapping errors, and database insert exceptions. Columns include source username/player/account, source business key, source date (`sourceDate` and `src_date` in variance detail CSV), migration window bounds, target id/date when an existing final row is found, a normalized `exceptionCode`, `insertDisposition`, and human-readable `exceptionMessage`.

The source-vs-Nexus variance detail report now uses more specific reason codes such as:

- `duplicate_reference_id_exists_expected_type_platform_target_date_before_migration_window`
- `duplicate_reference_id_exists_expected_type_platform_target_date_after_migration_window`
- `final_key_exists_wrong_transaction_type_target_date_inside_migration_window`
- `final_key_exists_wrong_platform_target_date_inside_migration_window`
- `not_present_in_final_table`

The screen/log summary groups all exception and variance reason codes by phase.

## Quiet source query display / final source-query summary

This build suppresses per-batch `[SOURCE QUERY]` and `[SOURCE QUERY RESULT]` loop output by default.
It prints a compact `[SOURCE QUERY SUMMARY]` after each phase with:

- executedSelects
- dataBatches
- totalRowsFetched
- lastRows
- limit
- lastExecutedQueryForPsql

Environment controls:

```bash
export MIGRATION_LOG_SOURCE_SQL="false"
export MIGRATION_LOG_SOURCE_SQL_EVERY_BATCH="false"
export MIGRATION_LOG_SOURCE_LAST_QUERY="true"
export MIGRATION_LOG_SOURCE_RESULTS="false"
export MIGRATION_LOG_SOURCE_RESULTS_EVERY_BATCH="false"
export MIGRATION_SOURCE_RESULT_EVERY="20"
```

Set `MIGRATION_LOG_SOURCE_SQL_EVERY_BATCH=true` only when you intentionally want the full loop queries printed again.


## PlayerDetails safety

Fact-only no-FK is the default. playerDetails, outlet, and dimension checks are skipped in migration, dry-run, comparison, reconciliation, and data-quality modes unless `--add-playerDetails` or `--addplayerDetails` is explicitly used.


## Time window mode

Default extraction uses timezone-unaware timestamp bounds:

```bash
--date-from 2024-11-01 --date-to 2024-11-02
```

This produces:

```text
2024-11-01 06:00:00 to 2024-11-02 06:00:00
```

Use this only when the source JSONB timestamp values are stored as local/plain timestamps.

To force Asia/Manila-aware bounds, add:

```bash
--time-pht-aware
```

This produces:

```text
2024-11-01 06:00:00+08:00 to 2024-11-02 06:00:00+08:00
```

Use this when the source values are stored/compared as timestamptz and the business window must be interpreted as Philippine time.

## Progress visibility

If the run looks quiet during source extraction or preflight, add:

```bash
--show-progress
```

This prints lightweight running/source-result messages without dumping full SQL.


## Show source SQL once per phase

The ETL now flashes the first extraction SELECT once per phase and the first import INSERT/ON CONFLICT template once per target entity. No flag is required. It does not print SQL for every batch.

Example wallet-only diagnostic run:

```bash
python -m helpers.main \
  --brand inplayv1 \
  --migrate-all \
  --date-from 2024-11-01 \
  --date-to 2024-11-02 \
  --entity walletTransaction \
  --dry-run \
```

For InplayV1 wallet extraction, deposits and withdrawals use the JSONB `transferDate` source date key. Game transactions use `GameDate`.

## v12 InplayV1 GameTransaction source index optimization

For `--brand inplayv1 --entity gameTransaction`, the source extraction date expression now uses the physical source column:

```sql
game_dt
```

instead of the older JSONB expression:

```sql
immutable_json_timestamp(NULLIF(data->>'GameDate',''))
```

The old JSONB expression is preserved as comments in:

```text
helpers/brands/inplayv1/adapter.py
```

Reason: `GameTransactionInplayV1` already has an index on `(game_dt, id)`, so the extraction query can use an index scan for the date-window filter and keyset/order-by pattern.

Deposits and withdrawals are unchanged; they still use JSONB `transferDate` unless an equivalent indexed physical date column is added later.

## v13 update - duration timing and lightweight fact column DQ

Added runtime timing fields:

- Source extraction SELECT timing is shown in each `[SOURCE QUERY SUMMARY]` as `extractionSeconds`, `extractionMinutes`, and `lastSelectSeconds`.
- Target import/insert/upsert timing is shown in each `[PHASE SUMMARY]` as `importDurationSeconds` and `importDurationMinutes`.
- Overall run timing is shown in `[RUN SUMMARY]` as `totalDurationSeconds` and `totalDurationMinutes`.

Added lightweight post-insert batch column DQ for fact tables:

- Enabled by default through `MIGRATION_BATCH_COLUMN_DQ=true`.
- Capped by `MIGRATION_BATCH_COLUMN_DQ_LIMIT`, default `500` rows per insert batch.
- Game batch DQ compares: `startDateTime`, `playerUserName`, `betAmount`, `validBet`, `payoutAmount`, `brand`, `platform`, and `roundId` by `externalId`.
- Wallet batch DQ compares: `transactionType`, `amount`, `status`, `createdDatetime`, and `platform` by `platform + referenceId`.
- Summary fields: `columnDqCheckedRows`, `columnVarianceRows`, `columnVarianceColumns`, and `columnVarianceColumnList`.

This is not a full historical reconciliation. It is a low-cost operational smoke check for the current batch and can detect wrong mapping or idempotent duplicates whose existing target values differ from the incoming source row.

Disable if needed:

```bash
export MIGRATION_BATCH_COLUMN_DQ=false
```

Increase/decrease sample cap:

```bash
export MIGRATION_BATCH_COLUMN_DQ_LIMIT=500
```

## v15 reporting/index/DQ update


- Normal mode is fact-only/no-FK. Old provider/game/type/player FK IDs remain nullable/blank.
- Added `sql/game_wallet_fact_reporting_structure_indexes.sql`.
- Added post-load DQ modules: `helpers.dq_reconcile` and `helpers.dq_deep_compare`.
- Added `helpers/dq_tables.sql` and `scripts/run_dq_by_date.sh`.
- See `README_REPORTING_INDEX_DQ.md`.


## db_admin DQ/QA schema

DQ/QA tables now live in `db_admin`, not `public`. See `README_DB_ADMIN_DQ_GRANTS.md`.

Run target setup:

```bash
psql -d iestdbrds -f sql/create_unquoted_existing_column_names_fact_tables.sql
psql -d iestdbrds -f sql/create_db_admin_dq_tables.sql
psql -d iestdbrds -f sql/grants_target_db.sql
```

Run source grants:

```bash
psql -d iestdl -f sql/grants_source_db.sql
```

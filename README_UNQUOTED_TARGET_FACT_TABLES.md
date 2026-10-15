# V19 - Unquoted target fact tables, original-style SQL tokens

This package recreates the target reporting fact tables so target SQL does not require double quotes.

Important PostgreSQL behavior:
- You may write unquoted tokens such as `gameTransaction_prod`, `startDateTime`, and `externalId`.
- PostgreSQL stores and resolves those identifiers as lowercase internally: `gametransaction_prod`, `startdatetime`, `externalid`.
- Therefore, this is the closest valid PostgreSQL design to "same column names but no quotes".
- Source datalake tables such as `"GameTransactionInplayV1"` are not changed and still require quotes.

## Create target fact tables

```bash
psql -d iestdbrds -f sql/create_unquoted_existing_column_names_fact_tables.sql
psql -d iestdbrds -f helpers/dq_tables.sql
```

The target tables are:

```sql
public.gameTransaction_prod
public.walletTransaction_prod
```

The loader inserts into target tables without the old FK/dimension columns:

```text
providerId
gameId
gameTypeId
playerId
```

Those columns are not required for the T-1 dashboard/reporting fact load.

## Post-load QA/DQ

Run after live load:

```bash
python -m helpers.dq_reconcile \
  --brand inplayv1 \
  --entity gameTransaction \
  --date-from 2024-11-03 \
  --date-to 2024-11-04 \
  --chunk-size 500
```

Deep column check only when needed:

```bash
python -m helpers.dq_deep_compare \
  --brand inplayv1 \
  --entity gameTransaction \
  --date-from 2024-11-03 \
  --date-to 2024-11-04 \
  --chunk-size 500 \
  --max-mismatches 1000
```

The DQ tables are included in `helpers/dq_tables.sql`.

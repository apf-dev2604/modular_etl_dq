# v23 Clean Target Fact Columns

This package removes duplicate/extra target columns added in earlier iterations.

## Target fact-table rule

The target fact tables keep only the reporting/fact columns needed for load and QA.

Removed from `public.gameTransaction_prod`:

- `providerId`
- `gameId`
- `gameTypeId`
- `playerId`
- `betId`
- `betTransactionId`
- `sourcePlayerId`
- `sourceMemberId`
- `updatedAt`

`createdAt` is replaced by `import_date` as the ETL load timestamp.

Removed from `public.walletTransaction_prod`:

- `playerId`
- `playerUserName`
- `sourcePlayerId`
- `sourceMemberId`
- `memberId`
- `updatedAt`

`createdAt` is replaced by `import_date` as the ETL load timestamp.

## SQL files

Use this when recreating tables from scratch:

```bash
psql -d iestdbrds -f sql/create_unquoted_existing_column_names_fact_tables.sql
```

Use this when you already created the previous version and want to drop/revert added columns:

```bash
psql -d iestdbrds -f sql/drop_added_duplicate_columns.sql
```

Create DQ tables under db_admin:

```bash
psql -d iestdbrds -f sql/create_db_admin_dq_tables.sql
```

## Updated scripts

- `helpers/game_transactions.py`
- `helpers/wallet_transactions.py`

Both scripts no longer insert the removed columns.

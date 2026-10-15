# Reporting structure, index pattern, and post-load DQ

## Current decision

This package keeps `gameTransaction_prod` and `walletTransaction_prod` as fact tables for reporting, but it does **not** add denormalized provider/outlet/branch text columns.

The old FK/dimension ID columns are preserved only for schema compatibility and may remain NULL:

- `providerId`
- `gameId`
- `gameTypeId`
- `playerId`

The normal load does not create or look up old dimension parent rows.

## Date window rule

Your source and target use plain timestamp windows:

```text
YYYY-MM-DD 06:00:00 <= timestamp < YYYY-MM-DD 06:00:00
```

Use default mode. Do not use `--time-pht-aware` unless you intentionally want `+08:00` aware windows.

## Reporting indexes

Run the SQL in:

```text
sql/game_wallet_fact_reporting_structure_indexes.sql
```

Do not run `CREATE INDEX CONCURRENTLY` inside a transaction block.

## T-1 report support from gameTransaction_prod

Supported directly from existing/current fact fields:

- Hour/day/week/month filters by `startDateTime`
- outlet-like grouping using `tableRoomId`
- unique players using `playerUserName`
- GrossBet using `betAmount` or `validBet`
- payout using `payoutAmount`
- jackpot contribution using `progressionContributionPaid`

Not supported from `gameTransaction_prod` alone if old dimension/FK columns are NULL and no denormalized text columns are added:

- true `Game Provider` name
- true `Outlet Name`
- true `Branch Code`

Those values must come from datalake JSONB, a separate dimension/reference process, or a separate reporting summary table.

## Post-load DQ

Create tables once:

```bash
psql -d iestdbrds -f helpers/dq_tables.sql
```

Run summary reconciliation after live load:

```bash
python -m helpers.dq_reconcile \
  --brand inplayv1 \
  --entity gameTransaction \
  --date-from 2024-11-03 \
  --date-to 2024-11-04 \
  --chunk-size 500
```

Run deep column check only when needed:

```bash
python -m helpers.dq_deep_compare \
  --brand inplayv1 \
  --entity gameTransaction \
  --date-from 2024-11-03 \
  --date-to 2024-11-04 \
  --chunk-size 500 \
  --max-mismatches 1000
```

The deep checker is chunked for a 4GB RAM / 1 CPU host.

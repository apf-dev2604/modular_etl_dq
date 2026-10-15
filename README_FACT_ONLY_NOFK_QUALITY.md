# Fact-only no-FK ETL package

This package was rebuilt from the uploaded modular project `psql-ETL-bkp-20260617-4(1).zip`.

## What changed

- `playerDetails` load is skipped by default.
- Outlet loading is skipped in fact-only mode.
- Game provider, game type, and game list creation are skipped in fact-only mode.
- No shadow, ghost, or stub player account is created.
- `gameTransaction_prod` and `walletTransaction_prod` are loaded only as fact tables.
- `playerId`, `providerId`, `gameId`, and `gameTypeId` are written as `NULL` in fact-only mode.
- Source username/member identifiers are preserved when the target table has matching soft-link columns.
- Batch-level quality logging was added and kept lightweight for 1 CPU / 4 GB.

## Main command

Run from the extracted package root.

```bash
python -m helpers.main --brand inplayv1 --migrate-all --date-from 2025-11-01 --date-to 2025-11-02 --batch-size 100 --commit-every 500
python -m helpers.main --brand inplay --migrate-all --date-from 2026-06-05 --date-to 2026-06-06 --batch-size 100 --commit-every 500
python -m helpers.main --brand instaplay --migrate-all --date-from 2026-06-05 --date-to 2026-06-06 --batch-size 100 --commit-every 500 --tx-limit 50 --wallet-limit 50
```

Shortcut launchers are also provided:

```bash
python scripts/run_inplayv1_fact_only.py --migrate-all --date-from 2025-11-01 --date-to 2025-11-02
python scripts/run_inplayv2_fact_only.py --migrate-all --date-from 2026-06-05 --date-to 2026-06-06
python scripts/run_instaplay_fact_only.py --migrate-all --date-from 2026-06-05 --date-to 2026-06-06
```

## Expected log lines

Success per batch:

```text
OK for Data Import | phase=gameTransaction source_rows=... transformed=... attempted=... inserted=... target_present_for_batch_keys=...
OK for Data Import | phase=walletTransaction source_rows=... transformed=... attempted=... inserted=... target_present_for_batch_keys=...
```

Failure is a one-liner:

```text
FAILED: <reason>
```

Details are still written into the normal trace/report logs.

## Required target adjustments

Run `ddl_fact_only_prod_adjustments.sql` first if your prod tables still require FK-derived UUID values.

The DDL keeps your fields and non-FK constraints. It only relaxes FK-derived UUID columns and adds optional soft-link columns for wallet/game repair.

## Notes

The original uploaded `scripts/*.py` files were moved to `legacy_scripts/`. The fact-only implementation uses the modular engine under `helpers/` plus the default fact-only no-FK mode.


## 2026-10 update

- `fact-only no-FK` is the default behavior. You do not need to pass a flag for it. Use `--add-playerDetails / --addplayerDetails` only if you intentionally want the legacy full parent-table behavior. `--no-fact-only-no-fk` remains only as a deprecated backward-compatible alias.
- `--dry-run` performs source fetch, mapping/transformation, and quality counts only. It does not insert rows and logs `DRY RUN ONLY ... inserted=0`.
- Game transaction safeguards preserve `roundId` and, when the target columns exist, also `betId` and `betTransactionId`. If those columns do not exist, the loader does not fail; it logs available columns during preflight.


## v4 console-output cleanup

The loader is now quiet by default for routine runs. It no longer prints the fact-only banner, TCP probe success messages, or preflight column-found messages. Set `ETL_VERBOSE_PROBE=1` only when troubleshooting TCP connectivity.

The project-root `.env` is loaded automatically from `Path(__file__).resolve().parent.parent / ".env"` inside `helpers/main.py`.


## v5 comparison/dry-run behavior

- `--dry-run` now fetches/counts/transforms source rows only and does not auto-run source-vs-target comparison.
- In fact-only no-FK mode, source-vs-target comparison excludes `players` / `playerDetails_final`.
- Fact-only comparison checks only `game_transactions`, `deposits`, and `withdrawals`.
- `playerDetails`, outlet, and dimension tables are not read or validated unless `--add-playerDetails / --addplayerDetails` is used.


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

## v8 notes: timezone and entity-only extraction

Default date behavior is timezone-unaware. For a plain 06:00-to-06:00 source window, omit `--time-pht-aware`:

```bash
python -m helpers.main --brand inplayv1 --migrate-all --date-from 2024-11-01 --date-to 2024-11-02
```

Use `--time-pht-aware` only when the source comparison must be Asia/Manila-aware and the effective bounds should include `+08:00`:

```bash
python -m helpers.main --brand inplayv1 --migrate-all --date-from 2024-11-01 --date-to 2024-11-02 --time-pht-aware
```

You can limit the ETL to one fact entity using `--entity`:

```bash
# GameTransaction entity only
python -m helpers.main --brand inplayv1 --migrate-all --date-from 2024-11-01 --date-to 2024-11-02 --entity gameTransaction --dry-run

# WalletTransaction entity only: deposits + withdrawals
python -m helpers.main --brand inplayv1 --migrate-all --date-from 2024-11-01 --date-to 2024-11-02 --entity walletTransaction --dry-run
```

Allowed values:

```text
--entity all
--entity gameTransaction
--entity walletTransaction
--entity game
--entity wallet
```

`--dry-run` fetches and maps source rows but does not insert into the target tables.


## v9 notes: entity selector naming

Preferred selector: `--entity`. Accepted values: `all`, `gameTransaction`, `GameTransaction`, `walletTransaction`, `WalletTransaction`, `game`, `wallet`. `walletTransaction` / `WalletTransaction` means both deposit and withdrawal source tables are processed into `walletTransaction_prod`. `--object` is accepted as an alias. The old `--only` flag remains as a hidden backward-compatible alias but should no longer be used in examples.


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

## v12 index usage note

InplayV1 game extraction now uses source `game_dt` for filtering and ordering. This is intended to use the existing datalake index:

```text
idx_gt_inplayv1_game_dt_id btree (game_dt, id)
```

The previous JSONB expression is preserved in comments in `helpers/brands/inplayv1/adapter.py` for rollback/reference.

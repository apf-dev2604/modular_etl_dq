# db_admin DQ/QA schema and grants

This package stores post-load DQ/QA tables in `db_admin`, not `public`.

## 1. Target database setup

Run in the target database, usually `iestdbrds`:

```bash
psql -d iestdbrds -f sql/create_unquoted_existing_column_names_fact_tables.sql
psql -d iestdbrds -f sql/create_db_admin_dq_tables.sql
psql -d iestdbrds -f sql/grants_target_db.sql
```

## 2. Source database grants

Run in the source/datalake database, usually `iestdl`:

```bash
psql -d iestdl -f sql/grants_source_db.sql
```

## 3. Runtime user/role

The grants are assigned to role `de_etl_role`, then granted to user `dbade_etl_user`.

The ETL/DQ scripts should connect using `dbade_etl_user` or another user that has `de_etl_role`:

```bash
export SOURCE_RDS_USER=dbade_etl_user
export TARGET_RDS_USER=dbade_etl_user
```

## 4. DQ schema

The default DQ schema is now `db_admin`. You do not need to pass `--dq-schema` unless overriding it.

```bash
python -m helpers.dq_reconcile \
  --brand inplayv1 \
  --entity gameTransaction \
  --date-from 2024-11-03 \
  --date-to 2024-11-04
```

To override:

```bash
python -m helpers.dq_reconcile ... --dq-schema db_admin
```

## 5. Production readiness signal

The DQ summary writes to:

```text
db_admin.etl_dq_run_summary
```

Mismatch rows write to:

```text
db_admin.etl_dq_mismatch_detail
```

The `trust_result` column is set to `READY_FOR_REPORTING` only when configured source-vs-target checks pass.

# DQ v5 targeted changes

- Records rounding-only, true hash mismatch, and missing-target external IDs in `etl_dq_run_summary.notes`.
- Defaults to at most 100 IDs per category in notes (set `DQ_NOTES_ID_LIMIT`).
- Writes all affected records to existing `db_admin.etl_dq_mismatch_detail` using the already present helper. `ROUNDING_ONLY` remains reportable and does not trigger alert.
- New DQ installations create UUID primary keys with `gen_random_uuid()`. **Existing tables remain bigint** until the optional migration SQL is reviewed and run manually. Python `CREATE TABLE IF NOT EXISTS` never alters existing `id` types.
- No connection, ETL import, or target business-table index changes.
- `sql/OPTIONAL_migrate_existing_dq_ids_to_uuid.sql` is deliberately NOT executed automatically; changing ID type could break dependencies.
- Detail writing uses existing `write_mismatches` function after summary; a detail insert failure can leave a summary row already committed and should be retried/investigated.

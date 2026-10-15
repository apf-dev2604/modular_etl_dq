-- OPTIONAL MANUAL MIGRATION (NOT run by Python). Requires maintenance window.
-- Back up both DQ tables first. IDs will change; do not run when external keys reference them.
-- Confirm no foreign keys/views/applications rely on the existing bigint IDs.
-- PostgreSQL with gen_random_uuid() available.
BEGIN;
ALTER TABLE db_admin.etl_dq_run_summary
  ALTER COLUMN id DROP DEFAULT,
  ALTER COLUMN id TYPE uuid USING gen_random_uuid(),
  ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE db_admin.etl_dq_mismatch_detail
  ALTER COLUMN id DROP DEFAULT,
  ALTER COLUMN id TYPE uuid USING gen_random_uuid(),
  ALTER COLUMN id SET DEFAULT gen_random_uuid();
COMMIT;
-- Prior sequence grants in sql/grants_target_db.sql are unnecessary for UUID IDs.
-- Remove obsolete sequences separately after reviewing dependencies.

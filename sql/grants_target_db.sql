-- =============================================================================
-- Target database grants for Nexus ETL and DQ/QA
-- Run this in the TARGET database, for example: iestdbrds
-- Role : de_etl_role
-- User : dbade_etl_user
-- =============================================================================

-- Optional safety: create role/user only if your DBA policy allows it.
-- If these already exist, these DO blocks will not replace passwords.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'de_etl_role') THEN
        CREATE ROLE de_etl_role;
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dbade_etl_user') THEN
        CREATE ROLE dbade_etl_user LOGIN;
    END IF;
END
$$;

GRANT de_etl_role TO dbade_etl_user;

DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO de_etl_role', current_database());
END
$$;

-- Required schemas.
CREATE SCHEMA IF NOT EXISTS db_admin;
GRANT USAGE ON SCHEMA public TO de_etl_role;
GRANT USAGE, CREATE ON SCHEMA db_admin TO de_etl_role;

-- Target fact tables and checkpoint table.
-- These target tables are intentionally unquoted in SQL; PostgreSQL stores them as lowercase internally.
GRANT SELECT, INSERT ON TABLE public.gameTransaction_prod TO de_etl_role;
GRANT SELECT, INSERT ON TABLE public.walletTransaction_prod TO de_etl_role;
GRANT SELECT, INSERT, UPDATE ON TABLE public.migration_checkpoint_prod TO de_etl_role;

-- Needed when id defaults use gen_random_uuid(), no sequence grant required for UUID PK.
-- DQ/QA tables in db_admin.
GRANT SELECT, INSERT, UPDATE ON TABLE db_admin.etl_dq_run_summary TO de_etl_role;
GRANT SELECT, INSERT, UPDATE ON TABLE db_admin.etl_dq_mismatch_detail TO de_etl_role;

-- bigserial sequences for DQ tables.
GRANT USAGE, SELECT ON SEQUENCE db_admin.etl_dq_run_summary_id_seq TO de_etl_role;
GRANT USAGE, SELECT ON SEQUENCE db_admin.etl_dq_mismatch_detail_id_seq TO de_etl_role;

-- Optional: allow DQ cleanup/retention jobs if you later create them.
-- GRANT DELETE ON TABLE db_admin.etl_dq_mismatch_detail TO de_etl_role;
-- GRANT DELETE ON TABLE db_admin.etl_dq_run_summary TO de_etl_role;

-- Future DQ objects under db_admin.
ALTER DEFAULT PRIVILEGES IN SCHEMA db_admin
GRANT SELECT, INSERT, UPDATE ON TABLES TO de_etl_role;

ALTER DEFAULT PRIVILEGES IN SCHEMA db_admin
GRANT USAGE, SELECT ON SEQUENCES TO de_etl_role;

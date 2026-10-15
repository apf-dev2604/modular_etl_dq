-- =============================================================================
-- DQ/QA tables for Nexus ETL
-- Schema: db_admin
-- Run in the TARGET database, for example: iestdbrds
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS db_admin;

CREATE TABLE IF NOT EXISTS db_admin.etl_dq_run_summary (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id text NOT NULL,
    brand text NOT NULL,
    entity text NOT NULL,
    window_from timestamp NOT NULL,
    window_to timestamp NOT NULL,
    source_rows bigint,
    target_rows bigint,
    source_distinct_keys bigint,
    target_distinct_keys bigint,
    source_duplicate_keys bigint,
    target_duplicate_keys bigint,
    source_total_bet numeric(24,4),
    target_total_bet numeric(24,4),
    source_total_valid_bet numeric(24,4),
    target_total_valid_bet numeric(24,4),
    source_total_payout numeric(24,4),
    target_total_payout numeric(24,4),
    source_total_jackpot_contribution numeric(24,4),
    target_total_jackpot_contribution numeric(24,4),
    missing_in_target bigint,
    extra_in_target bigint,
    hash_mismatch_rows bigint,
    status text NOT NULL,
    trust_result text NULL,
    notes text NULL,
    duration_seconds numeric(18,3),
    duration_minutes numeric(18,3),
    created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS db_admin.etl_dq_mismatch_detail (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id text NOT NULL,
    brand text NOT NULL,
    entity text NOT NULL,
    business_key text NOT NULL,
    mismatch_type text NOT NULL,
    mismatch_columns text[] NULL,
    source_hash text NULL,
    target_hash text NULL,
    source_sample jsonb NULL,
    target_sample jsonb NULL,
    created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_etl_dq_summary_window
ON db_admin.etl_dq_run_summary (brand, entity, window_from, window_to);

CREATE INDEX IF NOT EXISTS idx_etl_dq_mismatch_run
ON db_admin.etl_dq_mismatch_detail (run_id);

CREATE INDEX IF NOT EXISTS idx_etl_dq_mismatch_key
ON db_admin.etl_dq_mismatch_detail (business_key);

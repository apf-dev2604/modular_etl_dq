-- =============================================================================
-- Source/datalake database grants for Nexus ETL and post-load DQ
-- Run this in the SOURCE database, for example: iestdl
-- Role : de_etl_role
-- User : dbade_etl_user
-- =============================================================================

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

GRANT USAGE ON SCHEMA public TO de_etl_role;

CREATE OR REPLACE FUNCTION pg_temp.grant_select_if_exists(p_schema text, p_table text)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    IF to_regclass(format('%I.%I', p_schema, p_table)) IS NOT NULL THEN
        EXECUTE format('GRANT SELECT ON TABLE %I.%I TO de_etl_role', p_schema, p_table);
    ELSE
        RAISE NOTICE 'Skipping grant; %.% does not exist', p_schema, p_table;
    END IF;
END;
$$;

-- InplayV1 source tables used by current ETL/DQ.
SELECT pg_temp.grant_select_if_exists('public', 'GameTransactionInplayV1');
SELECT pg_temp.grant_select_if_exists('public', 'DepositsInplayV1');
SELECT pg_temp.grant_select_if_exists('public', 'WithdrawalsInplayV1');
SELECT pg_temp.grant_select_if_exists('public', 'PlayerRegistrationsInplayV1');

-- InplayV2 source tables, if you use --brand inplay.
SELECT pg_temp.grant_select_if_exists('public', 'GameTransactionInplayV2');
SELECT pg_temp.grant_select_if_exists('public', 'DepositsInplayV2');
SELECT pg_temp.grant_select_if_exists('public', 'WithdrawalsInplayV2');
SELECT pg_temp.grant_select_if_exists('public', 'PlayerRegistrationsInplayV2');
SELECT pg_temp.grant_select_if_exists('public', 'PlayerDetailInplayV2');

-- 88Play / Instaplay source tables, if you use --brand instaplay.
SELECT pg_temp.grant_select_if_exists('public', 'GameTransaction88Play');
SELECT pg_temp.grant_select_if_exists('public', 'Deposits88Play');
SELECT pg_temp.grant_select_if_exists('public', 'Withdrawals88Play');
SELECT pg_temp.grant_select_if_exists('public', 'PlayerRegistrations88Play');
SELECT pg_temp.grant_select_if_exists('public', 'PlayerDetail88Play');

-- 1Play source tables, if you use --brand 1play.
SELECT pg_temp.grant_select_if_exists('public', 'GameTransaction1play');
SELECT pg_temp.grant_select_if_exists('public', 'PlayerCashTransactions1play');
SELECT pg_temp.grant_select_if_exists('public', 'PlayerDetails1play');
SELECT pg_temp.grant_select_if_exists('public', 'PlayerRegistrations1play');
SELECT pg_temp.grant_select_if_exists('public', 'Outlet1Play');

-- Function used by JSONB date expression indexes/queries, if it exists in your source DB.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND p.proname = 'immutable_json_timestamp'
    ) THEN
        GRANT EXECUTE ON FUNCTION public.immutable_json_timestamp(text) TO de_etl_role;
    END IF;
END
$$;

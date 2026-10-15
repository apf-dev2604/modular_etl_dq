-- =============================================================================
-- Unquoted unquoted original-style reporting fact tables for Nexus ETL
-- Target: PostgreSQL
-- Notes:
--   * No double-quoted table or column names.
--   * No FK/dimension ID columns: provider_id, game_id, game_type_id, player_id removed.
--   * Source/original records remain preserved in the datalake JSONB source tables.
--   * These fact tables are intended for T-1 reporting/dashboard workloads.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Optional checkpoint table for resumable runs. Target fact tables use unquoted original-style tokens; PostgreSQL stores them lowercase internally.
CREATE TABLE IF NOT EXISTS public.migration_checkpoint_prod (
    platform text PRIMARY KEY,
    last_source_id text NULL,
    updated_at timestamptz DEFAULT now() NOT NULL
);

CREATE TABLE IF NOT EXISTS public.gameTransaction_prod (
    id uuid DEFAULT gen_random_uuid() NOT NULL,

    startDateTime timestamptz NOT NULL,
    playerUserName text NOT NULL,
    tableRoomId text NULL,

    sideBetAmount numeric(18,4) DEFAULT 0 NOT NULL,
    betAmount numeric(18,4) DEFAULT 0 NOT NULL,
    validBet numeric(18,4) DEFAULT 0 NOT NULL,
    payoutAmount numeric(18,4) DEFAULT 0 NOT NULL,

    PC1 numeric(18,4) DEFAULT 0 NOT NULL,
    PC2 numeric(18,4) DEFAULT 0 NOT NULL,
    PC3 numeric(18,4) DEFAULT 0 NOT NULL,
    PC4 numeric(18,4) DEFAULT 0 NOT NULL,
    PC5 numeric(20,8) DEFAULT 0 NOT NULL,

    JW1 numeric(18,4) DEFAULT 0 NOT NULL,
    JW2 numeric(18,4) DEFAULT 0 NOT NULL,
    JW3 numeric(18,4) DEFAULT 0 NOT NULL,
    JW4 numeric(18,4) DEFAULT 0 NOT NULL,
    JW5 numeric(20,8) DEFAULT 0 NOT NULL,

    progressionContributionPaid numeric(18,4) DEFAULT 0 NOT NULL,
    seedMoneyWon numeric(18,4) DEFAULT 0 NOT NULL,
    seedMoneyJackpotOver1000 int DEFAULT 0 NOT NULL,

    endDateTime timestamptz NOT NULL,
    externalId text NOT NULL,

    sportsName text NULL,
    sportsGameType text NULL,
    leagueName text NULL,
    eventName text NULL,
    eventDetails text NULL,
    betType text NULL,
    parlay boolean DEFAULT false NOT NULL,
    betDetails jsonb NULL,
    betTiming text NULL,

    brand varchar(100) NULL,
    platform varchar(50) NULL,
    roundId text NULL,

    -- Loader audit timestamp. Not from source; records when ETL inserted the row.
    import_date timestamptz DEFAULT now() NOT NULL,

    CONSTRAINT gameTransaction_prod_pkey PRIMARY KEY (id),
    CONSTRAINT uq_gameTransaction_prod_external UNIQUE (externalId),
    CONSTRAINT chk_gameTransaction_prod_amounts_nonneg CHECK (
        sideBetAmount >= 0 AND betAmount >= 0 AND validBet >= 0 AND payoutAmount >= 0 AND
        PC1 >= 0 AND PC2 >= 0 AND PC3 >= 0 AND PC4 >= 0 AND PC5 >= 0 AND
        JW1 >= 0 AND JW2 >= 0 AND JW3 >= 0 AND JW4 >= 0 AND JW5 >= 0 AND
        progressionContributionPaid >= 0 AND seedMoneyWon >= 0
    ),
    CONSTRAINT chk_gameTransaction_prod_betTiming CHECK (
        betTiming IS NULL OR betTiming IN ('PRE','INP')
    ),
    CONSTRAINT chk_gameTransaction_prod_seed_flag CHECK (
        seedMoneyJackpotOver1000 IN (0,1)
    )
);

CREATE TABLE IF NOT EXISTS public.walletTransaction_prod (
    id uuid DEFAULT gen_random_uuid() NOT NULL,

    transactionType text NOT NULL,
    platform text NOT NULL,
    paymentGateway text NOT NULL,
    domain text NOT NULL,
    amount numeric(18,4) NOT NULL,
    status text NOT NULL,
    bettingPhase text NULL,

    createdDatetime timestamptz DEFAULT now() NOT NULL,
    confirmedDatetime timestamptz NULL,
    cancelledDatetime timestamptz NULL,
    failedDatetime timestamptz NULL,
    referenceId text NULL,

    -- Loader audit timestamp. Not from source; records when ETL inserted the row.
    import_date timestamptz DEFAULT now() NOT NULL,

    CONSTRAINT walletTransaction_prod_pkey PRIMARY KEY (id),
    CONSTRAINT walletTransaction_prod_amount_check CHECK (amount >= 0),
    CONSTRAINT chk_walletTransaction_prod_phase CHECK (
        bettingPhase IS NULL OR bettingPhase IN ('PRE','INP')
    ),
    CONSTRAINT chk_walletTransaction_prod_status_dates CHECK (
        (status <> 'confirmed' OR confirmedDatetime IS NOT NULL) AND
        (status <> 'cancelled' OR cancelledDatetime IS NOT NULL) AND
        (status <> 'failed' OR failedDatetime IS NOT NULL)
    )
);

-- =============================================================================
-- Idempotency / uniqueness
-- =============================================================================

-- game transactions use source TransactionID -> externalId
CREATE UNIQUE INDEX IF NOT EXISTS ux_gameTransaction_prod_externalId
ON public.gameTransaction_prod (externalId);

-- wallet transactions use platform + source referenceId
CREATE UNIQUE INDEX IF NOT EXISTS ux_walletTransaction_prod_platform_reference
ON public.walletTransaction_prod (platform, referenceId)
WHERE referenceId IS NOT NULL;

-- =============================================================================
-- Reporting indexes for T-1 date windows and dashboards
-- =============================================================================

-- Primary T-1 date-window filter: yyyy-mm-dd 06:00:00 to yyyy-mm-dd 06:00:00 UTC.
CREATE INDEX IF NOT EXISTS idx_gameTransaction_prod_brand_platform_start
ON public.gameTransaction_prod (brand, platform, startDateTime);

-- Hourly grouping/filtering by outlet-like tableRoomId.
CREATE INDEX IF NOT EXISTS idx_gameTransaction_prod_hourly_room
ON public.gameTransaction_prod (brand, platform, startDateTime, tableRoomId);

-- Unique player count and retention by date windows.
CREATE INDEX IF NOT EXISTS idx_gameTransaction_prod_player_start
ON public.gameTransaction_prod (playerUserName, startDateTime);

CREATE INDEX IF NOT EXISTS idx_gameTransaction_prod_start_player
ON public.gameTransaction_prod (startDateTime, playerUserName);

-- Low-maintenance helper for very large append-only fact tables.
CREATE INDEX IF NOT EXISTS brin_gameTransaction_prod_start
ON public.gameTransaction_prod USING brin (startDateTime);

CREATE INDEX IF NOT EXISTS idx_walletTransaction_prod_platform_created
ON public.walletTransaction_prod (platform, createdDatetime);

CREATE INDEX IF NOT EXISTS idx_walletTransaction_prod_status_created
ON public.walletTransaction_prod (status, createdDatetime);

CREATE INDEX IF NOT EXISTS idx_walletTransaction_prod_type_created
ON public.walletTransaction_prod (transactionType, createdDatetime);

CREATE INDEX IF NOT EXISTS idx_walletTransaction_prod_platform_reference_lookup
ON public.walletTransaction_prod (platform, referenceId)
WHERE referenceId IS NOT NULL;

-- =============================================================================
-- Source-side indexes, if not already present.
-- Keep source table quotes because your existing datalake source table is mixed-case.
-- =============================================================================

-- CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_gt_inplayv1_game_dt_id
-- ON public."GameTransactionInplayV1" (game_dt, id)
-- WHERE game_dt IS NOT NULL AND data IS NOT NULL;

-- CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_src_inplayv1_deposits_transferdate_id
-- ON public."DepositsInplayV1" (immutable_json_timestamp(NULLIF(data->>'transferDate','')), id)
-- WHERE data IS NOT NULL AND NULLIF(data->>'transferDate','') IS NOT NULL;

-- CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_src_inplayv1_withdrawals_transferdate_id
-- ON public."WithdrawalsInplayV1" (immutable_json_timestamp(NULLIF(data->>'transferDate','')), id)
-- WHERE data IS NOT NULL AND NULLIF(data->>'transferDate','') IS NOT NULL;

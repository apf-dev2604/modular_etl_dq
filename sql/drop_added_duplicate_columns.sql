-- =============================================================================
-- Revert/remove duplicated traceability columns from target fact tables
-- Keep current unquoted target table names. PostgreSQL stores these as lowercase.
-- Run on target database after you confirm you do not need these columns.
-- =============================================================================

-- gameTransaction_prod: remove extra duplicate columns that were added in earlier package versions.
ALTER TABLE public.gameTransaction_prod DROP COLUMN IF EXISTS betId;
ALTER TABLE public.gameTransaction_prod DROP COLUMN IF EXISTS betTransactionId;
ALTER TABLE public.gameTransaction_prod DROP COLUMN IF EXISTS sourcePlayerId;
ALTER TABLE public.gameTransaction_prod DROP COLUMN IF EXISTS sourceMemberId;

-- Replace loader-created timestamp with import_date.
-- If createdAt exists, rename it to import_date. If import_date already exists, keep it.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='gametransaction_prod' AND column_name='createdat'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='gametransaction_prod' AND column_name='import_date'
    ) THEN
        ALTER TABLE public.gameTransaction_prod RENAME COLUMN createdAt TO import_date;
    END IF;
END $$;

ALTER TABLE public.gameTransaction_prod DROP COLUMN IF EXISTS updatedAt;
ALTER TABLE public.gameTransaction_prod ADD COLUMN IF NOT EXISTS import_date timestamptz DEFAULT now() NOT NULL;

-- walletTransaction_prod: remove extra soft-link/duplicate columns.
ALTER TABLE public.walletTransaction_prod DROP COLUMN IF EXISTS playerUserName;
ALTER TABLE public.walletTransaction_prod DROP COLUMN IF EXISTS sourcePlayerId;
ALTER TABLE public.walletTransaction_prod DROP COLUMN IF EXISTS sourceMemberId;
ALTER TABLE public.walletTransaction_prod DROP COLUMN IF EXISTS memberId;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='wallettransaction_prod' AND column_name='createdat'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='wallettransaction_prod' AND column_name='import_date'
    ) THEN
        ALTER TABLE public.walletTransaction_prod RENAME COLUMN createdAt TO import_date;
    END IF;
END $$;

ALTER TABLE public.walletTransaction_prod DROP COLUMN IF EXISTS updatedAt;
ALTER TABLE public.walletTransaction_prod ADD COLUMN IF NOT EXISTS import_date timestamptz DEFAULT now() NOT NULL;

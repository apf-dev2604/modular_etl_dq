-- Fact-only / no-FK target adjustments.
-- Keeps columns and non-FK business constraints. Only relaxes FK-dependent UUID columns.

ALTER TABLE kemet."gameTransaction_prod"
  ALTER COLUMN "providerId" DROP NOT NULL,
  ALTER COLUMN "gameId" DROP NOT NULL,
  ALTER COLUMN "gameTypeId" DROP NOT NULL,
  ALTER COLUMN "playerId" DROP NOT NULL;

ALTER TABLE kemet."walletTransaction_prod"
  ALTER COLUMN "playerId" DROP NOT NULL;

-- Soft ownership columns for wallet repair/backfill. Safe to run repeatedly.
ALTER TABLE kemet."walletTransaction_prod"
  ADD COLUMN IF NOT EXISTS "playerUserName" text NULL,
  ADD COLUMN IF NOT EXISTS "sourcePlayerId" text NULL,
  ADD COLUMN IF NOT EXISTS "sourceMemberId" text NULL,
  ADD COLUMN IF NOT EXISTS "memberId" text NULL;

-- Optional source identity fields for game transaction if you want to preserve bet-specific identifiers.
ALTER TABLE kemet."gameTransaction_prod"
  ADD COLUMN IF NOT EXISTS "betId" text NULL,
  ADD COLUMN IF NOT EXISTS "betTransactionId" text NULL,
  ADD COLUMN IF NOT EXISTS "sourcePlayerId" text NULL,
  ADD COLUMN IF NOT EXISTS "sourceMemberId" text NULL;

CREATE INDEX IF NOT EXISTS idx_wallettx_prod_playerusername
ON kemet."walletTransaction_prod" ("playerUserName");

CREATE INDEX IF NOT EXISTS idx_wallettx_prod_sourceplayer
ON kemet."walletTransaction_prod" ("sourcePlayerId");

CREATE INDEX IF NOT EXISTS idx_wallettx_prod_sourcemember
ON kemet."walletTransaction_prod" ("sourceMemberId");

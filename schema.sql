-- Neon Schema Migration for ClosedEmailDeBounce
-- Adds missing columns to existing tables

ALTER TABLE validation_jobs 
  ADD COLUMN IF NOT EXISTS processed INTEGER DEFAULT 0,
  ADD COLUMN IF NOT EXISTS valid_count INTEGER DEFAULT 0,
  ADD COLUMN IF NOT EXISTS filtered_count INTEGER DEFAULT 0,
  ADD COLUMN IF NOT EXISTS invalid_count INTEGER DEFAULT 0;

-- Ensure useful indexes exist
CREATE INDEX IF NOT EXISTS idx_validation_jobs_list_id ON validation_jobs(list_id);
CREATE INDEX IF NOT EXISTS idx_validation_jobs_status ON validation_jobs(status);
CREATE INDEX IF NOT EXISTS idx_uploaded_files_token ON uploaded_files(token);

-- Balances table for top-up system
CREATE TABLE IF NOT EXISTS balances (
    user_id     INTEGER PRIMARY KEY,
    credits     DECIMAL(12,2) DEFAULT 0.00,
    updated_at  TIMESTAMPTZ DEFAULT now()
);

-- Payment requests for admin approval
ALTER TABLE payment_requests ADD COLUMN IF NOT EXISTS tx_hash TEXT DEFAULT '';
CREATE TABLE IF NOT EXISTS payment_requests (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL,
    uid_str         TEXT NOT NULL DEFAULT '',
    amount_usd      DECIMAL(10,2) NOT NULL,
    tx_hash         TEXT DEFAULT '',
    status          TEXT DEFAULT 'pending',
    admin_note      TEXT DEFAULT '',
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

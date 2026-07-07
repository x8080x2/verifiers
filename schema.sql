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

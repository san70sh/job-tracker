-- What the last check of a watched board found, so a failing board is visible and not retried every tick.
-- last_polled_at now means "last attempt" (success or not); last_success_at is the last good check and is what
-- tells a first check (silent baseline) from a later one.
ALTER TABLE watched_boards
  ADD COLUMN IF NOT EXISTS last_success_at timestamptz,
  ADD COLUMN IF NOT EXISTS last_error      text,         -- NULL after a good check
  ADD COLUMN IF NOT EXISTS last_listed     int,          -- postings the last good check listed
  ADD COLUMN IF NOT EXISTS last_new        int;          -- ...of which were new

-- every board checked so far was checked successfully (a failure used to record nothing)
UPDATE watched_boards SET last_success_at = last_polled_at WHERE last_success_at IS NULL AND last_polled_at IS NOT NULL;

-- A Notion push in progress is 'sending' (it used to stay 'pending'), so an edit made while the push is in flight
-- queues a new pending row instead of folding into the one the worker is about to mark done (the edit was lost).
-- claimed_at tells a push cut off by a crash from one still running, so the first can be sent again.
ALTER TABLE notion_outbox ADD COLUMN IF NOT EXISTS claimed_at timestamptz;
ALTER TABLE notion_outbox DROP CONSTRAINT IF EXISTS notion_outbox_status_check;
ALTER TABLE notion_outbox ADD CONSTRAINT notion_outbox_status_check
    CHECK (status IN ('pending', 'sending', 'done', 'failed'));

-- Experience filter of a board (years, either end optional) and the years each discovered posting asked for.
ALTER TABLE watched_boards
    ADD COLUMN IF NOT EXISTS experience_min smallint CHECK (experience_min >= 0),
    ADD COLUMN IF NOT EXISTS experience_max smallint CHECK (experience_max >= 0);
ALTER TABLE watched_boards DROP CONSTRAINT IF EXISTS watched_boards_experience_order;
ALTER TABLE watched_boards ADD CONSTRAINT watched_boards_experience_order
    CHECK (experience_min IS NULL OR experience_max IS NULL OR experience_min <= experience_max);
ALTER TABLE discovered_postings
    ADD COLUMN IF NOT EXISTS experience_min numeric(4,1),
    ADD COLUMN IF NOT EXISTS experience_max numeric(4,1);  -- NULL max with a min = "N+ years"; both NULL = not stated or not read

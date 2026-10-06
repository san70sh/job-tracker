-- BASELINE: applied only to an empty database (src/jobtracker/migrate.py). Do not edit; add db/migrations/NNN_name.sql.
-- Job Tracker: self-hosted schema (PostgreSQL 14+)
-- Mirrors the Notion "Job Tracker" database (every Notion property maps 1:1 to a column or a
-- child table) and adds the tables the deterministic pipeline needs.
-- Notion property -> column mapping is noted in comments as [Notion: <Property> (<type>)].

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS pg_trgm;    -- fuzzy company / title matching

-- ───────────────────────────── enums ─────────────────────────────
-- Values copied verbatim from the Notion select options.
CREATE TYPE job_status AS ENUM (
  'Applied', 'OA / Assessment', 'Recruiter Screen', 'Interviewing',
  'Offer', 'Rejected', 'Withdrawn', 'Ghosted'
);
CREATE TYPE work_mode AS ENUM ('Onsite', 'Hybrid', 'Remote');

-- Pre-apply stage Notion does not have (a row exists before you decide to apply).
CREATE TYPE job_stage AS ENUM ('Inbox', 'Shortlisted', 'Applied', 'Closed');

CREATE TYPE ats_type AS ENUM (
  'greenhouse', 'lever', 'ashby', 'smartrecruiters', 'workday', 'oracle', 'eightfold', 'amazon',
  'jibe', 'avature', 'icims', 'html', 'manual'
);
CREATE TYPE tech_kind AS ENUM ('required', 'nice_to_have', 'mentioned');
CREATE TYPE section_kind AS ENUM (
  'about_company', 'role', 'responsibilities', 'requirements',
  'nice_to_have', 'benefits', 'logistics', 'other'
);
CREATE TYPE extract_method AS ENUM ('ats_api', 'jsonld', 'rules', 'llm', 'manual');

-- ───────────────────────────── companies ─────────────────────────────
CREATE TABLE companies (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name             text NOT NULL,                      -- [Notion: Company (text)]
  normalized_name  text NOT NULL UNIQUE,               -- lower, strip Inc/Ltd/Pvt, punctuation
  website          text,
  ats              ats_type,
  ats_slug         text,                               -- e.g. greenhouse board token
  ats_config       jsonb NOT NULL DEFAULT '{}',        -- P2 extras: workday {tenant,wd,site}, oracle {host,siteNumber}, eightfold {host,domain}
  about            text,                               -- "About <company>" paragraph (LLM once, cached)
  about_hash       text,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX companies_name_trgm ON companies USING gin (name gin_trgm_ops);

-- ───────────────────────────── jobs (the Notion table) ─────────────────────────────
CREATE TABLE jobs (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  notion_page_id       text UNIQUE,                    -- sync key, NULL until first push

  -- Notion properties ------------------------------------------------------
  role                 text NOT NULL,                  -- [Notion: Role (title)]
  company_id           uuid NOT NULL REFERENCES companies(id),  -- [Notion: Company (text)]
  status               job_status,                     -- [Notion: Status (select)]; NULL while stage<>'Applied'
  date_applied         date,                           -- [Notion: Date Applied (date)]
  job_ref              text,                           -- [Notion: Job ID (text)] employer's req id
  job_link             text NOT NULL,                  -- [Notion: Job Link (url)] as pasted
  level                text,                           -- [Notion: Level (text)] display string
  location             text,                           -- [Notion: Location (text)]
  work_mode            work_mode,                      -- [Notion: Work Mode (select)]
  experience_required  text,                           -- [Notion: Experience Required (text)] display string
  team_domain          text,                           -- [Notion: Team / Domain (text)]
  key_responsibilities text,                           -- [Notion: Key Responsibilities (text)] bullets joined by \n
  requirements         text,                           -- [Notion: Requirements (text)] REQUIRED / GOOD TO HAVE blocks
  notes                text,                           -- [Notion: Notes (text)] fit summary
  salary_min_lpa       numeric(7,2),                   -- [Notion: Salary Min (LPA) (number)]
  salary_max_lpa       numeric(7,2),                   -- [Notion: Salary Max (LPA) (number)]
  salary_details       text,                           -- [Notion: Salary Details (text)]
  salary_sources       text,                           -- [Notion: Salary Sources (text)] rendered from salary_estimates
  -- [Notion: Key Technologies (multi_select)] -> job_technologies

  -- Pipeline-owned columns (not in Notion) -------------------------------
  stage                job_stage NOT NULL DEFAULT 'Inbox',
  canonical_url        text NOT NULL,                  -- tracking params stripped, host lowercased
  ats                  ats_type NOT NULL,
  ats_posting_id       text,                           -- id inside the ATS (distinct from job_ref when both exist)
  posted_at            date,
  closed_at            date,                           -- posting disappeared / 404
  level_normalized     text,                           -- canonical ladder, e.g. 'IC3', 'L5', 'SDE3'
  experience_min_years numeric(4,1),
  experience_max_years numeric(4,1),
  last_activity_at     timestamptz,                    -- drives Ghosted automation
  extraction_summary   jsonb NOT NULL DEFAULT '{}',    -- {"role":{"method":"jsonld","conf":0.99}, ...}
  needs_review         boolean NOT NULL DEFAULT false, -- true when any required field conf < threshold
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT jobs_applied_has_status CHECK (stage <> 'Applied' OR status IS NOT NULL),
  CONSTRAINT jobs_salary_order CHECK (salary_min_lpa IS NULL OR salary_max_lpa IS NULL
                                      OR salary_min_lpa <= salary_max_lpa)
);
-- Dedup: same posting never enters twice.
CREATE UNIQUE INDEX jobs_canonical_url_uq ON jobs (canonical_url);
CREATE UNIQUE INDEX jobs_company_ref_uq   ON jobs (company_id, job_ref) WHERE job_ref IS NOT NULL;
CREATE INDEX jobs_status_idx   ON jobs (status);
CREATE INDEX jobs_stage_idx    ON jobs (stage);
CREATE INDEX jobs_applied_idx  ON jobs (date_applied DESC);

-- Rendered "page body" of the Notion page (About / The role / Technical skills / ...).
CREATE TABLE job_sections (
  id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id    uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  kind      section_kind NOT NULL,
  heading   text NOT NULL,
  body_md   text NOT NULL,
  position  int  NOT NULL,
  UNIQUE (job_id, position)
);

-- ───────────────────────────── technologies (Key Technologies) ─────────────────────────────
-- Controlled vocabulary == the Notion multi_select options. Source of truth for names AND the
-- alias/regex patterns the tagger uses is config/vocab.json; `python -m jobtracker seed` mirrors the
-- names into this table so job_technologies can reference them.
CREATE TABLE technologies (
  id        serial PRIMARY KEY,
  name      text NOT NULL UNIQUE,                      -- exact Notion option name
  category  text NOT NULL,                             -- language, framework, datastore, cloud, practice, ...
  notion_color text
);
CREATE TABLE job_technologies (
  job_id   uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  tech_id  int  NOT NULL REFERENCES technologies(id),
  kind     tech_kind NOT NULL DEFAULT 'required',
  PRIMARY KEY (job_id, tech_id)
);
CREATE INDEX job_technologies_tech_idx ON job_technologies (tech_id);

-- ───────────────────────────── status history ─────────────────────────────
CREATE TABLE status_events (
  id         bigserial PRIMARY KEY,
  job_id     uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  from_status job_status,
  to_status   job_status NOT NULL,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  source     text NOT NULL DEFAULT 'user',             -- user | ghost_cron | email_parser | notion_sync
  note       text
);
CREATE INDEX status_events_job_idx ON status_events (job_id, occurred_at);

-- Legal transitions; the API refuses anything not listed (state machine in docs/architecture.md).
CREATE TABLE status_transitions (
  id          serial PRIMARY KEY,
  from_status job_status,                              -- NULL = initial
  to_status   job_status NOT NULL
);
-- PG14 has no NULLS NOT DISTINCT, so enforce uniqueness with two partial indexes.
CREATE UNIQUE INDEX status_transitions_uq      ON status_transitions (from_status, to_status) WHERE from_status IS NOT NULL;
CREATE UNIQUE INDEX status_transitions_init_uq ON status_transitions (to_status) WHERE from_status IS NULL;

-- ───────────────────────────── raw fetch snapshots ─────────────────────────────
-- Keep the source so improving a parser = re-run on stored data, no re-fetch, no LLM.
CREATE TABLE job_snapshots (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id         uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  fetched_at     timestamptz NOT NULL DEFAULT now(),
  http_status    int,
  content_type   text,
  body           text NOT NULL,                        -- ATS JSON or HTML
  content_hash   text NOT NULL,                        -- sha256 of normalised text; change => re-extract
  parser_version text NOT NULL,
  UNIQUE (job_id, content_hash)
);

-- ───────────────────────────── salary ─────────────────────────────
-- India-only scope: every amount is INR per year. Rows are entered by the user (CSV import), never scraped.
CREATE TABLE salary_benchmarks (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id    uuid REFERENCES companies(id),
  level_norm    text,                                  -- matches jobs.level_normalized
  location_norm text,                                  -- e.g. 'Hyderabad', 'India', 'Bengaluru'
  base          numeric(14,2),
  stock_annual  numeric(14,2),
  bonus_annual  numeric(14,2),
  total_annual  numeric(14,2) NOT NULL,                -- INR per year
  years_exp     numeric(4,1),
  kind          text NOT NULL,                         -- posted_range | aggregate_avg | self_reported
  source        text NOT NULL,                         -- 'levels.fyi' | 'leetcode' | 'posting' | 'glassdoor' | 'manual'
  source_url    text,
  observed_at   date NOT NULL DEFAULT current_date
);
CREATE INDEX salary_bench_lookup ON salary_benchmarks (company_id, level_norm, location_norm);

-- What the resolver actually used for a given job (-> salary_min/max/details/sources).
CREATE TABLE salary_estimates (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id        uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  benchmark_id  uuid REFERENCES salary_benchmarks(id),
  min_lpa       numeric(7,2) NOT NULL,
  max_lpa       numeric(7,2) NOT NULL,
  method        text NOT NULL,                         -- posted_range | percentile_band | manual
  explanation   text NOT NULL,                         -- templated -> salary_details
  created_at    timestamptz NOT NULL DEFAULT now()
);


-- ───────────────────────────── LLM fallback + cache ─────────────────────────────
CREATE TABLE llm_calls (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id        uuid REFERENCES jobs(id) ON DELETE SET NULL,
  task          text NOT NULL,        -- extract_fields | fit_narrative | company_about | cover_letter
  input_hash    text NOT NULL,        -- sha256(task + prompt_version + input); cache key
  model         text NOT NULL,
  prompt_version text NOT NULL,
  output        jsonb NOT NULL,
  input_tokens  int,
  output_tokens int,
  cost_usd      numeric(10,5),
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (task, input_hash)
);

-- Per-field provenance, so you can see what is rule-derived vs model-derived and audit it.
CREATE TABLE field_provenance (
  job_id     uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  field      text NOT NULL,
  method     extract_method NOT NULL,
  confidence numeric(3,2) NOT NULL,
  evidence   text,                                     -- snippet / JSON path the value came from
  llm_call_id uuid REFERENCES llm_calls(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (job_id, field)
);

-- ───────────────────────────── intake + sync + ops ─────────────────────────────
CREATE TABLE intake_queue (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  url         text NOT NULL,
  origin      text NOT NULL,          -- paste | extension | email_alert | board_watch | api
  state       text NOT NULL DEFAULT 'queued',  -- queued | running | done | failed | duplicate
  attempts    int  NOT NULL DEFAULT 0,
  error       text,
  job_id      uuid REFERENCES jobs(id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz
);
CREATE INDEX intake_queue_state_idx ON intake_queue (state, created_at);

-- Boards you want polled for new postings (optional discovery feature).
CREATE TABLE watched_boards (
  id          serial PRIMARY KEY,
  company_id  uuid NOT NULL REFERENCES companies(id),
  ats         ats_type NOT NULL,
  board_url   text NOT NULL UNIQUE,
  slug        text,                                    -- adapter BoardRef.slug (token / host / tenant)
  config      jsonb NOT NULL DEFAULT '{}',             -- adapter BoardRef.config (workday site, oracle siteNumber, ...)
  title_include text[] NOT NULL DEFAULT '{}',          -- regexes
  title_exclude text[] NOT NULL DEFAULT '{}',
  location_include text[] NOT NULL DEFAULT '{}',
  last_polled_at timestamptz,
  poll_interval_minutes int NOT NULL DEFAULT 180,
  etag           text,                                 -- conditional GET where the ATS supports it
  enabled        boolean NOT NULL DEFAULT true
);

-- Temporary working set for the board watcher (NOT a history). Lifecycle:
--   baseline        open on the board when the board was first checked; silent, never surfaced
--   filtered        failed the board's title/location filters; silent
--   new             surfaced in the Inbox, not looked at yet
--   seen            looked at ("mark all seen"), still undecided  -> both new and seen trigger reminders
--   added           became a tracked job                          -> purged by the scheduled purge
--   not_interested  rejected by the user; kept until the posting leaves the board so it is not re-surfaced
--   closed          no longer listed on the board                 -> purged by the scheduled purge
CREATE TABLE discovered_postings (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  board_id        int  NOT NULL REFERENCES watched_boards(id) ON DELETE CASCADE,
  ats_posting_id  text NOT NULL,
  url             text NOT NULL,
  canonical_url   text NOT NULL,
  title           text NOT NULL,
  location        text,
  posted_at       date,
  state           text NOT NULL DEFAULT 'new'
                  CHECK (state IN ('baseline','filtered','new','seen','added','not_interested','closed')),
  job_id          uuid REFERENCES jobs(id) ON DELETE SET NULL,   -- set when added
  first_seen_at   timestamptz NOT NULL DEFAULT now(),
  last_seen_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (board_id, ats_posting_id)
);
CREATE INDEX discovered_pending_idx ON discovered_postings (first_seen_at) WHERE state IN ('new','seen');

-- Notion is a READ-ONLY mirror: the app writes, Notion never writes back.
-- notion_sync remembers what was last pushed for each job so unchanged jobs are skipped.
CREATE TABLE notion_sync (
  job_id            uuid PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
  notion_page_id    text NOT NULL UNIQUE,
  last_pushed_at    timestamptz,
  last_local_hash   text                               -- hash of mapped fields at last push
);

-- Every change that must reach Notion is written here in the SAME transaction as the change itself,
-- then a worker delivers it with retries. A failed or interrupted push is therefore never lost.
CREATE TABLE notion_outbox (
  id              bigserial PRIMARY KEY,
  job_id          uuid REFERENCES jobs(id) ON DELETE CASCADE,   -- NULL for 'archive' (the job row is already gone)
  notion_page_id  text,                                         -- needed for 'archive'
  op              text NOT NULL CHECK (op IN ('upsert','archive')),
  status          text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','done','failed')),
  attempts        int  NOT NULL DEFAULT 0,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_error      text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  done_at         timestamptz
);
-- at most one pending upsert per job: repeated edits coalesce into a single push
CREATE UNIQUE INDEX notion_outbox_pending_upsert_uq ON notion_outbox (job_id) WHERE op = 'upsert' AND status = 'pending';
CREATE INDEX notion_outbox_due_idx ON notion_outbox (next_attempt_at) WHERE status = 'pending';

-- keep updated_at honest
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END $$ LANGUAGE plpgsql;
CREATE TRIGGER jobs_touch BEFORE UPDATE ON jobs FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- Legal status transitions (seed; lives here because the CHECK-like logic depends on it).
INSERT INTO status_transitions (from_status, to_status) VALUES
  (NULL,               'Applied'),
  ('Applied',          'OA / Assessment'),
  ('Applied',          'Recruiter Screen'),
  ('Applied',          'Interviewing'),
  ('Applied',          'Rejected'),
  ('Applied',          'Withdrawn'),
  ('Applied',          'Ghosted'),
  ('OA / Assessment',  'Recruiter Screen'),
  ('OA / Assessment',  'Interviewing'),
  ('OA / Assessment',  'Rejected'),
  ('OA / Assessment',  'Withdrawn'),
  ('OA / Assessment',  'Ghosted'),
  ('Recruiter Screen', 'OA / Assessment'),
  ('Recruiter Screen', 'Interviewing'),
  ('Recruiter Screen', 'Rejected'),
  ('Recruiter Screen', 'Withdrawn'),
  ('Recruiter Screen', 'Ghosted'),
  ('Interviewing',     'Offer'),
  ('Interviewing',     'Rejected'),
  ('Interviewing',     'Withdrawn'),
  ('Interviewing',     'Ghosted'),
  ('Offer',            'Withdrawn'),
  ('Offer',            'Rejected'),
  ('Ghosted',          'Recruiter Screen'),   -- they came back
  ('Ghosted',          'OA / Assessment'),
  ('Ghosted',          'Interviewing'),
  ('Ghosted',          'Rejected');

-- ───────────────────────────── handy views ─────────────────────────────
-- Notion "Pipeline" board view
CREATE VIEW v_pipeline AS
SELECT j.status, j.id, c.name AS company, j.role, j.level, j.salary_min_lpa, j.salary_max_lpa, j.date_applied
FROM jobs j JOIN companies c ON c.id = j.company_id
WHERE j.stage = 'Applied';

-- Funnel: how far do applications get?
CREATE VIEW v_funnel AS
SELECT to_status AS status, count(DISTINCT job_id) AS jobs
FROM status_events GROUP BY to_status;

-- Tech demand across everything you've tracked (for deciding what to study).
CREATE VIEW v_tech_demand AS
SELECT t.name, t.category, count(*) AS postings,
       count(*) FILTER (WHERE jt.kind = 'required') AS as_required
FROM job_technologies jt JOIN technologies t ON t.id = jt.tech_id
GROUP BY t.name, t.category ORDER BY postings DESC;


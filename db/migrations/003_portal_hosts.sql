-- Company career domains the app has worked out by itself (careers.example.com -> the job system behind it).
-- `entry` has the same shape as an entry in config/hosts.json, which still wins when both name a host.
CREATE TABLE IF NOT EXISTS portal_hosts (
    host       text PRIMARY KEY,
    entry      jsonb NOT NULL,
    evidence   text NOT NULL,
    learned_at timestamptz NOT NULL DEFAULT now()
);

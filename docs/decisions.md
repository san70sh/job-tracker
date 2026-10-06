# Architectural decisions

Each entry: what was decided, why, and what it costs. Newest decisions that changed an earlier one say so.
Status of every decision is **accepted** unless noted.

| # | Decision | One line |
|---|---|---|
| 1 | Deterministic first, LLM only as a narrow fallback | Rules fill ~all fields; the model fills only what rules cannot |
| 2 | Python + FastAPI + plain HTML/JS, no Node | One language, nothing to build |
| 3 | Use the existing PostgreSQL 18 service, not a private cluster | Supersedes the original private-PG14 design |
| 4 | Synchronous DB access, called from async code via threads | Avoids Windows event-loop trouble |
| 5 | The schema mirrors the Notion database 1:1 | Lossless migration, familiar shape |
| 6 | One vocabulary file is the source of truth for tags | `config/vocab.json` |
| 7 | Four extraction pathways behind one adapter interface | Documented API, undocumented JSON, iCIMS, generic |
| 8 | Per-host configuration for sites the URL cannot identify | `config/hosts.json` |
| 9 | Fixed confidence scores and a `needs_review` flag | Honest about what was guessed |
| 10 | Keep raw snapshots and per-field provenance | Re-parse later; audit what to trust |
| 11 | India-only, INR-only salary, benchmarks entered by the user | No FX, no scraping |
| 12 | Notion is a one-way, read-only mirror driven by an outbox | The app is the only writer |
| 13 | Board watcher feeds a temporary working table with a defined lifecycle | Not a history |
| 14 | Scope cuts: ghost sweep, reminders table, FX, fit scoring | Removed, not hidden |
| 15 | Workday answers closed postings with 403 | Treated as "posting gone" |
| 16 | Real-data scoring harness against the Notion export | The deterministic path is measured, not assumed |
| 17 | Single user, localhost only, no authentication | Simplest safe default |
| 18 | `intake_queue` kept but unused | **Open**: likely to be dropped |
| 19 | Two database roles and a two-command setup script | Least privilege; the admin password is never stored |

---

## 1. Deterministic first, LLM only as a narrow fallback
**Context.** Each application previously cost a full Claude pass: read the posting, fill ~19 properties, tag technologies, research salary, write notes.
**Decision.** Everything that can be read from structured data or rules is done in code (ATS APIs, JSON-LD, heading-based section splitting, regex for experience/work mode/level, a dictionary for technologies). The LLM runs only when a *hard* field (title, company, location, responsibilities, requirements) is missing or low-confidence. Soft fields (team, level, work mode, experience) ride along in that call but never trigger one. Output is validated and cached by input hash in `llm_calls`.
**Consequences.** With no API key the app still works; a minority of postings get flagged for review instead. Measured on 100 of your real links, about half still needed the fallback after tuning, mostly from the generic HTML path. The fallback has not yet been run against the live API.

## 2. Python + FastAPI + plain HTML/JS, no Node
**Decision.** FastAPI serves JSON and Jinja templates; the UI is hand-written HTML with vanilla JavaScript and server-sent events. No bundler, no framework.
**Why.** One toolchain (Python 3.12 + uv). Node was not installed, and nothing in v1 needs it.
**Consequences.** UI changes are plain file edits. If the UI grows (resume comparison, charts) a framework can be introduced later without touching the API.

## 3. The existing PostgreSQL 18 service (supersedes the private-cluster design)
**History.** The first design ran a private PostgreSQL 14 cluster inside the project (`.pgdata/`, port 5433, trust authentication) because the system server's password was not available and its settings were not ours to change. That cluster only ever held development data. When the system PostgreSQL was upgraded to 18.6 and PostgreSQL 14 was uninstalled, the private cluster could no longer start, and `.pgdata/` was deleted.
**Decision.** The app uses the system PostgreSQL service (`localhost:5432`, PostgreSQL 15 or newer, developed on 18.6) through the `DATABASE_URL` in `.env`. The project no longer starts, stops or hosts a database server. See decision 19 for the roles and setup scripts.
**Consequences.** One server to maintain, with real authentication. The database now exists whenever the Windows service does, so there is nothing to start by hand. A password is required and lives in the git-ignored `.env`. The schema was checked against the PostgreSQL 18 release notes and loads cleanly on 18.6; it still avoids features newer than PG14 (two partial unique indexes instead of `NULLS NOT DISTINCT`), which keeps the option of simplifying later. Docker is not required; a Compose file may be added later for packaging.

## 4. Synchronous database access via threads
**Decision.** `psycopg` with a sync connection pool. Async code (FastAPI handlers, the pipeline) calls the repository through `asyncio.to_thread` or runs DB-bound handlers as plain `def`.
**Why.** psycopg's async mode needs a selector event loop, which is awkward on Windows. Database calls are short; threads are simple.
**Consequences.** All SQL lives in `repo.py`. The pool is capped at 8 connections.

## 5. The schema mirrors the Notion database 1:1
**Decision.** `jobs` carries all 19 Notion properties with identical meaning (Key Technologies becomes `job_technologies`; the page body becomes `job_sections`). Status options, work modes and the 100 technology tags are copied verbatim. Extra columns hold pipeline state (stage, canonical URL, ATS, posted date, numeric experience, review flag).
**Consequences.** Migration from Notion is lossless and the mirror back to Notion is a straight mapping. A pre-apply stage (`Inbox`) exists that Notion never had.

## 6. One vocabulary file for tags
**Decision.** `config/vocab.json` holds the 100 technologies, their categories, alias regexes, matching quirks (for example `Go` is case-sensitive and context-gated; `Express` needs `Express.js`) and the title-to-level rules. The tagger reads it directly; `seed` mirrors only the names into the `technologies` table for foreign keys.
**Consequences.** Tests need no database. An earlier design had alias and level tables in SQL; they were removed to avoid two sources of truth.

## 7. Four extraction pathways behind one adapter interface
**Decision.** Every source is an adapter with the same methods (`identify`, `fetch`, `list_postings`). They fall into four pathways: P1 documented JSON (Greenhouse, Lever, Ashby, SmartRecruiters), P2 undocumented JSON the career site itself calls (Workday, Oracle Cloud, Eightfold, Amazon Jobs), P3 iCIMS (Jibe JSON; classic portals via JSON-LD), P4 generic (JSON-LD, then page text). A failing structured adapter degrades to P4 instead of failing the job.
**Consequences.** Amazon Jobs was added beyond the original list because it is about 12% of the Notion data. Undocumented endpoints can change; captured real responses are kept as test fixtures so a break is caught immediately. See `extraction-pathways.md`.

## 8. Per-host configuration
**Decision.** `config/hosts.json` maps custom career domains to an ATS, company name and settings (for example `careers.servicenow.com` is SmartRecruiters `ServiceNow`; `apply.careers.microsoft.com` is Eightfold domain `microsoft.com`). Resolution order: hosts file, URL patterns, an HTML fingerprint for Jibe, then the generic parser. Tenant details (Workday site, Oracle site number) come from the real URL and are never guessed.
**Consequences.** A new vanity domain is a one-line config change.

## 9. Fixed confidence scores and a review flag
**Decision.** Each extracted field carries a method (`ats_api`, `jsonld`, `rules`, `llm`, `manual`) and a fixed confidence: ATS values 0.90 to 0.99; section-based text 0.85 (0.40 to 0.45 when no heading was recognised); text rules for level and work mode 0.70; LLM output 0.70; manual edits 1.00. A job is flagged `needs_review` when a hard field stays below 0.8. Saving edits that fill every hard field clears the flag, as does an explicit "Mark reviewed".
**Consequences.** The numbers are heuristics, not calibrated probabilities. LLM-filled values are deliberately below the threshold, so they always get a human look.

## 10. Raw snapshots and per-field provenance
**Decision.** `job_snapshots` stores the original ATS payload (and `job_sections` the parsed text); `field_provenance` records method and confidence per field; `llm_calls` caches model calls.
**Why.** Postings disappear (16 of 100 Notion links were already dead), and parsers improve.
**Consequences.** These are optional conveniences, not core. A re-extract command does not exist yet, and generic-HTML jobs store only the HTML length, so they cannot be re-parsed.

## 11. India-only, INR-only salary; benchmarks entered by the user
**Decision.** Salary resolves in two tiers: a pay range printed in the posting (INR only), then cached rows in `salary_benchmarks` that the **user** imports from CSV. Any non-INR range is ignored; there is no FX table. Nothing scrapes salary sites and no LLM fills salary.
**Why.** No salary source relevant to India offers a public API (Naukri, AmbitionBox and Glassdoor have none; Payscale is commercial; Levels.fyi sells a data licence), and scraping them is both brittle and against their terms.
**Consequences.** Expect blank salary on most Indian postings until benchmarks exist. Planned: seed benchmarks from the researched ranges already in the Notion rows, then an optional cited "research" action (see `roadmap.md`).

## 12. Notion is a read-only mirror, driven by an outbox
**Decision.** After migration the app is the only writer. Every change is inserted into `notion_outbox` in the same database transaction as the change; a worker (every minute) delivers it. Repeated edits to one job coalesce into one push; failures back off (2, 4, 8 ... up to 60 minutes) and park as `failed` after 8 attempts, shown as a red chip in the header. Calls are paced at 2.5 requests/second against Notion's roughly 3/second average limit, 429 `Retry-After` is honoured, text is chunked below 2,000 characters. Deleting a job queues the page for trashing. Only applied jobs are mirrored. `notion-reconcile` detects hand-edits, missing pages, orphans and duplicates; `--fix` overwrites Notion with the app's values.
**Consequences.** A crash or Notion outage never loses a change. Read-only is enforced by overwrite, not prevention: Notion cannot be locked against its own owner, so set your own access to "can view". The page body is written once at creation. Not yet exercised against a real workspace.

## 13. Board watcher feeds a temporary working table
**Decision.** `discovered_postings` is a working set, not a history. States: `baseline` (open when a board was first checked; silent), `filtered` (failed the board's filters; silent), `new`, `seen` (both are "undecided" and trigger reminders), `added`, `not_interested`, `closed`. A nightly purge (03:30, and once at startup) deletes `added` and `closed`. A posting only becomes `closed` if the board listing was *complete*: adapters report when they stopped at a page cap. Postings already tracked as jobs are never re-surfaced, which is what makes purging `added` rows safe.
**Deviation from the first spec.** `not_interested` rows are removed when the posting leaves the board, not immediately; deleting them sooner would let the next poll show the same posting as new again.
**Reminders.** Anything undecided for more than `REMINDER_AFTER_DAYS` (default 2) shows a persistent banner on every page and a daily 09:00 popup.

## 14. Scope cuts
Removed after review, with the reason: the **ghost sweep** and `reminders` table (not wanted; "Ghosted" stays a status you set by hand), **`fx_rates`** and currency columns (India-only), and the **`profiles`, `profile_skills`, `fit_results` tables plus `jobs.fit_score`** (fit scoring is deferred to v2; they will be redesigned with the resume feature rather than kept as dead schema).

## 15. Workday answers a closed posting with 403
**Finding.** For tenants where the list endpoint works, the detail endpoint returns `403 errorCode S22 permission denied` for a posting that has been closed, not 404.
**Decision.** Treated as "posting gone".

## 16. A real-data scoring harness
**Decision.** `scripts/golden.py` runs the deterministic pipeline over the Notion export (100 rows Claude filled in) and reports field agreement, technology recall/precision and how many rows would still need the LLM. It needs the network but no database.
**Why.** The claim "rules can replace Claude" is only credible if measured. It found real defects (Workday closed-posting handling, Amazon coverage, heading vocabulary gaps).
**Consequences.** Results drift as postings close; it is a development tool, not a pass/fail test.

## 17. Single user, localhost, no authentication
**Decision.** No login, no multi-tenancy. The server binds to 127.0.0.1 by default.
**Consequences.** Do not expose port 8000 to a network. The PostgreSQL service should likewise stay bound to localhost (check `listen_addresses` and the Windows firewall). Adding auth is a prerequisite for any remote use.

## 18. `intake_queue` is kept but unused (open)
URLs are processed synchronously in the request, a few seconds each, so a background queue adds nothing today. It would matter for bulk paste or a browser extension. Recommendation: drop it unless one of those is planned.

## 19. Two database roles and a two-command setup script
**Decision.** Two roles, each with its own password:
- `jobtracker`, the app role: owns the `jobtracker` database, can log in and has `CREATEDB` (the tests create a throwaway database). Its password is the one in `DATABASE_URL` in `.env`.
- `jobtracker_ro`, a read-only role for browsing the data in DBeaver: `CONNECT`, `USAGE` on `public` and `SELECT` on all current and future tables and views (via default privileges granted `FOR ROLE jobtracker`). Its password is never stored in the project.

`scripts/db.ps1` has two deliberately separate commands. `bootstrap` is a one-time step run by the person who knows the `postgres` admin password: it creates the roles and the database, and can be re-run safely. `apply` runs as the app role only, loads the schema and the technology tags, and never needs the admin password.
**Why.** Creating roles and databases needs a superuser; applying the schema does not, so routine runs should not use the admin credential. Splitting the two keeps the admin password out of files and out of the routine path, and keeps a read-only account available for casual inspection.
**How secrets are handled.** Passwords reach `psql` through environment variables that live only for the duration of the script, never on a command line (where other processes could see them) and never on disk. Role and database names are validated as plain lowercase identifiers before being used in SQL.
**Consequences.** `apply` does not migrate: if the tables exist it leaves them alone, so future schema changes need a migration mechanism. `.env` must exist, with the password, before `bootstrap` can set the app role's password. A server that logs DDL statements could record the `ALTER ROLE ... PASSWORD` text, so keep `log_statement` at its default (`none`).

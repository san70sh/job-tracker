# Walkthrough

How to start the app, what it can do, and every API endpoint. Commands are PowerShell, run from the project folder.

## 1. Startup

### Prerequisites
- A **PostgreSQL 15+ service** (developed against 18.6) reachable at `localhost:5432`, with its `postgres` admin password. The app does not run its own database.
- The PostgreSQL client tools (`psql`) on this machine; `db.ps1` finds `psql.exe` on `PATH` or under `C:\Program Files\PostgreSQL\<newest>\bin`, or use `$env:PSQL_PATH`.
- Python 3.12 and `uv`. Dependencies install automatically the first time you use `uv run`.

### One-time setup
1. **Create `.env`.** Copy `.env.example` to `.env`, and in `.env` uncomment the `DATABASE_URL` line and replace `YOUR_PASSWORD` with a new password you choose for the app role. Edit it in VS Code (Notepad may save `.env.txt`). `.env` is git-ignored; never paste its contents into a chat.
2. **Preview the bootstrap** (changes nothing, asks for no password):
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\db.ps1 bootstrap -DryRun
   ```
3. **Bootstrap.** Prompts for the `postgres` admin password (kept in memory only) and a new password for the read-only role:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\db.ps1 bootstrap
   ```
   It creates the app role `jobtracker` (can log in, `CREATEDB`, owns the database), the read-only role `jobtracker_ro`, and the `jobtracker` database, and grants the read-only role `SELECT` on everything now and in future. Safe to run again: existing roles and the database are kept and passwords refreshed. Add `-RestrictPublic` to stop other roles from connecting to the database.
4. **Apply the schema.** Runs as the app role (no admin password); creates the 17 tables and 3 views and loads the 100 technology tags. If the tables already exist it skips the schema step.
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\db.ps1 apply
   ```

### Every time
```powershell
uv run python -m jobtracker serve
```
Open **http://127.0.0.1:8000**. Stop the app with Ctrl+C. Change the port with `serve --port 8001`. The PostgreSQL service is a normal Windows service and starts with Windows; if the page loads but actions fail with a connection error, check that the service is running and that `.env` is correct.

### `db.ps1` reference
| Command | Runs as | Effect |
|---|---|---|
| `bootstrap [-DryRun] [-AdminUser postgres] [-ReadOnlyRole name] [-RestrictPublic]` | the admin role | create the roles and the database, grant read-only access |
| `apply` | the app role (password from `.env`) | load `db\schema.sql` if the tables are missing, then the technology tags |

`apply` runs `python -m jobtracker migrate` (see `src/jobtracker/migrate.py`): on an empty database it loads `db\schema.sql`, the frozen baseline; then it applies every file in `db\migrations` it has not applied before, in name order, each in its own transaction and recorded with a checksum in `schema_migrations`. Schema changes are therefore always a new migration file, never an edit to `schema.sql` or to an applied file (the command warns if one was edited). Migrations only go forward; take a backup (`pg_dump` or DBeaver) before the first `apply` after an upgrade. `migrate --status` shows what is pending without changing anything, and the app logs a warning at start when migrations are pending. There is also no reset command. To start over, drop the `jobtracker` database as the admin (for example in DBeaver) and run `bootstrap` then `apply` again; **that destroys all data**.

### Browsing the data (DBeaver)
Create a connection with host `localhost`, port `5432`, database `jobtracker`, user **`jobtracker_ro`** and its password. That role can read every table and view but cannot change anything. Editing rows directly (as the app role or admin) bypasses the app: the allowed-status-move check, the "edited by hand" record, and the queue that sends changes to Notion all live in the app's code, so a direct edit is neither validated nor mirrored.

### Settings
Copy `.env.example` to `.env`. Only `DATABASE_URL` (with the password) is required; everything else has a default.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | none for the password; fallback `postgresql://jobtracker@localhost:5432/jobtracker` | connection for the app role. Encode `@ / # : %` and spaces in the password as `%40 %2F %23 %3A %25 %20`, or use the key-value form `host=localhost port=5432 dbname=jobtracker user=jobtracker password=...` |
| `ANTHROPIC_API_KEY` | unset | enables the LLM fallback; without it, weak jobs are only flagged for review |
| `LLM_MODEL` | `claude-haiku-4-5` | model used by the fallback |
| `NOTION_TOKEN`, `NOTION_DATA_SOURCE_ID` | unset | enable the Notion mirror (see section 3.6) |
| `REMINDER_AFTER_DAYS` | `2` | how long a discovered posting may wait before the banner appears |
| `CONFIDENCE_THRESHOLD` | `0.8` | below this, a hard field counts as weak |
| `PER_HOST_DELAY` | `1.0` | seconds between requests to the same host |

### Check it works
```powershell
uv run pytest
```
93 tests. The database-backed ones connect with your `.env`, create a throwaway `jobtracker_test` database on the same server (the app role needs the `CREATEDB` permission that `bootstrap` grants), and drop it afterwards; your real database is never touched. They are skipped, with the reason shown, if the server cannot be reached.

## 2. What it can do

- **Add a job from a URL.** Greenhouse, Lever, Ashby, SmartRecruiters, Workday, Oracle Cloud, Eightfold, Amazon Jobs, DocuSign's Jibe site, Avature sites (`*.avature.net`) and classic iCIMS pages are read from their data; anything else falls back to the page's structured data or text. Custom career domains are mapped in `config/hosts.json`.
- **Fill the Notion properties automatically:** title, company, job ID, location, work mode, level, experience, team, responsibilities, requirements, technologies (from your 100 tags, required vs nice-to-have), posted date, and salary when the posting prints an INR range or you have imported benchmarks.
- **Reject duplicates** by URL (tracking parameters are ignored) and by company plus requisition ID.
- **Track the pipeline** on a board with your 8 statuses. Illegal jumps are refused; every change is logged with its source.
- **Flag weak extractions** for review, show where each field came from, and let you correct anything.
- **Watch company boards** and show a popup when new roles appear, with a reminder banner for ones you have not decided on.
- **Mirror to Notion** one way, with retries, and check for drift.
- **Optional LLM fallback** for postings whose structure the rules cannot read.

## 3. Using it

The top bar has four tabs: **Home**, **Pipeline**, **Inbox** (postings from watched boards; the badge counts new ones) and **Company boards**.

### 3.1 Add a job (Home, `/`)
One box in the middle of the page. It decides what you pasted: a single `http(s)://` link is a **URL**, anything else is **full text**. A chip under the box shows which, and "treat as full text / URL" flips it.
- **URL:** press **Add job** (tick *already applied* to mark it applied today). A toast says "Added" (with a link), "Already tracked", or why it failed. Closed postings are reported as gone. LinkedIn, and any site that answers 401, 403 or 999 or serves a login page, is not fetched: the page says so and the box waits for you to paste the posting text, which is then saved together with that link.
- **Full text** (for LinkedIn, Indeed, emails, PDFs): press **Review**. Nothing is saved yet; you see every field the rules found as an editable form. Company and job title are required; values the rules were unsure of (or could not find) are highlighted yellow. **Save for later** puts the job in the Inbox column; **Save as applied** also marks it applied today. A repeated paste is refused and links to the existing job.
- The text path reads the company and title from labels (`Company:`, `Job Title:`), the job-board header line (`Company · Location (Hybrid) · 2 weeks ago · 45 applicants`), or the first role-like line; headings are guessed from plain text (a short line such as "Responsibilities", an ALL CAPS line, or a line ending in a colon). The link, if the text contains one, is kept; a LinkedIn or Indeed link also supplies the Job ID. Fields you change are stored as manual, the rest as rules, and the pasted text is kept as the snapshot.

**Browser extension (best for LinkedIn and Indeed).** Instead of copying text, load the Chrome extension in `extension/` (setup in its README) and click its toolbar button on a job page. It reads the page you are viewing, sends it to the app with a shared token (`CAPTURE_TOKEN` in `.env`, made by `python -m jobtracker capture-token`), and opens the same review form in a small pop-up window. Company, title and location come from the page itself, so they are trusted rather than guessed. The job link is the company's own posting when the page links to it ("Go to company site"), otherwise the LinkedIn address of the job. A whole LinkedIn page is cut down to the posting: the card, the text from "About the job" up to "Set alert for similar jobs" or the insights, with application status, people to contact, applicant statistics and the legal footer dropped. When the company link is a careers position page (a recognised ATS, a JobPosting data block, or a careers-style address with real job sections), the job is read from that page instead of LinkedIn's text; the review window says which source it used and offers **Use the LinkedIn text instead**. Other links (a seller portal, a blog, another job board) are ignored and the reason is shown. Captures wait 30 minutes in memory for you to review them.

### 3.2 The pipeline (`/pipeline`)
A **summary strip** at the top shows every status with its job count (Inbox, Applied, OA / Assessment, ...). Below it, one column per status that has jobs, plus **Inbox** for jobs you have added but not applied to; empty statuses are hidden unless you tick **show empty columns**. Each column scrolls on its own, so a long Applied column does not stretch the page. Click a chip in the strip to **focus** that status (its jobs fill the page in a grid); click it again, or **Clear all**, to go back. Drag a card to a column, or onto a chip in the strip (the only way to reach a hidden empty status), to change its status; or press **Mark applied** on an Inbox card. Cards are compact: title (two lines at most), company and applied date, a one-line summary (level, city, work mode, salary) and the first three technologies. Hover for the full text. A yellow "review" tag means some fields are weak. **Colours:** each status has one colour, shown on its chip, its column's top edge, the left edge of its cards and the pill in a job's header; work mode is a small pill (Onsite grey, Hybrid blue, Remote green) and salary is green. Column scrollbars are invisible until you point at a column or scroll it, then a thumb in that column's colour fades in and fades out again.

Above the board:
- **Search** matches the title, job ID and company; type several words and every word must match ("amazon sde").
- **Company** and **Location** are multi-select dropdowns with counts and a find box. Within one filter the choices are OR (Hyderabad + Pune shows both); across filters, search and filters are AND. Locations are clean city tags derived from the free-text location (Bangalore and Bengaluru are one; a location that names no confirmed city is **Unspecified**; "Remote" is its own tag). The cities and their spellings are in `config/vocab.json`.
- **Order** sorts every column by applied date, newest first by default (oldest first is one click away). Jobs with no applied date go last.
- **Clear all** resets everything. Your choices are remembered in the browser.

### 3.3 A job (modal, or `/jobs/{id}`)
Clicking a job anywhere (a card, a Recently added row, a link in a toast) opens it in a **modal** over the page you were on, with the page behind blurred and dimmed. Your filters, search and scroll position are untouched. **Esc**, the ✕, or a click on the blurred area closes it (it asks first if you have unsaved edits); the browser's Back button closes it too, and the address changes to `/jobs/{id}` so a refresh or a shared link opens the same job as a full page. **←** and **→** (or the ‹ › buttons) step through the jobs the page is showing, in the order shown, so filters apply; **Next to review ›** jumps to the next job flagged for review. Ctrl, Shift or middle click, and links that open in a new tab, still give the full page.

A job tagged for review lists exactly which of title, company, key responsibilities and requirements is missing ("not found") or was read with low confidence ("not sure it is right"); each is highlighted, and drops off the list once you save a value for it. The tag clears when none are left, or at once with **Mark reviewed** (which a later edit does not undo). Location and the soft fields never tag a job. Edit any field, including the **Company**, and **Save**. Only the fields you actually changed are saved and recorded as manual (confidence 1.00); saving with nothing changed does nothing. Renaming the company moves the job to that company and removes the old company if nothing else uses it. Saving a job whose title, responsibilities and requirements are all filled clears the review flag, or press **Mark reviewed**. The page also shows the stored posting sections, technologies (dashed = nice-to-have), the status history, and for each field whether it came from the ATS, rules, the LLM or you. **Sync to Notion now** pushes immediately. **Delete** removes the job and queues its Notion page for the trash.

### 3.4 Watch company boards (`/boards`, `/inbox`)
1. On **Company boards**, paste a board URL, or any job URL on that board (for example `https://boards.greenhouse.io/stripe`). A careers site the app does not know by address is opened and read: it works out which supported job system is behind it (see section *Careers sites it does not know* in `docs/extraction-pathways.md`), checks that the system lists jobs, and says how it found it. Only when that fails is there an error, and it says what was looked at. A domain whose own pages turn out to be the job system's front end is remembered (table `portal_hosts`), so the next paste from it needs no looking; `config/hosts.json` always wins. Add filters if you want: **Title must match** (any one of the entries), **Title must not match**, and **Location**. **Preview matches** shows how many open postings the filters keep right now, with a few titles, before you save anything.
2. **Filter entries** are plain words matched as whole words in any case (`java`, `software engineer`); wrap one in slashes for a regular expression (`/java|kotlin/`). A place that names a known city also matches its other spellings (Bengaluru finds Bangalore). Each field offers suggestions drawn from the jobs you applied to, five first with a "more…" link; typing searches all of them, and any text you type is accepted. With no applied jobs the fields simply take text. "Must not match" suggests words that never appear in any title you applied to (intern, director, manager...).
3. **Workday boards** are narrowed at the source: the Location entries are matched against the tenant's own list of offices and only those are requested, so a role in several offices (Workday lists it as "2 Locations") is kept. The offices are looked up on every check, so a new office that matches is picked up. Filters in a pasted Workday address (`?locations=...`) are applied too. Other systems filter by the place text of each posting.
4. Only postings **posted in the last 2 days** (`max_age_days` in `config/boards.json`) that pass the filters are announced; older ones are stored silently. A posting with no posted date (Avature lists none) is kept, because it cannot be shown to be old. Workday only says "Posted 3 Days Ago", which is read as a date. Postings already waiting in the Inbox are never hidden for age.
5. The **first good check** records what is already open silently. Only postings that appear later are announced; with a title filter, matching existing postings are announced straight away. A board already watched is never added twice, whatever address you paste (it opens for editing instead).
6. Each row shows the last check: postings listed, how many were new, how long ago, or the error if it failed, and a warning when there has been no good check for 24 hours. **Edit** changes the company name, the three filters, the check interval and whether the board is **active** (paused boards are skipped); renaming the company renames it on every saved job (the dialog says how many), and merges into an existing company of that name. After an edit, postings waiting in the Inbox that no longer match leave it, and ones that now match return quietly. **Check now** checks one board immediately.
7. New postings pop up and add to the **N new** badge. In **Inbox** (`/inbox`, oldest first) press **Add to tracker** or **Not interested**. Anything undecided for more than 2 days shows a banner on every page, plus a daily 09:00 popup. Each night added and closed postings are purged; not-interested postings are removed once they leave the board.

Each board is checked every 3 hours by default (choices in `config/boards.json`; the default itself is the database column's). A scheduler wakes every 15 minutes while the app runs and checks the boards that are due, so closing the app pauses checking; the next check compares against what it already knows. A failed check counts as an attempt: that board waits for its interval instead of being retried every tick.

### 3.5 Salary
A posted range is used only when it is in rupees. Otherwise the app looks for benchmark rows matching company, level and city. Import them from a CSV:
```powershell
uv run python -m jobtracker bench-import benchmarks.csv
```
```csv
company,level,location,total_annual,kind,source,source_url,years_exp
ServiceNow,Senior,Hyderabad,5500000,aggregate_avg,Levels.fyi,https://www.levels.fyi/...,
ServiceNow,Senior,Hyderabad,4400000,self_reported,LeetCode,,5
```
`total_annual` is INR per year. `level` uses the app's ladder: Intern, Junior, Mid, Senior, Staff, Principal, Manager, Director. `location` matches as a substring of the job's location (`India` matches any Indian location). With several matching rows the job gets the lowest-to-highest band.

### 3.6 Notion (optional, one-way)
1. In Notion create an internal integration and copy its token. Share your Job Tracker database with it.
2. Put `NOTION_TOKEN` and `NOTION_DATA_SOURCE_ID` in `.env`. For your current database the data source ID is `8f504589-0d99-4005-8276-8ce43e6a50e1`.
3. Migrate once: `uv run python -m jobtracker notion-import` (use `--dry-run` first). Existing rows are never updated by import.
4. Sync: `uv run python -m jobtracker notion-push`. After that, every change is queued and delivered automatically while the app runs.
5. Treat Notion as read-only. Check for drift with `notion-reconcile`; add `--fix` to overwrite hand-edits, re-create missing pages. Orphans and duplicates are only reported.
6. A red **Notion: N pushes failed** chip means pushes gave up; click it to retry, or run `notion-retry`.

Notion's property names must match exactly (Role, Company, Status, Date Applied, Job ID, Job Link, Level, Location, Work Mode, Experience Required, Team / Domain, Key Responsibilities, Requirements, Key Technologies, Notes, Salary Min (LPA), Salary Max (LPA), Salary Details, Salary Sources).

### 3.7 Command-line reference
`uv run python -m jobtracker <command>`

| Command | Purpose |
|---|---|
| `serve [--host H] [--port P]` | run the web app |
| `ingest URL [--dry-run]` | add one job; `--dry-run` prints the extracted fields and stores nothing |
| `add-board URL [--company C] [--include RE]... [--exclude RE]... [--location RE]...` | watch a board |
| `poll [--all]` | check due boards now (`--all` ignores the interval) |
| `purge` | purge added and closed discovered postings now |
| `bench-import FILE.csv` | import salary benchmarks |
| `seed` | (re)load technology names into the database |
| `notion-import [--dry-run]` | one-time import of existing Notion rows |
| `notion-push [--include-inbox]` | queue every applied job and deliver |
| `notion-reconcile [--fix]` | compare Notion with the app |
| `notion-retry` | retry parked pushes |
| `backfill-experience [--dry-run]` | fill the numeric experience years (`experience_min_years` / `max`) from the experience text, for jobs imported from Notion; skips text that says "Not stated" |
| `backfill-team [--dry-run]` | infer a blank Team / Domain from the stored title and sections; never overwrites a value, and jobs imported from Notion (no stored sections) are left alone |
| `migrate [--status]` | bring the database up to date (baseline on an empty one, then pending migrations); `--status` only reports |
| `reparse [--dry-run] [--job ID]` | re-run extraction on saved jobs from the response stored with each one (no refetch), so jobs saved before an extractor improved get the better text. Updates responsibilities, requirements, experience, team (if blank), sections and technologies; never touches a field you edited (provenance `manual`), nor title, company, location, level or salary. Rebuilds Workday, Greenhouse, Lever, Ashby, SmartRecruiters, Oracle, Eightfold, Jibe and Amazon responses and pasted or captured text; generic HTML pages and jobs imported from Notion have nothing stored and are skipped. Run with `--dry-run` first |

Development scripts: `scripts\capture_fixtures.py` re-records real ATS responses into `tests\fixtures`; `scripts\golden.py` scores the pipeline against `tests\fixtures\notion_golden.json`.

### 3.8 Scheduled work (runs inside `serve`)
| What | When |
|---|---|
| Board polling | every 15 minutes, plus once 20 s after start; each board by its own interval (default 3 hours) |
| Notion outbox delivery | every minute |
| Purge of added/closed discovered postings | 03:30 daily, plus once 30 s after start |
| Overdue-postings reminder | 09:00 daily, plus once 10 s after start |

## 4. API reference

Base URL `http://127.0.0.1:8000`. Everything is JSON unless noted. There is no authentication. Interactive docs are served at `/docs`.

### Pages (HTML)
| Path | Page |
|---|---|
| `GET /` | home: the add box |
| `GET /pipeline` | the status board |
| `GET /jobs/{id}` | job detail |
| `GET /inbox` | postings awaiting a decision |
| `GET /boards` | company boards (watched) |

### Browser extension
| Endpoint | Body / params | Returns |
|---|---|---|
| `POST /api/capture` | header `X-Capture-Token`; `{url, text, top, hints: {title, company, location, confidence}, source}` | `{id, path}` for the review window. `401` wrong token, `503` no token configured, `422` text too short |
| `GET /api/captures/{id}` | | the parked `{text, link, hints, source}`; `404` once expired (30 minutes) |
| `GET /capture/{id}` | | the review pop-up page |

`/api/jobs/parse` and `/api/jobs/manual` also accept `hints`.

### Live events
`GET /events` is a server-sent event stream. Each message is `data: {"kind": ..., ...}` with kind one of `new_postings`, `poll_error`, `action_needed`, `job_added`, `status_changed`.

### Jobs
| Endpoint | Body / params | Returns |
|---|---|---|
| `POST /api/jobs` | `{"url": "...", "applied": false}` | `{status: created\|duplicate\|needs_text\|failed, message, job_id, needs_review, missing, ats, review}`. `422` when status is failed or needs_text (the site needs a login: paste the text instead) |
| `POST /api/jobs/parse` | `{"text": "...", "link": null}` | the preview, nothing saved: `{fields: {company, role, location, work_mode, level, experience_required, team_domain, job_ref, job_link, salary_min_lpa, salary_max_lpa: {value, confidence, method, evidence}}, technologies, counts, duplicate}`. `422` if the text is under 40 characters |
| `POST /api/jobs/manual` | `{"text": "...", "link": null, "fields": {...the confirmed values...}, "applied": false}` | same shape as `POST /api/jobs`; company and role are required (`422`); `duplicate` when the link or text is already tracked |
| `GET /api/jobs` | `?stage=Inbox\|Applied` (optional) | job summaries with technologies, `job_ref`, and `cities` (the clean location tags the board filters on) |
| `GET /api/jobs/{id}` | | full job with `technologies`, `sections`, `events`, `provenance` |
| `PATCH /api/jobs/{id}` | any of: `role, company, job_ref, level, location, work_mode, experience_required, team_domain, key_responsibilities, requirements, notes, salary_min_lpa, salary_max_lpa, salary_details, date_applied` | `{ok: true, changed: [field names that really changed]}`. Fields whose value is unchanged are ignored (no manual stamp, no Notion push). `409` if renaming the company would duplicate a Job ID, `400` for an empty company |
| `POST /api/jobs/{id}/status` | `{"status": "Interviewing", "note": "..."}` | `{ok: true}`; `409` for an illegal move, `400` for an unknown status |
| `POST /api/jobs/{id}/reviewed` | | `{ok: true}` |
| `DELETE /api/jobs/{id}` | | `{ok: true}` |
| `GET /api/funnel` | | `[{status, jobs}]`, jobs that ever reached each status |

### Discovery
| Endpoint | Returns |
|---|---|
| `GET /api/discovered` | undecided postings (`new` and `seen`), oldest first, with `age_days` |
| `GET /api/discovered/count` | `{new: n}` |
| `GET /api/discovered/summary` | `{new, seen, pending, overdue, oldest_days, overdue_after_days}` |
| `POST /api/discovered/{id}/add` | runs the pipeline; same result shape as `POST /api/jobs` |
| `POST /api/discovered/{id}/not-interested` | `{ok: true}` |
| `POST /api/discovered/seen-all` | marks all `new` as `seen` |

### Boards
| Endpoint | Body | Returns |
|---|---|---|
| `GET /api/meta` | | what pages need from the server: `inbox`, `statuses` (name and colour slug), `poll_intervals`, `default_poll_minutes`, `stale_after_hours`, `ats_colours` |
| `GET /api/boards` | | boards with filters, `poll_interval_minutes`, `enabled`, `last_polled_at`, `last_success_at`, `last_error`, `last_listed`, `last_new`, `new_count`, `company_jobs` |
| `GET /api/boards/suggestions` | | `{title_include, title_exclude, location_include}`, each `{top, more}` lists of `{value, count}` learned from applied jobs (empty lists when there are none) |
| `POST /api/boards` | `{"url": "...", "company": null, "title_include": [], "title_exclude": [], "location_include": [], "poll_interval_minutes": null}` | `{id, company, ats, created, found_by}`; `found_by` says what gave the system away when the careers page had to be read (null when the address was enough); `created: false` when that board is already watched (nothing is overwritten); `422` for a site no supported system was found behind (the message says what was looked at) or an invalid filter |
| `PATCH /api/boards/{id}` | any of `company`, `title_include`, `title_exclude`, `location_include`, `poll_interval_minutes` (one of the listed choices), `enabled` | `{ok, changed}`; `422` bad value, `404` unknown board, `409` a company merge clashed on a Job ID |
| `POST /api/boards/preview` | `{"board_id": 3 or "url": "...", filters...}` | `{listed, matched, recent, max_age_days, complete, narrowed_at_source, sample}`: what the filters would keep now; nothing is saved |
| `POST /api/boards/{id}/poll` | | `{new: n}` after checking that board now |
| `DELETE /api/boards/{id}` | | `{ok: true}` (its discovered postings go too) |
| `POST /api/boards/poll` | | `{new: n}` after checking every board now |

### Notion
| Endpoint | Returns |
|---|---|
| `GET /api/notion/status` | `{configured, pending, failed, last_error}` |
| `POST /api/jobs/{id}/notion` | queues the job and delivers now; `400` if Notion is not configured |
| `POST /api/notion/retry` | `{requeued, delivered, failed}` |

## 5. Troubleshooting
| Symptom | Cause and fix |
|---|---|
| Page loads, actions fail with a connection error | the PostgreSQL service is stopped, or `DATABASE_URL` in `.env` is wrong (password, port, special characters not encoded) |
| `db.ps1` stops with a message about `.env` | the message names the exact problem: no `.env`, a `.env.txt`, `.env` saved as UTF-16, no `DATABASE_URL` line, the line commented out with `#`, or no password in it. Fix that one thing and rerun |
| A command prints `error: ...` and exits | a fixable problem (Notion not configured, database unreachable, Notion refused the token or the database is not shared). The line says what to check |
| `db.ps1 apply` cannot connect | run `bootstrap` first (the role and database do not exist yet), or check `.env` |
| `db.ps1 apply` says the schema is already present | expected; it only reloads the technology tags |
| `bootstrap` says an extension is not available | install the PostgreSQL contrib package (`pg_trgm`, `pgcrypto`) and run it again |
| "posting is gone" for a link that looks fine | the posting was closed (Workday answers closed postings with 403; others 404/410) |
| A job is flagged for review | open it: the banner names the fields to check (title, company, responsibilities or requirements) and why (not found / not sure it is right), and they are highlighted. Correct them and Save, or Mark reviewed; set `ANTHROPIC_API_KEY` for the fallback |
| Salary is empty | the posting printed no INR range and no benchmark matches; import benchmarks |
| Red Notion chip | click it to retry; the tooltip shows the last error (often a wrong property name or an unshared database) |
| Tests skipped or failing on `CREATEDB` | the server is unreachable, or run `ALTER ROLE jobtracker CREATEDB;` as the admin |

# Job Tracker (self-hosted)

A self-hosted replacement for the Notion "Job Tracker" database. Paste a job URL, or let it watch company career boards; a deterministic pipeline fills in the record, and an LLM is only an optional fallback. Notion stays as a read-only mirror. India-only, single user, runs on localhost.

## Quick start
Needs a PostgreSQL 15+ service (developed on 18, listening on `localhost:5432`), Python 3.12 and `uv`. One-time setup:

1. Copy `.env.example` to `.env` and set `DATABASE_URL` with a password of your choosing for the app role.
2. Create the roles and database (asks for the `postgres` admin password):
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\db.ps1 bootstrap
   ```
3. Create or update the tables (applies pending migrations) and load the technology tags:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\db.ps1 apply
   ```

Then, any time:
```powershell
uv run python -m jobtracker serve        # open http://127.0.0.1:8000
```
Details, including how to browse the data in DBeaver, are in [docs/walkthrough.md](docs/walkthrough.md).

## What it does
- Reads postings from Greenhouse, Lever, Ashby, SmartRecruiters, Workday, Oracle Cloud, Eightfold, Amazon Jobs, DocuSign (Jibe), Avature and iCIMS pages; other sites fall back to their structured data or text.
- Fills all 19 Notion properties and the technology tags from rules; flags weak extractions for review and shows where each value came from.
- Tracks applications on a board with your 8 statuses and a full history.
- Watches company boards, pops up new postings, and nags about ones you have not decided on.
- Mirrors applied jobs to Notion one way, with a retrying queue and a drift check.

## Documentation
| Doc | Contents |
|---|---|
| [docs/walkthrough.md](docs/walkthrough.md) | startup, how to use it, command line, every API endpoint, troubleshooting |
| [docs/architecture.md](docs/architecture.md) | components, diagrams (system, ingest, status, discovery, Notion, data model), code map, limitations |
| [docs/decisions.md](docs/decisions.md) | the architectural decisions and why |
| [docs/extraction-pathways.md](docs/extraction-pathways.md) | how each source is read |
| [docs/roadmap.md](docs/roadmap.md) | v1.x hardening and v2 (resume import, skills, job comparison, scoring) |
| [docs/tooling.md](docs/tooling.md) | what is installed on this machine |
| [docs/diagrams.html](docs/diagrams.html) | all diagrams in one page (loads Mermaid from a CDN, needs internet) |

## Objectives
**Primary:** cut per-application effort and cost; own the data; keep the Notion shape; track the pipeline reliably.
**Secondary:** discover new postings from watched boards; trustworthy results (provenance, review flag); learn from the data (funnel, technology demand).
**Not in v1:** fit scoring and resume comparison (v2, see roadmap), multi-user use, auto-applying, email-alert parsing, scraping sites that forbid it.

## Layout
```
config/    vocab.json (tags, levels), hosts.json (custom career domains)
db/        schema.sql
docs/      documentation
scripts/   db.ps1, capture_fixtures.py, golden.py
src/jobtracker/   adapters/, extract/, web/, pipeline, repo, notion_sync, watcher, llm, ...
tests/     93 tests with real captured responses
```

## Status
Working and verified: the pipeline on real postings, the database layer, the web app over HTTP (add job, duplicate and illegal-move handling, boards, polling, event stream), and 56 automated tests. **Not yet run against the live services:** the LLM fallback and the Notion mirror. The project has no git commits yet.

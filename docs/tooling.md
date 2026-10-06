# Tooling

State of this machine (Windows 11) after setup.

| Tool | Used for | Status |
|---|---|---|
| Python 3.12 | the application | installed (winget, 3.12.10) |
| uv | environment and dependency management | installed (0.12.x); `uv run` syncs dependencies automatically |
| PostgreSQL 18.6 service | the database | the system service on `localhost:5432` (PostgreSQL 14 was uninstalled); the project uses a `jobtracker` database owned by the `jobtracker` role |
| psql (PostgreSQL client tools) | `scripts\db.ps1` | installed with PostgreSQL 18; the script finds it on `PATH` or under `C:\Program Files\PostgreSQL\<newest>\bin` |
| DBeaver | browsing the data | installed; connect as the read-only `jobtracker_ro` role |
| Git | version control | installed; the project folder is a git repository (I have made no commits) |
| VS Code | editing | installed |
| Docker Desktop | optional packaging | installed, **not used** (not running) |
| Node.js | nothing | not installed and not needed |
| Playwright | JavaScript-only sites | not installed; not needed so far |
| Ollama | optional local model | not installed; not needed |

## Python libraries (see `pyproject.toml`)
`fastapi`, `uvicorn`, `httpx`, `psycopg` (+ pool), `selectolax` (lexbor backend), `trafilatura`, `extruct`, `apscheduler`, `pydantic`, `pydantic-settings`, `jinja2`, `notion-client`, `anthropic`. Dev: `pytest`, `pytest-asyncio`, `respx`, `ruff`. `trafilatura` and `extruct` are installed but not currently used.

## Notes
- `db.ps1` no longer manages a server. `bootstrap` creates the roles and database; `apply` loads the schema and tags (see `docs/walkthrough.md`). It needs psql 15 or newer and is plain ASCII on purpose, because Windows PowerShell 5.1 mangles other characters in scripts.
- Passwords live in the git-ignored `.env` (the app role) and are typed at prompts (the admin and read-only roles). Keep the PostgreSQL service bound to localhost.
- The earlier private PostgreSQL 14 cluster (`.pgdata/`) was deleted; the `.pgdata/` line in `.gitignore` is a harmless leftover.
- The first version of some files was rewritten through PowerShell and picked up a byte-order mark and mangled non-ASCII characters; both were repaired. Edit with tools that keep UTF-8 without BOM.

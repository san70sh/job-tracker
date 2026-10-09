from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    # Fallback only: put the real value, including the password, in .env (see .env.example).
    database_url: str = "postgresql://jobtracker@localhost:5432/jobtracker"

    # Fetching
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JobTracker/0.1"
    http_timeout: float = 30.0
    per_host_delay: float = 1.0  # seconds between requests to the same host (P2 endpoints are undocumented)

    # Pipeline
    confidence_threshold: float = 0.8
    reminder_after_days: int = 2  # discovered postings undecided for longer than this trigger the reminder banner

    # Lag diagnostics (logs/diagnostics.log): a request or background job slower than this, or an event-loop gap longer than the other
    slow_request_seconds: float = 1.0
    loop_stall_seconds: float = 0.5

    # LLM fallback (optional; pipeline works without it)
    anthropic_api_key: str | None = None
    llm_model: str = "claude-haiku-4-5"  # bulk extraction fallback; override in .env if you want a stronger model

    # Browser extension: it must send this in X-Capture-Token. Create one with `python -m jobtracker capture-token`.
    capture_token: str | None = None

    # Notion mirror (optional)
    notion_token: str | None = None
    notion_data_source_id: str | None = None  # collection:// id of the Job Tracker data source


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""Common data model every adapter normalises into."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

SectionKind = Literal[
    "about_company", "role", "responsibilities", "requirements",
    "nice_to_have", "benefits", "logistics", "other",
]
WorkMode = Literal["Onsite", "Hybrid", "Remote"]


class Section(BaseModel):
    kind: SectionKind
    heading: str
    bullets: list[str] = Field(default_factory=list)  # list items / sentences
    text: str = ""  # full text of the section (bullets joined when present)


class PayRange(BaseModel):
    min: float
    max: float
    currency: str  # ISO 4217
    interval: Literal["year", "month", "hour"] = "year"
    note: str | None = None  # e.g. "New York,NY"


class Posting(BaseModel):
    """One job posting as fetched, before field extraction."""

    ats: str
    url: str
    title: str
    ats_posting_id: str | None = None
    company: str | None = None
    job_ref: str | None = None  # employer's requisition id
    location: str | None = None
    work_mode: WorkMode | None = None  # only when the ATS states it explicitly
    department: str | None = None
    posted_at: date | None = None
    description_html: str | None = None
    sections: list[Section] | None = None  # when the ATS already separates them
    pay: PayRange | None = None
    closed: bool = False
    raw: Any = None  # untouched ATS payload for job_snapshots
    via: str | None = None  # the careers page this was reached from, when it only displayed the job (see registry.delegate_from_html)


class ListedBatch(list):
    """Result of listing a board. `complete` is False when the listing stopped at a page cap before the end,
    in which case postings missing from it must NOT be treated as closed."""

    def __init__(self, items=(), complete: bool = True):
        super().__init__(items)
        self.complete = complete


class ListedPosting(BaseModel):
    """A posting seen on a board listing (no full description required)."""

    ats_posting_id: str
    url: str
    title: str
    location: str | None = None
    posted_at: date | None = None
    department: str | None = None


# config keys that tell two boards of one system apart (a Workday tenant has several sites, an Avature host several portals)
IDENTITY_KEYS = ("site", "base", "domain")


@dataclass
class BoardRef:
    """Everything an adapter needs to list or fetch from one company's board."""

    ats: str
    slug: str | None = None  # greenhouse token, lever/ashby board, smartrecruiters id, jibe host...
    company: str | None = None
    config: dict[str, Any] = field(default_factory=dict)  # P2 extras: workday/oracle/eightfold

    def identity(self) -> tuple[str, str, str]:
        """The same board whatever address it was reached by (boards.greenhouse.io/x and job-boards.greenhouse.io/x)."""
        return (self.ats, (self.slug or "").lower(), next((str(self.config[k]) for k in IDENTITY_KEYS if self.config.get(k)), ""))


@dataclass
class FieldValue:
    value: Any
    method: str  # ats_api | jsonld | rules | llm | manual
    confidence: float
    evidence: str | None = None


@dataclass
class ExtractedJob:
    """Output of the deterministic extractors; maps onto jobs + children."""

    posting: Posting
    fields: dict[str, FieldValue] = field(default_factory=dict)
    sections: list[Section] = field(default_factory=list)
    technologies: dict[str, str] = field(default_factory=dict)  # tech name -> required|nice_to_have
    pay: PayRange | None = None

    def get(self, name: str, default: Any = None) -> Any:
        fv = self.fields.get(name)
        return fv.value if fv else default

    def set(self, name: str, value: Any, method: str, confidence: float, evidence: str | None = None) -> None:
        if value in (None, "", []):
            return
        self.fields[name] = FieldValue(value, method, confidence, evidence)

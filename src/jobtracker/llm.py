"""LLM fallback: fills ONLY the fields the deterministic extractors could not, and caches by input hash.

The pipeline is fully functional without an API key; this module simply returns nothing then.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

from .config import get_settings
from .extract.facts import level_display, normalize_level
from .extract.job import format_requirements
from .extract.tech import tag_technologies
from .extract.vocab import load_vocab
from .models import ExtractedJob, Section

PROMPT_VERSION = "1"

SYSTEM = (
    "You extract structured fields from a job posting for a personal job tracker. "
    "Use only what the posting states; never infer or invent. Use null when a field is not stated. "
    "Reply with a single JSON object and nothing else."
)

FIELD_SPEC = {
    "role": 'string: the job title',
    "company": "string: hiring company name",
    "location": "string: city/region/country as written",
    "work_mode": 'one of "Onsite", "Hybrid", "Remote" (only if the posting says so)',
    "level": 'string: seniority/level as the company states it (e.g. "Senior", "SDE3", "L5", "IC3")',
    "experience_required": "string: the headline experience requirement, quoted briefly (e.g. \"5+ years backend engineering\")",
    "team_domain": "string: team or domain (e.g. \"Payments Platform\")",
    "key_responsibilities": "array of strings: what the person will do, one item per duty",
    "requirements": 'object {"required": [strings], "nice_to_have": [strings]}: qualifications, split by whether the posting marks them preferred/good-to-have',
}


class LLMUnavailable(Exception):
    pass


class LLMFallback:
    def __init__(
        self,
        cache_get: Callable[[str, str], dict | None],
        cache_put: Callable[..., str],
        client: Any | None = None,
    ):
        self.settings = get_settings()
        self._client = client
        self.cache_get, self.cache_put = cache_get, cache_put

    @property
    def available(self) -> bool:
        return self._client is not None or bool(self.settings.anthropic_api_key)

    def _get_client(self) -> Any:
        if self._client is None:
            if not self.settings.anthropic_api_key:
                raise LLMUnavailable("ANTHROPIC_API_KEY not set")
            import anthropic

            self._client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        return self._client

    def fill(self, ex: ExtractedJob, missing: list[str], description_text: str, job_id: str | None = None) -> dict[str, str]:
        """Fill `missing` fields on `ex` in place. Returns {field: llm_call_id} for provenance."""
        fields = [f for f in missing if f in FIELD_SPEC]
        if not fields or not self.available:
            return {}
        model = self.settings.llm_model
        payload = {
            "title": ex.get("role"), "company": ex.get("company"), "location": ex.get("location"),
            "description": description_text,
        }
        key = hashlib.sha256(json.dumps([PROMPT_VERSION, model, sorted(fields), payload], sort_keys=True).encode()).hexdigest()
        hit = self.cache_get("extract_fields", key)
        if hit:
            data, call_id = hit["output"], str(hit["id"])
        else:
            data, usage = self._call(model, fields, payload)
            call_id = self.cache_put(task="extract_fields", input_hash=key, model=model, prompt_version=PROMPT_VERSION,
                                     output=data, input_tokens=usage[0], output_tokens=usage[1], job_id=job_id)
        applied = apply_llm_output(ex, data, fields)
        return {f: call_id for f in applied}

    def _call(self, model: str, fields: list[str], payload: dict) -> tuple[dict, tuple[int | None, int | None]]:
        spec = "\n".join(f'- "{f}": {FIELD_SPEC[f]}' for f in fields)
        user = (
            f"Return a JSON object with exactly these keys:\n{spec}\n\n"
            f"Job title: {payload['title']}\nCompany: {payload['company']}\nLocation: {payload['location']}\n\n"
            f"Posting text:\n{payload['description']}"
        )
        client = self._get_client()
        resp = client.messages.create(
            model=model, max_tokens=4000, system=SYSTEM, messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return parse_json_object(text), (getattr(resp.usage, "input_tokens", None), getattr(resp.usage, "output_tokens", None))


def parse_json_object(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _strs(v: Any) -> list[str]:
    if not isinstance(v, list):
        return []
    return [s.strip() for s in v if isinstance(s, str) and s.strip()]


def apply_llm_output(ex: ExtractedJob, data: dict, fields: list[str]) -> list[str]:
    """Validate the model's output and merge it into `ex`. Never trusts the shape; returns fields applied."""
    applied: list[str] = []
    conf = 0.7  # below rule-based confidence on purpose: a human can tell what came from the model

    def put(name: str, value: Any) -> None:
        if value not in (None, "", []):
            ex.set(name, value, "llm", conf)
            applied.append(name)

    for f in ("role", "company", "location", "level", "experience_required", "team_domain"):
        if f in fields and isinstance(data.get(f), str) and data[f].strip():
            put(f, data[f].strip())
    if "work_mode" in fields and data.get("work_mode") in ("Onsite", "Hybrid", "Remote"):
        put("work_mode", data["work_mode"])
    if "level" in applied:
        norm = normalize_level(ex.get("level") or "") or normalize_level(ex.get("role") or "")
        ex.set("level_normalized", norm, "llm", conf)
    if "key_responsibilities" in fields:
        items = _strs(data.get("key_responsibilities"))
        if items:
            put("key_responsibilities", "\n".join(f"• {b}" for b in items))
            ex.sections = [s for s in ex.sections if s.kind != "responsibilities"]
            ex.sections.append(Section(kind="responsibilities", heading="Key responsibilities", bullets=items, text="\n".join(items)))
    if "requirements" in fields and isinstance(data.get("requirements"), dict):
        req, nice = _strs(data["requirements"].get("required")), _strs(data["requirements"].get("nice_to_have"))
        if req or nice:
            put("requirements", format_requirements(req, nice))
            ex.sections = [s for s in ex.sections if s.kind not in ("requirements", "nice_to_have")]
            if req:
                ex.sections.append(Section(kind="requirements", heading="Qualifications", bullets=req, text="\n".join(req)))
            if nice:
                ex.sections.append(Section(kind="nice_to_have", heading="Good to have", bullets=nice, text="\n".join(nice)))
            # re-tag technologies from the cleaner sections (deterministic tagger, model only supplied the split)
            ex.technologies = {**tag_technologies(ex.sections, load_vocab()), **ex.technologies}
    if "level" not in applied and ex.get("level_normalized") and not ex.get("level"):
        ex.set("level", level_display(ex.get("role") or "", ex.get("level_normalized")), "rules", 0.5)
    return applied

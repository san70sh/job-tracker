"""Dictionary-based technology tagger: section text -> {tech name: required | nice_to_have}."""
from __future__ import annotations

from ..models import Section
from .vocab import Vocab, load_vocab

# Sections whose mentions say nothing about what the job needs.
IGNORED = {"about_company", "benefits", "logistics"}
NICE = {"nice_to_have"}


def tag_technologies(sections: list[Section], vocab: Vocab | None = None) -> dict[str, str]:
    vocab = vocab or load_vocab()
    found: dict[str, str] = {}
    for sec in sections:
        if sec.kind in IGNORED:
            continue
        kind = "nice_to_have" if sec.kind in NICE else "required"
        text = f"{sec.heading}\n{sec.text}" if sec.kind == "other" else sec.text
        for tech in vocab.techs:
            if any(p.search(text) for p in tech.patterns):
                # required beats nice_to_have if a tech shows up in both
                if found.get(tech.name) != "required":
                    found[tech.name] = kind
    return found


def tag_text(text: str, vocab: Vocab | None = None) -> list[str]:
    """Tag a bare string (used for titles and tests)."""
    vocab = vocab or load_vocab()
    return [t.name for t in vocab.techs if any(p.search(text) for p in t.patterns)]

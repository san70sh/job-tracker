"""Rebuild docs/diagrams.html from the Mermaid blocks in docs/*.md.

    uv run python scripts/build_diagrams.py

The page loads Mermaid from a CDN, so it needs internet when opened.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"
SOURCES = ["architecture.md", "extraction-pathways.md"]
FENCE = re.compile(r"```mermaid\n(.*?)```", re.S)


def collect() -> list[tuple[str, str, str]]:
    out = []
    for name in SOURCES:
        text = (DOCS / name).read_text(encoding="utf-8")
        heading = name
        pos = 0
        for m in FENCE.finditer(text):
            heads = re.findall(r"^#{2,3} (.+)$", text[pos:m.start()], re.M)
            if heads:
                heading = re.sub(r"^\d+\.\s*", "", heads[-1])
                if heading == "Summary":
                    heading = "Source resolution and pathways"
            pos = m.start()
            out.append((name, heading, m.group(1)))
    return out


def main() -> None:
    parts = []
    for i, (src, heading, code) in enumerate(collect(), 1):
        parts.append(f"<section><h2>{i}. {html.escape(heading)}</h2><p class='src'>{src}</p>"
                     f"<pre class='mermaid'>{html.escape(code)}</pre></section>")
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job Tracker Diagrams</title>
<style>
  :root {{ --bg:#fff; --ink:#1b1f24; --muted:#667085; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#14171c; --ink:#e8eaee; --muted:#9aa3b2; }} }}
  body {{ margin:0 auto; max-width:1200px; padding:16px; background:var(--bg); color:var(--ink); font:15px/1.45 system-ui,sans-serif; }}
  h2 {{ margin:36px 0 2px; }} .src {{ margin:0 0 8px; color:var(--muted); font-size:13px; }}
  .mermaid {{ overflow-x:auto; background:#fff; border-radius:8px; padding:8px; }}
</style></head><body>
<h1>Job Tracker diagrams</h1>
<p class="src">Generated from docs/*.md by scripts/build_diagrams.py</p>
{''.join(parts)}
<script type="module">
  import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
  mermaid.initialize({{ startOnLoad: true, securityLevel: 'loose' }});
</script></body></html>
"""
    (DOCS / "diagrams.html").write_text(page, encoding="utf-8", newline="")
    print(f"wrote docs/diagrams.html with {len(parts)} diagrams")


if __name__ == "__main__":
    main()

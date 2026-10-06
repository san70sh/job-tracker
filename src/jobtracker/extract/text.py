"""HTML -> structured text blocks."""
from __future__ import annotations

import html as htmllib
import re
from dataclasses import dataclass

from selectolax.lexbor import LexborHTMLParser as HTMLParser, LexborNode as Node

BLOCK_TAGS = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "ul", "ol", "br", "table", "blockquote"}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_WS = re.compile(r"[ \t\u00a0\u200b]+")


@dataclass
class Block:
    kind: str  # "heading" | "li" | "p"
    text: str


def fix_mojibake(s: str) -> str:
    """UTF-8 text that was decoded as cp1252 upstream ('â€¢' for '•'); seen in Workday descriptions."""
    if "â€" in s or "Ã" in s:
        try:
            return s.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return s
    return s


def clean(s: str) -> str:
    return _WS.sub(" ", fix_mojibake(s).replace("\r", "")).strip()


def maybe_unescape(s: str) -> str:
    """Greenhouse returns its HTML entity-escaped (&lt;p&gt;); detect and undo that."""
    if "&lt;" in s and "<" not in s:
        return htmllib.unescape(s)
    return s


def _is_bold_only(node: Node) -> bool:
    """<p><strong>Heading</strong></p> style pseudo-headings."""
    text = clean(node.text(deep=True))
    if not text or len(text) > 90:
        return False
    bold = "".join(clean(b.text(deep=True)) for b in node.css("strong, b"))
    return bool(bold) and len(bold) >= len(text) - 2


INLINE_TAGS = {"b", "strong", "i", "em", "u", "span", "a", "font", "small", "sup", "sub", "mark", "abbr", "code", "s"}
# lines that are part of a web page around the posting, not of the posting
_CHROME = re.compile(
    r"^(show (more|less|fewer)( jobs like this)?|see (more|less)|apply( now)?|browse more jobs|start assessment now|save|share|"
    r"report( this)? job)$", re.I)


def html_to_blocks(html: str) -> list[Block]:
    from .sections import looks_like_heading  # imported here: sections imports this module

    html = maybe_unescape(html)
    if "<" not in html:  # already plain text
        return text_to_blocks(html)
    tree = HTMLParser(html)
    root = tree.body or tree.root
    blocks: list[Block] = []

    def emit(kind: str, text: str) -> None:
        text = clean(text)
        if kind == "p" and _BULLET.match(text):  # '• item' typed into a paragraph
            kind, text = "li", _BULLET.sub("", text)
        if text:
            blocks.append(Block(kind, text))

    def walk(node: Node) -> None:
        tag = node.tag
        if tag in ("script", "style", "-text", "#comment"):
            return
        if tag in HEADING_TAGS:
            emit("heading", node.text(deep=True))
            return
        if tag == "li":
            # nested lists: take own text, then recurse into children lists
            own = "".join(c.text(deep=True) for c in node.iter(include_text=False) if c.tag not in ("ul", "ol"))
            has_sub = any(c.tag in ("ul", "ol") for c in node.iter(include_text=False))
            emit("li", own if has_sub else node.text(deep=True))
            if has_sub:
                for c in node.iter(include_text=False):
                    if c.tag in ("ul", "ol"):
                        walk(c)
            return
        if tag in ("p", "div") and not any(c.tag in BLOCK_TAGS - {"br"} for c in node.iter(include_text=False)):
            # leaf paragraph: may contain <br>-separated lines
            raw = node.inner_html or ""
            parts = re.split(r"<br\s*/?>", raw, flags=re.I)
            for part in parts:
                piece = clean(HTMLParser(f"<div>{part}</div>").text(deep=True))
                if not piece:
                    continue
                if _is_bold_only(HTMLParser(f"<p>{part}</p>").css_first("p")) or (
                    piece.endswith(":") and len(piece) < 70
                ):
                    emit("heading", piece.rstrip(":"))
                else:
                    emit("p", piece)
            return
        # A container that mixes elements with loose text (Workday writes whole descriptions this way:
        # "<p></p>Overview<br>text<br><br>Key Responsibilities<br>• item<br>..."). Read the text between the tags as
        # lines, one per <br>; inline tags (b, i, span, a) stay part of their line.
        run: list[str] = []

        def flush() -> None:
            text = htmllib.unescape("".join(run)).replace("\xa0", " ")
            run.clear()
            text = clean(text)
            if not any(ch.isalnum() for ch in text) or _CHROME.match(text):
                return  # whitespace/entity leftovers and page furniture ("Show more", "Apply Now")
            if not _BULLET.match(text) and looks_like_heading(text):
                emit("heading", text.rstrip(":"))
            else:
                emit("p", text)

        for child in node.iter(include_text=True):
            t = child.tag
            if t == "-text":
                run.append(child.text_content or "")
            elif t == "br":
                flush()
            elif t in INLINE_TAGS and not any(c.tag in BLOCK_TAGS | HEADING_TAGS for c in child.iter(include_text=False)):
                run.append(child.text(deep=True))
            else:
                flush()
                walk(child)
        flush()

    walk(root)
    return blocks


_BULLET = re.compile(r"^\s*(?:[-\u2022*\u25aa\u25cf\u25e6\u00b7\u2013]|\d+[.)])\s+")


def text_to_blocks(text: str) -> list[Block]:
    """Plain-text description: lines starting with bullet glyphs are items; short lines ending in ':' are headings."""
    out: list[Block] = []
    for line in text.replace("\r", "").split("\n"):
        line = clean(line)
        if not line:
            continue
        if _BULLET.match(line):
            out.append(Block("li", _BULLET.sub("", line)))
        elif line.endswith(":") and len(line) < 70:
            out.append(Block("heading", line.rstrip(":")))
        else:
            out.append(Block("p", line))
    return out


def blocks_to_text(blocks: list[Block]) -> str:
    lines = []
    for b in blocks:
        lines.append(f"\u2022 {b.text}" if b.kind == "li" else b.text)
    return "\n".join(lines)


def html_to_text(html: str) -> str:
    return blocks_to_text(html_to_blocks(html))


def text_coverage(html: str) -> float:
    """Share of the words a browser would show for this HTML that the extractor kept (1.0 = nothing lost).
    The guard against silently dropping text: a section being found does not mean the section was complete."""
    from collections import Counter

    tree = HTMLParser(maybe_unescape(html))
    for bad in tree.css("script, style"):
        bad.decompose()
    shown = tree.body.text(deep=True, separator=" ") if tree.body else ""  # a space between elements, as a browser shows them
    visible = Counter(re.findall(r"\w+", htmllib.unescape(shown).lower()))
    kept = Counter(re.findall(r"\w+", htmllib.unescape(html_to_text(html)).lower()))
    total = sum(visible.values())
    return sum(min(n, kept[w]) for w, n in visible.items()) / total if total else 1.0

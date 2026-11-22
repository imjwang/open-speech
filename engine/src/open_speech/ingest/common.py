"""Shared ingest helpers: draft blocks → `Block`s with ids and marks, source ids (05 § 1), titles."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from open_speech.document.model import BBox, Block, BlockType, Document, DomAnchor, Mark, MarkKind, SourceKind, WordBox
from open_speech.ids import doc_id_for, sha256_hex
from open_speech.ingest.marks import detect_marks

EXTRACTOR_VERSION = "1"

_ID_RE = re.compile(r"^blk_[A-Za-z0-9_-]{1,64}$")
_JUNK_TITLES = re.compile(
    r"^(untitled|document\d*|microsoft word\s*-.*|slide\s*\d*|title|none|null|\s*)$|\.(docx?|pdf|txt|md|tex)$",
    re.IGNORECASE,
)


@dataclass
class DraftBlock:
    type: BlockType
    text: str
    level: int | None = None
    page: int | None = None
    bbox: BBox | None = None
    marks: list[tuple[MarkKind, int, int]] = field(default_factory=list)  # structural marks
    dom_anchor: DomAnchor | None = None
    word_boxes: list[WordBox] = field(default_factory=list)
    image_id: str | None = None
    block_id: str | None = None  # explicit id (e.g. from the shell)


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def normalized_text(texts: Iterable[str]) -> str:
    """Canonical text for source ids: NFC, whitespace collapsed, one block per line."""
    return "\n".join(t for t in (normalize_space(x) for x in texts) if t)


def source_id_bytes(data: bytes) -> str:
    return sha256_hex(data)


def source_id_text(text: str) -> str:
    return sha256_hex(normalized_text([text]))


def source_id_url(url: str, texts: Iterable[str]) -> str:
    return sha256_hex(url.strip(), normalized_text(texts))


def site_for(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlsplit(url if "://" in url else f"https://{url}").hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def finalize_blocks(drafts: Sequence[DraftBlock], *, footnote_markers: Iterable[str] = ()) -> list[Block]:
    """Assign `blk_<n>` ids and orders (keeping valid explicit ids) and detect marks."""
    markers = set(footnote_markers)
    seen: set[str] = set()
    blocks: list[Block] = []
    for order, d in enumerate(drafts):
        bid = d.block_id if d.block_id and _ID_RE.match(d.block_id) and d.block_id not in seen else None
        if bid is None:
            bid = f"blk_{order}"
            while bid in seen:
                bid += "_"
        seen.add(bid)
        spans = detect_marks(d.text, block_type=d.type, footnote_markers=markers, structural=d.marks)
        marks = [Mark(id=f"mrk_{order}_{i}", kind=k, start=s, end=e) for i, (k, s, e) in enumerate(spans)]
        blocks.append(
            Block(
                id=bid,
                type=d.type,
                text=d.text,
                order=order,
                level=d.level if d.type == BlockType.heading else None,
                page=d.page,
                bbox=d.bbox,
                marks=marks,
                dom_anchor=d.dom_anchor,
                word_boxes=d.word_boxes,
                image_id=d.image_id,
            )
        )
    return blocks


_SITE_SUFFIX = re.compile(r"^(?P<main>.{10,}?)\s+(?:\||—|–|·)\s+[^|—–·]{2,60}$")
_DASH_SUFFIX = re.compile(r"^(?P<main>.{10,}?)\s+-\s+(?:\S+\s*){1,4}$")


def clean_title(title: str | None, *, max_len: int = 200, strip_site: bool = False) -> str | None:
    if not title:
        return None
    t = normalize_space(title)
    if not t or _JUNK_TITLES.search(t):
        return None
    if strip_site:  # "Story headline | Example News" → "Story headline"
        m = _SITE_SUFFIX.match(t) or _DASH_SUFFIX.match(t)
        if m:
            t = m.group("main")
    return t[:max_len]


def infer_title(
    blocks: Sequence[Block],
    *,
    explicit: str | None = None,
    fallback: str | None = None,
    strip_site: bool = False,
) -> str:
    """Explicit title, else the first top-level heading, else the first line of text, else `fallback`."""
    t = clean_title(explicit, strip_site=strip_site)
    if t:
        return t
    headings = [b for b in blocks if b.type == BlockType.heading and b.text.strip()]
    if headings:
        top = min(h.level or 1 for h in headings)
        first = next(h for h in headings if (h.level or 1) == top)
        return normalize_space(first.text)[:200]
    for b in blocks:
        if b.type in (BlockType.paragraph, BlockType.list_item, BlockType.quote) and b.text.strip():
            line = normalize_space(b.text)
            return line if len(line) <= 80 else line[:77].rsplit(" ", 1)[0] + "…"
    return clean_title(fallback) or fallback or "Untitled"


def build_document(
    *,
    source_id: str,
    kind: SourceKind,
    blocks: list[Block],
    title: str,
    page_count: int | None = None,
    url: str | None = None,
    site: str | None = None,
    flags: Sequence[str] = (),
    language: str | None = None,
    extractor_version: str = EXTRACTOR_VERSION,
) -> Document:
    return Document(
        id=doc_id_for(source_id),
        source_id=source_id,
        kind=kind,
        title=title,
        blocks=blocks,
        page_count=page_count,
        url=url,
        site=site,
        language=language,
        extractor_version=extractor_version,
        flags=list(flags),
    )

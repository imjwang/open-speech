"""Headless web pages (05 § 2 Web): fetch with httpx, extract the main content with trafilatura.

Fetching is injectable (`Fetcher`) so tests and offline runs work from HTML fixtures. A URL that serves a
PDF is handed to the PDF extractor with the page's site attached (so `sites:` rules apply to it).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from lxml import etree

from open_speech.document.model import BlockType, Document, SourceKind
from open_speech.errors import EngineError
from open_speech.ingest.common import (
    DraftBlock,
    build_document,
    clean_title,
    finalize_blocks,
    infer_title,
    normalize_space,
    site_for,
    source_id_url,
)

USER_AGENT = "Mozilla/5.0 (compatible; OpenSpeech/0.1; +https://open-speech.dev)"
MAX_BYTES = 50 * 1024 * 1024


@dataclass
class Fetched:
    url: str  # final URL after redirects
    content: bytes
    media_type: str  # e.g. "text/html", "application/pdf"


Fetcher = Callable[[str], Awaitable[Fetched]]


async def http_fetch(url: str) -> Fetched:
    """Default fetcher (httpx, redirects followed, 20 s timeout, 50 MB cap)."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(url)
    except httpx.HTTPError as e:
        raise EngineError("offline", f"I couldn't load that page ({type(e).__name__}).") from e
    if resp.status_code >= 400:
        raise EngineError("invalid_request", f"That page returned an error ({resp.status_code}).")
    if len(resp.content) > MAX_BYTES:
        raise EngineError("invalid_request", "That page is too large to read.")
    media = resp.headers.get("content-type", "text/html").split(";")[0].strip().lower()
    return Fetched(url=str(resp.url), content=resp.content, media_type=media)


def _text_of(el: etree._Element) -> str:
    return normalize_space("".join(el.itertext()))


def _xml_to_drafts(root: etree._Element) -> list[DraftBlock]:
    drafts: list[DraftBlock] = []
    main = root.find("main")
    body = main if main is not None else root
    for el in body:
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag == "head":
            rend = (el.get("rend") or "h2").lower()
            level = int(rend[1]) if len(rend) == 2 and rend[0] == "h" and rend[1].isdigit() else 2
            text = _text_of(el)
            if text:
                drafts.append(DraftBlock(type=BlockType.heading, text=text, level=level))
        elif tag == "p":
            text = _text_of(el)
            if text:
                drafts.append(DraftBlock(type=BlockType.paragraph, text=text))
        elif tag == "list":
            for item in el.iter("item"):
                text = _text_of(item)
                if text:
                    drafts.append(DraftBlock(type=BlockType.list_item, text=text))
        elif tag == "quote":
            text = _text_of(el)
            if text:
                drafts.append(DraftBlock(type=BlockType.quote, text=text))
        elif tag == "code":
            text = "".join(el.itertext()).strip("\n")
            if text.strip():
                drafts.append(DraftBlock(type=BlockType.code, text=text))
        elif tag == "table":
            rows = []
            for row in el.iter("row"):
                cells = [_text_of(c) for c in row.iter("cell")]
                cells = [c for c in cells if c]
                if cells:
                    line = ", ".join(cells)
                    rows.append(line if line.endswith((".", "!", "?")) else line + ".")
            if rows:
                drafts.append(DraftBlock(type=BlockType.table, text="\n".join(rows)))
        elif tag == "graphic":
            alt = normalize_space(el.get("alt") or el.get("title") or "")
            drafts.append(DraftBlock(type=BlockType.figure, text=alt))
        else:
            text = _text_of(el)
            if text:
                drafts.append(DraftBlock(type=BlockType.paragraph, text=text))
    return drafts


def extract_html(html: str | bytes, *, url: str, title: str | None = None) -> Document:
    """HTML → Document via trafilatura (main content only: no nav, ads or comments)."""
    import trafilatura

    xml = trafilatura.extract(
        html,
        url=url,
        output_format="xml",
        include_tables=True,
        include_images=True,
        include_formatting=False,
        include_links=False,
        include_comments=False,
        with_metadata=True,
    )
    if not xml:
        raise EngineError("invalid_request", "I couldn't find any readable text on that page.")
    root = etree.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    drafts = _xml_to_drafts(root)
    blocks = finalize_blocks(drafts)
    if not blocks:
        raise EngineError("invalid_request", "I couldn't find any readable text on that page.")
    page_title = clean_title(root.get("title"))
    return build_document(
        source_id=source_id_url(url, (b.text for b in blocks)),
        kind=SourceKind.web,
        blocks=blocks,
        title=infer_title(blocks, explicit=title or page_title, fallback=site_for(url) or url, strip_site=True),
        url=url,
        site=site_for(url),
    )

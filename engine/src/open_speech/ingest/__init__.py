"""Ingest (05 § 1–2): any source → `Document` of typed blocks, cached by source id + extractor version.

Binding entry point: `ingest(src, *, extract_cache, vision=None, vision_rules=()) -> IngestResult`.
Addition: `fetch=` injects the URL fetcher (tests, offline runs).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from open_speech.api.schemas import HtmlBlocksPayload
from open_speech.cache.extract import ExtractCache
from open_speech.document.model import Document
from open_speech.errors import EngineError
from open_speech.ingest.common import EXTRACTOR_VERSION, clean_title, site_for, source_id_bytes, source_id_text
from open_speech.ingest.docx import ingest_docx
from open_speech.ingest.html_blocks import ingest_html_blocks
from open_speech.ingest.image import ingest_image, sniff_image
from open_speech.ingest.pdf import extract_pdf
from open_speech.ingest.text import decode_text, ingest_text, ingest_text_file
from open_speech.ingest.url import Fetched, Fetcher, extract_html, http_fetch
from open_speech.providers.base import LlmCall, RuleBrief, VisionProvider

__all__ = [
    "EXTRACTOR_VERSION",
    "Fetched",
    "Fetcher",
    "IngestResult",
    "SourceInput",
    "detect_file_kind",
    "ingest",
]

FileKind = Literal["pdf", "docx", "image", "text", "markdown", "html", "audio", "unknown"]

_AUDIO_EXT = (".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".aac", ".aiff")


class SourceInput(BaseModel):
    kind: Literal["file", "url", "text", "image", "html_blocks"]
    data: bytes | None = None
    filename: str | None = None
    media_type: str | None = None
    url: str | None = None
    text: str | None = None
    title: str | None = None
    html_blocks: HtmlBlocksPayload | None = None
    image_mode: Literal["auto", "transcribe", "describe"] = "auto"


class IngestResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    document: Document
    llm_calls: list[tuple[str, LlmCall]] = Field(default_factory=list)
    source_bytes: bytes | None = None


def detect_file_kind(data: bytes, filename: str | None = None, media_type: str | None = None) -> FileKind:
    name = (filename or "").lower()
    mt = (media_type or "").lower()
    if data[:5] == b"%PDF-" or data[:1024].lstrip().startswith(b"%PDF-") or mt == "application/pdf":
        return "pdf"
    if data[:2] == b"PK" and (b"word/" in data[:4096] or name.endswith((".docx", ".docm", ".dotx"))):
        return "docx"
    if mt.endswith("wordprocessingml.document") or name.endswith(".docx"):
        return "docx"
    if sniff_image(data) or mt.startswith("image/"):
        return "image"
    if mt.startswith("audio/") or name.endswith(_AUDIO_EXT):
        return "audio"
    if name.endswith((".md", ".markdown", ".mdown", ".mkd")) or mt == "text/markdown":
        return "markdown"
    if name.endswith((".html", ".htm", ".xhtml")) or mt in ("text/html", "application/xhtml+xml"):
        return "html"
    head = data[:4096]
    if b"\x00" in head and not head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "unknown"
    return "text"


def _with_title(doc: Document, title: str | None) -> Document:
    t = clean_title(title)
    return doc.model_copy(update={"title": t}) if t and t != doc.title else doc


async def ingest(
    src: SourceInput,
    *,
    extract_cache: ExtractCache,
    vision: VisionProvider | None = None,
    vision_rules: Sequence[RuleBrief] = (),
    fetch: Fetcher | None = None,
) -> IngestResult:
    """Ingest one source. Raises `Unavailable("vision")` when an image needs vision
    and `vision` is None; EngineError("invalid_request", …) for unreadable input."""
    if src.kind == "html_blocks":
        if src.html_blocks is None:
            raise EngineError("invalid_request", "No page blocks were sent.")
        doc = ingest_html_blocks(src.html_blocks, title=src.title)
        cached = extract_cache.get(doc.source_id, EXTRACTOR_VERSION)
        if cached is None:
            extract_cache.put(doc)
        return IngestResult(document=doc)

    if src.kind == "text":
        text = src.text if src.text is not None else decode_text(src.data or b"")
        if not text.strip():
            raise EngineError("invalid_request", "There's no text to read.")
        sid = source_id_text(text)
        doc = extract_cache.get(sid, EXTRACTOR_VERSION)
        if doc is None:
            doc = ingest_text(text, title=src.title)
            extract_cache.put(doc)
        return IngestResult(document=_with_title(doc, src.title))

    if src.kind == "url":
        if not src.url:
            raise EngineError("invalid_request", "No URL was given.")
        fetched = await (fetch or http_fetch)(src.url)
        kind = detect_file_kind(fetched.content, fetched.url, fetched.media_type)
        if kind == "pdf":
            res = await _ingest_file(
                fetched.content,
                filename=fetched.url.rsplit("/", 1)[-1],
                media_type="application/pdf",
                title=src.title,
                extract_cache=extract_cache,
                vision=vision,
                vision_rules=vision_rules,
                image_mode=src.image_mode,
            )
            doc = res.document.model_copy(update={"url": fetched.url, "site": site_for(fetched.url)})
            return IngestResult(document=doc, llm_calls=res.llm_calls, source_bytes=res.source_bytes)
        if kind not in ("html", "text", "markdown"):
            raise EngineError("invalid_request", "That link isn't a web page or a PDF I can read.")
        doc = await asyncio.to_thread(extract_html, fetched.content, url=fetched.url, title=src.title)
        cached = extract_cache.get(doc.source_id, EXTRACTOR_VERSION)
        if cached is None:
            extract_cache.put(doc)
        return IngestResult(document=doc)

    if src.kind in ("file", "image"):
        if not src.data:
            raise EngineError("invalid_request", "The file is empty.")
        return await _ingest_file(
            src.data,
            filename=src.filename,
            media_type=src.media_type,
            title=src.title,
            extract_cache=extract_cache,
            vision=vision,
            vision_rules=vision_rules,
            image_mode=src.image_mode,
            force_image=src.kind == "image",
        )
    raise EngineError("invalid_request", f"Unknown source kind {src.kind!r}.")


async def _ingest_file(
    data: bytes,
    *,
    filename: str | None,
    media_type: str | None,
    title: str | None,
    extract_cache: ExtractCache,
    vision: VisionProvider | None,
    vision_rules: Sequence[RuleBrief],
    image_mode: Literal["auto", "transcribe", "describe"],
    force_image: bool = False,
) -> IngestResult:
    kind: FileKind = "image" if force_image else detect_file_kind(data, filename, media_type)
    sid = source_id_bytes(data)
    if kind == "image":
        version = f"{EXTRACTOR_VERSION}:image:{image_mode}"
        doc = extract_cache.get(sid, version)
        if doc is not None:
            return IngestResult(document=_with_title(doc, title), source_bytes=data)
        doc, call = await ingest_image(
            data,
            vision=vision,
            rules=vision_rules,
            mode=image_mode,
            title=title,
            filename=filename,
            extractor_version=version,
        )
        extract_cache.put(doc)
        return IngestResult(document=doc, llm_calls=[("vision", call)], source_bytes=data)
    if kind == "audio":
        raise EngineError("invalid_request", "Audio files aren't supported yet.")
    if kind == "unknown":
        raise EngineError("invalid_request", "I don't know how to read that kind of file.")

    doc = extract_cache.get(sid, EXTRACTOR_VERSION)
    if doc is None:
        if kind == "pdf":
            doc = await asyncio.to_thread(extract_pdf, data, filename=filename)
        elif kind == "docx":
            doc = await asyncio.to_thread(ingest_docx, data, filename=filename)
        elif kind == "html":
            html_doc = await asyncio.to_thread(extract_html, data, url=filename or "file.html")
            doc = html_doc.model_copy(update={"source_id": sid, "id": f"doc_{sid[:16]}", "url": None, "site": None})
        else:
            doc = await asyncio.to_thread(ingest_text_file, data, filename=filename)
        extract_cache.put(doc)
    return IngestResult(document=_with_title(doc, title), source_bytes=data)

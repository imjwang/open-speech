"""Document model (05 § 2). Shared contract: every ingest path produces a `Document` of typed `Block`s.

Offsets are Python string indices (code points) into `Block.text`. They are the backbone of
highlighting and span ops, so every transformation downstream keeps a map back to them.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class SourceKind(StrEnum):
    pdf = "pdf"
    web = "web"
    docx = "docx"
    text = "text"
    image = "image"
    audio = "audio"


class BlockType(StrEnum):
    heading = "heading"
    paragraph = "paragraph"
    list_item = "list_item"
    quote = "quote"
    code = "code"
    table = "table"
    caption = "caption"
    figure = "figure"
    footnote = "footnote"
    header = "header"
    footer = "footer"
    page_number = "page_number"
    equation = "equation"
    toc = "toc"
    reference_entry = "reference_entry"


class MarkKind(StrEnum):
    citation_ref = "citation_ref"
    link = "link"
    url = "url"
    emphasis = "emphasis"
    parenthetical = "parenthetical"
    bracket = "bracket"
    brace = "brace"
    formula = "formula"
    footnote_ref = "footnote_ref"


class Mark(BaseModel):
    id: str
    kind: MarkKind
    start: int
    end: int


class BBox(BaseModel):
    """PDF points, origin top-left of the page (y grows downward)."""

    page: int
    x0: float
    y0: float
    x1: float
    y1: float


class DomAnchor(BaseModel):
    xpath: str
    start_offset: int = 0
    end_offset: int | None = None


class WordBox(BaseModel):
    """Word-level geometry for PDF highlight overlays: char range into Block.text + bbox."""

    start: int
    end: int
    bbox: BBox


class Block(BaseModel):
    id: str  # "blk_<n>" — stable within a document
    type: BlockType
    text: str
    order: int
    level: int | None = None  # heading level
    page: int | None = None  # 1-based
    bbox: BBox | None = None
    marks: list[Mark] = Field(default_factory=list)
    dom_anchor: DomAnchor | None = None  # web blocks posted by the shell preload
    word_boxes: list[WordBox] = Field(default_factory=list)  # PDF only, optional
    image_id: str | None = None  # figure/image blocks


class Document(BaseModel):
    id: str  # "doc_<source_hash[:16]>"
    source_id: str  # sha256 per 05 § 1
    kind: SourceKind
    title: str
    blocks: list[Block]
    page_count: int | None = None
    url: str | None = None
    site: str | None = None  # hostname, for rule scopes
    path: str | None = None
    language: str | None = None
    extractor_version: str = "1"
    flags: list[str] = Field(default_factory=list)  # e.g. "scanned_pages:3,4"

    def block(self, block_id: str) -> Block:
        for b in self.blocks:
            if b.id == block_id:
                return b
        raise KeyError(block_id)


Fidelity = Literal["exact", "block", "section", "media"]


class Anchor(BaseModel):
    """Where spoken text lives in the source. One of the three shapes in 04 (`playback.word`)."""

    block_id: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    bbox: BBox | None = None
    dom_anchor: DomAnchor | None = None
    media_t0: float | None = None
    media_t1: float | None = None

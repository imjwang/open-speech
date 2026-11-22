"""`html_blocks` from the shell's page preload (Readability in the user's session) → Document.

Block text is kept byte-for-byte as the preload sent it, because the preload maps `dom_anchor` plus char
offsets back to a DOM Range for highlighting; normalizing whitespace here would shift those offsets.
"""

from __future__ import annotations

import re

from open_speech.api.schemas import HtmlBlocksPayload
from open_speech.document.model import BlockType, Document, SourceKind
from open_speech.ingest.common import DraftBlock, build_document, finalize_blocks, infer_title, site_for, source_id_url

_TYPE_MAP: dict[str, BlockType] = {
    "p": BlockType.paragraph,
    "paragraph": BlockType.paragraph,
    "text": BlockType.paragraph,
    "div": BlockType.paragraph,
    "section": BlockType.paragraph,
    "heading": BlockType.heading,
    "title": BlockType.heading,
    "li": BlockType.list_item,
    "list_item": BlockType.list_item,
    "listitem": BlockType.list_item,
    "dt": BlockType.list_item,
    "dd": BlockType.list_item,
    "blockquote": BlockType.quote,
    "quote": BlockType.quote,
    "pre": BlockType.code,
    "code": BlockType.code,
    "table": BlockType.table,
    "caption": BlockType.caption,
    "figcaption": BlockType.caption,
    "figure": BlockType.figure,
    "img": BlockType.figure,
    "image": BlockType.figure,
    "footnote": BlockType.footnote,
    "aside": BlockType.footnote,
    "header": BlockType.header,
    "footer": BlockType.footer,
    "page_number": BlockType.page_number,
    "math": BlockType.equation,
    "equation": BlockType.equation,
    "toc": BlockType.toc,
    "nav": BlockType.toc,
    "reference": BlockType.reference_entry,
    "reference_entry": BlockType.reference_entry,
    "cite": BlockType.reference_entry,
}
_H = re.compile(r"^h([1-6])$")


def map_type(type_str: str, level: int | None) -> tuple[BlockType, int | None]:
    t = (type_str or "paragraph").strip().lower()
    if m := _H.match(t):
        return BlockType.heading, int(m.group(1))
    bt = _TYPE_MAP.get(t)
    if bt is None:
        try:
            bt = BlockType(t)
        except ValueError:
            bt = BlockType.paragraph
    return bt, (level or 2) if bt == BlockType.heading else None


def ingest_html_blocks(payload: HtmlBlocksPayload, *, title: str | None = None) -> Document:
    drafts: list[DraftBlock] = []
    for hb in payload.blocks:
        bt, level = map_type(hb.type, hb.level)
        if (not hb.text or not hb.text.strip()) and bt != BlockType.figure:
            continue
        drafts.append(DraftBlock(type=bt, text=hb.text, level=level, dom_anchor=hb.dom_anchor, block_id=hb.id))
    blocks = finalize_blocks(drafts)
    source_id = source_id_url(payload.url, (b.text for b in blocks))
    return build_document(
        source_id=source_id,
        kind=SourceKind.web,
        blocks=blocks,
        title=infer_title(
            blocks, explicit=title or payload.title, fallback=site_for(payload.url) or payload.url, strip_site=True
        ),
        url=payload.url,
        site=site_for(payload.url),
    )

"""Reading order at the top of a web page (05 § 2 Web): the lead is read before the material beside it.

Pages often put side material ahead of the first paragraph in the DOM: an infobox or sidebar table that
floats beside the lead, a hero image with its caption, a status icon. Read in DOM order, a listener hears a
picture caption or a fact table before learning what the page is about.

Decision: those blocks are *moved* to just after the first body paragraph; they are not dropped. Dropping
would remove content without it showing up in the "removed" list. Moved, they keep their type (`table`,
`caption`, `figure`), so the Tables and Captions presets and "skip figures" rules decide whether they are
read at all, and they stay next to the paragraph they sit beside on the page. Only the order changes:
block ids, text and DOM anchors are untouched, so in-page highlighting still lands on the right element.

Both web paths use this: blocks posted by the shell's page preload and headless trafilatura extraction.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from open_speech.document.model import BlockType
from open_speech.ingest.common import DraftBlock

SIDE_TYPES = frozenset({BlockType.table, BlockType.caption, BlockType.figure})
_PROSE_MIN_CHARS = 80
_SENTENCE_END = (".", "!", "?", "…", "。", "！", "？")
_TRAILING_REFS = re.compile(r"(?:\s*\[[^\]\n]{1,24}\])+\s*$")  # "… in Kent.[1][2]"
_CLOSERS = "\"'”’)]» "


def is_body_paragraph(draft: DraftBlock) -> bool:
    """A paragraph of prose (a sentence, or long enough to be one) rather than a label such as a byline,
    a dateline or a photo credit."""
    if draft.type != BlockType.paragraph:
        return False
    text = draft.text.strip()
    return len(text) >= _PROSE_MIN_CHARS or _TRAILING_REFS.sub("", text).rstrip(_CLOSERS).endswith(_SENTENCE_END)


def lead_first(drafts: Sequence[DraftBlock]) -> list[DraftBlock]:
    """Move tables, captions and figures that precede the first body paragraph to just after it.

    Nothing moves when the page has no body paragraph, or when a heading stands between a table and that
    paragraph: a table followed by a new section belongs to what came before (a page that opens with
    data), not beside the lead. A picture above the headline still moves.
    """
    out = list(drafts)
    lead = next((i for i, d in enumerate(out) if is_body_paragraph(d)), None)
    if lead is None:
        return out
    side = [i for i in range(lead) if out[i].type in SIDE_TYPES]
    if not side:
        return out
    last_heading = max((i for i in range(side[0], lead) if out[i].type == BlockType.heading), default=None)
    if last_heading is not None and any(out[i].type == BlockType.table for i in side if i < last_heading):
        return out
    moved = set(side)
    head = [d for i, d in enumerate(out[:lead]) if i not in moved]
    return [*head, out[lead], *(out[i] for i in side), *out[lead + 1 :]]

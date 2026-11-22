"""Spoken → source mapping helpers (for `playback.word` anchors and Script view).

A TTS word boundary arrives in *spoken-text* offsets of an utterance. `source_spans` maps it through
`Utterance.segments` to block char ranges; `anchor_for` builds the `Anchor` the reader draws (with the
word's PDF bbox when the block has word boxes, or the DOM anchor for web blocks).
"""

from __future__ import annotations

from open_speech.document.model import Anchor, BBox, Document
from open_speech.plan.script import Utterance

__all__ = ["anchor_for", "source_spans"]


def source_spans(utt: Utterance, start: int, end: int) -> list[tuple[str, int, int]]:
    """Block char ranges `(block_id, src_start, src_end)` that spoken [start, end) came from."""
    out: list[tuple[str, int, int]] = []
    for seg in utt.segments:
        if seg.spoken_end <= start or seg.spoken_start >= end:
            continue
        exact = (seg.spoken_end - seg.spoken_start) == (seg.src_end - seg.src_start)
        if exact:
            s = seg.src_start + max(0, start - seg.spoken_start)
            e = seg.src_start + min(seg.src_end - seg.src_start, end - seg.spoken_start)
        else:
            s, e = seg.src_start, seg.src_end
        if out and out[-1][0] == seg.block_id and out[-1][2] >= s:
            out[-1] = (seg.block_id, out[-1][1], max(out[-1][2], e))
        else:
            out.append((seg.block_id, s, e))
    return out


def anchor_for(utt: Utterance, start: int, end: int, document: Document | None = None) -> Anchor:
    """Best anchor for spoken [start, end): exact char range (+ bbox / DOM anchor) or the utterance's
    own block/section anchor for generated text."""
    spans = source_spans(utt, start, end)
    if not spans:
        return utt.anchors[0] if utt.anchors else Anchor()
    block_id, s, e = spans[0]
    anchor = Anchor(block_id=block_id, char_start=s, char_end=e)
    if document is not None:
        try:
            block = document.block(block_id)
        except KeyError:
            return anchor
        boxes = [wb.bbox for wb in block.word_boxes if wb.start < e and wb.end > s]
        if boxes:
            page = boxes[0].page
            same = [b for b in boxes if b.page == page]
            anchor.bbox = BBox(
                page=page,
                x0=min(b.x0 for b in same),
                y0=min(b.y0 for b in same),
                x1=max(b.x1 for b in same),
                y1=max(b.y1 for b in same),
            )
        anchor.dom_anchor = block.dom_anchor
    return anchor

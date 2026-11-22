"""DOCX → blocks (FR-READ-3): headings, lists, quotes, captions, code, TOC entries and tables map from
python-docx structure; footnotes and endnotes come from their XML parts (python-docx has no API for
them). A footnote block follows the paragraph that first references it; its marker is a `footnote_ref`
mark in that paragraph. Endnotes go at the end. Hyperlinks become `link` marks, bold/italic `emphasis`.
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterator

from lxml import etree

from open_speech.document.model import BlockType, Document, MarkKind, SourceKind
from open_speech.errors import EngineError
from open_speech.ingest.common import (
    DraftBlock,
    build_document,
    clean_title,
    finalize_blocks,
    infer_title,
    source_id_bytes,
)

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
RT_FOOTNOTES = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes"
RT_ENDNOTES = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/endnotes"
Mk = tuple[MarkKind, int, int]


def q(tag: str) -> str:
    return f"{{{W}}}{tag}"


_SKIP_NOTE_TYPES = {"separator", "continuationSeparator", "continuationNotice"}
_HEADING_STYLE = re.compile(r"^heading\s*(\d)$", re.IGNORECASE)
_PASSTHROUGH = {q("ins"), q("smartTag"), q("fldSimple"), q("customXml"), q("sdt"), q("sdtContent"), q("moveTo")}


class _Notes:
    """Footnote/endnote texts by id, plus display numbers assigned in reference order."""

    def __init__(self, root: etree._Element | None, tag: str) -> None:
        self.text: dict[str, str] = {}
        self.numbers: dict[str, str] = {}
        if root is None:
            return
        for note in root.iter(q(tag)):
            if note.get(q("type")) in _SKIP_NOTE_TYPES:
                continue
            nid = note.get(q("id"))
            if nid is None:
                continue
            parts = []
            for p in note.iter(q("p")):
                t, _, _ = _run_text(p, None)
                if t.strip():
                    parts.append(t.strip())
            self.text[nid] = " ".join(parts)

    def number(self, nid: str) -> str:
        if nid not in self.numbers:
            self.numbers[nid] = str(len(self.numbers) + 1)
        return self.numbers[nid]


def _run_text(el: etree._Element, notes: tuple[_Notes, _Notes] | None) -> tuple[str, list[Mk], list[tuple[str, str]]]:
    """Text of a paragraph-like element: (text, marks, [(kind, note id)] referenced in order)."""
    out: list[str] = []
    marks: list[Mk] = []
    refs: list[tuple[str, str]] = []
    length = 0

    def emit(s: str) -> None:
        nonlocal length
        out.append(s)
        length += len(s)

    def walk(node: etree._Element) -> None:
        for child in node:
            tag = child.tag
            if tag == q("r"):
                rpr = child.find(q("rPr"))
                emph = rpr is not None and any(
                    (x := rpr.find(q(t))) is not None and x.get(q("val"), "true") not in ("0", "false")
                    for t in ("b", "i")
                )
                start = length
                for rc in child:
                    rt = rc.tag
                    if rt == q("t"):
                        emit(rc.text or "")
                    elif rt in (q("tab"), q("br"), q("cr")):
                        emit(" ")
                    elif rt == q("noBreakHyphen"):
                        emit("-")
                    elif rt in (q("footnoteReference"), q("endnoteReference")) and notes is not None:
                        nid = rc.get(q("id")) or ""
                        kind = "foot" if rt == q("footnoteReference") else "end"
                        num = (notes[0] if kind == "foot" else notes[1]).number(nid)
                        s = length
                        emit(num)
                        marks.append((MarkKind.footnote_ref, s, length))
                        refs.append((kind, nid))
                if emph and length > start and "".join(out)[start:length].strip():
                    marks.append((MarkKind.emphasis, start, length))
            elif tag == q("hyperlink"):
                start = length
                walk(child)
                if length > start:
                    marks.append((MarkKind.link, start, length))
            elif tag in _PASSTHROUGH:
                walk(child)

    walk(el)
    return "".join(out), marks, refs


def _normalize(text: str, marks: list[Mk]) -> tuple[str, list[Mk]]:
    """Collapse whitespace runs to one space and trim, remapping mark offsets."""
    mapping: list[int] = []  # raw index → output index where that char starts
    out: list[str] = []
    prev_space = True
    for ch in text:
        mapping.append(len(out))
        if ch.isspace():
            if not prev_space:
                out.append(" ")
                prev_space = True
            continue
        out.append(ch)
        prev_space = False
    mapping.append(len(out))
    result = "".join(out)
    trimmed = result.rstrip()
    new_marks: list[Mk] = []
    for k, s, e in marks:
        ns, ne = mapping[s], min(mapping[e], len(trimmed))
        while ns < ne and trimmed[ns] == " ":
            ns += 1
        if ne > ns:
            new_marks.append((k, ns, ne))
    return trimmed, new_marks


def _style_name(p: etree._Element, styles: dict[str, str]) -> str:
    ppr = p.find(q("pPr"))
    if ppr is None:
        return ""
    ps = ppr.find(q("pStyle"))
    sid = ps.get(q("val")) if ps is not None else None
    return styles.get(sid or "", sid or "")


def _classify(p: etree._Element, style: str) -> tuple[BlockType, int | None]:
    s = style.strip().lower()
    if m := _HEADING_STYLE.match(s):
        return BlockType.heading, int(m.group(1))
    if s == "title":
        return BlockType.heading, 1
    ppr = p.find(q("pPr"))
    if ppr is not None:
        ol = ppr.find(q("outlineLvl"))
        if ol is not None and (v := ol.get(q("val"))) and v.isdigit() and int(v) < 9:
            return BlockType.heading, int(v) + 1
    if s.startswith("toc"):
        return BlockType.toc, None
    if "quote" in s:
        return BlockType.quote, None
    if s == "caption":
        return BlockType.caption, None
    if "code" in s or "preformatted" in s or s in ("source", "macro text"):
        return BlockType.code, None
    if s.startswith("list") or (ppr is not None and ppr.find(q("numPr")) is not None):
        return BlockType.list_item, None
    return BlockType.paragraph, None


def _cell_text(tc: etree._Element) -> str:
    parts = []
    for p in tc.iter(q("p")):
        t, _, _ = _run_text(p, None)
        if t.strip():
            parts.append(" ".join(t.split()))
    return " ".join(parts)


def _table_text(tbl: etree._Element) -> str:
    rows = []
    for tr in tbl.iter(q("tr")):
        cells: list[str] = []
        for tc in tr.findall(q("tc")):
            c = _cell_text(tc)
            if c and (not cells or cells[-1] != c):
                cells.append(c)
        if cells:
            line = ", ".join(cells)
            rows.append(line if line.endswith((".", "!", "?")) else line + ".")
    return "\n".join(rows)


def _body_elements(body: etree._Element) -> Iterator[etree._Element]:
    for child in body:
        if child.tag in (q("sdt"), q("sdtContent"), q("customXml")):
            yield from _body_elements(child)
        elif child.tag in (q("p"), q("tbl")):
            yield child


def _part_xml(doc: object, reltype: str) -> etree._Element | None:
    try:
        part = doc.part.part_related_by(reltype)  # type: ignore[attr-defined]
    except (KeyError, ValueError):
        return None
    try:
        return etree.fromstring(part.blob)
    except etree.XMLSyntaxError:
        return None


def ingest_docx(data: bytes, *, filename: str | None = None, title: str | None = None) -> Document:
    import docx

    try:
        d = docx.Document(io.BytesIO(data))
    except Exception as e:  # python-docx raises a variety of errors on bad packages
        raise EngineError("invalid_request", "That Word document couldn't be opened.") from e
    styles = {s.style_id: s.name for s in d.styles if getattr(s, "style_id", None) and s.name}
    foot = _Notes(_part_xml(d, RT_FOOTNOTES), "footnote")
    end = _Notes(_part_xml(d, RT_ENDNOTES), "endnote")
    drafts: list[DraftBlock] = []
    placed: set[str] = set()
    endnote_ids: list[str] = []
    for el in _body_elements(d.element.body):
        if el.tag == q("tbl"):
            t = _table_text(el)
            if t:
                drafts.append(DraftBlock(type=BlockType.table, text=t))
            continue
        raw, marks, refs = _run_text(el, (foot, end))
        text, marks = _normalize(raw, marks)
        if not text:
            continue
        btype, level = _classify(el, _style_name(el, styles))
        drafts.append(DraftBlock(type=btype, text=text, level=level, marks=marks))
        for kind, nid in refs:
            if kind == "foot" and nid not in placed and foot.text.get(nid):
                placed.add(nid)
                drafts.append(DraftBlock(type=BlockType.footnote, text=f"{foot.number(nid)} {foot.text[nid]}"))
            elif kind == "end" and nid not in endnote_ids and end.text.get(nid):
                endnote_ids.append(nid)
    for nid in endnote_ids:
        drafts.append(DraftBlock(type=BlockType.footnote, text=f"{end.number(nid)} {end.text[nid]}"))
    blocks = finalize_blocks(drafts)
    stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0] if filename else None
    core_title = clean_title(d.core_properties.title)
    return build_document(
        source_id=source_id_bytes(data),
        kind=SourceKind.docx,
        blocks=blocks,
        title=infer_title(blocks, explicit=title or core_title, fallback=stem),
    )

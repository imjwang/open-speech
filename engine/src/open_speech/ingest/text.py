"""TXT / Markdown / pasted text → blocks (FR-READ-4: Markdown structure maps to blocks).

Markdown: ATX/setext headings, paragraphs, lists (incl. task lists), block quotes, fenced and indented
code, GFM tables, `$$` math, images, footnote definitions; inline syntax is stripped from the spoken text
and kept as marks (links, emphasis, footnote refs). Plain text: paragraphs separated by blank lines
(hard-wrapped lines are re-joined), bullet lists, and conservative heading detection.
"""

from __future__ import annotations

import re

from open_speech.document.model import BlockType, Document, MarkKind, SourceKind
from open_speech.ingest.common import (
    DraftBlock,
    build_document,
    finalize_blocks,
    infer_title,
    source_id_bytes,
    source_id_text,
)

Mk = tuple[MarkKind, int, int]

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([\w+-]*)")
_ATX = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_SETEXT = re.compile(r"^\s{0,3}(=+|-+)\s*$")
_HR = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_BULLET = re.compile(r"^(\s*)([-*+•]|\d{1,3}[.)])\s+(.*)$")
_TASK = re.compile(r"^\[[ xX]\]\s+")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_FOOTDEF = re.compile(r"^\[\^([^\]\s]+)\]:\s*(.*)$")
_IMAGE_LINE = re.compile(r"^\s*!\[([^\]]*)\]\(([^)]*)\)\s*$")
_MATH_FENCE = re.compile(r"^\s*\$\$\s*(.*?)\s*(\$\$)?\s*$")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_FRONTMATTER = re.compile(r"\A---\s*\n.*?\n(---|\.\.\.)\s*\n", re.DOTALL)

_INLINE = re.compile(
    r"(?P<code>`+)(?P<code_body>.+?)(?P=code)"
    r"|(?P<img>!\[(?P<img_alt>[^\]]*)\]\([^)]*\))"
    r"|(?P<fn>\[\^(?P<fn_id>[^\]\s]+)\])"
    r"|(?P<link>\[(?P<link_text>(?:[^\[\]]|\[[^\]]*\])+)\]\((?P<link_href>[^)\s]*)(?:\s+\"[^\"]*\")?\))"
    r"|(?P<reflink>\[(?P<ref_text>[^\]]+)\]\[[^\]]*\])"
    r"|(?P<auto><(?P<auto_url>(?:https?|mailto):[^>\s]+)>)"
    r"|(?P<strong>(?P<sd>\*\*|__)(?P<strong_body>.+?)(?P=sd))"
    r"|(?P<em>(?<![\w*])\*(?P<em_body>[^*\s](?:[^*]*[^*\s])?)\*(?![\w*])"
    r"|(?<![\w_])_(?P<em_body2>[^_\s](?:[^_]*[^_\s])?)_(?![\w_]))"
    r"|(?P<strike>~~(?P<strike_body>.+?)~~)"
    r"|(?P<esc>\\(?P<esc_ch>[\\`*_{}\[\]()#+\-.!|~<>]))"
    r"|(?P<html></?[a-zA-Z][^>\n]{0,200}>)"
)


def render_inline(src: str) -> tuple[str, list[Mk]]:
    """Strip inline Markdown; return (plain text, structural marks with offsets into it)."""
    out: list[str] = []
    marks: list[Mk] = []
    length = 0
    pos = 0
    for m in _INLINE.finditer(src):
        lit = src[pos : m.start()]
        out.append(lit)
        length += len(lit)
        pos = m.end()

        def nested(body: str, kind: MarkKind | None) -> None:
            nonlocal length
            text, inner = render_inline(body)
            marks.extend((k, s + length, e + length) for k, s, e in inner)
            if kind is not None and text:
                marks.append((kind, length, length + len(text)))
            out.append(text)
            length += len(text)

        if m.group("code") is not None:
            body = m.group("code_body").strip()
            out.append(body)
            length += len(body)
        elif m.group("img") is not None:
            alt = m.group("img_alt")
            out.append(alt)
            length += len(alt)
        elif m.group("fn") is not None:
            fid = m.group("fn_id")
            marks.append((MarkKind.footnote_ref, length, length + len(fid)))
            out.append(fid)
            length += len(fid)
        elif m.group("link") is not None:
            nested(m.group("link_text"), MarkKind.link)
        elif m.group("reflink") is not None:
            nested(m.group("ref_text"), MarkKind.link)
        elif m.group("auto") is not None:
            url = m.group("auto_url")
            out.append(url)
            length += len(url)
        elif m.group("strong") is not None:
            nested(m.group("strong_body"), MarkKind.emphasis)
        elif m.group("em") is not None:
            nested(m.group("em_body") or m.group("em_body2") or "", MarkKind.emphasis)
        elif m.group("strike") is not None:
            nested(m.group("strike_body"), None)
        elif m.group("esc") is not None:
            out.append(m.group("esc_ch"))
            length += 1
        # html tags: dropped
    out.append(src[pos:])
    return "".join(out), marks


def _inline_block(btype: BlockType, raw: str, **kw: object) -> DraftBlock | None:
    joined = re.sub(r"\s*\n\s*", " ", raw.strip())
    text, marks = render_inline(joined)
    # Collapse whitespace while keeping mark offsets valid: only strip the ends.
    lead = len(text) - len(text.lstrip())
    text = text.strip()
    marks = [(k, s - lead, e - lead) for k, s, e in marks if s - lead >= 0 and e - lead <= len(text)]
    if not text:
        return None
    return DraftBlock(type=btype, text=text, marks=marks, **kw)  # type: ignore[arg-type]


def _table_text(rows: list[str]) -> str:
    out = []
    for row in rows:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        cells = [render_inline(c)[0].strip() for c in cells]
        cells = [c for c in cells if c]
        if cells:
            line = ", ".join(cells)
            out.append(line if line.endswith((".", "!", "?")) else line + ".")
    return "\n".join(out)


def parse_markdown(text: str) -> list[DraftBlock]:
    text = _FRONTMATTER.sub("", text.replace("\r\n", "\n").replace("\r", "\n"), count=1)
    text = _HTML_COMMENT.sub("", text)
    lines = text.split("\n")
    blocks: list[DraftBlock] = []
    para: list[str] = []
    footnotes: list[DraftBlock] = []

    def flush() -> None:
        if para:
            b = _inline_block(BlockType.paragraph, "\n".join(para))
            if b:
                blocks.append(b)
            para.clear()

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            flush()
            i += 1
            continue
        if m := _FENCE.match(line):
            flush()
            fence = m.group(1)
            body: list[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith(fence):
                body.append(lines[i])
                i += 1
            i += 1
            code = "\n".join(body).strip("\n")
            if code.strip():
                blocks.append(DraftBlock(type=BlockType.code, text=code))
            continue
        if m := _MATH_FENCE.match(line):
            flush()
            first = m.group(1)
            if m.group(2) and first:
                body_m = [first]
                i += 1
            else:
                body_m = [first] if first else []
                i += 1
                while i < n and "$$" not in lines[i]:
                    body_m.append(lines[i])
                    i += 1
                if i < n:
                    body_m.append(lines[i].replace("$$", ""))
                    i += 1
            eq = " ".join(x.strip() for x in body_m if x.strip())
            if eq:
                blocks.append(DraftBlock(type=BlockType.equation, text=eq))
            continue
        if m := _ATX.match(line):
            flush()
            b = _inline_block(BlockType.heading, m.group(2), level=len(m.group(1)))
            if b:
                blocks.append(b)
            i += 1
            continue
        if para and len(para) == 1 and (sm := _SETEXT.match(line)) and not _BULLET.match(para[0]):
            b = _inline_block(BlockType.heading, para[0], level=1 if sm.group(1)[0] == "=" else 2)
            para.clear()
            if b:
                blocks.append(b)
            i += 1
            continue
        if _HR.match(line):
            flush()
            i += 1
            continue
        if m := _FOOTDEF.match(stripped):
            flush()
            fid, body_text = m.group(1), [m.group(2)]
            i += 1
            while i < n and lines[i].startswith(("    ", "\t")) and lines[i].strip():
                body_text.append(lines[i].strip())
                i += 1
            b = _inline_block(BlockType.footnote, f"{fid} " + " ".join(body_text))
            if b:
                footnotes.append(b)
            continue
        if m := _IMAGE_LINE.match(line):
            flush()
            alt = m.group(1).strip()
            blocks.append(DraftBlock(type=BlockType.figure, text=alt))
            i += 1
            continue
        if _QUOTE.match(line):
            flush()
            qlines: list[str] = []
            while i < n and (qm := _QUOTE.match(lines[i])):
                qlines.append(qm.group(1))
                i += 1
            # Paragraphs inside the quote become separate quote blocks.
            chunk: list[str] = []
            for q in [*qlines, ""]:
                if q.strip():
                    chunk.append(q.lstrip("> ").strip())
                elif chunk:
                    b = _inline_block(BlockType.quote, "\n".join(chunk))
                    if b:
                        blocks.append(b)
                    chunk = []
            continue
        if "|" in line and i + 1 < n and _TABLE_SEP.match(lines[i + 1]):
            flush()
            rows = [line]
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(lines[i])
                i += 1
            t = _table_text(rows)
            if t:
                blocks.append(DraftBlock(type=BlockType.table, text=t))
            continue
        if m := _BULLET.match(line):
            flush()
            indent = len(m.group(1))
            marker, item = m.group(2), [_TASK.sub("", m.group(3))]
            i += 1
            while (
                i < n
                and lines[i].strip()
                and not _BULLET.match(lines[i])
                and (len(lines[i]) - len(lines[i].lstrip()) > indent or not _starts_block(lines[i]))
            ):
                item.append(lines[i].strip())
                i += 1
            prefix = f"{marker} " if marker[0].isdigit() else ""
            b = _inline_block(BlockType.list_item, prefix + "\n".join(item))
            if b:
                blocks.append(b)
            continue
        if line.startswith(("    ", "\t")) and not para:
            code_lines: list[str] = []
            while i < n and (lines[i].startswith(("    ", "\t")) or not lines[i].strip()):
                code_lines.append(lines[i][4:] if lines[i].startswith("    ") else lines[i].lstrip("\t"))
                i += 1
            code = "\n".join(code_lines).strip("\n")
            if code.strip():
                blocks.append(DraftBlock(type=BlockType.code, text=code))
            continue
        para.append(line)
        i += 1
    flush()
    return blocks + footnotes


def _starts_block(line: str) -> bool:
    return bool(_ATX.match(line) or _FENCE.match(line) or _QUOTE.match(line) or _HR.match(line))


# ------------------------------------------------------------------ plain text

_CHAPTER = re.compile(r"^(chapter|part|section|appendix)\s+([0-9ivxlcdm]+|[a-z]+)\b", re.IGNORECASE)
_NUMBERED_HEADING = re.compile(r"^\d+(\.\d+)*\.?\s+[A-Z]")


def _plain_heading_level(line: str, is_last: bool) -> int | None:
    if is_last or len(line) > 80 or line.endswith((".", ",", ";", ":", "!", "?")):
        return None
    letters = [c for c in line if c.isalpha()]
    if len(letters) >= 3 and all(c.isupper() for c in letters):
        return 1
    if _CHAPTER.match(line):
        return 1
    if _NUMBERED_HEADING.match(line) and len(line.split()) <= 10:
        return 2
    return None


def parse_plain(text: str) -> list[DraftBlock]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    blocks: list[DraftBlock] = []
    for idx, p in enumerate(paras):
        lines = [ln.strip() for ln in p.split("\n") if ln.strip()]
        if len(lines) > 1 and all(_BULLET.match(ln) for ln in lines):
            for ln in lines:
                m = _BULLET.match(ln)
                assert m
                prefix = f"{m.group(2)} " if m.group(2)[0].isdigit() else ""
                blocks.append(DraftBlock(type=BlockType.list_item, text=prefix + m.group(3).strip()))
            continue
        if len(lines) == 1:
            m = _BULLET.match(lines[0])
            if m and m.group(2) in "-*+•":
                blocks.append(DraftBlock(type=BlockType.list_item, text=m.group(3).strip()))
                continue
            level = _plain_heading_level(lines[0], idx == len(paras) - 1)
            if level is not None:
                blocks.append(DraftBlock(type=BlockType.heading, text=lines[0], level=level))
                continue
        joined = ""
        for ln in lines:
            if joined.endswith("-") and len(joined) > 1 and joined[-2].isalpha() and ln[:1].islower():
                joined = joined[:-1] + ln  # re-join a hyphenated line break
            else:
                joined = f"{joined} {ln}" if joined else ln
        blocks.append(DraftBlock(type=BlockType.paragraph, text=joined))
    return blocks


_MD_SIGNALS = (
    re.compile(r"^#{1,6}\s+\S", re.M),
    re.compile(r"^\s*(```|~~~)", re.M),
    re.compile(r"\[[^\]\n]+\]\([^)\s]+\)"),
    re.compile(r"^\s*\|.+\|\s*$\n^\s*\|?\s*:?-{3,}", re.M),
    re.compile(r"^\s*>\s+\S", re.M),
    re.compile(r"\*\*[^*\n]+\*\*"),
)


def looks_like_markdown(text: str) -> bool:
    return any(rx.search(text) for rx in _MD_SIGNALS)


def decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig",):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("cp1252", errors="replace")


def ingest_text(
    text: str,
    *,
    markdown: bool | None = None,
    title: str | None = None,
    filename: str | None = None,
    source_id: str | None = None,
) -> Document:
    """Plain text or Markdown → Document. `markdown=None` sniffs pasted text."""
    use_md = looks_like_markdown(text) if markdown is None else markdown
    drafts = parse_markdown(text) if use_md else parse_plain(text)
    blocks = finalize_blocks(drafts)
    stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0] if filename else None
    return build_document(
        source_id=source_id or source_id_text(text),
        kind=SourceKind.text,
        blocks=blocks,
        title=infer_title(blocks, explicit=title, fallback=stem),
    )


def ingest_text_file(data: bytes, *, filename: str | None, title: str | None = None) -> Document:
    text = decode_text(data)
    is_md = bool(filename and filename.lower().endswith((".md", ".markdown", ".mdown", ".mkd")))
    return ingest_text(
        text, markdown=True if is_md else None, title=title, filename=filename, source_id=source_id_bytes(data)
    )

"""Golden corpus generator (10 § Testing strategy).

Builds deterministic fixtures with reportlab / python-docx / Pillow:

- academic.pdf  — two-column paper: running headers (left + right fragments), footers, "Page N of M",
                  numeric and author-year citations, superscript footnotes, a hyphenated line break, a
                  paragraph continuing across columns, a display equation, a raster figure + caption,
                  and a References section.
- report.pdf    — single-column report: headings, paragraphs with spacing, bullets, a ruled table, a URL,
                  "Page N of M" footers.
- scanned.pdf   — one page that is only an image (no text layer).
- footnotes.docx — headings, paragraphs, a bulleted list, a quote, a table, two footnotes, a hyperlink.
- news_blocks.json — an `html_blocks` payload as the shell preload would post it.
- notes.md      — Markdown with headings, lists, a code fence, a table, a quote and a footnote.

Run `uv run python -m tests.golden.make_fixtures` (from engine/) to rewrite tests/fixtures/pipeline/.
The tests call these functions directly, so the committed binaries are for manual inspection only.
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pipeline"

# ------------------------------------------------------------------ tiny layout engine (reportlab)


@dataclass
class Item:
    kind: str  # h1 | h2 | p | p0 (no indent) | eq | fig | cap | ref | li
    text: str = ""
    footnote: tuple[str, str] | None = None  # (marker, text) placed at the bottom of the column


@dataclass
class Region:
    x: float
    width: float
    top: float  # y from top of page
    bottom: float  # y from top of page


class Writer:
    """Places items into column regions across pages. Text markup: `^{1}` superscript, `~` soft hyphen."""

    BODY = ("Times-Roman", 10.0, 12.0)

    def __init__(self, pagesize: tuple[float, float]) -> None:
        from reportlab.pdfgen import canvas

        self.buf = io.BytesIO()
        self.c = canvas.Canvas(self.buf, pagesize=pagesize, invariant=1)
        self.w, self.h = pagesize
        self.page_regions: list[list[Region]] = []

    # coordinates: we track y-from-top; reportlab wants y-from-bottom
    def Y(self, y_top: float) -> float:  # noqa: N802
        return self.h - y_top

    @staticmethod
    def width(text: str, font: str, size: float) -> float:
        from reportlab.pdfbase.pdfmetrics import stringWidth

        return stringWidth(re.sub(r"\^\{[^}]*\}", "", text).replace("~", ""), font, size) + sum(
            stringWidth(m, font, size * 0.6) for m in re.findall(r"\^\{([^}]*)\}", text)
        )

    @staticmethod
    def soft_hyphenate(word: str) -> str:
        """Allow a break in the middle of long plain words (like LaTeX would)."""
        core = word.rstrip(".,;:)")
        if "~" in word or len(core) < 9 or not core.isalpha() or not core.islower():
            return word
        k = len(core) // 2
        return word[:k] + "~" + word[k:]

    def wrap(self, text: str, font: str, size: float, width: float, indent: float = 0.0) -> list[str]:
        words = [self.soft_hyphenate(w) for w in text.split()]
        lines: list[str] = []
        cur = ""
        avail = width - indent
        i = 0
        while i < len(words):
            w = words[i]
            if "|" in w:  # forced hyphenated break (fixture control)
                head, _, tail = w.partition("|")
                cand2 = f"{cur} {head}-" if cur else f"{head}-"
                if self.width(cand2, font, size) <= avail:
                    lines.append(cand2)
                    words[i] = tail
                    cur = ""
                    avail = width
                    continue
                w = words[i] = w.replace("|", "")
            cand = f"{cur} {w}" if cur else w
            if self.width(cand, font, size) <= avail:
                cur = cand
                i += 1
                continue
            if "~" in w:
                head, _, tail = w.partition("~")
                cand2 = f"{cur} {head}-" if cur else f"{head}-"
                if self.width(cand2, font, size) <= avail:
                    lines.append(cand2)
                    words[i] = tail
                    cur = ""
                    avail = width
                    continue
            if not cur:
                cur = w
                i += 1
            lines.append(cur)
            cur = ""
            avail = width
        if cur:
            lines.append(cur)
        return [ln.replace("~", "") for ln in lines]

    def draw_line(self, x: float, y_top: float, text: str, font: str, size: float, center_w: float | None = None):  # type: ignore[no-untyped-def]
        if center_w is not None:
            x = x + (center_w - self.width(text, font, size)) / 2
        t = self.c.beginText(x, self.Y(y_top))
        for piece in re.split(r"(\^\{[^}]*\})", text):
            if not piece:
                continue
            if piece.startswith("^{"):
                t.setFont(font, size * 0.6)
                t.setRise(size * 0.38)
                t.textOut(piece[2:-1])
                t.setRise(0)
            else:
                t.setFont(font, size)
                t.textOut(piece)
        self.c.drawText(t)

    def new_page(self, regions: list[Region]) -> None:
        if self.page_regions:
            self.c.showPage()
        self.page_regions.append(regions)


def _figure_png(w: int = 360, h: int = 220) -> bytes:
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(im)
    for i, val in enumerate([40, 90, 140, 180, 120]):
        d.rectangle([30 + i * 60, h - 20 - val, 70 + i * 60, h - 20], fill=(60, 90 + i * 20, 160))
    d.line([20, h - 20, w - 10, h - 20], fill="black", width=2)
    d.line([20, 10, 20, h - 20], fill="black", width=2)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _flow(
    wr: Writer,
    items: list[Item],
    columns: list[list[Region]],
    *,
    first_indent: float = 12.0,
    para_space: float = 0.0,
) -> None:
    """Flow items through `columns` (a list per page of column regions)."""
    from reportlab.lib.utils import ImageReader

    page_idx, col_idx = 0, 0
    region = columns[0][0]
    y = region.top
    bottom = region.bottom
    col_notes: list[tuple[str, str]] = []
    prev_kind = ""

    def flush_notes() -> None:
        nonlocal col_notes
        if not col_notes:
            return
        font, size, lead = "Times-Roman", 8.0, 9.5
        lines: list[str] = []
        for marker, text in col_notes:
            wrapped = wr.wrap(text, font, size, region.width - 8)
            lines.append(f"^{{{marker}}}" + wrapped[0])
            lines.extend(wrapped[1:])
        yy = region.bottom - lead * (len(lines) - 1)
        wr.c.setLineWidth(0.5)
        wr.c.line(region.x, wr.Y(yy - 12), region.x + 60, wr.Y(yy - 12))
        for ln in lines:
            wr.draw_line(region.x, yy, ln, font, size)
            yy += lead
        col_notes = []

    def next_column() -> None:
        nonlocal page_idx, col_idx, region, y, bottom
        flush_notes()
        col_idx += 1
        if col_idx >= len(columns[page_idx]):
            page_idx += 1
            col_idx = 0
            if page_idx >= len(columns):
                raise RuntimeError("fixture text overflowed the page plan")
            wr.new_page(columns[page_idx])
        region = columns[page_idx][col_idx]
        y = region.top
        bottom = region.bottom

    for it in items:
        if it.kind in ("h1", "h2"):
            font, size, lead = ("Helvetica-Bold", 12.0, 15.0) if it.kind == "h1" else ("Helvetica-Bold", 10.5, 13.0)
            if y + 2 * lead + 24 > bottom:
                next_column()
            if y > region.top + 1:
                y += 8
            y += size
            wr.draw_line(region.x, y, it.text, font, size)
            y += lead - size + 3
            prev_kind = it.kind
            continue
        if it.kind == "eq":
            if y + 22 > bottom:
                next_column()
            y += 16
            wr.draw_line(region.x, y, it.text, "Times-Italic", 10.0, center_w=region.width)
            y += 10
            prev_kind = it.kind
            continue
        if it.kind == "fig":
            h = 120.0
            if y + h + 10 > bottom:
                next_column()
            img = ImageReader(io.BytesIO(_figure_png()))
            wr.c.drawImage(img, region.x + 10, wr.Y(y + 6 + h), width=region.width - 20, height=h)
            y += h + 10
            prev_kind = it.kind
            continue
        font, size, lead = wr.BODY
        indent = 0.0
        if it.kind == "cap" or it.kind == "ref":
            font, size, lead = "Times-Roman", 9.0, 10.5
        elif it.kind == "p" and prev_kind == "p":
            indent = first_indent
        text = it.text
        if it.kind == "li":
            text = "• " + text
        lines = wr.wrap(text, font, size, region.width - (10 if it.kind == "li" else 0), indent=indent)
        if para_space and prev_kind in ("p", "p0", "li", "cap"):
            y += para_space
        for i, ln in enumerate(lines):
            note_room = 0.0
            if it.footnote and not col_notes:
                note_room = 24.0
            if y + lead > bottom - note_room - (len(col_notes) * 12 if col_notes else 0):
                next_column()
            y += lead
            x = region.x + (indent if i == 0 else 0)
            if it.kind == "li" and i > 0:
                x += 10
            if it.kind == "ref" and i > 0:
                x += 12  # hanging indent
            wr.draw_line(x, y, ln, font, size)
            if it.footnote and f"^{{{it.footnote[0]}}}" in ln:
                col_notes.append(it.footnote)
                bottom = region.bottom - 26
        if it.kind == "cap":
            y += 4
        prev_kind = it.kind if it.kind != "p0" else "p"
    flush_notes()


ACADEMIC_TITLE = "Listening to Papers: Robust Reading Order for Synthetic Documents"
ABSTRACT = (
    "We study how a speech engine can read academic papers aloud without stumbling over running heads, "
    "page numbers or citation markers. Our method clusters text lines into columns, detects repeated "
    "page furniture and types every block, so that a listener hears only the content. On a synthetic "
    "corpus it removes all headers and footers while keeping the body in the correct reading order."
)

ACADEMIC_ITEMS: list[Item] = [
    Item("h1", "1 Introduction"),
    Item(
        "p0",
        "Reading documents aloud is an old idea [1], yet most tools still read everything on the page, "
        "including the running head and the page number. Screen readers improved this for the web [2, 3], "
        "but scientific papers remain hard because of their dense two-column layouts [4–6].",
    ),
    Item(
        "p",
        "Prior systems relied on hand-written templates (Smith et al., 2020). Others learned layout from "
        "large corpora (Lee and Kim, 2019; Garcia 2021) and needed expensive annota|tion of every page "
        "in the training set. Our approach needs no training data^{1} and runs in milliseconds on a laptop.",
        footnote=("1", "Code and data are available from the authors on request."),
    ),
    Item(
        "p",
        "Listeners care most about continuity. A sentence that is split across two columns should still "
        "be heard as one sentence, and a word hyphenated at the end of a line should be heard as one word. "
        "We therefore treat the column as the unit of reading and merge paragraphs that continue across "
        "column breaks, which keeps the prosody of the synthe~sized speech natural and the highlighting "
        "exact, because every word in the output maps back to its box on the page. Earlier proto|types "
        "that ignored this produced audible seams in the middle of sentences, which listeners in our "
        "pilot study found distracting. They also found it tiring to hear the same journal name at the "
        "top of every page, and several asked for a way to skip citation markers entirely.",
    ),
    Item(
        "p",
        "This paper makes three contributions. First, we describe a gutter detector that works on the "
        "raw character boxes of a page. Second, we show how repeated page furniture can be found with a "
        "simple vote across pages. Third, we evaluate the combination on a synthetic corpus whose ground "
        "truth is known exactly, so that every error can be traced to a specific heuristic and fixed.",
    ),
    Item("h1", "2 Method"),
    Item(
        "p0",
        "We first group charac~ters into lines and lines into columns. Smith et al. (2020) showed that "
        "the gap between columns is the strongest signal, and we follow their observation. The score of "
        "a candidate gutter is defined as",
    ),
    Item("eq", "S(g) = w · h(g) + b          (1)"),
    Item(
        "p0",
        "where h(g) is the height of the empty band. Lines that cross every gutter are treated as "
        "full-width and break the page into bands^{2} that are read one after another.",
        footnote=("2", "A band is a horizontal slice of the page between two full-width lines."),
    ),
    Item("h2", "2.1 Page furniture"),
    Item(
        "p0",
        "Repeated page furniture is detected by comparing the top and bottom eight percent of every page. "
        "Text that repeats on at least thirty percent of pages is marked as a header or footer, and short "
        "numeric lines are marked as page numbers. Digits are normalized before the comparison, so that "
        "a running head which includes the page number still matches across pages. More details are "
        "given at https://example.org/method together with the evalua~tion scripts.",
    ),
    Item("fig"),
    Item("cap", "Figure 1: Share of pages with correctly ordered columns for five document families."),
    Item(
        "p",
        "Footnotes are recognized by their small type, their position at the bottom of a column and a "
        "leading marker that matches a superscript in the body text. Captions are recognized by their "
        "label and by their distance to the nearest figure or table on the page.",
    ),
    Item("h1", "3 Results"),
    Item(
        "p0",
        "On our synthetic corpus the method removed every running head and page number while keeping the "
        "order of the body text intact. Errors were concentrated in pages with wide tables [7], which we "
        "leave for future work. The detector never merged two columns into one line, even on pages where "
        "the gutter was only a few points wide, because the vote uses every line of the page rather than "
        "a single row of text.",
    ),
    Item(
        "p",
        "We also measured how often a paragraph was split in the wrong place. Splits happened only when "
        "a paragraph ended exactly at the right margin, which is rare in practice and harmless for "
        "listening, since the reader pauses briefly at the end of each utterance anyway. A short pilot "
        "with six listeners confirmed that the output was easy to follow (Park, 2022).",
    ),
    Item("h1", "4 Discussion"),
    Item(
        "p0",
        "Two limitations deserve attention. The first is that the method assumes a fixed number of "
        "columns per band, so a page that mixes a narrow sidebar with a wide main column is read as two "
        "columns of equal standing. In practice this produced a plausible order on every page we tried, "
        "but a sidebar placed in the middle of the main text would be read too early.",
    ),
    Item(
        "p",
        "The second limitation concerns tables. A ruled table is easy to find, but a table drawn with "
        "whitespace alone looks very much like a block of short lines, and our detector reads it row by "
        "row. Listeners rated this acceptable for small tables and tedious for large ones, which suggests "
        "that a short spoken summary would serve them better than a faithful reading of every cell.",
    ),
    Item(
        "p",
        "Finally, we note that every heuristic described here is deterministic and runs without network "
        "access. This matters for privacy, since the documents people listen to are often personal, and "
        "for cost, since nothing needs to be sent to a remote model before reading can begin. When a "
        "language model is available, it can refine the result, but it is never required.",
    ),
    Item("h1", "5 Conclusion"),
    Item(
        "p0",
        "Simple geometric heuristics are enough to make papers pleasant to listen to. We hope this "
        "encourages more work on listening-first document tools, and we plan to extend the method to "
        "documents with three or more columns.",
    ),
    Item("h1", "References"),
    Item("ref", "[1] D. Klatt. Review of text-to-speech conversion for English. JASA, 1987."),
    Item("ref", "[2] A. Smith and B. Jones. Accessible reading on the web. In Proc. ASSETS, 2015."),
    Item("ref", "[3] C. Lee. Screen readers in practice: a decade of lessons learned in the field. CHI, 2018."),
    Item("ref", "[4] M. Garcia. Layout analysis of scientific documents. ICDAR, 2021."),
    Item("ref", "[5] R. Smith, T. Brown and K. Wu. Reading order from geometry. DocEng, 2020."),
    Item("ref", "[6] S. Lee and J. Kim. Learning layout from large corpora. ACL, 2019."),
    Item("ref", "[7] P. Park. Tables are hard. Workshop on Document Intelligence, 2022."),
]


def _academic_pages() -> int:
    import pypdfium2 as pdfium

    global _PAGES_PASS
    if _PAGES_PASS is None:
        _PAGES_PASS = 3  # first pass: any value; the layout doesn't depend on it
        data = academic_pdf()
        doc = pdfium.PdfDocument(data)
        _PAGES_PASS = len(doc)
        doc.close()
    return _PAGES_PASS


_PAGES_PASS: int | None = None


def academic_pdf() -> bytes:
    from reportlab.lib.pagesizes import letter

    w, h = letter
    left, right, gutter = 54.0, 54.0, 24.0
    col_w = (w - left - right - gutter) / 2
    top, bottom = 72.0, h - 66.0

    pages_plan = _academic_pages()
    wr = Writer(letter)
    wr.c.setTitle(ACADEMIC_TITLE)
    wr.c.setAuthor("A. Lee; B. Park")

    # Page 1: full-width title block, then two columns.
    wr.new_page([])
    y = top + 10
    for line in wr.wrap(ACADEMIC_TITLE, "Helvetica-Bold", 17, w - left - right - 60):
        y += 20
        wr.draw_line(left, y, line, "Helvetica-Bold", 17, center_w=w - left - right)
    y += 22
    wr.draw_line(left, y, "Ana Lee and Ben Park", "Helvetica", 11, center_w=w - left - right)
    y += 14
    wr.draw_line(left, y, "Department of Listening, Example University", "Helvetica", 9.5, center_w=w - left - right)
    y += 26
    wr.draw_line(left + 36, y, "Abstract", "Helvetica-Bold", 10.5)
    y += 4
    for line in wr.wrap(ABSTRACT, "Times-Roman", 10, w - left - right - 72):
        y += 12
        wr.draw_line(left + 36, y, line, "Times-Roman", 10)
    y += 18
    cols = [
        [Region(left, col_w, y, bottom), Region(left + col_w + gutter, col_w, y, bottom)],
        [Region(left, col_w, top, bottom), Region(left + col_w + gutter, col_w, top, bottom)],
        [Region(left, col_w, top, bottom), Region(left + col_w + gutter, col_w, top, bottom)],
    ]
    wr.page_regions[0] = cols[0]
    furniture_drawn: set[int] = set()

    def furniture(page_no: int) -> None:
        if page_no in furniture_drawn:
            return
        furniture_drawn.add(page_no)
        c = wr.c
        c.setFont("Helvetica-Oblique", 8)
        c.drawString(left, h - 40, "Journal of Synthetic Results · Vol. 7")
        c.drawRightString(w - right, h - 40, "Lee and Park")
        c.setFont("Helvetica", 7.5)
        c.drawString(left, 30, "© 2026 The Authors. Licensed under CC BY 4.0.")
        c.setFont("Helvetica", 9)
        c.drawCentredString(w / 2, 44, f"Page {page_no} of {pages_plan}")

    # Hook new_page so furniture is drawn on every page before it is finished.
    orig_new_page = wr.new_page

    def new_page(regions: list[Region]) -> None:
        furniture(len(wr.page_regions))
        orig_new_page(regions)

    wr.new_page = new_page  # type: ignore[method-assign]
    _flow(wr, ACADEMIC_ITEMS, cols)
    furniture(len(wr.page_regions))
    wr.c.showPage()
    wr.c.save()
    return wr.buf.getvalue()


REPORT_ITEMS: list[Item] = [
    Item("h1", "Quarterly Operations Report"),
    Item(
        "p0",
        "This report summarizes operations for the third quarter. Output grew steadily while costs stayed "
        "flat, and the new roasting line reached full capacity in August.",
    ),
    Item("h2", "Highlights"),
    Item("li", "Production rose to 41 tonnes, up 12 percent from the second quarter."),
    Item("li", "Energy use per kilogram fell for the third quarter in a row."),
    Item("li", "Two new wholesale partners signed annual contracts."),
    Item("h2", "Production"),
    Item(
        "p0",
        "Most of the growth came from the new line, which ran two shifts from July onward. The older line "
        "was used for small batches and for training new staff. Detailed figures are published at "
        "https://example.com/reports/q3 for partners.",
    ),
    Item("table"),
    Item("cap", "Table 1: Output by line, in tonnes."),
    Item("h2", "Outlook"),
    Item(
        "p0",
        "We expect demand to stay strong through the holidays. The main risk is the price of green coffee, "
        "which rose sharply in September (see the appendix for the full price series). We will review "
        "contracts in January.",
    ),
]


def report_pdf() -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import Table, TableStyle

    w, h = letter
    left = 72.0
    width = w - 2 * left
    wr = Writer(letter)
    wr.c.setTitle("Quarterly Operations Report")
    total = 2

    def furniture(page_no: int) -> None:
        wr.c.setFont("Helvetica", 8)
        wr.c.drawString(left, 34, "Northwind Roasters · Internal")
        wr.c.drawRightString(w - left, 34, f"Page {page_no} of {total}")

    split = REPORT_ITEMS.index(next(it for it in REPORT_ITEMS if it.kind == "table"))
    first, second = REPORT_ITEMS[:split], REPORT_ITEMS[split + 1 :]

    wr.new_page([Region(left, width, 80, h - 80)])
    _flow(wr, first, [[Region(left, width, 80, h - 80)]], first_indent=0, para_space=8)
    furniture(1)
    wr.c.showPage()
    # Page 2: table, caption, the rest.
    wr.page_regions.append([Region(left, width, 80, h - 80)])
    data = [
        ["Line", "July", "August", "September"],
        ["New line", "6.1", "8.4", "8.9"],
        ["Old line", "5.2", "5.0", "4.7"],
    ]
    t = Table(data, colWidths=[110, 80, 80, 80])
    t.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.6, colors.black),
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
                ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
            ]
        )
    )
    tw, th = t.wrapOn(wr.c, width, 200)
    t.drawOn(wr.c, left, h - 90 - th)
    y0 = 90 + th + 6
    _flow(wr, second, [[Region(left, width, y0, h - 80)]], first_indent=0, para_space=8)
    furniture(2)
    wr.c.showPage()
    wr.c.save()
    return wr.buf.getvalue()


def scanned_pdf() -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter, invariant=1)
    c.drawImage(ImageReader(io.BytesIO(_figure_png(612, 792))), 0, 0, width=612, height=792)
    c.showPage()
    c.save()
    return buf.getvalue()


# ------------------------------------------------------------------ DOCX

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def footnotes_docx() -> bytes:
    import datetime

    import docx
    from docx.opc.constants import CONTENT_TYPE as CT
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    d = docx.Document()
    d.core_properties.title = "Field Notes on Cupping"
    d.core_properties.created = datetime.datetime(2026, 1, 1)
    d.core_properties.modified = datetime.datetime(2026, 1, 1)
    d.core_properties.last_modified_by = "fixture"

    notes = {
        1: "Scores follow the 2004 protocol with a 100-point scale.",
        2: "Water was filtered to 120 ppm total dissolved solids.",
    }
    body = "".join(
        f'<w:footnote w:id="{i}"><w:p><w:pPr><w:pStyle w:val="FootnoteText"/></w:pPr>'
        f'<w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:footnoteRef/></w:r>'
        f'<w:r><w:t xml:space="preserve"> {t}</w:t></w:r></w:p></w:footnote>'
        for i, t in notes.items()
    )
    xml = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:footnotes xmlns:w="{W_NS}">'
        '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
        '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p>'
        f"</w:footnote>{body}</w:footnotes>"
    )
    part = Part(PackURI("/word/footnotes.xml"), CT.WML_FOOTNOTES, xml.encode("utf-8"), d.part.package)
    d.part.relate_to(part, RT.FOOTNOTES)

    def ref(paragraph, nid: int) -> None:  # type: ignore[no-untyped-def]
        r = paragraph.add_run()._r
        rpr = OxmlElement("w:rPr")
        va = OxmlElement("w:vertAlign")
        va.set(qn("w:val"), "superscript")
        rpr.append(va)
        r.append(rpr)
        el = OxmlElement("w:footnoteReference")
        el.set(qn("w:id"), str(nid))
        r.append(el)

    d.add_heading("Field Notes on Cupping", level=0)
    p = d.add_paragraph("We cupped twelve lots from three origins over two days. Each lot was scored by four tasters")
    ref(p, 1)
    p.add_run(" and the scores were averaged.")
    d.add_heading("Method", level=1)
    p = d.add_paragraph("Samples were roasted light and rested for a day. Brewing water was heated to 93 °C")
    ref(p, 2)
    p.add_run(". The ")
    run = p.add_run("full protocol")
    run.italic = True
    p.add_run(" is described at https://example.net/cupping.")
    d.add_paragraph("Grind size was fixed for every sample.", style="List Bullet")
    d.add_paragraph("Cups were covered until the break.", style="List Bullet")
    d.add_heading("Results", level=1)
    d.add_paragraph("The washed lots scored highest overall, as the table shows.")
    table = d.add_table(rows=3, cols=2)
    for r, (a, b) in enumerate([("Origin", "Mean score"), ("Ethiopia", "87.5"), ("Colombia", "85.0")]):
        table.cell(r, 0).text = a
        table.cell(r, 1).text = b
    d.add_paragraph("Clarity beats intensity when the cups cool.", style="Quote")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ web + markdown


def news_blocks() -> dict[str, object]:
    url = "https://www.example-news.com/2026/11/council-budget"
    blocks = [
        ("h1", "Council approves new budget after long debate"),
        (
            "p",
            "The city council voted 7 to 2 on Tuesday to approve next year's budget, which adds funding "
            "for parks [3] and road repairs (about $5 million in total).",
        ),
        (
            "p",
            'Supporters said the plan was overdue. "It\'s about time," said council member Rosa Diaz, '
            "who has pushed for park funding since 2019 (Diaz, 2021).",
        ),
        ("h2", "What changes"),
        ("li", "Parks get $2 million for maintenance."),
        ("li", "Road repairs get $3 million, see https://example-news.com/budget-table for details."),
        ("blockquote", "We will deliver on our promises, the mayor said in a statement."),
        ("figcaption", "Figure 2: Council members during the vote on Tuesday."),
        ("p", "The budget takes effect in January."),
    ]
    return {
        "url": url,
        "title": "Council approves new budget after long debate | Example News",
        "blocks": [
            {
                "id": f"b{i}",
                "type": t,
                "text": text,
                "dom_anchor": {"xpath": f"/html/body/article/*[{i + 1}]", "start_offset": 0},
            }
            for i, (t, text) in enumerate(blocks)
        ],
    }


NOTES_MD = """---
author: fixture
---
# Brewing Notes

A short guide to **pour-over** brewing. See the [full guide](https://example.org/guide) for more.

## Equipment

- A gooseneck kettle
- A paper filter, rinsed[^1]
- 1. Not a real list item, just text

## Recipe

1. Weigh 15 g of coffee.
2. Pour 250 g of water at 94 °C in three stages.

```
ratio = water / coffee
```

| Step | Time |
|------|------|
| Bloom | 0:45 |
| Finish | 3:00 |

> Taste first, then adjust the grind.

[^1]: Rinsing removes the papery taste.
"""


def notes_md() -> str:
    return NOTES_MD


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / "academic.pdf").write_bytes(academic_pdf())
    (FIXTURES / "report.pdf").write_bytes(report_pdf())
    (FIXTURES / "scanned.pdf").write_bytes(scanned_pdf())
    (FIXTURES / "footnotes.docx").write_bytes(footnotes_docx())
    (FIXTURES / "news_blocks.json").write_text(json.dumps(news_blocks(), indent=2, ensure_ascii=False) + "\n")
    (FIXTURES / "notes.md").write_text(notes_md())
    print(f"wrote fixtures to {FIXTURES}")


if __name__ == "__main__":
    main()

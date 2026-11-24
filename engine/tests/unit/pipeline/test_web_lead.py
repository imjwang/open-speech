"""Web pages read the lead before the material beside it: an infobox, a picture caption, an icon (05 § 2 Web).

`wikipedia_lead.html` is the lead of a Wikipedia article as the desktop site serves it. In its DOM the infobox
table (with an image and its caption) comes before the first paragraph. Both web paths are covered:

- the shell preload: `wikipedia_lead_blocks.json` is what `app/src/preload/web/extract.ts` posts for that page
  (the app's `extract.test.ts` fails if the two drift apart);
- headless: trafilatura on the same HTML.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_speech.api.schemas import HtmlBlocksPayload
from open_speech.config import Paths
from open_speech.document.model import BlockType, Document
from open_speech.ingest.common import DraftBlock
from open_speech.ingest.html_blocks import ingest_html_blocks
from open_speech.ingest.lead import is_body_paragraph, lead_first
from open_speech.ingest.url import extract_html
from open_speech.plan.planner import PlanContext, Planner
from open_speech.plan.script import SpeechScript
from open_speech.rules.files import RuleStore

FIXTURES = Path(__file__).parents[2] / "fixtures" / "pipeline"
PAGE_URL = "https://en.wikipedia.org/wiki/Ada_Lovelace"
LEAD = "Augusta Ada King, Countess of Lovelace (née Byron; 10 December 1815 – 27 November 1852)"
SECOND = "Lovelace was the only legitimate child"
INFOBOX_CAPTION = "Daguerreotype of Lovelace"

B = BlockType
PROSE = "The city council voted on Tuesday to approve next year's budget after a long debate."


def _drafts(*specs: tuple[BlockType, str]) -> list[DraftBlock]:
    return [DraftBlock(type=t, text=text) for t, text in specs]


def _texts(drafts: list[DraftBlock]) -> list[str]:
    return [d.text for d in drafts]


def _payload() -> HtmlBlocksPayload:
    return HtmlBlocksPayload.model_validate(json.loads((FIXTURES / "wikipedia_lead_blocks.json").read_text("utf-8")))


def _posted() -> Document:
    return ingest_html_blocks(_payload())


def _headless() -> Document:
    return extract_html((FIXTURES / "wikipedia_lead.html").read_bytes(), url=PAGE_URL)


async def _script(doc: Document, tmp_path: Path, **presets: bool) -> SpeechScript:
    store = RuleStore(Paths(tmp_path).ensure())
    store.ensure_defaults()
    if presets:
        from open_speech.rules.prompt import prompt_from_presets

        store.paths.speech_md.write_text(prompt_from_presets(presets), "utf-8")
        store.reload()
    ctx = PlanContext(
        document=doc, rules=store.plan_rules(doc), lexicon=store.lexicon(), speech_md=store.plan_speech_md()
    )
    return (await Planner(None).plan(ctx, None)).script


def _spoken(script: SpeechScript) -> str:
    return "\n".join(u.text for u in script.utterances)


# ------------------------------------------------------------------ the rule


def test_side_material_before_the_lead_moves_to_just_after_it() -> None:
    drafts = _drafts(
        (B.heading, "Title"),
        (B.figure, "Padlock icon"),
        (B.table, "Born, 1815."),
        (B.caption, "Portrait, 1843"),
        (B.paragraph, PROSE),
        (B.paragraph, "Second paragraph."),
    )
    assert _texts(lead_first(drafts)) == [
        "Title",
        PROSE,
        "Padlock icon",
        "Born, 1815.",
        "Portrait, 1843",
        "Second paragraph.",
    ]
    assert _texts(drafts)[1] == "Padlock icon"  # the input list is not reordered in place


def test_nothing_moves_when_the_lead_already_comes_first() -> None:
    drafts = _drafts((B.heading, "Title"), (B.paragraph, PROSE), (B.caption, "Photo"), (B.table, "A, 1."))
    assert lead_first(drafts) == drafts


def test_a_byline_or_dateline_is_not_the_lead() -> None:
    drafts = _drafts(
        (B.paragraph, "By Jane Doe, Senior Reporter"),
        (B.paragraph, "Published 14 November 2026"),
        (B.caption, "The mayor at Tuesday's meeting"),
        (B.paragraph, PROSE),
    )
    assert _texts(lead_first(drafts)) == [
        "By Jane Doe, Senior Reporter",
        "Published 14 November 2026",
        PROSE,
        "The mayor at Tuesday's meeting",
    ]


def test_a_page_that_opens_with_a_data_table_before_a_new_section_keeps_its_order() -> None:
    drafts = _drafts(
        (B.heading, "Population by year"),
        (B.caption, "Table 1: Residents"),
        (B.table, "2020, 1,204."),
        (B.heading, "Notes"),
        (B.paragraph, PROSE),
    )
    assert lead_first(drafts) == drafts


def test_a_picture_above_the_headline_is_read_after_the_lead() -> None:
    drafts = _drafts(
        (B.figure, "Crowd outside city hall"),
        (B.caption, "Photo: the vote on Tuesday"),
        (B.heading, "Council approves new budget"),
        (B.paragraph, PROSE),
        (B.paragraph, "Second paragraph."),
    )
    assert _texts(lead_first(drafts)) == [
        "Council approves new budget",
        PROSE,
        "Crowd outside city hall",
        "Photo: the vote on Tuesday",
        "Second paragraph.",
    ]


def test_pages_without_a_body_paragraph_keep_their_order() -> None:
    drafts = _drafts((B.table, "A, 1."), (B.caption, "Table 1"), (B.list_item, "An item."), (B.paragraph, "Source"))
    assert lead_first(drafts) == drafts
    assert lead_first([]) == []


@pytest.mark.parametrize(
    ("block_type", "text", "expected"),
    [
        (B.paragraph, "Ada Lovelace was an English mathematician.", True),
        (B.paragraph, "She asked: “What is the engine for?”", True),
        (B.paragraph, "Hucknall is a market town in Nottinghamshire.[1][2]", True),
        (B.paragraph, "A long opening line with no closing punctuation that still clearly reads as body text", True),
        (B.paragraph, "By Jane Doe", False),
        (B.paragraph, "Photograph: Reuters", False),
        (B.paragraph, "   ", False),
        (B.list_item, PROSE, False),
        (B.table, PROSE, False),
    ],
)
def test_is_body_paragraph(block_type: BlockType, text: str, expected: bool) -> None:
    assert is_body_paragraph(DraftBlock(type=block_type, text=text)) is expected


# ------------------------------------------------------------------ shell preload path (posted html_blocks)


def test_posted_page_has_the_infobox_ahead_of_the_lead_in_dom_order() -> None:
    posted = _payload().blocks
    assert posted[0].type == "table" and posted[1].text.startswith(INFOBOX_CAPTION)
    assert next(i for i, b in enumerate(posted) if b.type == "paragraph") == 16


def test_posted_page_reads_the_lead_before_the_infobox() -> None:
    doc = _posted()
    blocks = doc.blocks
    assert [b.order for b in blocks] == list(range(len(blocks)))
    assert blocks[0].type == B.paragraph and blocks[0].text.startswith(LEAD)

    infobox = blocks[1:17]
    assert {b.type for b in infobox} == {B.table, B.caption}
    assert [b.id for b in infobox] == [f"blk_{n}" for n in range(1, 17)]  # the infobox keeps its own order
    assert infobox[1].type == B.caption and infobox[1].text.startswith(INFOBOX_CAPTION)

    assert blocks[17].type == B.paragraph and blocks[17].text.startswith(SECOND)
    # Nothing after the lead moves: headings, the thumbnail caption and the references stay where they were.
    assert [b.id for b in blocks[17:]] == [f"blk_{n}" for n in range(18, 29)]
    assert [(b.text, b.level) for b in blocks if b.type == B.heading] == [
        ("Biography", 2),
        ("Childhood", 3),
        ("References", 2),
    ]


def test_posted_page_keeps_ids_text_and_dom_anchors() -> None:
    """The shell maps `block_id` → the anchor it posted, and paints highlights with `dom_anchor` + offsets."""
    by_id = {b.id: b for b in _posted().blocks}
    posted = _payload().blocks
    assert set(by_id) == {b.id for b in posted}
    for hb in posted:
        block = by_id[hb.id]
        assert block.text == hb.text
        assert hb.dom_anchor is not None and block.dom_anchor == hb.dom_anchor
    assert by_id["blk_2"].dom_anchor is not None
    assert by_id["blk_2"].dom_anchor.xpath.endswith("/table[1]/tbody[1]/tr[2]/td[1]/div[1]")


async def test_posted_page_speaks_the_lead_first(tmp_path: Path) -> None:
    script = await _script(_posted(), tmp_path)
    assert script.utterances[0].text.startswith("Augusta Ada King, Countess of Lovelace")
    spoken = _spoken(script)
    # With the default reading instructions the infobox is still read, after the first paragraph and before the second.
    assert spoken.index("Augusta Ada King") < spoken.index(INFOBOX_CAPTION) < spoken.index(SECOND)
    assert spoken.index("Augusta Ada King") < spoken.index("Byron King-Noel") < spoken.index(SECOND)
    assert "[edit]" not in spoken and "edit" not in {u.text.strip(" .").lower() for u in script.utterances}


async def test_skipping_tables_and_captions_removes_the_whole_infobox(tmp_path: Path) -> None:
    doc = _posted()
    script = await _script(doc, tmp_path, tables=True, captions=True)
    spoken = _spoken(script)
    gone = ("Right Honourable", INFOBOX_CAPTION, "Marylebone", "Byron King-Noel", "Signature", "Ada Byron, aged four")
    for text in gone:
        assert text not in spoken
    assert spoken.index("Augusta Ada King") < spoken.index(SECOND)
    dropped = {h.block_id: h.rule_name for h in script.removed if h.action == "drop_block"}
    assert {dropped[f"blk_{n}"] for n in range(1, 17)} == {"Don't read tables", "Don't read captions"}
    assert dropped["blk_2"] == "Don't read captions" and dropped["blk_23"] == "Don't read captions"

    # Tables alone leaves the two captions, read after the lead.
    tables_only = _spoken(await _script(doc, tmp_path / "tables-only", tables=True))
    assert "Byron King-Noel" not in tables_only
    assert tables_only.index("Augusta Ada King") < tables_only.index(INFOBOX_CAPTION) < tables_only.index(SECOND)


# ------------------------------------------------------------------ headless path (trafilatura)


def test_headless_page_reads_the_lead_before_the_icon_and_the_infobox() -> None:
    blocks = _headless().blocks
    assert [(b.type, b.text[:19]) for b in blocks[:5]] == [
        (B.heading, "Ada Lovelace"),
        (B.paragraph, "Augusta Ada King, C"),
        (B.figure, "Page semi-protected"),
        (B.table, "The Countess of Lov"),
        (B.paragraph, "Lovelace was the on"),
    ]
    assert "Born, Augusta Ada Byron" in blocks[3].text
    assert [b.order for b in blocks] == list(range(len(blocks)))


def test_headless_page_keeps_section_headings_without_edit_links() -> None:
    blocks = _headless().blocks
    assert [b.text for b in blocks if b.type == B.heading] == ["Ada Lovelace", "Biography", "Childhood", "References"]
    assert all("[edit]" not in b.text for b in blocks)
    assert all("For the computer microarchitecture" not in b.text for b in blocks)  # the hatnote


async def test_headless_page_speaks_the_lead_first(tmp_path: Path) -> None:
    doc = _headless()
    spoken = _spoken(await _script(doc, tmp_path))
    assert spoken.index("Augusta Ada King") < spoken.index("Page semi-protected") < spoken.index("Born, Augusta")
    assert spoken.index("Born, Augusta") < spoken.index(SECOND)

    without_table = _spoken(await _script(doc, tmp_path / "tables", tables=True))
    assert "Born, Augusta" not in without_table
    assert without_table.index("Augusta Ada King") < without_table.index(SECOND)

"""Speech plan (05 § 4). Shared contract between the planner, the transport and the API.

An `Utterance` is the unit the transport plays and the audio cache keys on. Its `segments` map
ranges of the *spoken* text back to ranges of *source* block text, so a word boundary reported by a
TTS provider (or estimated) in spoken-text offsets can be turned into a highlight anchor.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from open_speech.document.model import Anchor, Fidelity


class TextSegment(BaseModel):
    """Spoken text [spoken_start, spoken_end) came from block text [src_start, src_end).

    A lexicon replacement ("SQL" → "sequel") is one segment whose spoken and source ranges differ in
    length. Text that has no source (a generated transform) has no segments; its utterance carries
    block/section anchors instead.
    """

    spoken_start: int
    spoken_end: int
    block_id: str
    src_start: int
    src_end: int


class RuleHit(BaseModel):
    """Which rule changed what. Powers Script view, the speech diff and "why didn't it read that?"."""

    rule_id: str
    rule_name: str
    action: Literal["drop_block", "skip", "replace", "transform", "keep_only"]
    block_id: str
    src_start: int | None = None
    src_end: int | None = None
    original: str = ""
    replacement: str = ""
    tier: Literal["filter", "annotate", "transform"] = "filter"


class Utterance(BaseModel):
    id: str  # "utt_<n>" — stable within a plan
    index: int
    text: str
    block_ids: list[str]
    anchors: list[Anchor] = Field(default_factory=list)
    segments: list[TextSegment] = Field(default_factory=list)
    fidelity: Fidelity = "exact"
    rule_trace: list[RuleHit] = Field(default_factory=list)
    page: int | None = None
    heading_path: list[str] = Field(default_factory=list)  # nearest headings, for "where am I"
    is_heading: bool = False
    paragraph_start: bool = False  # first utterance of a block (paragraph navigation)
    voice_ref: str | None = None  # override voice id; None = document/default voice


class SpeechScript(BaseModel):
    document_id: str
    plan_key: str
    mode: Literal["classic", "ai_enhanced"]
    utterances: list[Utterance]
    # Blocks/spans removed entirely (dropped blocks, skips) for Script view.
    removed: list[RuleHit] = Field(default_factory=list)
    annotated_upto_block: int | None = None  # AI Enhanced: highest block order annotated so far
    warnings: list[str] = Field(default_factory=list)


# The text Claude annotates shows `⟦label⟧` (e.g. `⟦url⟧`, `⟦citation⟧`) wherever a Classic filter already
# removed a span, so a sentence with a hole in it doesn't read as damage to repair. Markers are never spoken.
CUT_OPEN = "⟦"
CUT_CLOSE = "⟧"
# `SpanOp.rule_id` of an op that SPEECH.md asks for, rather than a rule file.
SPEECH_MD_RULE_ID = "speech_md"


class SpanOp(BaseModel):
    """Claude annotate output (05 § Tier 2). Quote-anchored, never numeric offsets."""

    block_id: str
    op: Literal["skip", "replace"]
    quote: str
    occurrence: int = 1
    text: str = ""
    rule_id: str | None = None

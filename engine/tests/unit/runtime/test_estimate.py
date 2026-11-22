from __future__ import annotations

import random
from decimal import Decimal

from open_speech.config import Paths
from open_speech.document.model import Block, BlockType, Document, Mark, MarkKind, SourceKind
from open_speech.plan.script import SpeechScript, TextSegment, Utterance
from open_speech.providers.base import TtsRequest
from open_speech.providers.fixture import FixtureTts
from open_speech.service.events import EventBus
from open_speech.store.db import Database
from open_speech.usage.estimate import estimate_read

from .helpers import SettingsBox
from .test_registry_synth import LOCAL, PAID, build

WORDS = [
    "the",
    "of",
    "and",
    "to",
    "in",
    "is",
    "was",
    "that",
    "for",
    "it",
    "with",
    "as",
    "his",
    "on",
    "be",
    "at",
    "by",
    "this",
    "had",
    "not",
    "are",
    "but",
    "from",
    "or",
    "have",
    "an",
    "they",
    "which",
    "one",
    "you",
    "were",
    "her",
    "all",
    "she",
    "there",
    "would",
    "their",
    "we",
    "him",
    "been",
    "has",
    "when",
    "who",
    "will",
    "more",
    "no",
    "if",
    "out",
    "so",
    "said",
    "what",
    "up",
    "its",
    "about",
    "into",
    "than",
    "them",
    "can",
    "only",
    "other",
    "new",
    "some",
    "could",
    "time",
    "these",
    "two",
    "may",
    "then",
    "do",
    "first",
    "any",
    "my",
    "now",
    "such",
    "like",
    "our",
    "over",
    "man",
    "me",
    "even",
    "most",
    "made",
    "after",
    "also",
    "did",
    "many",
    "before",
    "must",
    "through",
    "back",
    "years",
    "where",
    "much",
    "your",
    "way",
    "well",
    "down",
    "should",
    "because",
    "each",
    "just",
    "those",
    "people",
    "how",
    "too",
    "little",
    "state",
    "good",
    "very",
    "make",
    "world",
    "still",
    "own",
    "see",
    "men",
    "work",
    "long",
    "experiment",
    "analysis",
    "significant",
    "measurement",
    "temperature",
    "distribution",
    "approximately",
    "understanding",
    "particular",
]


def synthetic_document(n_paragraphs: int, seed: int) -> tuple[Document, SpeechScript]:
    rng = random.Random(seed)
    blocks: list[Block] = [
        Block(id="blk_h", type=BlockType.header, text="Journal of Synthetic Results, Vol. 3", order=0),
        Block(id="blk_t", type=BlockType.heading, text="A synthetic study", order=1, level=1),
    ]
    utterances: list[Utterance] = []
    for p in range(n_paragraphs):
        sentences = []
        for _ in range(rng.randint(2, 5)):
            words = [rng.choice(WORDS) for _ in range(rng.randint(8, 22))]
            sentences.append(" ".join(words).capitalize() + rng.choice([".", ".", ".", "?", "!"]))
        text = " ".join(sentences)
        bid = f"blk_{p}"
        blocks.append(
            Block(
                id=bid,
                type=BlockType.paragraph,
                text=text,
                order=p + 2,
                marks=[Mark(id=f"m{p}", kind=MarkKind.citation_ref, start=0, end=0)],
            )
        )
        pos = 0
        for i, s in enumerate(sentences):
            start = text.index(s, pos)
            pos = start + len(s)
            utterances.append(
                Utterance(
                    id=f"utt_{len(utterances)}",
                    index=len(utterances),
                    text=s,
                    block_ids=[bid],
                    segments=[
                        TextSegment(spoken_start=0, spoken_end=len(s), block_id=bid, src_start=start, src_end=pos)
                    ],
                    paragraph_start=i == 0,
                )
            )
    doc = Document(id="doc_syn", source_id="syn", kind=SourceKind.text, title="Synthetic", blocks=blocks)
    script = SpeechScript(document_id=doc.id, plan_key="pk", mode="classic", utterances=utterances)
    return doc, script


async def test_estimate_within_25_percent_of_actual_and_cache_is_subtracted(
    paths: Paths, db: Database, settings: SettingsBox, events: EventBus
) -> None:
    rt = build(paths, db, settings, events)
    voice = rt.registry.voice(PAID)
    tts = rt.registry.provider("fixture")
    assert tts is not None
    for seed, n in ((1, 6), (2, 15), (3, 40)):
        doc, script = synthetic_document(n, seed)
        est = estimate_read(
            doc,
            script,
            voice=voice,
            mode="classic",
            llm_model="fixture-llm",
            pricing=rt.ledger.pricing,
            audio_cache=rt.cache,
        )
        # "actual" = what the synthesizer bills: the provider's units for the rendered audio, priced
        actual = Decimal(0)
        for u in script.utterances:
            pcm, _ = FixtureTts.render(u.text, "soprano")
            req = TtsRequest(text=u.text, model="tone-paid-v1", voice="soprano")
            actual += rt.ledger.pricing.cost("fixture", "tone-paid-v1", tts.price_units(req, pcm.size / 24000))
        estimated = Decimal(est.usd_estimate)
        assert abs(estimated - actual) / actual <= Decimal("0.25"), (seed, estimated, actual)

    # cached utterances are subtracted (and the billed amount matches the per-utterance pricing)
    doc, script = synthetic_document(4, 9)
    full = Decimal(
        estimate_read(
            doc,
            script,
            voice=voice,
            mode="classic",
            llm_model="fixture-llm",
            pricing=rt.ledger.pricing,
            audio_cache=rt.cache,
        ).usd_estimate
    )
    half = script.utterances[: len(script.utterances) // 2]
    for u in half:
        await rt.synth.synth(u.text, PAID, document_id=doc.id)
    after = estimate_read(
        doc,
        script,
        voice=voice,
        mode="classic",
        llm_model="fixture-llm",
        pricing=rt.ledger.pricing,
        audio_cache=rt.cache,
    )
    assert 0 < Decimal(after.usd_estimate) < full
    assert any(f"{len(half)} of {len(script.utterances)} utterances are already cached" in a for a in after.assumptions)
    for u in script.utterances[len(half) :]:
        await rt.synth.synth(u.text, PAID, document_id=doc.id)
    done = estimate_read(
        doc,
        script,
        voice=voice,
        mode="classic",
        llm_model="fixture-llm",
        pricing=rt.ledger.pricing,
        audio_cache=rt.cache,
    )
    assert Decimal(done.usd_estimate) == 0


async def test_ai_enhanced_planning_and_confirmation_threshold(
    paths: Paths, db: Database, settings: SettingsBox, events: EventBus
) -> None:
    rt = build(paths, db, settings, events)
    doc, script = synthetic_document(400, 7)
    voice = rt.registry.voice(PAID)
    classic = estimate_read(
        doc,
        None,
        voice=voice,
        mode="classic",
        llm_model="claude-sonnet-5-5",
        pricing=rt.ledger.pricing,
        audio_cache=rt.cache,
    )
    ai = estimate_read(
        doc,
        None,
        voice=voice,
        mode="ai_enhanced",
        llm_model="claude-sonnet-5-5",
        pricing=rt.ledger.pricing,
        audio_cache=rt.cache,
        threshold_usd="0.50",
    )
    assert Decimal(ai.usd_estimate) > Decimal(classic.usd_estimate)
    planning = next(line for line in ai.breakdown if line.item.startswith("AI Enhanced planning"))
    assert Decimal(planning.usd_estimate) > 0
    assert ai.requires_confirmation
    assert any("Classic filters assumed" in a for a in classic.assumptions)
    # the header block is excluded from the no-plan estimate
    header_chars = len(doc.blocks[0].text)
    total_chars = sum(len(b.text) for b in doc.blocks)
    speech = classic.breakdown[0].item
    assert f"{total_chars - header_chars:,} characters" in speech
    # already-annotated plan: planning is free
    script.mode = "ai_enhanced"
    script.annotated_upto_block = 10**6
    planned = estimate_read(
        doc,
        script,
        voice=voice,
        mode="ai_enhanced",
        llm_model="claude-sonnet-5-5",
        pricing=rt.ledger.pricing,
        audio_cache=rt.cache,
    )
    assert any(line.item == "AI Enhanced planning: already planned" for line in planned.breakdown)


async def test_local_voice_and_images(paths: Paths, db: Database, settings: SettingsBox, events: EventBus) -> None:
    rt = build(paths, db, settings, events)
    doc, script = synthetic_document(5, 3)
    est = estimate_read(
        doc,
        script,
        voice=rt.registry.voice(LOCAL),
        mode="classic",
        llm_model="claude-sonnet-5-5",
        pricing=rt.ledger.pricing,
        audio_cache=rt.cache,
        images=2,
    )
    lines = {line.item.split(":")[0]: Decimal(line.usd_estimate) for line in est.breakdown}
    assert lines["Speech"] == 0 and lines["Image descriptions"] > 0
    assert not est.requires_confirmation
    assert abs(lines["Image descriptions"] / 2 - Decimal("0.005")) < Decimal("0.0025")  # ≈ $0.005 per image

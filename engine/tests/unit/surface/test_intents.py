"""Fast-path grammar (07 § Fast path, FR-BAR-11)."""

from __future__ import annotations

import time

import pytest

from open_speech.agent.intents import parse, parse_number

CASES: list[tuple[str, str | None, dict[str, object]]] = [
    ("pause", "pause", {}),
    ("Pause.", "pause", {}),
    ("please pause reading", "pause", {}),
    ("play", "play", {}),
    ("resume", "resume", {}),
    ("continue", "resume", {}),
    ("stop", "stop", {}),
    ("faster", "change_speed", {"delta": 0.25}),
    ("a bit slower", "change_speed", {"delta": -0.25}),
    ("2x", "set_speed", {"speed": 2.0}),
    ("speed 2", "set_speed", {"speed": 2.0}),
    ("speed one point five", "set_speed", {"speed": 1.5}),
    ("set the speed to 1.8", "set_speed", {"speed": 1.8}),
    ("read at two and a half times", "set_speed", {"speed": 2.5}),
    ("skip", "skip", {"unit": "sentence", "count": 1}),
    ("next", "skip", {"unit": "sentence", "count": 1}),
    ("back", "skip", {"unit": "sentence", "count": -1}),
    ("next paragraph", "skip", {"unit": "paragraph", "count": 1}),
    ("back a paragraph", "skip", {"unit": "paragraph", "count": -1}),
    ("next heading", "skip", {"unit": "heading", "count": 1}),
    ("back 10 seconds", "skip", {"unit": "seconds", "count": -10}),
    ("skip ahead thirty seconds", "skip", {"unit": "seconds", "count": 30}),
    ("go to page 12", "go_to_page", {"page": 12}),
    ("go to page twelve", "go_to_page", {"page": 12}),
    ("read clipboard", "read_clipboard", {"image_mode": "auto"}),
    ("Read my clipboard", "read_clipboard", {"image_mode": "auto"}),
    ("describe what's on my clipboard", "read_clipboard", {"image_mode": "describe"}),
    ("where am I", "where_am_i", {}),
    ("how much is left", "where_am_i", {}),
    ("say SQL as sequel", "lexicon", {"term": "SQL", "spoken": "sequel"}),
    ("Say kubectl as cube control.", "lexicon", {"term": "kubectl", "spoken": "cube control"}),
    ("skip footnotes", "preset", {"preset": "Footnotes", "enabled": True}),
    ("stop reading the citations", "preset", {"preset": "Citations", "enabled": True}),
    ("read page numbers again", "preset", {"preset": "Page numbers", "enabled": False}),
    ("AI enhanced on", "set_mode", {"mode": "ai_enhanced"}),
    ("classic mode", "set_mode", {"mode": "classic"}),
    ("how much have I spent this month", "usage", {"period": "month"}),
    ("how much have I spent today", "usage", {"period": "today"}),
    ("how much have I spent", "usage", {"period": "month"}),
    ("show voices", "show", {"what": "voices"}),
    ("show me my rules", "show", {"what": "rules"}),
    ("say that again", "repeat", {}),
    ("clear cache", "clear_cache", {}),
    ("use the coral voice", "set_voice_by_name", {"name": "coral", "scope": "document"}),
    ("read https://example.org/article", "open_source", {"target": "https://example.org/article", "play": True}),
    # Goes to Claude:
    ("summarize the rest of this chapter", None, {}),
    ("use a calmer voice, a bit faster", None, {}),
    ("describe figures in one sentence", None, {}),
]


@pytest.mark.parametrize(("text", "kind", "params"), CASES)
def test_grammar(text: str, kind: str | None, params: dict[str, object]) -> None:
    intent = parse(text)
    if kind is None:
        assert intent is None, f"{text!r} should go to Claude, got {intent}"
        return
    assert intent is not None, f"{text!r} not recognised"
    assert intent.kind == kind
    for k, v in params.items():
        assert intent.params.get(k) == v, (text, intent)


def test_scoped_skip_becomes_rule_proposal() -> None:
    i = parse("stop reading the footnotes on this site")
    assert i is not None and i.kind == "propose_rule"
    assert i.params["scope_hint"] == "site"
    i = parse("skip citations for this paper")
    assert i is not None and i.kind == "propose_rule" and i.params["scope_hint"] == "document"


def test_confirmations_only_with_pending_action() -> None:
    assert parse("yes") is None
    assert parse("yes", has_pending_action=True).kind == "confirm"  # type: ignore[union-attr]
    assert parse("do it", has_pending_action=True).kind == "confirm"  # type: ignore[union-attr]
    assert parse("no", has_pending_action=True).kind == "cancel"  # type: ignore[union-attr]
    # "stop" while a card is pending cancels the card rather than stopping playback
    assert parse("stop", has_pending_action=True).kind == "cancel"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("words", "value"),
    [
        ("1.5", 1.5),
        ("one point five", 1.5),
        ("two and a half", 2.5),
        ("twenty one", 21),
        ("twelve", 12),
        ("a hundred", 100),
    ],
)
def test_numbers(words: str, value: float) -> None:
    assert parse_number(words) == value


def test_fast_path_is_fast() -> None:
    start = time.perf_counter()
    for _ in range(200):
        for text, _, _ in CASES:
            parse(text)
    per_call_ms = (time.perf_counter() - start) * 1000 / (200 * len(CASES))
    assert per_call_ms < 5  # budget is 100 ms end to end; the parser is a rounding error

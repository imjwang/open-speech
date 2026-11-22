"""P0 flows end to end through the public API (10 § Testing: end-to-end; NFR-5 voice parity)."""

from __future__ import annotations

import base64
import io
import json
import struct
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from .conftest import WsReader, turn, wait_state, ws_connect

TEXT = (
    "Transformers dominate sequence modeling [12]. "
    "As shown by Smith et al. (2020), attention scales quadratically. "
    "SQL databases remain popular. "
    "This is the final sentence of the test document."
)

ARXIV_BLOCKS = {
    "url": "https://arxiv.org/abs/1706.03762",
    "title": "Attention Is All You Need",
    "blocks": [
        {"id": "h1", "type": "heading", "level": 1, "text": "Attention Is All You Need",
         "dom_anchor": {"xpath": "/html/body/h1[1]", "start_offset": 0}},
        {"id": "p1", "type": "paragraph", "text": "The dominant sequence transduction models are based on recurrent networks [1].",
         "dom_anchor": {"xpath": "/html/body/p[1]", "start_offset": 0}},
        {"id": "f1", "type": "footnote", "text": "1 See appendix B for the full derivation.",
         "dom_anchor": {"xpath": "/html/body/aside[1]", "start_offset": 0}},
        {"id": "p2", "type": "paragraph", "text": "We propose a new simple network architecture, the Transformer.",
         "dom_anchor": {"xpath": "/html/body/p[2]", "start_offset": 0}},
        {"id": "f2", "type": "footnote", "text": "2 Equal contribution.",
         "dom_anchor": {"xpath": "/html/body/aside[2]", "start_offset": 0}},
    ],
}


def _create_text(client: TestClient, text: str = TEXT, play: bool = False) -> dict[str, Any]:
    r = client.post("/api/v1/documents", json={"kind": "text", "text": text, "title": "Test doc", "play": play})
    assert r.status_code == 200, r.text
    return r.json()


def _script_text(client: TestClient, doc_id: str) -> str:
    return " ".join(u["text"] for u in client.get(f"/api/v1/documents/{doc_id}/script").json()["utterances"])


# ------------------------------------------------------------------ M0: speaks; ledger; replay is free


def test_read_text_speaks_with_word_events_and_ledger(api: tuple[Any, TestClient]) -> None:
    engine, client = api
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        reader.of_type("hello")
        client.put("/api/v1/playback/speed", json={"speed": 5.0})
        res = _create_text(client, play=True)
        doc_id = res["document"]["id"]
        assert res["playing"] is True
        first = reader.of_type("playback.word", document_id=doc_id)
        assert first["anchor"]["block_id"]
        assert first["fidelity"] in ("exact", "block")
        wait_state(client, {"stopped", "idle"}, timeout=30)
        words = [m for m in reader.drain() + reader.seen if m.get("type") == "playback.word"]
        assert len(words) >= 10
    rows = client.get("/api/v1/usage/ledger").json()["items"]
    tts = [r for r in rows if r["op"] == "tts"]
    assert tts, "every synthesis is in the ledger"
    assert all(r["usd_estimate"] == "0" or r["local"] for r in tts)


def test_replay_makes_zero_provider_calls(api: tuple[Any, TestClient]) -> None:
    engine, client = api
    client.put("/api/v1/playback/speed", json={"speed": 5.0})
    doc = _create_text(client, play=True)["document"]
    wait_state(client, {"stopped", "idle"}, timeout=30)
    before = client.get("/api/v1/usage/ledger").json()["items"]
    synth_before = sum(1 for r in before if r["op"] == "tts" and not r["cache_hit"])
    client.post("/api/v1/playback/play", json={"document_id": doc["id"], "from": {"utterance_index": 0}})
    wait_state(client, {"stopped", "idle"}, timeout=30)
    after = client.get("/api/v1/usage/ledger").json()["items"]
    synth_after = sum(1 for r in after if r["op"] == "tts" and not r["cache_hit"])
    hits = [r for r in after if r["op"] == "tts" and r["cache_hit"]]
    assert synth_after == synth_before, "replay synthesized again"
    assert hits, "cache hits are logged at $0"
    assert all(r["usd_estimate"] == "0" for r in hits)


# ------------------------------------------------------------------ transport by typed and spoken commands


def test_fast_path_transport_commands(api: tuple[Any, TestClient]) -> None:
    engine, client = api
    long_text = " ".join(f"Sentence number {i} of a longer passage for navigation." for i in range(1, 40))
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        _create_text(client, long_text, play=True)
        wait_state(client, {"playing"})
        t0 = time.perf_counter()
        r = turn(client, reader, "pause")
        assert r["done"]["handled_by"] == "fast_path"
        assert time.perf_counter() - t0 < 2.0
        assert wait_state(client, {"paused"})["state"] == "paused"
        turn(client, reader, "resume")
        wait_state(client, {"playing"})
        r = turn(client, reader, "2x")
        assert client.get("/api/v1/playback").json()["speed"] == 2.0
        turn(client, reader, "faster")
        assert client.get("/api/v1/playback").json()["speed"] == 2.25
        idx = client.get("/api/v1/playback").json()["utterance_index"]
        turn(client, reader, "next paragraph")
        turn(client, reader, "skip")
        assert client.get("/api/v1/playback").json()["utterance_index"] > idx
        r = turn(client, reader, "where am I", source="spoken")
        assert "left" in r["done"]["text"] or "percent" in r["done"]["text"]
        assert r["cards"] and r["cards"][0]["kind"] == "where"
        assert r["done"]["reply_recording_id"], "spoken turns get a spoken, replayable reply"
        turn(client, reader, "stop")
        assert wait_state(client, {"stopped", "idle"})


def test_read_clipboard_by_voice(api: tuple[Any, TestClient], home: Path) -> None:
    """Voice-only: mic PCM over WS → STT partials → final → fast path → reading (FR-BAR-2, FR-READ-5)."""
    from open_speech.providers.fixture import FixtureStt

    engine, client = api
    (home / "clipboard.txt").write_text("Copied paragraph from another app. It should be read aloud.")
    FixtureStt.enqueue("read my clipboard")
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        reader.of_type("hello")
        ws.send_json({"type": "bar.state", "open": True})
        ws.send_json({"type": "mic.start", "session_id": "s1", "mode": "auto"})
        silence = struct.pack("<" + "h" * 1600, *([0] * 1600))
        for _ in range(10):
            ws.send_bytes(silence)
        partial = reader.of_type("stt.partial", session_id="s1")
        assert partial["text"]
        ws.send_json({"type": "mic.stop", "session_id": "s1"})
        final = reader.of_type("stt.final", session_id="s1")
        assert final["text"] == "read my clipboard"
        assert final["recording_id"], "the user's utterance is stored for replay (FR-BAR-6)"
        turn_msg = reader.of_type("assistant.turn")
        assert turn_msg["source"] == "spoken"
        done = reader.of_type("assistant.done", turn_id=turn_msg["turn_id"])
        assert done["handled_by"] == "fast_path"
        ws.send_json({"type": "bar.state", "open": False})
        st = wait_state(client, {"playing"})
        assert st["document_title"] == "Clipboard"
    rec = client.get(f"/api/v1/recordings/{final['recording_id']}")
    assert rec.status_code == 200 and rec.headers["content-type"] == "audio/ogg"


def test_mic_cancel_does_not_submit(api: tuple[Any, TestClient]) -> None:
    """FR-BAR-3: typing cancels the mic session; FR-BAR-4: each path submits at most once."""
    from open_speech.providers.fixture import FixtureStt

    engine, client = api
    FixtureStt.enqueue("pause")
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        ws.send_json({"type": "mic.start", "session_id": "s2"})
        ws.send_bytes(b"\x00\x00" * 3200)
        ws.send_json({"type": "mic.stop", "session_id": "s2", "submit": False})
        ws.send_json({"type": "mic.stop", "session_id": "s2"})  # duplicate stop is ignored
        time.sleep(0.5)
        assert not [m for m in reader.drain() if m["type"] in ("assistant.turn", "stt.final")]


# ------------------------------------------------------------------ rules by voice (M3 exit criterion)


def test_stop_reading_footnotes_on_this_site(api: tuple[Any, TestClient], home: Path) -> None:
    engine, client = api
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        res = client.post("/api/v1/documents", json={"kind": "html_blocks", "html_blocks": ARXIV_BLOCKS, "play": True})
        doc_id = res.json()["document"]["id"]
        assert res.json()["document"]["site"] == "arxiv.org"
        wait_state(client, {"playing"})
        assert "appendix B" in _script_text(client, doc_id)
        r = turn(client, reader, "stop reading the footnotes on this site", source="spoken")
        assert r["actions"], "a confirmation card is shown"
        action = r["actions"][0]
        assert action["preview"]["removals"], "the card carries a speech diff"
        assert "arxiv.org" in r["done"]["text"] and "?" in r["done"]["text"]
        assert not list((home / "rules").glob("*footnote*arxiv*")), "nothing is written before confirmation"
        r2 = turn(client, reader, "yes", source="spoken")
        assert r2["done"]["handled_by"] == "fast_path"
        reader.of_type("document.updated", document_id=doc_id)
    assert "appendix B" not in _script_text(client, doc_id)
    rules = client.get("/api/v1/rules").json()["items"]
    site_rules = [x for x in rules if "arxiv.org" in x["scope"]["sites"]]
    assert site_rules and site_rules[0]["tier"] == "filter"
    assert Path(site_rules[0]["path"]).exists()


def test_cancelling_a_rule_leaves_no_trace(api: tuple[Any, TestClient], home: Path) -> None:
    engine, client = api
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        client.post("/api/v1/documents", json={"kind": "html_blocks", "html_blocks": ARXIV_BLOCKS, "play": True})
        before = sorted(p.name for p in (home / "rules").glob("*.md"))
        turn(client, reader, "stop reading the footnotes on this site")
        turn(client, reader, "no")
    assert sorted(p.name for p in (home / "rules").glob("*.md")) == before
    assert client.get("/api/v1/actions").json() == []


def test_preset_toggle_and_lexicon(api: tuple[Any, TestClient]) -> None:
    engine, client = api
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        doc_id = _create_text(client, play=True)["document"]["id"]
        assert "[12]" in _script_text(client, doc_id) or "12" in _script_text(client, doc_id)
        r = turn(client, reader, "skip citations")
        assert r["actions"] and r["actions"][0]["kind"] == "set_preset"
        turn(client, reader, "yes")
        assert "[12]" not in _script_text(client, doc_id)
        presets = {p["name"]: p["enabled"] for p in client.get("/api/v1/rules/presets").json()["presets"]}
        assert presets["Citations"] is True
        r = turn(client, reader, "say SQL as sequel")
        assert r["actions"] and r["actions"][0]["kind"] == "add_pronunciation"
        turn(client, reader, "do it")
    lex = client.get("/api/v1/rules/lexicon").json()["items"]
    assert {"term": "SQL", "spoken": "sequel", "scope": ""} in lex
    assert "sequel databases" in _script_text(client, doc_id)


# ------------------------------------------------------------------ usage and cost (FR-ASST-6, FR-COST-2)


def test_usage_question(api: tuple[Any, TestClient]) -> None:
    engine, client = api
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        r = turn(client, reader, "how much have I spent this month")
        assert r["done"]["handled_by"] == "fast_path"
        assert r["cards"] and r["cards"][0]["kind"] == "usage"
        assert "estimated" in r["cards"][0]["title"].lower()
        assert r["done"]["text"]
    summary = client.get("/api/v1/usage/summary?period=month&group_by=provider").json()
    assert summary["estimated"] is True
    assert isinstance(summary["total_usd"], str)


def test_budget_round_trip(api: tuple[Any, TestClient]) -> None:
    engine, client = api
    b = client.put("/api/v1/usage/budget", json={"monthly_usd": "20"}).json()
    assert b["monthly_usd"] == "20"
    assert client.get("/api/v1/settings").json()["budget"]["monthly_usd"] == "20"


# ------------------------------------------------------------------ images (FR-MM-1)


def test_image_is_described(api: tuple[Any, TestClient]) -> None:
    from PIL import Image

    engine, client = api
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 30, 30)).save(buf, format="PNG")
    r = client.post(
        "/api/v1/documents",
        json={"kind": "image", "image_base64": base64.b64encode(buf.getvalue()).decode(), "media_type": "image/png", "image_mode": "describe"},
    )
    assert r.status_code == 200, r.text
    doc = r.json()["document"]
    assert doc["kind"] == "image"
    assert _script_text(client, doc["id"]).strip()
    vision = [x for x in client.get("/api/v1/usage/ledger").json()["items"] if x["op"] == "vision"]
    assert vision, "vision calls are logged"


# ------------------------------------------------------------------ errors are explained (FR-ASST-7, FR-SETUP-7)


def test_agent_without_key_explains_next_step(api: tuple[Any, TestClient]) -> None:
    engine, client = api
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        r = turn(client, reader, "summarize the rest of this chapter")
        assert r["done"]["handled_by"] == "error"
        assert "Anthropic key" in r["done"]["text"]
        assert r["done"]["error"]["code"] == "missing_key"


def test_keys_are_write_only(api: tuple[Any, TestClient]) -> None:
    engine, client = api
    st = client.get("/api/v1/keys").json()
    assert set(st) == {"anthropic", "openai", "gemini"}
    assert client.get("/api/v1/keys/openai").status_code in (404, 405)
    r = client.put("/api/v1/keys/auto", json={"key": "not-a-key"}).json()
    assert r["ok"] is False and "recognise" in r["detail"]


def test_recents_and_search(api: tuple[Any, TestClient]) -> None:
    engine, client = api
    _create_text(client, "Bananas are botanically berries. Strawberries are not.")
    _create_text(client, "Quantum entanglement links particles across distance.")
    items = client.get("/api/v1/documents").json()["items"]
    assert len(items) == 2
    found = client.get("/api/v1/documents?q=entanglement").json()["items"]
    assert len(found) == 1 and "Quantum" in json.dumps(found[0]) or found[0]["title"]


def test_resume_position_survives_restart(engine_factory: Any) -> None:
    engine, client = engine_factory()
    long_text = " ".join(f"Line {i} of the resume test document." for i in range(1, 30))
    doc_id = _create_text(client, long_text, play=True)["document"]["id"]
    wait_state(client, {"playing"})
    client.post("/api/v1/playback/skip", json={"unit": "sentence", "count": 5})
    idx = client.get("/api/v1/playback").json()["utterance_index"]
    client.post("/api/v1/playback/pause")
    client.__exit__(None, None, None)
    engine2, client2 = engine_factory()
    doc = client2.get(f"/api/v1/documents/{doc_id}").json()
    assert doc["position"]["utterance_index"] == idx
    st = client2.post("/api/v1/playback/play", json={"document_id": doc_id}).json()
    assert st["utterance_index"] == idx


@pytest.mark.parametrize("cmd", ["show voices", "show rules", "show recents", "show presets", "show keys"])
def test_rows_not_screens(api: tuple[Any, TestClient], cmd: str) -> None:
    engine, client = api
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        r = turn(client, reader, cmd)
        assert r["cards"], f"{cmd} renders as rows"

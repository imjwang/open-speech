"""Every reply is heard (07 § Reply speech): typed, spoken and button turns alike, errors included, unless the
answer is reading audio that resumes when the conversation ends. Driven through the public API, as the bar does."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from .conftest import WsReader, turn, wait_state, ws_connect

LONG = " ".join(f"Sentence number {i} of a longer passage for navigation." for i in range(1, 40))


def ended(reader: WsReader, turn_id: str, timeout: float = 15.0) -> dict[str, Any]:
    return reader.of_type("assistant.speech", timeout=timeout, state="ended", turn_id=turn_id)


def speech_for(reader: WsReader, turn_id: str, wait: float = 0.6) -> list[dict[str, Any]]:
    time.sleep(wait)
    reader.drain()
    return [m for m in reader.seen if m.get("type") == "assistant.speech" and m.get("turn_id") == turn_id]


def _play(client: TestClient, text: str = LONG) -> None:
    r = client.post("/api/v1/documents", json={"kind": "text", "text": text, "title": "Doc", "play": True})
    assert r.status_code == 200, r.text
    wait_state(client, {"playing"})


def test_a_typed_turn_is_spoken_and_kept_for_replay(api: tuple[Any, TestClient]) -> None:
    engine, client = api
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        r = turn(client, reader, "where am I")
        done = r["done"]
        assert done["speaks"] is True and done["audible_effect"] is False
        assert "reply_recording_id" not in done  # it comes with the end of the speech now
        started = reader.of_type("assistant.speech", state="started", turn_id=done["turn_id"])
        end = ended(reader, done["turn_id"])
        assert started["kind"] == end["kind"] == "reply" and end["reason"] == "finished"
        rec = end["recording_id"]
        assert client.get(f"/api/v1/recordings/{rec}").status_code == 200
        row = engine.db.one("SELECT reply_recording_id FROM turns WHERE id=?", (done["turn_id"],))
        assert row["reply_recording_id"] == rec

        again = turn(client, reader, "say that again")["done"]  # the same audio, not a new synthesis
        assert again["speaks"] is True and again["text"] == done["text"]
        assert ended(reader, again["turn_id"])["recording_id"] == rec


def test_a_button_turn_says_its_summary(api: tuple[Any, TestClient]) -> None:
    _, client = api
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        r = turn(client, reader, "show settings", source="button")
        assert r["cards"] and r["done"]["speaks"] is True
        assert ended(reader, r["done"]["turn_id"])["reason"] == "finished"


def test_faster_while_reading_is_heard_as_reading_not_words(api: tuple[Any, TestClient]) -> None:
    _, client = api
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        _play(client)
        ws.send_json({"type": "bar.state", "open": True})  # a conversation: reading waits for it
        wait_state(client, {"paused"})
        r = turn(client, reader, "faster")
        assert r["done"]["speaks"] is False and r["done"]["audible_effect"] is True
        assert r["done"]["text"]  # still shown
        assert speech_for(reader, r["done"]["turn_id"]) == []
        ws.send_json({"type": "bar.state", "open": False})  # the bar ends the conversation: reading resumes
        assert wait_state(client, {"playing"})["speed"] == 1.25


def test_faster_with_nothing_playing_is_said(api: tuple[Any, TestClient]) -> None:
    _, client = api
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        r = turn(client, reader, "faster")
        assert r["done"]["speaks"] is True and r["done"]["audible_effect"] is False
        ended(reader, r["done"]["turn_id"])


def test_pause_stop_and_cancel_are_said(api: tuple[Any, TestClient]) -> None:
    _, client = api
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        _play(client)
        pause = turn(client, reader, "pause")["done"]
        assert pause["speaks"] is True and pause["text"] == "Paused."
        ended(reader, pause["turn_id"])
        card = turn(client, reader, "say SQL as sequel")
        assert card["actions"] and card["done"]["speaks"] is True
        no = turn(client, reader, "no")["done"]
        assert no["speaks"] is True and no["text"] == "Cancelled."
        stop = turn(client, reader, "stop")["done"]
        assert stop["speaks"] is True and stop["text"] == "Stopped."


def test_an_error_is_said(api: tuple[Any, TestClient]) -> None:
    _, client = api
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        r = turn(client, reader, "next paragraph")  # nothing is open
        assert r["done"]["handled_by"] == "error" and r["done"]["error"]
        assert r["done"]["speaks"] is True
        assert ended(reader, r["done"]["turn_id"])["reason"] == "finished"


def test_reading_something_new_is_the_answer(api: tuple[Any, TestClient], home: Path) -> None:
    _, client = api
    (home / "clipboard.txt").write_text("Copied paragraph from another app. It should be read aloud.")
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        ws.send_json({"type": "bar.state", "open": True})
        r = turn(client, reader, "read my clipboard")
        assert r["done"]["speaks"] is False and r["done"]["audible_effect"] is True
        assert r["done"]["text"] == "Reading your clipboard."
        ws.send_json({"type": "bar.state", "open": False})
        assert wait_state(client, {"playing"})["document_title"] == "Clipboard"


def test_say_returns_once_heard(api: tuple[Any, TestClient]) -> None:
    _, client = api
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        r = client.post("/api/v1/assistant/say", json={"text": "Hi, I'm listening."})
        assert r.status_code == 204, r.text
        kinds = [(m["state"], m["kind"]) for m in reader.drain() if m.get("type") == "assistant.speech"]
        assert kinds == [("started", "say"), ("ended", "say")]


def test_speech_stop_over_rest_and_websocket(api: tuple[Any, TestClient]) -> None:
    _, client = api
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        r = turn(client, reader, "where am I")
        reader.of_type("assistant.speech", state="started", turn_id=r["done"]["turn_id"])
        ws.send_json({"type": "speech.stop", "turn_id": r["done"]["turn_id"]})
        assert ended(reader, r["done"]["turn_id"])["reason"] == "stopped"
        r2 = turn(client, reader, "where am I")
        reader.of_type("assistant.speech", state="started", turn_id=r2["done"]["turn_id"])
        assert client.post("/api/v1/assistant/speech/stop", json={}).status_code == 204
        assert ended(reader, r2["done"]["turn_id"])["reason"] == "stopped"


# ------------------------------------------------------------------ the agent's words, as they are written


class StreamingRunner:
    """Stands in for Claude: writes its reply in pieces, with a pause (a slow model, or a tool) in between."""

    def __init__(self, pieces: list[str], pause_s: float = 0.5, script: list[tuple[str, dict[str, Any]]] = ()) -> None:  # type: ignore[assignment]
        self.pieces = pieces
        self.pause_s = pause_s
        self.script = list(script)

    async def run(self, *, system: str, user: str, tools: Any, model: str, effort: str, on_delta: Any) -> Any:
        from open_speech.providers.anthropic_agent import AgentResult
        from open_speech.providers.base import Units

        by_name = {t.__name__: t for t in tools}
        for name, args in self.script:
            await by_name[name](**args)
        for i, piece in enumerate(self.pieces):
            if i:
                await asyncio.sleep(self.pause_s)
            on_delta(piece)
        return AgentResult(text="".join(self.pieces).strip(), units=Units(in_tok=900, out_tok=40), model=model,
                           tool_calls=[n for n, _ in self.script])


def test_the_agents_first_sentence_is_heard_before_it_has_finished(engine_factory: Any) -> None:
    pieces = ["Here is the first sentence of the answer. ", "And here is the rest of it, a moment later."]
    engine, client = engine_factory(agent_runner_factory=lambda: StreamingRunner(pieces))
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        r = turn(client, reader, "tell me something about this")
        done = r["done"]
        assert done["handled_by"] == "agent" and done["speaks"] is True and done["audible_effect"] is False
        order = [m["type"] for m in reader.seen if m.get("turn_id") == done["turn_id"]]
        assert order.index("assistant.speech") < order.index("assistant.done")  # heard while still being written
        end = ended(reader, done["turn_id"])
        assert end["reason"] == "finished"
        texts = [u.text for u in engine.reply_speech.current._units]
        assert texts == ["Here is the first sentence of the answer.", "And here is the rest of it, a moment later."]


def test_an_agent_turn_that_changes_reading_gives_way_to_it(engine_factory: Any) -> None:
    engine, client = engine_factory(agent_runner_factory=lambda: StreamingRunner(
        ["Reading at one and a half."], script=[("set_speed", {"speed": 1.5})]))
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        _play(client)
        ws.send_json({"type": "bar.state", "open": True})
        done = turn(client, reader, "make it a bit quicker please, like one and a half")["done"]
        assert done["handled_by"] == "agent"
        assert done["speaks"] is True and done["audible_effect"] is True  # said, then the conversation ends
        ended(reader, done["turn_id"])

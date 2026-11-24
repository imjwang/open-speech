"""Cloned and designed voices through the public API and the conversation (08 § Voice cloning): consent, the passage
check, a file's transcript, the check synthesis and its grade, rename / new text / delete, designed candidates, "clone
my voice" → "Keep it?" → "yes", the download offer, "clone this voice" with an attached file, and the CLI writing a
sample to a file. Qwen3-TTS runs in-process on its fake model (a tone; no MLX, no weights), the speech recognizer is a
stand-in that says what the test tells it to."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from open_speech.voices import passages as P
from open_speech.voices import reference as R
from tests.speechlike import phrases, wav_bytes

from .conftest import WsReader, turn, ws_connect

BASE = "qwen3-tts-0.6b-base-8bit"
DESIGN = "qwen3-tts-1.7b-voicedesign-8bit"


class Heard:
    """The on-device recognizer, as the test wants it: queued answers first, then what it was asked to check."""

    def __init__(self) -> None:
        self.queue: list[str | None] = []
        self.calls = 0
        self.default: str | None = None

    async def __call__(self, x16: np.ndarray) -> str | None:
        self.calls += 1
        if self.queue:
            return self.queue.pop(0)
        return self.default


def fake_weights(engine: Any, *ids: str) -> None:
    for vid in ids:
        v = engine.catalog.weights_variant(vid)
        for rel in v.required_files():
            f = engine.paths.models / "qwen3" / vid / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"")


@pytest.fixture
def cloning(engine_factory: Callable[..., tuple[Any, TestClient]]) -> tuple[Any, TestClient, Heard]:
    from open_speech.providers import qwen3_worker as qw
    from open_speech.providers.qwen3 import InProcessBackend, Qwen3Tts

    engine, client = engine_factory()

    def make(catalog: Any, paths: Any, **kw: Any) -> Qwen3Tts:
        return Qwen3Tts(catalog, paths, backend_factory=lambda: InProcessBackend(qw.FakeModel), memory_gb=lambda: 16.0,
                        platform_ok=lambda: True, bundled_models=paths.home / "none", **kw)

    engine.registry._f.qwen3 = make
    engine.registry._cache.pop("tts:qwen3", None)
    fake_weights(engine, BASE, DESIGN)
    heard = Heard()
    engine.clones.transcriber = heard
    return engine, client, heard


def reading() -> bytes:
    return wav_bytes(phrases([4.0, 4.0]))


def wait_job(client: TestClient, job_id: str, timeout: float = 30.0) -> dict[str, Any]:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] != "running":
            return job  # type: ignore[no-any-return]
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished")


def clone(client: TestClient, heard: Heard, *, passage: str | None = "en-1", said: str | None = None,
          name: str = "My voice", **form: Any) -> dict[str, Any]:
    text = P.passage(passage).text if passage else "hello there this is my sample voice"  # type: ignore[union-attr]
    heard.queue = [said if said is not None else text]
    heard.default = P.check_text("en")
    data = {"name": name, "consent": "true", **({"passage_id": passage, "source": "recording"} if passage else {}),
            **form}
    r = client.post("/api/v1/voices/clones", files={"file": ("take.wav", reading(), "audio/wav")}, data=data)
    assert r.status_code == 202, r.text
    return wait_job(client, r.json()["job_id"])


# ------------------------------------------------------------------ the API


def test_consent_is_required(cloning: tuple[Any, TestClient, Heard]) -> None:
    engine, client, _ = cloning
    for data in ({"name": "x"}, {"name": "x", "consent": "false"}):
        r = client.post("/api/v1/voices/clones", files={"file": ("take.wav", reading())}, data=data)
        assert r.status_code == 422 and r.json()["code"] == "invalid_request"
        assert "permission" in r.json()["detail"]
    assert not any(p.is_dir() and not p.name.startswith(".") for p in engine.paths.voices.iterdir())


def test_a_read_passage_becomes_a_voice(cloning: tuple[Any, TestClient, Heard]) -> None:
    engine, client, _ = cloning
    job = clone(client, cloning[2])
    assert job["state"] == "done", job
    res = job["result"]
    assert res["voice_id"] == "qwen3:clone:my-voice" and res["kept"]
    assert res["ref_text"] == P.passage("en-1").text  # type: ignore[union-attr]
    assert res["check"]["grade"] == "good" and res["check"]["wer"] == 0 and res["check"]["recording_id"]
    folder = engine.paths.voices / "my-voice"
    meta = json.loads((folder / "voice.json").read_text())
    assert meta["source"] == "recording" and meta["consent"]["text_version"] == P.CONSENT_VERSION
    assert meta["processing_version"] == R.PROCESSING_VERSION and len(meta["ref_sha256"]) == 64
    assert (folder / "source.wav").exists()
    info = sf.info(str(folder / "reference.wav"))
    assert info.samplerate == 24000 and info.channels == 1 and 6.0 <= info.duration <= 11.5
    listed = client.get("/api/v1/voices/clones").json()["items"]
    assert [c["slug"] for c in listed] == ["my-voice"] and listed[0]["check"]["grade"] == "good"
    voices = {v["id"]: v for v in client.get("/api/v1/voices").json()["items"]}
    assert voices["qwen3:clone:my-voice"]["available"]
    preview = client.post("/api/v1/voices/qwen3:clone:my-voice/preview", params={"play": "false", "text": "Hi."})
    assert preview.status_code == 200 and preview.json()["recording_id"]


def test_a_recording_that_doesnt_say_the_passage_is_turned_down(cloning: tuple[Any, TestClient, Heard]) -> None:
    engine, client, heard = cloning
    job = clone(client, heard, said="the quick brown fox jumps over the lazy dog")
    assert job["state"] == "failed" and "doesn't match the passage" in job["error"]["detail"]
    assert not (engine.paths.voices / "my-voice").exists()


def test_a_files_transcript_is_its_text(cloning: tuple[Any, TestClient, Heard]) -> None:
    _, client, heard = cloning
    job = clone(client, heard, passage=None, name="Grandpa")
    assert job["state"] == "done", job
    assert job["result"]["ref_text"] == "hello there this is my sample voice"
    # Typed text wins over the transcript.
    heard.queue = ["wrong words"]
    r = client.post("/api/v1/voices/clones", files={"file": ("dad.m4a.wav", reading())},
                    data={"name": "Dad", "consent": "true", "ref_text": "Exactly what he says."})
    done = wait_job(client, r.json()["job_id"])
    assert done["result"]["ref_text"] == "Exactly what he says."


def test_without_a_recognizer_a_file_needs_typed_text(cloning: tuple[Any, TestClient, Heard]) -> None:
    _, client, heard = cloning
    heard.queue = [None]
    r = client.post("/api/v1/voices/clones", files={"file": ("x.wav", reading())}, data={"consent": "true"})
    job = wait_job(client, r.json()["job_id"])
    assert job["state"] == "failed" and "Type what is said" in job["error"]["detail"]


def test_rename_keeps_the_hash_new_text_rehashes(cloning: tuple[Any, TestClient, Heard]) -> None:
    engine, client, heard = cloning
    clone(client, heard)
    before = client.get("/api/v1/voices/clones").json()["items"][0]
    renamed = client.patch("/api/v1/voices/clones/my-voice", json={"name": "Me"}).json()
    assert renamed["name"] == "Me" and renamed["ref_sha256"] == before["ref_sha256"]
    client.post("/api/v1/voices/qwen3:clone:my-voice/preview", params={"play": "false"})
    assert _cached(engine, "my-voice") >= 1
    changed = client.patch("/api/v1/voices/clones/my-voice", json={"ref_text": "A corrected transcript."}).json()
    assert changed["ref_text"] == "A corrected transcript." and changed["ref_sha256"] != before["ref_sha256"]
    assert _cached(engine, "my-voice") == 0  # what it said with the old text is made again


def _cached(engine: Any, slug: str) -> int:
    n = 0
    for row in engine.db.query("SELECT key FROM assets WHERE kind='voice'"):
        a = engine.assets.get("voice", row["key"], touch=False)
        if a is not None and a.meta.get("model") == "clone" and a.meta.get("voice") == slug:
            n += 1
    return n


def test_delete_evicts_audio_and_resets_settings(cloning: tuple[Any, TestClient, Heard]) -> None:
    engine, client, heard = cloning
    clone(client, heard)
    vid = "qwen3:clone:my-voice"
    client.patch("/api/v1/settings", json={"defaults": {"voice": vid, "assistant_voice": vid}})
    asyncio.run(engine.settings.set_key("defaults.voices", {"en": vid, "es": "kokoro:kokoro-82m-v1.0:ef_dora"}))
    client.post(f"/api/v1/voices/{vid}/preview", params={"play": "false"})
    assert _cached(engine, "my-voice") >= 1
    assert client.delete("/api/v1/voices/clones/my-voice").status_code == 204
    assert not (engine.paths.voices / "my-voice").exists() and _cached(engine, "my-voice") == 0
    st = client.get("/api/v1/settings").json()["defaults"]
    assert st["voice"] == "auto" and st["assistant_voice"] == "auto"
    assert st["voices"] == {"es": "kokoro:kokoro-82m-v1.0:ef_dora"}
    assert client.get("/api/v1/voices/clones").json()["items"] == []
    assert client.delete("/api/v1/voices/clones/my-voice").status_code == 404


def test_designed_voices(cloning: tuple[Any, TestClient, Heard]) -> None:
    engine, client, heard = cloning
    heard.default = P.design_text("en")
    r = client.post("/api/v1/voices/clones/design", json={"description": "a calm, low storyteller", "name": "Teller"})
    assert r.status_code == 202
    job = wait_job(client, r.json()["job_id"])
    assert job["state"] == "done", job
    cands = job["result"]["candidates"]
    assert [c["index"] for c in cands] == [1, 2, 3] and len({c["seed"] for c in cands}) == 3
    assert all(c["recording_id"] and c["wer"] == 0 for c in cands)
    heard.default = P.check_text("en")
    kept = client.post(f"/api/v1/voices/clones/design/{cands[1]['candidate_id']}", json={}).json()
    assert kept["source"] == "design" and kept["name"] == "Teller"
    assert kept["design_prompt"] == "a calm, low storyteller"
    assert kept["ref_text"] == P.design_text("en") and kept["check"]["grade"] == "good"
    voices = {v["id"]: v for v in client.get("/api/v1/voices").json()["items"]}
    assert "designed voice" in voices["qwen3:clone:teller"]["name"]
    # The other candidates are gone once one is kept.
    assert client.post(f"/api/v1/voices/clones/design/{cands[0]['candidate_id']}", json={}).status_code == 404


def test_passages(cloning: tuple[Any, TestClient, Heard]) -> None:
    _, client, _ = cloning
    es = client.get("/api/v1/voices/clones/passages", params={"language": "es"}).json()
    assert {p["language"] for p in es["items"]} == {"es"} and es["consent_version"] == P.CONSENT_VERSION
    en = client.get("/api/v1/voices/clones/passages").json()
    assert "permission" in en["consent"] and all(p["id"].startswith("en-") for p in en["items"])


# ------------------------------------------------------------------ the conversation


def _upload(client: TestClient, heard: Heard, action: dict[str, Any], session: str) -> dict[str, Any]:
    heard.queue = [action["details"]["passage"]]
    heard.default = P.check_text("en")
    r = client.post("/api/v1/voices/clones", files={"file": ("recording.wav", reading())},
                    data={"consent": "true", "passage_id": action["details"]["passage_id"], "source": "recording",
                          "keep": "false", "session_id": session, "name": action["details"]["name"]})
    assert r.status_code == 202, r.text
    return r.json()  # type: ignore[no-any-return]


def test_clone_my_voice_then_keep_it(cloning: tuple[Any, TestClient, Heard]) -> None:
    engine, client, heard = cloning
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        r = turn(client, reader, "clone my voice", source="spoken")
        action = r["actions"][0]
        assert action["kind"] == "clone_record"
        assert action["details"]["passage"] == P.passage(action["details"]["passage_id"]).text  # type: ignore[union-attr]
        assert "permission" in action["details"]["consent"] and "ready" in action["spoken"]
        # "ready": the card is confirmed (the bar starts its own recording).
        ready = turn(client, reader, "ready", source="spoken")
        assert ready["done"]["text"] == "Go ahead."
        resolved = next(m for m in reader.seen if m.get("type") == "assistant.action_resolved"
                        and m.get("action_id") == action["action_id"])
        assert resolved["status"] == "confirmed"
        accepted = _upload(client, heard, action, "sess-upload")
        started = reader.of_type("assistant.turn", session_id="sess-upload")
        done = reader.of_type("assistant.done", turn_id=started["turn_id"], timeout=30)
        assert done["speaks"] and "Keep it?" in done["text"] and done["text"].startswith("This is how I sound")
        keep = next(m for m in reader.seen if m.get("type") == "assistant.action" and m.get("kind") == "keep_clone")
        assert keep["details"]["slug"] == accepted["slug"] and keep["details"]["grade"] == "good"
        # Not a voice yet: unlisted until kept.
        assert client.get("/api/v1/voices/clones").json()["items"] == []
        yes = turn(client, reader, "yes", source="spoken")
    assert yes["done"]["text"] == "Saved. I'll read with it from now on."
    assert client.get("/api/v1/settings").json()["defaults"]["voice"] == "qwen3:clone:my-voice"
    assert [c["slug"] for c in client.get("/api/v1/voices/clones").json()["items"]] == ["my-voice"]
    turns = engine.db.query("SELECT source, user_text FROM turns ORDER BY created_at")
    assert ("button", "") in [(t["source"], t["user_text"]) for t in turns]


def test_no_discards_the_new_voice(cloning: tuple[Any, TestClient, Heard]) -> None:
    engine, client, heard = cloning
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        action = turn(client, reader, "clone my voice")["actions"][0]
        client.post(f"/api/v1/actions/{action['action_id']}/confirm")  # the record button
        accepted = _upload(client, heard, action, "sess-2")
        started = reader.of_type("assistant.turn", session_id="sess-2")
        reader.of_type("assistant.done", turn_id=started["turn_id"], timeout=30)
        no = turn(client, reader, "no")
    assert no["done"]["text"] == "Okay, I deleted it."
    assert not (engine.paths.voices / accepted["slug"]).exists()


def test_a_failed_recording_offers_another_passage(cloning: tuple[Any, TestClient, Heard]) -> None:
    _, client, heard = cloning
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        action = turn(client, reader, "clone my voice")["actions"][0]
        client.post(f"/api/v1/actions/{action['action_id']}/confirm")
        heard.queue = ["something completely different from the passage text"]
        r = client.post("/api/v1/voices/clones", files={"file": ("recording.wav", reading())},
                        data={"consent": "true", "passage_id": "en-1", "source": "recording", "keep": "false",
                              "session_id": "sess-3"})
        assert r.status_code == 202
        started = reader.of_type("assistant.turn", session_id="sess-3")
        done = reader.of_type("assistant.done", turn_id=started["turn_id"], timeout=30)
    assert "doesn't match the passage" in done["text"]
    retry = [m for m in reader.seen if m.get("type") == "assistant.action" and m.get("turn_id") == started["turn_id"]]
    assert retry and retry[0]["kind"] == "clone_record" and retry[0]["details"]["passage_id"] == "en-2"


def test_the_download_comes_first_when_the_cloning_model_is_missing(
    engine_factory: Callable[..., tuple[Any, TestClient]],
) -> None:
    from open_speech.providers import qwen3_worker as qw
    from open_speech.providers.qwen3 import InProcessBackend, Qwen3Tts

    engine, client = engine_factory()
    engine.registry._f.qwen3 = lambda catalog, paths, **kw: Qwen3Tts(
        catalog, paths, backend_factory=lambda: InProcessBackend(qw.FakeModel), memory_gb=lambda: 16.0,
        platform_ok=lambda: True, bundled_models=paths.home / "none", **kw)
    engine.registry._cache.pop("tts:qwen3", None)
    installs: list[str] = []
    engine.model_manager.start_install = lambda model_id: installs.append(model_id) or "job_x"  # type: ignore[method-assign]
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        r = turn(client, reader, "clone my voice")
        action = r["actions"][0]
        assert action["kind"] == "clone_download" and action["details"]["model_id"] == BASE
        assert "gigabytes" in action["spoken"] and "GB" in action["summary"]
        yes = turn(client, reader, "yes")
    assert installs == [BASE]
    assert yes["actions"] and yes["actions"][0]["kind"] == "clone_record"  # and on to the passage, while it downloads
    assert yes["done"]["text"].startswith("Downloading the cloning model now.")


def test_clone_this_voice_with_an_attached_file(cloning: tuple[Any, TestClient, Heard], tmp_path: Path) -> None:
    _, client, heard = cloning
    sample = tmp_path / "grandpa.wav"
    sample.write_bytes(reading())
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        tid = client.post("/api/v1/assistant/turns", json={
            "text": "clone this voice", "source": "spoken",
            "attachments": [{"kind": "file", "path": str(sample), "name": "grandpa.wav"}]}).json()["turn_id"]
        reader.of_type("assistant.done", turn_id=tid)
        action = next(m for m in reader.seen if m.get("type") == "assistant.action" and m.get("turn_id") == tid)
        assert action["kind"] == "clone_file" and "grandpa.wav" in action["summary"]
        assert "permission" in action["spoken"]
        heard.queue = ["hello there this is my sample voice"]
        heard.default = P.check_text("en")
        yes = turn(client, reader, "yes", timeout=30)
    assert yes["done"]["speaks"] and "Keep it?" in yes["done"]["text"]
    assert yes["actions"] and yes["actions"][0]["kind"] == "keep_clone"
    assert yes["actions"][0]["details"]["name"] == "Grandpa"


def test_delete_and_rename_by_voice(cloning: tuple[Any, TestClient, Heard]) -> None:
    engine, client, heard = cloning
    clone(client, heard, name="Jeff")
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        renamed = turn(client, reader, "rename the voice Jeff to Dad")
        assert renamed["done"]["text"] == "Renamed Jeff to Dad."
        ask = turn(client, reader, "delete the voice Dad")
        assert ask["actions"][0]["kind"] == "delete_voice"
        done = turn(client, reader, "yes")
    assert done["done"]["text"] == "Deleted Dad."
    assert client.get("/api/v1/voices/clones").json()["items"] == []


def test_the_voices_card_offers_cloning(cloning: tuple[Any, TestClient, Heard]) -> None:
    _, client, heard = cloning
    clone(client, heard)
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        card = turn(client, reader, "show voices")["cards"][0]
    rows = card["rows"]
    assert rows[-1]["id"] == "voices:clone" and rows[-1]["data"]["command"] == "clone my voice"
    mine = next(r for r in rows if r["id"] == "qwen3:clone:my-voice")
    assert mine["data"]["clone"] and mine["data"]["source"] == "recording"


def test_settings_rows_for_local_voice_size_appear_with_qwen(cloning: tuple[Any, TestClient, Heard]) -> None:
    _, client, _ = cloning
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        rows = {r["id"]: r for r in turn(client, reader, "show settings")["cards"][0]["rows"]}
    size, precision = rows["qwen3.size"], rows["qwen3.precision"]
    assert size["data"]["options"] == ["0.6b", "1.7b"] and size["data"]["auto"]
    assert size["data"]["family"] == "customvoice"
    assert "1.7b" in size["data"]["downloads"] and size["data"]["downloads"]["1.7b"]["size"].endswith("GB")
    assert precision["data"]["options"][-1] == "bf16" and precision["data"]["value"] == "8bit"


def test_settings_rows_hidden_without_qwen(api: tuple[Any, TestClient]) -> None:
    engine, client = api
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        rows = {r["id"] for r in turn(client, reader, "show settings")["cards"][0]["rows"]}
    assert "qwen3.size" not in rows and "qwen3.precision" not in rows


def test_choosing_a_designed_voice_by_voice(cloning: tuple[Any, TestClient, Heard]) -> None:
    from open_speech.service.clones import chosen_index, name_from_description

    assert name_from_description("a calm old storyteller with a deep British accent") == "Calm old storyteller"
    assert name_from_description("A warm, deep male voice of an older storyteller") == "Warm deep male"
    assert name_from_description("una voz de locutor tranquilo") == "Locutor tranquilo"

    assert [chosen_index(x) for x in ("the second one", "keep two", "number 3", "la segunda", "me quedo con la uno",
                                      "one", "the first one please")] == [2, 2, 3, 2, 1, 1, 1]
    assert chosen_index("one or two") is None and chosen_index("what time is it") is None
    engine, client, heard = cloning
    heard.default = P.design_text("en")
    with ws_connect(client) as ws:
        reader = WsReader(ws)
        made = turn(client, reader, "make a voice that sounds like a calm old storyteller", timeout=30)
        assert made["done"]["speaks"] and "Which one should I keep?" in made["done"]["text"]
        assert made["cards"][0]["kind"] == "design" and len(made["cards"][0]["rows"]) == 3
        heard.default = P.check_text("en")
        kept = turn(client, reader, "the second one", source="spoken", timeout=30)
    assert kept["done"]["text"] == "Saved “Calm old storyteller”. I'll read with it from now on."
    assert client.get("/api/v1/settings").json()["defaults"]["voice"] == "qwen3:clone:calm-old-storyteller"


def test_designed_candidates_survive_a_restart(cloning: tuple[Any, TestClient, Heard]) -> None:
    engine, client, heard = cloning
    heard.default = P.design_text("en")
    job = wait_job(client, client.post("/api/v1/voices/clones/design", json={"description": "a bright young voice"})
                   .json()["job_id"])
    cid = job["result"]["candidates"][2]["candidate_id"]
    engine.clones._candidates.clear()  # what a restart forgets
    heard.default = P.check_text("en")
    kept = client.post(f"/api/v1/voices/clones/design/{cid}", json={"name": "Bright"}).json()
    assert kept["name"] == "Bright" and kept["source"] == "design"


# ------------------------------------------------------------------ the CLI


def test_cli_preview_writes_a_file(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from open_speech.cli.main import app

    monkeypatch.setenv("OPEN_SPEECH_AUDIO_OUTPUT", "null")
    out = tmp_path / "sample.wav"
    result = CliRunner().invoke(app, ["voices", "preview", "fixture:tone-v1:alto", "--out", str(out),
                                      "--text", "Hello from a file."])
    assert result.exit_code == 0, result.output
    y, sr = sf.read(str(out))
    assert sr > 0 and len(y) / sr > 0.5 and "Wrote" in result.output
    nothing = CliRunner().invoke(app, ["voices", "clone", "x", "--file", str(out)])
    assert nothing.exit_code == 2 and "--i-have-consent" in nothing.output

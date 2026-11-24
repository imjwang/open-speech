"""Google Cloud Text-to-Speech (Chirp 3 HD): request shape, audio, the 5,000-byte limit, error mapping, the
availability check and its effect on the voice list, keys (its own identity, Gemini's key as a fallback), and the
synthesizer's handling of a refused key. Plain httpx, so respx intercepts it; fixtures are recorded shapes."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import struct
from decimal import Decimal
from typing import Any

import httpx
import numpy as np
import pytest
import respx
import soundfile as sf

from open_speech.config import Paths
from open_speech.errors import EngineError, Unavailable
from open_speech.local.catalog import ModelCatalog
from open_speech.providers.base import TtsRequest
from open_speech.providers.errors import ProviderError
from open_speech.providers.google_tts import (
    API_BASE,
    MAX_INPUT_BYTES,
    GoogleTts,
    GoogleVoiceCheck,
    decode_linear16,
    split_for_limit,
)
from open_speech.providers.keys import ProviderKeys
from open_speech.service.events import EventBus
from open_speech.store.db import Database

from .helpers import SettingsBox, fixture_text, make_env
from .test_registry_synth import build

SYNTH_URL = f"{API_BASE}/v1/text:synthesize"
VOICES_URL = f"{API_BASE}/v1/voices"
ZEPHYR = "google:chirp3-hd:en-US-Chirp3-HD-Zephyr"


def synth_ok() -> httpx.Response:
    return httpx.Response(200, json=json.loads(fixture_text("google_tts_synthesize.json")))


def blocked() -> httpx.Response:
    return httpx.Response(403, json=json.loads(fixture_text("google_tts_403_service_blocked.json")))


def voices_for(request: httpx.Request) -> httpx.Response:
    locale = request.url.params.get("languageCode")
    items = json.loads(fixture_text("google_tts_voices.json"))["voices"]
    return httpx.Response(200, json={"voices": [v for v in items if locale in v["languageCodes"]]})


def google_error(status: int, reason: str, gstatus: str = "PERMISSION_DENIED") -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": status, "message": "nope", "status": gstatus, "details": [
        {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason, "domain": "googleapis.com"}]}})


async def drain(tts: GoogleTts, req: TtsRequest) -> list[Any]:
    return [c async for c in tts.synthesize(req)]


# ------------------------------------------------------------------ the adapter


@respx.mock
async def test_request_shape_header_and_audio(catalog: ModelCatalog) -> None:
    route = respx.post(SYNTH_URL).mock(return_value=synth_ok())
    tts = GoogleTts(catalog, "g-test-key")
    req = TtsRequest(text="Hola, ¿qué tal?", model="chirp3-hd", voice="es-US-Chirp3-HD-Zephyr", language="es")
    chunks = await drain(tts, req)
    sent = route.calls.last.request
    assert sent.headers["x-goog-api-key"] == "g-test-key"
    assert "key=" not in str(sent.url)  # the key travels in a header, never in the URL
    assert json.loads(sent.content) == {
        "input": {"text": "Hola, ¿qué tal?"},
        "voice": {"languageCode": "es-US", "name": "es-US-Chirp3-HD-Zephyr"},
        "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": 24000},  # no speakingRate: speed is ours
    }
    assert len(chunks) == 1 and chunks[0].final and chunks[0].sample_rate == 24000
    pcm = np.frombuffer(chunks[0].pcm, dtype="<f4")
    assert abs(pcm.size / 24000 - 1.1) < 0.01 and np.max(np.abs(pcm)) > 0.1
    units = tts.price_units(req, 1.1)
    assert units.chars == len(req.text) and units.in_tok == 0  # billed per character
    assert not tts.timestamps
    await tts.aclose()


@respx.mock
async def test_long_text_is_split_at_sentences_under_the_byte_limit(catalog: ModelCatalog) -> None:
    route = respx.post(SYNTH_URL).mock(return_value=synth_ok())
    sentence = "Él leyó la carta dos veces, despacio y en voz baja. "  # multibyte: bytes > characters
    text = (sentence * 120).strip()
    assert len(text.encode()) > MAX_INPUT_BYTES
    req = TtsRequest(text=text, model="chirp3-hd", voice="es-ES-Chirp3-HD-Kore")
    chunks = await drain(GoogleTts(catalog, "k"), req)
    sent = [json.loads(c.request.content)["input"]["text"] for c in route.calls]
    assert len(sent) == 2
    assert all(len(s.encode()) <= MAX_INPUT_BYTES for s in sent)
    assert all(s.endswith(".") for s in sent)  # cut between sentences
    assert " ".join(sent) == text
    assert np.frombuffer(chunks[0].pcm, dtype="<f4").size == 2 * int(1.1 * 24000)  # the pieces' audio, joined


def test_split_for_limit_falls_back_to_words_then_characters() -> None:
    assert split_for_limit("short") == ["short"]
    words = " ".join(["palabra"] * 40)
    pieces = split_for_limit(words, 50)
    assert all(len(p.encode()) <= 50 for p in pieces) and " ".join(pieces) == words
    one = "ñ" * 40  # 80 bytes, no spaces: cut between characters, never inside one
    pieces = split_for_limit(one, 25)
    assert all(len(p.encode()) <= 25 for p in pieces) and "".join(pieces) == one


def test_decode_linear16_header_variants() -> None:
    pcm = (np.sin(np.arange(2400) / 10) * 0.5).astype(np.float32)
    buf = io.BytesIO()
    sf.write(buf, pcm, 24000, format="WAV", subtype="PCM_16")
    wav = buf.getvalue()
    x, sr = decode_linear16(wav)
    assert sr == 24000 and x.size == 2400 and np.allclose(x, pcm, atol=1e-3)
    # A streaming-style header whose data size is unknown (0xFFFFFFFF): read to the end.
    i = wav.index(b"data")
    streaming = wav[: i + 4] + struct.pack("<I", 0xFFFFFFFF) + wav[i + 8 :]
    assert decode_linear16(streaming)[0].size == 2400
    # Headerless PCM: taken at the requested rate.
    raw = (pcm * 32767).astype("<i2").tobytes()
    x, sr = decode_linear16(raw, 16000)
    assert sr == 16000 and x.size == 2400


@pytest.mark.parametrize(
    ("response", "code", "blocked_kind", "key_invalid", "retryable"),
    [
        (blocked, "unavailable", "blocked", False, False),
        (lambda: google_error(403, "SERVICE_DISABLED"), "unavailable", "disabled", False, False),
        (lambda: google_error(403, "ACCESS_DENIED"), "unavailable", "blocked", False, False),  # voices.list, live
        (lambda: google_error(403, "SOMETHING_NEW"), "unavailable", "blocked", False, False),  # a 403 we can't name
        (lambda: google_error(403, "BILLING_DISABLED"), "unavailable", "billing", False, False),
        (lambda: google_error(400, "API_KEY_INVALID", "INVALID_ARGUMENT"), "provider_error", None, True, False),
        (lambda: httpx.Response(401, json={"error": {"code": 401}}), "provider_error", None, True, False),
        (lambda: google_error(429, "RATE_LIMIT_EXCEEDED", "RESOURCE_EXHAUSTED"), "rate_limited", None, False, True),
        (lambda: httpx.Response(503, json={"error": {"code": 503}}), "provider_error", None, False, True),
        (lambda: httpx.Response(400, json={"error": {"code": 400, "status": "INVALID_ARGUMENT"}}),
         "provider_error", None, False, False),
    ],
)
@respx.mock
async def test_error_mapping(catalog: ModelCatalog, response: Any, code: str, blocked_kind: str | None,
                             key_invalid: bool, retryable: bool) -> None:
    respx.post(SYNTH_URL).mock(return_value=response())
    with pytest.raises(ProviderError) as ei:
        await drain(GoogleTts(catalog, "k"), TtsRequest(text="x", model="chirp3-hd", voice="en-US-Chirp3-HD-Kore"))
    err = ei.value
    assert (err.code, err.blocked, err.key_invalid, err.retryable) == (code, blocked_kind, key_invalid, retryable)  # type: ignore[attr-defined]
    if blocked_kind == "blocked":
        assert err.detail == "Google's voices need Cloud Text-to-Speech enabled for this build's key."


@respx.mock
async def test_network_failure_is_offline_and_retryable(catalog: ModelCatalog) -> None:
    respx.post(SYNTH_URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(ProviderError) as ei:
        await drain(GoogleTts(catalog, "k"), TtsRequest(text="x", model="chirp3-hd", voice="en-US-Chirp3-HD-Kore"))
    assert ei.value.code == "offline" and ei.value.retryable


# ------------------------------------------------------------------ keys


def test_google_key_precedence_and_identity() -> None:
    both = ProviderKeys(make_env(google_tts_api_key="g-tts", gemini_api_key="g-gem"))
    assert both.get("google") == "g-tts" and both.get("gemini") == "g-gem"
    only_gemini = ProviderKeys(make_env(gemini_api_key="g-gem"))
    assert only_gemini.get("google") == "g-gem" and only_gemini.configured("google")
    blank = ProviderKeys(make_env(google_tts_api_key="  ", gemini_api_key="g-gem"))
    assert blank.get("google") == "g-gem"
    assert ProviderKeys(make_env()).status("google") == "missing"
    # Marking Google invalid never touches Gemini, and the other way round.
    only_gemini.mark_invalid("google")
    assert only_gemini.status("google") == "invalid" and only_gemini.status("gemini") == "configured"
    other = ProviderKeys(make_env(gemini_api_key="g-gem"))
    other.mark_invalid("gemini")
    assert other.status("gemini") == "invalid" and other.status("google") == "configured"
    assert "g-gem" not in repr(other)


@respx.mock
async def test_the_header_carries_the_google_key_when_both_are_set(paths: Paths, db: Database,
                                                                  settings: SettingsBox, events: EventBus) -> None:
    route = respx.post(SYNTH_URL).mock(return_value=synth_ok())
    rt = build(paths, db, settings, events, fixture=False, google_tts_api_key="g-tts", gemini_api_key="g-gem")
    res = await rt.synth.synth("Hello there.", ZEPHYR)
    assert res.voice_id == ZEPHYR and route.calls.last.request.headers["x-goog-api-key"] == "g-tts"


# ------------------------------------------------------------------ the synthesizer and a refused key


@respx.mock
async def test_service_blocked_403_leaves_gemini_valid_and_marks_google_unavailable(
    paths: Paths, db: Database, settings: SettingsBox, events: EventBus
) -> None:
    respx.post(SYNTH_URL).mock(return_value=blocked())
    rt = build(paths, db, settings, events, gemini_api_key="g-gem")  # fixture mode: the local voice is the tone
    res = await rt.synth.synth("Hello there.", ZEPHYR)
    assert res.voice_id == "fixture:tone-v1:alto"  # read on with the local voice
    assert res.notice_reason == "unavailable"
    assert rt.keys.status("google") == "configured"  # not a bad key: nothing is marked invalid
    assert rt.keys.status("gemini") == "configured"
    assert any(v.provider == "gemini" and v.available for v in rt.registry.voices())
    google = [v for v in rt.registry.voices() if v.provider == "google"]
    assert google and not any(v.available for v in google)
    assert google[0].unavailable_reason == "Google's voices need Cloud Text-to-Speech enabled for this build's key."
    with pytest.raises(Unavailable):
        rt.registry.tts_for(ZEPHYR)
    assert not rt.registry.google_usable() and rt.registry.auto_cloud_voice("en") is None
    assert len(respx.calls) == 1  # a refusal isn't retried


@respx.mock
async def test_invalid_key_marks_only_google_invalid(paths: Paths, db: Database, settings: SettingsBox,
                                                     events: EventBus) -> None:
    respx.post(SYNTH_URL).mock(return_value=google_error(400, "API_KEY_INVALID", "INVALID_ARGUMENT"))
    rt = build(paths, db, settings, events, gemini_api_key="g-gem")
    res = await rt.synth.synth("Hello there.", ZEPHYR)
    assert res.voice_id == "fixture:tone-v1:alto"
    assert rt.keys.status("google") == "invalid" and rt.keys.status("gemini") == "configured"
    assert not any(v.provider == "google" for v in rt.registry.voices())


@respx.mock
async def test_429_is_retried_then_succeeds(paths: Paths, db: Database, settings: SettingsBox,
                                            events: EventBus) -> None:
    route = respx.post(SYNTH_URL).mock(
        side_effect=[google_error(429, "RATE_LIMIT_EXCEEDED", "RESOURCE_EXHAUSTED"), synth_ok()]
    )
    rt = build(paths, db, settings, events, fixture=False, gemini_api_key="g-gem")
    res = await rt.synth.synth("Hello there.", ZEPHYR)
    assert res.voice_id == ZEPHYR and res.notice is None and route.call_count == 2
    row = rt.ledger.rows().items[0]
    assert row.provider == "google" and Decimal(row.usd_estimate) == 0  # inside the free allowance
    assert row.units["free_chars"] == len("Hello there.")


# ------------------------------------------------------------------ the availability check


@respx.mock
async def test_check_lists_live_voices_and_reports_changes(catalog: ModelCatalog) -> None:
    respx.get(VOICES_URL).mock(side_effect=voices_for)
    changes: list[str] = []
    check = GoogleVoiceCheck(catalog, lambda: "k", on_change=lambda: changes.append("x"))
    assert check.state == "unknown" and check.usable() and check.listed("es-ES-Chirp3-HD-Sadachbia")
    assert await check.check() is True
    assert check.state == "available" and changes == ["x"]
    assert check.listed("en-US-Chirp3-HD-Zephyr") and not check.listed("es-ES-Chirp3-HD-Sadachbia")
    locales = {c.request.url.params["languageCode"] for c in respx.calls}
    assert locales == {"en-US", "en-GB", "es-US", "es-ES"}
    assert all(c.request.headers["x-goog-api-key"] == "k" for c in respx.calls)
    assert await check.check() is False and changes == ["x"]  # nothing new: no event


@respx.mock
async def test_check_blocked_then_enabled(catalog: ModelCatalog) -> None:
    route = respx.get(VOICES_URL).mock(return_value=blocked())
    changes: list[str] = []
    check = GoogleVoiceCheck(catalog, lambda: "k", on_change=lambda: changes.append(check.state))
    assert await check.check() is True
    assert check.state == "blocked" and not check.usable() and changes == ["blocked"]
    assert check.status("en-US-Chirp3-HD-Zephyr") == (False, check.reason())
    assert route.call_count == 1  # the first locale answers for all of them
    route.mock(side_effect=voices_for)  # the owner allows Cloud Text-to-Speech for the key
    assert await check.check() is True and changes == ["blocked", "available"] and check.usable()


@respx.mock
async def test_offline_check_keeps_what_was_known(catalog: ModelCatalog) -> None:
    respx.get(VOICES_URL).mock(side_effect=httpx.ConnectError("down"))
    check = GoogleVoiceCheck(catalog, lambda: "k")
    assert await check.check() is False and check.state == "unknown" and check.usable()


@respx.mock
async def test_schedule_rechecks_and_a_synthesis_403_rechecks_at_once(catalog: ModelCatalog) -> None:
    route = respx.get(VOICES_URL).mock(side_effect=voices_for)
    check = GoogleVoiceCheck(catalog, lambda: "k", after_s=(0.05, 0.05), every_s=30.0)
    task = asyncio.create_task(check.run())
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if route.call_count >= 12:  # startup, +1 "minute", +10 "minutes" (4 locales each)
                break
        assert route.call_count == 12
        await asyncio.sleep(0.1)
        assert route.call_count == 12  # now hourly
        route.mock(return_value=blocked())
        err = ProviderError("unavailable", "blocked", provider="google", status=403)
        err.blocked = "blocked"  # type: ignore[attr-defined]
        check.note_synthesis_error(err)
        assert check.state == "blocked"  # believed at once...
        for _ in range(100):
            await asyncio.sleep(0.01)
            if route.call_count > 12:
                break
        assert route.call_count == 13 and check.state == "blocked"  # ...and confirmed by a check right away
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@respx.mock
async def test_registry_offers_only_listed_voices_and_capabilities_follow(
    paths: Paths, db: Database, settings: SettingsBox, events: EventBus
) -> None:
    respx.get(VOICES_URL).mock(side_effect=voices_for)
    rt = build(paths, db, settings, events, fixture=False, gemini_api_key="g-gem")
    seen: list[int] = []
    rt.registry.on_voices_changed = lambda: seen.append(1)
    before = [v for v in rt.registry.voices() if v.provider == "google"]
    assert len(before) == 120 and all(v.available for v in before)  # not checked yet: offered
    await rt.registry.google_check().check()
    after = {v.id: v for v in rt.registry.voices() if v.provider == "google"}
    assert len(after) == 119 and "google:chirp3-hd:es-ES-Chirp3-HD-Sadachbia" not in after
    assert seen == [1]
    z = after[ZEPHYR]
    assert z.name == "Zephyr (Chirp 3 HD, American English)" and z.languages == ["en-US"]
    assert z.persona == "chirp3-hd:Zephyr" and not z.local and not z.timestamps
    assert after["google:chirp3-hd:es-US-Chirp3-HD-Zephyr"].persona == z.persona
    with pytest.raises(EngineError):
        rt.registry.tts_for("google:chirp3-hd:es-ES-Chirp3-HD-Sadachbia")


@respx.mock
async def test_engine_publishes_capabilities_when_google_starts_working(tmp_path: Any) -> None:
    from open_speech.service.engine import Engine

    route = respx.get(VOICES_URL).mock(return_value=blocked())
    engine = Engine(make_env(open_speech_home=tmp_path / "home", gemini_api_key="g-gem",
                             open_speech_audio_output="null"))
    seen: list[dict[str, Any]] = []
    engine.events.add_listener(seen.append)
    check = engine.registry.google_check()
    await check.check()
    caps = [m for m in seen if m["type"] == "capabilities.changed"]
    assert caps and "google" not in caps[-1]["capabilities"]["cloud_voices"]
    assert "gemini" in caps[-1]["capabilities"]["cloud_voices"]
    route.mock(side_effect=voices_for)
    await check.check()
    caps = [m for m in seen if m["type"] == "capabilities.changed"]
    assert "google" in caps[-1]["capabilities"]["cloud_voices"]
    engine.db.close()


def test_fixture_audio_is_a_real_wav() -> None:
    body = json.loads(fixture_text("google_tts_synthesize.json"))
    raw = base64.b64decode(body["audioContent"])
    assert raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"

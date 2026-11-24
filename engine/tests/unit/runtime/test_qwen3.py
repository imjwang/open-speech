"""Local Qwen3-TTS: variant resolution, voice ids, cache identity, availability, the worker protocol (a real child
process serving `qwen3_worker.FakeModel`, no MLX, no weights), cancelling, crashes, idle unload, the registry table,
the model manager's variants, install by voice, and catalog generation from recorded Hugging Face responses."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import numpy as np
import pytest

from open_speech.agent.intents import parse
from open_speech.config import Paths
from open_speech.errors import EngineError, NotFound
from open_speech.local.catalog import ModelCatalog, WeightsVariant
from open_speech.local.manager import HardwareInfo, LocalModelManager
from open_speech.providers import qwen3_worker as qw
from open_speech.providers.base import TtsRequest, cache_identity
from open_speech.providers.keys import ProviderKeys
from open_speech.providers.qwen3 import (
    CloneVoice,
    InProcessBackend,
    Qwen3Tts,
    WorkerBackend,
    qwen_language,
    resolve_variant,
    utterance_seed,
)
from open_speech.providers.registry import TTS_PROVIDERS, ProviderFactories, ProviderRegistry
from open_speech.store.db import Database
from tools import pin_hf_weights as pin

from .helpers import FIXTURES, SettingsBox, make_env

SMALL = "qwen3-tts-0.6b-customvoice-8bit"
LARGE = "qwen3-tts-1.7b-customvoice-8bit"
BASE_SMALL = "qwen3-tts-0.6b-base-8bit"


def install(paths: Paths, catalog: ModelCatalog, *ids: str) -> None:
    """Fake weights on disk (empty files at every pinned path): enough for 'installed'."""
    for vid in ids:
        v = catalog.weights_variant(vid)
        assert v is not None
        for rel in v.required_files():
            f = paths.models / "qwen3" / vid / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"")


class Clones:
    def __init__(self, *clones: CloneVoice) -> None:
        self.items = {c.slug: c for c in clones}

    def clones(self) -> list[CloneVoice]:
        return list(self.items.values())

    def clone(self, slug: str) -> CloneVoice | None:
        return self.items.get(slug)


def provider(paths: Paths, catalog: ModelCatalog, settings: SettingsBox | None = None, *,
             backend: Any = None, memory: float = 16.0, apple: bool = True, **kw: Any) -> Qwen3Tts:
    factory = (lambda: backend) if backend is not None else (lambda: InProcessBackend(qw.FakeModel))
    return Qwen3Tts(catalog, paths, settings=settings, backend_factory=factory if apple else None,
                    memory_gb=lambda: memory, platform_ok=lambda: apple, bundled_models=paths.home / "none", **kw)


# ------------------------------------------------------------------ resolution


def variants(catalog: ModelCatalog) -> list[WeightsVariant]:
    return catalog.weights_variants("qwen3")


def test_resolution_defaults_and_small_macs(catalog: ModelCatalog) -> None:
    defaults = catalog.tts_model("qwen3", "qwen3-tts").extra  # type: ignore[union-attr]
    none: Any = lambda _v: False  # noqa: E731
    r = resolve_variant(variants(catalog), "customvoice", installed=none, memory_gb=16, defaults=defaults)
    assert r is not None and r.wanted.model == f"qwen3-tts-{defaults['default_size']}-customvoice-8bit"
    assert not r.installed and r.note is None
    small = resolve_variant(variants(catalog), "customvoice", installed=none, memory_gb=8, defaults=defaults)
    assert small is not None and small.wanted.model == SMALL  # 8 GB Macs read with 0.6B
    big = resolve_variant(variants(catalog), "customvoice", "1.7b", "4bit", installed=none, defaults=defaults)
    assert big is not None and big.wanted.model == "qwen3-tts-1.7b-customvoice-4bit"
    design = resolve_variant(variants(catalog), "voicedesign", "0.6b", "auto", installed=none, defaults=defaults)
    assert design is not None and design.wanted.size == "1.7b"  # VoiceDesign exists at 1.7B only


def test_a_downloaded_sibling_reads_with_a_note(catalog: ModelCatalog) -> None:
    have = {"qwen3-tts-0.6b-customvoice-bf16", "qwen3-tts-1.7b-customvoice-8bit"}
    r = resolve_variant(variants(catalog), "customvoice", "0.6b", "8bit", installed=lambda v: v.model in have)
    assert r is not None and r.installed and r.wanted.model == SMALL
    assert r.variant.model == "qwen3-tts-0.6b-customvoice-bf16"  # same size first
    assert r.note is not None and "0.6B bf16" in r.note and "0.6B 8-bit" in r.note
    other = resolve_variant(variants(catalog), "customvoice", "0.6b", "8bit", installed=lambda v: v.model == LARGE)
    assert other is not None and other.variant.model == LARGE and other.note
    exact = resolve_variant(variants(catalog), "customvoice", "1.7b", "8bit", installed=lambda v: v.model in have)
    assert exact is not None and exact.variant.model == LARGE and exact.note is None
    # a family's siblings never stand in for another family
    base = resolve_variant(variants(catalog), "base", "0.6b", "8bit", installed=lambda v: v.model in have)
    assert base is not None and not base.installed and base.variant.family == "base"


def test_language_map() -> None:
    assert qwen_language("en") == qwen_language("en-US") == qwen_language("EN_gb") == "english"
    assert qwen_language("es-MX") == "spanish" and qwen_language("ja") == "japanese"
    assert qwen_language("nl") == qwen_language(None) == qwen_language("") == "auto"


# ------------------------------------------------------------------ voices, ids, cache identity


def test_voice_ids_survive_size_and_precision_changes(paths: Paths, catalog: ModelCatalog,
                                                      settings: SettingsBox) -> None:
    install(paths, catalog, SMALL, LARGE)
    clone = CloneVoice("my-voice", "Mine", ref_audio=paths.home / "ref.wav", ref_text="hello", ref_sha256="ab" * 32)
    (paths.home / "ref.wav").write_bytes(b"RIFF")
    q = provider(paths, catalog, settings, clones=Clones(clone))
    before = [v.id for v in q.voices()]
    assert before[0] == "qwen3:qwen3-tts:Ryan" and "qwen3:clone:my-voice" in before
    assert len([v for v in q.voices() if v.model == "qwen3-tts"]) == 9
    settings.set("qwen3.size", "1.7b")
    settings.set("qwen3.precision", "8bit")
    assert [v.id for v in q.voices()] == before
    ryan = next(v for v in q.voices() if v.id == "qwen3:qwen3-tts:Ryan")
    assert ryan.local and ryan.available and ryan.languages[0] == "en" and "es" in ryan.languages
    assert ryan.name == "Ryan (Qwen, English)" and ryan.styles  # 1.7B takes style instructions
    settings.set("qwen3.size", "0.6b")
    assert not next(v for v in q.voices() if v.id == "qwen3:qwen3-tts:Ryan").styles
    assert q.has_voice("qwen3-tts", "Ryan") and q.has_voice("clone", "my-voice")
    assert not q.has_voice("qwen3-tts", "Nobody") and not q.has_voice("clone", "gone")


def test_cache_identity_names_variant_params_reference_and_language(paths: Paths, catalog: ModelCatalog,
                                                                     settings: SettingsBox) -> None:
    install(paths, catalog, SMALL, LARGE, BASE_SMALL)
    clone = CloneVoice("me", "Me", ref_audio=paths.home / "ref.wav", ref_text="hi", ref_sha256="cd" * 32)
    clones = Clones(clone)
    q = provider(paths, catalog, settings, clones=clones)
    req = TtsRequest(text="The library will be closed for the rest of the month, and it is not open on Sundays.",
                     model="qwen3-tts", voice="Ryan")
    a = cache_identity(q, req)
    rev = catalog.weights_variant(SMALL).weights.revision[:12]  # type: ignore[union-attr]
    assert a == f"qwen3-tts/{SMALL}@{rev}/p1/english"
    settings.set("qwen3.size", "1.7b")
    assert cache_identity(q, req) != a and LARGE in cache_identity(q, req)
    settings.set("qwen3.size", "0.6b")
    es = TtsRequest(text="La biblioteca estará cerrada hasta el final del mes y no abre los domingos.",
                    model="qwen3-tts", voice="Ryan")
    assert cache_identity(q, es).endswith("/spanish")
    assert cache_identity(q, req.model_copy(update={"language": "es"})).endswith("/spanish")
    q2 = provider(paths, catalog, settings, clones=clones)
    q2._entry.extra["params_version"] = 2  # type: ignore[union-attr]  # (shared catalog object: restored below)
    try:
        assert "/p2/" in cache_identity(q2, req)
    finally:
        q2._entry.extra["params_version"] = 1  # type: ignore[union-attr]
    creq = TtsRequest(text="Hello there, friend.", model="clone", voice="me")
    first = cache_identity(q, creq)
    assert BASE_SMALL in first and first.endswith("/ref-" + "cd" * 6)
    clones.items["me"] = CloneVoice("me", "Me", ref_audio=clone.ref_audio, ref_text="hi", ref_sha256="ef" * 32)
    assert cache_identity(q, creq) != first


def test_seed_is_stable_per_text_voice_and_variant() -> None:
    s = utterance_seed("Hello.", "qwen3-tts:Ryan", SMALL)
    assert s == utterance_seed("Hello.", "qwen3-tts:Ryan", SMALL) and 0 <= s < 2**31
    assert len({s, utterance_seed("Hello!", "qwen3-tts:Ryan", SMALL), utterance_seed("Hello.", "qwen3-tts:Aiden",
               SMALL), utterance_seed("Hello.", "qwen3-tts:Ryan", LARGE)}) == 4


# ------------------------------------------------------------------ availability


def test_unavailable_reasons(paths: Paths, catalog: ModelCatalog, settings: SettingsBox) -> None:
    intel = provider(paths, catalog, settings, apple=False)
    assert intel.voices() == []  # not offered at all where it can't run
    assert "Apple Silicon" in (intel.unavailable_reason("qwen3-tts") or "")
    missing = Qwen3Tts(catalog, paths, settings=settings, platform_ok=lambda: True, memory_gb=lambda: 16,
                       bundled_models=paths.home / "none")
    import open_speech.providers.qwen3 as qmod

    real_find = qmod.importlib.util.find_spec
    qmod.importlib.util.find_spec = lambda name, *a: None if name == "mlx_audio" else real_find(name, *a)  # type: ignore[assignment]
    try:
        assert "engine isn't part of this build" in (missing.unavailable_reason("qwen3-tts") or "")
    finally:
        qmod.importlib.util.find_spec = real_find  # type: ignore[assignment]
    q = provider(paths, catalog, settings)
    reason = q.unavailable_reason("qwen3-tts", "Ryan") or ""
    assert "Download the Qwen voices (0.6B 8-bit, about 1.8 GB)" in reason
    assert "cloning model" in (q.unavailable_reason("clone", None) or "")
    assert "no cloned voice called ghost" in (q.unavailable_reason("clone", "ghost") or "")
    install(paths, catalog, SMALL, LARGE)
    assert q.unavailable_reason("qwen3-tts", "Ryan") is None
    settings.set("qwen3.size", "1.7b")
    tiny = provider(paths, catalog, settings, memory=4.0)
    assert "needs about 4.2 GB of memory and this Mac has 4 GB" in (tiny.unavailable_reason("qwen3-tts") or "")
    gone = Clones(CloneVoice("old", "Old", ref_audio=paths.home / "missing.wav", ref_text="x"))
    install(paths, catalog, "qwen3-tts-1.7b-base-8bit")
    assert "recording is missing" in (provider(paths, catalog, settings, clones=gone).unavailable_reason(
        "clone", "old") or "")


# ------------------------------------------------------------------ synthesis through a backend


async def collect(q: Qwen3Tts, req: TtsRequest) -> np.ndarray:
    parts = [np.frombuffer(c.pcm, dtype="<f4") async for c in q.synthesize(req)]
    return np.concatenate(parts) if parts else np.zeros(0, np.float32)


async def test_in_process_synthesis_streams_seeded_pieces(paths: Paths, catalog: ModelCatalog,
                                                          settings: SettingsBox) -> None:
    install(paths, catalog, SMALL)
    backend = InProcessBackend(qw.FakeModel)
    q = provider(paths, catalog, settings, backend=backend)
    req = TtsRequest(text="Turn left at the next light.", model="qwen3-tts", voice="Ryan")
    pieces = [c async for c in q.synthesize(req)]
    assert len(pieces) > 1 and all(c.sample_rate == 24000 for c in pieces)  # streamed, ~0.5 s each
    audio = np.concatenate([np.frombuffer(c.pcm, dtype="<f4") for c in pieces])
    assert audio.size == int(0.06 * 24000 * len(req.text))
    model = backend._model
    call = model.calls[-1]
    assert call["voice"] == "Ryan" and call["lang_code"] == "english" and call["stream"] is True
    assert call["temperature"] == 0.9 and call["top_k"] == 50 and call["max_tokens"] >= 75
    assert model._seed == utterance_seed(req.text, "qwen3-tts:Ryan", SMALL)
    assert q.loaded() and q.realtime_factor() > 1.0 and q.rtf_measured()  # measured from that utterance
    await q.aclose()


async def test_worker_process_round_trip_cancel_and_crash(paths: Paths, catalog: ModelCatalog,
                                                          settings: SettingsBox) -> None:
    """A real child process (`-I -m open_speech.providers.qwen3_worker --fake`): framing, chunks, cancel, crash."""
    w = WorkerBackend(args=["--fake"])
    try:
        info = await w.load("v", paths.home, {"voice": "Ryan", "lang": "english"})
        assert info["op"] == "loaded" and info["sample_rate"] == 24000 and "ryan" in info["speakers"]
        assert await w.ping()
        text = "Hello from the worker."
        got = [p async for p in w.generate({"text": text, "voice": "Ryan", "seed": 3, "interval": 0.25})]
        assert len(got) > 1 and sum(p.size for p in got) == int(0.06 * 24000 * len(text))
        assert w.last_done["audio_s"] == pytest.approx(0.06 * len(text), abs=0.01) and not w.last_done["cancelled"]
        # cancel: stop reading after the first piece; the worker stops at its next one and is free again
        gen = w.generate({"text": "__slow__" + "x" * 200, "voice": "Ryan", "interval": 0.1})
        first = await gen.__anext__()
        assert first.size > 0
        await gen.aclose()
        await asyncio.wait_for(w.wait_idle(), 3)
        assert w.alive
        again = [p async for p in w.generate({"text": "Still here.", "voice": "Ryan", "interval": 0.5})]
        assert sum(p.size for p in again) > 0
        # an error inside the model is reported, the worker lives on
        with pytest.raises(RuntimeError, match="fake model failure"):
            async for _ in w.generate({"text": "__error__", "voice": "Ryan"}):
                pass
        assert w.alive
        # a crash ends the process: the request fails with WorkerCrashed
        from open_speech.providers.qwen3 import WorkerCrashed

        with pytest.raises(WorkerCrashed):
            async for _ in w.generate({"text": "__crash__", "voice": "Ryan"}):
                pass
        for _ in range(100):
            if not w.alive:
                break
            await asyncio.sleep(0.02)
        assert not w.alive
    finally:
        await w.close()


async def test_a_crash_restarts_the_worker_once_then_falls_back(paths: Paths, catalog: ModelCatalog,
                                                                settings: SettingsBox) -> None:
    install(paths, catalog, SMALL)
    made: list[WorkerBackend] = []

    def factory() -> WorkerBackend:
        made.append(WorkerBackend(args=["--fake"]))
        return made[-1]

    q = Qwen3Tts(catalog, paths, settings=settings, backend_factory=factory, memory_gb=lambda: 16,
                 platform_ok=lambda: True, bundled_models=paths.home / "none")
    try:
        ok = await collect(q, TtsRequest(text="A first sentence.", model="qwen3-tts", voice="Ryan"))
        assert ok.size > 0 and len(made) == 1
        with pytest.raises(EngineError) as err:
            await collect(q, TtsRequest(text="__crash__ now", model="qwen3-tts", voice="Ryan"))
        assert err.value.code == "provider_error" and "stopped unexpectedly" in err.value.detail
        assert len(made) == 2  # restarted once (and that one crashed too), then gave up
        after = await collect(q, TtsRequest(text="And it reads on.", model="qwen3-tts", voice="Ryan"))
        assert after.size > 0 and len(made) == 3
    finally:
        await q.aclose()
    assert all(not b.alive for b in made)


async def test_idle_unload_stops_the_worker(paths: Paths, catalog: ModelCatalog, settings: SettingsBox) -> None:
    install(paths, catalog, SMALL)
    backend = InProcessBackend(qw.FakeModel)
    q = provider(paths, catalog, settings, backend=backend, idle_unload_s=0.05)
    await collect(q, TtsRequest(text="Short.", model="qwen3-tts", voice="Aiden"))
    assert q.loaded()
    await asyncio.sleep(0.2)
    assert not q.loaded() and backend.closed
    await q.prewarm("qwen3-tts", "Aiden")  # opening a document loads it again, ready for the first sentence
    assert q.loaded() and backend.loads == 2
    await q.aclose()


async def test_clone_requests_send_the_reference(paths: Paths, catalog: ModelCatalog, settings: SettingsBox) -> None:
    install(paths, catalog, BASE_SMALL)
    ref = paths.home / "ref.wav"
    ref.write_bytes(b"RIFF")
    backend = InProcessBackend(qw.FakeModel)
    q = provider(paths, catalog, settings, backend=backend,
                 clones=Clones(CloneVoice("me", "Me", ref_audio=ref, ref_text="This is me.", ref_sha256="0" * 64)))
    audio = await collect(q, TtsRequest(text="Read this in my voice.", model="clone", voice="me"))
    assert audio.size > 0
    call = backend._model.calls[-1]
    assert call["ref_audio"] == str(ref) and call["ref_text"] == "This is me." and call["voice"] is None
    await q.aclose()


# ------------------------------------------------------------------ registry table


def registry(paths: Paths, settings: SettingsBox, *, fixture: bool = False, clones: Any = None) -> ProviderRegistry:
    def make_qwen(catalog: ModelCatalog, p: Paths, *, settings: Any = None, kv: Any = None,
                  clones: Any = None) -> Qwen3Tts:
        return Qwen3Tts(catalog, p, settings=settings, kv=kv, clones=clones,
                        backend_factory=lambda: InProcessBackend(qw.FakeModel), memory_gb=lambda: 16,
                        platform_ok=lambda: True, bundled_models=p.home / "none")

    env = make_env(open_speech_fixture_providers=fixture)
    r = ProviderRegistry(env, paths, ModelCatalog.load(paths), ProviderKeys(env), settings=settings,
                         factories=ProviderFactories(qwen3=make_qwen))
    if clones is not None:
        r.set_clone_store(clones)  # how cloning (workstream 8) plugs in
    return r


def test_registry_table_keeps_every_provider(paths: Paths, settings: SettingsBox) -> None:
    assert [e.id for e in TTS_PROVIDERS] == ["openai", "gemini", "google", "kokoro", "qwen3", "openai_compatible",
                                             "fixture"]
    assert {e.id for e in TTS_PROVIDERS if e.keyed} == {"openai", "gemini", "google"}
    assert {e.id for e in TTS_PROVIDERS if e.persistent} == {"kokoro", "qwen3"}
    r = registry(paths, settings, fixture=True)
    providers = list(dict.fromkeys(v.provider for v in r.voices()))
    assert providers == ["kokoro", "qwen3", "fixture"]  # no keys here: the keyed providers offer nothing


def test_clones_resolve_through_the_registry_and_survive_refresh(paths: Paths, settings: SettingsBox) -> None:
    ref = paths.home / "ref.wav"
    ref.write_bytes(b"RIFF")
    clones = Clones(CloneVoice("me", "Me", ref_audio=ref, ref_text="hi"))
    r = registry(paths, settings, clones=clones)
    install(paths, r.catalog, SMALL, BASE_SMALL)
    q = r.qwen3()
    prov, req = r.tts_for("qwen3:clone:me")
    assert prov is q and req.model == "clone" and req.voice == "me"
    assert r.is_local("qwen3:clone:me") and r.is_local("qwen3:qwen3-tts:Ryan")
    assert "qwen3:clone:me" in {v.id for v in r.voices()}
    with pytest.raises(NotFound):
        r.tts_for("qwen3:clone:nobody")
    with pytest.raises(NotFound):
        r.tts_for("qwen3:qwen3-tts:Nobody")
    r.refresh()
    assert r.qwen3() is q  # the worker (and its loaded model) is kept across refresh()


def test_an_undownloaded_qwen_voice_is_refused_with_its_size(paths: Paths, settings: SettingsBox) -> None:
    r = registry(paths, settings)
    with pytest.raises(EngineError) as err:
        r.tts_for("qwen3:qwen3-tts:Ryan")
    assert err.value.code == "invalid_request" and "Download the Qwen voices" in err.value.detail


async def test_a_failing_qwen_voice_reads_on_with_the_local_voice(paths: Paths, settings: SettingsBox,
                                                                  db: Database, events: Any) -> None:
    from .test_registry_synth import build

    def make_qwen(catalog: ModelCatalog, p: Paths, *, settings: Any = None, kv: Any = None,
                  clones: Any = None) -> Qwen3Tts:
        return Qwen3Tts(catalog, p, settings=settings, backend_factory=lambda: InProcessBackend(qw.FakeModel),
                        memory_gb=lambda: 16, platform_ok=lambda: True, bundled_models=p.home / "none")

    rt = build(paths, db, settings, events, factories=ProviderFactories(qwen3=make_qwen))
    install(paths, rt.registry.catalog, SMALL)
    res = await rt.synth.synth("__error__ in the model", "qwen3:qwen3-tts:Ryan")
    assert res.voice_id == "fixture:tone-v1:alto" and res.notice_reason == "failure"
    assert "Qwen isn't responding" in (res.notice or "")
    good = await rt.synth.synth("This one works.", "qwen3:qwen3-tts:Ryan")
    assert good.voice_id == "qwen3:qwen3-tts:Ryan" and good.audio.duration_s > 0
    again = await rt.synth.synth("This one works.", "qwen3:qwen3-tts:Ryan")
    assert again.cache_hit  # same text, voice, variant and language: the cached audio
    await rt.registry.aclose()


# ------------------------------------------------------------------ model manager


def test_manager_lists_installed_and_chosen_variants_only(paths: Paths, catalog: ModelCatalog) -> None:
    hw = HardwareInfo(system="Darwin", machine="arm64", apple_silicon=True, memory_gb=16)
    mgr = LocalModelManager(paths, catalog, hardware=hw, bundled_models=paths.home / "none")
    ids = {m.id for m in mgr.list(selected={SMALL})}
    assert SMALL in ids and LARGE not in ids and "kokoro-82m-v1.0" in ids
    install(paths, catalog, LARGE)
    rows = {m.id: m for m in mgr.list(selected={SMALL})}
    assert rows[LARGE].installed and rows[SMALL].selected and not rows[LARGE].selected
    assert rows[SMALL].name == "Qwen voices — 0.6B 8-bit" and rows[SMALL].family == "customvoice"
    assert rows[SMALL].size == "0.6b" and rows[SMALL].precision == "8bit" and rows[SMALL].ram_gb == 3.1
    assert rows[SMALL].fit == "fits" and rows[SMALL].size_mb == 1883
    every = mgr.list(all=True)
    assert len([m for m in every if m.provider == "qwen3"]) == 25
    tight = LocalModelManager(paths, catalog, hardware=HardwareInfo(system="Darwin", machine="arm64",
                                                                    apple_silicon=True, memory_gb=8))
    big = {m.id: m for m in tight.list(all=True)}["qwen3-tts-1.7b-customvoice-bf16"]
    assert big.fit == "slow"


async def test_install_reuses_files_another_variant_already_has(paths: Paths) -> None:
    import httpx

    blobs = {"config.json": b'{"tts_model_type":"custom_voice"}', "model.safetensors": b"weights-a" * 100,
             "speech_tokenizer/model.safetensors": b"codec" * 1000}
    other = {**blobs, "model.safetensors": b"weights-b" * 100}

    def entry(vid: str, data: dict[str, bytes]) -> str:
        files = ", ".join(f'{{ path = "{k}", sha256 = "{hashlib.sha256(v).hexdigest()}", size_mb = 0.1 }}'
                          for k, v in data.items())
        return (f'[local_weights.qwen3."{vid}"]\nfamily = "customvoice"\nsize = "0.6b"\nprecision = "8bit"\n'
                f'[local_weights.qwen3."{vid}".weights]\nurl = "hf://x/{vid}"\nrevision = "{"a" * 40}"\n'
                f'model_file = "model.safetensors"\nfiles = [{files}]\n')

    paths.models_toml.write_text(entry("v-a", blobs) + entry("v-b", other))
    catalog = ModelCatalog.load(paths)
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        name = request.url.path.split("/resolve/" + "a" * 40 + "/", 1)[1]
        data = blobs if "/v-a/" in request.url.path else other
        return httpx.Response(200, content=data[name])

    mgr = LocalModelManager(paths, catalog, hardware=HardwareInfo(system="Darwin", machine="arm64",
                                                                  apple_silicon=True, memory_gb=16),
                            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                            bundled_models=paths.home / "none")
    await mgr.install("v-a")
    assert len(requests) == 3
    d = await mgr.install("v-b")
    assert len(requests) == 4 and requests[-1].endswith("/v-b/resolve/" + "a" * 40 + "/model.safetensors")
    codec_a = paths.models / "qwen3" / "v-a" / "speech_tokenizer" / "model.safetensors"
    codec_b = d / "speech_tokenizer" / "model.safetensors"
    assert codec_b.read_bytes() == blobs["speech_tokenizer/model.safetensors"]
    assert codec_a.stat().st_ino == codec_b.stat().st_ino  # hard-linked, not stored twice
    manifest = json.loads((d / "manifest.json").read_text())
    assert manifest["variant"]["family"] == "customvoice" and manifest["pinned"]


# ------------------------------------------------------------------ install by voice


@pytest.mark.parametrize(("text", "language", "size"), [
    ("install the small Qwen voice", "en", "0.6b"),
    ("Download the big Qwen voices.", "en", "1.7b"),
    ("install qwen", "en", None),
    ("instala la voz Qwen pequeña", "es", "0.6b"),
    ("descarga las voces grandes de qwen", "es", "1.7b"),
])
def test_install_by_voice_intents(text: str, language: str, size: str | None) -> None:
    it = parse(text, language=language)
    assert it is not None and it.kind == "install_voice_model" and it.params == {"family": "customvoice",
                                                                                 "size": size}


# ------------------------------------------------------------------ catalog generation


def test_catalog_block_from_recorded_hugging_face_responses() -> None:
    data = json.loads((FIXTURES / "hf_qwen3_api.json").read_text())
    rec = pin.Recorder(replay=data)
    entries = pin.generate({SMALL, "qwen3-tts-1.7b-voicedesign-8bit"}, rec)
    assert [e["id"] for e in entries] == [SMALL, "qwen3-tts-1.7b-voicedesign-8bit"]
    small = entries[0]
    assert small["revision"] == "049ef77fe8816b536193c0c25f9a214d17921282" and small["family"] == "customvoice"
    assert "ryan" in small["speakers"] and "spanish" in small["languages"] and small["ram_gb"] == 3.1
    by = {f["path"]: f for f in small["files"]}
    assert ".gitattributes" not in by and "README.md" not in by
    assert by["model.safetensors"]["sha256"].startswith("3bcb2c4a127e")  # LFS hash from the tree listing
    cfg = data[f"{pin.HF}/mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit/resolve/{small['revision']}/config.json"]
    assert by["config.json"]["sha256"] == hashlib.sha256(cfg.encode()).hexdigest()  # small files hashed here
    block = pin.render_block(entries, today="2026-11-22")
    assert block.startswith(pin.BEGIN) and block.rstrip().endswith(pin.END)
    parsed = ModelCatalog.from_dict(__import__("tomllib").loads(block))
    v = parsed.weights_variant(SMALL)
    assert v is not None and v.weights is not None and v.weights.model_file == "model.safetensors"
    assert parsed.weights_variant("qwen3-tts-1.7b-voicedesign-8bit").family == "voicedesign"  # type: ignore[union-attr]
    # a repository whose config disagrees with its name is refused
    bad = json.loads(json.dumps(data))
    key = f"{pin.HF}/mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit/resolve/{small['revision']}/config.json"
    bad[key] = bad[key].replace('"custom_voice"', '"base"')
    with pytest.raises(ValueError, match="tts_model_type"):
        pin.generate({SMALL}, pin.Recorder(replay=bad))
    replaced = pin.replace_block("a = 1\n" + block + "b = 2\n", block.replace("2026-11-22", "2026-12-01"))
    assert replaced.count(pin.BEGIN) == 1 and "2026-12-01" in replaced and replaced.endswith("b = 2\n")


def test_bundled_catalog_block_matches_the_recording() -> None:
    """The committed models.toml pins the same commits the recording saw (re-run the tool to update both)."""
    data = json.loads((FIXTURES / "hf_qwen3_api.json").read_text())
    catalog = ModelCatalog.load()
    for vid, repo in ((SMALL, "Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit"),
                      ("qwen3-tts-1.7b-voicedesign-8bit", "Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit")):
        v = catalog.weights_variant(vid)
        assert v is not None and v.weights is not None
        assert v.weights.revision == data[f"{pin.HF}/api/models/mlx-community/{repo}"]["sha"]


def test_worker_frames_round_trip() -> None:
    import io

    pcm = np.linspace(-1, 1, 480, dtype=np.float32)
    buf = io.BytesIO(qw.encode_frame({"op": "chunk", "id": 7, "sr": 24000}, pcm.astype("<f4").tobytes())
                     + qw.encode_frame({"op": "done", "id": 7}))
    h, payload = qw.read_frame(buf)  # type: ignore[misc]
    assert h == {"op": "chunk", "id": 7, "sr": 24000, "bytes": 1920}
    assert np.array_equal(np.frombuffer(payload, dtype="<f4"), pcm)
    assert qw.read_frame(buf) == ({"op": "done", "id": 7}, b"")
    assert qw.read_frame(buf) is None
    assert qw.max_tokens_for("Hi.") == 75 and qw.max_tokens_for("x" * 200) == int(np.ceil((3 + 40) * 12.5))

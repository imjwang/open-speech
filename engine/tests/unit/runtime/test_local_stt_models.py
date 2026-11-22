"""Local speech-to-text in the catalog, the model manager and the registry (no weights, no network)."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from open_speech.config import Paths
from open_speech.errors import EngineError, NotFound
from open_speech.local.catalog import ModelCatalog
from open_speech.local.manager import HardwareInfo, LocalModelManager
from open_speech.providers.fixture import FixtureStt
from open_speech.providers.keys import ProviderKeys
from open_speech.providers.local_stt import LocalStt
from open_speech.providers.registry import ProviderFactories, ProviderRegistry
from open_speech.service.events import EventBus

from .helpers import SettingsBox, make_env
from .test_local_stt import STT_MODEL, Factory, install_fake_stt_weights

HW = HardwareInfo(system="Darwin", machine="arm64", apple_silicon=True, chip="Apple M4", generation=4, memory_gb=16)
REPO = "csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
REVISION = "ed5d745623472f97cfc2e6c2916ee248798fc5a8"

# ------------------------------------------------------------------ catalog


def test_bundled_catalog_declares_a_pinned_default(catalog: ModelCatalog) -> None:
    models = catalog.local_stt_models()
    assert [m.model for m in models] == [STT_MODEL]
    m = catalog.default_local_stt_model()
    assert m is models[0] and catalog.local_stt_model(STT_MODEL) is m
    assert catalog.local_stt_model(f"local:{STT_MODEL}") is m
    assert m.provider == "local" and m.family == "nemo_transducer" and m.license == "CC-BY-4.0"
    assert {"en", "es"} <= set(m.languages) and len(m.languages) == 25
    assert m.ram_gb == 2.0 and m.size_mb == 642 and m.num_threads == 2
    w = m.weights
    assert w is not None and w.base_url() == f"https://huggingface.co/{REPO}/resolve/{REVISION}"
    files = w.all_files()
    assert [f.path for f in files] == [m.encoder, m.decoder, m.joiner, m.tokens, m.vad] == m.required_files()
    assert all(re.fullmatch(r"[0-9a-f]{64}", f.sha256) for f in files)  # every file pinned
    assert sum(f.size_mb for f in files) == pytest.approx(w.size_mb, abs=1)
    vad = files[-1]
    assert vad.url is not None and re.search(r"/resolve/[0-9a-f]{40}/silero_vad_v5\.onnx$", vad.url)  # pinned too
    assert all(f.url is None for f in files[:-1])


def test_cloud_stt_entries_and_voice_models_are_untouched(catalog: ModelCatalog) -> None:
    openai, gemini = catalog.stt_for("openai"), catalog.stt_for("gemini")
    assert openai is not None and openai.batch[0] == "gpt-4o-transcribe"
    assert gemini is not None and gemini.batch == ["gemini-3.5-transcribe"]
    assert catalog.stt_for("local") is None  # local models are `local_stt_models()`, not a cloud-style entry
    assert STT_MODEL not in {m.model for m in catalog.local_models()}  # those are voices
    assert catalog.local_model(STT_MODEL) is None and catalog.local_stt_model("kokoro-82m-v1.0") is None


def test_user_catalog_can_add_a_model_and_change_the_default(paths: Paths) -> None:
    paths.models_toml.write_text(
        '[stt.local]\ndefault = "my-transducer"\n\n'
        '[stt.local."my-transducer"]\nname = "Mine"\nfamily = "transducer"\njoiner = "j.onnx"\nnum_threads = 4\n\n'
        f'[stt.local."{STT_MODEL}"]\nnum_threads = 1\n'
    )
    cat = ModelCatalog.load(paths)
    assert [m.model for m in cat.local_stt_models()] == ["my-transducer", STT_MODEL]  # the default comes first
    mine = cat.default_local_stt_model()
    assert mine is not None and mine.family == "transducer" and mine.weights is None
    assert mine.required_files() == ["encoder.int8.onnx", "decoder.int8.onnx", "j.onnx", "tokens.txt"]
    bundled = cat.local_stt_model(STT_MODEL)
    assert bundled is not None and bundled.num_threads == 1 and bundled.weights is not None  # deep-merged
    # a default that names nothing falls back to the first entry
    paths.models_toml.write_text('[stt.local]\ndefault = "gone"\n')
    fallback = ModelCatalog.load(paths).default_local_stt_model()
    assert fallback is not None and fallback.model == STT_MODEL


# ------------------------------------------------------------------ model manager

BLOBS = {
    "encoder.int8.onnx": b"E" * 70_000,
    "decoder.int8.onnx": b"D" * 3_000,
    "joiner.int8.onnx": b"J" * 2_000,
    "tokens.txt": b"<blk> 0\n",
    "silero_vad.onnx": b"V" * 5_000,
}
VAD_URL = "https://vad.test/models/silero_vad_v5.onnx"


def tiny_catalog(paths: Paths) -> ModelCatalog:
    """The bundled catalog with the speech-to-text weights swapped for a few kilobytes, pinned the same way."""
    raw = copy.deepcopy(ModelCatalog.load(paths).raw)
    raw["stt"]["local"][STT_MODEL]["weights"]["files"] = [
        {"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size_mb": len(data) / 1024**2,
         **({"url": VAD_URL} if name == "silero_vad.onnx" else {})}
        for name, data in BLOBS.items()
    ]
    return ModelCatalog.from_dict(raw)


class Hub:
    """Fake download server: the model repo on one host, the VAD file on another; honours Range."""

    def __init__(self, blobs: dict[str, bytes] | None = None) -> None:
        self.blobs = dict(blobs or BLOBS)
        self.requests: list[tuple[str, str | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append((url, request.headers.get("range")))
        if url == VAD_URL:
            data = self.blobs["silero_vad.onnx"]
        else:
            prefix = f"https://hf.test/{REPO}/resolve/{REVISION}/"
            name = url.removeprefix(prefix)
            if not url.startswith(prefix) or name not in self.blobs or name == "silero_vad.onnx":
                return httpx.Response(404)
            data = self.blobs[name]
        rng = request.headers.get("range")
        if rng:
            start = int(rng.removeprefix("bytes=").rstrip("-"))
            return httpx.Response(206, content=data[start:])
        return httpx.Response(200, content=data)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


def make_manager(paths: Paths, catalog: ModelCatalog, hub: Hub | None = None, **kw: Any) -> LocalModelManager:
    hub = hub or Hub()
    return LocalModelManager(paths, catalog, hardware=HW, client_factory=hub.client, hf_base="https://hf.test",
                             chunk_size=1 << 12, **kw)


def test_manager_lists_speech_models_next_to_voices(catalog: ModelCatalog, paths: Paths) -> None:
    rows = {m.id: m for m in make_manager(paths, catalog).list()}
    stt = rows[STT_MODEL]
    assert (stt.kind, stt.provider, stt.name) == ("stt", "local", "Parakeet speech recognition")
    assert (stt.size_mb, stt.ram_gb, stt.license, stt.fit) == (642, 2.0, "CC-BY-4.0", "fits")
    assert not stt.installed and stt.status == "~25x real time on CPU"
    assert rows["kokoro-82m-v1.0"].kind == "tts" and rows["qwen3-tts-12hz-0.6b"].kind == "tts"
    with pytest.raises(NotFound):
        make_manager(paths, catalog).entry("no-such-model")
    small = HardwareInfo(system="Darwin", machine="arm64", apple_silicon=True, memory_gb=3)
    tight = {m.id: m for m in LocalModelManager(paths, catalog, hardware=small).list()}[STT_MODEL]
    assert tight.fit == "slow" and tight.status == "Uses about 2 GB of your 3 GB; it may be slow."


async def test_install_verifies_pins_and_reports_progress(paths: Paths, events: EventBus,
                                                          captured: list[dict[str, Any]]) -> None:
    cat, hub, changed = tiny_catalog(paths), Hub(), []
    mgr = make_manager(paths, cat, hub, events=events, on_change=changed.append)
    d = await mgr.install(STT_MODEL)
    assert d == paths.models / "stt" / STT_MODEL
    assert {p.name: p.read_bytes() for p in d.iterdir() if p.name != "manifest.json"} == BLOBS
    # one request per file; the VAD file came from its own pinned URL, not from the model repository
    assert [u.rsplit("/", 1)[-1] for u, _ in hub.requests] == [*list(BLOBS)[:4], "silero_vad_v5.onnx"]
    assert hub.requests[-1][0] == VAD_URL
    manifest = json.loads((d / "manifest.json").read_text())
    assert manifest["model"] == STT_MODEL and manifest["kind"] == "stt" and manifest["pinned"] is True
    assert set(manifest["files"]) == set(BLOBS) and all(f["pinned"] for f in manifest["files"].values())
    progress = [m for m in captured if m["type"] == "job.progress"]
    assert progress[-1]["pct"] == 100.0 and progress[-1]["label"] == "Parakeet speech recognition is installed"
    assert any("Downloading Parakeet speech recognition" in m["label"] for m in progress)
    assert changed == [STT_MODEL]
    row = next(m for m in mgr.list() if m.id == STT_MODEL)
    assert row.installed and row.status == "Installed" and row.kind == "stt"
    # the provider finds the same directory
    stt = LocalStt(cat, paths, recognizer_factory=Factory())
    assert stt.installed() and stt.model_dir() == d


async def test_install_resumes_a_partial_download(paths: Paths) -> None:
    cat, hub = tiny_catalog(paths), Hub()
    d = paths.models / "stt" / STT_MODEL
    d.mkdir(parents=True)
    (d / "encoder.int8.onnx.part").write_bytes(BLOBS["encoder.int8.onnx"][:10_000])
    await make_manager(paths, cat, hub).install(STT_MODEL)
    assert hub.requests[0][1] == "bytes=10000-"
    assert (d / "encoder.int8.onnx").read_bytes() == BLOBS["encoder.int8.onnx"]
    assert not (d / "encoder.int8.onnx.part").exists()


async def test_install_rejects_a_file_that_misses_its_pin(paths: Paths, events: EventBus,
                                                         captured: list[dict[str, Any]]) -> None:
    cat = tiny_catalog(paths)
    hub = Hub({**BLOBS, "decoder.int8.onnx": b"tampered"})
    mgr = make_manager(paths, cat, hub, events=events)
    with pytest.raises(EngineError, match="didn't match its checksum"):
        await mgr.install(STT_MODEL)
    d = paths.models / "stt" / STT_MODEL
    assert not (d / "decoder.int8.onnx").exists() and not mgr.installed(STT_MODEL)
    assert not LocalStt(cat, paths, recognizer_factory=Factory()).installed()
    row = next(m for m in mgr.list() if m.id == STT_MODEL)
    assert not row.installed and row.status.startswith("Install failed")
    assert any(m["type"] == "error" for m in captured)
    # a second try with the right bytes completes, reusing the file that was already verified
    hub.blobs = dict(BLOBS)
    await mgr.install(STT_MODEL)
    assert mgr.installed(STT_MODEL)


async def test_remove_deletes_the_app_data_copy(paths: Paths) -> None:
    cat, changed = tiny_catalog(paths), []
    mgr = make_manager(paths, cat, on_change=changed.append)
    assert mgr.remove(STT_MODEL) is False
    d = await mgr.install(STT_MODEL)
    assert mgr.remove(STT_MODEL) is True and not d.exists() and not mgr.installed(STT_MODEL)
    assert changed == [STT_MODEL, STT_MODEL]


async def test_bundled_copy_counts_as_installed_and_is_never_touched(catalog: ModelCatalog, paths: Paths,
                                                                    tmp_path: Path) -> None:
    def no_network() -> httpx.AsyncClient:
        raise AssertionError("bundled weights must not trigger a download")

    runtime = Paths(tmp_path / "runtime")
    bundled = install_fake_stt_weights(runtime, catalog)
    assert bundled == tmp_path / "runtime" / "models" / "stt" / STT_MODEL
    mgr = LocalModelManager(paths, catalog, hardware=HW, client_factory=no_network, bundled_models=runtime.models)
    row = next(m for m in mgr.list() if m.id == STT_MODEL)
    assert row.installed and row.status == "Included with the app"
    assert await mgr.ensure_installed(STT_MODEL) == bundled
    assert mgr.remove(STT_MODEL) is False and (bundled / "encoder.int8.onnx").exists()
    assert not (paths.models / "stt").exists()
    (bundled / "silero_vad.onnx").unlink()  # an incomplete bundle is not an install
    assert not mgr.installed(STT_MODEL)


# ------------------------------------------------------------------ registry


def build_registry(paths: Paths, settings: SettingsBox, *, fixture: bool = False, **env: Any) -> ProviderRegistry:
    class Tagged(FixtureStt):
        def __init__(self, tag: str) -> None:
            super().__init__()
            self.tag = tag

    e = make_env(open_speech_fixture_providers=fixture, **env)
    catalog = ModelCatalog.load(paths)
    factories = ProviderFactories(
        openai_stt=lambda c, k: Tagged("openai"),
        gemini_stt=lambda c, k: Tagged("gemini"),
        local_stt=lambda c, p: LocalStt(c, p, recognizer_factory=Factory()),
    )
    return ProviderRegistry(e, paths, catalog, ProviderKeys(e), settings=settings, factories=factories)


def test_auto_prefers_local_then_openai_then_gemini(paths: Paths, settings: SettingsBox) -> None:
    keys = {"openai_api_key": "sk-x", "gemini_api_key": "AIza-x"}
    r = build_registry(paths, settings, **keys)
    assert r.stt().tag == "openai"  # type: ignore[union-attr]  # no weights yet: the cloud order applies
    assert build_registry(paths, settings, gemini_api_key="AIza-x").stt().tag == "gemini"  # type: ignore[union-attr]
    install_fake_stt_weights(paths, r.catalog)
    stt = r.stt()
    assert isinstance(stt, LocalStt) and stt.id == "local" and stt.model == STT_MODEL
    assert r.stt() is stt and r.local_stt() is stt  # one cached adapter
    assert isinstance(build_registry(paths, settings).stt(), LocalStt)  # and no key is needed at all


def test_pinned_provider_is_respected(paths: Paths, settings: SettingsBox) -> None:
    r = build_registry(paths, settings, openai_api_key="sk-x")
    install_fake_stt_weights(paths, r.catalog)
    settings.set("voice_input.stt_provider", "openai")
    assert r.stt().tag == "openai"  # type: ignore[union-attr]
    settings.set("voice_input.stt_provider", "gemini")
    assert r.stt() is None
    settings.set("voice_input.stt_provider", "local")
    assert isinstance(r.stt(), LocalStt)
    (paths.models / "stt" / STT_MODEL / "tokens.txt").unlink()
    assert r.stt() is None  # pinned to local: no silent fall-through to a cloud provider


def test_local_needs_its_runtime(paths: Paths, settings: SettingsBox, monkeypatch: pytest.MonkeyPatch) -> None:
    r = build_registry(paths, settings, openai_api_key="sk-x")
    install_fake_stt_weights(paths, r.catalog)
    monkeypatch.setattr(r.local_stt(), "runtime_available", lambda: False)
    assert r.stt().tag == "openai"  # type: ignore[union-attr]


def test_fixture_mode_never_routes_to_local(paths: Paths, settings: SettingsBox) -> None:
    """Hermetic runs must not depend on weights that happen to be installed on the machine."""
    r = build_registry(paths, settings, fixture=True)
    install_fake_stt_weights(paths, r.catalog)
    assert type(r.stt()) is FixtureStt
    settings.set("voice_input.stt_provider", "local")
    assert type(r.stt()) is FixtureStt
    settings.set("voice_input.stt_provider", "auto")
    keyed = build_registry(paths, settings, fixture=True, openai_api_key="sk-x")
    assert keyed.stt().tag == "openai"  # type: ignore[union-attr]  # as before: keys first, fixture last


def test_adapter_explains_itself_when_nothing_can_listen(paths: Paths, settings: SettingsBox) -> None:
    """What the registry needs to say why the bar can't listen, and which download would fix it."""
    r = build_registry(paths, settings)
    assert r.stt() is None
    local = r.local_stt()
    assert local.model == STT_MODEL and not local.installed() and local.runtime_available()
    reason = local.unavailable_reason()
    assert reason == "Download Parakeet speech recognition (about 642 MB) to use speech input on this Mac."
    assert "key" not in reason.lower()


async def test_refresh_keeps_the_loaded_model_until_its_weights_go(paths: Paths, settings: SettingsBox) -> None:
    from .test_local_stt import collect, speech_pcm

    r = build_registry(paths, settings)
    d = install_fake_stt_weights(paths, r.catalog)
    stt = r.stt()
    assert isinstance(stt, LocalStt)
    await collect(stt, speech_pcm(0.5))
    assert stt.loaded()
    r.refresh()  # a key changed, or another model was installed
    assert r.stt() is stt and stt.loaded()
    (d / "encoder.int8.onnx").unlink()  # the model manager removed the weights, then calls refresh()
    r.refresh()
    assert not stt.loaded() and r.stt() is None

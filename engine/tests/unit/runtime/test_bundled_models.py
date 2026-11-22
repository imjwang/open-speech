"""Kokoro weights shipped read-only in the packaged app (local/bundled.py, FR-SETUP-3)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from open_speech.config import Paths
from open_speech.local.bundled import BUNDLED_RUNTIME_ENV, bundled_models_dir
from open_speech.local.catalog import ModelCatalog
from open_speech.local.manager import HardwareInfo, LocalModelManager

from .test_kokoro import MODEL, install_fake_weights, make_kokoro

HW = HardwareInfo(system="Darwin", machine="arm64", apple_silicon=True, chip="Apple M4", generation=4, memory_gb=16)


@pytest.fixture
def runtime(tmp_path: Path) -> Path:
    """A packaged runtime dir with Kokoro under `<runtime>/models/kokoro/<model>/`."""
    root = tmp_path / "runtime"
    install_fake_weights(Paths(root))
    return root


def _no_network() -> httpx.AsyncClient:
    raise AssertionError("bundled weights must not trigger a download")


def test_bundled_models_dir_from_env(runtime: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(BUNDLED_RUNTIME_ENV, raising=False)
    assert bundled_models_dir() is None
    assert bundled_models_dir({BUNDLED_RUNTIME_ENV: str(tmp_path / "missing")}) is None
    monkeypatch.setenv(BUNDLED_RUNTIME_ENV, str(runtime))
    assert bundled_models_dir() == runtime / "models"


def test_kokoro_uses_bundled_weights(catalog: ModelCatalog, paths: Paths, runtime: Path) -> None:
    tts, _ = make_kokoro(catalog, paths, bundled_models=runtime / "models")
    assert tts.installed(MODEL)
    assert tts.model_dir(MODEL) == runtime / "models" / "kokoro" / MODEL
    heart = next(v for v in tts.voices() if v.id.endswith(":af_heart"))
    assert heart.available and heart.unavailable_reason is None
    # Voices not shipped are reported per voice, as for a downloaded copy.
    assert not next(v for v in tts.voices() if v.id.endswith(":bm_george")).available


def test_app_data_copy_wins_over_bundled(catalog: ModelCatalog, paths: Paths, runtime: Path) -> None:
    user = install_fake_weights(paths)
    tts, _ = make_kokoro(catalog, paths, bundled_models=runtime / "models")
    assert tts.model_dir(MODEL) == user


async def test_manager_reports_bundled_copy_and_never_touches_it(
    catalog: ModelCatalog, paths: Paths, runtime: Path
) -> None:
    mgr = LocalModelManager(paths, catalog, hardware=HW, client_factory=_no_network, bundled_models=runtime / "models")
    bundled = runtime / "models" / "kokoro" / MODEL
    assert mgr.installed(MODEL)
    row = next(m for m in mgr.list() if m.id == MODEL)
    assert row.installed and row.status == "Included with the app"
    assert await mgr.ensure_installed(MODEL) == bundled
    assert mgr.remove(MODEL) is False
    assert (bundled / "onnx" / "model_quantized.onnx").exists()
    assert not (paths.models / "kokoro" / MODEL).exists()


def test_manager_without_bundle_is_unchanged(catalog: ModelCatalog, paths: Paths, tmp_path: Path) -> None:
    mgr = LocalModelManager(paths, catalog, hardware=HW, bundled_models=tmp_path / "nothing-here")
    assert not mgr.installed(MODEL)
    assert next(m for m in mgr.list() if m.id == MODEL).status == "~10x real time on CPU"

"""Local model manager (08 § Local models, FR-SETUP-3, FR-VOICE-5/6, NFR-9).

- Hardware probe: Apple Silicon generation and unified memory via `sysctl` on macOS; elsewhere a generic
  report (OS, arch, RAM). Each catalog entry gets a fit badge: fits | slow | unsupported.
- Install is a job: files download with HTTP Range resume (`*.part`), are sha256-verified against the
  catalog pins, and land in `models/<provider>/<model>/`. Progress goes out as `job.progress` events.
  Files without a pin are accepted trust-on-first-use: the computed hash is recorded in `manifest.json`
  and the model's status says "unpinned".
- Remove deletes the model directory. Qwen3-TTS (P2) is listed as unsupported/later.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel

from open_speech.api.schemas import LocalModel
from open_speech.api.ws_schema import ErrorMsg, JobProgress
from open_speech.config import Paths
from open_speech.errors import EngineError, NotFound
from open_speech.ids import new_id
from open_speech.local.catalog import ModelCatalog, TtsModel, WeightFile
from open_speech.service.events import EventBus

log = logging.getLogger(__name__)
Fit = Literal["fits", "slow", "unsupported"]


class HardwareInfo(BaseModel):
    system: str
    machine: str
    apple_silicon: bool = False
    chip: str | None = None
    generation: int | None = None  # M1 → 1, M2 → 2, …
    memory_gb: float = 0.0
    cpu_count: int = 1

    def summary(self) -> str:
        if self.apple_silicon and self.chip:
            return f"{self.chip}, {self.memory_gb:.0f} GB unified memory"
        return f"{self.system} {self.machine}, {self.memory_gb:.0f} GB RAM"


def _sysctl(name: str, run: Callable[..., Any]) -> str | None:
    try:
        out = run(["sysctl", "-n", name], capture_output=True, text=True, timeout=2, check=False)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def probe_hardware(*, run: Callable[..., Any] = subprocess.run, system: str | None = None,
                   machine: str | None = None) -> HardwareInfo:
    system = system or platform.system()
    machine = machine or platform.machine()
    info = HardwareInfo(system=system, machine=machine, cpu_count=os.cpu_count() or 1)
    if system == "Darwin":
        brand = _sysctl("machdep.cpu.brand_string", run)
        mem = _sysctl("hw.memsize", run)
        if mem and mem.isdigit():
            info.memory_gb = round(int(mem) / 1024**3, 1)
        if machine == "arm64" or (brand and brand.startswith("Apple")):
            info.apple_silicon = True
            info.chip = brand
            m = re.search(r"\bM(\d+)\b", brand or "")
            info.generation = int(m.group(1)) if m else None
        return info
    with contextlib.suppress(ValueError, OSError, AttributeError):
        info.memory_gb = round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3, 1)
    return info


def fit_for(entry: TtsModel, hw: HardwareInfo) -> tuple[Fit, str]:
    if entry.status == "later":
        return "unsupported", "Planned for a later release."
    if entry.requires == "apple-silicon" and not hw.apple_silicon:
        return "unsupported", "Needs an Apple Silicon Mac."
    if hw.memory_gb and entry.ram_gb:
        if entry.ram_gb > hw.memory_gb * 0.8:
            return "unsupported", f"Needs about {entry.ram_gb:g} GB of memory."
        if entry.ram_gb > hw.memory_gb * 0.5:
            return "slow", f"Uses about {entry.ram_gb:g} GB of your {hw.memory_gb:.0f} GB; it may be slow."
    return "fits", entry.speed or ""


class _Job:
    def __init__(self, job_id: str, model: str) -> None:
        self.id = job_id
        self.model = model
        self.pct = 0.0
        self.label = "Starting"
        self.state: Literal["running", "done", "failed"] = "running"
        self.error: str | None = None
        self.task: asyncio.Task[Path] | None = None


class LocalModelManager:
    def __init__(
        self,
        paths: Paths,
        catalog: ModelCatalog,
        events: EventBus | None = None,
        *,
        hardware: HardwareInfo | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        hf_base: str = "https://huggingface.co",
        on_change: Callable[[str], None] | None = None,
        chunk_size: int = 1 << 16,
    ) -> None:
        self.paths = paths
        self.catalog = catalog
        self.events = events
        self._hw = hardware
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0, read=60.0))
        )
        self._hf = hf_base
        self._on_change = on_change
        self._chunk = chunk_size
        self._jobs: dict[str, _Job] = {}
        self._by_model: dict[str, _Job] = {}

    # ------------------------------------------------------------------ queries

    @property
    def hardware(self) -> HardwareInfo:
        if self._hw is None:
            self._hw = probe_hardware()
        return self._hw

    def entry(self, model_id: str) -> TtsModel:
        e = self.catalog.local_model(model_id)
        if e is None:
            raise NotFound(f"a local model called {model_id}")
        return e

    def model_dir(self, entry: TtsModel) -> Path:
        return self.paths.models / entry.provider / entry.model

    def _files(self, entry: TtsModel) -> list[WeightFile]:
        w = entry.weights
        if w is None:
            return []
        files = w.all_files()
        if w.voice_file:
            files += [WeightFile(path=w.voice_file.format(voice=v), size_mb=w.voice_size_mb) for v in entry.voices]
        return files

    def manifest(self, entry: TtsModel) -> dict[str, Any] | None:
        p = self.model_dir(entry) / "manifest.json"
        if not p.exists():
            return None
        try:
            data: dict[str, Any] = json.loads(p.read_text("utf-8"))
            return data
        except json.JSONDecodeError:
            return None

    def installed(self, model_id: str) -> bool:
        entry = self.entry(model_id)
        if entry.weights is None:
            return False
        d = self.model_dir(entry)
        return all((d / f.path).exists() for f in entry.weights.all_files())

    def list(self) -> list[LocalModel]:
        out: list[LocalModel] = []
        hw = self.hardware
        for entry in self.catalog.local_models():
            fit, note = fit_for(entry, hw)
            installed = entry.weights is not None and self.installed(entry.model)
            job = self._by_model.get(entry.model)
            if job is not None and job.state == "running":
                status = f"Downloading, {job.pct:.0f}%"
            elif installed:
                man = self.manifest(entry) or {}
                status = "Installed" + ("" if man.get("pinned", False) else " (sha256 not pinned)")
            elif job is not None and job.state == "failed":
                status = f"Install failed: {job.error}"
            else:
                status = note or "Not installed"
            out.append(
                LocalModel(
                    id=entry.model,
                    provider=entry.provider,
                    name=entry.name or entry.model,
                    size_mb=int(round(entry.size_mb)),
                    ram_gb=entry.ram_gb,
                    license=entry.license or "unknown",
                    installed=installed,
                    fit=fit,
                    status=status,
                )
            )
        return out

    def job(self, job_id: str) -> dict[str, Any] | None:
        j = self._jobs.get(job_id)
        if j is None:
            return None
        return {"job_id": j.id, "model": j.model, "pct": j.pct, "label": j.label, "state": j.state, "error": j.error}

    # ------------------------------------------------------------------ install / remove

    def start_install(self, model_id: str) -> str:
        """Start (or join) an install job on the running loop; returns the job id."""
        entry = self.entry(model_id)
        running = self._by_model.get(entry.model)
        if running is not None and running.state == "running":
            return running.id
        job = _Job(new_id("job"), entry.model)
        self._jobs[job.id] = job
        self._by_model[entry.model] = job
        job.task = asyncio.get_running_loop().create_task(self._install(entry, job))
        job.task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        return job.id

    async def install(self, model_id: str) -> Path:
        """Install and wait (CLI, first use)."""
        job_id = self.start_install(model_id)
        task = self._jobs[job_id].task
        assert task is not None
        return await asyncio.shield(task)

    async def ensure_installed(self, model_id: str) -> Path:
        entry = self.entry(model_id)
        if self.installed(entry.model):
            return self.model_dir(entry)
        return await self.install(entry.model)

    def remove(self, model_id: str) -> bool:
        entry = self.entry(model_id)
        d = self.model_dir(entry)
        if not d.exists():
            return False
        shutil.rmtree(d)
        if self._on_change is not None:
            self._on_change(entry.model)
        return True

    async def _install(self, entry: TtsModel, job: _Job) -> Path:
        if entry.status == "later" or entry.weights is None:
            err = EngineError("invalid_request", f"{entry.name or entry.model} can't be installed yet.")
            self._fail(job, err)
            raise err
        fit, note = fit_for(entry, self.hardware)
        if fit == "unsupported":
            err = EngineError("invalid_request", note)
            self._fail(job, err)
            raise err
        d = self.model_dir(entry)
        d.mkdir(parents=True, exist_ok=True)
        files = self._files(entry)
        expected = [int(f.size_mb * 1024 * 1024) for f in files]
        total = max(1, sum(expected))
        done_bytes = 0
        manifest_files: dict[str, Any] = {}
        all_pinned = True
        base = entry.weights.base_url(self._hf)
        name = entry.name or entry.model
        try:
            async with self._client_factory() as client:
                for f, exp in zip(files, expected, strict=True):
                    def progress(n: int, _base: int = done_bytes) -> None:
                        self._progress(job, min(99.0, (_base + n) * 100 / total),
                                       f"Downloading {name} ({(_base + n) / 1024**2:.0f} of {total / 1024**2:.0f} MB)")

                    digest, size = await self._download(client, f"{base}/{f.path}", d / f.path, progress)
                    if f.sha256:
                        if digest.lower() != f.sha256.lower():
                            with contextlib.suppress(FileNotFoundError):
                                (d / f.path).unlink()
                            raise EngineError(
                                "provider_error",
                                f"The download of {f.path} didn't match its checksum, so I deleted it. Try again.",
                            )
                    else:
                        all_pinned = False
                    manifest_files[f.path] = {"sha256": digest, "bytes": size, "pinned": bool(f.sha256)}
                    done_bytes += max(size, exp)
        except EngineError as exc:
            self._fail(job, exc)
            raise
        except Exception as exc:
            err = EngineError(
                "offline", f"I couldn't download {name} ({type(exc).__name__}). Check your connection and try again."
            )
            self._fail(job, err)
            raise err from exc
        (d / "manifest.json").write_text(
            json.dumps(
                {
                    "model": entry.model,
                    "provider": entry.provider,
                    "installed_at": datetime.now(UTC).isoformat(),
                    "pinned": all_pinned,
                    "files": manifest_files,
                },
                indent=2,
            ),
            "utf-8",
        )
        job.state = "done"
        self._progress(job, 100.0, f"{name} is installed", force=True)
        if self._on_change is not None:
            self._on_change(entry.model)
        return d

    async def _download(
        self, client: httpx.AsyncClient, url: str, target: Path, progress: Callable[[int], None]
    ) -> tuple[str, int]:
        """Download with Range resume into `target.part`, then verify-and-rename. Returns (sha256, bytes)."""
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            return await asyncio.to_thread(_sha256, target), target.stat().st_size
        part = target.with_name(target.name + ".part")
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        async with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code == 416 and have:
                pass  # the part file is already complete
            elif resp.status_code == 206 and have:
                with part.open("ab") as fh:
                    async for chunk in resp.aiter_bytes(self._chunk):
                        fh.write(chunk)
                        have += len(chunk)
                        progress(have)
            elif resp.status_code == 200:
                have = 0
                with part.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(self._chunk):
                        fh.write(chunk)
                        have += len(chunk)
                        progress(have)
            else:
                raise EngineError("offline", f"The download server answered {resp.status_code} for {target.name}.")
        digest = await asyncio.to_thread(_sha256, part)
        os.replace(part, target)
        return digest, target.stat().st_size

    # ------------------------------------------------------------------ events

    def _progress(self, job: _Job, pct: float, label: str, *, force: bool = False) -> None:
        now = time.monotonic()
        last = getattr(job, "_last_emit", 0.0)
        if not force and pct - job.pct < 1.0 and now - last < 0.25:
            return
        job.pct = round(pct, 1)
        job.label = label
        job._last_emit = now  # type: ignore[attr-defined]
        if self.events is not None:
            self.events.publish(JobProgress(job_id=job.id, pct=job.pct, label=label))

    def _fail(self, job: _Job, err: EngineError) -> None:
        job.state = "failed"
        job.error = err.detail
        if self.events is not None:
            self.events.publish(JobProgress(job_id=job.id, pct=job.pct, label=f"Install failed: {err.detail}"))
            self.events.publish(ErrorMsg(problem={**err.problem(), "job_id": job.id}))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()

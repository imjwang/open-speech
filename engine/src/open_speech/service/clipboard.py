"""Clipboard access for "read my clipboard" (FR-READ-5, FR-MM-1).

The shell's ⌥⇧R hotkey reads the clipboard itself (Electron) and posts text/image. This reader serves the
voice and CLI paths, where the engine is asked directly. macOS uses `pbpaste` for text and AppleScript for
images (no permission prompts). In fixture mode the clipboard is a file in app data so tests are
deterministic.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ClipboardContent:
    text: str | None = None
    image_png: bytes | None = None


class ClipboardReader:
    def __init__(self, fixture_dir: Path | None = None) -> None:
        self.fixture_dir = fixture_dir

    async def read(self) -> ClipboardContent:
        if self.fixture_dir is not None:
            return self._read_fixture()
        if sys.platform == "darwin":
            img = await self._mac_image()
            if img:
                return ClipboardContent(image_png=img)
            return ClipboardContent(text=await self._run(["pbpaste"]))
        for cmd in (["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"], ["xsel", "-ob"]):
            if shutil.which(cmd[0]):
                return ClipboardContent(text=await self._run(cmd))
        return ClipboardContent()

    def _read_fixture(self) -> ClipboardContent:
        assert self.fixture_dir is not None
        png = self.fixture_dir / "clipboard.png"
        txt = self.fixture_dir / "clipboard.txt"
        if png.exists():
            return ClipboardContent(image_png=png.read_bytes())
        if txt.exists():
            return ClipboardContent(text=txt.read_text("utf-8"))
        return ClipboardContent()

    @staticmethod
    async def _run(cmd: list[str]) -> str | None:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await proc.communicate()
        return out.decode("utf-8", "replace") if proc.returncode == 0 else None

    @staticmethod
    async def _mac_image() -> bytes | None:
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "clip.png"
            script = (
                "try\n"
                "  set png to (the clipboard as «class PNGf»)\n"
                f'  set f to open for access POSIX file "{target}" with write permission\n'
                "  write png to f\n"
                "  close access f\n"
                "on error\n"
                '  return "none"\n'
                "end try"
            )
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            await proc.communicate()
            if target.exists() and target.stat().st_size > 0:
                return target.read_bytes()
        return None

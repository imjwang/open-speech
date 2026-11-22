"""settings.toml as a service (04 `GET/PATCH /settings`, FR-ASST-3, FR-SETUP-6)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from open_speech.config import EngineEnv, Paths, UserSettings, load_user_settings, save_user_settings
from open_speech.errors import EngineError


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class SettingsService:
    def __init__(self, paths: Paths, env: EngineEnv) -> None:
        self.paths = paths
        self._settings = load_user_settings(paths.settings_toml, env)
        if not paths.settings_toml.exists():
            save_user_settings(paths.settings_toml, self._settings)
        self._mtime = paths.settings_toml.stat().st_mtime
        self._listeners: list[Callable[[UserSettings, UserSettings], None]] = []
        self._lock = asyncio.Lock()

    def get(self) -> UserSettings:
        # Hand edits to settings.toml are picked up on the next read (files are the source of truth).
        try:
            mtime = self.paths.settings_toml.stat().st_mtime
        except FileNotFoundError:
            return self._settings
        if mtime != self._mtime:
            self._mtime = mtime
            try:
                new = load_user_settings(self.paths.settings_toml)
            except (ValidationError, ValueError):
                return self._settings  # keep the last good settings; a half-saved buffer shouldn't crash us
            old, self._settings = self._settings, new
            if new != old:
                self._notify(old, new)
        return self._settings

    def _notify(self, old: UserSettings, new: UserSettings) -> None:
        for fn in list(self._listeners):
            fn(old, new)

    def on_change(self, fn: Callable[[UserSettings, UserSettings], None]) -> None:
        self._listeners.append(fn)

    async def patch(self, patch: dict[str, Any]) -> UserSettings:
        async with self._lock:
            old = self.get()
            try:
                new = UserSettings.model_validate(deep_merge(old.model_dump(), patch))
            except ValidationError as e:
                raise EngineError("invalid_request", f"Invalid settings: {e.errors()[0]['msg']}") from e
            self._write(new)
            self._notify(old, new)
            return new

    async def set_key(self, key: str, value: Any) -> UserSettings:
        """Dotted-key write (assistant `update_setting`)."""
        async with self._lock:
            old = self.get()
            try:
                new = old.with_value(key, value)
            except KeyError as e:
                raise EngineError("invalid_request", f"There's no setting called {key}.") from e
            except ValidationError as e:
                raise EngineError("invalid_request", f"{key}: {e.errors()[0]['msg']}") from e
            self._write(new)
            self._notify(old, new)
            return new

    def _write(self, new: UserSettings) -> None:
        save_user_settings(self.paths.settings_toml, new)
        self._settings = new
        self._mtime = self.paths.settings_toml.stat().st_mtime

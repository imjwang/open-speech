"""Everything the engine says or shows to a person, in each supported language (FR-I18N).

`t("key", name=value)` returns the text in the user's language. The engine serves one person, so that
language is process-wide (`set_default_language`, kept in step with the `language` setting by the engine); a
turn that must answer in another language (the one that just asked to switch) overrides it for its own
task with `use_language`, which is a `ContextVar` and so never leaks into other work.

Catalogs are plain dicts in `en.py` and `es.py` with identical keys and placeholders (a test enforces it).
Plural forms are two keys, `<key>.one` and `<key>.other`, picked by `tn()`.
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
import sys
from contextvars import ContextVar

from open_speech.config import LANGUAGES, Language
from open_speech.i18n import en, es

CATALOGS: dict[Language, dict[str, str]] = {"en": en.MESSAGES, "es": es.MESSAGES}

_default: Language = "en"
_override: ContextVar[Language | None] = ContextVar("open_speech_language", default=None)


def normalize(code: str | None) -> Language | None:
    """"es-MX" → "es"; None for a language we have no catalog for."""
    base = (code or "").strip().lower().replace("_", "-").split("-")[0]
    return next((lang for lang in LANGUAGES if lang == base), None)


@functools.cache
def system_language() -> str | None:
    """ISO 639-1 code of the Mac's first preferred language ("en" from "en-US"), or the POSIX locale."""
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["defaults", "read", "-g", "AppleLanguages"], capture_output=True, text=True, timeout=2
            ).stdout
            m = re.search(r'"?([a-z]{2,3})(?:[-_][A-Za-z]+)*"?', out.replace("(", " "))
            if m:
                return m.group(1)
        except (OSError, subprocess.SubprocessError):
            pass
    loc = os.environ.get("LC_ALL") or os.environ.get("LANG") or ""
    m = re.match(r"([a-z]{2,3})[_.-]", loc)
    return m.group(1) if m else None


def resolve_language(setting: str | None) -> Language:
    """The `language` setting as a supported language: an explicit choice wins, "auto" follows the Mac."""
    explicit = normalize(setting) if setting and setting != "auto" else None
    return explicit or normalize(system_language()) or "en"


def set_default_language(lang: str | None) -> Language:
    """The language everything is said in unless a task overrides it."""
    global _default
    _default = normalize(lang) or "en"
    return _default


def use_language(lang: str | None) -> Language:
    """Override the language for the current task only (and whatever it awaits) and return it."""
    resolved = normalize(lang) or "en"
    _override.set(resolved)
    return resolved


def current_language() -> Language:
    return _override.get() or _default


def t(key: str, /, **params: object) -> str:
    lang = current_language()
    template = CATALOGS[lang].get(key)
    if template is None:
        template = CATALOGS["en"][key]
    return template.format(**params) if params else template


def tn(key: str, n: int | float, /, **params: object) -> str:
    """Plural-aware: `<key>.one` when n == 1, else `<key>.other`. `{n}` is available in the template."""
    return t(f"{key}.one" if n == 1 else f"{key}.other", n=n, **params)


def has(key: str) -> bool:
    return key in CATALOGS[current_language()] or key in CATALOGS["en"]


__all__ = [
    "CATALOGS",
    "current_language",
    "has",
    "normalize",
    "resolve_language",
    "set_default_language",
    "system_language",
    "t",
    "tn",
    "use_language",
]

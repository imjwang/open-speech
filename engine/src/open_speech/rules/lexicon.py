"""Pronunciation lexicon (06 § Lexicon): `rules/lexicon.csv` with `term,spoken,scope`.

Matching is whole-word and case-sensitive unless the term is all lowercase. Entries are respellings
("SQL" → "sequel"), applied after filters and before chunking, on every provider.

Scope strings: empty = global; otherwise `key:value[|value...]` joined with `;`, using the rule scope keys,
e.g. `types:pdf`, `sites:arxiv.org|*.acm.org`, `docs:doc_0123abcd;types:pdf`.
"""

from __future__ import annotations

import csv
import io
import os
import re
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from open_speech.api.schemas import LexiconEntry, RuleScope
from open_speech.document.model import Document
from open_speech.errors import EngineError, NotFound
from open_speech.ids import sha256_hex
from open_speech.rules.scope import scope_matches

__all__ = ["Lexicon", "LexiconEntry", "LexMatch", "parse_scope", "format_scope"]

HEADER = ["term", "spoken", "scope"]
_SCOPE_KEYS = {"sites", "types", "docs", "languages"}


def parse_scope(scope: str) -> RuleScope:
    data: dict[str, list[str]] = {}
    for part in scope.split(";"):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError(f"lexicon scope {part!r} must look like key:value")
        key, _, values = part.partition(":")
        key = key.strip().lower()
        if key == "site":
            key = "sites"
        elif key in {"type", "doc", "language"}:
            key += "s"
        if key not in _SCOPE_KEYS:
            raise ValueError(f"unknown lexicon scope key {key!r}")
        data.setdefault(key, []).extend(v.strip() for v in re.split(r"[|,]", values) if v.strip())
    return RuleScope(**data)


def format_scope(scope: RuleScope) -> str:
    parts = [
        f"{key}:{'|'.join(getattr(scope, key))}"
        for key in ("docs", "sites", "types", "languages")
        if getattr(scope, key)
    ]
    return ";".join(parts)


@lru_cache(maxsize=2048)
def _pattern(term: str) -> re.Pattern[str]:
    flags = re.IGNORECASE if term == term.lower() else 0
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", flags)


@dataclass(frozen=True)
class LexMatch:
    start: int
    end: int
    spoken: str
    entry: LexiconEntry


class Lexicon:
    """Mutable lexicon bound to `lexicon.csv`. `add`/`remove` persist immediately."""

    def __init__(self, entries: Iterable[LexiconEntry] = (), path: Path | None = None) -> None:
        self.entries: list[LexiconEntry] = list(entries)
        self.path = path
        self._lock = threading.RLock()

    # ------------------------------------------------------------ persistence

    @classmethod
    def load(cls, path: Path) -> Lexicon:
        if not path.exists():
            return cls([], path)
        return cls(cls.parse(path.read_text("utf-8")), path)

    @staticmethod
    def parse(text: str) -> list[LexiconEntry]:
        out: list[LexiconEntry] = []
        for row in csv.reader(io.StringIO(text)):
            if not row or not row[0].strip() or row[0].lstrip().startswith("#"):
                continue
            cells = [c.strip() for c in row]
            if [c.lower() for c in cells[:2]] == HEADER[:2]:
                continue
            if len(cells) < 2 or not cells[1]:
                continue
            scope = cells[2] if len(cells) > 2 else ""
            try:
                parse_scope(scope)
            except ValueError:
                continue  # ignore malformed rows rather than failing the whole file
            out.append(LexiconEntry(term=cells[0], spoken=cells[1], scope=scope))
        return out

    def dumps(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(HEADER)
        for e in self.entries:
            w.writerow([e.term, e.spoken, e.scope])
        return buf.getvalue()

    def save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(self.dumps(), "utf-8")
            os.replace(tmp, self.path)

    # ------------------------------------------------------------ editing

    def add(self, entry: LexiconEntry) -> None:
        term, spoken = entry.term.strip(), entry.spoken.strip()
        if not term or not spoken:
            raise EngineError("invalid_request", "A pronunciation needs both a word and how to say it.")
        try:
            parse_scope(entry.scope)
        except ValueError as e:
            raise EngineError("invalid_request", f"That pronunciation scope isn't valid: {e}.") from e
        clean = LexiconEntry(term=term, spoken=spoken, scope=entry.scope.strip())
        with self._lock:
            self.entries = [e for e in self.entries if not (e.term == term and e.scope == clean.scope)]
            self.entries.append(clean)
            self.save()

    def remove(self, term: str) -> None:
        with self._lock:
            kept = [e for e in self.entries if e.term != term]
            if len(kept) == len(self.entries):
                raise NotFound(f"a pronunciation for {term!r}")
            self.entries = kept
            self.save()

    # ------------------------------------------------------------ matching

    def for_document(self, doc: Document | None) -> list[LexiconEntry]:
        if doc is None:
            return list(self.entries)
        out = []
        for e in self.entries:
            if not e.scope:
                out.append(e)
                continue
            try:
                if scope_matches(parse_scope(e.scope), doc):
                    out.append(e)
            except ValueError:
                continue
        return out

    @staticmethod
    def find(text: str, entries: Sequence[LexiconEntry]) -> list[LexMatch]:
        """Non-overlapping whole-word matches; longer terms win."""
        found: list[LexMatch] = []
        taken: list[tuple[int, int]] = []
        for e in sorted(entries, key=lambda e: (-len(e.term), e.term)):
            for m in _pattern(e.term).finditer(text):
                s, t = m.start(), m.end()
                if any(s < te and t > ts for ts, te in taken):
                    continue
                taken.append((s, t))
                found.append(LexMatch(s, t, e.spoken, e))
        found.sort(key=lambda m: m.start)
        return found

    @staticmethod
    def fingerprint(entries: Sequence[LexiconEntry]) -> str:
        rows = sorted((e.term, e.spoken, e.scope) for e in entries)
        return sha256_hex(*("\x1e".join(r) for r in rows))

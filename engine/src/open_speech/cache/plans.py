"""Plan cache (05 § Caches): `SpeechScript`s as zstd-compressed JSON in the `plans` table, keyed by
`plan_key`. The cache also pins LLM output, so a document sounds the same on every replay.

Additions to the binding interface:
- `latest(document_id, mode)` — the newest plan for a document.
- `get_memo` / `put_memo` — per-block annotate span ops and per-section transform outputs, stored in the
  same table under `memo:<kind>:<hash>` keys. When rules change, blocks whose annotate input is unchanged
  re-use their pinned span ops, so re-planning costs no Claude calls ("only affected blocks re-plan").
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import zstandard

from open_speech.ids import sha256_hex
from open_speech.plan.script import SpeechScript
from open_speech.store.db import Database

_LEVEL = 6
_MEMO_PREFIX = "memo:"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class PlanCache:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._c = zstandard.ZstdCompressor(level=_LEVEL)
        self._d = zstandard.ZstdDecompressor()

    def _pack(self, obj: Any) -> bytes:
        raw = obj.encode("utf-8") if isinstance(obj, str) else json.dumps(obj).encode("utf-8")
        return self._c.compress(raw)

    def _unpack(self, blob: bytes) -> str:
        return self._d.decompress(blob).decode("utf-8")

    # ------------------------------------------------------------ scripts

    def get(self, plan_key: str) -> SpeechScript | None:
        row = self.db.one("SELECT script FROM plans WHERE plan_key=?", (plan_key,))
        if row is None:
            return None
        try:
            return SpeechScript.model_validate_json(self._unpack(row["script"]))
        except Exception:  # corrupt or from an incompatible version: treat as a miss
            self.db.execute("DELETE FROM plans WHERE plan_key=?", (plan_key,))
            return None

    def put(self, script: SpeechScript) -> None:
        blob = self._pack(script.model_dump_json())
        self.db.execute(
            "INSERT INTO plans(plan_key, document_id, script, created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(plan_key) DO UPDATE SET script=excluded.script, created_at=excluded.created_at",
            (script.plan_key, script.document_id, blob, _now()),
        )

    def latest(self, document_id: str, mode: str | None = None) -> SpeechScript | None:
        rows = self.db.query(
            "SELECT plan_key FROM plans WHERE document_id=? AND plan_key NOT LIKE 'memo:%' ORDER BY created_at DESC",
            (document_id,),
        )
        for row in rows:
            script = self.get(row["plan_key"])
            if script is not None and (mode is None or script.mode == mode):
                return script
        return None

    def drop_document(self, doc_id: str) -> None:
        """Forget every plan and pinned LLM output for a document ("regenerate")."""
        self.db.execute("DELETE FROM plans WHERE document_id=?", (doc_id,))

    def drop_plans(self, doc_id: str) -> None:
        """Addition: forget plans but keep pinned LLM output (cheap re-plan after a rule edit)."""
        self.db.execute("DELETE FROM plans WHERE document_id=? AND plan_key NOT LIKE 'memo:%'", (doc_id,))

    # ------------------------------------------------------------ pinned LLM output

    def get_memo(self, document_id: str, kind: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        rows = self.db.query(
            "SELECT script FROM plans WHERE document_id=? AND plan_key LIKE ? ORDER BY created_at",
            (document_id, f"{_MEMO_PREFIX}{kind}:%"),
        )
        for row in rows:
            try:
                data = json.loads(self._unpack(row["script"]))
            except Exception:
                continue
            if isinstance(data, dict):
                out.update(data)
        return out

    def put_memo(self, document_id: str, kind: str, entries: dict[str, Any]) -> None:
        if not entries:
            return
        key = f"{_MEMO_PREFIX}{kind}:{sha256_hex(document_id, *sorted(entries))}"
        self.db.execute(
            "INSERT INTO plans(plan_key, document_id, script, created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(plan_key) DO UPDATE SET script=excluded.script, created_at=excluded.created_at",
            (key, document_id, self._pack(entries), _now()),
        )

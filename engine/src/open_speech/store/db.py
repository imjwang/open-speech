"""SQLite store (03 § Storage): one file in app data, WAL mode, forward-only migrations.

A single connection guarded by a lock. The engine is a single process with modest write volume, so this
keeps things simple and safe from both the asyncio loop and worker threads.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

MIGRATIONS: list[str] = [
    # 1 — initial schema
    """
    CREATE TABLE documents (
      id TEXT PRIMARY KEY,
      source_id TEXT NOT NULL,
      kind TEXT NOT NULL,
      title TEXT NOT NULL,
      url TEXT, site TEXT, path TEXT,
      page_count INTEGER,
      extract_key TEXT NOT NULL,
      settings_json TEXT NOT NULL DEFAULT '{}',     -- {voice_id?, mode?, pinned?}
      created_at TEXT NOT NULL,
      last_opened_at TEXT NOT NULL
    );
    CREATE VIRTUAL TABLE documents_fts USING fts5(doc_id UNINDEXED, title, body);

    CREATE TABLE positions (
      document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
      utterance_index INTEGER NOT NULL DEFAULT 0,
      offset_ms INTEGER NOT NULL DEFAULT 0,
      progress REAL NOT NULL DEFAULT 0,
      updated_at TEXT NOT NULL
    );

    CREATE TABLE plans (
      plan_key TEXT PRIMARY KEY,
      document_id TEXT NOT NULL,
      script BLOB NOT NULL,                          -- zstd(json(SpeechScript))
      created_at TEXT NOT NULL
    );
    CREATE INDEX plans_doc ON plans(document_id);

    CREATE TABLE ledger (
      id INTEGER PRIMARY KEY,
      ts TEXT NOT NULL,
      provider TEXT NOT NULL,
      model TEXT NOT NULL,
      op TEXT NOT NULL,
      units_json TEXT NOT NULL,
      usd_estimate TEXT NOT NULL,
      pricing_version TEXT NOT NULL,
      document_id TEXT,
      turn_id TEXT,
      cache_hit INTEGER NOT NULL DEFAULT 0,
      local INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX ledger_ts ON ledger(ts);

    CREATE TABLE recordings (
      id TEXT PRIMARY KEY,
      kind TEXT NOT NULL,                            -- user | reply | preview
      path TEXT NOT NULL,
      duration_s REAL NOT NULL DEFAULT 0,
      turn_id TEXT,
      created_at TEXT NOT NULL
    );

    CREATE TABLE turns (
      id TEXT PRIMARY KEY,
      session_id TEXT NOT NULL,
      user_text TEXT NOT NULL,
      source TEXT NOT NULL,                          -- typed | spoken
      reply_text TEXT NOT NULL DEFAULT '',
      user_recording_id TEXT,
      reply_recording_id TEXT,
      handled_by TEXT NOT NULL DEFAULT '',           -- fast_path | agent | error
      created_at TEXT NOT NULL
    );

    CREATE TABLE actions (
      id TEXT PRIMARY KEY,
      turn_id TEXT,
      kind TEXT NOT NULL,
      summary TEXT NOT NULL,
      payload_json TEXT NOT NULL,
      preview_json TEXT,
      status TEXT NOT NULL DEFAULT 'pending',        -- pending | confirmed | cancelled | failed
      created_at TEXT NOT NULL,
      resolved_at TEXT
    );

    CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
    """,
]


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self.migrate()

    def migrate(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
                self._conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version={i};\nCOMMIT;")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.lastrowid or cur.rowcount

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self._conn.execute(sql, params).fetchone()
            return row

    def get_kv(self, k: str, default: str | None = None) -> str | None:
        row = self.one("SELECT v FROM kv WHERE k=?", (k,))
        return row["v"] if row else default

    def set_kv(self, k: str, v: str) -> None:
        self.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

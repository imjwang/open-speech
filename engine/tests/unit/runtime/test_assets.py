"""The local asset store (store/assets.py): voice audio, transcripts and fetched pages, by content hash."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_speech.store.assets import SIDECAR_VERSION, AssetStore, text_hash
from open_speech.store.db import Database


@pytest.fixture
def store(tmp_path: Path) -> AssetStore:
    return AssetStore(tmp_path / "cache", Database(tmp_path / "db.sqlite"))


def _age(store: AssetStore, kind: str, key: str, stamp: str) -> None:
    store.db.execute("UPDATE assets SET last_access = ? WHERE kind = ? AND key = ?", (stamp, kind, key))


def test_round_trip_keeps_bytes_text_and_metadata(store: AssetStore) -> None:
    put = store.put(
        "voice", "ab" * 32, b"OggS-audio", ext=".opus", meta={"voice": "kokoro:af_heart"}, text="Hello there."
    )
    assert put.path.read_bytes() == b"OggS-audio"
    assert put.path.suffix == ".opus"

    got = store.get("voice", "ab" * 32)
    assert got is not None
    assert got.read() == b"OggS-audio"
    assert got.text == "Hello there."
    assert got.text_hash == text_hash("Hello there.")
    assert got.meta == {"voice": "kokoro:af_heart"}
    assert store.contains("voice", "ab" * 32)
    assert store.get("voice", "cd" * 32) is None
    assert not store.contains("transcript", "ab" * 32), "kinds do not share keys"


def test_kinds_live_in_their_own_folders_with_a_sidecar(store: AssetStore, tmp_path: Path) -> None:
    store.put("voice", "a1" * 32, b"v", ext=".opus")
    store.put("transcript", "b2" * 32, b"t", ext=".txt", text="pause")
    store.put("page", "c3" * 32, b"<html>", ext=".bin", meta={"url": "https://example.org/"})
    root = tmp_path / "cache"
    assert sorted(p.name for p in root.iterdir()) == ["audio", "pages", "transcripts"]
    side = json.loads((root / "transcripts" / "b2" / "b2" / f"{'b2' * 32}.json").read_text("utf-8"))
    assert side["v"] == SIDECAR_VERSION and side["kind"] == "transcript" and side["text"] == "pause"
    assert store.stats() == {
        "voice": {"count": 1, "bytes": store.size_bytes("voice")},
        "transcript": {"count": 1, "bytes": store.size_bytes("transcript")},
        "page": {"count": 1, "bytes": store.size_bytes("page")},
    }
    assert store.count() == 3 and store.count("page") == 1


def test_every_rendering_of_a_sentence_can_be_found_by_its_text(store: AssetStore) -> None:
    store.put("voice", "a1" * 32, b"1", ext=".opus", meta={"voice": "heart"}, text="Good morning.")
    store.put("voice", "b2" * 32, b"2", ext=".opus", meta={"voice": "dora"}, text="Good morning.")
    store.put("voice", "c3" * 32, b"3", ext=".opus", meta={"voice": "heart"}, text="Good night.")
    found = store.find_text("Good morning.")
    assert sorted(a.meta["voice"] for a in found) == ["dora", "heart"]
    assert store.find_text("Good morning") == [], "the hash is of the exact text"
    assert store.find_text("Good morning.", kind="transcript") == []


def test_writing_again_replaces_the_entry(store: AssetStore) -> None:
    store.put("page", "d4" * 32, b"old body", ext=".bin", meta={"etag": '"1"'})
    store.put("page", "d4" * 32, b"new", ext=".bin", meta={"etag": '"2"'})
    got = store.get("page", "d4" * 32)
    assert got is not None and got.read() == b"new" and got.meta["etag"] == '"2"'
    assert store.count("page") == 1
    assert not list(got.path.parent.glob(".tmp-*")), "no temporary files are left behind"


def test_update_meta_merges_without_touching_the_body(store: AssetStore) -> None:
    store.put("page", "e5" * 32, b"body", ext=".bin", meta={"url": "https://example.org/", "fetched_at": 1.0})
    updated = store.update_meta("page", "e5" * 32, fetched_at=99.0)
    assert updated is not None and updated.meta == {"url": "https://example.org/", "fetched_at": 99.0}
    again = store.get("page", "e5" * 32)
    assert again is not None and again.meta["fetched_at"] == 99.0 and again.read() == b"body"
    assert store.update_meta("page", "ff" * 32, x=1) is None


def test_least_recently_used_goes_first_and_pinned_entries_stay(store: AssetStore) -> None:
    keys = [f"{i:02x}" * 32 for i in range(1, 5)]
    for i, key in enumerate(keys):
        store.put("voice", key, bytes(1000), ext=".opus")
        _age(store, "voice", key, f"2026-01-0{i + 1}T00:00:00+00:00")
    store.get("voice", keys[0])  # using the oldest makes it the newest
    one = store.size_bytes() // 4

    removed = store.evict(one * 2 + 10, pinned={keys[1]})
    assert removed == 2
    assert [store.contains("voice", k) for k in keys] == [True, True, False, False]
    assert store.evict(one * 2 + 10) == 0, "already fits"


def test_eviction_spans_kinds(store: AssetStore) -> None:
    store.put("page", "a1" * 32, bytes(5000), ext=".bin")
    _age(store, "page", "a1" * 32, "2026-01-01T00:00:00+00:00")
    store.put("voice", "b2" * 32, bytes(5000), ext=".opus")
    assert store.evict(6000) == 1
    assert store.get("page", "a1" * 32) is None and store.contains("voice", "b2" * 32)


def test_clear_one_kind_or_everything(store: AssetStore) -> None:
    store.put("voice", "a1" * 32, b"v", ext=".opus")
    store.put("voice", "b2" * 32, b"v", ext=".opus")
    store.put("transcript", "c3" * 32, b"t", ext=".txt")
    assert store.clear("voice", pinned={"b2" * 32}) == 1
    assert store.count("voice") == 1 and store.count("transcript") == 1
    assert store.clear() == 2
    assert store.count() == 0 and store.size_bytes() == 0


def test_a_file_the_index_forgot_is_adopted_and_a_missing_file_is_forgotten(store: AssetStore) -> None:
    store.put("voice", "a1" * 32, b"v", ext=".opus", text="hi")
    store.db.execute("DELETE FROM assets")
    assert store.count() == 0
    assert store.get("voice", "a1" * 32) is not None
    assert store.count("voice") == 1, "found on disk, so it is indexed again"

    asset = store.get("voice", "a1" * 32)
    assert asset is not None
    asset.path.unlink()
    assert store.get("voice", "a1" * 32) is None
    assert store.count("voice") == 0, "the index row for a vanished file is dropped"


def test_a_damaged_entry_is_dropped_not_served(store: AssetStore) -> None:
    put = store.put("voice", "a1" * 32, b"v", ext=".opus")
    side = put.path.with_suffix(".json")
    side.write_text("{not json", "utf-8")
    assert store.get("voice", "a1" * 32) is None
    assert not put.path.exists() and not side.exists()

    put = store.put("voice", "b2" * 32, b"v", ext=".opus")
    side = put.path.with_suffix(".json")
    side.write_text(json.dumps({"v": 1, "kind": "voice", "ext": ".opus"}), "utf-8")  # an older layout
    assert store.get("voice", "b2" * 32) is None


def test_clear_also_removes_files_without_an_index_row(store: AssetStore) -> None:
    put = store.put("voice", "a1" * 32, b"v", ext=".opus")
    store.db.execute("DELETE FROM assets")
    assert store.clear("voice") == 1
    assert not put.path.exists()

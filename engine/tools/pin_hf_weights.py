"""Pin Hugging Face weights for the model catalog (08 § Local models, 03 § Supply chain).

    uv run python -m tools.pin_hf_weights qwen3            # print the generated block
    uv run python -m tools.pin_hf_weights qwen3 --write    # replace it in src/open_speech/data/models.toml
    uv run python -m tools.pin_hf_weights qwen3 --record tests/fixtures/providers/hf_qwen3_api.json --only <ids>

For every Qwen3-TTS repo the app can install (`mlx-community/Qwen3-TTS-12Hz-{0.6B|1.7B}-{Base|CustomVoice|
VoiceDesign}-{bf16|8bit|6bit|5bit|4bit}`; there is no 0.6B VoiceDesign) it pins the current commit, records each
file's sha256 (Hugging Face's LFS hash for weights; small files are downloaded and hashed here), reads the speakers
and languages from that commit's `config.json`, and checks that `tts_model_type` is the family the repo name says.
The result goes between `# BEGIN GENERATED qwen3` and `# END GENERATED qwen3` in models.toml as
`[local_weights.qwen3."qwen3-tts-<size>-<family>-<precision>"]` tables (local/catalog.py `WeightsVariant`).

Nothing is installed here; `local/manager.py` downloads a variant when the user asks for it and verifies these pins.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

HF = "https://huggingface.co"
MODELS_TOML = Path(__file__).resolve().parents[1] / "src" / "open_speech" / "data" / "models.toml"
BEGIN = "# BEGIN GENERATED qwen3"
END = "# END GENERATED qwen3"

SIZES = ("0.6B", "1.7B")
FAMILIES = {"Base": "base", "CustomVoice": "custom_voice", "VoiceDesign": "voice_design"}
PRECISIONS = ("bf16", "8bit", "6bit", "5bit", "4bit")
SKIP_FILES = {".gitattributes", "README.md"}  # never read by the model loader

# Memory while generating (GB): the weights resident in Metal memory plus the codec decoder's working set while it
# streams. Measured on an M4 (16 GB) with `stream=True` and a 30 s paragraph (2026-11-22); variants not measured
# get the weights' size plus the same working set (~1.05 GB).
MEASURED_RAM_GB = {
    "qwen3-tts-0.6b-customvoice-8bit": 3.1,
    "qwen3-tts-1.7b-customvoice-8bit": 4.2,
}
WORKING_SET_GB = 1.05

FetchJson = Callable[[str], Any]
FetchBytes = Callable[[str], bytes]


def variant_id(size: str, family: str, precision: str) -> str:
    return f"qwen3-tts-{size.lower()}-{family.lower()}-{precision}"


def repos() -> list[tuple[str, str, str, str]]:
    """(variant id, repo, size, family) for every published combination."""
    out = []
    for size in SIZES:
        for family in FAMILIES:
            if size == "0.6B" and family == "VoiceDesign":
                continue
            for precision in PRECISIONS:
                repo = f"mlx-community/Qwen3-TTS-12Hz-{size}-{family}-{precision}"
                out.append((variant_id(size, family, precision), repo, size, family))
    return out


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "open-speech-pin/1"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 — fixed https host
        data: bytes = resp.read()
        return data


def http_json(url: str) -> Any:
    return json.loads(_get(url).decode("utf-8"))


def build_entry(vid: str, repo: str, size: str, family: str, precision: str, fetch_json: FetchJson,
                fetch_bytes: FetchBytes) -> dict[str, Any]:
    info = fetch_json(f"{HF}/api/models/{repo}")
    sha = str(info["sha"])
    tree = fetch_json(f"{HF}/api/models/{repo}/tree/{sha}?recursive=true&expand=true")
    files: list[dict[str, Any]] = []
    config: dict[str, Any] | None = None
    for f in sorted((x for x in tree if x.get("type") == "file"), key=lambda x: str(x["path"])):
        path = str(f["path"])
        if path in SKIP_FILES:
            continue
        lfs = f.get("lfs") or {}
        size_b = int(lfs.get("size") or f.get("size") or 0)
        digest = str(lfs.get("oid") or "")
        if not digest or path == "config.json":
            data = fetch_bytes(f"{HF}/{repo}/resolve/{sha}/{path}")
            digest = hashlib.sha256(data).hexdigest()
            size_b = len(data)
            if path == "config.json":
                config = json.loads(data.decode("utf-8"))
        files.append({"path": path, "sha256": digest, "size_mb": round(size_b / 1024**2, 1) or 0.1})
    if config is None:
        raise ValueError(f"{repo}: no config.json")
    mtype = config.get("tts_model_type")
    if mtype != FAMILIES[family]:
        raise ValueError(f"{repo}: tts_model_type is {mtype!r}, expected {FAMILIES[family]!r}")
    talker = config.get("talker_config") or {}
    speakers = list((talker.get("spk_id") or {}).keys())
    languages = [x for x in (talker.get("codec_language_id") or {}) if not x.endswith("_dialect")]
    total_mb = round(sum(f["size_mb"] for f in files), 1)
    ram = MEASURED_RAM_GB.get(vid, round(total_mb / 1024 * 1.074 + WORKING_SET_GB, 1))
    return {
        "id": vid,
        "repo": repo,
        "revision": sha,
        "family": family.lower(),
        "size": size.lower(),
        "precision": precision,
        "size_mb": total_mb,
        "ram_gb": ram,
        "speakers": speakers,
        "languages": languages,
        "files": files,
    }


def _toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def render_block(entries: list[dict[str, Any]], *, today: str | None = None) -> str:
    lines = [
        f"{BEGIN} (engine/tools/pin_hf_weights.py qwen3 --write, {today or date.today().isoformat()}).",
        "# Do not edit by hand: re-run the tool. Pins: commit + sha256 of every file (LFS hashes for weights).",
    ]
    for e in entries:
        lines += [
            "",
            f'[local_weights.qwen3.{_toml_str(e["id"])}]',
            f'family = {_toml_str(e["family"])}',
            f'size = {_toml_str(e["size"])}',
            f'precision = {_toml_str(e["precision"])}',
            f'ram_gb = {e["ram_gb"]}',
            "speakers = [" + ", ".join(_toml_str(s) for s in e["speakers"]) + "]",
            "languages = [" + ", ".join(_toml_str(s) for s in e["languages"]) + "]",
            f'[local_weights.qwen3.{_toml_str(e["id"])}.weights]',
            f'url = {_toml_str("hf://" + e["repo"])}',
            f'revision = {_toml_str(e["revision"])}',
            f'quant = {_toml_str(e["precision"])}',
            f'size_mb = {e["size_mb"]}',
            'model_file = "model.safetensors"',
            "files = [",
            *(f'  {{ path = {_toml_str(f["path"])}, sha256 = {_toml_str(f["sha256"])}, size_mb = {f["size_mb"]} }},'
              for f in e["files"]),
            "]",
        ]
    lines.append(END)
    return "\n".join(lines) + "\n"


def replace_block(toml_text: str, block: str) -> str:
    a, b = toml_text.find(BEGIN), toml_text.find(END)
    if a < 0 or b < 0:
        return toml_text.rstrip("\n") + "\n\n" + block
    b = toml_text.index("\n", b) + 1 if "\n" in toml_text[b:] else len(toml_text)
    return toml_text[:a] + block + toml_text[b:]


class Recorder:
    """Fetchers that remember every response (for a test fixture) or replay a recording."""

    def __init__(self, replay: dict[str, Any] | None = None) -> None:
        self.data: dict[str, Any] = dict(replay or {})
        self.replay = replay is not None

    def json(self, url: str) -> Any:
        if self.replay:
            return self.data[url]
        self.data[url] = http_json(url)
        return self.data[url]

    def bytes(self, url: str) -> bytes:
        if self.replay:
            return str(self.data[url]).encode("utf-8")
        raw = _get(url)
        self.data[url] = raw.decode("utf-8")
        return raw


def generate(only: set[str] | None, rec: Recorder) -> list[dict[str, Any]]:
    entries = []
    for vid, repo, size, family in repos():
        if only and vid not in only:
            continue
        precision = vid.rsplit("-", 1)[-1]
        try:
            entries.append(build_entry(vid, repo, size, family, precision, rec.json, rec.bytes))
        except urllib.error.HTTPError as exc:
            print(f"skipping {repo}: HTTP {exc.code}", file=sys.stderr)
            continue
        print(f"pinned {vid} @ {entries[-1]['revision'][:12]} ({entries[-1]['size_mb']:.0f} MB)", file=sys.stderr)
    return entries


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pin_hf_weights")
    ap.add_argument("family", choices=["qwen3"])
    ap.add_argument("--write", action="store_true", help=f"replace the generated block in {MODELS_TOML.name}")
    ap.add_argument("--only", default=None, help="comma-separated variant ids")
    ap.add_argument("--record", default=None, help="also save every API response to this JSON file")
    ap.add_argument("--replay", default=None, help="use a recorded JSON file instead of the network")
    args = ap.parse_args(argv)
    only = set(args.only.split(",")) if args.only else None
    rec = Recorder(json.loads(Path(args.replay).read_text("utf-8")) if args.replay else None)
    entries = generate(only, rec)
    if args.record:
        Path(args.record).write_text(json.dumps(rec.data, indent=1, sort_keys=True) + "\n", "utf-8")
    block = render_block(entries)
    if args.write:
        MODELS_TOML.write_text(replace_block(MODELS_TOML.read_text("utf-8"), block), "utf-8")
        print(f"wrote {len(entries)} variants to {MODELS_TOML}", file=sys.stderr)
    else:
        sys.stdout.write(block)
    return 0


if __name__ == "__main__":
    sys.exit(main())

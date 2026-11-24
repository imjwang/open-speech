#!/usr/bin/env bash
# Builds the self-contained engine runtime that electron-builder copies to
# "Open Speech.app/Contents/Resources/engine" (electron-builder.yml → extraResources; launch command in
# src/main/engine/spawn-spec.ts):
#
#   engine-runtime/
#     python/                          relocatable CPython (uv-managed python-build-standalone build) with the
#                                      engine and its locked dependencies, incl. the local-kokoro, local-stt
#                                      and local-qwen extras, installed into its own site-packages. Not a venv:
#                                      no absolute paths.
#     models/kokoro/<model>/           Kokoro weights (the catalog's first Kokoro model and the variant it pins:
#                                      model file, tokenizer, one file per voice), pinned by commit + sha256
#                                      from engine/src/open_speech/data/models.toml (FR-SETUP-3), read-only
#                                      (engine/src/open_speech/local/bundled.py)
#     models/stt/<model>/              the catalog's default speech-to-text model (about 640 MB), pinned by
#                                      commit + sha256, read-only: the bar listens offline from first launch
#     provider.env                     the provider keys this build ships with (see "Provider keys" below)
#
# Re-runnable; downloads are cached in engine-runtime-cache/. macOS arm64 only (NFR-6).
#
# Environment:
#   UV                         uv binary (default: uv on PATH, else ~/.local/bin/uv)
#   OPEN_SPEECH_PYTHON         CPython version to bundle (default below)
#   OPEN_SPEECH_BUNDLE_KOKORO  0 skips the weights (the engine then downloads them on first use)
#   OPEN_SPEECH_BUNDLE_STT     0 skips the speech-to-text weights (the user downloads them from the model list)
#   OPEN_SPEECH_BUNDLE_QWEN    0 leaves out the local Qwen3-TTS runtime (the local-qwen extra: MLX, mlx-audio,
#                              transformers; about 380 MB). Its weights are never bundled: the user downloads the
#                              size and precision they want from the model list (engine/src/open_speech/local/manager.py)
#   OPEN_SPEECH_BUNDLE_KEYS    0 builds without provider keys (local voice and local speech input only)
#   ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY, GOOGLE_TTS_API_KEY
#                              the keys to build in; when unset they are read from <repo>/.env
#                              (GOOGLE_TTS_API_KEY is optional: the engine uses GEMINI_API_KEY for Chirp without it)
set -euo pipefail

PYTHON_VERSION="${OPEN_SPEECH_PYTHON:-3.13.11}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(cd "$APP_DIR/.." && pwd)"
ENGINE_DIR="$REPO_DIR/engine"
OUT="$APP_DIR/engine-runtime"
CACHE="$APP_DIR/engine-runtime-cache"
PY="$OUT/python"
PYV="${PYTHON_VERSION%.*}"   # 3.13
PY_REQUEST="cpython-${PYTHON_VERSION}-macos-aarch64-none"

step() { printf '\n==> %s\n' "$*"; }
die() { printf 'bundle-engine: %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]] || die "builds the macOS arm64 runtime; run it on an Apple Silicon Mac"
UV="${UV:-$(command -v uv || true)}"
[[ -n "$UV" ]] || UV="$HOME/.local/bin/uv"
[[ -x "$UV" ]] || die "uv not found (set UV=/path/to/uv)"
for tool in install_name_tool codesign otool curl shasum ditto; do
  command -v "$tool" >/dev/null || die "$tool not found (Xcode Command Line Tools)"
done
mkdir -p "$CACHE"

# --------------------------------------------------------------------------- Python
step "CPython $PYTHON_VERSION (uv-managed, python-build-standalone)"
# A private install (not the user's ~/.local/share/uv copy) so the bundle starts from a pristine tree.
"$UV" python install --no-config --no-bin --install-dir "$CACHE/python" "$PY_REQUEST"
SRC="$CACHE/python/$PY_REQUEST"
[[ -x "$SRC/bin/python$PYV" ]] || die "no interpreter at $SRC/bin/python$PYV"

rm -rf "$OUT"
mkdir -p "$OUT"
ditto "$SRC" "$PY"
# uv patches build-time config (sysconfigdata: prefix, LIBDIR, INCLUDEPY, ...) to its install path; put back
# python-build-standalone's placeholder. Runtime paths (sys.prefix, sys.path) come from the binary's location.
grep -rIl -F "$SRC" "$PY" | while IFS= read -r f; do sed -i '' "s|$SRC|/install|g" "$f"; done

# The copy belongs to the app now, not to uv; drop what the engine never uses (Tk, IDLE, headers, pip).
rm -f "$PY/lib/python$PYV/EXTERNALLY-MANAGED"
rm -rf "$PY/include" "$PY/share" "$PY/lib/pkgconfig" \
  "$PY"/lib/itcl* "$PY"/lib/tcl* "$PY"/lib/tk* "$PY"/lib/thread* "$PY"/lib/libtcl* \
  "$PY/lib/python$PYV"/{idlelib,tkinter,turtledemo,ensurepip} "$PY/lib/python$PYV/turtle.py" \
  "$PY/lib/python$PYV"/lib-dynload/{_tkinter,_ctypes_test}.*.so \
  "$PY/lib/python$PYV"/site-packages/pip "$PY/lib/python$PYV"/site-packages/pip-*.dist-info
find "$PY/bin" -mindepth 1 ! -name "python$PYV" ! -name python3 ! -name python -delete

# uv rewrites libpython's install name to its absolute install path; make it relative again. The python
# binary already loads it through @rpath (= @executable_path/../lib). Re-sign: arm64 refuses modified code.
install_name_tool -id "@rpath/libpython$PYV.dylib" "$PY/lib/libpython$PYV.dylib" 2>/dev/null
codesign --force --sign - "$PY/lib/libpython$PYV.dylib"

# --------------------------------------------------------------------------- engine + dependencies
EXTRAS=(--extra local-kokoro --extra local-stt)
if [[ "${OPEN_SPEECH_BUNDLE_QWEN:-1}" != 0 ]]; then
  EXTRAS+=(--extra local-qwen)  # Qwen3-TTS voices: the runtime only (Apple Silicon wheels), weights on demand
fi
step "Engine and locked dependencies (uv.lock, ${EXTRAS[*]}, no dev group)"
"$UV" export --project "$ENGINE_DIR" --frozen --no-dev "${EXTRAS[@]}" --no-emit-project \
  --format requirements.txt --no-header --output-file "$CACHE/requirements.txt" >/dev/null
"$UV" pip install --no-config --python "$PY/bin/python3" --link-mode copy --only-binary :all: \
  --require-hashes -r "$CACHE/requirements.txt"
"$UV" pip install --no-config --python "$PY/bin/python3" --link-mode copy --no-deps --reinstall-package open-speech \
  "$ENGINE_DIR"

SITE="$PY/lib/python$PYV/site-packages"
# Console scripts carry absolute shebangs and the app launches the engine through the interpreter
# (spawn-spec.ts), so they go; so does pip-style install metadata that records the build path.
find "$PY/bin" -mindepth 1 ! -name "python$PYV" ! -name python3 ! -name python -delete
rm -f "$SITE"/open_speech-*.dist-info/direct_url.json

# Size: package test suites and onnxruntime's offline tooling are never imported at runtime; universal2
# binaries only need their arm64 slice (thinning keeps that slice's own signature intact).
find "$SITE" -type d -name tests -prune -exec rm -rf {} +
rm -rf "$SITE"/onnxruntime/{transformers,quantization,tools}
while IFS= read -r -d '' f; do
  if lipo -archs "$f" 2>/dev/null | grep -q ' '; then
    lipo -thin arm64 "$f" -output "$f.thin" && mv "$f.thin" "$f"
  fi
done < <(find "$PY" -type f \( -name '*.so' -o -name '*.dylib' \) -print0)
if [[ -n "$(find "$SITE" -name '*.pth' -exec grep -l -e "$REPO_DIR" -e "$HOME" {} +)" ]]; then
  die "a .pth file points outside the bundle (editable install?)"
fi

# Bytecode: the shell sets PYTHONDONTWRITEBYTECODE, so precompile once. unchecked-hash: the files are
# immutable inside the signed app and copying them must not invalidate the cache (mtimes may change).
find "$PY" -name __pycache__ -type d -prune -exec rm -rf {} +
"$PY/bin/python3" -I -m compileall -q -j 0 --invalidation-mode unchecked-hash "$PY/lib/python$PYV" \
  >"$CACHE/compileall.log" 2>&1 || echo "note: some files did not byte-compile (see $CACHE/compileall.log)"

# --------------------------------------------------------------------------- checks
step "Checks: imports, sys.path, linkage, licenses"
env -i HOME="$HOME" PATH=/usr/bin:/bin PYTHONNOUSERSITE=1 ORT_DISABLE_TELEMETRY=1 "$PY/bin/python3" - "$PY" <<'PYCODE'
import os, sys
root = os.path.realpath(sys.argv[1])
import open_speech, open_speech.cli.main, open_speech.api.app  # noqa: E401,F401
import onnxruntime, misaki, numpy, sounddevice, soundfile, pypdfium2  # noqa: E401,F401
import sherpa_onnx  # noqa: E402  local speech-to-text; ships its own onnxruntime next to the wheel's
sherpa_onnx.OfflineRecognizer.from_transducer, sherpa_onnx.VadModel.create  # what providers/local_stt.py calls
outside = [p for p in sys.path if p and not os.path.realpath(p).startswith(root)]
if outside:
    sys.exit(f"sys.path entries outside the runtime: {outside}")
data = os.path.join(os.path.dirname(open_speech.__file__), "data", "models.toml")
if not os.path.exists(data):
    sys.exit("open_speech/data/models.toml missing from the installed package")
print(f"python {sys.version.split()[0]}, open-speech {open_speech.__version__}, onnxruntime {onnxruntime.__version__}, "
      f"sherpa-onnx {sherpa_onnx.__version__}")
print("sys.path:", " ".join(os.path.relpath(p, root) for p in sys.path if p))
PYCODE

if [[ "${OPEN_SPEECH_BUNDLE_QWEN:-1}" != 0 ]]; then
  # The Qwen3-TTS runtime, offline and as the engine starts it: MLX runs a kernel on the GPU (its metallib loads
  # from inside the runtime), mlx-audio's loader and the Qwen3-TTS model code import without the network, and the
  # worker process (`-I -m open_speech.providers.qwen3_worker`) answers over its pipes (fake model: no weights).
  env -i HOME="$HOME" PATH=/usr/bin:/bin PYTHONNOUSERSITE=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 TOKENIZERS_PARALLELISM=false "$PY/bin/python3" -I - "$PY" <<'PYCODE'
import os, subprocess, sys
root = os.path.realpath(sys.argv[1])
import mlx.core as mx
mx.eval(mx.ones(4) + 1)
assert mx.default_device() == mx.gpu, mx.default_device()
from mlx_audio.tts.utils import load_model  # noqa: F401
import mlx_audio.tts.models.qwen3_tts  # noqa: F401
from open_speech.providers import qwen3_worker as qw
metallib = [os.path.join(d, f) for d, _, fs in os.walk(os.path.dirname(mx.__file__)) for f in fs if f.endswith(".metallib")]
if not metallib or not all(os.path.realpath(p).startswith(root) for p in metallib):
    sys.exit(f"MLX's metallib isn't inside the runtime: {metallib}")
proc = subprocess.Popen([sys.executable, "-I", "-m", "open_speech.providers.qwen3_worker", "--fake"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE)
proc.stdin.write(qw.encode_frame({"op": "ping", "id": 1}) + qw.encode_frame({"op": "exit"}))
proc.stdin.flush()
reply = qw.read_frame(proc.stdout)
proc.wait(timeout=30)
if not reply or reply[0].get("op") != "pong":
    sys.exit(f"the Qwen3-TTS worker didn't answer: {reply}")
import importlib.metadata as md
print(f"mlx {md.version('mlx')}, mlx-audio {md.version('mlx-audio')}, transformers {md.version('transformers')}; "
      f"GPU kernel ran; worker answered")
PYCODE
fi

# Every Mach-O must load only system libraries or files inside the runtime (@rpath/@loader_path/...).
bad_links=0
while IFS= read -r -d '' f; do
  file -b "$f" | grep -q 'Mach-O' || continue
  while IFS= read -r dep; do
    case "$dep" in
      @rpath/* | @loader_path/* | @executable_path/* | /usr/lib/* | /System/Library/*) ;;
      *) echo "outside dependency: $f -> $dep"; bad_links=1 ;;
    esac
  done < <(otool -arch arm64 -L "$f" | tail -n +2 | awk '{print $1}' | grep -v -x -F "$(otool -arch arm64 -D "$f" | tail -n +2)")
done < <(find "$PY" -type f \( -name '*.so' -o -name '*.dylib' -o -perm -u+x \) -print0)
[[ $bad_links == 0 ]] || die "the runtime links against libraries outside the bundle"

# No text file may mention the build machine's paths (shebangs, .pth, configs, RECORD files).
if grep -rIl -e "$REPO_DIR" -e "$HOME/.local" -e "$CACHE" "$PY" --exclude='*.pyc' >"$CACHE/abs-paths.txt"; then
  cat "$CACHE/abs-paths.txt"; die "files above contain absolute build-machine paths"
fi

# NFR-10: no GPL/AGPL in the default install (LGPL is fine). Checks every installed distribution's metadata.
"$PY/bin/python3" -I - "$CACHE/licenses.txt" <<'PYCODE'
import importlib.metadata as md, re, sys
copyleft = re.compile(r"(?<![L\w])A?GPL|GNU (Affero )?General Public License")
rows, bad = [], []
for d in sorted(md.distributions(), key=lambda d: d.metadata["Name"].lower()):
    m = d.metadata
    lic = m.get("License-Expression") or ""
    classifiers = [c.split(" :: ")[-1] for c in (m.get_all("Classifier") or []) if c.startswith("License ::")]
    free = (m.get("License") or "").strip()
    summary = " | ".join(filter(None, [lic, *classifiers])) or (free.splitlines() or ["?"])[0]
    rows.append(f"{m['Name']} {m['Version']}: {summary[:80]}")
    if copyleft.search(" ".join([lic, *classifiers, free[:200]])):
        bad.append(rows[-1])
open(sys.argv[1], "w").write("\n".join(rows) + "\n")
if bad:
    sys.exit("GPL/AGPL distributions in the default install (NFR-10):\n  " + "\n  ".join(bad))
print(f"{len(rows)} distributions, none GPL/AGPL (list: {sys.argv[1]})")
PYCODE

# --------------------------------------------------------------------------- Kokoro weights
if [[ "${OPEN_SPEECH_BUNDLE_KOKORO:-1}" != 0 ]]; then
  # Like speech-to-text below: the model, the revision, every file (model, tokenizer, one per voice) and its sha256
  # come from the catalog installed in the runtime, the same pins the engine verifies when it downloads the
  # weights itself. A file without a pin is refused. Lines: "<sha256> <path> <url>".
  kokoro_pins="$("$PY/bin/python3" -I - <<'PYCODE'
import sys
from open_speech.local.catalog import ModelCatalog
models = ModelCatalog.load().tts_models("kokoro")
if not models or models[0].weights is None:
    sys.exit("the catalog has no Kokoro model with weights")
e = models[0]
w = e.weights
base = w.base_url()
print(e.model, f"{w.url}@{w.revision}", w.quant or "")
for f in w.all_files() + w.voice_files(e.voices):
    if len(f.sha256) != 64:
        sys.exit(f"{e.model}: {f.path} has no sha256 pin in the catalog")
    print(f.sha256, f.path, f.url or f"{base}/{f.path}")
PYCODE
)"
  read -r KOKORO_MODEL KOKORO_SOURCE KOKORO_QUANT <<<"$(head -n 1 <<<"$kokoro_pins")"
  step "Kokoro weights ($KOKORO_MODEL ${KOKORO_QUANT:+$KOKORO_QUANT }from $KOKORO_SOURCE)"
  dl="$CACHE/kokoro/${KOKORO_SOURCE##*@}"
  dest="$OUT/models/kokoro/$KOKORO_MODEL"
  while read -r sha rel url; do
    [[ -n "${url:-}" ]] || continue
    f="$dl/$rel"
    if [[ ! -f "$f" ]] || [[ "$(shasum -a 256 "$f" | awk '{print $1}')" != "$sha" ]]; then
      mkdir -p "$(dirname "$f")"
      curl -fsSL --retry 3 --retry-delay 2 -o "$f.part" "$url"
      got="$(shasum -a 256 "$f.part" | awk '{print $1}')"
      [[ "$got" == "$sha" ]] || { rm -f "$f.part"; die "$rel: sha256 $got, expected $sha"; }
      mv "$f.part" "$f"
    fi
    mkdir -p "$(dirname "$dest/$rel")"
    cp "$f" "$dest/$rel"
  done < <(tail -n +2 <<<"$kokoro_pins")

  "$PY/bin/python3" -I - "$dest" "$KOKORO_MODEL" "$KOKORO_SOURCE" <<'PYCODE'
import hashlib, json, sys
from datetime import UTC, datetime
from pathlib import Path
dest, model, source = Path(sys.argv[1]), *sys.argv[2:]
files = {
    str(p.relative_to(dest)): {"sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "bytes": p.stat().st_size, "pinned": True}
    for p in sorted(dest.rglob("*")) if p.is_file() and p.name != "manifest.json"
}
manifest = {"model": model, "provider": "kokoro", "installed_at": datetime.now(UTC).isoformat(), "pinned": True,
            "bundled": True, "source": source, "files": files}
(dest / "manifest.json").write_text(json.dumps(manifest, indent=2), "utf-8")
print(f"{len(files)} files, {sum(f['bytes'] for f in files.values()) / 1e6:.0f} MB")
PYCODE
else
  step "Skipping Kokoro weights (OPEN_SPEECH_BUNDLE_KOKORO=0): the engine downloads them on first use"
fi

# --------------------------------------------------------------------------- speech-to-text weights
if [[ "${OPEN_SPEECH_BUNDLE_STT:-1}" != 0 ]]; then
  # The model id, the download URLs and the sha256 of every file come from the catalog installed in the
  # runtime (engine/src/open_speech/data/models.toml → [stt.local]): the same pins the engine verifies when
  # it installs the model itself. A file without a pin is refused. Lines: "<sha256> <path> <url>".
  stt_pins="$("$PY/bin/python3" -I - <<'PYCODE'
import sys
from open_speech.local.catalog import ModelCatalog
e = ModelCatalog.load().default_local_stt_model()
if e is None or e.weights is None:
    sys.exit("the catalog has no default local speech-to-text model with weights")
base = e.weights.base_url()
print(e.model, f"{e.weights.url}@{e.weights.revision}")
for f in e.weights.all_files():
    if len(f.sha256) != 64:
        sys.exit(f"{e.model}: {f.path} has no sha256 pin in the catalog")
    print(f.sha256, f.path, f.url or f"{base}/{f.path}")
PYCODE
)"
  read -r STT_MODEL STT_SOURCE <<<"$(head -n 1 <<<"$stt_pins")"
  step "Speech-to-text weights ($STT_MODEL, $STT_SOURCE)"
  dl="$CACHE/stt/$STT_MODEL"
  dest="$OUT/models/stt/$STT_MODEL"
  while read -r sha rel url; do
    [[ -n "${url:-}" ]] || continue
    f="$dl/$rel"
    if [[ ! -f "$f" ]] || [[ "$(shasum -a 256 "$f" | awk '{print $1}')" != "$sha" ]]; then
      mkdir -p "$(dirname "$f")"
      curl -fsSL --retry 3 --retry-delay 2 -o "$f.part" "$url"
      got="$(shasum -a 256 "$f.part" | awk '{print $1}')"
      [[ "$got" == "$sha" ]] || { rm -f "$f.part"; die "$rel: sha256 $got, expected $sha"; }
      mv "$f.part" "$f"
    fi
    mkdir -p "$(dirname "$dest/$rel")"
    cp "$f" "$dest/$rel"
  done < <(tail -n +2 <<<"$stt_pins")

  "$PY/bin/python3" -I - "$dest" "$STT_MODEL" "$STT_SOURCE" <<'PYCODE'
import hashlib, json, sys
from datetime import UTC, datetime
from pathlib import Path
from open_speech.local.catalog import ModelCatalog
dest, model, source = Path(sys.argv[1]), *sys.argv[2:]
entry = ModelCatalog.load().local_stt_model(model)
missing = [f for f in entry.required_files() if not (dest / f).is_file()]
if missing:
    sys.exit(f"{model}: missing after download: {missing}")
def sha256(p):
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()
files = {
    str(p.relative_to(dest)): {"sha256": sha256(p), "bytes": p.stat().st_size, "pinned": True}
    for p in sorted(dest.rglob("*")) if p.is_file() and p.name != "manifest.json"
}
manifest = {"model": model, "provider": "local", "kind": "stt", "installed_at": datetime.now(UTC).isoformat(),
            "pinned": True, "bundled": True, "source": source, "files": files}
(dest / "manifest.json").write_text(json.dumps(manifest, indent=2), "utf-8")
print(f"{len(files)} files, {sum(f['bytes'] for f in files.values()) / 1e6:.0f} MB")
PYCODE
else
  step "Skipping speech-to-text weights (OPEN_SPEECH_BUNDLE_STT=0): the user downloads them from the model list"
fi

# --------------------------------------------------------------------------- provider keys
# Keys are build configuration: the app never asks a user for one. Whatever is set when this script runs is
# written to provider.env, which the packaged engine reads (engine/src/open_speech/config.py). Only the provider
# keys are copied, never the rest of .env.
#
# A key shipped inside an app can be read by anyone who has the app. That is acceptable for builds that stay
# on our own machines; anything distributed further must use the hosted deployment instead of bundled keys.
if [[ "${OPEN_SPEECH_BUNDLE_KEYS:-1}" != 0 ]]; then
  step "Provider keys (build configuration)"
  keys_file="$OUT/provider.env"
  ( umask 077; : > "$keys_file" )
  included=()
  for name in ANTHROPIC_API_KEY OPENAI_API_KEY GEMINI_API_KEY GOOGLE_TTS_API_KEY; do
    value="${!name:-}"
    if [[ -z "$value" && -f "$REPO_DIR/.env" ]]; then
      value="$(grep -E "^${name}=" "$REPO_DIR/.env" | tail -n 1 | cut -d= -f2- | sed -e "s/^[\"']//" -e "s/[\"']\$//" || true)"
    fi
    if [[ -n "$value" ]]; then
      printf '%s=%s\n' "$name" "$value" >> "$keys_file"
      included+=("${name%_API_KEY}")
    fi
  done
  if (( ${#included[@]} )); then
    echo "built in: ${included[*]} (readable by anyone who has this build: keep it on your own machines)"
  else
    rm -f "$keys_file"
    echo "no provider keys set: this build reads with the local voice and listens with the local model only"
  fi
else
  step "Skipping provider keys (OPEN_SPEECH_BUNDLE_KEYS=0)"
fi

step "Done"
du -sh "$PY" "$OUT/models" 2>/dev/null || true
du -sh "$OUT"

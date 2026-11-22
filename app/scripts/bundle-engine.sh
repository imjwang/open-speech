#!/usr/bin/env bash
# Builds the self-contained engine runtime that electron-builder copies to
# "Open Speech.app/Contents/Resources/engine" (electron-builder.yml → extraResources; launch command in
# src/main/engine/spawn-spec.ts):
#
#   engine-runtime/
#     python/                          relocatable CPython (uv-managed python-build-standalone build) with the
#                                      engine and its locked dependencies, incl. the local-kokoro extra,
#                                      installed into its own site-packages. Not a venv: no absolute paths.
#     models/kokoro/kokoro-82m-v1.0/   Kokoro weights, pinned by commit + sha256 (FR-SETUP-3), read-only
#                                      (engine/src/open_speech/local/bundled.py)
#
# Re-runnable; downloads are cached in engine-runtime-cache/. macOS arm64 only (NFR-6).
#
# Environment:
#   UV                         uv binary (default: uv on PATH, else ~/.local/bin/uv)
#   OPEN_SPEECH_PYTHON         CPython version to bundle (default below)
#   OPEN_SPEECH_BUNDLE_KOKORO  0 skips the weights (the engine then downloads them on first use)
set -euo pipefail

PYTHON_VERSION="${OPEN_SPEECH_PYTHON:-3.13.11}"
KOKORO_MODEL="kokoro-82m-v1.0"
KOKORO_REPO="onnx-community/Kokoro-82M-v1.0-ONNX-timestamped"
KOKORO_REVISION="dd4401a9add81ac692d20e240d22ec9dda82cc29"
# sha256 of every bundled file at KOKORO_REVISION: Hugging Face's LFS hashes for the model and voices, and
# the hash of tokenizer.json (a small non-LFS file) at that commit. The file list must match the catalog
# (engine/src/open_speech/data/models.toml); the script fails if it doesn't.
KOKORO_PINS='
c0c02b3299fd97c34ea92a98e6d41eaa1a739c8f77bf685aac34bd7b34c1132c  onnx/model_quantized.onnx
77a02c8e164413299b4b4c403b14f8e0e1c1b727db4d46a09d6327b861060a34  tokenizer.json
d583ccff3cdca2f7fae535cb998ac07e9fcb90f09737b9a41fa2734ec44a8f0b  voices/af_heart.bin
c4a6b876047fd7fb472edf4ebd63cfac7c3b958a7cae7c106e8f038ca6308c45  voices/af_alloy.bin
4a004c33430762e2461eedb2013fad808ef4ab3121f5300f554476caf58d8361  voices/af_aoede.bin
f69d836209b78eb8c66e75e3cda491e26ea838a3674257e9d4e5703cbaf55c8b  voices/af_bella.bin
a240a5e3c15b43563d6e923bdca8ef5613a23471d9b77653694012435df23bd8  voices/af_jessica.bin
9be5221b6a941c04b561959b8ff0b06e809444dcc4ab7e75a7b23606f691819e  voices/af_kore.bin
cd2191ab31b914ed7b318416b0e4440fdf392ddad9106a060819aa600a64f59a  voices/af_nicole.bin
18778272caa0d0eebaea251c35fd635f038434f9eee5e691d02a174bd328414f  voices/af_nova.bin
00a2bcf82b1d86e8f19902ede58c65ccf6c0e43b44b7d74fad54e5d8933c9c30  voices/af_river.bin
4409fbc125afabacc615d94db5398d847006a737b0247d6892b7a9a0007a2f0a  voices/af_sarah.bin
4435255c9744f3f31659e0d714ab7689bf65d9e77ec1cce060f083912614f0b9  voices/af_sky.bin
162b035ed91cfc48b6046982184c645f72edcdd1b82843347f605d7bf7b15716  voices/am_adam.bin
3968b92c3c4cd1c4416dbded36c13eaa388a90d5788d02a13e4d781f5f8cf3c3  voices/am_echo.bin
e8b5be17edd1e3636901ce7598baafe2dc8dd8ff707a0c23bf9e461add7e2832  voices/am_eric.bin
c27989f741f7ee34d273a39d8a595cc0837d35f5ced9a29b7cc162614616df43  voices/am_fenrir.bin
52403be32fd047c6a44517cb0bcd6b134f2a18baa73e70ef41651e0eab921ade  voices/am_liam.bin
1d1f21dd8da39c30705cd4c75d039d265e9bc4a2a93ed09bc9e1b1225eb95ba1  voices/am_michael.bin
da5d135b424164916d75a68ffb4c2abce3d7d5ccc82dd1ee6cf447ce286145e6  voices/am_onyx.bin
fcf73c989033e9233e0b98713eca600c8c74dcc1614b37009d5450ff4a2274a0  voices/am_puck.bin
61150cf726ab6c5ed7a99f90a304f91f5a72c00c592e89ec94e5df11c319227a  voices/am_santa.bin
08afa6ba24da61ea5e8efa139e5aadc938d83f0a6da5a900adaf763ac1da5573  voices/bf_alice.bin
669fe0647f9dd04fcab92f1439a40eeb4c8b4ab1f82e4996fe3d918ce4a63b73  voices/bf_emma.bin
3754352c4aaa46d17f27654ab7518d65b62ad6163a0f55a5f4330c2da2c4e94f  voices/bf_isabella.bin
5e0ee32ebe64a467124976b14e69590746f1c4ce41a12b587a50c862edfea335  voices/bf_lily.bin
6b3194bbceffb746733cbc22c8f593dd44e401a71d53895a2dca891bc595a1e8  voices/bm_daniel.bin
f889083196807b4adb15e9204252165f503b8d33d3982e681c52443c49d798f1  voices/bm_fable.bin
c4b235a4c1f2cd3b939fed08b899ce9385638b763f7b73a59616c4fc9bd6c9bc  voices/bm_george.bin
b8f671cef828c30e66fdf0b0756a76bba58f6bb3398cbbf27058642acbcedb97  voices/bm_lewis.bin
'

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
step "Engine and locked dependencies (uv.lock, --extra local-kokoro, no dev group)"
"$UV" export --project "$ENGINE_DIR" --frozen --no-dev --extra local-kokoro --no-emit-project \
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
env -i HOME="$HOME" PATH=/usr/bin:/bin PYTHONNOUSERSITE=1 "$PY/bin/python3" - "$PY" <<'PYCODE'
import os, sys
root = os.path.realpath(sys.argv[1])
import open_speech, open_speech.cli.main, open_speech.api.app  # noqa: E401,F401
import onnxruntime, misaki, numpy, sounddevice, soundfile, pypdfium2, keyring  # noqa: E401,F401
outside = [p for p in sys.path if p and not os.path.realpath(p).startswith(root)]
if outside:
    sys.exit(f"sys.path entries outside the runtime: {outside}")
data = os.path.join(os.path.dirname(open_speech.__file__), "data", "models.toml")
if not os.path.exists(data):
    sys.exit("open_speech/data/models.toml missing from the installed package")
print(f"python {sys.version.split()[0]}, open-speech {open_speech.__version__}, onnxruntime {onnxruntime.__version__}")
print("sys.path:", " ".join(os.path.relpath(p, root) for p in sys.path if p))
PYCODE

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
  step "Kokoro weights ($KOKORO_REPO @ ${KOKORO_REVISION:0:12})"
  # The pinned file list must be exactly what the catalog expects (model, tokenizer, one file per voice).
  expected="$("$PY/bin/python3" -I - "$KOKORO_MODEL" <<'PYCODE'
import sys
from open_speech.local.catalog import ModelCatalog
e = ModelCatalog.load().local_model(sys.argv[1])
w = e.weights
files = {f.path for f in w.all_files()} | {w.voice_file.format(voice=v) for v in e.voices}
print("\n".join(sorted(files)))
PYCODE
)"
  pinned="$(printf '%s\n' "$KOKORO_PINS" | awk 'NF==2 {print $2}' | sort)"
  [[ "$expected" == "$pinned" ]] || die "Kokoro pins don't match the catalog:
$(diff <(echo "$expected") <(echo "$pinned") || true)"

  dl="$CACHE/kokoro/$KOKORO_REVISION"
  dest="$OUT/models/kokoro/$KOKORO_MODEL"
  base="https://huggingface.co/$KOKORO_REPO/resolve/$KOKORO_REVISION"
  while read -r sha rel; do
    [[ -n "${rel:-}" ]] || continue
    f="$dl/$rel"
    if [[ ! -f "$f" ]] || [[ "$(shasum -a 256 "$f" | awk '{print $1}')" != "$sha" ]]; then
      mkdir -p "$(dirname "$f")"
      curl -fsSL --retry 3 --retry-delay 2 -o "$f.part" "$base/$rel"
      got="$(shasum -a 256 "$f.part" | awk '{print $1}')"
      [[ "$got" == "$sha" ]] || { rm -f "$f.part"; die "$rel: sha256 $got, expected $sha"; }
      mv "$f.part" "$f"
    fi
    mkdir -p "$(dirname "$dest/$rel")"
    cp "$f" "$dest/$rel"
  done <<<"$KOKORO_PINS"

  "$PY/bin/python3" -I - "$dest" "$KOKORO_MODEL" "$KOKORO_REPO" "$KOKORO_REVISION" <<'PYCODE'
import hashlib, json, sys
from datetime import UTC, datetime
from pathlib import Path
dest, model, repo, rev = Path(sys.argv[1]), *sys.argv[2:]
files = {
    str(p.relative_to(dest)): {"sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "bytes": p.stat().st_size, "pinned": True}
    for p in sorted(dest.rglob("*")) if p.is_file()
}
manifest = {"model": model, "provider": "kokoro", "installed_at": datetime.now(UTC).isoformat(), "pinned": True,
            "bundled": True, "source": f"hf://{repo}@{rev}", "files": files}
(dest / "manifest.json").write_text(json.dumps(manifest, indent=2), "utf-8")
print(f"{len(files)} files, {sum(f['bytes'] for f in files.values()) / 1e6:.0f} MB")
PYCODE
else
  step "Skipping Kokoro weights (OPEN_SPEECH_BUNDLE_KOKORO=0): the engine downloads them on first use"
fi

step "Done"
du -sh "$PY" "$OUT/models" 2>/dev/null || true
du -sh "$OUT"

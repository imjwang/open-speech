# 10 — Delivery

Status: Draft v0.1 · 2026-11-22

**Platform:** macOS 14+ on Apple Silicon only. CI runs on macOS arm64 runners; verification runs on local builds.

## Repository layout

```
open-speech/
  engine/                      Python (uv) — the product
    pyproject.toml             package "open-speech", scripts: open-speech
    uv.lock
    openapi.json               committed; CI diffs it
    ws-schema.json             committed; CI diffs it
    src/open_speech/           see 03 § Package layout
    tests/
      unit/  contract/  integration/  golden/  fixtures/
  app/                         Electron shell
    package.json  bun.lock  electron.vite.config.ts
    scripts/bundle-engine.sh   builds the engine runtime for packaging: Python, dependencies, Kokoro,
                               the speech model, and provider.env from the build's keys
    src/main/                  tray, shortcuts, engine supervisor, windows, file search for the @ menu
    src/preload/               app preload; web page preload (Readability + highlight)
    src/renderer/commandbar/   the bar: composer (Lexical), rows and cards, toolbar, first run
    src/renderer/reader/       pdf.js view, text view, WebContentsView host
    src/renderer/rules/        the rules editor
    src/shared/api/            generated OpenAPI + WS client (do not edit)
    src/shared/i18n/           interface text, English and Spanish
    e2e/driver.mjs             drives the built app for hands-on checks (Playwright _electron)
  docs/
  .env.example  .gitignore  README.md  LICENSE
```

The shell must not ship a feature the engine doesn't expose. CI enforces the generated client is current.

## Milestones

| M | Name | Scope | Exit criteria |
|---|---|---|---|
| **M0** | Engine skeleton | Settings and keys (`.env`), catalogs (`models.toml`, `pricing.toml`), SQLite + migrations, ledger, providers (OpenAI TTS, Gemini TTS via Interactions, Kokoro), audio cache, transport (sounddevice + WSOLA), REST/WS skeleton with token auth, CLI `serve`/`read` for text | `open-speech read - <<< "hello"` speaks on each of the three providers; ledger rows correct; replay makes 0 API calls |
| **M1** | Read documents headless | PDF + TXT/MD + image ingest, segmentation, Classic presets and filters, lexicon, plan cache, navigation, speed, resume | `open-speech read paper.pdf` reads a two-column paper with no headers, footers or page numbers; images are described; golden snapshots pass |
| **M2** | Shell | electron-vite + React Compiler + shadcn; tray; command bar (listening first with on-device STT, live partials, the composer with badges and the `@` file menu, dictation, recordings and replay, rows and cards, fast-path commands in English and Spanish, the reading queue); themes; reader for PDF and text with highlight and follow; clipboard hotkey; media keys; generated client | Hotkey → speak "read clipboard" → audio + highlight in ≤ 3 s; VoiceOver can operate the bar |
| **M3** | Web, DOCX, AI Enhanced rules | `WebContentsView` reader with Readability blocks + Custom Highlight; DOCX; annotate tier (quote-anchored span ops, bounded by the guard); rule files, watcher, compiler, speech diff, Script view; `SPEECH.md`; the rules editor; the asset store | Logged-in article reads with in-page highlight; "stop reading footnotes on this site" works end to end with preview |
| **M4** | Assistant and cost | Claude agent with the full tool catalog (incl. rule maintenance and links), confirmations, spoken replies and ducking; usage summaries; budgets; pre-flight estimates; first run; keys as build configuration | Every P0 flow completes by voice alone; budget hard stop falls back to Kokoro; estimate within ±25% |
| **M5** | Hardening and packaging | a11y audit, e2e suite, error paths, macOS arm64 packaging (electron-builder + uv-managed Python runtime + bundled Kokoro and speech model; provider keys written at build time or left out) | Fresh Mac: install → first run by voice → read a PDF, no terminal needed |

**Post-MVP (P1, in rough order):** model manager rows · BYO OpenAI-compatible server · MCP
server · OpenAI-compatible facade · local alignment (v1.1 word sync) · read selection / screenshot ·
document Q&A and summaries · transforms and skills · audio-file sources · hands-free + barge-in
· export audio · folders · auto-ramp speed · more languages · a hosted deployment that holds the provider
keys and bills for use (replaces keys set at build time).
**P2:** Qwen3-TTS (preset voices), video sources, EPUB, notes and bookmarks, sleep timer, provider usage reconciliation, Message
Batches pre-processing, sync via a user folder.

## Testing strategy

| Layer | What | Tooling |
|---|---|---|
| Unit | Segmenter offsets, PDF heuristics, filters, lexicon, span-op application (quote matching, rejection of mismatches), cache keys, pricing math, budget logic, fast-path grammar | pytest |
| Provider adapters | Request shaping, streaming, retries, error mapping, against **recorded fixtures**; a small live smoke suite behind `OPEN_SPEECH_LIVE=1` | pytest + respx/vcr-style cassettes |
| Golden corpus | Academic two-column PDF, single-column report, scanned page, news article (HTML blocks), docs page, DOCX with footnotes, screenshot image → **speech-diff snapshots** (what is spoken, per preset/mode) | pytest snapshot; reviewed on change |
| Contract | OpenAPI and WS schema are stable; the generated TS client compiles; API behavior matches the schema | schemathesis-style property tests; tsc |
| Shell | Components, stores | Vitest |
| End-to-end | Hotkey → bar listening → injected audio produces partials → "read clipboard" → playback → highlight advances → speed change → cache replay offline; voice-only flows from injected transcripts | Engine integration tests over the real REST + WS API with fixture providers; `app/e2e/driver.mjs` (Playwright `_electron`) for hands-on runs against either fixture providers or the real local models, with a microphone fed from recordings (`voicemic` + `say -o`), so nothing touches the real microphone or the speakers |
| Local speech | Real recognizer and voices where the weights are present (skipped elsewhere): English and Spanish commands, non-speech rejected, one-word answers after a long silence; Spanish G2P against a frozen reference | pytest. The real-model tests run where the weights are (`OPEN_SPEECH_TEST_STT_HOME`, `OPEN_SPEECH_KOKORO_DIR`); the G2P reference test always runs |
| Accessibility | Automated axe checks for bar and reader; manual VoiceOver pass per milestone | axe-core, manual |
| Cost | Estimator accuracy against the golden corpus (±25%) | pytest |

**Live API use in tests** is opt-in, capped, and logged to the ledger like any other use.

## Risk register

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | Cloud TTS has no word timestamps | Word highlight is approximate | Exact sentences + syllable estimate (v1); local alignment (v1.1); Kokoro exact |
| R2 | LLM annotate latency (~2.5 s per batch) | Slower first open in AI Enhanced | First batch before play, lookahead after; plan cache; Classic is instant |
| R3 | LLM span accuracy | Wrong words skipped | Quote-anchored ops only; exact-match or drop; speech diff before saving rules; Script view; golden snapshots |
| R4 | LLM nondeterminism | Replays differ | Plan cache pins output; "regenerate" is explicit |
| R5 | LLM cost on large documents | Surprise spend | Tiered planning, pre-flight estimates, budgets, prefetch caps |
| R6 | PDF layout quality (columns, footnotes, tables) | Bad reading order | Heuristics + golden corpus; AI Enhanced extraction (P1) for hard pages |
| R7 | Model-ID churn (e.g. Gemini TTS moved to the Interactions API) | Broken providers | Catalog-driven IDs; live smoke tests; adapters isolate SDK changes |
| R8 | Provider rate limits | Stalls | Backoff, per-provider concurrency limits, prefetch, fallback voice |
| R9 | Prompt injection via content | Unwanted changes | Tool-less plan/vision calls; tagged content; confirmations; engine-enforced budget |
| R10 | Localhost API exposure | Local attacker or DNS rebinding | Loopback bind, token, Host/Origin checks, 0600 `engine.json` |
| R11 | Packaging Python inside Electron | Install size, first-run failures | uv-managed runtime; locked deps; local extras on demand; M5 dedicated to packaging |
| R12 | Python 3.14 wheel availability (onnxruntime, mlx, sherpa-onnx, PyAV) | Build breaks | Choose the newest Python with wheels for all pins at scaffold time |
| R13 | espeak-ng (GPL) in some Kokoro phonemizer paths | License conflict | English lexicon G2P and our own rule-based Spanish G2P; espeak is never installed, and languages that would need it are not offered |
| R14 | Qwen3-TTS (P2) speed on base M-series Macs | Stutter | Gate by chip and memory; render ahead; fall back to Kokoro |
| R15 | Echo cancellation for hands-free barge-in | False triggers | Push-to-talk in P0; engine-side AEC or renderer playback for P1 |
| R16 | macOS permissions (Accessibility, Screen Recording) | Friction | P1 only; spoken explanation before the OS prompt |
| R17 | Signing and notarization | Gatekeeper warnings | Verification uses local ad-hoc-signed builds; Developer ID signing and notarization before any public download |
| R18 | Prosody seams between chunks | Choppy audio | 1–3 sentence units; consistent style instructions; crossfade 10 ms |
| R19 | Storage growth | Disk use | 5 GB LRU cap, recording retention, "clear cache" |
| R20 | Speed above ~3x degrades WSOLA quality | Harsher audio | Tune WSOLA window; document limits; optional provider-side speed for exports |
| R21 | Provider keys inside a build | Anyone with the build can read and spend the keys | Builds with keys stay on our own machines; anything distributed is built without (`OPEN_SPEECH_BUNDLE_KEYS=0`); the hosted deployment replaces bundled keys before any public release |
| R22 | One-word spoken commands are misheard | "pause" heard as "cause"; a lone "sí" as "see" | Audio cut to the detected speech; measured mishearings mapped back for spoken one-word input; two-word phrasings; every command also has a key or a button. Not yet tested with human voices |
| R23 | Kokoro first audio above the 0.5 s target | A beat of silence before reading starts | Play before storing, playhead before prefetch, warm the voice on open (0.65 s measured); a shorter first unit or streaming synthesis if it matters |
| R24 | The assistant is steered by a page it reads | Data carried to another server, or a local address fetched | Link boundary (`07`): only hosts the user named after content has been read; local and private addresses refused on every hop. DNS rebinding between check and request is not covered |
| R25 | Size of the packaged app with the speech model | A much larger download | The model can be left out (`OPEN_SPEECH_BUNDLE_STT=0`) and downloaded on first use |

## Open questions

1. **Distribution:** verification runs on local macOS builds. When do we add Developer ID signing and a public DMG?
2. **Sync:** user-owned folder (iCloud Drive / Dropbox) for rules and positions, or skip entirely?
3. **Default assistant voice:** Kokoro (fast, $0) or the user's reading voice?
4. **Speed above 3x:** acceptable with WSOLA, or worth a higher-quality stretcher (Rubber Band is GPL; look for permissive options)?
5. **Hosted deployment:** what holds the keys, how users are identified and billed, and which features move behind it first. Until it exists, cloud features exist only in builds made with keys.
6. **First sentence faster than 0.65 s:** split off a short first clause (needs someone to listen for prosody) or stream partial audio into the renderer?

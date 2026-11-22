# 10 — Delivery

Status: Draft v0.1 · 2026-11-21

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
      unit/  contract/  golden/  fixtures/providers/
  app/                         Electron shell
    package.json  pnpm-lock.yaml  electron.vite.config.ts
    src/main/                  tray, shortcuts, engine supervisor, windows
    src/preload/               bar/reader preload; web page preload (Readability + highlight)
    src/renderer/commandbar/   React + shadcn Command
    src/renderer/reader/       pdf.js view, text view, WebContentsView host
    src/shared/api/            generated OpenAPI + WS client (do not edit)
    e2e/                       Playwright _electron
  docs/
  .env.example  .gitignore  README.md  LICENSE
```

The shell must not ship a feature the engine doesn't expose. CI enforces the generated client is current.

## Milestones

| M | Name | Scope | Exit criteria |
|---|---|---|---|
| **M0** | Engine skeleton | Settings and keys (`.env`), catalogs (`models.toml`, `pricing.toml`), SQLite + migrations, ledger, providers (OpenAI TTS, Gemini TTS via Interactions, Kokoro), audio cache, transport (sounddevice + WSOLA), REST/WS skeleton with token auth, CLI `serve`/`read` for text | `open-speech read - <<< "hello"` speaks on each of the three providers; ledger rows correct; replay makes 0 API calls |
| **M1** | Read documents headless | PDF + TXT/MD + image ingest, segmentation, Classic presets and filters, lexicon, plan cache, navigation, speed, resume | `open-speech read paper.pdf` reads a two-column paper with no headers, footers or page numbers; images are described; golden snapshots pass |
| **M2** | Shell | electron-vite + React Compiler + shadcn; tray; command bar (listening first, live STT partials via OpenAI/Gemini, typed mode, recordings and replay, rows, fast-path commands); reader for PDF and text with highlight and follow; clipboard hotkey; media keys; generated client | Hotkey → speak "read clipboard" → audio + highlight in ≤ 3 s; VoiceOver can operate the bar |
| **M3** | Web, DOCX, AI Enhanced rules | `WebContentsView` reader with Readability blocks + Custom Highlight; DOCX; annotate tier (quote-anchored span ops); rule files, watcher, compiler, speech diff, Script view; `SPEECH.md` | Logged-in article reads with in-page highlight; "stop reading footnotes on this site" works end to end with preview |
| **M4** | Assistant and cost | Claude agent with the full tool catalog, confirmations, spoken replies and ducking; usage summaries; budgets; pre-flight estimates; key paste; onboarding | Every P0 flow completes by voice alone; budget hard stop falls back to Kokoro; estimate within ±25% |
| **M5** | Hardening and packaging | a11y audit, e2e suite, error paths, macOS arm64 packaging (electron-builder + uv-managed Python runtime + bundled Kokoro) | Fresh Mac: install → onboarding → read a PDF, no terminal needed |

**Post-MVP (P1, in rough order):** model manager rows · BYO OpenAI-compatible server · MCP
server · OpenAI-compatible facade · local alignment (v1.1 word sync) · read selection / screenshot ·
document Q&A and summaries · transforms and skills · audio-file sources · local STT + hands-free + barge-in
· export audio · folders · auto-ramp speed.
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
| End-to-end | Hotkey → bar listening → injected audio produces partials → "read clipboard" → playback → highlight advances → speed change → cache replay offline; voice-only flows from injected transcripts | Playwright `_electron` with a fake mic stream and fixture providers |
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
| R13 | espeak-ng (GPL) in some Kokoro phonemizer paths | License conflict | English G2P without espeak by default; espeak only as an optional, labelled extra |
| R14 | Qwen3-TTS (P2) speed on base M-series Macs | Stutter | Gate by chip and memory; render ahead; fall back to Kokoro |
| R15 | Echo cancellation for hands-free barge-in | False triggers | Push-to-talk in P0; engine-side AEC or renderer playback for P1 |
| R16 | macOS permissions (Accessibility, Screen Recording) | Friction | P1 only; spoken explanation before the OS prompt |
| R17 | Signing and notarization | Gatekeeper warnings | Verification uses local ad-hoc-signed builds; Developer ID signing and notarization before any public download |
| R18 | Prosody seams between chunks | Choppy audio | 1–3 sentence units; consistent style instructions; crossfade 10 ms |
| R19 | Storage growth | Disk use | 5 GB LRU cap, recording retention, "clear cache" |
| R20 | Speed above ~3x degrades WSOLA quality | Harsher audio | Tune WSOLA window; document limits; optional provider-side speed for exports |

## Open questions

1. **Distribution:** verification runs on local macOS builds. When do we add Developer ID signing and a public DMG?
2. **Sync:** user-owned folder (iCloud Drive / Dropbox) for rules and positions, or skip entirely?
3. **Default assistant voice:** Kokoro (fast, $0) or the user's reading voice?
4. **Speed above 3x:** acceptable with WSOLA, or worth a higher-quality stretcher (Rubber Band is GPL; look for permissive options)?

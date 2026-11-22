# Implementation status

As of 2026-11-21 · branch `feat/implementation` · macOS 26 on Apple M4

Open Speech v0.1 runs end to end on Apple Silicon: the packaged app installs from a DMG, onboards with
no keys, and reads PDFs, web pages and the clipboard aloud with the bundled Kokoro voice and live
highlighting. Everything that needs a cloud key was exercised only against recorded fixtures, because
no keys were available during the build.

**Legend.** *Verified*: driven by hand in the real Electron app on macOS (screenshots taken).
*Tested*: covered by the automated suites (engine 270 tests, app 202 tests) but not driven by hand.
*Partial*: works with a stated gap. *Not verified*: needs hardware, a key or a human.

## What was verified by hand

| Flow | How | Result |
|---|---|---|
| Keyless onboarding, keyboard | Fresh profile, three steps, prompts spoken by Kokoro | Works; ends on a typing-first bar |
| Onboarding by voice only | Scripted speech-to-text: "skip", "the second one", "yes" | Works; ends listening for the first command |
| Read a two-column PDF | Open file → reader | Plays at once; soft sentence tint, outlined word; headers, citations and URLs hatched as skipped |
| Read a web page | Type a link → live page in the reader | Plays; in-page sentence and word highlight; Script view shows exactly what is spoken |
| Voice commands | "read my clipboard", "say SQL as sequel" → spoken card → "yes" | Works; replies spoken and replayable; lexicon saved |
| Spending, voices, keyless guidance | Typed commands | Calm, labelled cards; locked providers collapsed to one "add a key" row |
| Packaged app | DMG → fresh profile → onboarding → PDF | See [Packaged app](#packaged-app) |
| CLI | `open-speech models install kokoro-82m-v1.0`, `open-speech read notes.md` | Works without a running engine |

## Functional requirements (P0)

| ID | Requirement | Status | Notes |
|---|---|---|---|
| FR-BAR-1 | Hotkey opens the bar listening | Partial | Opens listening when speech input is set up (verified via the app's open event). Without an OpenAI/Gemini key it opens typing by design. ⌥Space is registered but wasn't pressed physically |
| FR-BAR-2 | Live transcript | Verified | Fixture speech-to-text partials replace in place |
| FR-BAR-3 | Typing switches to text mode | Tested | |
| FR-BAR-4 | Submit on release, end of speech, Enter | Verified | End of speech (energy VAD) and Enter by hand; push-to-talk in unit tests |
| FR-BAR-5 | Results as rows | Verified | Answers, action cards with speech diff, lists, spending card |
| FR-BAR-6 | Replay recording and reply | Tested | Replay rows render; replay itself covered by integration tests |
| FR-BAR-7 | Drop or paste file, URL, image, text | Partial | Open file and links verified; drag-and-drop not driven by hand |
| FR-BAR-8 | Recents and search | Partial | Recents verified; full-text search tested |
| FR-BAR-9 | Menu-bar icon shows state | Not verified | Icons and state machine exist; not inspected on screen |
| FR-BAR-11 | Fast-path commands without the LLM | Verified | 58 grammar cases; links and file paths added |
| FR-READ-1 | PDFs | Verified | |
| FR-READ-2 | URLs in the in-app reader, persistent logins | Partial | Live page and highlight verified; login persistence not tested |
| FR-READ-3 | DOCX | Tested | Golden snapshots |
| FR-READ-4 | TXT/MD and pasted text | Verified | |
| FR-READ-5 | Read-clipboard hotkey | Partial | "read my clipboard" verified; ⌥⇧R not pressed physically |
| FR-READ-6 | Sentence and word highlight | Verified | PDF, web page, Script view |
| FR-READ-7 | Follow mode, click-to-start | Tested | |
| FR-READ-8 | Navigation | Tested | |
| FR-READ-12 | Speed 0.5–5x | Tested | |
| FR-READ-14 | Resume position | Tested | Survives restart (integration test); recents show progress |
| FR-READ-17 | Media keys, background playback | Partial | Playback is engine-owned and continues with windows closed; media keys need hardware |
| FR-MM-1 | Images become speech | Tested | Needs an Anthropic key live |
| FR-VOICE-1 | One voice list | Verified | |
| FR-VOICE-2 | Preview without switching | Verified | Real Kokoro synthesis |
| FR-VOICE-3 | Default, per-document, assistant voice | Tested | Default set from onboarding verified |
| FR-VOICE-4 | Style instructions | Tested | Adapter fixtures only |
| FR-VOICE-5 | Local Kokoro, offline | Verified | Bundled in the app; sha256-pinned |
| FR-RULE-1 | Presets, Classic / AI Enhanced | Verified | Classic verified; AI Enhanced needs a key |
| FR-RULE-2 | Rules by voice or text with preview | Tested | "Stop reading the footnotes on this site" end to end with the deterministic compiler |
| FR-RULE-3 | Markdown rule files with scopes | Tested | |
| FR-RULE-4 | Pronunciation lexicon | Verified | By voice, with spoken confirmation |
| FR-RULE-5 | Script view | Verified | |
| FR-RULE-6 | `SPEECH.md` | Tested | |
| FR-ASST-1 | One Claude agent | Tested | Tool Runner adapter against a mocked API; no live key |
| FR-ASST-2 | Spoken replies; reading pauses | Verified | Reading resumes after the reply even if the bar stays open |
| FR-ASST-3 | Voice parity for settings | Tested | |
| FR-ASST-4 | Confirm mutating actions | Verified | |
| FR-ASST-5 | "Where am I?" | Tested | |
| FR-ASST-6 | Usage questions | Verified | Always labelled estimated |
| FR-ASST-7 | Spoken errors with the fix | Verified | Missing-key replies explain what works and offer "paste a key" |
| FR-COST-1 | Ledger | Verified | $0 rows for Kokoro and cache hits |
| FR-COST-2 | Usage summaries | Verified | |
| FR-COST-3 | Pre-flight estimate | Tested | Within ±25% in tests |
| FR-COST-4 | Monthly budget | Tested | Hard stop falls back to Kokoro |
| FR-COST-5 | Editable pricing | Tested | |
| FR-CACHE-1 | Audio cache, free replays | Tested | |
| FR-CACHE-2 | Cap, LRU, pins, clear | Tested | |
| FR-CACHE-3 | Incremental re-synthesis | Tested | |
| FR-CACHE-4 | Extraction and plan caches | Tested | |
| FR-API-1 | REST + WebSocket with token auth | Tested | 13 security contract tests |
| FR-API-2 | Generated TypeScript client | Tested | `pnpm check:api` |
| FR-API-3 | CLI | Verified | |
| FR-API-6 | Headless operation | Verified | |
| FR-SETUP-1 | Keys from `.env` | Tested | |
| FR-SETUP-2 | Paste a key into the bar | Partial | UI verified; no real key to test with |
| FR-SETUP-3 | Speak from first launch | Verified | Kokoro bundled |
| FR-SETUP-4 | Onboarding ≤ 3 steps, spoken, skippable | Verified | Keyboard and voice-only |
| FR-SETUP-5 | Appearance | Partial | Light theme reviewed; dark mode and reader options not reviewed by hand |
| FR-SETUP-6 | Remappable shortcuts | Not verified | |
| FR-SETUP-7 | Graceful missing keys | Verified | |
| FR-SETUP-8 | Recording retention | Tested | |

P1 and P2 requirements are not started except where the scaffold already covered them (MCP server,
bring-your-own OpenAI-compatible server adapter, local model manager).

## Non-functional requirements

| ID | Status | Notes |
|---|---|---|
| NFR-1 Performance | Partial | See measurements below |
| NFR-3 Privacy | Verified | No telemetry; onnxruntime telemetry forced off in every entry point |
| NFR-4 Security | Tested | Token, Host and Origin checks; strict CSP in the shell |
| NFR-5 Accessibility | Partial | axe: no serious or critical violations after fixes; VoiceOver pass not done |
| NFR-6 Platform | Verified | macOS 26, Apple M4 |
| NFR-9 Resources | Partial | See measurements below |
| NFR-10 Licensing | Verified | 88 bundled packages, no GPL/AGPL |

## Packaged app

`pnpm dist:mac` builds `OpenSpeech-0.1.0-arm64.dmg` (about 290 MB; the app is 728 MB installed:
Electron 243 MB, Python runtime 379 MB, Kokoro 110 MB). Ad-hoc signed, hardened runtime off, not
notarized; 88 Python packages, none GPL/AGPL.

Run on this Mac from the DMG, with a fresh profile and the PATH a Finder launch gets:

| Step | Result |
|---|---|
| Launch → engine ready | 0.8 s (first launch after install: about 40 s while macOS scans the new binaries) |
| Onboarding, no keys | Prompts spoken by the bundled Kokoro voice; finishes on a typing-first bar |
| Open a PDF from Finder ("Open With") | Reader window in 0.74 s; audio playing in 1.56 s; highlight and skipped spans as in development |
| Memory while reading | Engine 838 MB RSS with Kokoro loaded; Electron processes 47–176 MB each |
| Quit (SIGTERM) and force-quit | All processes exit; `engine.json` is removed |

## Measurements against NFR-1 and NFR-9

| Target | Measured |
|---|---|
| Time to first audio, Kokoro, Classic (≤ 0.5 s p50) | 1.56 s from Finder open on a first open, including import and the reader window; not yet measured per utterance |
| Engine API latency during synthesis | 2–30 ms |
| URL import (Wikipedia article) | 0.24 s after the first import of the session |
| Kokoro RAM (≤ 1.3 GB) | 838 MB engine RSS |

## Not done yet

- A run on a second, clean Mac, and the Gatekeeper path for a downloaded copy.
- Live smoke tests with real Anthropic, OpenAI and Gemini keys (`OPEN_SPEECH_LIVE=1`).
- VoiceOver pass; physical hotkeys and media keys; menu-bar icon states on screen.
- Developer ID signing and notarization (open question in spec 10).
- About 40 long-line lint warnings in the engine (`ruff check`); behavior is unaffected.

# Implementation status

As of 2026-11-22 · branch `feat/implementation` · macOS 26 on Apple M4

Open Speech v0.1 runs end to end on Apple Silicon: it reads PDFs, web pages and the clipboard aloud with
the bundled Kokoro voices and live highlighting, listens on the Mac with a local speech recognizer, and
works in English and Spanish. Provider keys are build configuration; the app never asks for one. The
first round (2026-11-21) packaged the app and tested Anthropic, OpenAI and Gemini live (see
[Live providers](#live-providers-2026-11-21)); the second round is described next.

**Legend.** *Verified*: driven in the real Electron app on macOS (screenshots taken), by hand or through
the test driver. *Tested*: covered by the automated suites (engine 853 tests, app 259 tests) but not
driven in the app. *Partial*: works with a stated gap. *Not verified*: needs hardware, a key or a human.

## This round (2026-11-22)

**What changed**

- **No user keys.** Key entry, the keychain, the `/keys` routes and the `keys` CLI command are gone. Keys come from the environment, the repo `.env` in development, and `provider.env` written into a packaged build by `app/scripts/bundle-engine.sh` (`OPEN_SPEECH_BUNDLE_KEYS=0` builds without). `GET /capabilities` replaces `/keys`; `unavailable` replaces `missing_key`. A key inside a build is readable by whoever has the build (see `08` § Keys).
- **Local speech-to-text.** Parakeet TDT 0.6B v3 through sherpa-onnx, first in the routing order, bundled by default, installable through the model manager. Audio is cut to the detected speech before recognition.
- **Spanish.** Interface, fast-path grammar, replies, document language detection, language-aware voices, three Kokoro Spanish voices with a rule-based Spanish G2P, Spanish sentence rules.
- **Themes.** Lavender (pastel, light) and Ember (dark, orange); "system" follows the Mac; every window follows live.
- **Interface.** Icon-first bar (orb, toolbar, transport strip, cards that are controls); the composer is a Lexical editor with badges for files, links and pasted text, an `@` file menu, drop and paste, dictation; first run has no key step.
- **Reading queue and attachments.** Several files, links or texts on one command are read one after another.
- **Rules editor.** A window that edits the rule files as text, with autosave, a whole-file view and dictation; a rule-file API behind it.
- **Asset store.** Voice audio, transcripts and fetched pages in one content-addressed local store; fetched pages are cached and revalidated.
- **Assistant.** Reads links (with a boundary on which links it may follow), maintains rules (list, read, edit, delete, preferences), changes visible settings at once and lasting ones by card.
- **Known issues from the test handoff.** AI Enhanced over-deletion next to a removed URL (markers + guard); the Wikipedia infobox caption read before the first paragraph (lead rule); Kokoro first audio measured and roughly halved; typing during processing no longer dropped; lint warnings cleared.

**Automated checks as of this date**

| Suite | Result |
|---|---|
| Engine `pytest` | 853 passed, 10 skipped (the skips need local model weights; with the weights present those pass) |
| Engine `ruff check` | Clean |
| Engine `mypy src` | 51 errors, all in files that had them before this round (61 at the start of it) |
| App `pnpm test` | 259 passed |
| App `pnpm typecheck`, `pnpm check:api` | Clean |

Tests no longer open the Mac's audio output: the suite sets `OPEN_SPEECH_AUDIO_OUTPUT=null` itself. Before
that was in place, a few full runs by the agents working on this round played the fixture voice's test
tones through the default output.

**Verified in the app** (2026-11-22). The real Electron app against a real engine: no keys, no fixture
providers, the real Kokoro voices and the real Parakeet model, sound output disabled, windows made
non-focusable. Speech came from recordings made with `say -o` and fed into the test driver's `voicemic`,
so the real microphone and the speakers were never used.

| Flow | Result |
|---|---|
| First run by voice | "one" picked the voice; "yes" after eight seconds of silence answered the cleanup question; the bar ended listening. With one voice installed the voice question is skipped |
| English commands | "pause please", "faster", "where am I", "next paragraph", "show voices", "show settings", "dark theme": recognized and carried out |
| Spanish commands | "sigue", "más rápido", "pausa por favor", "muestra las voces", "tema claro", "en español", "¿dónde estoy?", "abre las reglas": recognized and carried out; replies in Spanish once the language was switched |
| A rule change by voice, confirmed by voice | "deja de leer las tablas" → card → "sí" (heard as "See.", mapped back) → saved |
| Spanish document | Detected as Spanish, read by `ef_dora` without being asked, "Sra." not split, word highlight in step |
| Language switch | Every label in the bar, the settings card, the rules editor and the reader in Spanish |
| Themes | Bar, reader and rules editor in Lavender and in Ember; switched live by voice and by the API |
| Rules editor | Opens by voice; built-in names in Spanish; preferences empty with an explanation; a typed rule saved to `rules/` and compiled (earlier in the round, with fixture providers) |
| Paste and drop | Paste and drop events dispatched in the real app: a pasted link, two dropped links, a dropped file and a long pasted text each became a badge; sending them read four items one after another and named the link that returned 404 |
| Composer (earlier in the round, with fixture providers) | `@` menu with recent files and places, folder browsing, file badges, a typed link becoming a badge, "read them" → three items queued, settings card, auto-skip chips, spending ring, confirmation card |

**Found by that run and fixed**

- A one-word answer spoken after a few seconds of silence came back empty from the recognizer (now cut to the speech; measured in `08`).
- Toolbar labels stayed in the previous language after a switch (a compiled component kept its output).
- One attachment of several failing to open was dropped without a word.
- An address like `javascript:alert(1)` crashed the page store instead of being refused.
- Preset names were English on Spanish confirmation cards and in the rules editor.
- "about less than a minute left".
- First run asked to "say one, two or three" with one voice; "keep this one" picked voice one.
- `@~/` did not list the home folder.
- `voice_input.stt_provider` was ignored by the running engine.
- "read this file" with a dropped file, a bare "voices" or "ajustes", "usa el tema del sistema" and "presupuesto de 15" fell through to the assistant.
- The microphone's idle timeout could land on the first instant of speech and switch it off mid-word.

**Verified with the real providers** (2026-11-22). A second engine read the build-time keys from `.env`
(the development source of the same configuration a package carries in `provider.env`), with sound off
and a 60-cent budget cap. Total spend: about $0.12.

| Flow | Result |
|---|---|
| Capabilities with keys | Assistant, AI cleanup, vision and the OpenAI and Gemini voices reported; listening still on-device |
| Assistant (Claude) | Answered from settings and playback state; listed the active rules; changed nothing without a card |
| A site rule by request | "On wikipedia.org, stop reading the figure captions" → card scoped to wikipedia.org → "yes" → `rules/skip-captions.md`, compiled as a filter |
| The same by voice (handoff T10) | Recorded speech → on-device recognition (exact) → Claude → card with a speech diff (3 removals) → spoken "yes" → rule file written only then; the article's script then drops the infobox |
| Web access | Claude read a Wikipedia article through `fetch_url`; the page was stored once and reused by a second question |
| Spanish | With the interface in Spanish, Claude answered in Spanish from the stored page |
| Cloud voices | OpenAI and Gemini read a short text; costs logged; a replay was served from the store at no cost |
| Budget (handoff T8) | Warning at 91 % of the budget; at the limit the paid voice was refused and reading went on with Kokoro and a notice |
| Real Wikipedia page | The lead paragraph is read before the infobox (headless path) |

**Found by the real-provider run and fixed**

- A rule for a site the user named, with nothing open, could not be scoped to that site (the assistant correctly warned that the card said "everywhere"). `propose_rule` now takes the site.
- A rule scoped to `wikipedia.org` did not apply on `en.wikipedia.org`, although its preview showed what it would remove. A site now covers what is under it (`06` § Scope keys).
- After a spoken "yes" the assistant proposed the same rule again: confirmations and other instant commands were missing from the conversation it sees.
- A long document with a paid voice started reading without asking when opened or played directly; only the bar asked. The cost check now sits where every read starts (`08` § Budgets and estimates).
- "your nine cents budget".

**Seen and not fixed**

- The recognizer misheard two synthetic takes: "speak Spanish" as "Speanish" and one voice's "¿dónde estoy?" as "¿dónde les doy?". "Spanish" / "en español" on their own now switch the language.
- Kokoro's first audio for an ordinary first sentence is about 0.65 s warm, above the 0.5 s target (`05` § Measured).

**Not verified this round**

- **Human speech.** Every spoken test used synthetic voices. The six short commands, in particular, should be tried by a person in a quiet room.
- **Nobody listened.** Sound output was disabled throughout; Spanish and English audio quality rests on agreement with a reference phonemizer and on a speech recognizer transcribing it back.
- **Cloud providers, beyond the table above.** Not run live: cloud speech-to-text (OpenAI, Gemini), image description, AI Enhanced on a whole document after the guard (two Claude calls, about $0.02, reproduced and confirmed the over-deletion fix), and whether a cost estimate lands within ±25 % of the ledger (the question itself is tested with a paid test voice).
- **A wart left in place.** On protected Wikipedia articles the headless path reads the padlock icon's alt text ("Page semi-protected") after the lead.
- **By hand with a real pointer and keyboard:** a drag from Finder or a browser (the events were dispatched by script), the file picker, dictation into the composer and the rules editor, the speech-model download prompt, physical hotkeys.
- **The package's windows.** The packaged app was started once without opening the bar (see below); its first run, windows and Finder integration were not exercised this round, and it has not been on a second Mac.

**Packaged build** (2026-11-22, from the final code). `pnpm dist:mac` produces
`release/OpenSpeech-0.1.0-arm64.dmg`: 797 MB (290 MB before the speech model); the app is 1.4 GB on disk, of
which the speech model is 673 MB and the 30 Kokoro voices 108 MB.

| Check | Result |
|---|---|
| Runtime self-checks in `bundle-engine.sh` | Pass: imports including sherpa-onnx, nothing on `sys.path` or linked from outside the bundle, no GPL/AGPL distribution |
| Engine started the way the app starts it, clean environment, scratch profile, sound off | Keys come from `provider.env`: assistant, AI cleanup, vision, OpenAI and Gemini voices available. Listening on-device. Both models "Included with the app". 30 of 30 Kokoro voices. A Spanish text is read by `ef_dora` |
| The packaged app, scratch profile, sound off, bar never opened | Engine ready, bar page loaded, the app's own engine reports the bundled keys and on-device listening; quit left no process behind |

`provider.env` inside the package holds the three provider keys in plain text. That is what "keys set before
building" means until the hosted deployment exists: this DMG must not leave machines we control.
`OPEN_SPEECH_BUNDLE_KEYS=0 pnpm dist:mac` builds one that can be handed around (local voice and listening only).

## What was verified by hand (2026-11-21)

| Flow | How | Result |
|---|---|---|
| Keyless onboarding, keyboard | Fresh profile, three steps (the first was a key step, since removed), prompts spoken by Kokoro | Works; ended on a typing-first bar (the bar now listens without a key) |
| Onboarding by voice only | Scripted speech-to-text: "skip", "the second one", "yes" | Works; ends listening for the first command |
| Read a two-column PDF | Open file → reader | Plays at once; soft sentence tint, outlined word; headers, citations and URLs hatched as skipped |
| Read a web page | Type a link → live page in the reader | Plays; in-page sentence and word highlight; Script view shows exactly what is spoken |
| Voice commands | "read my clipboard", "say SQL as sequel" → spoken card → "yes" | Works; replies spoken and replayable; lexicon saved |
| Spending, voices, keyless guidance | Typed commands | Calm, labelled cards (the "add a key" row of that build no longer exists) |
| Packaged app | DMG → fresh profile → onboarding → PDF | See [Packaged app](#packaged-app-2026-11-21-before-the-speech-model-and-build-time-keys) |
| CLI | `open-speech models install kokoro-82m-v1.0`, `open-speech read notes.md` | Works without a running engine |

## Functional requirements (P0)

| ID | Requirement | Status | Notes |
|---|---|---|---|
| FR-BAR-1 | Hotkey opens the bar listening | Partial | Opens listening with the local recognizer, no key needed (verified via the app's open event). ⌥Space is registered but wasn't pressed physically |
| FR-BAR-2 | Live transcript | Verified | 2026-11-21: real microphone + OpenAI speech-to-text, partials about every 0.7–1 s. 2026-11-22: local recognizer with recordings, partials about every 0.6 s |
| FR-BAR-3 | Typing switches to text mode | Tested | Including: text typed while an utterance is processed is kept |
| FR-BAR-4 | Submit on release, end of speech, Enter | Verified | End of speech (energy VAD) and Enter by hand; push-to-talk in unit tests |
| FR-BAR-5 | Results as rows | Verified | Answers, action cards with speech diff, lists, spending card |
| FR-BAR-6 | Replay recording and reply | Tested | Replay rows render; replay itself covered by integration tests |
| FR-BAR-7 | Drop or paste file, URL, image, text | Partial | Badges verified (inserted and typed links); the drop and paste handlers are tested but not driven with a real pointer |
| FR-BAR-8 | Recents and search | Partial | Recents verified; full-text search tested |
| FR-BAR-9 | Menu-bar icon shows state | Not verified | Icons and state machine exist; not inspected on screen |
| FR-BAR-11 | Fast-path commands without the LLM | Verified | English and Spanish grammars (about 200 cases); spoken in both languages against the real recognizer |
| FR-BAR-12 | Composer with an `@` file menu | Verified | Menu, folder browsing and badges in the app; search confinement tested |
| FR-BAR-13 | Several attachments read in order | Verified | "…and read them" → three items, two queued; queue commands tested |
| FR-BAR-14 | Command or dictation by context | Tested | Controller tests; not spoken in the app |
| FR-BAR-15 | Icon-first | Verified | Bar, reader and rules editor reviewed in both themes and both languages |
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
| FR-MM-1 | Images become speech | Verified | Live Claude vision: chart described with values in 2.5 s, $0.004 |
| FR-VOICE-1 | One voice list | Verified | |
| FR-VOICE-2 | Preview without switching | Verified | Real Kokoro synthesis |
| FR-VOICE-3 | Default, per-document, assistant voice | Tested | Default set from onboarding verified |
| FR-VOICE-4 | Style instructions | Tested | Adapter fixtures only (cloud voices themselves verified live) |
| FR-VOICE-5 | Local Kokoro, offline | Verified | Bundled in the app; sha256-pinned; Spanish voices read a Spanish document (not listened to) |
| FR-VOICE-8 | Voice follows the document's language | Verified | Spanish document read by a Spanish voice with an English default |
| FR-RULE-1 | Presets, Classic / AI Enhanced | Verified | AI Enhanced live: Claude span ops on top of Classic, $0.009 for a 2-page paper (estimate $0.0095) |
| FR-RULE-2 | Rules by voice or text with preview | Tested | "Stop reading the footnotes on this site" end to end with the deterministic compiler |
| FR-RULE-3 | Markdown rule files with scopes | Tested | |
| FR-RULE-4 | Pronunciation lexicon | Verified | By voice, with spoken confirmation |
| FR-RULE-5 | Script view | Verified | |
| FR-RULE-6 | `SPEECH.md` | Tested | Starts empty |
| FR-RULE-10 | Rules editor | Verified | Opened by voice, names and switches, a typed rule saved and compiled; rename, whole-file view, delete and outside changes are tested, not driven in the app |
| FR-ASST-1 | One Claude agent | Verified | Live: summaries, voice change proposed as a confirmation card and confirmed |
| FR-ASST-2 | Spoken replies; reading pauses | Verified | Reading resumes after the reply even if the bar stays open |
| FR-ASST-3 | Voice parity for settings | Tested | |
| FR-ASST-4 | Confirm mutating actions | Verified | |
| FR-ASST-5 | "Where am I?" | Tested | |
| FR-ASST-6 | Usage questions | Verified | Always labelled estimated |
| FR-ASST-7 | Spoken errors with the fix | Verified | "Not part of this build" replies say what works, in the interface language, and ask for nothing |
| FR-ASST-10 | Local speech-to-text | Partial | Verified with synthetic speech in English and Spanish; no human voice yet |
| FR-ASST-11 | The assistant reads links | Tested | Link boundary and local-address refusal with a scripted assistant; not run against Claude |
| FR-COST-1 | Ledger | Verified | $0 rows for Kokoro and cache hits |
| FR-COST-2 | Usage summaries | Verified | |
| FR-COST-3 | Pre-flight estimate | Tested | Within ±25% in tests |
| FR-COST-4 | Monthly budget | Tested | Hard stop falls back to Kokoro |
| FR-COST-5 | Editable pricing | Tested | |
| FR-CACHE-1 | Audio cache, free replays | Tested | |
| FR-CACHE-2 | Cap, LRU, pins, clear | Tested | |
| FR-CACHE-3 | Incremental re-synthesis | Tested | |
| FR-CACHE-4 | Extraction and plan caches | Tested | |
| FR-CACHE-5 | One local asset store | Tested | Voice, transcript and page assets; page caching, revalidation and offline fallback |
| FR-API-1 | REST + WebSocket with token auth | Tested | 13 security contract tests |
| FR-API-2 | Generated TypeScript client | Tested | `pnpm check:api` |
| FR-API-3 | CLI | Verified | |
| FR-API-6 | Headless operation | Verified | |
| FR-SETUP-1 | Keys are build configuration | Tested | Environment and `.env` tested; `provider.env` read by the packaged engine (2026-11-22) |
| FR-SETUP-2 | The app never handles keys | Tested | No key route, command or field; `/keys` answers 404 |
| FR-SETUP-3 | Speak and listen from first launch | Partial | Kokoro and the speech model bundled; the packaged engine listens and speaks from a clean profile (2026-11-22) |
| FR-SETUP-4 | First run ≤ 3 steps, spoken, skippable | Verified | By voice against the real recognizer |
| FR-SETUP-5 | Appearance | Verified | Lavender and Ember in the bar, reader and rules editor; reader font options not reviewed |
| FR-SETUP-6 | Remappable shortcuts | Not verified | |
| FR-SETUP-7 | Graceful missing keys | Verified | |
| FR-SETUP-8 | Recording retention | Tested | |
| FR-SETUP-9 | English and Spanish | Verified | Switched by voice; interface and replies seen in the app; the tray menu's Spanish was not inspected |

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
| NFR-10 Licensing | Verified | 88 bundled packages, no GPL/AGPL in the 2026-11-21 build; the package list has changed since (sherpa-onnx added, keyring removed) and the license check runs again in every package build |

## Live providers (2026-11-21)

Run with real keys from a local `.env` (development; that build did not bundle keys, today's build script
does unless told not to) and the MacBook microphone.
Total spend for the session: about $0.10.

| Check | Result |
|---|---|
| Keys | All three detected; 13 OpenAI, 60 Gemini and 27 Kokoro voices available |
| OpenAI `gpt-4o-mini-tts` | Preview 3.3 s; reading starts in 1.41 s; about $0.0012 per short sentence |
| Gemini `gemini-3.8-flash-tts` | Preview 2.1 s; about $0.001 |
| Claude AI Enhanced cleanup | Span ops applied on top of Classic filters; cost within the estimate |
| Claude vision | Chart described, values read, 2.5 s |
| Claude assistant | Accurate two-sentence summary; "use a calmer voice for everything" → card → confirmed |
| Microphone + OpenAI speech-to-text | Live partials as people speak; end of speech in a normal room after the VAD fix |
| Spending answer | "About ten cents this month, estimated, mostly Claude…" |

Fixed because of this run: no live partials (interim transcription was off); the end-of-speech detector
assumed a near-silent room; Chromium's gain control changed the system input volume; noise transcribed as
Korean or Japanese words became paid assistant turns (now: language hint plus a noise filter); the
assistant asked in prose instead of proposing a confirmation card; preview responses misreported cost.

Not yet done live: a full spoken command run with the real microphone (the room had other people talking,
and their speech was being transcribed, so the test was stopped), Gemini speech-to-text.

## Packaged app (2026-11-21, before the speech model and build-time keys)

`pnpm dist:mac` built `OpenSpeech-0.1.0-arm64.dmg` (about 290 MB; the app is 728 MB installed:
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
| Time to first audio, Kokoro, Classic (≤ 0.5 s p50) | Per utterance, warm, 89-character first sentence: 0.65 s p50 (was 1.19 s). Cold with a fresh profile, opening a PDF and playing in one request: 0.81 s. An already stored utterance: 0.05 s. Table and causes in `05` § Measured |
| Speech recognition after the microphone stops | 69 ms for 1 s of speech, 141 ms for 3 s, 198 ms for 6 s (median) |
| Engine API latency during synthesis | 2–30 ms |
| URL import (Wikipedia article) | 0.24 s after the first import of the session |
| Kokoro RAM (≤ 1.3 GB) | 838 MB engine RSS |
| Speech recognizer RAM | The engine grows by about 1.9 GB while it is loaded, and returns to about 0.43 GB after it unloads |

## Not done yet

- A run on a second, clean Mac, and the Gatekeeper path for a downloaded copy.
- A full spoken-command run with a human voice and the real microphone in a quiet room, in English and Spanish; Gemini speech-to-text.
- Listening to the Spanish voices, and to the first second of playback after the first-audio change.
- The new assistant tools (links, rule maintenance) against Claude itself.
- The hosted deployment that is to replace keys set at build time.
- VoiceOver pass; physical hotkeys and media keys; menu-bar icon states on screen.
- Developer ID signing and notarization (open question in spec 10).

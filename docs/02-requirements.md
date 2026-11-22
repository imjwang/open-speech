# 02 — Requirements

Status: Draft v0.1 · 2026-11-21

**Priority:** P0 = MVP · P1 = fast-follow · P2 = later.
**Milestones** (M0–M5) are defined in `10-delivery.md`. **Spec** points to the doc that defines the behavior.
IDs are stable. Never renumber; deprecate instead.

## User stories

- *Maya (dyslexic student):* "I press a key, say 'read my biology paper and skip the citations', and it just starts, without me reading anything." → FR-BAR-1, FR-BAR-2, FR-ASST-1, FR-RULE-2
- *Maya:* "When a section is dense, I say 'summarize this section' instead of listening to all of it." → FR-ASST-8, FR-RULE-8
- *Dev (knowledge worker):* "I copy a paragraph from anywhere, hit a hotkey, and hear it at 2.5x." → FR-READ-5, FR-READ-12
- *Dev:* "On arxiv.org it never reads references, figure numbers or page headers again." → FR-RULE-1, FR-RULE-3
- *Ruth (low vision):* "I ask 'what's on my clipboard?' and it describes the chart I copied." → FR-MM-1
- *Ruth:* "I ask 'how much did I spend this month?' and hear the answer, broken down by provider." → FR-ASST-6, FR-COST-2
- *Sam (developer):* "I `curl` the engine to read a file, or call it from Claude Code over MCP." → FR-API-1, FR-API-3, FR-API-4
- *Sam:* "I point it at my own local TTS server and never pay for audio." → FR-VOICE-7

## Functional requirements

### Command bar and shell (BAR)

| ID | P | Requirement | Acceptance criteria | Spec | M |
|---|---|---|---|---|---|
| FR-BAR-1 | P0 | A global hotkey opens the command bar **already listening** | Bar visible ≤ 150 ms after ⌥Space; mic level meter live; works from any app | 09 | M2 |
| FR-BAR-2 | P0 | Live STT transcript appears in the input as the user speaks | First partial ≤ 700 ms after speech onset; partials replace in place; final text replaces partials | 07, 04 | M2 |
| FR-BAR-3 | P0 | Typing switches to text mode | Any keystroke stops the mic; existing transcript becomes editable text | 09 | M2 |
| FR-BAR-4 | P0 | Submit on push-to-talk release, end of speech, or Enter | End of speech = 800 ms silence (configurable); each path submits exactly once | 07 | M2 |
| FR-BAR-5 | P0 | Results render as rows: answer, action card, list, usage card | Action card confirms with Enter or a spoken "yes" and cancels with Esc or "no"; rows are keyboard navigable | 09 | M2 |
| FR-BAR-6 | P0 | Replay the last recording and the assistant's reply | ▶ on the user's utterance plays the stored recording; ▶ on the reply replays its audio; earlier turns this session are scrollable | 07, 09 | M2 |
| FR-BAR-7 | P0 | Drop or paste a file, URL, image or text onto the bar | The default row is "Read it"; alternatives: Describe (images), Summarize, Open in reader | 09 | M2 |
| FR-BAR-8 | P0 | Recents and search | The bar lists the last 10 documents with progress; typing filters titles and full text; Enter resumes at the saved position | 09 | M2 |
| FR-BAR-9 | P0 | Menu-bar icon shows state | Icon distinguishes idle, listening, playing, error; click opens the bar; menu has play/pause, recents, quit | 09 | M2 |
| FR-BAR-10 | P1 | Floating now-playing pill | Toggleable; shows title, time left, play/pause, speed | 09 | — |
| FR-BAR-11 | P0 | Fast-path commands run locally without the LLM | "pause", "play", "stop", "faster", "slower", "2x", "skip", "back", "next paragraph", "read clipboard" and "where am I" execute in ≤ 100 ms with no API call; the grammar is documented in 07 | 07 | M2 |

### Reading (READ)

| ID | P | Requirement | Acceptance criteria | Spec | M |
|---|---|---|---|---|---|
| FR-READ-1 | P0 | Open and read PDFs | Drop, "open …" in the bar, or `open-speech read file.pdf`; text PDFs extract with reading order and typed blocks | 05 | M1 |
| FR-READ-2 | P0 | Open URLs in the in-app reader with persistent logins | The page renders in the reader; "Read this page" reads the main content; logins survive restarts | 03, 05 | M3 |
| FR-READ-3 | P0 | Open and read DOCX | Headings, lists, tables and footnotes map to block types | 05 | M3 |
| FR-READ-4 | P0 | Read TXT/MD and pasted text | Markdown structure maps to blocks | 05 | M1 |
| FR-READ-5 | P0 | Read-clipboard hotkey | ⌥⇧R reads clipboard text; an image goes to FR-MM-1; no OS permission prompts | 09 | M2 |
| FR-READ-6 | P0 | Highlight the current sentence and word in the reader | Sentence highlight is exact; word highlight is exact on Kokoro and estimated on cloud voices (≤ 1 word drift inside a sentence, median) | 05 | M2 |
| FR-READ-7 | P0 | Follow mode and click-to-start | Auto-scroll keeps the sentence in view; manual scroll releases follow; "back to playhead" restores it; clicking a sentence starts there | 09 | M2 |
| FR-READ-8 | P0 | Navigation | Play/pause, ±sentence, ±paragraph, ±10 s, and by voice "go to page 12" / "next heading" | 04, 07 | M1 |
| FR-READ-9 | P1 | Read the selection in any app | Global hotkey reads the selected text; requests the Accessibility permission with a spoken explanation | 09 | — |
| FR-READ-10 | P1 | Screenshot to listen | Hotkey → region select → Claude vision → transcribe or describe | 05 | — |
| FR-READ-11 | P1 | AI Enhanced extraction for scanned or hard PDFs | Hard pages go to Claude PDF/vision input; estimate shown and confirmed before spending | 05 | — |
| FR-READ-12 | P0 | Speed 0.5x–5x | 0.1 steps; pitch preserved; no re-synthesis; a change audibly applies ≤ 300 ms | 05 | M1 |
| FR-READ-13 | P1 | Auto-ramp speed | Optional ramp, e.g. +0.1x every 900 words up to a ceiling | 05 | — |
| FR-READ-14 | P0 | Resume position | Position persists per document across app and engine restarts (saved every 5 s and on pause) | 03 | M1 |
| FR-READ-15 | P1 | Export audio | Ogg/Opus or MP3 for a document or a range, at any speed; estimate first | 05 | — |
| FR-READ-16 | P2 | EPUB | — | 05 | — |
| FR-READ-17 | P0 | Media keys, global play/pause, background playback | Playback continues with every window closed; media keys and ⌥⇧Space control it | 03, 09 | M2 |

### Multimodal (MM)

| ID | P | Requirement | Acceptance criteria | Spec | M |
|---|---|---|---|---|---|
| FR-MM-1 | P0 | Images become speech | Open, paste, drop or clipboard; Claude chooses *transcribe* (text-heavy) or *describe* (visual), overridable by command or rule; cost logged | 05 | M1 |
| FR-MM-2 | P1 | Describe figures and charts inside PDFs and pages | A rule such as "describe figures in one sentence" produces utterances anchored to the figure block | 05, 06 | — |
| FR-MM-3 | P2 | Local video files (**deferred**, out of scope for now) | — | 05 | — |
| FR-MM-4 | P1 | Local audio files | Transcript → rules → re-voiced or summarized | 05 | — |
| FR-MM-5 | P2 | Videos in web pages (**deferred**) | — | 05 | — |

### Voices (VOICE)

| ID | P | Requirement | Acceptance criteria | Spec | M |
|---|---|---|---|---|---|
| FR-VOICE-1 | P0 | One voice list across providers | OpenAI, Gemini and Kokoro voices as rows, filterable by provider and language; providers without a key are hidden with a reason | 08 | M0 |
| FR-VOICE-2 | P0 | Preview without switching | ▶ plays a cached sample (first preview may cost; logged) | 08 | M2 |
| FR-VOICE-3 | P0 | Default voice, per-document override, separate assistant voice | Precedence: document > default; the assistant voice is independent | 08 | M1 |
| FR-VOICE-4 | P0 | Style instructions where supported | Free-text style passed to OpenAI `instructions` / Gemini `speech_metadata.style`; ignored with a notice elsewhere | 08 | M1 |
| FR-VOICE-5 | P0 | Local Kokoro voice, offline | Works with no network or keys; automatic fallback when offline or over budget; exact word timestamps | 08 | M0 |
| FR-VOICE-6 | P2 | Local Qwen3-TTS, preset voices only (no cloning) | Offered only on capable Apple Silicon Macs; managed install with size and RAM disclosed | 08 | — |
| FR-VOICE-7 | P1 | Bring-your-own OpenAI-compatible server | Base URL + key + model + voice; tested against Kokoro-FastAPI and mlx-audio | 08 | — |
| FR-VOICE-8 | P1 | Per-language voice pinning | Language-detected blocks use the pinned voice | 08 | — |

### Rules (RULE)

| ID | P | Requirement | Acceptance criteria | Spec | M |
|---|---|---|---|---|---|
| FR-RULE-1 | P0 | Built-in skip presets with Classic / AI Enhanced | Presets: Headers, Footers, Footnotes, Tables, Formulas, Citations, URLs, Parentheses, Brackets, Braces, Page numbers, Captions, Code. Classic = filters only (no API cost); AI Enhanced adds Claude span ops | 06 | M1 |
| FR-RULE-2 | P0 | Author rules by voice or text with preview | Assistant proposes a rule file → speech-diff preview on the current document → confirm saves; cancel leaves no trace | 06, 07 | M3 |
| FR-RULE-3 | P0 | Rules are Markdown files with scopes | `global`, `sites`, `types`, `docs` scopes; precedence doc > site > type > global; hand edits to files are picked up within 1 s | 06 | M3 |
| FR-RULE-4 | P0 | Pronunciation lexicon | "Say X as Y" adds a row to `lexicon.csv`; applied on every provider | 06 | M1 |
| FR-RULE-5 | P0 | Script view | The reader toggles to show exactly what will be spoken; skipped spans are dimmed in the source view with the rule name on hover | 06, 09 | M3 |
| FR-RULE-6 | P0 | Global preferences file `SPEECH.md` | Free-form preferences included in every Claude plan call | 06 | M3 |
| FR-RULE-7 | P1 | Skills (Agent Skills format) | Valid `SKILL.md` packs load; suggested by document type ("This looks like a paper — use Academic Paper?") | 06 | — |
| FR-RULE-8 | P1 | Generative transforms | Summarize, explain or extract rules produce section-anchored utterances | 05, 06 | — |
| FR-RULE-9 | P1 | Share rules and skills | Export/import a folder or zip; no app-specific state inside | 06 | — |

### Assistant (ASST)

| ID | P | Requirement | Acceptance criteria | Spec | M |
|---|---|---|---|---|---|
| FR-ASST-1 | P0 | One Claude agent behind the bar (voice and text) | Tools cover rules, voices, playback, sources, settings, usage (catalog in 07) | 07 | M4 |
| FR-ASST-2 | P0 | Spoken replies; reading pauses | Replies use the assistant voice; reading pauses and resumes exactly where it was | 07 | M4 |
| FR-ASST-3 | P0 | Voice parity for settings | An automated test asserts every key in the settings schema is readable and writable via tools | 07 | M4 |
| FR-ASST-4 | P0 | Confirm mutating actions | Rule, budget, setting and model-install changes need a confirmation; playback and speed don't | 07 | M4 |
| FR-ASST-5 | P0 | "Where am I?" | Answers document, section, page or percent, and time left at the current speed | 07 | M4 |
| FR-ASST-6 | P0 | Usage questions | Answers with ledger numbers by period, provider, feature or document, always labelled "estimated" | 07, 08 | M4 |
| FR-ASST-7 | P0 | Spoken errors with the fix | Missing key, budget stop, offline and provider errors are explained in one sentence with the next step | 07 | M4 |
| FR-ASST-8 | P1 | Questions and summaries about the current document | Answers cite the section; summaries can be queued as utterances | 07 | — |
| FR-ASST-9 | P1 | Hands-free (VAD) and barge-in | Speaking interrupts the reply; echo of our own audio doesn't trigger it | 07 | — |
| FR-ASST-10 | P1 | Local STT | Push-to-talk works with no cloud key | 07, 08 | — |

### Usage and cost (COST)

| ID | P | Requirement | Acceptance criteria | Spec | M |
|---|---|---|---|---|---|
| FR-COST-1 | P0 | Ledger of every billable call | Rows carry provider, model, op, units, estimated USD, pricing version, document; cache hits and local runs logged at $0 | 08 | M0 |
| FR-COST-2 | P0 | Usage summaries | Bar card and spoken summary for today, week and month, by provider, feature and document, including cache savings | 08, 09 | M4 |
| FR-COST-3 | P0 | Pre-flight estimate | Work estimated above the threshold (default $0.50) asks before spending; estimate within ±25% of actual for text documents | 08 | M4 |
| FR-COST-4 | P0 | Monthly budget | Warn at 80% (spoken + row); hard stop at 100% with explicit override; reading falls back to Kokoro | 08 | M4 |
| FR-COST-5 | P0 | Editable pricing catalog | `pricing.toml` is dated and versioned; user overrides apply immediately | 08 | M0 |
| FR-COST-6 | P2 | Reconcile with provider usage APIs | — | 08 | — |

### Cache (CACHE)

| ID | P | Requirement | Acceptance criteria | Spec | M |
|---|---|---|---|---|---|
| FR-CACHE-1 | P0 | Content-addressed audio cache | Replaying a document makes zero TTS calls; survives restarts | 05 | M0 |
| FR-CACHE-2 | P0 | Size cap, LRU, keep-offline, clear | Default cap 5 GB; pinned documents are never evicted; "clear cache" by voice | 05 | M1 |
| FR-CACHE-3 | P0 | Incremental re-synthesis | Editing a rule re-synthesizes only utterances whose text changed | 05 | M3 |
| FR-CACHE-4 | P0 | Extraction and plan caches with "regenerate" | Re-opening a document reuses its plan; "regenerate" re-plans | 05 | M3 |

### Engine API (API)

| ID | P | Requirement | Acceptance criteria | Spec | M |
|---|---|---|---|---|---|
| FR-API-1 | P0 | REST + WebSocket on localhost with token auth | 127.0.0.1 only; requests without a token get 401; committed OpenAPI document | 04 | M0 |
| FR-API-2 | P0 | Generated TypeScript client | The shell calls the engine only through the generated client; CI fails if it is stale | 04 | M2 |
| FR-API-3 | P0 | CLI | `serve`, `read`, `pause`, `resume`, `status`, `usage`, `voices`, `rules ls` | 04 | M0 |
| FR-API-4 | P1 | MCP server (stdio) | Tools in 04 work from Claude Code / Claude Desktop | 04 | — |
| FR-API-5 | P1 | OpenAI-compatible facade | `/v1/audio/speech` and `/v1/audio/transcriptions` with rules, cache and ledger applied | 04 | — |
| FR-API-6 | P0 | Headless operation | Every P0 reading feature works via CLI/REST with no shell running | 03, 04 | M1 |

### Setup (SETUP)

| ID | P | Requirement | Acceptance criteria | Spec | M |
|---|---|---|---|---|---|
| FR-SETUP-1 | P0 | Keys from `.env` in development | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` read from the repo-root `.env` | 08 | M0 |
| FR-SETUP-2 | P0 | Paste a key into the bar | Provider detected by prefix; stored in the OS keychain; a test call confirms; the key is never shown again | 08, 09 | M4 |
| FR-SETUP-3 | P0 | Speak from first launch | Packaged builds include Kokoro weights; CLI/dev downloads them on first use with progress | 08 | M0 |
| FR-SETUP-4 | P0 | Onboarding ≤ 3 steps, spoken, skippable | Works with no keys (local-only); steps in 09 | 09 | M4 |
| FR-SETUP-5 | P0 | Appearance | Follows the OS theme; reader font size, dyslexia-friendly font, line spacing | 09 | M2 |
| FR-SETUP-6 | P0 | Remappable shortcuts | `settings.toml`; defaults documented in 09 | 09 | M2 |
| FR-SETUP-7 | P0 | Graceful missing keys | Features needing a missing key are hidden or explain themselves; nothing crashes | 08 | M0 |
| FR-SETUP-8 | P0 | Recording retention | Default 30 days; "never keep" option; recordings are never sent anywhere except the chosen STT provider | 07 | M2 |

## Non-functional requirements

| ID | Area | Requirement |
|---|---|---|
| NFR-1 | Performance | Bar visible ≤ 150 ms; first STT partial ≤ 700 ms; time-to-first-audio p50: cached ≤ 300 ms, Kokoro ≤ 0.5 s, cloud Classic ≤ 2.5 s (measured first byte: OpenAI 1.4 s, Gemini 2.0 s), AI Enhanced first open ≤ 4.5 s; first page of a 300-page PDF readable ≤ 3 s; speed change ≤ 300 ms; shell UI at 60 fps during playback |
| NFR-2 | Reliability | Exponential backoff on 429/5xx; resume after network loss without losing position; the shell restarts a crashed engine and playback resumes; no paid work is lost to a crash (results are persisted per chunk) |
| NFR-3 | Privacy | No telemetry. All data stays local. Only configured providers receive content, and a "what gets sent where" page lists exactly what each provider receives |
| NFR-4 | Security | Per `03` § Security: loopback-only API with token and Host/Origin checks; keys only in the engine and keychain; hardened Electron |
| NFR-5 | Accessibility | WCAG 2.2 AA; full keyboard operation; VoiceOver labels and live regions; reduced motion; high contrast; **every P0 flow completable by voice alone** |
| NFR-6 | Platform | macOS 14+ on Apple Silicon is the **only** target until verification is complete. Windows, Linux and Intel Macs are out of scope for now: the engine stays portable Python, but nothing is built, packaged or tested for them |
| NFR-7 | Extensibility | A new TTS/STT provider = one adapter implementing the protocol + one `models.toml` entry + recorded test fixtures |
| NFR-8 | Cost safety | No paid call without an explicit user action, a confirmed plan, or a running read. Every paid call appears in the ledger |
| NFR-9 | Resources | Engine idle ≤ 300 MB RSS without local models; Kokoro adds ≤ 1.3 GB while loaded and unloads after 10 idle minutes |
| NFR-10 | Licensing | Core dependencies are Apache-2.0-compatible (no AGPL/GPL in the default install). Copyleft components only as optional, user-installed extras, clearly labelled |

## Traceability summary

| Area | Spec docs | Milestones |
|---|---|---|
| BAR | 07, 09 | M2 |
| READ | 03, 05, 09 | M1–M3 |
| MM | 05 | M1 (images); P1 rest |
| VOICE | 08 | M0–M2 |
| RULE | 06, 09 | M1 (presets, lexicon), M3 |
| ASST | 07 | M4 |
| COST | 08 | M0 (ledger), M4 |
| CACHE | 05 | M0–M3 |
| API | 04 | M0–M2 |
| SETUP | 08, 09 | M0–M4 |

Original brief → where it lives: PDF (FR-READ-1) · websites rendered in-app (FR-READ-2) · Electron + React Compiler + shadcn (03) · chat-managed rules and skills (FR-RULE-2..7) · Claude pre-processing (05 § Plan) · hashing and caching (FR-CACHE-*) · billing by voice (FR-ASST-6, FR-COST-*) · OpenAI + Gemini TTS (FR-VOICE-1) · BYOK (FR-SETUP-*) · headless engine + local API (FR-API-*) · command bar, listening first, live STT, recording playback (FR-BAR-*) · multimodal (FR-MM-*) · local open-weight TTS (FR-VOICE-5..7).

# Open Speech — Specification

Status: **Draft v0.1** · 2026-11-22 · Kept in step with the code. What is built and verified: [`dev/status.md`](dev/status.md).

## Reading order

| # | Doc | What it settles |
|---|---|---|
| 00 | [Overview](00-overview.md) | Vision, principles, personas, goals, non-goals, glossary |
| 01 | [Speechify analysis](01-speechify-analysis.md) | What Speechify does, what we keep/simplify/drop, switcher guide |
| 02 | [Requirements](02-requirements.md) | User stories, FR/NFR with IDs, acceptance criteria, traceability |
| 03 | [Architecture](03-architecture.md) | Engine + shell + surfaces, stack, layout, storage, security |
| 04 | [Engine API](04-engine-api.md) | REST, WebSocket protocol, CLI, MCP, OpenAI-compatible facade |
| 05 | [Speech pipeline](05-speech-pipeline.md) | Ingest → extract → plan → synthesize → time → play; caches; latency |
| 06 | [Rules and skills](06-rules-and-skills.md) | The reading instructions, rule files, lexicon, compiler, speech diff, Agent Skills |
| 07 | [Assistant and voice](07-assistant-and-voice.md) | Fast path, Claude agent, tools, confirmations, link and injection boundaries, voice loop, languages |
| 08 | [Providers and usage](08-providers-and-usage.md) | Provider protocols, model catalog, local voices and speech recognition, build-time keys, ledger, budgets |
| 09 | [Interface](09-interface.md) | Command bar and composer, menu bar, reader, rules editor, themes, shortcuts, accessibility, first run |
| 10 | [Delivery](10-delivery.md) | Repo layout, milestones, testing, risks, open questions |

**Short on time?** Read 00, then the decision table below, then 02.

## Decisions at a glance

| Topic | Decision |
|---|---|
| Shape | Headless Python (FastAPI) engine with a local REST/WS API; Electron shell is a thin client; CLI now, MCP next |
| UI | Voice-first, icon-first command bar (opens listening, live transcript, a composer with file and link badges, replay) + menu bar + on-demand reader + a rules editor. No settings screens |
| Cloud voices | OpenAI `gpt-4o-mini-tts`; Gemini `gemini-3.8-flash-tts` / `-lite-tts` (Interactions API) |
| Local voices | Kokoro-82M (offline, $0, exact word timing; English and Spanish); Qwen3-TTS on Apple Silicon (presets, cloned and designed voices, weights on demand); any OpenAI-compatible server (P1) |
| Voice cloning | Local only (Qwen3-TTS Base 0.6B): read a two-sentence passage or give an audio file, with the speaker's consent; the reference is cleaned and checked on-device (Parakeet), you hear the clone and keep it or not. Designed voices from a description (VoiceDesign 1.7B) |
| Speech input | On-device by default: Parakeet TDT 0.6B v3 through sherpa-onnx (offline, $0, English and Spanish with no language hint). OpenAI or Gemini speech-to-text only as a fallback in builds that include them |
| Languages | English and Spanish: interface, spoken commands, replies, reading voices, sentence rules |
| LLM | Claude only: `claude-haiku-5-5` by default, everywhere (Sonnet 5.5 / Opus 5.5 selectable). AI Enhanced annotate with quote-anchored span ops at low effort, adaptive thinking on Haiku (chosen by the accuracy gate, `08`); transforms at medium effort; assistant at low effort |
| Cleanup | One prompt, the reading instructions (`SPEECH.md`, like `CLAUDE.md`), that Claude follows; its skip lines double as free filters. Scoped rule files, lexicon, chat- or voice-authored edits with preview |
| Multimodal | Images → Claude vision (P0); audio files (P1); video deferred |
| Platform | macOS 14+ on Apple Silicon only, for now |
| Cost | Local ledger in dollars, editable pricing, budgets with hard stop, pre-flight estimates, all answerable by voice |
| Cache | Content-addressed extract and plan caches, and one local asset store for voice audio, transcripts and fetched pages; replays are free |
| Privacy | Local-only, no telemetry, no account. Microphone audio stays on the Mac by default |
| Keys | Build configuration, never entered in the app. A key inside a build is readable by whoever has the build, so builds with keys stay on our own machines; a hosted deployment with billing is planned |
| Themes | Lavender (pastel, light) and Ember (dark, orange); "system" follows the Mac |

## Conventions

- Requirement IDs (`FR-AREA-n`, `NFR-n`) are stable. Deprecate instead of renumbering.
- "(verify)" marks a fact to confirm at implementation time. Everything else was checked on the date shown.
- Model IDs and prices live in `models.toml` / `pricing.toml`, never in code.

## Doc status

| Doc | Status |
|---|---|
| 00–10 | Draft v0.1, ready for review |

# Open Speech — Specification

Status: **Draft v0.1** · 2026-11-21 · Spec only; no code yet.

## Reading order

| # | Doc | What it settles |
|---|---|---|
| 00 | [Overview](00-overview.md) | Vision, principles, personas, goals, non-goals, glossary |
| 01 | [Speechify analysis](01-speechify-analysis.md) | What Speechify does, what we keep/simplify/drop, switcher guide |
| 02 | [Requirements](02-requirements.md) | User stories, FR/NFR with IDs, acceptance criteria, traceability |
| 03 | [Architecture](03-architecture.md) | Engine + shell + surfaces, stack, layout, storage, security |
| 04 | [Engine API](04-engine-api.md) | REST, WebSocket protocol, CLI, MCP, OpenAI-compatible facade |
| 05 | [Speech pipeline](05-speech-pipeline.md) | Ingest → extract → plan → synthesize → time → play; caches; latency |
| 06 | [Rules and skills](06-rules-and-skills.md) | Rule files, presets, lexicon, compiler, speech diff, Agent Skills |
| 07 | [Assistant and voice](07-assistant-and-voice.md) | Claude agent, tools, confirmations, injection boundary, voice loop |
| 08 | [Providers and usage](08-providers-and-usage.md) | Provider protocols, model catalog, local models, keys, ledger, budgets |
| 09 | [Interface](09-interface.md) | Command bar, menu bar, reader, shortcuts, accessibility, onboarding |
| 10 | [Delivery](10-delivery.md) | Repo layout, milestones, testing, risks, open questions |

**Short on time?** Read 00, then the decision table below, then 02.

## Decisions at a glance

| Topic | Decision |
|---|---|
| Shape | Headless Python (FastAPI) engine with a local REST/WS API; Electron shell is a thin client; CLI now, MCP next |
| UI | Voice-first command bar (opens listening, live transcript, replay) + menu bar + on-demand reader. No settings screens |
| Cloud voices | OpenAI `gpt-4o-mini-tts`; Gemini `gemini-3.8-flash-tts` / `-lite-tts` (Interactions API) |
| Local voices | Kokoro-82M (offline, $0, exact word timing); any OpenAI-compatible server (P1); Qwen3-TTS later (P2). Preset voices only, no cloning |
| LLM | Claude only: `claude-sonnet-5-5` by default. Annotate with thinking off and quote-anchored span ops; transforms at medium effort; assistant at low effort |
| Cleanup | Rules as files: Speechify-named presets (Classic = free filters, AI Enhanced = Claude span ops), lexicon, chat- or voice-authored rules with preview |
| Multimodal | Images → Claude vision (P0); audio files (P1); video deferred |
| Platform | macOS 14+ on Apple Silicon only, for now |
| Cost | Local ledger in dollars, editable pricing, budgets with hard stop, pre-flight estimates, all answerable by voice |
| Cache | Content-addressed extract / plan / audio caches; replays are free |
| Privacy | Local-only, no telemetry, BYOK |

## Conventions

- Requirement IDs (`FR-AREA-n`, `NFR-n`) are stable. Deprecate instead of renumbering.
- "(verify)" marks a fact to confirm at implementation time. Everything else was checked on the date shown.
- Model IDs and prices live in `models.toml` / `pricing.toml`, never in code.

## Doc status

| Doc | Status |
|---|---|
| 00–10 | Draft v0.1, ready for review |

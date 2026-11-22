# 00 — Overview

Status: Draft v0.1 · 2026-11-21

## What Open Speech is

Open Speech is an open-source, bring-your-own-key alternative to Speechify. It is a **headless speech
engine with a local API**. You reach it through a desktop shell that stays almost invisible: a menu-bar
icon, a voice-first **command bar**, and a reader window that opens only for PDFs and web pages.

You point it at something: a PDF, a web page, a Word document, the clipboard, or an image.
It reads that aloud in a voice you choose, following **speech rules you write in plain language**
("skip the citations", "say SQL as sequel", "describe the figures in one sentence"). Claude turns those
rules into a cleaned or transformed script before any audio is generated. Audio is cached by content
hash, so nothing is paid for twice. Every API call is logged with an estimated cost, and you can ask
for it out loud: "How much have I spent this month?"

## Problem

- Speechify-class readers read the wrong things (headers, footnotes, citations, page numbers), and users can't fix that beyond a fixed set of toggles.
- Pricing is opaque (word quotas, trials that roll into annual plans), and that drives most billing complaints.
- The product has sprawled (podcasts, meetings, studio, audiobooks). The reader is buried under upsells.
- Users who most need audio (dyslexia, ADHD, low vision) still have to read screens to configure it.
- Everything goes through a vendor cloud, with no choice of voice provider and no local option.

## Principles

1. **Listening-first, voice-first.** Every capability works eyes-free. The command bar opens listening. Following along visually is optional, never required.
2. **Engine first, UI last.** All behavior lives in the engine and its API. The shell only renders and relays. Anything the UI can do, the CLI, the API and (later) MCP can do.
3. **One cleanup mechanism.** Skip toggles, pronunciations, and chat- or voice-authored instructions are all **rules**, stored as files.
4. **Transparent cost.** Real dollar estimates per provider and per document. Cache hits are free and shown as savings. Local voices cost $0.
5. **Local-first, private, open standards.** No accounts and no telemetry. Documents go only to the providers you configured. Skills follow the Agent Skills spec, the API is described by OpenAPI, and tools are exposed over MCP.
6. **Calm.** No upsells, badges or onboarding marathons. As little UI as possible.

## Personas

| Persona | Needs | What Open Speech gives them |
|---|---|---|
| **Maya**, dyslexic/ADHD student | Listen to readings at 1.5–2x without fighting the layout; never be forced to read settings | Voice-operated everything; academic-paper rules; summaries when a section is dense |
| **Dev**, knowledge worker | Papers, docs and articles at 2.5–3x; skip boilerplate | Rules per site and document type; AI Enhanced skipping; clipboard hotkey |
| **Ruth**, low-vision user | Operate the whole app without seeing it | Command bar opens listening; spoken replies; VoiceOver-labelled UI; image descriptions |
| **Sam**, developer / tinkerer | Script it, self-host voices, extend it | REST/WS API, CLI, MCP, rule and skill files, local models, provider adapters |

## Goals (MVP)

- Read PDF, web pages (rendered in-app), DOCX, TXT/MD, pasted text, clipboard text, and images aloud with good voices.
- Cloud voices from OpenAI and Gemini, plus a local, offline voice (Kokoro) that needs no key.
- Speech rules authored by voice or text through Claude, previewed before saving, stored as files.
- Usage and cost ledger with budgets and pre-flight estimates, answerable by voice.
- Content-addressed caching of extraction, speech plans and audio.
- A local engine API (REST + WebSocket) and CLI, with a thin Electron shell on top.

## Non-goals (MVP)

- Accounts, payments, cloud library, cross-device sync, mobile apps, browser extension.
- Podcasts, meeting notes, dubbing, audiobook store.
- Voice cloning or custom/self voices. Preset voices only.
- Video sources (deferred).
- Windows, Linux and Intel Mac builds. macOS on Apple Silicon is the only target until verification is complete.
- Rich document editing or annotation.
- Running non-Claude LLMs for rules or the assistant.

## Success metrics

- Open a 300-page PDF, start listening in ≤ 3 s, and hear **zero** headers, footers or page numbers with the Classic presets.
- A switching Speechify user finds every "day-one" feature (see `01`) or a deliberate replacement within 10 minutes.
- A user completes onboarding and changes voice, speed, a rule and the budget **without looking at the screen**.
- The cost for any document is visible before it is spent. Replays cost $0.
- Adding a TTS provider takes one adapter module and one catalog entry.

## Glossary

| Term | Meaning |
|---|---|
| **Engine** | The Python service that does all the work (`engine/`). Headless; exposes REST, WS, CLI and later MCP |
| **Shell** | The Electron app (`app/`): menu bar, command bar, reader. A client of the engine |
| **Command bar** | Global-hotkey floating bar. Opens listening, shows the live transcript, takes typed commands |
| **Source** | A thing to read: file, URL, clipboard content, image |
| **Document** | A source extracted into typed **blocks** (paragraph, heading, footnote, …) with stable IDs |
| **Rule** | A Markdown file with a scope and an instruction that changes what is spoken |
| **Skill** | A reusable pack of rules and instructions in Agent Skills format |
| **Speech plan / script** | The ordered **utterances** actually spoken, each anchored back to the source |
| **Span op** | A Claude-proposed edit on a block (skip or replace a quoted span); never a whole-text rewrite |
| **Fidelity** | How precisely an utterance maps to the source: `exact` (word), `block`, `section`, `media` (time range) |
| **Classic / AI Enhanced** | Rule modes: deterministic filters only (free, instant) vs. filters plus Claude span ops |
| **Transport** | The engine's single playback session (queue, position, speed, voice) that every surface controls |
| **Ledger** | Local record of every billable call with units and estimated cost |

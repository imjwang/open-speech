# Open Speech

An open-source alternative to Speechify. Press a hotkey and **tell it what to read**, in English or
Spanish: a PDF, a web page, your clipboard, an image, or several of them one after another. It reads
aloud in the voice you choose, following speech rules you write in plain language.

> **Status:** v0.1, first working build. The engine, the menu-bar app and the macOS package work
> end to end on Apple Silicon. See [`docs/dev/status.md`](docs/dev/status.md) for what is verified
> and what isn't yet. The spec lives in [`docs/`](docs/README.md).
> **Platform:** macOS 14+ on Apple Silicon.

## What makes it different

- **Voice-first, icon-first.** The command bar opens listening, and it listens on this Mac: speech recognition runs on-device, with no account and no network. Change voices, speed, themes, rules and budgets by talking. What is on screen is icons and a few controls, not settings pages.
- **Your rules.** "Skip the citations." "Say SQL as sequel." "Describe figures in one sentence." Rules are plain text files, previewed before they're saved, and editable in a small built-in editor.
- **Local first, costs in the open.** The Kokoro voices (English and Spanish) and the speech recognizer are included and work offline for $0. Builds that include OpenAI or Gemini add cloud voices. Every paid call is logged with an estimated cost, and you can ask "how much have I spent this month?"
- **Claude in the loop** (in builds that include it). Claude cleans text before it's spoken, describes images, reads links you give it, and runs the assistant.
- **Headless engine with an API.** Everything the app does is available over a local REST/WebSocket API and a CLI, with MCP to follow.
- **Local and private.** No accounts, no telemetry. Speech, transcripts and fetched pages are stored on this Mac by content hash, so replays are free.

## Install (build the app)

```bash
cd app
bun install
bun run dist:mac    # bundles Python, the engine, the Kokoro voices and the speech recognizer
                     # (the first build downloads about 750 MB of model weights)
open release/OpenSpeech-0.1.0-arm64.dmg
```

Drag Open Speech to Applications and open it. It lives in the menu bar (no Dock icon). Press
**⌥ Space** and tell it, or type, what to read. Setup is two short questions you can answer out loud.
Nothing asks for a key or an account: the voices and the speech recognizer are included and work offline.

The build is ad-hoc signed, not notarized. A copy you build yourself opens normally; a copy
downloaded from elsewhere needs **System Settings → Privacy & Security → Open Anyway** once.

### Provider keys are set before building

Cloud features are part of a build or they are not. The app never asks for a key, shows one or stores
one. Set the keys you want in the repo-root `.env` (or the environment) before `bun run dist:mac`; the
build copies those three values, and nothing else from `.env`, into the app.

| Key at build time | Adds |
|---|---|
| `ANTHROPIC_API_KEY` | Free-form requests ("summarize this chapter"), AI Enhanced cleanup, image descriptions, rules that need judgment |
| `OPENAI_API_KEY` or `GEMINI_API_KEY` | Cloud voices, and cloud speech-to-text as a fallback |

With no keys, everything else works offline: talking to the bar, reading PDFs, web pages, DOCX, text
and the clipboard, queues of several items, the reading instructions, pronunciations, the rules editor, voices, speed,
navigation, themes and spending. `OPEN_SPEECH_BUNDLE_KEYS=0 bun run dist:mac` builds without keys even
when `.env` has them, and `OPEN_SPEECH_BUNDLE_STT=0` leaves out the speech model (about 640 MB; the app
then offers to download it).

> **A key inside an app can be read by anyone who has that app.** Builds with keys are for our own
> machines while we test. Anything handed to other people must be built without keys. A hosted
> deployment, where we hold the keys and bill for use, is the planned replacement; it is not built yet.

## Develop

```bash
cp .env.example .env               # optional build-time keys: ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY
cd engine && uv sync --all-extras
uv run pytest                      # 853 tests, no network, no sound
uv run open-speech models install parakeet-tdt-0.6b-v3-int8   # speech recognition for development (642 MB)
uv run open-speech read ../some-paper.pdf

cd ../app && bun install
bun run test && bun run typecheck  # 259 tests
bun run dev                        # Electron; starts the engine with `uv run`
```

`OPEN_SPEECH_FIXTURE_PROVIDERS=1` swaps in deterministic offline providers (tone voices, scripted
speech-to-text) for automated UI runs; `OPEN_SPEECH_AUDIO_OUTPUT=null` keeps playback silent (the test
suite sets it by itself). `app/e2e/driver.mjs` drives the real app for hands-on checks, including a
microphone fed from recordings (`say -o`), so spoken flows can be tested without the real microphone
or the speakers.

## Coming from Speechify?

See the [switcher guide](docs/01-speechify-analysis.md#switcher-guide-goes-into-the-readme). The same
auto-skips (headers, footers, footnotes, citations, …) are lines in one editable prompt, the reading
instructions (`rules/SPEECH.md`), which Claude follows on every document. Ask the assistant to change it
("stop reading captions") or edit it yourself.

## License

Apache-2.0. See [LICENSE](LICENSE).

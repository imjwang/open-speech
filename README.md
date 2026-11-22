# Open Speech

An open-source, bring-your-own-key alternative to Speechify. Press a hotkey and **tell it what to read**:
a PDF, a web page, your clipboard, an image. It reads aloud in the voice you choose, following speech
rules you write in plain language.

> **Status:** v0.1, first working build. The engine, the menu-bar app and the macOS package work
> end to end on Apple Silicon. See [`docs/dev/status.md`](docs/dev/status.md) for what is verified
> and what isn't yet. The spec lives in [`docs/`](docs/README.md).
> **Platform:** macOS 14+ on Apple Silicon.

## What makes it different

- **Voice-first.** The command bar opens listening. Change voices, speed, rules and budgets by talking. You never have to read a settings screen.
- **Your rules.** "Skip the citations." "Say SQL as sequel." "Describe figures in one sentence." Rules are plain Markdown files, previewed before they're saved.
- **Your keys, your costs.** Use OpenAI, Gemini, or the free local Kokoro voice, which works offline. Every call is logged with an estimated cost, and you can ask "how much have I spent this month?"
- **Claude in the loop.** Claude cleans text before it's spoken, describes images, and runs the assistant.
- **Headless engine with an API.** Everything the app does is available over a local REST/WebSocket API and a CLI, with MCP to follow.
- **Local and private.** No accounts, no telemetry. Audio is cached by content hash, so replays are free.

## Install (build the app)

```bash
cd app
pnpm install
pnpm dist:mac        # ~3 min; bundles Python, the engine and the Kokoro voice
open release/OpenSpeech-0.1.0-arm64.dmg
```

Drag Open Speech to Applications and open it. It lives in the menu bar (no Dock icon). Press
**⌥ Space** and tell it, or type, what to read. Setup takes three short steps and needs no key: the
Kokoro voice is included and works offline.

The build is ad-hoc signed, not notarized. A copy you build yourself opens normally; a copy
downloaded from elsewhere needs **System Settings → Privacy & Security → Open Anyway** once.

Keys are optional and can be pasted into the bar at any time:

| Key | Adds |
|---|---|
| Anthropic | Free-form requests ("summarize this chapter"), AI Enhanced cleanup, image descriptions, rules from plain language |
| OpenAI or Gemini | Talking to the bar (speech-to-text) and cloud voices |

Without an OpenAI or Gemini key the bar opens for typing, and everything except free-form requests
works offline: reading PDFs, web pages, DOCX, text and the clipboard, presets, pronunciations,
voices, speed, navigation and spending.

## Develop

```bash
cp .env.example .env               # optional: ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY
cd engine && uv sync --all-extras
uv run pytest                      # 270 tests, no network
uv run open-speech read ../some-paper.pdf

cd ../app && pnpm install
pnpm test && pnpm typecheck        # 202 tests
pnpm dev                           # Electron; starts the engine with `uv run`
```

`OPEN_SPEECH_FIXTURE_PROVIDERS=1` swaps in deterministic offline providers (tone voices, scripted
speech-to-text) for automated UI runs; `OPEN_SPEECH_AUDIO_OUTPUT=null` keeps playback silent.

## Coming from Speechify?

See the [switcher guide](docs/01-speechify-analysis.md#switcher-guide-goes-into-the-readme). The same
auto-skip names (Headers, Footers, Footnotes, Citations, …) and the same Classic / AI Enhanced modes are here.

## License

Apache-2.0. See [LICENSE](LICENSE).

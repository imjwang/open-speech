# Open Speech

An open-source, bring-your-own-key alternative to Speechify. Press a hotkey and **tell it what to read**:
a PDF, a web page, your clipboard, an image. It reads aloud in the voice you choose, following speech
rules you write in plain language.

> **Status:** specification phase. No runnable code yet. Start with [`docs/`](docs/README.md).
> **Platform:** macOS on Apple Silicon.

## What makes it different

- **Voice-first.** The command bar opens listening. Change voices, speed, rules and budgets by talking. You never have to read a settings screen.
- **Your rules.** "Skip the citations." "Say SQL as sequel." "Describe figures in one sentence." Rules are plain Markdown files, previewed before they're saved.
- **Your keys, your costs.** Use OpenAI, Gemini, or the free local Kokoro voice, which works offline. Every call is logged with an estimated cost, and you can ask "how much have I spent this month?"
- **Claude in the loop.** Claude cleans text before it's spoken, describes images, and runs the assistant.
- **Headless engine with an API.** Everything the app does is available over a local REST/WebSocket API and a CLI, with MCP to follow.
- **Local and private.** No accounts, no telemetry. Audio is cached by content hash, so replays are free.

## Planned quick start (once the engine exists)

```bash
cp .env.example .env              # add any of: ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY
cd engine && uv sync
uv run open-speech read ../some-paper.pdf
```

With no keys at all, Open Speech still reads with the local Kokoro voice and the Classic (free) cleanup rules.

## Coming from Speechify?

See the [switcher guide](docs/01-speechify-analysis.md#switcher-guide-goes-into-the-readme). The same
auto-skip names (Headers, Footers, Footnotes, Citations, …) and the same Classic / AI Enhanced modes are here.

## License

Apache-2.0. See [LICENSE](LICENSE).

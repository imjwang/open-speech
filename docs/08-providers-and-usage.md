# 08 — Providers, models, keys and usage

Status: Draft v0.1 · 2026-11-22 · Model IDs verified against live APIs on 2026-11-21 unless marked *(verify)*.

## Provider protocols

```python
class TtsProvider(Protocol):
    id: str                                   # "openai" | "gemini" | "kokoro" | "qwen3" | "openai_compatible"
    def voices(self) -> list[Voice]: ...
    def limits(self) -> TtsLimits: ...        # max input chars, formats, style support, languages
    async def synthesize(self, req: TtsRequest) -> AsyncIterator[AudioChunk]: ...
    timestamps: bool                          # True → chunks carry word/token timings
    def price_units(self, req: TtsRequest, result: TtsResult) -> Units: ...

class SttProvider(Protocol):          # "local" | "openai" | "gemini"
    async def stream(self, pcm: AsyncIterator[bytes], lang: str | None) -> AsyncIterator[SttEvent]: ...   # partial/final
    async def transcribe(self, audio: Path, words: bool = False) -> Transcript: ...

class LlmProvider(Protocol):      # Anthropic only, for now
    async def annotate(self, blocks, rules) -> list[SpanOp]: ...
    async def transform(self, section, rules) -> list[Utterance]: ...
    def agent(self, tools) -> AgentSession: ...

class VisionProvider(Protocol):   # Anthropic only, for now
    async def describe_or_transcribe(self, image: bytes, rules, mode: Literal["auto", "transcribe", "describe"]) -> list[Block]: ...
```

The rule: nothing outside `providers/` imports a vendor SDK. Each adapter ships with **recorded fixtures**
so tests run offline.

A provider whose key is not in the build simply isn't there: it contributes no voices, the registry
returns no adapter for it, and a feature that needs it answers `unavailable` (`04` § Conventions). The
registry (`providers/registry.py`) picks the speech-to-text provider from `voice_input.stt_provider`.

## Model catalog (`models.toml`)

The bundled catalog lives at `engine/src/open_speech/data/models.toml`; user overrides in app data are
merged over it. Model IDs change often (Gemini 2.5 TTS was retired in Oct 2026, and Gemini's
`models.list` doesn't even show TTS models), so **nothing is hard-coded outside the catalog**.

```toml
[tts.openai."gpt-4o-mini-tts"]
snapshot = "gpt-4o-mini-tts-2025-12-15"
voices = ["alloy","ash","ballad","cedar","coral","echo","fable","marin","nova","onyx","sage","shimmer","verse"]  # (verify list)
formats = ["opus","mp3","aac","flac","wav","pcm"]
style = "instructions"
stream = true
max_input_chars = 4000          # (verify)
timestamps = false

[tts.gemini."gemini-3.8-flash-tts"]
api = "interactions"            # client.interactions.create, api_version v1beta, google-genai >= 2.25
voices = ["Zephyr","Puck","Charon","Kore","Fenrir","Leda","Orus","Aoede","Callirrhoe","Autonoe","Enceladus","Iapetus","Umbriel","Algieba","Despina","Erinome","Algenib","Rasalgethi","Laomedeia","Achernar","Alnilam","Schedar","Gacrux","Pulcherrima","Achird","Zubenelgenubi","Vindemiatrix","Sadachbia","Sadaltager","Sulafat"]
voices_endpoint = true          # client.voices.list() for the larger library
format = "l16"                  # 24 kHz mono PCM16 when streaming; wav when unary
style = "speech_metadata"
stream = true
timestamps = false

[tts.gemini."gemini-3.8-flash-lite-tts"]
inherit = "gemini-3.8-flash-tts"
languages = 101

[tts.kokoro."kokoro-82m-v1.0"]
local = true
weights = { url = "hf://onnx-community/Kokoro-82M-v1.0-ONNX-timestamped", quant = "q8", size_mb = 92, sha256 = "<pin>" }
ram_gb = 1.3
license = "Apache-2.0"
languages = ["en-US","en-GB","es"]   # one built-in phonemizer per language (providers/g2p*.py)
timestamps = true
# 30 voices; the first letter is the language: a = American English, b = British English, e = Spanish
# (ef_dora, em_alex, em_santa)

[tts.qwen3."qwen3-tts-12hz-0.6b"]            # P2, preset voices only (no cloning)
local = true
runtimes = ["mlx-audio"]         # Apple Silicon
size_gb = 1.3                    # GGUF Q8; ~2.5 GB bf16
ram_gb = 3
license = "Apache-2.0"
languages = ["zh","en","ja","ko","de","fr","ru","pt","es","it"]
requires = "apple-silicon"
timestamps = false

[stt.openai]
streaming = ["gpt-realtime-whisper", "gpt-live-transcribe"]   # (verify streaming semantics)
batch = ["gpt-4o-transcribe", "gpt-4o-mini-transcribe", "gpt-transcribe"]
word_timestamps = ["whisper-1"]

[stt.gemini]
streaming = ["gemini-3.8-live"]       # (verify) Live API input transcription
batch = ["gemini-3.5-transcribe"]     # word timestamps

[stt.local]
default = "parakeet-tdt-0.6b-v3-int8"

[stt.local."parakeet-tdt-0.6b-v3-int8"]   # run by sherpa-onnx; no key, offline, free
family = "nemo_transducer"
license = "CC-BY-4.0"            # the recognizer; the Silero VAD that ships with it is MIT
ram_gb = 2.0
num_threads = 2
vad = "silero_vad.onnx"          # keeps non-speech from being transcribed as words
weights = { url = "hf://csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8", revision = "<pinned commit>", size_mb = 642, files = [ <path + sha256 for each of five files> ] }

[llm.anthropic]
default = "claude-sonnet-5-5"
options = ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5", "claude-fable-5-2"]
```

### Verified on 2026-11-21 (grounding spike)

| Check | Result |
|---|---|
| OpenAI `gpt-4o-mini-tts` streaming Opus, one sentence | First byte **1.38 s**, total 2.0 s |
| OpenAI audio models present | `gpt-4o-mini-tts(-2025-12-15)`, `tts-1(-hd)`, `gpt-4o(-mini)-transcribe`, `gpt-transcribe`, `gpt-live-transcribe`, `gpt-realtime-whisper`, `gpt-realtime-2.1(-mini)`, `whisper-1` |
| Gemini TTS via `generateContent` + `speechConfig` | **Rejected** (400: unknown field). Use the Interactions API |
| Gemini `gemini-3.8-flash-tts` / `-lite-tts` via Interactions, streaming | First audio **1.97 s**; unary 4.7–5.0 s; `Kore` voice OK |
| Claude models present | `claude-sonnet-5-5`, `claude-opus-5-5`, `claude-haiku-5-5`, `claude-fable-5-2`, … |
| Claude annotate, numeric offsets, adaptive thinking low | Correct, 7.6 s, 925 output tokens |
| Claude annotate, numeric offsets, thinking off | **Wrong offsets** |
| Claude annotate, **quote-anchored**, Sonnet 5.5 thinking off | **Correct** (2/2), 2.1–2.5 s, 138 output tokens → the default |
| Claude annotate, quote-anchored, Haiku 5.5 low | Wrong judgments (dropped a cited author's name) → not recommended |

## Local models

**Tiering**

| Tier | Model | Runtime | Download / RAM | Speed | Word timing | Status |
|---|---|---|---|---|---|---|
| Default local voice | **Kokoro-82M v1.0** (Apache-2.0); 27 English and 3 Spanish voices | onnxruntime (CPU) | ~92 MB q8 + 0.5 MB per voice / ≤ 1.3 GB | ~10x real time on CPU | Exact (timestamped export) | **P0**, included in packaged builds |
| Default speech input | **Parakeet TDT 0.6B v3**, int8 (CC-BY-4.0), with Silero VAD v5 (MIT) | sherpa-onnx (CPU, 2 threads) | 642 MB / about 1.9 GB while loaded | ~25x real time | — | **P0**, included in packaged builds |
| Later | **Qwen3-TTS 0.6B** (Apache-2.0), preset voices only | mlx-audio (Apple Silicon) | ~1.3 GB Q8 / 2–3 GB | ~1.3–1.8x real time on M-series (MLX, third-party) | Via alignment | P2 |
| BYO server | Kokoro-FastAPI, mlx-audio server, vLLM-Omni, Speaches, qwentts.cpp | `openai_compatible` adapter | — | — | Via alignment (Kokoro-FastAPI `/dev/captioned_speech` optional) | P1 |
| Local alignment | Qwen3-ForcedAligner-0.6B (Apache-2.0) or whisper.cpp DTW | Python / mlx-audio / whisper.cpp | ~0.6–1 GB | background | — | v1.1 |

**Voice cloning and custom voices are out of scope**: catalog entries expose preset voices only, and the larger Qwen3-TTS 1.7B (voice design, cloning) is not included.

Excluded from the bundled catalog for license or hardware reasons (users can still add them via BYO
server): F5-TTS (CC-BY-NC), Fish/OpenAudio (non-commercial), IndexTTS-2 (restricted), Voxtral TTS
(CC-BY-NC), VibeVoice (repos withdrawn), Piper (GPL engine), and 1.6B+ GPU-only models.

**Model manager** (`local/manager.py`):
- Hardware probe: Apple Silicon generation and unified memory. Marks each catalog entry `fits | slow | unsupported`.
- Installs show size, RAM and expected speed **before** confirming (by voice too).
- Downloads resume, verify sha256 and land in `models/`. Optional Python extras install into the engine environment with `uv pip install open-speech[local-stt]`.
- Idle models unload after 10 minutes (NFR-9).
- Speech-to-text models are rows in the same list (`kind: stt`), stored under `models/stt/<model>/`, installed and removed the same way (`open-speech models install parakeet-tdt-0.6b-v3-int8`). When one finishes installing, clients are told (`capabilities.changed`) and the bar can listen without a restart.
- A packaged app carries read-only copies of Kokoro and the speech model inside the app (`app/scripts/bundle-engine.sh`, pinned by commit and sha256). `OPEN_SPEECH_BUNDLE_KOKORO=0` and `OPEN_SPEECH_BUNDLE_STT=0` leave them out; the engine or the user then downloads them.
- **License note:** Kokoro's usual phonemizer falls back to espeak-ng (GPL). Open Speech does not use it: English goes through a lexicon G2P and Spanish through a rule-based G2P of our own (`providers/g2p_es.py`), so no GPL code is installed. Languages that would need espeak are not offered.

## Local speech-to-text

The command bar listens on the Mac. `providers/local_stt.py` runs **NVIDIA Parakeet TDT 0.6B v3** (int8
ONNX) through **sherpa-onnx**, greedy decoding, with no language hint: the model detects English or
Spanish itself. Around the recognizer:

- **Two gates against non-speech.** A cheap energy check stops silence and steady hiss or hum before the model runs; Silero VAD v5 stops clicks, coughs and noise bursts.
- **Only the speech is recognized.** The audio is cut to the stretch the VAD calls speech (0.3 s kept before, 0.5 s after). The bar's microphone is often open for seconds before anyone speaks, and a short word after a long silence is otherwise heard as nothing.
- **Levelling.** The audio is peak-normalised first, because the microphone runs without gain control.
- **Partials** about every 0.6 s while the user speaks, one decode in flight at a time; then one final.
- **Idle unload** after 10 minutes; the model loads while the user is still drawing breath.

It was chosen by measurement over Canary 180M Flash and Whisper base. **All speech in these measurements
was synthetic** (macOS `say` voices, 415 utterances: 210 English, 205 Spanish; natural voices only in the
figures). No human voice has been tested.

Exact match after normalising case and punctuation (word error rate for dictated sentences), speech at
−26 dBFS with 0.4 s of lead-in ("mic"), and the same with pink noise 20 dB below the speech:

| Model | Condition | EN all | EN one word | EN commands | EN dictation WER | ES all | ES one word | ES commands | ES dictation WER |
|---|---|---|---|---|---|---|---|---|---|
| Parakeet v3 | mic | 88.7% | 82.7% | 91% | 0.4% | 89.0% | 75.0% | 94% | 0.0% |
| | noise | 81.5% | 71.2% | 87% | 1.6% | 72.0% | 60.4% | 73% | 0.0% |
| Canary 180M (language given) | mic | 90.5% | 86.5% | 91% | 0.0% | 82.3% | 83.3% | 86% | 4.7% |
| | noise | 85.1% | 80.8% | 85% | 0.0% | 64.0% | 56.2% | 69% | 7.0% |
| Whisper base (language given) | mic | 79.2% | 86.5% | 76% | 2.0% | 62.8% | 77.1% | 58% | 5.5% |
| | noise | 79.8% | 92.3% | 75% | 2.8% | 51.8% | 64.6% | 46% | 3.9% |

Parakeet is the best overall in both languages, clearly ahead in Spanish and on multi-word commands, the
fastest, and the only one that needs no language hint (Whisper on automatic detection heard "sí" 0 times
in 12). It costs the largest download and the most memory, and on isolated one-word commands Whisper base
with the language given was better.

**One-word commands are the weak spot**, and where the pause before speaking matters most. Exact match
for one-word commands (52 English, 48 Spanish), by how much silence came before the word, without and
with cutting the audio to the speech:

| Silence before the word | EN, whole buffer | EN, cut to the speech | ES, whole buffer | ES, cut to the speech |
|---|---|---|---|---|
| 0.4 s | 32/52 | 40/52 | 22/48 | 26/48 |
| 3 s | 20/52 | 42/52 | 9/48 | 26/48 |
| 10 s | 14/52 | 34/52 | 1/48 | 25/48 |

Run to run these counts move by a few utterances (the test adds its own noise floor): a second pass
with the cut gave 42/52 and 26/48 at 0.4 s. The same cut helps longer input too: multi-word commands and
dictated sentences after 5 s of silence went from 100 to 105 of 116 in English and from 84 to 99 of 116 in
Spanish. Rejection of non-speech did not change: of 58 clips (silence, hiss, noise, tones, clicks, hum,
coughs, thumps, humming, whistling) the bare recognizer lets the same 5 through with or without the cut,
and with the engine's energy gate in front all 58 came back empty when that was measured, before the cut
was added.

What remains wrong is specific: "pause" is heard as "cause", "calls", "hose" or "holds", "sí" as "see" or
"c", "pausa" as "pauza" on some synthetic takes. The intent layer maps those back for spoken one-word
transcripts (`07` § Fast path). Two-word phrasings are reliable ("pause please", "pausa por favor").

| | Parakeet v3 | Canary 180M | Whisper base |
|---|---|---|---|
| Weights | 642 MB | 207 MB | 160 MB |
| Load | 1.0 s | 0.45 s | 0.27 s |
| Decode 1 s / 3 s / 6 s / 30 s of audio | 47 / 108 / 186 / 965 ms | 77 / 156 / 290 / 2673 ms | 121 / 227 / 362 / 1595 ms |
| Memory loaded / peak | 1.8 / 2.5 GB | 0.67 / 1.1 GB | 0.52 / 1.0 GB |

Through the engine at microphone pace (Apple M4, 2 threads): the final transcript arrives a median 69 ms
after the microphone stops for 1 s of speech, 141 ms for 3 s, 198 ms for 6 s; each partial appears
0.06–0.08 s after its 0.6 s boundary; listening costs 28–54% of one core; the engine grows by about
1.9 GB while the model is loaded and returns to about 0.43 GB after it unloads.

Not verified: human voices; utterances over 30 s; what sherpa-onnx does with a corrupt weight file. The
pinned Silero VAD file comes from a repository with no licence tag (upstream Silero VAD is MIT).

## Voices and languages

**Kokoro speaks English and Spanish.** The first letter of a Kokoro voice is its language: `a` American
English, `b` British English, `e` Spanish (`ef_dora`, `em_alex`, `em_santa`). Each language has a built-in
phonemizer (`providers/g2p.py`, `providers/g2p_es.py`), chosen from the voice. The Spanish one is
rule-based and our own (no espeak): letter-to-sound rules, stress, numbers, ordinals, abbreviations
("Sra.", "Ud."), times and common units, in Castilian pronunciation (θ, ʎ), which is what the reference
pipeline produces for these voices.

How well it matches the reference pipeline (misaki with espeak, used only as an oracle in a scratch
environment) and how it sounds to a recognizer:

| Check | Result |
|---|---|
| Agreement with the oracle, held-out words (3,000) | 99.33% |
| Agreement with the oracle, held-out sentences (300 Wikipedia + 300 from a reader) | 99.40% and 99.27% of words |
| Proper nouns, many foreign (800) | 97.88% |
| Round trip: Kokoro speaks 45 Spanish sentences (480 words), Parakeet transcribes | Word error rate 0.62% (`ef_dora`), 0.42% (`em_alex`), 0.42% (`em_santa`) |
| The same sentences through the English phonemizer (what a Spanish document got before) | Word error rate about 71% |
| Word timings | One per word, in order, all words covered |

The remaining disagreements with the oracle are deliberate or harmless: it reads "Vd." as "uve de" where
we say "usted", Roman numerals ("Fernando VII" as "siete" where we say "séptimo"), acronyms, and foreign
names. Limits: nobody has listened to it, so quality rests on those two measures; number agreement is
conservative ("1 casa" is read "uno casa" for nouns outside a short list); British voices use the American
lexicon; estimated word timings for cloud voices are tuned on English syllables.

**Which voice reads what.** A document's language is detected when it is imported (`05` § Extract).
`VoiceService.for_language` then decides:

1. A voice chosen for that document always wins.
2. The default voice, when it can speak the document's language (a cloud voice with no declared language speaks any).
3. Otherwise the voice the user picked for that language (`defaults.voices`, filled in when they choose a voice while reading or speaking that language).
4. Otherwise the first installed voice that speaks it, local voices first.
5. Otherwise the default voice: reading with the wrong accent beats not reading.

Replies are spoken by the assistant voice under the same rule, for the interface language. The voice the
document will actually use is the one warmed up when it is opened.

## Keys

**Provider keys are build configuration, not user data.** The app never asks for a key, shows one,
stores one or changes one. A build either includes a provider or it doesn't, and features that depend on
a missing provider are absent.

| Where a key comes from | When | Priority |
|---|---|---|
| The process environment (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`) | Any | Highest |
| `<bundled runtime>/provider.env` | Packaged app. Written by `app/scripts/bundle-engine.sh` at build time from the same three variables in the build environment, or from the repo-root `.env` when they are unset. Only those three values are copied, never the rest of `.env` | Middle |
| Repo-root `.env` | Development (`uv run`, `pnpm dev`). Gitignored | Lowest |

- `OPEN_SPEECH_BUNDLE_KEYS=0 pnpm dist:mac` builds without keys whatever `.env` holds. With no keys at all, Open Speech reads with Kokoro, listens with the local recognizer and uses Classic rules.
- Keys are held as `SecretStr`, never logged and never returned by any API. There is no keychain entry, no `/keys` route, no `keys` CLI command and no key field anywhere in the interface.
- What a client can learn is `GET /capabilities` (`04`): whether the build has the assistant, AI Enhanced cleanup, image descriptions, which providers have cloud voices, and whether and how the bar can listen. A feature the build lacks answers `unavailable` (HTTP 424) with the `feature` it was.
- If a provider rejects the build's key (401/403), that provider is switched off for the rest of the run and its features become unavailable; nothing prompts for another key.

> **A key shipped inside an app can be read by anyone who has that app.** `provider.env` is a plain file
> in the bundle. That is acceptable only for builds that stay on our own machines while we test. A build
> that goes to anyone else must be made without keys. The planned replacement is a hosted deployment that
> holds the keys and bills users for what they use; it is not built, and nothing in the engine depends on
> it yet.

## Usage ledger

Pattern adapted from `aden/aden/billing/ledger.py` and `cost.py`, simplified to local estimates.

```sql
CREATE TABLE ledger (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,                 -- ISO 8601 UTC
  provider TEXT NOT NULL,           -- anthropic | openai | gemini | kokoro | local | qwen3 | openai_compatible
  model TEXT NOT NULL,
  op TEXT NOT NULL,                 -- tts | stt | annotate | transform | vision | chat | compile | preview
  units_json TEXT NOT NULL,         -- {chars, in_tok, out_tok, cache_read_tok, cache_write_tok, audio_sec, frames, images}
  usd_estimate TEXT NOT NULL,       -- decimal string; "0" for cache hits and local runs
  pricing_version TEXT NOT NULL,
  document_id TEXT,
  turn_id TEXT,
  cache_hit INTEGER NOT NULL DEFAULT 0,
  local INTEGER NOT NULL DEFAULT 0
);
```

- **Claude:** units come from `response.usage` (exact input, output, cache-read and cache-write tokens).
- **OpenAI TTS:** estimated from input chars → tokens and output audio seconds, priced per `pricing.toml`.
- **Gemini TTS:** `usage_metadata` tokens when present; otherwise estimated.
- **STT:** audio seconds.
- **Cache hits** write a `$0` row with `cache_hit = 1` and the *avoided* cost in `units_json.avoided_usd`, which powers "cache saved you $1.20".
- **Local runs** (Kokoro, the local recognizer) write `$0` rows with `local = 1`.

**Stored, not paid twice.** What providers return is kept in the local asset store (`03` § The asset store): synthesized speech under the hash of its text and voice, what a recording said under the hash of its audio, and fetched web pages under the hash of their address (reused while fresh, revalidated after 15 minutes, served from the store when offline). A page the user opened and the same page read by the assistant are one download.

## Pricing catalog (`pricing.toml`)

```toml
version = "2026-11-21"
currency = "USD"

[anthropic."claude-sonnet-5-5"]
input_per_mtok = "2.00"
output_per_mtok = "10.00"
cache_read_per_mtok = "0.10"

[anthropic."claude-opus-5-5"]
input_per_mtok = "4.00"
output_per_mtok = "20.00"
cache_read_per_mtok = "0.20"

[anthropic."claude-haiku-5-5"]
input_per_mtok = "0.10"
output_per_mtok = "0.50"

[openai."gpt-4o-mini-tts"]
# (verify) text input + audio output pricing; approx $0.015 per audio minute
text_input_per_mtok = "0.60"
audio_output_per_mtok = "12.00"

[gemini."gemini-3.8-flash-tts"]
# (verify)

[openai."whisper-1"]
per_audio_minute = "0.006"   # (verify)
```

Prices are dated and versioned, overridable in app data, and updated by PR. Every number the UI or voice
reports is labelled **estimated**.

## Budgets and estimates

- **Monthly budget** (`settings.toml` `[budget] monthly_usd`, `warn_at = 0.8`, `hard_stop = true`).
  - At 80%: a spoken notice and a bar row, once per threshold crossing.
  - At 100%: paid calls are refused with `budget_exceeded`. Reading continues on Kokoro and Classic rules. The user can override ("raise my budget to $20", confirmed).
- **Pre-flight estimate** when a single operation is estimated above `estimate_threshold_usd` (default `0.50`). Applies to reading a document with a paid voice, large documents in AI Enhanced mode, whole-document exports, transforms and keep-offline pins. Reading with a local voice and Classic cleanup is free and is never estimated.
  - **One place asks.** Every way of starting a read (the bar, the reader's play button, the tray, a hotkey, the queue, the assistant, the API) goes through the playback service, which refuses a read over the threshold the first time with `estimate_required` and the amount. The go-ahead is any of: a confirmation card in the bar ("yes" by voice or Enter), `confirm_estimate: true` on the request, or simply asking to play again within two minutes (the message says so). A queue is agreed to as a whole, so reading never stops halfway to ask. A document opened with `play: true` comes back with its `estimate` and `playing: false`, and clients are told the amount in a `usage.alert`; one press of play then starts it. Once agreed, a document is not asked about again in that session.
  - **Estimator:** extracted chars → TTS chars after Classic filters, minus cached utterances; annotate tokens ≈ block tokens × 1.1 + output allowance; vision per image/frame. Target ±25% for text documents (FR-COST-3).
- **P2:** "prepare whole document" through the Claude Message Batches API (50% cheaper, minutes to hours) for books queued in advance.

### Reference costs (estimates, Sonnet 5.5 + OpenAI TTS)

| Item | Approx. |
|---|---|
| 300-page book, AI Enhanced annotate | ~$0.45 (Claude) |
| 300-page book, OpenAI TTS (~11 h audio) | ~$10 |
| Same book on Kokoro | $0 |
| One image described | ~$0.005 |
| One assistant turn | ~$0.002–0.01 |

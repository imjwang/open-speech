# 08 — Providers, models, keys and usage

Status: Draft v0.1 · 2026-11-22 · Model IDs verified against live APIs on 2026-11-21 unless marked *(verify)*; Haiku 5.5 gate run on 2026-11-22.

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

[tts.google."chirp3-hd"]               # Google Cloud Text-to-Speech, plain HTTPS (no SDK)
voice_template = "{locale}-Chirp3-HD-{star}"
locales = ["en-US","en-GB","es-US","es-ES"]   # the first locale of a language is the one "auto" uses
stars = ["Achernar","Achird","Algenib", …, "Zephyr","Zubenelgenubi"]   # the 30 Chirp 3 HD voices
default_star = "Zephyr"                 # "auto"
format = "l16"                          # LINEAR16 WAV at 24 kHz; no speakingRate
max_input_chars = 5000                  # the API's limit is 5,000 bytes per request; longer text is split
timestamps = false                      # word timing estimated from the audio (05 § 8)

[tts.kokoro."kokoro-82m-v1.0"]
local = true
weights = { url = "hf://onnx-community/Kokoro-82M-v1.0-ONNX-timestamped", quant = "q8", size_mb = 92, sha256 = "<pin>" }
ram_gb = 1.3
license = "Apache-2.0"
languages = ["en-US","en-GB","es"]   # one built-in phonemizer per language (providers/g2p*.py)
timestamps = true
# 30 voices; the first letter is the language: a = American English, b = British English, e = Spanish
# (ef_dora, em_alex, em_santa)

[tts.qwen3."qwen3-tts"]          # one voice list; the settings pick the weights (§ Local Qwen3-TTS voices)
local = true
requires = "apple-silicon"
runtimes = ["mlx-audio"]
voices = ["Ryan", "Aiden", "Serena", "Vivian", "Uncle_Fu", "Dylan", "Eric", "Ono_Anna", "Sohee"]
languages = ["en","es","zh","ja","ko","de","fr","ru","pt","it"]
default_size = "0.6b"            # measured (below); "auto" on a Mac with ≤ 8 GB is always 0.6B
default_precision = "8bit"
stream_interval_s = 0.5
params_version = 1               # bump when the sampling below changes: cached Qwen audio is made again
temperature = 0.9                # the model's generation_config.json defaults
top_k = 50
top_p = 1.0
repetition_penalty = 1.05

# 25 downloadable variants, generated by engine/tools/pin_hf_weights.py between "# BEGIN/END GENERATED qwen3":
[local_weights.qwen3."qwen3-tts-0.6b-customvoice-8bit"]
family = "customvoice"           # base (cloning) | customvoice (the presets) | voicedesign (1.7B only)
size = "0.6b"
precision = "8bit"               # bf16 | 8bit | 6bit | 5bit | 4bit
ram_gb = 3.1                     # measured while generating (weights + the codec's working set)
weights = { url = "hf://mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit", revision = "<pinned commit>", files = [ <path + sha256 for each of 12 files> ] }

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
default = "claude-haiku-5-5"
options = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-2"]
thinking_reserve_tokens = 4096        # added to max_tokens whenever a call may think

# Per-model request traits (LlmTraits); a model without an entry: thinking parameter left out, no fallback.
[llm.anthropic.models."claude-haiku-5-5"]
thinking_off = "disabled"             # `between_tools` is a 400 on Haiku 5.5
server_fallback = false               # Haiku 5.5 has no server-side refusal fallback
annotate_thinking = "adaptive"        # the AI Enhanced accuracy gate's choice (below)

[llm.anthropic.models."claude-sonnet-5-5"]
thinking_off = "between_tools"        # `disabled` is a 400 on Sonnet 5.5
server_fallback = true
annotate_thinking = "off"

[llm.anthropic.models."claude-opus-5-5"]
thinking_off = "omit"                 # thinking can't be turned off: low effort is the lever
server_fallback = true
annotate_thinking = "adaptive"
```

**Haiku 5.5 everywhere.** The assistant, AI Enhanced annotate and transforms, rule compiling and image
reading all use `llm.model`, Haiku 5.5 by default. Request shapes per model come from the traits above: the
tool-less calls (compile, vision; annotate when its trait says "off") turn thinking off in the model's own way,
the agent runs adaptive thinking with `max_tokens` 4096 and sends `fallbacks` only where the model has one,
and no request ever carries `temperature`, `top_p` or `top_k`. Responses are read by block type (a Haiku 5.5
response may start with a thinking block) and `stop_reason` is checked for `max_tokens` and `refusal`.

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
| Claude annotate, **quote-anchored**, Sonnet 5.5 thinking off | **Correct** (2/2), 2.1–2.5 s, 138 output tokens |
| Claude annotate, quote-anchored, Haiku 5.5 | Superseded by the accuracy gate below: with thinking off Haiku drops kept text; with adaptive thinking at low effort it matches Sonnet → the default |

### AI Enhanced accuracy gate (2026-11-22)

`tests/live/test_annotate_eval.py` (opt-in, `OPEN_SPEECH_LIVE=1`) plans seven documents through the real
pipeline in AI Enhanced mode with the default presets: the pipeline fixtures (academic paper, report, Word
file, news page, Markdown notes, a Wikipedia lead) and a page of hard cases. Hand labels
(`tests/live/annotate_expectations.json`): 47 **must-keep** spans (content next to removed spans, a cited
author who is the subject, years and parentheses that carry meaning) and 9 **must-drop** spans that only
AI Enhanced can remove (author-year forms, "Page 4 of 18" in the body, a running head fused into a paragraph,
the path of a bare address, odd note markers). A hermetic test checks that every label holds in the Classic
output. Three runs per variant, interleaved; must-keep is scored on what would be spoken, after the guard.

| Variant | Must-keep violations | Must-drop recall | Guard refusals | Unmatched quotes | Model refusals | p50 / p90 latency | Output tokens / call | Cost / run (7 docs) | Pass |
|---|---|---|---|---|---|---|---|---|---|
| Sonnet 5.5, thinking off (`between_tools`), low (old default) | 0 | 100 % | 0 | 0/27 | 0 | 1.80 / 3.51 s | 87 | $0.0224 | baseline |
| Haiku 5.5, thinking `disabled`, low | **6** | 96 % | 5 | 0/51 | 0 | 1.66 / 3.09 s | 255 | $0.0018 | no |
| **Haiku 5.5, adaptive thinking, low** | 0 | 100 % | 0 | 0/27 | 0 | 1.47 / 7.58 s | 443 | **$0.0024** | **yes → default** |
| Haiku 5.5, adaptive thinking, medium | 0 | 100 % | 0 | 0/30 | 0 | 3.64 / 9.44 s | 668 | $0.0032 | yes |

Pass bar: no must-keep violation in any run, recall at least Sonnet's, at most 5 % unmatched quotes. With
thinking off, Haiku removed "Smith et al. showed", "Lee and Kim disagree", "as noted by Brandt" and "(released
in 2021)"; the guard refused some of these and the rest reached the listener. Every call read the cached
prefix (about 1.7K tokens a call, Haiku's minimum cacheable prompt is 512). Haiku adaptive-low is about a tenth
of Sonnet's cost with a faster median but a longer tail (one call in ten took 4–8 s while it thought), which
matters for the first batch, planned before AI Enhanced playback starts. The shipped configuration's recorded
responses are replayed offline by `tests/unit/pipeline/test_annotate_replay.py`; a prompt or request change
makes that test ask for the live gate to be run again.

## Google Chirp 3 HD (the default voice)

Chirp 3 HD is Google Cloud Text-to-Speech's newest voice family: the same 30 voices as Gemini TTS (Zephyr, Kore,
Puck…) without Gemini's per-token price, and **the first million characters each month are free**. It is the
default for reading and for every spoken reply through the `auto` voice. `providers/google_tts.py` calls the
REST API with httpx (no Google SDK):

- **Synthesis:** `POST https://texttospeech.googleapis.com/v1/text:synthesize`, the key in the `X-Goog-Api-Key`
  header (never in the URL), `{"input": {"text"}, "voice": {"languageCode", "name"}, "audioConfig":
  {"audioEncoding": "LINEAR16", "sampleRateHertz": 24000}}`. The base64 `audioContent` is a WAV file, decoded
  to PCM and handed over as one chunk. No `speakingRate`: speed stays in the transport, so it is not part of
  the cache key and changing speed never re-synthesizes. The API takes at most 5,000 bytes of UTF-8 per
  request; utterances are much shorter, and anything longer is split at sentence ends (then clauses, then
  words) and the audio joined. Word timing is estimated from the audio (`05` § 8).
- **Voices and languages:** the catalog writes the voices as locales × stars (`voice_template`); ids look like
  `google:chirp3-hd:en-US-Chirp3-HD-Zephyr`. Locales: American and British English, US and European Spanish
  (120 voices). Names read "Zephyr (Chirp 3 HD, American English)". Voices that are the same speaker in another
  locale share a `persona` (`chirp3-hd:Zephyr`), so a Spanish document read with an English Chirp voice keeps
  that speaker (`es-US`) instead of switching to a Kokoro voice.
- **Is it allowed? (startup check).** A key can work for Gemini and still be refused by Cloud Text-to-Speech.
  At startup the engine lists the voices of each catalog locale (`GET /v1/voices?languageCode=…`, free), again
  after 1 and 10 minutes, then hourly, and right after a synthesis is refused. Only catalog voices the API also
  lists are offered. While the API refuses the key, every Chirp voice is listed as unavailable with the reason
  in plain words, `capabilities.cloud_voices` leaves `google` out, and `auto` quietly means the local voice;
  when the check sees it working, `capabilities.changed` goes out and `auto` becomes Chirp from the next read.
- **Errors:** 403 with `API_KEY_SERVICE_BLOCKED` or `SERVICE_DISABLED` (and `BILLING_DISABLED`, or a key
  restricted to other apps) → `unavailable` with the fix ("Google's voices need Cloud Text-to-Speech enabled for
  this build's key"); the key is **not** marked invalid, so the Gemini voices and Gemini speech input keep
  working. `API_KEY_INVALID` or a 401 → only `google` is marked invalid. 429 and 5xx are retried with backoff,
  then the local voice reads on.

**`auto`** (`defaults.voice` and `defaults.assistant_voice`, the defaults): Chirp **Zephyr** in the language's
locale (`en-US`, `es-US`) while Chirp can be used and the month's free characters last — or past them when the
user chose to keep paying — otherwise the local voice (`defaults.local_voice`, Kokoro Heart, adjusted to the
language: `ef_dora` for Spanish). Structural reasons (no Google key in this build, Cloud Text-to-Speech refused)
switch silently; runtime reasons (free characters used up, the budget's hard stop) say why once. Resolution is
`VoiceService.resolve()`, used wherever a voice is about to speak: reading, replies, previews, estimates, the
voices card and the settings card ("Automatic (Zephyr)"). Profiles on the old default Kokoro Heart — and the
owner's Gemini Zephyr profile, same voice name — were moved to `auto` once (settings migration
`2026-11-voice-auto`).

**The free allowance (estimated).** `pricing.toml`: `per_mchars = "30.00"`, `free_chars_per_month = 1_000_000`,
billing month in Pacific time (*verify*; both from Google's price list, the allowance belongs to the Google
billing account). The ledger prices only the characters past the allowance (`units_json.free_chars` says how
many were free); cache hits and local runs never count. Because other use of the same Google account is
invisible here, every figure is an estimate and `FreeTierGuard` (`usage/free_tier.py`) switches at the
allowance less a 2 % reserve:

- `playback.beyond_free_tier = "local"` (default): past 980,000 characters, reading and replies use the local
  voice with the notice "This month's free Google voice is used up; reading with the local voice."
  (`notice.free_tier`), spoken once per session through the transport's `speak_notice` hook.
- "Keep using the Google voice" / "sigue con la voz de Google" (fast path) → a card with the rate (about 3 cents
  per thousand characters) → `beyond_free_tier = "pay"` for the current billing month only
  (`beyond_free_tier_month`); the next month is `local` again by itself. The budget still applies.
- Alerts (`usage.alert`, level warn) at 80 % and when the allowance is used up, once per month.
- `BudgetState.free_tiers` carries `{provider: "google", label: "Google voice", unit: "chars", used, limit,
  resets_on}` for the spend chip. Estimates price against what is left ("within the free allowance").

## Local models

**Tiering**

| Tier | Model | Runtime | Download / RAM | Speed | Word timing | Status |
|---|---|---|---|---|---|---|
| Default local voice | **Kokoro-82M v1.0** (Apache-2.0); 27 English and 3 Spanish voices | onnxruntime (CPU) | ~92 MB q8 + 0.5 MB per voice / ≤ 1.3 GB | ~10x real time on CPU | Exact (timestamped export) | **P0**, included in packaged builds |
| Default speech input | **Parakeet TDT 0.6B v3**, int8 (CC-BY-4.0), with Silero VAD v5 (MIT) | sherpa-onnx (CPU, 2 threads) | 642 MB / about 1.9 GB while loaded | ~25x real time | — | **P0**, included in packaged builds |
| Optional local voices | **Qwen3-TTS** 0.6B / 1.7B (Apache-2.0): nine preset speakers, cloned voices (workstream 8); 25 downloadable size × family × precision variants | mlx-audio 0.6.7 on MLX (Apple Silicon), in a worker process | 1.6–4.3 GB / 2.7–5.6 GB (default 0.6B 8-bit: 1.8 GB / 3.1 GB) | 0.6B 8-bit about 2x real time sustained on an M4 Air; first audio ~0.25 s (streamed) | Estimated from the audio (pause-anchored) | **P1**, runtime in packaged builds, weights on demand |
| BYO server | Kokoro-FastAPI, mlx-audio server, vLLM-Omni, Speaches, qwentts.cpp | `openai_compatible` adapter | — | — | Via alignment (Kokoro-FastAPI `/dev/captioned_speech` optional) | P1 |
| Local alignment | Qwen3-ForcedAligner-0.6B (Apache-2.0) or whisper.cpp DTW | Python / mlx-audio / whisper.cpp | ~0.6–1 GB | background | — | v1.1 |

Voice cloning is in scope: Qwen3-TTS Base weights read a cloned voice from a short reference recording, VoiceDesign
weights make one from a description (§ Voice cloning).

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

### Local Qwen3-TTS voices

**Voices.** One list whatever weights read it: the nine preset speakers of the CustomVoice weights, ids
`qwen3:qwen3-tts:<Speaker>` (Ryan and Aiden are English natives, Serena, Vivian, Uncle Fu, Dylan and Eric Chinese, Ono
Anna Japanese, Sohee Korean; each reads all ten languages, its own most naturally), then cloned voices,
`qwen3:clone:<slug>`, read by the Base weights. Offered only on Apple Silicon (MLX); elsewhere the provider lists
nothing. Until weights are downloaded the voices are listed unavailable with the download size.

**Sizes and precisions without 25 voice lists.** Weights come as `[local_weights.qwen3."qwen3-tts-<size>-<family>-<precision>"]`
variants (0.6B and 1.7B × Base, CustomVoice, VoiceDesign (1.7B only) × bf16, 8-, 6-, 5-, 4-bit), generated with
`engine/tools/pin_hf_weights.py qwen3 --write` from the `mlx-community` repositories: the commit, every file's sha256
(LFS hashes; small files downloaded and hashed), speakers and languages from `config.json`, `tts_model_type` checked
against the name. The settings `qwen3.size` (`auto`, `0.6b`, `1.7b`) and `qwen3.precision` (`auto`, `bf16`, `8bit`,
`6bit`, `5bit`, `4bit`) pick one per family (`providers/qwen3.py` `resolve_variant`): `auto` is the catalog's measured
default (`default_size`, `default_precision`), and always 0.6B on a Mac with 8 GB or less. When the chosen variant isn't
downloaded but another of the same family is, that one reads (same size first) and a note says so. Changing either
setting never changes a voice id. The 650 MB codec (`speech_tokenizer/model.safetensors`) is the same file in all 25
repositories: installing a second variant hard-links it instead of downloading it again.

**Runtime.** Generation runs in a worker process (03 § The Qwen3-TTS worker process), started when a document opens
(`prewarm`, which also compiles the streaming decoder: the first streamed sentence otherwise waits 2–4.5 s more) and
stopped after 10 idle minutes. One generation at a time. The worker always streams: on an M4 the streaming decoder's
output equals the whole-utterance decode (largest difference 3e-6, so chunk boundaries are seamless), it halves peak
memory (0.6B 8-bit: 3.0 GB instead of 6.4 GB for a 30 s paragraph; 1.7B 8-bit: 4.1 instead of 8.5 GB), and it gives a
cancel point every 0.5 s of audio. Each utterance is seeded from its text, voice and variant (`mx.random.seed`; the
same seed gave bit-identical audio), so a regenerated cache entry, or the retry after a worker crash, sounds the same.
Cache identity: `<model>/<variant>@<revision[:12]>/p<params_version>/<language>`, plus `ref-<sha256[:12]>` of the
reference for a cloned voice. The language comes from the request, else the text (`document/language.py`), else
`auto`: told "spanish", Spanish round-trips with 1.0 % word errors, in `auto` 2.0 % (0.6B). A crash restarts the
worker once; a second crash in 10 minutes, or any model error, is a `provider_error` and the synthesizer reads on with
Kokoro, saying so (`falls_back`). A seek or skip cancels generations nobody will hear (`cancel_on_seek`).

**Start fast: growing utterances.** A whole 200-character sentence takes about 7 s to generate on 0.6B, so the
transport plays utterances while they are generated (`Synthesizer.synth(on_chunk=…)`, `render.GrowingPcm`): an utterance
starts once the audio already there, plus what will be generated while earlier audio plays, covers the projected
shortfall `max(0.8 s, remaining × (1 − rtf / (speed × 1.1)))`, so it plays through without a gap; if generation falls
behind anyway the renderer fades out over 5 ms and back in when audio returns. The sentence is tinted until the
utterance is complete, then the word highlight picks up at the playhead (timings estimated from the audio). The
measured speed (`rtf`, seconds of audio per second of work, an EWMA per variant kept in the database) feeds that
formula; when it is below `speed × 1.1` the engine says once: "This voice can't keep up at 2 times speed on this Mac.
A smaller size or Kokoro keeps up." (English and Spanish).

**Measured on this Mac** (Apple M4 MacBook Air, fanless, 16 GB; 2026-11-23; `tools/audiolab qwen`, through the worker
exactly as the app runs it, voice Ryan, a seed per sentence; other work was running on the machine at times, so each
run records the load average). Speed is seconds of audio per second of generation over 10 minutes of reading sentence
after sentence; word errors are the Parakeet round trip on 20 English and 10 Spanish sentences; steadiness compares
the voice across those sentences (log-mel similarity, spread of median pitch).

| Variant | Download | Peak memory | Speed, 10 min (last 3 min) | First audio p50 / p90 | Word errors en / es | Steadiness en |
|---|---|---|---|---|---|---|
| **0.6B 8-bit** (default) | 1.8 GB | 2.7 GB (3.0 GB, 30 s paragraph) | 2.00x (1.86x; 2.23x in the quiet first minutes, load 2.5; 1.7–2.0x at load 5–7) | 0.25 / 0.33 s | 1.3 % / 1.0 % | 0.975, ±14 % |
| 0.6B bf16 | 2.3 GB | 3.2 GB | 1.47x (1.47x, quiet) | 0.34 / 0.34 s | 0.4 % / 1.0 % | 0.969, ±13 % |
| 1.7B 4-bit | 2.2 GB | 3.0 GB | 2.31x (2.25x) | 0.24 / 0.25 s | 0.9 % / 5.0 % | 0.939, ±18 % |
| 1.7B 6-bit | 2.5 GB | 3.4 GB | 1.83x (1.82x) | 0.30 / 0.31 s | 0.0 % / 4.0 % | 0.959, ±11 % |
| 1.7B 8-bit | 2.9 GB | 3.8 GB (4.1 GB, 30 s paragraph) | 1.51x (1.64x; load 2–4) | 0.33 / 0.46 s | 0.0 % / 3.0 % | 0.969, ±11 % |
| Kokoro q8 (reference) | bundled | ~1.3 GB | ~10x | ~0.65 s (whole sentence) | 1.8 % / 0.5 % | — |

Through the whole engine path (synthesizer, transport, growing utterances, worker; sound off, 0.6B 8-bit): first
audio for a new sentence 0.63 s p50 (0.58–0.69 s over six sentences of 48–91 characters; the 0.8 s start buffer is most
of it), 3.7 s when the worker isn't running yet (started when the document opens 0.2 s before play: load 2.3 s, decoder
warm-up 0.9 s); ten sentences read at 1x, 1.5x and 2x with no silence longer than 0.4 s and no starvation after the
start. The packaged runtime grows by about 380 MB with `local-qwen` (`bundle-engine.sh` install step, measured).

No thermal throttling showed within 10 minutes (the quiet runs are flat minute by minute); the dips track the load
average of other work. Spanish told "auto" instead of "spanish" went from 1.0 % to 20 % word errors on 0.6B bf16, so
the language is always passed when it is known. Without streaming, a 200-character sentence would start only after
7 s (0.6B) or 6.7 s (1.7B); streamed, audio starts after the first 0.5 s piece.

**Default: 0.6B 8-bit.** Every variant passes the rule's 1.3x sustained speed on this Mac, so the choice is by ear, and
nobody has listened yet (the blind set is in `~/Downloads/open-speech-listening/qwen/`). Until then 0.6B 8-bit: the
steadiest voice across sentences, the best Spanish of the five, fast enough to keep up to about 1.8x listening speed
(1.7B 8-bit stops keeping up above about 1.35x), the smallest download, and the same on 8 GB Macs. If the owner prefers
the 1.7B sound, 1.7B 6-bit is the one to switch to (`default_size = "1.7b"`, `default_precision = "6bit"`): 1.83x, no
English word errors; 4-bit is faster still but measurably less steady.

**Clones plug in here.** `providers/qwen3.py` defines `CloneVoice {slug, name, language, ref_audio (24 kHz mono WAV),
ref_text, ref_sha256, available, unavailable_reason, source}` and the `CloneStore` protocol (`clones()`, `clone(slug)`);
`voices/store.py` `VoiceStore` is the one the engine installs (`ProviderRegistry.set_clone_store`, kept across catalog
reloads). Cloned voices are listed, validated (`has_voice`), cached by reference hash and synthesized with the Base
variant the settings choose (`ref_audio` + `ref_text` to mlx-audio's in-context cloning, which forces a repetition
penalty of at least 1.5); the worker warms up on the clone about to speak, which also encodes its reference.
`Qwen3Tts.design(text, description, seed)` renders with VoiceDesign (1.7B).

**Model manager.** Rows "Qwen voices", "Qwen voice cloning", "Qwen voice design", each with its size and precision as
chips (0.6B · 8-bit), a mark on the variant the settings choose, download size, memory and fit; only installed, downloading and chosen variants are listed, everything with
`GET /models/local?all=1`. "Install the small Qwen voice" / "instala la voz Qwen pequeña" (and "big"/"grande")
puts the variant on a confirmation card with its size; "yes" downloads it with progress.

### Voice cloning

Local only, free, and never sent anywhere: the recording, its transcript and the voice stay in `voices/<slug>/` in app
data (`source.<ext>`, `reference.wav`, `voice.json`), outside recording retention and the cache budget.

**Consent.** A clone is made only after the person confirms "This is my voice, or I have the speaker's permission to
clone it. Clones stay on this Mac." (en/es): in the bar, the passage card's record button or saying "ready" (the line is
on the card), or a card before cloning an attached file; through the API, `consent=true` (422 without it). The record
keeps `{text_version, accepted_at}`.

**The reference** (`voices/reference.py`, processing `ref-v2`). Decode (libsndfile; M4A/AAC/CAF through macOS
`afconvert`), mono, soxr to 24 kHz. Speech by Silero VAD (from the speech model's weights; an energy detector without
it). Measured, with a warning and a rejection level each: sample rate (< 22.05 kHz warns, < 16 kHz is refused),
clipping (flat tops at the ceiling over 0.1 % of the window warn, refused when the ceiling is full scale), signal-to-noise
(< 25 dB warns, < 18 dB refused), speech (< 5 s refused). The window: whole phrases of 6-10 s (15 s at most), cut in
pauses, scored on continuity, signal-to-noise, evenness of level, no clipping and clean edges. Then a 50 Hz high-pass,
voiced RMS to -20 dBFS, a look-ahead limiter at -1 dBFS, 20 ms fades, and at least 0.3 s of silence before the speech and
0.6 s after it. That last step matters: on a reference cut right after its last word, 3 of 12 test sentences lost their
first words ("Please remember to water" → "Pleater"); with the silence, none of 24 did (two voices, Parakeet round trip).

**What it says.** The on-device recognizer (Parakeet) transcribes the window. For a passage the user read, `ref_text` is
the passage's own words for that window (the best-matching span, with its punctuation) when the recognizer agrees within
a word error rate of 0.15; otherwise the recording is refused ("That recording doesn't match the passage…") and the bar
offers the next passage. For a file it is the transcript (correct it with `PATCH`; typed text is needed only without
the recognizer). `ref_sha256` = sha256(reference WAV, NFC text, processing version): part of the cache identity, so a
new reference or transcript makes new audio and nothing else does.

**The check.** The new voice says a test sentence (not from any passage); the recognizer grades it by word error rate:
good ≤ 10 %, fair ≤ 25 %, poor above. The user hears that sentence, then "Keep it?" ("It's a little rough. Keep it
anyway?" when fair). "Yes" keeps it and makes it the reading voice; "no" deletes it. Without the cloning weights the size
is said first ("To clone a voice I need a 1.9 gigabyte model first…"); "yes" downloads it while the user records, and the
check waits for the download. Measured on an M4 Air (0.6B Base 8-bit): making a voice 7.1 s from a cold start (worker
start and load included), 4.1 s warm; first audio of a clone sentence 2.7 s cold, 0.45-0.53 s warm (about 2x real
time); check grades good (0 % word errors) for two `say` voices and a designed one.

**Designed voices.** "Make a voice that sounds like …" renders a neutral passage three times with VoiceDesign 1.7B
(different seeds; a candidate that garbles the passage, word error rate over 25 %, is rendered again), plays them with
their numbers, and keeps the one named ("the second one") as a clone of itself (`source: design`, the description kept as
`design_prompt`), so it sounds the same every time afterwards. Measured: three candidates in about 21 s; keeping one
6.9 s (switching weights included). Candidates outlive an engine restart for a day.

**Managing them.** Deleting a voice removes its folder and every cached sentence it said, and settings that used it go
back to `auto` (by voice: "delete the voice Dad", a card first). Renaming keeps the id and the hash. The CLI does all of
it (`open-speech voices clone|design|clones|rm-clone|preview`, `04` § CLI).

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
lexicon; estimated word timings for cloud voices weigh English-style syllables and letters (measured on both
languages, `05` § 8).

**Which voice reads what.** A document's language is detected when it is imported (`05` § Extract).
`VoiceService.for_language` then decides:

1. A voice chosen for that document always wins.
2. The default voice, when it can speak the document's language (a cloud voice with no declared language speaks any). `auto` speaks both: Chirp Zephyr in that language's locale, or the local voice for that language — except that a voice the user picked for a language other than the interface's (`defaults.voices`) is used there.
3. Otherwise the voice the user picked for that language (`defaults.voices`, filled in when they choose a voice while reading or speaking that language).
4. Otherwise the same speaker in a locale that speaks it (a Chirp voice's `persona`: Kore in `en-US` → Kore in `es-US`).
5. Otherwise the first installed voice that speaks it, local voices first.
6. Otherwise the default voice: reading with the wrong accent beats not reading.

Replies are spoken by the assistant voice under the same rule, for the interface language. The voice the
document will actually use is the one warmed up when it is opened.

## Keys

**Provider keys are build configuration, not user data.** The app never asks for a key, shows one,
stores one or changes one. A build either includes a provider or it doesn't, and features that depend on
a missing provider are absent.

| Where a key comes from | When | Priority |
|---|---|---|
| The process environment (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_TTS_API_KEY`) | Any | Highest |
| `<bundled runtime>/provider.env` | Packaged app. Written by `app/scripts/bundle-engine.sh` at build time from the same variables in the build environment, or from the repo-root `.env` when they are unset. Only those values are copied, never the rest of `.env` | Middle |
| Repo-root `.env` | Development (`uv run`, `bun run dev`). Gitignored | Lowest |

- `OPEN_SPEECH_BUNDLE_KEYS=0 bun run dist:mac` builds without keys whatever `.env` holds. With no keys at all, Open Speech reads with Kokoro, listens with the local recognizer and uses Classic rules.
- Keys are held as `SecretStr`, never logged and never returned by any API. There is no keychain entry, no `/keys` route, no `keys` CLI command and no key field anywhere in the interface.
- What a client can learn is `GET /capabilities` (`04`): whether the build has the assistant, AI Enhanced cleanup, image descriptions, which providers have cloud voices, and whether and how the bar can listen. A feature the build lacks answers `unavailable` (HTTP 424) with the `feature` it was.
- If a provider rejects the build's key (401/403), that provider is switched off for the rest of the run and its features become unavailable; nothing prompts for another key.
- **Google's voices (`google`) are their own key identity.** Their key is `GOOGLE_TTS_API_KEY`, else `GEMINI_API_KEY` (both are Google Cloud API keys; the Gemini key works once Cloud Text-to-Speech is enabled in its project and allowed for the key). A Cloud Text-to-Speech rejection marks only `google` invalid, and a "this API isn't allowed for the key" 403 marks nothing invalid at all (see Google Chirp 3 HD above).

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
version = "2026-11-22"
currency = "USD"

[anthropic."claude-haiku-5-5"]
# Prompts up to 100K tokens; cache reads 0.1x input, 5-minute cache writes 1.25x.
input_per_mtok = "0.10"
output_per_mtok = "0.50"
cache_read_per_mtok = "0.01"
cache_write_per_mtok = "0.125"

[anthropic."claude-haiku-5-5".long_context]
# A call whose prompt (input + cache-read + cache-write tokens) is over 100K tokens is billed entirely here.
above_tokens = 100_000
input_per_mtok = "0.50"
output_per_mtok = "2.50"
cache_read_per_mtok = "0.05"
cache_write_per_mtok = "0.625"

[anthropic."claude-sonnet-5-5"]
input_per_mtok = "2.00"
output_per_mtok = "10.00"
cache_read_per_mtok = "0.10"
cache_write_per_mtok = "2.50"

[anthropic."claude-opus-5-5"]
input_per_mtok = "4.00"
output_per_mtok = "20.00"
cache_read_per_mtok = "0.20"
cache_write_per_mtok = "5.00"

[openai."gpt-4o-mini-tts"]
# (verify) text input + audio output pricing; approx $0.015 per audio minute
text_input_per_mtok = "0.60"
audio_output_per_mtok = "12.00"

[gemini."gemini-3.8-flash-tts"]
# (verify)

[google."chirp3-hd"]
# (verify) the first 1,000,000 characters each month are free, then $30 per million (spaces count)
per_mchars = "30.00"
free_chars_per_month = 1_000_000
free_period_tz = "America/Los_Angeles"   # (verify) the billing month

[openai."whisper-1"]
per_audio_minute = "0.006"   # (verify)
```

Prices are dated and versioned, overridable in app data, and updated by PR. Every number the UI or voice
reports is labelled **estimated**.

## Budgets and estimates

- **Month-to-date spend, always visible.** The ledger keeps the month's total in memory (one SQL sum when it
  is first needed or the month changes, then each row adds itself under the insert's lock), so the budget
  check before every paid call, `GET /usage/budget` and the spend chip cost nothing. `usage.changed` carries
  the same `BudgetState` after a quiet second (bounded to 3 s under a steady stream of rows), only when what a
  client shows changed: the spend rounded to $0.0001, the budget, a free allowance, or the month. Kokoro's $0
  rows never reach a client. `BudgetState.free_tiers` lists provider allowances that cost nothing until used
  up; providers register a `FreeTierSource` (`usage/free_tier.py`; Google's free Chirp characters, above).
  The ledger is the only trigger of the budget alerts.
- **Free allowances come first.** A call that fits in a free allowance costs nothing and the budget doesn't
  apply to it; past the allowance, the budget applies as to any paid call (Google Chirp 3 HD, above).
- **Monthly budget** (`settings.toml` `[budget] monthly_usd`, `warn_at = 0.8`, `hard_stop = true`).
  - At 80%: a spoken notice and a bar row, once per threshold crossing.
  - At 100%: paid calls are refused with `budget_exceeded`. Reading continues on Kokoro and Classic rules. The user can override ("raise my budget to $20", confirmed).
- **Pre-flight estimate** when a single operation is estimated above `estimate_threshold_usd` (default `0.50`). Applies to reading a document with a paid voice, large documents in AI Enhanced mode, whole-document exports, transforms and keep-offline pins. Reading with a local voice and Classic cleanup is free and is never estimated.
  - **One place asks.** Every way of starting a read (the bar, the reader's play button, the tray, a hotkey, the queue, the assistant, the API) goes through the playback service, which refuses a read over the threshold the first time with `estimate_required` and the amount. The go-ahead is any of: a confirmation card in the bar ("yes" by voice or Enter), `confirm_estimate: true` on the request, or simply asking to play again within two minutes (the message says so). A queue is agreed to as a whole, so reading never stops halfway to ask. A document opened with `play: true` comes back with its `estimate` and `playing: false`, and clients are told the amount in a `usage.alert`; one press of play then starts it. Once agreed, a document is not asked about again in that session.
  - **Estimator:** extracted chars → TTS chars after Classic filters, minus cached utterances; annotate tokens ≈ block tokens × 1.1 + output allowance; vision per image/frame. Target ±25% for text documents (FR-COST-3).
- **P2:** "prepare whole document" through the Claude Message Batches API (50% cheaper, minutes to hours) for books queued in advance.

### Reference costs (estimates, Haiku 5.5 + OpenAI TTS)

| Item | Approx. |
|---|---|
| 300-page book, AI Enhanced annotate | ~$0.04 on Haiku 5.5 (~$0.45 on Sonnet 5.5) |
| 300-page book, OpenAI TTS (~11 h audio) | ~$10 |
| Same book on Chirp 3 HD (~600K characters) | $0 inside the month's free million characters; ~$18 past it |
| Same book on Kokoro | $0 |
| One image described | ~$0.0002 on Haiku 5.5 (~$0.005 on Sonnet 5.5) |
| One assistant turn | ~$0.0004 on Haiku 5.5 (20 live turns cost $0.008, 2026-11-22) |

# 05 — Speech pipeline

Status: Draft v0.1 · 2026-11-22

```
Source ──ingest──▶ Document(blocks) ──segment──▶ sentences/words
        ──plan (filter → annotate → transform)──▶ SpeechScript(utterances)
        ──chunk──▶ synthesis units ──synthesize──▶ audio (cached)
        ──time──▶ word/segment timings ──play──▶ transport (+ highlight events)
```

Every stage is **cached by content hash** and **resumable**. Work happens just ahead of the playhead, so
a 300-page book starts in seconds and stopping early costs almost nothing.

## 1. Ingest

| Kind | Source id | Notes |
|---|---|---|
| File (pdf, docx, txt, md, image, audio) | `sha256(bytes)` | Same file anywhere = same document |
| URL (headless) | `sha256(url + normalized extracted text)` | Fetched through the page store (`03` § The asset store): reused while fresh, revalidated when older, so the same link opened by the user and by the assistant is one download. Re-fetch detects changed content |
| `html_blocks` (from the shell) | `sha256(url + normalized block text)` | Logged-in/paywalled pages work because extraction happens in the user's session |
| Text / clipboard text | `sha256(normalized text)` | |
| Clipboard image | `sha256(png bytes)` | |

## 2. Extract → `Document`

```python
Block(id, type, text, order, page?, bbox?, marks: list[Mark], anchor)
Mark(id, kind, start, end)  # kind: citation_ref | link | url | emphasis | parenthetical | bracket | brace | formula | footnote_ref
```

**Block types:** `heading(level)`, `paragraph`, `list_item`, `quote`, `code`, `table`, `caption`, `figure`,
`footnote`, `header`, `footer`, `page_number`, `equation`, `toc`, `reference_entry`.

**PDF heuristics** (pypdfium2 char boxes + pdfplumber layout):
- **Header/footer:** lines in the top/bottom 8% whose normalized text repeats on ≥ 30% of pages.
- **Page number:** short numeric or roman lines in those bands, or patterns like "Page 4 of 18".
- **Footnote:** small font (≤ 85% of body median) at the page bottom, starting with a marker that matches an in-text reference mark.
- **Columns:** cluster x-ranges of text lines per page; read column by column. A full-width heading breaks the columns.
- **Caption:** a line starting with `Figure|Fig.|Table|Listing N` adjacent to an image or table region.
- **References section:** a heading matching `References|Bibliography|Works Cited` → following entries typed `reference_entry`.
- **Hyphenation:** rejoin end-of-line hyphenated words; keep original offsets in the anchor map.
- **Scanned pages** (no text layer): flagged. P1 "AI Enhanced extraction" sends them to Claude vision, estimated first.

**Marks** are found deterministically (regex + layout): numeric citations `[12]`, `[3–5]`, author-year
`(Smith et al., 2020)`, URLs, bracket/brace/parenthetical spans, inline formulas. Marks are *candidates*.
The reading instructions and rules decide what happens to them.

**DOCX:** headings, lists, tables, footnotes and comments map directly from python-docx structure.
**Web:** the shell preload sends Readability-based blocks with DOM anchors. Headless uses trafilatura.

- **Typing in the preload.** Everything inside a data table is a `table` block and a picture's caption is a `caption`, whatever tag the page used, so skip lines for tables and captions decide whether they are read. Short link-only siblings of headings ("[edit]") are dropped before Readability runs, because they made it discard most headings on long articles.
- **The lead comes first.** Side material that the page's markup puts ahead of the opening paragraph (an infobox, a picture and its caption) is moved to just after the first body paragraph, on both paths (`ingest/lead.py`). A body paragraph is prose: at least 80 characters, or ending in sentence punctuation. Blocks keep their type, id, text and anchor; only the order changes. Nothing moves when a heading stands between a table and the lead (a page that opens with data keeps its order), or when the page has no body paragraph.

**Language.** Each document is labelled English or Spanish (or neither) from the words at its start (`document/language.py`). The label chooses the reading voice (`08` § Voices and languages) and the sentence rules below.

## 3. Segment

Sentences and words with stable char offsets into each block (rule-based sentence splitter with an
abbreviation list, pySBD; words via Unicode word boundaries). Offsets are the backbone of
highlighting and span ops.

The rules follow the document's language. For Spanish: pySBD's Spanish rules, plus a pass that keeps an
abbreviation with what follows it ("Sra. Ellis", "EE. UU.", "s. XVI", "n.º", "p. ej.", "a. C."), and
`¿ ¡ «` and accented capitals count as sentence openers. Without it, English rules cut Spanish text after
"Sr." and each fragment is spoken with a sentence pause.

## 4. Plan → `SpeechScript`

```python
Utterance(id, text, voice_ref, anchors: list[Anchor], fidelity: "exact"|"block"|"section"|"media", rule_trace: list[RuleHit])
```

Cleanup is Claude following the user's reading instructions (`SPEECH.md`, `06` § The reading instructions)
and rule files. It runs in three tiers, **cheapest first**. There is no mode to choose: without Claude in the
build, or while it can't be reached or the budget is spent, only tier 1 runs.

### Tier 1: Filter (free, instant)
Compiled deterministic rules: drop blocks by type (`header`, `footer`, `page_number`, …), drop marks by
kind (`citation_ref`, `url`, …), regex replacements, and lexicon substitution (`SQL → sequel`). They come
from rule files and from the skip lines of the reading instructions ("- Skip footnotes and footnote
markers.", compiled offline, `rules/prompt.py`).

### Tier 2: Annotate (Claude span ops)
Claude applies the reading instructions and the rules that need judgment ("skip author-year citations but
keep the author name when it's the subject", "don't read the legal boilerplate"). It receives a **batch of
blocks with IDs**, the reading instructions (without the lines tier 1 already applies) and the active rules
(those lines included, each as a rule with the whole line as its instruction), and returns **span ops**
through structured outputs:

```json
{"ops": [{"block_id": "blk_12", "op": "skip", "quote": " (see Smith and Jones, 2020)", "occurrence": 1, "text": "", "rule_id": "speech_skip-citations"},
         {"block_id": "blk_12", "op": "replace", "quote": "e.g.", "occurrence": 1, "text": "for example", "rule_id": "speech_md"}]}
```

- **Claude sees where tier 1 already cut.** The text it receives is the filtered text with a marker (`⟦url⟧`, `⟦citation⟧`, `⟦footnote⟧`, …) wherever a filter removed a span: "More details are given at ⟦url⟧ together with the evaluation scripts." Markers have no width (a quote may run across one) and are never spoken. Without them a sentence with an intended hole reads as damage to repair, and the model deleted the clause after it.
- **Every op names its rule.** `rule_id` is an active rule, or `speech_md` when only the rest of the reading instructions asks for it.
- **A guard bounds what an op may remove** (`plan/guard.py`). An op credited to a skip line of the reading instructions, or to no rule, may remove the kind of span the active skip lines name (an address, a bracketed group with a year or number, an author-year item, a parenthetical, a running head that repeats in the document) plus at most 3 other words (24 letters): the connective that only introduced it. An op that removes more is refused, logged, and the text is read as written; the script carries a warning saying how many edits were ignored. Ops credited to a rule file of tier `annotate`, or to the rest of `SPEECH.md`, are not bounded: there the user asked for judgment over whole passages. The guard also runs when stored ops are applied again.
- **Spans are identified by exact quote + occurrence, never by numeric offsets.** The engine locates the quote in the block and computes offsets itself. *Measured on 2026-11-21:* Sonnet 5.5 returned wrong character offsets with thinking off. Quote-anchored ops were correct in repeated trials at 2.1–2.5 s and ~140 output tokens per call. Numeric offsets were only right with adaptive thinking, at 7.6 s and ~925 output tokens.
- Ops that don't match their block exactly are **dropped and logged**, never applied fuzzily.
- **Default config:** `claude-haiku-5-5` with adaptive thinking (`max_tokens` 8192, since thinking counts toward it), `effort: "low"`, structured outputs. The model's `annotate_thinking` trait in the catalog decides thinking: on Sonnet 5.5 it is off (`between_tools`), on Haiku 5.5 adaptive, because the AI cleanup accuracy gate (`08` § AI Enhanced accuracy gate) found Haiku with thinking `disabled` dropping kept text (a cited author who is the subject, a year that carries meaning) while adaptive thinking at low effort matched Sonnet: 0 must-keep violations and 100 % must-drop recall over three runs, at a tenth of the cost, faster at the median (1.5 s vs 1.8 s) with a longer tail (p90 7.6 s vs 3.5 s). The system prompt, the reading instructions and the rules form a stable, prompt-cached prefix. The prompt version (`annotate-3/transform-1`) is part of the plan key, so a prompt change rebuilds plans. Users can switch the model or effort in settings. Haiku 5.5 has no server-side refusal fallback: a declined batch is read with tier-1 cleanup. *Re-run on 2026-11-23 with the reading instructions in place of presets:* Haiku 5.5 adaptive/low, 0 must-keep violations and 100 % must-drop recall (9/9), same as the Sonnet baseline.
- **Batching:** ~1.5k tokens of blocks per call. Run the first batch before playback starts (first open ≈ 2.5 s + TTS first byte), then stay ≥ 2 batches ahead of the playhead.
- **Cost:** a 300-page book (~150k tokens, ~100 batches) ≈ $0.015 input + ~$0.02 output, thinking included ≈ **$0.04 on Haiku 5.5** (≈ $0.45 on Sonnet 5.5). Each batch's prompt is far below Haiku's 100K-token threshold, so the cheaper rate card applies. Re-opens are free (plan cache).
- Text never gets rewritten wholesale, so every spoken word still maps to the page.

### Tier 3: Transform (generative)
For rules that change *what* is said: "summarize each section in two sentences", "read only the abstract
and conclusions", "explain equations in words", "describe figures", image descriptions.
- Config: the settings model (`claude-haiku-5-5` by default), adaptive thinking, `effort: "medium"`, streaming.
- Output utterances carry **block-, section- or media-level anchors** (fidelity `block`/`section`/`media`). The reader outlines the region instead of highlighting words. Listening-first means this is fine.
- Always estimated first for whole-document transforms.

### Rule trace
Every utterance records which rule changed what (`rule_trace`). This powers **Script view**, the
**speech diff** preview, and answers to "why didn't it read that?"

## 5. Multimodal

| Input | Path | Default output | Cost (Haiku 5.5, est.) |
|---|---|---|---|
| Image (open/paste/clipboard/screenshot) | Downscale to ≤ 1280 px long edge → Claude vision with a transcribe-or-describe instruction, plus active rules | Text-heavy → verbatim transcript as blocks; visual → 1–5 sentence description; mixed → both | ≈ $0.0002 per image (~1.2k input tokens; ≈ $0.005 on Sonnet 5.5) |
| Figure inside PDF/web (P1) | Crop the figure region → vision | Description utterance anchored to the figure block | ≈ $0.0002 per figure |
| Audio file (P1) | STT (cloud or local) → transcript blocks → rules | Re-voiced or summarized transcript | STT only, unless transformed |

Video is **deferred** (out of scope for now). The vision and transform calls **have no tools**. Their output is validated and treated as content.

## 6. Chunk

Synthesis units are 1–3 sentences (≈ 200–400 chars, never over 1000), split only at sentence boundaries
(by the document's language) and kept under each provider's input limit from `models.toml`. Short units
start audio sooner and give exact sentence timing. Grouping 2–3 sentences avoids choppy prosody. The
first unit of a session is a single sentence, to minimize time-to-first-audio.

## 7. Synthesize

| Provider | Call | Stream format | Stored as |
|---|---|---|---|
| OpenAI | `audio.speech` streaming, `gpt-4o-mini-tts`, `instructions` for style | Opus (`response_format="opus"`) | Ogg/Opus as received |
| Gemini | **Interactions API** `client.interactions.create(..., stream=True, response_format={"type":"audio"})` with `generation_config.speech_config=[{"voice": ...}]`; style via a `speech_metadata` annotation | Headerless L16 PCM, 24 kHz mono | Encoded to Ogg/Opus |
| Kokoro (local) | onnxruntime, timestamped ONNX export; phonemes from the built-in G2P for the voice's language (English lexicon, Spanish rules) | PCM + per-token durations | Ogg/Opus + timings |
| Qwen3-TTS (local) | mlx-audio 0.6.7 in a worker process (`providers/qwen3_worker.py`), streamed (`stream=True`, 0.5 s pieces), seeded per utterance | PCM pieces while generating | Ogg/Opus + estimated timings |
| OpenAI-compatible (P1) | `POST {base}/v1/audio/speech` | As configured | Ogg/Opus |

*Measured first audio for a one-sentence utterance (2026-11-21):* OpenAI `gpt-4o-mini-tts` 1.38 s;
Gemini `gemini-3.8-flash-tts` / `-lite-tts` 1.97 s (streaming).

Providers that fail (429, 5xx) retry with jittered backoff. Persistent failure falls back per settings:
same provider, another voice, or Kokoro, with a spoken notice.

Fresh audio goes to the transport the moment it exists (`Synthesizer.synth(on_audio=…)`); encoding it to
Opus and writing it to the store happen behind the playback, not in front of it. A replay reads the stored
copy.

**Growing utterances.** A streaming local voice (`provider.streams`: Qwen3, about 2x real time) hands each piece over
as it is generated (`Synthesizer.synth(on_chunk=…)`), so the transport can start an utterance before it is finished:
the piece goes into a `render.GrowingPcm`, which the renderer plays up to what has arrived and ends only once it is
complete. The transport hands a growing utterance to the renderer once the audio already there, plus what will be
generated while earlier audio plays, covers the projected shortfall `max(0.8 s, remaining × (1 − rtf / (speed ×
1.1)))` (remaining = the utterance's estimated length, from the characters per second this voice has shown, minus
what has arrived; `rtf` = the voice's measured speed). If generation still falls behind, the renderer fades out over
5 ms and back in when audio returns (counted as a synthesis wait). The sentence is tinted while the utterance grows;
its word timings are estimated from the whole audio when it is complete, and the word highlight takes over at the
playhead. A seek or skip cancels generations nobody will hear (`provider.cancel_on_seek`). When the measured speed is
below `speed × 1.1`, the engine says once that the voice can't keep up at this speed (08 § Local Qwen3-TTS voices).

## 8. Time (word sync)

| Voice | Word timing | Fidelity |
|---|---|---|
| Kokoro | Native: the timestamped export yields per-token durations → word times | Exact |
| Cloud: Chirp, Gemini, OpenAI (later Qwen3) | Sentence boundaries are exact (one utterance per unit); words inside are **pause-anchored**: pauses found in the audio are matched to punctuation, pinned, and words spread by weight between pins (below) | Sentence exact, word ≈ (p50 51 ms, p90 139 ms) |
| Cloud (v1.1, if needed) | Local forced alignment of cached audio (Qwen3-ForcedAligner-0.6B or whisper DTW) in the background, refining timings in the cache. Parakeet's token times are too coarse for this (80 ms frames, ~0.3 s bias) and keep 1.9 GB resident | Exact |

**Pause-anchored estimate** (`playback/timing.py` `estimate_word_timings_from_audio`, every voice without
timestamps):

1. 10 ms frames, level in dB re the loudest frame. Speech starts and ends at the first and last frame above
   −40 dB; a **pause** is a run of frames below −40 dB lasting ≥ 80 ms inside the speech (shorter silences are
   mostly stop-consonant closures); two pauses split by ≤ 80 ms of quiet sound below −30 dB (a breath) are one.
2. Every gap between two words is a slot with a class from its punctuation: none, comma, clause (`; : —`),
   sentence (`. ! ? …`).
3. A monotonic dynamic program matches pauses to slots: a match scores the slot's class (an unpunctuated slot
   scores −2, so a pause there must fit well), the pause's length, and how close the slot's share of the word
   weight is to the pause's share of the *speech* time (pauses taken out); leaving a pause unexplained costs
   more the longer it is, leaving punctuation without a pause costs by class. Two passes: the second counts
   unmatched pauses as speech.
4. Matched pauses pin word ends and starts to their edges; between pins, words are spread by weight
   (0.5 × syllables + 0.15 × letters), leaving a small gap at punctuation that had no pause.

Measured against Kokoro's exact word onsets (`uv run python -m tools.timing_eval`: Kokoro renders 40 English and
20 Spanish sentences in five voices, its timings are dropped and re-estimated from the audio alone; 2,400
words). The old estimate spread syllables evenly over the voiced span.

| Word-onset error | Old p50 | Old p90 | New p50 | New p90 |
|---|---|---|---|---|
| English (af_heart, am_michael, bf_emma; 1,824 words) | 119 ms | 352 ms | 52 ms | 141 ms |
| Spanish (ef_dora, em_alex; 576 words) | 91 ms | 252 ms | 45 ms | 131 ms |
| All | 111 ms | 332 ms | **51 ms** | **139 ms** |
| Held out (12 + 6 other sentences, incl. unseen voices am_echo, bm_george, em_santa; 686 words) | 91 ms | 292 ms | 53 ms | 146 ms |

The weights and costs were fitted on the first set; the held-out row is the honest figure. Words just after a
pause are estimated about 50 ms early relative to Kokoro's phoneme boundary (the audio rises before the model's
nominal onset), which reads as the highlight leading the voice slightly. A voice that never drops below −40 dB
between phrases (em_santa) gets no pins and falls back to weights with punctuation gaps. Kokoro's prosody is
not Chirp's: recheck with Chirp audio once the key works (the plan's bar for a forced aligner is p90 > 120 ms).

## 9. Play

The transport (see `03`) streams cached or incoming audio through the renderer to the output device, and emits
`playback.word` events at word boundaries scaled by the current speed. Speed is **not** part of any cache key.

**Chain** (`playback/`): utterance PCM (24 kHz) → `stretch.py` → resampled to the device's native rate with
soxr (VHQ) in the render thread → a ~0.3 s ring → `pull()` in the audio callback, which only copies, ramps,
sums and soft-limits from preallocated buffers. Replies, previews, recordings and `/assistant/say` are
`ClipStream`s mixed in `pull` (never stretched; appended in pieces, played gapless). Nothing was ever listened to
before round 3; every choice below was measured in the audio lab (`engine/tools/audiolab`, results in
`tools/audiolab/results/`) and then put to blind listening (`~/Downloads/open-speech-listening/`).

- **Time-stretch.** At exactly 1x a bit-exact bypass (the old audiotsm WSOLA ran even at 1x: 7.6 splice jumps
  per second, residual +2.2 dB against the source, the "flutter"). Other speeds: WSOLA with Hann frames whose
  windows sum to one, a fractional analysis hop (exact speed), and splice points chosen by FFT-normalized
  cross-correlation over the overlap. Up to 2x: 40 ms frames, 50 % overlap, ±15 ms search. Above 2x: 30 ms,
  75 %, ±10 ms, which keeps more short sounds (Parakeet round-trip WER at 3x 37 % vs 43 % old, 50 % for the
  40 ms setting). Speed changes re-render from the playhead with an aligned 10 ms crossfade (heard within
  ~50 ms). Per-hop source positions place words to ~10 ms.
- **Fades** (`fade.py`, raised cosine): pause/duck 12 ms, resume 12 ms, seek/skip/voice change crossfade 12 ms,
  stop 15 ms (the device stops only after the fade has played), a render-thread underrun 5 ms at each edge,
  3 ms at the ends of every utterance or clip whose edges aren't already silent. Before: 4 hard edges in a
  pause/resume test, 3 in a seek test, 1 at stop; now none.
- **Timing.** Positions are recorded in `pull` and reported at `frames handed out − output latency`, so the
  highlight follows what is audible; `assistant.speech` started/ended use the same clock (ended = last sample
  audible + `voice_input.echo_tail_ms`, 150 ms), emitted by the transport's ticker.
- **Output** (`output.py`): the device's native rate (48 kHz on a MacBook Air), callback mode, 100 ms latency,
  `sys.setswitchinterval(0.001)` while a device is open; PortAudio status flags counted; the stream reopens
  when the system output device or its rate changes. Deadline harness (an audio-callback probe against the real
  renderer, 10 ms period, M4 Air): with Python's default 5 ms switch interval a pure-Python thread (G2P)
  delayed the callback up to 31 ms, all background work at once up to 69 ms; at 1 ms G2P alone stays under
  7 ms. Callback lateness, worst seen (ms), against the device buffer it must fit in:

  | Load (probe every 10 ms unless noted) | Before (5 ms switch, old renderer, onnxruntime defaults) | After, 5 ms switch | After, 1 ms switch |
  |---|---|---|---|
  | Idle | 7.4 | 7.1 | — |
  | Kokoro, one synthesis / two at once | 12.9 / 9.1 | 10.5 / 4.2 | — |
  | G2P flat out (a long document opening) | 42.8 | 31.5 | 6.1 |
  | Opus encoding / Parakeet | 24.8 / 19.8 | 6.4 / 5.4 | — |
  | Reading worst case: Kokoro + G2P + Opus, 5 min | — | — | 34.2 (0 over 40) |
  | Everything at once, 1 min / 5 min | 54.4 | 69.0 | 56.2 / 32.6 |
  | Everything at once, probe = 60 ms buffer, 3 min | — | — | 99.2 (1 miss) |
  | Everything at once, probe = 100 ms buffer, 3 min | — | — | 26.6 (0 misses) |
  | Everything at once, blocking writes of 10 ms into a 100 ms FIFO, 3 min | — | — | 65.3 (0 underruns) |

  The 30 ms PortAudio "low" latency used before missed deadlines under load. 40 and 60 ms each missed under the
  everything-at-once load (which the app can't produce any more: one Kokoro synthesis at a time, reading paused
  while the bar listens), so the output uses 100 ms: a pause or stop is still heard within ~115 ms and the
  highlight is latency-corrected. Callback mode is kept: a blocking-write stream fed by a writer thread has the
  same GIL budget for the same depth and measured no better (last row; `--mode blocking`), and stays available
  as `SoundDeviceOutput(mode="blocking")`.
- **CPU.** Kokoro: 4 intra-op threads, 1 inter-op, no spinning, one `sess.run` at a time (RTF 0.092 vs 0.099 for
  onnxruntime's defaults; a second concurrent synthesis only doubled the wait for the first). Each provider
  declares `max_concurrency` (Kokoro 1, cloud 3), replacing the transport's global limit of 2.
- **Kokoro precision** (`tools.audiolab kokoro-variants`, same pinned revision, 60 sentences, engine session
  options; first audio warm, median of 5):

  | Weights | Size | First audio, 22 / 64 / 89 / 200 chars (ms) | RTF | 4–12 kHz in pauses | Log-spectral distance to fp32 | Parakeet WER en / es |
  |---|---|---|---|---|---|---|
  | q8 (shipped) | 92 MB | 165 / 422 / 566 / 1488 | 0.108 | -97.7 dB | 2.7 dB | 1.8 % / 0.5 % |
  | q8f16 | 86 MB | 181 / 472 / 598 / 1415 | 0.124 | -98.3 dB | 2.8 dB | 1.6 % / 0.5 % |
  | fp16 | 163 MB | 432 / 977 / 1251 / 3257 | 0.273 | -97.0 dB | — (NaN/inf samples in 8 of 60) | 12 % / 17 % |
  | fp32 | 326 MB | 437 / 1051 / 1402 / 3728 | 0.280 | -97.9 dB | 0 | 1.8 % / 0.5 % |

  q8 stays: it is no noisier and no less intelligible, and fp32 would take ~2.5x as long to the first sound
  (the rule was ≤ 1.3x). The spectral difference is real (2.7 dB), so the blind pair q8 vs fp32 is in the
  listening set; the owner's ears decide. Kokoro's cache key carries `model/quant`, and `bundle-engine.sh` takes
  its Kokoro pins from the catalog, so a switch is a catalog edit.
- **Diagnostics:** `GET /api/v1/diagnostics/audio` (callbacks, device underflows, render starvations vs.
  synthesis waits, ring low-water, slowest pull) and `open-speech doctor audio` (a self-test that renders into
  memory and plays nothing).

Several documents can wait in the **reading queue**. When one ends, the next starts by itself. `POST
/playback/queue` adds documents (`now`, `next` or `last`); "read these" with several attachments plays the
first and queues the rest.

## Caches and keys

| Cache | Key | Invalidated by |
|---|---|---|
| Extract | `sha256(source_id, extractor_version)` | Extractor upgrades |
| Plan | `sha256(source_id, rules_hash, model_id, prompt_version, mode)` | Rule/lexicon/`SPEECH.md` edits (only affected blocks re-plan), model or prompt change, "regenerate" |
| Audio (asset kind `voice`) | `sha256(utterance_text, provider, model identity, voice, style, format, engine_audio_version)`; the model identity is the model id, except Kokoro's `model/quant` and Qwen3's `<model>/<variant>@<revision>/p<params_version>/<language>[/ref-<hash>]` (`cache_identity()`), so a change of weights, sampling or clone reference re-synthesizes only that voice's audio. Stored as Ogg/Opus at libsndfile compression level 0.8 (~57 kbps on speech; the old default ~36 kbps measured 2.0 dB log-spectral distance from the fresh audio, 0.8 measures 0.9 dB) | Nothing. Content-addressed, so identical text + voice is reused across documents. The transcript is stored beside the audio and hashed on its own, so every rendering of a sentence can be found by its words |
| Transcript (asset kind `transcript`) | `sha256(recorded audio, recognizer, model, language)` | Nothing; follows the recording-retention setting |
| Page (asset kind `page`) | `sha256(normalized URL)` | Replaced when the server sends a new copy; a copy older than 15 minutes is revalidated (ETag / Last-Modified) |
| Timings | Same as audio + `aligner_version` | Aligner upgrades |

The plan cache also **pins LLM output**, so the same document sounds the same on every replay.
`rules_hash` covers only the rules whose scope matches the document, so editing an arxiv rule doesn't
invalidate your DOCX plans. Voice audio, transcripts and pages share one store with one budget
(`03` § The asset store): eviction is LRU by last use across all three, with a 5 GB default cap; pinned
"keep offline" documents are exempt.

## Latency budget (time to first audio, p50)

| Path | Budget | Composition |
|---|---|---|
| Cached replay | ≤ 300 ms | Cache read + output start |
| Kokoro, no Claude | ≤ 0.5 s | Extract first page + filter + Kokoro first sentence |
| Cloud, no Claude | ≤ 2.5 s | Extract first page (≤ 1 s, in parallel) + TTS first byte (1.4–2.0 s) |
| Cloud, with Claude cleanup (first open) | ≤ 4.5 s | First annotate batch (~2.5 s) + TTS first byte |
| Re-open with plan cached | ≤ 2.5 s cloud / 0.3 s if audio cached | — |

### Measured: local voice (2026-11-22)

In-process on an Apple M4, sound output disabled, from the play request to the first non-silent block the
output pulled. Before and after the first-audio work, back to back; other jobs were running on the
machine, so read the figures as ±15%. p50 / p90 in ms, 10 runs unless noted.

| Scenario | Before | After |
|---|---|---|
| Cold engine, 89-character first sentence (document opened 0.2 s before play) | 1274 / 1286 | 642 / 663 |
| Warm engine, new document, 89-character sentence | 1187 / 1326 | 647 / 708 |
| Warm engine, utterance already stored | 44 / 51 | 46 / 48 |
| Cold engine, fresh profile, one request opens a PDF and plays (65-character title) | 1248 / 1394 | 810 / 907 |
| Warm engine, 1,500-paragraph document (5 runs) | 1233 / 1258 | 758 / 774 |

What changed: fresh audio is played before it is encoded and stored (encoding cost about 190–240 ms and decoding the file
just written another 25–35 ms, all in front of the first frame); with a local voice the playhead is
synthesized alone before prefetch starts (893 ms with prefetch alongside, 644 ms alone); the voice starts
loading when a document is opened instead of at the first sentence (250–320 ms for the session, about
70 ms for the English lexicon).

**The 0.5 s target is not met for an ordinary first sentence: warm p50 is about 0.65 s.** What is left is
model inference. The bundled q8 model takes roughly 105 ms per second of audio on this machine, so 0.5 s
is only reachable when the first unit is about 40–60 characters (measured alone: 22 characters 289 ms, 38
characters 484 ms, 64 characters 669 ms, 89 characters 938 ms under load). Options not taken: splitting
off a short first clause in the chunker (prosody unjudged, nobody has listened); streaming partial audio
into the renderer; other weights or runtimes (fp32 needs a download; thread tuning gave at most 13%;
CoreML gave nothing). Round 3 measured the alternatives (§ 9): fp32 is 2.5x slower to first audio than q8 and not
measurably cleaner, so q8 stays; four intra-op threads without spinning are 7 % faster than onnxruntime's defaults.

## Prefetch and cost bounds

- Synthesize ~30 s ahead; never more than 2 minutes ahead unless the document is pinned "keep offline". With a local voice, prefetch waits until the playhead has its audio.
- Annotate ≥ 2 batches ahead.
- On pause or stop, in-flight work finishes and is cached; nothing new starts.
- A whole-document operation (export, keep offline, transform, regenerate) always shows an estimate first when above the threshold.

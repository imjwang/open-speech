# 05 — Speech pipeline

Status: Draft v0.1 · 2026-11-21

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
| URL (headless) | `sha256(url + normalized extracted text)` | Re-fetch detects changed content |
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
Presets and rules decide what happens to them.

**DOCX:** headings, lists, tables, footnotes and comments map directly from python-docx structure.
**Web:** the shell preload sends Readability-based blocks with DOM anchors. Headless uses trafilatura.

## 3. Segment

Sentences and words with stable char offsets into each block (rule-based sentence splitter with an
abbreviation list, e.g. pySBD; words via Unicode word boundaries). Offsets are the backbone of
highlighting and span ops.

## 4. Plan → `SpeechScript`

```python
Utterance(id, text, voice_ref, anchors: list[Anchor], fidelity: "exact"|"block"|"section"|"media", rule_trace: list[RuleHit])
```

Rules apply in three tiers, **cheapest first**. Only the tiers the active rules need actually run.

### Tier 1: Filter (Classic, free, instant)
Compiled deterministic rules: drop blocks by type (`header`, `footer`, `page_number`, …), drop marks by
kind (`citation_ref`, `url`, …), regex replacements, and lexicon substitution (`SQL → sequel`). This
covers every Speechify-style toggle.

### Tier 2: Annotate (AI Enhanced, Claude span ops)
For rules that need judgment ("skip author-year citations but keep the author name when it's the subject",
"don't read the legal boilerplate"). Claude receives a **batch of blocks with IDs** plus the active rules and
`SPEECH.md`, and returns **span ops** through structured outputs:

```json
{"ops": [{"block_id": "blk_12", "op": "skip", "quote": " [12], [13]", "occurrence": 1, "text": ""},
         {"block_id": "blk_12", "op": "replace", "quote": "e.g.", "occurrence": 1, "text": "for example"}]}
```

- **Spans are identified by exact quote + occurrence, never by numeric offsets.** The engine locates the quote in the block and computes offsets itself. *Measured on 2026-11-21:* Sonnet 5.5 returned wrong character offsets with thinking off. Quote-anchored ops were correct in repeated trials at 2.1–2.5 s and ~140 output tokens per call. Numeric offsets were only right with adaptive thinking, at 7.6 s and ~925 output tokens.
- Ops that don't match their block exactly are **dropped and logged**, never applied fuzzily.
- **Default config:** `claude-sonnet-5-5`, `thinking: {type: "between_tools"}` (thinking off; no tools are present), `effort: "low"`, structured outputs. The system prompt, preset rules and `SPEECH.md` form a stable, prompt-cached prefix. Users can switch the model or effort in settings. Haiku 5.5 was tested and made wrong skip judgments, so it isn't recommended for annotate.
- **Batching:** ~1.5k tokens of blocks per call. Run the first batch before playback starts (AI Enhanced first-open ≈ 2.5 s + TTS first byte), then stay ≥ 2 batches ahead of the playhead.
- **Cost:** a 300-page book (~150k tokens) ≈ $0.30 input + ~$0.15 output ≈ **$0.45 on Sonnet 5.5**. Re-opens are free (plan cache).
- Text never gets rewritten wholesale, so every spoken word still maps to the page.

### Tier 3: Transform (generative)
For rules that change *what* is said: "summarize each section in two sentences", "read only the abstract
and conclusions", "explain equations in words", "describe figures", image descriptions.
- Config: `claude-sonnet-5-5`, adaptive thinking, `effort: "medium"`, streaming.
- Output utterances carry **block-, section- or media-level anchors** (fidelity `block`/`section`/`media`). The reader outlines the region instead of highlighting words. Listening-first means this is fine.
- Always estimated first for whole-document transforms.

### Rule trace
Every utterance records which rule changed what (`rule_trace`). This powers **Script view**, the
**speech diff** preview, and answers to "why didn't it read that?"

## 5. Multimodal

| Input | Path | Default output | Cost (Sonnet 5.5, est.) |
|---|---|---|---|
| Image (open/paste/clipboard/screenshot) | Downscale to ≤ 1280 px long edge → Claude vision with a transcribe-or-describe instruction, plus active rules | Text-heavy → verbatim transcript as blocks; visual → 1–5 sentence description; mixed → both | ≈ $0.005 per image (~1.2k input tokens) |
| Figure inside PDF/web (P1) | Crop the figure region → vision | Description utterance anchored to the figure block | ≈ $0.005 per figure |
| Audio file (P1) | STT (cloud or local) → transcript blocks → rules | Re-voiced or summarized transcript | STT only, unless transformed |

Video is **deferred** (out of scope for now). The vision and transform calls **have no tools**. Their output is validated and treated as content.

## 6. Chunk

Synthesis units are 1–3 sentences (≈ 200–400 chars), split only at sentence boundaries and kept under
each provider's input limit from `models.toml`. Short units start audio sooner and give exact sentence
timing. Grouping 2–3 sentences avoids choppy prosody. The first unit of a session is a single sentence,
to minimize time-to-first-audio.

## 7. Synthesize

| Provider | Call | Stream format | Stored as |
|---|---|---|---|
| OpenAI | `audio.speech` streaming, `gpt-4o-mini-tts`, `instructions` for style | Opus (`response_format="opus"`) | Ogg/Opus as received |
| Gemini | **Interactions API** `client.interactions.create(..., stream=True, response_format={"type":"audio"})` with `generation_config.speech_config=[{"voice": ...}]`; style via a `speech_metadata` annotation | Headerless L16 PCM, 24 kHz mono | Encoded to Ogg/Opus |
| Kokoro (local) | onnxruntime, timestamped ONNX export | PCM + per-token durations | Ogg/Opus + timings |
| Qwen3-TTS (local, P2) | mlx-audio / faster-qwen3-tts / qwentts.cpp server | PCM | Ogg/Opus |
| OpenAI-compatible (P1) | `POST {base}/v1/audio/speech` | As configured | Ogg/Opus |

*Measured first audio for a one-sentence utterance (2026-11-21):* OpenAI `gpt-4o-mini-tts` 1.38 s;
Gemini `gemini-3.8-flash-tts` / `-lite-tts` 1.97 s (streaming).

Providers that fail (429, 5xx) retry with jittered backoff. Persistent failure falls back per settings:
same provider, another voice, or Kokoro, with a spoken notice.

## 8. Time (word sync)

| Voice | Word timing | Fidelity |
|---|---|---|
| Kokoro | Native: the timestamped export yields per-token durations → word times | Exact |
| Cloud (v1) | Sentence boundaries are exact (one sentence per unit or measured split points); words inside a sentence are estimated by syllable weight over the measured duration | Sentence exact, word ≈ |
| Cloud (v1.1) | Local forced alignment of cached audio (Qwen3-ForcedAligner-0.6B or whisper DTW) runs in the background and refines timings in the cache. Cloud alternatives: OpenAI `whisper-1` word timestamps, Gemini `gemini-3.5-transcribe` | Exact |

## 9. Play

The transport (see `03`) streams cached or incoming audio through WSOLA time-stretch (0.5–5x) to the
output device, and emits `playback.word` events at word boundaries scaled by the current speed. Speed is
**not** part of any cache key.

## Caches and keys

| Cache | Key | Invalidated by |
|---|---|---|
| Extract | `sha256(source_id, extractor_version)` | Extractor upgrades |
| Plan | `sha256(source_id, rules_hash, model_id, prompt_version, mode)` | Rule/lexicon/`SPEECH.md` edits (only affected blocks re-plan), model or prompt change, "regenerate" |
| Audio | `sha256(utterance_text, provider, model, voice, style, format, engine_audio_version)` | Nothing. Content-addressed, so identical text + voice is reused across documents |
| Timings | Same as audio + `aligner_version` | Aligner upgrades |

The plan cache also **pins LLM output**, so the same document sounds the same on every replay.
`rules_hash` covers only the rules whose scope matches the document, so editing an arxiv rule doesn't
invalidate your DOCX plans. Eviction is LRU by last play, with a 5 GB default cap; pinned "keep offline"
documents are exempt.

## Latency budget (time to first audio, p50)

| Path | Budget | Composition |
|---|---|---|
| Cached replay | ≤ 300 ms | Cache read + output start |
| Kokoro, Classic | ≤ 0.5 s | Extract first page + filter + Kokoro first sentence |
| Cloud, Classic | ≤ 2.5 s | Extract first page (≤ 1 s, in parallel) + TTS first byte (1.4–2.0 s) |
| Cloud, AI Enhanced (first open) | ≤ 4.5 s | First annotate batch (~2.5 s) + TTS first byte |
| Re-open with plan cached | ≤ 2.5 s cloud / 0.3 s if audio cached | — |

## Prefetch and cost bounds

- Synthesize ~30 s ahead; never more than 2 minutes ahead unless the document is pinned "keep offline".
- Annotate ≥ 2 batches ahead.
- On pause or stop, in-flight work finishes and is cached; nothing new starts.
- A whole-document operation (export, keep offline, transform, regenerate) always shows an estimate first when above the threshold.

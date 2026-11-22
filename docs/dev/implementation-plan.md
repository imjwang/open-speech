# Implementation plan and coordination brief

Status: active · 2026-11-21 · Branch `feat/implementation`

The spec in `docs/00`–`10` is the source of truth. This file says **who builds what** and pins the
**interfaces between workstreams** so they can be built in parallel. If you must change a signature
below, add to it rather than changing it, and say so in your final report.

## Environment facts (verification workstation)

- Linux x86_64, 2 CPUs, 7 GB RAM. Target remains macOS 14+ on Apple Silicon (NFR-6); the engine is
  portable Python, so it is developed and tested here.
- **Python 3.13** (3.14 could not be installed here; spec 03 allows the newest Python with wheels).
  `cd engine && uv run pytest`.
- PyPI and npm are reachable. **Blocked:** GitHub release downloads (so no Electron binary — install
  with `ELECTRON_SKIP_BINARY_DOWNLOAD=1`), Hugging Face (no Kokoro weights), OpenAI and Gemini APIs.
  There are **no provider keys**. All provider adapters are tested against recorded/handwritten
  fixtures (respx), and a deterministic **fixture provider** set stands in for real audio in e2e runs.
- No PortAudio on Linux: audio output must fall back to a `NullOutput` that advances on a real-time
  clock, so the transport, word events and highlight still work headless.
- Google Chrome for Testing is at `/usr/bin/google-chrome` (for Playwright UI tests of the renderer).
- Do **not** run `git commit`; the coordinator commits at integration points.

## Already in place (shared contracts — read these first)

| File | What |
|---|---|
| `engine/src/open_speech/document/model.py` | `Document`, `Block`, `Mark`, `BlockType`, `MarkKind`, `BBox`, `WordBox`, `DomAnchor`, `Anchor`, `SourceKind` |
| `engine/src/open_speech/plan/script.py` | `Utterance` (with `segments` spoken→source map), `TextSegment`, `RuleHit`, `SpeechScript`, `SpanOp` |
| `engine/src/open_speech/providers/base.py` | `TtsProvider`, `SttProvider`, `LlmProvider`, `VisionProvider` protocols; `Voice`, `TtsRequest`, `AudioChunk`, `WordTiming`, `Units`, `LlmCall`, `RuleBrief`, `CompiledRuleDraft`, `TransformOutput`, `VisionBlock` |
| `engine/src/open_speech/config.py` | `EngineEnv` (.env/keys/flags), `Paths` (app-data layout), `UserSettings` (settings.toml schema; dotted-key get/set) |
| `engine/src/open_speech/errors.py` | `EngineError(code, detail)` + `MissingKey`, `BudgetExceeded`, `EstimateRequired`, `NotFound` |
| `engine/src/open_speech/store/db.py` | `Database` (SQLite, WAL, migrations). Tables: documents(+fts), positions, plans, ledger, recordings, turns, actions, kv |
| `engine/src/open_speech/api/schemas.py`, `ws_schema.py` | REST and WS contracts. `engine/openapi.json` and `engine/ws-schema.json` are generated from them (`uv run python -m open_speech.api.export`) |
| `engine/src/open_speech/api/routes.py`, `app.py`, `auth.py` | Routers (thin) calling `engine.<service>.<method>`; token + Host/Origin guard; WS endpoint |
| `engine/src/open_speech/service/events.py` | `EventBus.publish(msg)` (thread-safe) — the transport and others publish `ws_schema` messages |

## Workstreams and ownership

Each workstream owns its directories exclusively. Tests go under `engine/tests/unit/<stream>/` and
`engine/tests/golden/` (pipeline) to avoid collisions.

### A — Pipeline (owner: pipeline agent)
Owns `ingest/`, `document/segment.py`, `rules/`, `plan/` (except `script.py`), `cache/extract.py`,
`cache/plans.py`, `data/presets/`, `data/skills/`, `tests/unit/pipeline/`, `tests/golden/`.

Binding interface:

```python
# ingest/__init__.py
class SourceInput(BaseModel):
    kind: Literal["file", "url", "text", "image", "html_blocks"]
    data: bytes | None = None; filename: str | None = None; media_type: str | None = None
    url: str | None = None; text: str | None = None; title: str | None = None
    html_blocks: HtmlBlocksPayload | None = None          # from api.schemas
    image_mode: Literal["auto", "transcribe", "describe"] = "auto"

class IngestResult(BaseModel):
    document: Document
    llm_calls: list[tuple[str, LlmCall]] = []             # (op, call) e.g. ("vision", call) → ledger
    source_bytes: bytes | None = None                     # original file/image for GET /documents/{id}/source

async def ingest(src: SourceInput, *, extract_cache: ExtractCache, vision: VisionProvider | None = None,
                 vision_rules: Sequence[RuleBrief] = ()) -> IngestResult
# Raises MissingKey("anthropic", "Describing images") when an image needs vision and vision is None.

# document/segment.py
def sentences(text: str) -> list[tuple[int, int]]        # char spans, stable, abbreviation-aware (pysbd)
def words(text: str) -> list[tuple[int, int]]
def syllables(word: str) -> int

# rules/model.py
class CompiledFilter(BaseModel): drop_blocks: list[BlockType] = []; drop_marks: list[MarkKind] = []
    replace: list[ReplaceSpec] = []  (pattern, with_ — field alias "with"); keep_blocks_only: list[BlockType] | None = None
class Rule(BaseModel): id, name, description, instruction, scope: RuleScope (api.schemas), enabled: bool,
    tier: Literal["filter","annotate","transform"], compiled: CompiledFilter | None, created_by: str,
    preset: bool, path: Path | None, updated_at: float (mtime)
    def to_out(self) -> api.schemas.RuleOut

# rules/files.py
class RuleStore:
    def __init__(self, paths: Paths) -> None             # paths.rules, paths.speech_md, paths.lexicon
    def ensure_defaults(self) -> None                    # copy presets on first run, create SPEECH.md/lexicon.csv
    def reload(self) -> None
    def all(self) -> list[Rule]; def get(self, rule_id: str) -> Rule
    def save(self, rule: Rule) -> Rule                   # writes rules/<name>.md; returns with path
    def delete(self, rule_id: str) -> None
    def rules_for(self, doc: Document) -> list[Rule]     # enabled + scope-matching, precedence-ordered (06)
    def conflicts(self, rules: Sequence[Rule]) -> list[str]
    def presets(self) -> list[Rule]; def set_preset(self, name_or_id: str, enabled: bool) -> Rule
    def mode_for(self, doc: Document, global_mode: str) -> Literal["classic","ai_enhanced"]
    def mode_overrides(self) -> list[tuple[RuleScope, str]]; def set_mode_override(self, scope, mode) -> None
    def speech_md(self) -> str; def lexicon(self) -> Lexicon
    def rules_hash(self, rules: Sequence[Rule]) -> str   # covers matching rules + lexicon + SPEECH.md
    async def watch(self, on_change: Callable[[], None]) -> None   # watchfiles; ≤ 1 s reload (FR-RULE-3)

# rules/lexicon.py
class Lexicon: entries: list[LexiconEntry]; add(entry) / remove(term) persist to lexicon.csv

# rules/compiler.py
async def compile_rule(instruction: str, *, site: str | None, doc_kind: str | None, doc_id: str | None,
                       scope_hint: str | None, llm: LlmProvider | None) -> tuple[Rule, LlmCall | None]
# Deterministic fallback when llm is None (or for obvious phrasings): "skip/stop reading X" for every preset
# noun, "only read the body", "say X as Y" handled elsewhere. Unknown instructions without a key → MissingKey.

# plan/planner.py
class PlanContext(BaseModel): document; rules: list[Rule]; lexicon: Lexicon; speech_md: str; mode; llm_model: str
class PlanResult(BaseModel): script: SpeechScript; llm_calls: list[tuple[str, LlmCall]] = []
class Planner:
    def __init__(self, plan_cache: PlanCache) -> None
    async def plan(self, ctx: PlanContext, llm: LlmProvider | None, *, first_batch_only: bool = True) -> PlanResult
    async def extend(self, ctx: PlanContext, llm: LlmProvider | None, script: SpeechScript, upto_utterance: int) -> PlanResult
    # Classic plans the whole document deterministically (fast). AI Enhanced annotates the first batch
    # (~1.5k tokens of blocks) synchronously; `extend` keeps ≥ 2 batches ahead. Plans are cached by
    # plan_key (05 § Caches); a cached plan is returned without LLM calls. Utterances: 1–3 sentences,
    # 200–400 chars, first utterance one sentence (05 § 6). Every utterance has `segments` and anchors.

# plan/diff.py
async def speech_diff(document: Document, *, current: Sequence[Rule], proposed: Rule, lexicon: Lexicon,
                      speech_md: str, llm: LlmProvider | None, max_blocks: int = 60) -> tuple[SpeechDiff, list[tuple[str, LlmCall]]]

# cache/extract.py — ExtractCache(paths).get(source_id, extractor_version) / put(document)
# cache/plans.py   — PlanCache(db).get(plan_key) -> SpeechScript | None / put(script) / drop_document(doc_id)
```

### B — Runtime (owner: runtime agent)
Owns `providers/` (except `base.py`), `local/`, `usage/`, `cache/audio.py`, `cache/keys.py`, `playback/`,
`data/models.toml`, `data/pricing.toml`, `tests/unit/runtime/`, `tests/fixtures/providers/`. Sole owner of
engine dependencies (`uv add` if needed).

Binding interface:

```python
# providers/keys.py
class KeyStore:
    def __init__(self, env: EngineEnv, *, use_keychain: bool) -> None   # env overrides keychain (08 § Keys)
    def get(self, provider: str) -> str | None
    def status(self, provider: str) -> Literal["configured","missing","invalid"]
    @staticmethod
    def detect(key: str) -> str | None                   # sk-ant- → anthropic, sk- → openai, AIza → gemini
    async def set(self, provider: str, key: str) -> tuple[bool, str]    # test call, then store; never logs the key

# local/catalog.py — ModelCatalog.load(paths) merges data/models.toml with paths.models_toml
# providers/registry.py
class ProviderRegistry:
    def __init__(self, env: EngineEnv, paths: Paths, catalog: ModelCatalog, keys: KeyStore) -> None
    def voices(self) -> list[Voice]                      # every catalog voice with available/unavailable_reason
    def tts_for(self, voice_id: str) -> tuple[TtsProvider, TtsRequest-template (model, voice)]
    def llm(self) -> LlmProvider | None; def vision(self) -> VisionProvider | None; def stt(self) -> SttProvider | None
    def default_local_voice(self) -> str                 # kokoro, or fixture voice when fixture mode is on
    def refresh(self) -> None                            # after a key change
# providers/fixture.py — FixtureTts (provider "fixture": deterministic tone-per-word PCM with exact word
#   timings), FixtureStt (scripted transcripts: FixtureStt.enqueue(text); streams partial words then final),
#   FixtureLlm/FixtureVision (deterministic annotate/compile/transform/vision for tests). Only registered
#   when env.open_speech_fixture_providers is true.

# usage/pricing.py  Pricing.load(paths); .version; .cost(provider, model, units: Units) -> Decimal
# usage/ledger.py
class Ledger:
    def __init__(self, db: Database, pricing: Pricing) -> None
    def record(self, *, provider, model, op, units: Units, document_id=None, turn_id=None,
               cache_hit=False, local=False, avoided_usd: Decimal | None = None) -> Decimal
    def summary(self, period, group_by) -> api.schemas.UsageSummary (needs budget_usd → pass in)
    def rows(...) -> api.schemas.LedgerPage; def spent(self, since: datetime) -> Decimal; def month_spent() -> Decimal
# usage/budget.py
class BudgetGuard:
    def __init__(self, ledger: Ledger, settings: Callable[[], UserSettings], events: EventBus) -> None
    def allow_paid(self) -> bool; def check_paid(self) -> None (raises BudgetExceeded when hard stop hit)
    def after_record(self) -> None                       # emits usage.alert once per threshold crossing
# usage/estimate.py
def estimate_read(document, script: SpeechScript | None, *, voice: Voice, mode: str, llm_model: str,
                  pricing: Pricing, audio_cache: AudioCache) -> api.schemas.Estimate  # ±25% target

# cache/audio.py
@dataclass class CachedAudio: key: str; path: Path; duration_s: float; sample_rate: int; words: list[WordTiming]
    def load_pcm(self) -> np.ndarray  (float32 mono)
class AudioCache:
    def __init__(self, paths: Paths, db: Database) -> None
    @staticmethod
    def key(text, provider, model, voice, style, fmt="ogg-opus") -> str   # 05 § Caches (speed not in key)
    def get(self, key) -> CachedAudio | None; def put(self, key, pcm: np.ndarray, sr: int, words) -> CachedAudio
    def evict(self, max_bytes: int, pinned: set[str] = frozenset()) -> int; def clear(self) -> int; def size_bytes(self) -> int

# playback/synth.py
class Synthesizer:
    def __init__(self, registry, audio_cache, ledger, budget, settings: Callable[[], UserSettings], events) -> None
    async def synth(self, text: str, voice_id: str, *, style: str | None = None, document_id: str | None = None,
                    turn_id: str | None = None, op: str = "tts") -> SynthResult(audio: CachedAudio, voice_id: str, notice: str | None)
    # cache first (writes a $0 cache_hit ledger row with avoided cost); budget check before paid calls;
    # on budget stop / persistent provider failure / offline → falls back to the local voice with a notice.

# playback/output.py — AudioOutput protocol; SoundDeviceOutput; NullOutput (real-time clock, headless)
# playback/stretch.py — WSOLA (audiotsm) pitch-preserving 0.5–5x
# playback/timing.py — estimate_word_timings(text, duration_s) -> list[WordTiming] (syllable-weighted)
# playback/transport.py
class Transport:
    def __init__(self, synth: Synthesizer, output: AudioOutput, events: EventBus, *,
                 settings: Callable[[], UserSettings], on_position: Callable[[str, int, int], None]) -> None
    async def load(self, *, document_id: str, title: str, page_count: int | None, script: SpeechScript,
                   start_index: int = 0, offset_ms: int = 0, voice_id: str, style: str | None, speed: float) -> None
    async def play(self) / pause() / resume() / stop()
    async def seek(self, utterance_index: int, offset_ms: int = 0)
    async def skip(self, unit: Literal["sentence","paragraph","heading","seconds"], count: int)
    async def set_speed(self, speed: float)  # applies ≤ 300 ms, no re-synthesis
    async def set_voice(self, voice_id: str)
    def replace_script(self, script: SpeechScript, keep_position: bool = True) -> None   # rule changes re-plan from playhead
    async def play_clip(self, audio: CachedAudio | Path, *, duck: bool = True) -> None   # replies/previews/recordings
    async def duck(self, on: bool) -> None               # bar opened/closed (07 § Ducking)
    def snapshot(self) -> api.schemas.PlaybackState    # publishes playback.state on every change
    # Emits playback.word with anchors mapped through Utterance.segments. Prefetch ~30 s, ≤ 2 min ahead.
    # Persists position via on_position(document_id, utterance_index, offset_ms) every 5 s and on pause/stop.
    async def close(self)
```

### C — Shell (owner: shell agent)
Owns `app/`. Builds against `engine/openapi.json` and `engine/ws-schema.json` (generated clients in
`app/src/shared/api/`, never hand-edited). See 03 § Shell, 09 § Interface, 10 § Repository layout.

The renderer must also run in a plain browser for UI tests: a bridge module uses `window.openSpeech`
(preload) when present and otherwise reads `?engine=<base>&token=<t>` from the URL.

### Coordinator — Surface and integration
Owns `service/`, `api/` (beyond the shared contracts), `cli/`, `agent/`, `mcp/`, `store/` repositories,
`engine/tests/contract/`, the e2e harness, and integration.

## Definition of done per workstream

- `uv run ruff check` and `uv run pytest tests/unit/<stream>` pass (shell: `pnpm typecheck`, `pnpm test`, `pnpm build`).
- Behavior matches the cited spec sections and FR acceptance criteria; deviations are listed in the final report.
- No vendor SDK imported outside `providers/`. No keys logged. No GPL/AGPL dependency in the default install.

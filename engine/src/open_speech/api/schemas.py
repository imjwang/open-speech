"""REST request/response models (04 § REST resources). The committed `openapi.json` is generated from these;
the shell's TypeScript client is generated from that. Changing a model here is an API change.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from open_speech.document.model import Block, DomAnchor
from open_speech.plan.script import RuleHit, Utterance
from open_speech.providers.base import Voice

Mode = Literal["classic", "ai_enhanced"]  # of a script: "ai_enhanced" when Claude cleaned it up
Money = str  # decimal string, never a float (04 § Conventions)


# ------------------------------------------------------------------ documents


class HtmlBlock(BaseModel):
    id: str
    type: str = "paragraph"
    text: str
    level: int | None = None
    dom_anchor: DomAnchor | None = None


class HtmlBlocksPayload(BaseModel):
    url: str
    title: str = ""
    blocks: list[HtmlBlock]


class CreateDocumentRequest(BaseModel):
    """JSON sources. Files go to `POST /documents/upload` (multipart)."""

    kind: Literal["url", "text", "image", "html_blocks", "clipboard", "file_path"]
    url: str | None = None
    text: str | None = None
    title: str | None = None
    image_base64: str | None = None
    media_type: str | None = None
    html_blocks: HtmlBlocksPayload | None = None
    path: str | None = None
    image_mode: Literal["auto", "transcribe", "describe"] = "auto"
    play: bool = False
    voice_id: str | None = None
    confirm_estimate: bool = False


class DocumentSettings(BaseModel):
    voice_id: str | None = None
    pinned: bool = False


class Position(BaseModel):
    utterance_index: int = 0
    offset_ms: int = 0


class DocumentOut(BaseModel):
    id: str
    kind: str
    title: str
    url: str | None = None
    site: str | None = None
    path: str | None = None
    page_count: int | None = None
    block_count: int = 0
    created_at: str
    last_opened_at: str
    progress: float = 0.0
    position: Position = Field(default_factory=Position)
    settings: DocumentSettings = Field(default_factory=DocumentSettings)
    flags: list[str] = Field(default_factory=list)
    language: str | None = None  # detected ("en", "es"); decides which voice can read it
    has_source_file: bool = False  # GET /documents/{id}/source serves the original (PDF/image) for the reader


class EstimateLine(BaseModel):
    item: str
    usd_estimate: Money


class Estimate(BaseModel):
    usd_estimate: Money
    breakdown: list[EstimateLine] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    requires_confirmation: bool = False


class CreateDocumentResponse(BaseModel):
    document: DocumentOut
    estimate: Estimate | None = None
    playing: bool = False


class DocumentList(BaseModel):
    items: list[DocumentOut]


class BlocksPage(BaseModel):
    items: list[Block]
    total: int
    offset: int
    limit: int


class ScriptOut(BaseModel):
    document_id: str
    mode: Mode
    utterances: list[Utterance]
    removed: list[RuleHit]
    warnings: list[str] = Field(default_factory=list)


class DocumentSettingsUpdate(BaseModel):
    voice_id: str | None = None
    pinned: bool | None = None


# ------------------------------------------------------------------ playback

PlaybackStatus = Literal["idle", "loading", "playing", "paused", "stopped", "error"]


class QueueItem(BaseModel):
    document_id: str
    title: str


class PlaybackState(BaseModel):
    state: PlaybackStatus = "idle"
    document_id: str | None = None
    document_title: str | None = None
    utterance_id: str | None = None
    utterance_index: int = 0
    utterance_count: int = 0
    offset_ms: int = 0
    speed: float = 1.0
    voice_id: str | None = None
    time_left_s: float | None = None
    page: int | None = None
    page_count: int | None = None
    heading: str | None = None
    progress: float = 0.0
    fallback_notice: str | None = None  # e.g. "Budget reached; reading with the local voice."
    error: str | None = None
    queue: list[QueueItem] = Field(default_factory=list)  # read next, in order, when this one ends


class PlayFrom(BaseModel):
    block_id: str | None = None
    char: int | None = None
    page: int | None = None
    utterance_id: str | None = None
    utterance_index: int | None = None


class PlayRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    document_id: str | None = None
    from_: PlayFrom | None = Field(default=None, alias="from")
    # The user has seen what this read is estimated to cost and wants it anyway (see `estimate_required`).
    confirm_estimate: bool = False


class QueueRequest(BaseModel):
    """Read several documents one after another. `now` replaces what is playing and starts the first;
    `next` goes right after the current document; `last` appends."""

    document_ids: list[str] = Field(min_length=1, max_length=50)
    when: Literal["now", "next", "last"] = "now"
    confirm_estimate: bool = False  # as on `PlayRequest`


class SkipRequest(BaseModel):
    unit: Literal["sentence", "paragraph", "heading", "seconds"]
    count: int = 1


class SpeedRequest(BaseModel):
    speed: float = Field(ge=0.5, le=5.0)


class VoiceRequest(BaseModel):
    voice_id: str
    scope: Literal["document", "default", "assistant"] = "document"


# ------------------------------------------------------------------ voices


class VoiceList(BaseModel):
    items: list[Voice]


class PreviewResponse(BaseModel):
    recording_id: str
    cached: bool
    usd_estimate: Money = "0"


# ------------------------------------------------------------------ cloned and designed voices (08 § Voice cloning)


class CloneConsent(BaseModel):
    """What the person confirmed ("This is my voice, or I have the speaker's permission …") and when."""

    text_version: str
    accepted_at: str


class CloneCheck(BaseModel):
    """The new voice said a test sentence; the on-device recognizer's word error rate grades it."""

    wer: float | None = None
    similarity: float | None = None  # speaker similarity: not measured yet
    grade: Literal["good", "fair", "poor"] | None = None  # None: not checked (the cloning model isn't downloaded)
    recording_id: str | None = None  # the test sentence, for replay (GET /recordings/{id})
    note: str | None = None


class CloneOut(BaseModel):
    slug: str
    voice_id: str  # "qwen3:clone:<slug>": use it like any voice id
    name: str
    language: str  # of the reference; the voice reads every language Qwen3-TTS speaks
    source: Literal["recording", "file", "design"]
    design_prompt: str | None = None
    ref_text: str  # exactly what the reference says
    ref_sha256: str
    processing_version: str
    duration_s: float
    created_at: str
    kept: bool
    consent: CloneConsent | None = None
    check: CloneCheck | None = None
    quality: dict[str, Any] = Field(default_factory=dict)  # sample rate, speech, SNR, clipping, level, window
    warnings: list[str] = Field(default_factory=list)


class CloneList(BaseModel):
    items: list[CloneOut]


class PassageOut(BaseModel):
    id: str
    language: str
    text: str


class PassageList(BaseModel):
    """Two-sentence passages to read for a clone, and the consent line to show with them."""

    items: list[PassageOut]
    consent: str
    consent_version: str


class CloneAccepted(BaseModel):
    """The clone is being made: follow `job_id` (`job.progress`, GET /jobs/{id}; its result has the check)."""

    job_id: str
    slug: str
    voice_id: str


class ClonePatch(BaseModel):
    """Rename, correct what the reference says (a new reference hash: cached audio is made again), or its language."""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    ref_text: str | None = Field(default=None, max_length=2000)
    language: str | None = Field(default=None, max_length=8)


class DesignRequest(BaseModel):
    """Make voices from a description (VoiceDesign 1.7B): three candidates, each a few seconds of a neutral passage."""

    description: str = Field(min_length=3, max_length=300)
    name: str | None = Field(default=None, max_length=64)
    language: str | None = Field(default=None, max_length=8)


class DesignAccepted(BaseModel):
    """Follow `job_id`; its result lists the candidates ({candidate_id, recording_id, wer})."""

    job_id: str


class KeepCandidate(BaseModel):
    name: str | None = Field(default=None, max_length=64)


class JobOut(BaseModel):
    job_id: str
    kind: str
    pct: float
    label: str
    state: Literal["running", "done", "failed"]
    error: dict[str, Any] | None = None
    result: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------------ rules


class RuleScope(BaseModel):
    sites: list[str] = Field(default_factory=list)
    types: list[str] = Field(default_factory=list)
    docs: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)


Tier = Literal["filter", "annotate", "transform"]


class RuleOut(BaseModel):
    id: str
    name: str
    description: str
    instruction: str
    scope: RuleScope
    tier: Tier
    enabled: bool
    created_by: str
    compiled: dict[str, Any] | None = None
    preset: bool = False
    path: str | None = None
    updated_at: str | None = None


class RuleList(BaseModel):
    items: list[RuleOut]
    conflicts: list[str] = Field(default_factory=list)


class DiffRemoval(BaseModel):
    block_id: str
    text: str
    rule_name: str
    whole_block: bool = False


class DiffReplacement(BaseModel):
    block_id: str
    old: str
    new: str
    rule_name: str


class DiffTransform(BaseModel):
    source_block_ids: list[str]
    script: list[str]


class SpeechDiff(BaseModel):
    document_id: str | None = None
    removals: list[DiffRemoval] = Field(default_factory=list)
    replacements: list[DiffReplacement] = Field(default_factory=list)
    transforms: list[DiffTransform] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    summary: str = ""  # spoken summary for eyes-free users (06 § Speech diff)


class RuleProposalRequest(BaseModel):
    instruction: str
    scope: RuleScope | None = None
    scope_hint: Literal["site", "document", "type", "global"] | None = None
    document_id: str | None = None


class RuleProposal(BaseModel):
    proposal_id: str  # an act_ id; confirm via POST /rules {proposal_id} or POST /actions/{id}/confirm
    rule: RuleOut
    diff: SpeechDiff
    conflicts: list[str] = Field(default_factory=list)
    estimate: Estimate | None = None
    summary: str


class SaveRuleRequest(BaseModel):
    proposal_id: str | None = None
    rule: RuleOut | None = None


class RulePatch(BaseModel):
    enabled: bool | None = None
    scope: RuleScope | None = None
    tier: Tier | None = None
    instruction: str | None = None
    description: str | None = None


class RuleFile(BaseModel):
    """One text file under rules/: the reading instructions (SPEECH.md), the pronunciation list, or a rule."""

    id: str  # "preferences" (SPEECH.md) | "lexicon" | a rule id
    kind: Literal["preferences", "lexicon", "rule"]
    name: str
    path: str
    description: str = ""
    enabled: bool = True
    scope: RuleScope | None = None
    tier: Tier | None = None
    updated_at: str | None = None


class RuleFileList(BaseModel):
    items: list[RuleFile]
    errors: list[str] = Field(default_factory=list)  # files that could not be read, with the reason


class RuleFileContent(RuleFile):
    text: str  # the whole file as it is on disk
    body: str  # what a person edits: the instruction (rules) or the whole text (preferences, lexicon)


class RuleFileWrite(BaseModel):
    """Save a rule file. Send `body` to change what a rule says (the engine works out how to apply it again),
    or `text` to replace the whole file including its header. `name` creates or renames a rule."""

    body: str | None = None
    text: str | None = None
    name: str | None = Field(default=None, max_length=64)
    enabled: bool | None = None
    scope: RuleScope | None = None


class LexiconEntry(BaseModel):
    term: str
    spoken: str
    scope: str = ""


class LexiconList(BaseModel):
    items: list[LexiconEntry]


# ------------------------------------------------------------------ assistant


class TurnContext(BaseModel):
    document_id: str | None = None
    selection: str | None = None
    # One id for everything said while the bar stays open, so "a bit faster" → "more" has context.
    conversation_id: str | None = None


class Attachment(BaseModel):
    """Something attached to a command in the composer: a file, a link, pasted text, or a document that was
    already imported (uploads)."""

    kind: Literal["file", "url", "text", "document"]
    path: str | None = None
    url: str | None = None
    text: str | None = None
    document_id: str | None = None
    name: str | None = None


class AttachmentOut(BaseModel):
    kind: str
    title: str
    document_id: str | None = None
    error: str | None = None


class TurnRequest(BaseModel):
    text: str = ""  # may be empty when the attachments are the request ("read these")
    # "button": a control in the interface asked (e.g. the Voices button). Handled like typed text, but a
    # client shows only the result, not a "you said" line.
    source: Literal["typed", "spoken", "button"] = "typed"
    session_id: str | None = None
    recording_id: str | None = None
    context: TurnContext = Field(default_factory=TurnContext)
    attachments: list[Attachment] = Field(default_factory=list, max_length=50)


class SayRequest(BaseModel):
    """Speak a short prompt in the assistant voice (onboarding, spoken hints). Returns when it has played."""

    text: str = Field(min_length=1, max_length=600)


class SpeechStopRequest(BaseModel):
    """Stop what the engine is saying (the WS `speech.stop`): one turn's reply, or anything when `turn_id` is
    omitted. It fades out in ~15 ms and pending synthesis is dropped."""

    turn_id: str | None = None


class TurnAccepted(BaseModel):
    turn_id: str


class ActionResult(BaseModel):
    action_id: str
    status: Literal["confirmed", "cancelled", "failed"]
    message: str


class ActionOut(BaseModel):
    action_id: str
    turn_id: str | None = None
    kind: str
    summary: str
    preview: dict[str, Any] | None = None
    status: str


# ------------------------------------------------------------------ usage


class UsageLine(BaseModel):
    key: str
    usd_estimate: Money
    calls: int


class UsageSummary(BaseModel):
    period: Literal["today", "week", "month"]
    group_by: Literal["provider", "op", "document"]
    total_usd: Money
    cache_savings_usd: Money
    budget_usd: Money
    budget_pct: float
    breakdown: list[UsageLine]
    estimated: bool = True
    spoken: str


class LedgerRow(BaseModel):
    id: int
    ts: str
    provider: str
    model: str
    op: str
    units: dict[str, Any]
    usd_estimate: Money
    pricing_version: str
    document_id: str | None = None
    turn_id: str | None = None
    cache_hit: bool
    local: bool


class LedgerPage(BaseModel):
    items: list[LedgerRow]
    total: int


class EstimateRequest(BaseModel):
    document_id: str | None = None
    text: str | None = None
    voice_id: str | None = None
    operation: Literal["read", "regenerate", "export", "keep_offline", "transform"] = "read"


class FreeTierState(BaseModel):
    """A provider allowance that costs nothing until it is used up (e.g. Google's free voice characters).
    Counted from this app's own use only, so it is an estimate."""

    provider: str
    label: str
    unit: Literal["chars"] = "chars"
    used: int
    limit: int
    resets_on: str  # ISO date of the next reset


class BudgetState(BaseModel):
    monthly_usd: Money
    warn_at: float
    hard_stop: bool
    spent_usd: Money
    pct: float
    free_tiers: list[FreeTierState] = Field(default_factory=list)


class BudgetUpdate(BaseModel):
    monthly_usd: Money | None = None
    warn_at: float | None = Field(default=None, ge=0, le=1)
    hard_stop: bool | None = None


# ------------------------------------------------------------------ capabilities, models, health, jobs


class SpeechInput(BaseModel):
    """Whether the bar can listen, and with what."""

    available: bool
    provider: str | None = None  # "local" | "openai" | "gemini" | "fixture"
    local: bool = False  # audio never leaves this Mac
    model: str | None = None
    # When unavailable: the one-sentence reason, and the local model that would fix it (if downloadable).
    reason: str | None = None
    install_model: str | None = None


class Capabilities(BaseModel):
    """What this build can do (GET /capabilities). Cloud features depend on the providers configured at
    build time; nothing here can be changed from inside the app."""

    speech_input: SpeechInput
    assistant: bool  # open-ended requests (Claude)
    ai_cleanup: bool  # AI Enhanced
    vision: bool  # image descriptions
    cloud_voices: list[str] = Field(default_factory=list)  # provider ids with cloud voices
    languages: list[str] = Field(default_factory=list)  # interface and speech languages
    language: str = "en"  # the one in use now ("auto" resolved)


class LocalModel(BaseModel):
    id: str
    provider: str
    name: str
    size_mb: int
    ram_gb: float
    license: str
    installed: bool
    fit: Literal["fits", "slow", "unsupported"]
    status: str = ""
    kind: Literal["tts", "stt"] = "tts"  # a voice, or a speech-to-text model
    # Weights variants (Qwen3-TTS): which family ("customvoice" preset voices, "base" cloning, "voicedesign"), size
    # ("0.6b"/"1.7b") and precision ("bf16".."4bit"); `selected` = the one the settings choose for that family.
    family: str | None = None
    size: str | None = None
    precision: str | None = None
    selected: bool = False


class LocalModelList(BaseModel):
    items: list[LocalModel]


class JobAccepted(BaseModel):
    job_id: str


class Health(BaseModel):
    ok: bool
    version: str
    uptime_s: float
    fixture_providers: bool = False


class AudioDiagnostics(BaseModel):
    """What the audio path counted since the engine started (GET /diagnostics/audio). Starvation is split by
    cause: `render_starvations` (the renderer fell behind: a bug or an overloaded Mac) versus `synthesis_waits`
    (the next sentence wasn't synthesized yet: a slow voice)."""

    device: str | None = None
    rate: int = 0  # output sample rate (the device's native rate when it could be opened at it)
    latency_ms: float = 0.0
    mode: str = ""  # "callback" | "blocking" | "null"
    resampler: str = ""
    uptime_s: float = 0.0
    callbacks: int = 0
    audio_s: float = 0.0  # audio handed to the device
    output_underflows: int = 0  # PortAudio status flags reported by the device
    output_overflows: int = 0
    priming_output: int = 0
    render_starvations: int = 0
    synthesis_waits: int = 0
    stream_starvations: int = 0
    ring_low_water_ms: float | None = None  # least reading audio buffered ahead while reading played
    max_pull_ms: float = 0.0  # slowest audio callback (the renderer's part)
    mean_pull_ms: float | None = None
    device_reopens: int = 0
    reading_buffered_ms: float = 0.0
    speed: float = 1.0
    output_running: bool = False
    speaking: int = 0  # replies / clips being said now


class Problem(BaseModel):
    type: str
    title: str
    status: int
    detail: str
    code: str

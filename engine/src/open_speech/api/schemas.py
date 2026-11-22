"""REST request/response models (04 § REST resources). The committed `openapi.json` is generated from these;
the shell's TypeScript client is generated from that. Changing a model here is an API change.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from open_speech.document.model import Block, DomAnchor
from open_speech.plan.script import RuleHit, Utterance
from open_speech.providers.base import Voice

Mode = Literal["classic", "ai_enhanced"]
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
    mode: Mode | None = None
    confirm_estimate: bool = False


class DocumentSettings(BaseModel):
    voice_id: str | None = None
    mode: Mode | None = None
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
    mode: Mode | None = None
    pinned: bool | None = None


# ------------------------------------------------------------------ playback

PlaybackStatus = Literal["idle", "loading", "playing", "paused", "stopped", "error"]


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


class PresetOut(BaseModel):
    id: str
    name: str
    enabled: bool
    classic: str
    ai_enhanced_adds: str


class ModeOverride(BaseModel):
    scope: RuleScope
    mode: Mode


class PresetsState(BaseModel):
    mode: Mode
    overrides: list[ModeOverride] = Field(default_factory=list)
    presets: list[PresetOut]


class PresetsUpdate(BaseModel):
    mode: Mode | None = None
    presets: dict[str, bool] = Field(default_factory=dict)  # preset name or id → enabled
    scope: RuleScope | None = None  # mode override scope; None = global


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


class TurnRequest(BaseModel):
    text: str
    source: Literal["typed", "spoken"] = "typed"
    session_id: str | None = None
    recording_id: str | None = None
    context: TurnContext = Field(default_factory=TurnContext)


class SayRequest(BaseModel):
    """Speak a short prompt in the assistant voice (onboarding, spoken hints). Returns when it has played."""

    text: str = Field(min_length=1, max_length=600)


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
    mode: Mode | None = None
    operation: Literal["read", "regenerate", "export", "keep_offline", "transform"] = "read"


class BudgetState(BaseModel):
    monthly_usd: Money
    warn_at: float
    hard_stop: bool
    spent_usd: Money
    pct: float


class BudgetUpdate(BaseModel):
    monthly_usd: Money | None = None
    warn_at: float | None = Field(default=None, ge=0, le=1)
    hard_stop: bool | None = None


# ------------------------------------------------------------------ keys, models, health, jobs

KeyStatus = Literal["configured", "missing", "invalid"]


class KeyIn(BaseModel):
    key: str


class KeyTestResult(BaseModel):
    ok: bool
    provider: str
    detail: str


class KeysState(BaseModel):
    anthropic: KeyStatus
    openai: KeyStatus
    gemini: KeyStatus


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


class LocalModelList(BaseModel):
    items: list[LocalModel]


class JobAccepted(BaseModel):
    job_id: str


class Health(BaseModel):
    ok: bool
    version: str
    uptime_s: float
    fixture_providers: bool = False


class Problem(BaseModel):
    type: str
    title: str
    status: int
    detail: str
    code: str

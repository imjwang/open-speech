"""WebSocket protocol (04 § WebSocket protocol). `ws-schema.json` is generated from these models and committed.

Every message is JSON `{type, ts, ...}`, except mic audio: binary frames of 16-bit LE PCM, 16 kHz mono,
sent between `mic.start` and `mic.stop`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

from open_speech.api.schemas import (
    Attachment,
    AttachmentOut,
    Capabilities,
    PlaybackState,
    SpeechDiff,
    TurnContext,
)
from open_speech.config import UserSettings
from open_speech.document.model import Anchor, Fidelity

PROTOCOL = 2


def _now() -> str:
    return datetime.now(UTC).isoformat()


class _Msg(BaseModel):
    ts: str = Field(default_factory=_now)
    id: str | None = None


# ------------------------------------------------------------------ client → engine


class MicStart(_Msg):
    type: Literal["mic.start"] = "mic.start"
    session_id: str
    mode: Literal["push_to_talk", "auto"] = "auto"
    context: TurnContext = Field(default_factory=TurnContext)
    # Publish the final transcript but don't send it to the assistant (onboarding answers, dictation).
    transcribe_only: bool = False


class MicStop(_Msg):
    type: Literal["mic.stop"] = "mic.stop"
    session_id: str
    submit: bool = True  # False = cancel (e.g. the user started typing; FR-BAR-3)
    # What is attached in the composer when the utterance ends ("read these", "summarize this").
    attachments: list[Attachment] = Field(default_factory=list, max_length=50)


class Command(_Msg):
    type: Literal["command"] = "command"
    text: str = ""
    source: Literal["typed", "spoken", "button"] = "typed"
    session_id: str | None = None
    context: TurnContext = Field(default_factory=TurnContext)
    attachments: list[Attachment] = Field(default_factory=list, max_length=50)


class Subscribe(_Msg):
    type: Literal["subscribe"] = "subscribe"
    topics: list[str] = Field(default_factory=list)


class BarState(_Msg):
    """The bar tells the engine it opened or closed, for ducking (07 § Ducking)."""

    type: Literal["bar.state"] = "bar.state"
    open: bool


ClientMessage = Annotated[MicStart | MicStop | Command | Subscribe | BarState, Field(discriminator="type")]
client_message_adapter: TypeAdapter[Any] = TypeAdapter(ClientMessage)


# ------------------------------------------------------------------ engine → client


class Hello(_Msg):
    type: Literal["hello"] = "hello"
    protocol: int = PROTOCOL
    version: str


class SttPartial(_Msg):
    type: Literal["stt.partial"] = "stt.partial"
    session_id: str
    text: str


class SttFinal(_Msg):
    type: Literal["stt.final"] = "stt.final"
    session_id: str
    text: str
    recording_id: str | None = None
    duration_s: float = 0.0


class AssistantTurn(_Msg):
    """A turn started (typed or spoken). Lets every client render the user row immediately."""

    type: Literal["assistant.turn"] = "assistant.turn"
    turn_id: str
    session_id: str | None = None
    text: str
    source: Literal["typed", "spoken", "button"]
    recording_id: str | None = None
    attachments: list[AttachmentOut] = Field(default_factory=list)


class AssistantDelta(_Msg):
    type: Literal["assistant.delta"] = "assistant.delta"
    turn_id: str
    text: str


class CardRow(BaseModel):
    id: str
    title: str
    subtitle: str | None = None
    value: str | None = None
    selected: bool = False
    data: dict[str, Any] = Field(default_factory=dict)


class AssistantCard(_Msg):
    """Structured rows for the bar (09 § Rows, not screens)."""

    type: Literal["assistant.card"] = "assistant.card"
    turn_id: str
    kind: Literal["voices", "rules", "usage", "recents", "models", "where", "presets", "settings", "queue", "info"]
    title: str
    rows: list[CardRow] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class AssistantAction(_Msg):
    type: Literal["assistant.action"] = "assistant.action"
    turn_id: str
    action_id: str
    kind: str
    summary: str
    spoken: str | None = None
    preview: SpeechDiff | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class AssistantActionResolved(_Msg):
    type: Literal["assistant.action_resolved"] = "assistant.action_resolved"
    action_id: str
    status: Literal["confirmed", "cancelled", "failed"]
    message: str


class AssistantDone(_Msg):
    type: Literal["assistant.done"] = "assistant.done"
    turn_id: str
    text: str = ""
    reply_recording_id: str | None = None
    handled_by: Literal["fast_path", "agent", "error"] = "agent"
    error: dict[str, Any] | None = None


class PlaybackStateMsg(_Msg):
    type: Literal["playback.state"] = "playback.state"
    playback: PlaybackState


class PlaybackWord(_Msg):
    type: Literal["playback.word"] = "playback.word"
    document_id: str
    utterance_id: str
    utterance_index: int
    anchor: Anchor
    sentence_anchors: list[Anchor] = Field(default_factory=list)  # the whole utterance, for sentence tint
    fidelity: Fidelity
    spoken_start: int = 0
    spoken_end: int = 0


class UsageAlert(_Msg):
    type: Literal["usage.alert"] = "usage.alert"
    level: Literal["warn", "stop"]
    spent: str
    budget: str
    message: str


class JobProgress(_Msg):
    type: Literal["job.progress"] = "job.progress"
    job_id: str
    pct: float
    label: str


class DocumentUpdated(_Msg):
    """The script for a document changed (rule saved, lexicon edit, regenerate) — reader refreshes."""

    type: Literal["document.updated"] = "document.updated"
    document_id: str
    reason: str


class RulesChanged(_Msg):
    """A rule file, the pronunciation list or the preferences changed (by voice, the editor or on disk)."""

    type: Literal["rules.changed"] = "rules.changed"
    reason: str = ""


class SettingsChanged(_Msg):
    """settings.toml changed (by voice, the API or a hand edit): theme, language and shortcuts apply at once."""

    type: Literal["settings.changed"] = "settings.changed"
    settings: UserSettings
    language: str  # the language now in use ("auto" resolved)


class CapabilitiesChanged(_Msg):
    """What the build can do changed while running, e.g. the local speech model finished installing."""

    type: Literal["capabilities.changed"] = "capabilities.changed"
    capabilities: Capabilities


class UiOpen(_Msg):
    """The assistant asks the shell to show something: the rules editor (optionally on one file), the reader
    for a document, or the bar."""

    type: Literal["ui.open"] = "ui.open"
    target: Literal["rules", "reader", "bar"]
    item: str | None = None


class ErrorMsg(_Msg):
    type: Literal["error"] = "error"
    problem: dict[str, Any]


ServerMessage = Annotated[
    Hello
    | SttPartial
    | SttFinal
    | AssistantTurn
    | AssistantDelta
    | AssistantCard
    | AssistantAction
    | AssistantActionResolved
    | AssistantDone
    | PlaybackStateMsg
    | PlaybackWord
    | UsageAlert
    | JobProgress
    | DocumentUpdated
    | RulesChanged
    | SettingsChanged
    | CapabilitiesChanged
    | UiOpen
    | ErrorMsg,
    Field(discriminator="type"),
]
server_message_adapter: TypeAdapter[Any] = TypeAdapter(ServerMessage)


def ws_json_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Open Speech WebSocket protocol",
        "protocol": PROTOCOL,
        "binary_frames": "Mic audio: 16-bit LE PCM, 16 kHz mono, between mic.start and mic.stop",
        "client": client_message_adapter.json_schema(),
        "server": server_message_adapter.json_schema(),
    }


if __name__ == "__main__":
    print(json.dumps(ws_json_schema(), indent=2))

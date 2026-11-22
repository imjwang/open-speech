"""WebSocket protocol (04 § WebSocket protocol). `ws-schema.json` is generated from these models and committed.

Every message is JSON `{type, ts, ...}`, except mic audio: binary frames of 16-bit LE PCM, 16 kHz mono,
sent between `mic.start` and `mic.stop`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

from open_speech.api.schemas import PlaybackState, SpeechDiff, TurnContext
from open_speech.document.model import Anchor, Fidelity

PROTOCOL = 1


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


class MicStop(_Msg):
    type: Literal["mic.stop"] = "mic.stop"
    session_id: str
    submit: bool = True  # False = cancel (e.g. the user started typing; FR-BAR-3)


class Command(_Msg):
    type: Literal["command"] = "command"
    text: str
    source: Literal["typed", "spoken"] = "typed"
    session_id: str | None = None
    context: TurnContext = Field(default_factory=TurnContext)


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
    source: Literal["typed", "spoken"]
    recording_id: str | None = None


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
    kind: Literal["voices", "rules", "usage", "recents", "models", "where", "keys", "presets", "info"]
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

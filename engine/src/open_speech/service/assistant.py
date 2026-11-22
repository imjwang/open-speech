"""The one assistant behind the bar (07): built-in commands first, Claude second; a card for every change
worth confirming.

A turn is: what the user said or typed, plus whatever is attached in the composer (files, links, text). The
attachments are imported first, so by the time anything decides what to do they are ordinary documents:
"read these" queues them, anything else can refer to them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from open_speech.agent.intents import parse
from open_speech.api import schemas as s
from open_speech.api.ws_schema import (
    AssistantAction,
    AssistantActionResolved,
    AssistantDelta,
    AssistantDone,
    AssistantTurn,
)
from open_speech.errors import EngineError, NotFound
from open_speech.i18n import current_language, resolve_language, say, t, use_language
from open_speech.ids import new_id
from open_speech.service.commands import Commands, Reply, ToAgent, Turn, voice_display
from open_speech.store.pages import host_of

if TYPE_CHECKING:
    from open_speech.service.engine import Engine

log = logging.getLogger("open_speech.assistant")

HandledBy = Literal["fast_path", "agent", "error"]

__all__ = ["AssistantService", "Conversation", "Reply", "voice_display"]


@dataclass
class Conversation:
    """Everything said while the bar stayed open: what lets "a bit faster" be followed by "more"."""

    history: list[dict[str, Any]] = field(default_factory=list)
    last_reply_recording: str | None = None
    # Hosts the user named themselves (typed, said or attached): the links the assistant may always follow.
    hosts: set[str] = field(default_factory=set)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class AssistantService:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self.commands = Commands(engine, self)
        self._conversations: dict[str, Conversation] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ turns

    async def submit(self, req: s.TurnRequest) -> s.TurnAccepted:
        text = req.text.strip()
        if not text and not req.attachments:
            raise EngineError("invalid_request", t("turn.empty"))
        turn_id = new_id("turn")
        session_id = req.session_id or "default"
        attached = [self._describe(a) for a in req.attachments]
        self.e.db.execute(
            "INSERT INTO turns(id, session_id, user_text, source, user_recording_id, attachments_json, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                turn_id,
                session_id,
                text,
                req.source,
                req.recording_id,
                json.dumps([a.model_dump(exclude_none=True) for a in attached]),
                _now(),
            ),
        )
        self.e.events.publish(
            AssistantTurn(
                turn_id=turn_id,
                session_id=session_id,
                text=_without_attachments(text, req.attachments),
                source=req.source,
                recording_id=req.recording_id,
                attachments=attached,
            )
        )
        task = asyncio.create_task(self._run(turn_id, session_id, req))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return s.TurnAccepted(turn_id=turn_id)

    async def wait_idle(self, timeout: float = 30.0) -> None:
        """Tests and the CLI wait for in-flight turns."""
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=timeout)

    def conversation(self, key: str) -> Conversation:
        if key not in self._conversations:
            if len(self._conversations) > 32:  # the bar opens many times a day; old conversations are done
                for old in list(self._conversations)[:16]:
                    del self._conversations[old]
            self._conversations[key] = Conversation()
        return self._conversations[key]

    def last_reply_recording(self, conversation: str) -> str | None:
        conv = self._conversations.get(conversation)
        return conv.last_reply_recording if conv else None

    async def _run(self, turn_id: str, session_id: str, req: s.TurnRequest) -> None:
        language = use_language(resolve_language(self.e.settings.get().language))
        conversation = req.context.conversation_id or session_id
        conv = self.conversation(conversation)
        handled: HandledBy = "fast_path"
        try:
            reply, handled = await self._answer(turn_id, conversation, conv, req, language)
        except EngineError as e:
            handled = "error"
            reply = Reply(text=e.detail, error=e.problem())
        except Exception:  # noqa: BLE001
            log.exception("turn failed")
            handled = "error"
            reply = Reply(text=t("turn.failed"), error={"code": "internal"})

        if handled == "fast_path" and req.source != "button":
            # The assistant's picture of the conversation includes what was settled without it ("yes" → "Saved."):
            # left out, it once proposed again a rule the user had just confirmed.
            said = _without_attachments(req.text.strip(), req.attachments) or "[attachments]"
            answer = reply.text[:500] or "(done)"
            conv.history.extend([{"role": "user", "content": said[:500]}, {"role": "assistant", "content": answer}])
        if reply.card is not None:
            self.e.events.publish(reply.card)
        if reply.text and handled != "agent":
            self.e.events.publish(AssistantDelta(turn_id=turn_id, text=reply.text))

        audio = None
        rec_id = None
        if reply.text and reply.speak and req.source == "spoken":
            try:
                audio, rec_id = await self._synth_reply(reply.text, turn_id)
            except EngineError as e:
                log.info("reply not spoken: %s", e.detail)
        if rec_id:
            conv.last_reply_recording = rec_id
        self.e.db.execute(
            "UPDATE turns SET reply_text=?, reply_recording_id=?, handled_by=? WHERE id=?",
            (reply.text, rec_id, handled, turn_id),
        )
        self.e.events.publish(
            AssistantDone(
                turn_id=turn_id, text=reply.text, reply_recording_id=rec_id, handled_by=handled, error=reply.error
            )
        )
        if audio is not None:
            await self.e.transport.play_clip(audio, duck=True)

    async def _answer(
        self, turn_id: str, conversation: str, conv: Conversation, req: s.TurnRequest, language: str
    ) -> tuple[Reply, HandledBy]:
        text = req.text.strip()
        conv.hosts.update(_hosts_in(text))
        conv.hosts.update(host_of(a.url) for a in req.attachments if a.url)
        documents, failures = await self._import(req.attachments)
        pending = self.latest_pending()
        turn = Turn(id=turn_id, request=req, pending=pending, documents=documents, conversation=conversation)

        # Badges read as their names inside the sentence ("report.pdf and notes.md, read them"); the built-in
        # commands are matched on the words around them.
        words = _without_attachments(text, req.attachments)
        if not words:  # attachments alone mean "read these"
            if not documents:
                raise EngineError("invalid_request", failures[0] if failures else t("turn.empty"))
            reply = await self.commands.read_documents(documents, turn_id=turn_id)
            return _with_failures(reply, failures), "fast_path"

        intent = parse(
            words,
            has_pending_action=pending is not None,
            has_attachments=bool(documents),
            language=language,
            spoken=req.source == "spoken",
        )
        if intent is not None:
            try:
                reply = await self.commands.run(intent, turn)
                # Something attached that could not be opened is said, not silently left out.
                return (_with_failures(reply, failures) if intent.kind == "read_attachments" else reply), "fast_path"
            except ToAgent:
                pass  # the grammar matched but couldn't resolve it locally; Claude can
        from open_speech.agent.core import run_agent_turn

        return await run_agent_turn(self.e, turn=turn, conversation=conv, failures=failures), "agent"

    # ------------------------------------------------------------------ attachments

    def _describe(self, a: s.Attachment) -> s.AttachmentOut:
        """How an attachment reads in the conversation before it has been imported."""
        if a.kind == "file":
            title = a.name or Path(a.path or "").name
        elif a.kind == "url":
            title = a.name or host_of(a.url or "") or (a.url or "")
        elif a.kind == "document":
            try:
                title = a.name or self.e.documents.title(a.document_id or "")
            except EngineError:
                title = a.name or ""
        else:
            first = (a.text or "").strip().splitlines()[0] if (a.text or "").strip() else ""
            title = a.name or (first if len(first) <= 60 else first[:57] + "…")
        return s.AttachmentOut(kind=a.kind, title=title, document_id=a.document_id)

    async def _import(self, attachments: list[s.Attachment]) -> tuple[list[str], list[str]]:
        """Turn each attachment into a document. Returns (document ids in order, reasons for any that failed)."""
        ids: list[str] = []
        failures: list[str] = []
        for a in attachments:
            try:
                if a.kind == "document" and a.document_id:
                    self.e.documents.title(a.document_id)  # raises NotFound for an unknown id
                    ids.append(a.document_id)
                    continue
                if a.kind == "file" and a.path:
                    request = s.CreateDocumentRequest(kind="file_path", path=a.path)
                elif a.kind == "url" and a.url:
                    request = s.CreateDocumentRequest(kind="url", url=a.url)
                elif a.kind == "text" and a.text:
                    request = s.CreateDocumentRequest(kind="text", text=a.text, title=a.name)
                else:
                    continue
                ids.append((await self.e.documents.create(request)).document.id)
            except EngineError as e:
                failures.append(t("reply.attachment_skipped", name=self._describe(a).title, reason=e.detail))
        return list(dict.fromkeys(ids)), failures

    # ------------------------------------------------------------------ speaking

    async def say(self, text: str) -> None:
        """Speak a UI prompt in the assistant voice and wait for it to finish (09 § Onboarding). Reading
        pauses for it and resumes. Nothing is stored as a recording."""
        res = await self.e.synth.synth(text, self.e.voices.assistant_voice(), op="tts")
        await self.e.transport.play_clip(res.audio, duck=True)

    async def _synth_reply(self, text: str, turn_id: str) -> tuple[Any, str | None]:
        voice = self.e.voices.for_language(self.e.voices.assistant_voice(), current_language())
        res = await self.e.synth.synth(text, voice, turn_id=turn_id, op="tts")
        rec_id = None
        if self.e.recordings.keep_recordings():
            rec_id = self.e.recordings.register(
                res.audio.path, kind="reply", duration_s=res.audio.duration_s, turn_id=turn_id
            )
        return res.audio, rec_id

    # ------------------------------------------------------------------ actions (confirmation cards)

    def create_action(
        self,
        *,
        kind: str,
        summary: str,
        payload: dict[str, Any],
        spoken: str | None = None,
        preview: s.SpeechDiff | None = None,
        turn_id: str | None = None,
    ) -> str:
        # One pending card at a time: a new proposal supersedes the previous one (the bar shows one card).
        self.e.db.execute("UPDATE actions SET status='cancelled', resolved_at=? WHERE status='pending'", (_now(),))
        action_id = new_id("act")
        self.e.db.execute(
            "INSERT INTO actions(id, turn_id, kind, summary, payload_json, preview_json, status, created_at)"
            " VALUES(?,?,?,?,?,?, 'pending', ?)",
            (
                action_id,
                turn_id,
                kind,
                summary,
                json.dumps(payload),
                preview.model_dump_json() if preview else None,
                _now(),
            ),
        )
        self.e.events.publish(
            AssistantAction(
                turn_id=turn_id or "",
                action_id=action_id,
                kind=kind,
                summary=summary,
                spoken=spoken,
                preview=preview,
                details={k: v for k, v in payload.items() if k != "rule"},
            )
        )
        return action_id

    def _action_out(self, row: Any) -> s.ActionOut:
        return s.ActionOut(
            action_id=row["id"],
            turn_id=row["turn_id"],
            kind=row["kind"],
            summary=row["summary"],
            preview=json.loads(row["preview_json"]) if row["preview_json"] else None,
            status=row["status"],
        )

    def latest_pending(self) -> s.ActionOut | None:
        row = self.e.db.one("SELECT * FROM actions WHERE status='pending' ORDER BY created_at DESC LIMIT 1")
        return self._action_out(row) if row else None

    def pending_actions(self) -> list[s.ActionOut]:
        return [
            self._action_out(r)
            for r in self.e.db.query("SELECT * FROM actions WHERE status='pending' ORDER BY created_at DESC")
        ]

    def mark_action(self, action_id: str, status: str, message: str) -> None:
        self.e.db.execute("UPDATE actions SET status=?, resolved_at=? WHERE id=?", (status, _now(), action_id))
        self.e.events.publish(AssistantActionResolved(action_id=action_id, status=status, message=message))  # type: ignore[arg-type]

    async def confirm(self, action_id: str) -> s.ActionResult:
        row = self.e.db.one("SELECT * FROM actions WHERE id=?", (action_id,))
        if row is None:
            raise NotFound(t("what.action"))
        if row["status"] != "pending":
            return s.ActionResult(action_id=action_id, status="failed", message=t("action.already"))
        payload = json.loads(row["payload_json"])
        try:
            message = await self._execute(row["kind"], payload)
        except EngineError as e:
            self.mark_action(action_id, "failed", e.detail)
            return s.ActionResult(action_id=action_id, status="failed", message=e.detail)
        self.mark_action(action_id, "confirmed", message)
        return s.ActionResult(action_id=action_id, status="confirmed", message=message)

    async def cancel(self, action_id: str) -> s.ActionResult:
        row = self.e.db.one("SELECT status FROM actions WHERE id=?", (action_id,))
        if row is None:
            raise NotFound(t("what.action"))
        if row["status"] != "pending":
            return s.ActionResult(action_id=action_id, status="failed", message=t("action.already"))
        self.mark_action(action_id, "cancelled", t("done.cancelled"))
        return s.ActionResult(action_id=action_id, status="cancelled", message=t("done.cancelled"))

    async def _execute(self, kind: str, p: dict[str, Any]) -> str:  # noqa: C901 — one branch per confirmable change
        from open_speech.rules.model import Rule

        e = self.e
        match kind:
            case "save_rule":
                await e.rules.save_rule(Rule.model_validate(p["rule"]))
                return t("done.rule_saved")
            case "write_rule":
                await e.rules.write_file(p.get("file_id"), s.RuleFileWrite.model_validate(p["write"]))
                return t("done.rule_saved")
            case "set_preset":
                await e.rules.set_preset(str(p["preset"]), bool(p["enabled"]))
                return t("done.generic")
            case "set_mode":
                scope = s.RuleScope.model_validate(p["scope"]) if p.get("scope") else None
                await e.rules.set_mode(str(p["mode"]), scope)
                return t(f"done.mode.{p['mode']}")
            case "add_pronunciation":
                await e.rules.add_lexicon(s.LexiconEntry(term=p["term"], spoken=p["spoken"], scope=p.get("scope", "")))
                return t("done.saved")
            case "remove_pronunciation":
                await e.rules.delete_lexicon(str(p["term"]))
                return t("done.removed")
            case "set_voice":
                await e.playback.set_voice(s.VoiceRequest(voice_id=p["voice_id"], scope=p.get("scope", "default")))
                return t("done.voice")
            case "set_budget":
                st = await e.usage.set_budget(s.BudgetUpdate(**p))
                return t("done.budget", amount=say.money(Decimal(st.monthly_usd)))
            case "update_setting":
                await e.settings.set_key(str(p["key"]), p["value"])
                return t("done.setting")
            case "install_model":
                await e.models.install(str(p["model_id"]))
                return t("done.installing")
            case "clear_cache":
                e.audio_cache.clear()
                return t("done.cache_cleared")
            case "delete_rule":
                await e.rules.delete(str(p["rule_id"]))
                return t("done.rule_deleted")
            case "read_documents":
                request = s.QueueRequest(
                    document_ids=list(p["document_ids"]), when=p.get("when", "now"), confirm_estimate=True
                )
                await e.playback.enqueue(request)
                return t("done.reading")
            case "update_rule":
                await e.rules.patch(str(p["rule_id"]), s.RulePatch(**p.get("changes", {})))
                return t("done.rule_updated")
        raise EngineError("invalid_request", f"Unknown action {kind}.")


_LEADING_GLUE = re.compile(r"^(?:[\s,;:.]|\b(?:and|then|also|y|luego|también)\b)+", re.I)


def _without_attachments(text: str, attachments: list[s.Attachment]) -> str:
    """The typed or spoken words of a command, without the stand-ins for its badges."""
    if not attachments:
        return text
    for a in attachments:
        for token in (a.url, a.name, Path(a.path).name if a.path else None):
            if token:
                text = text.replace(token, " ")
    return _LEADING_GLUE.sub("", re.sub(r"\s+", " ", text)).strip()


_HOST = re.compile(r"(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})(?::\d+)?(?:/\S*)?", re.I)


def _with_failures(reply: Reply, failures: list[str]) -> Reply:
    if failures:
        reply.text = " ".join([reply.text, *failures]).strip()
    return reply


def _hosts_in(text: str) -> set[str]:
    """Hosts the user wrote or said ("read example.org/post")."""
    return {m.group(1).lower() for m in _HOST.finditer(text)}

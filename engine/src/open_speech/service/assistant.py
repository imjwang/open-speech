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
import unicodedata
from collections.abc import Awaitable, Callable
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
    AssistantCard,
    AssistantDelta,
    AssistantDone,
    AssistantTurn,
)
from open_speech.errors import EngineError, NotFound
from open_speech.i18n import current_language, resolve_language, say, t, use_language
from open_speech.ids import new_id
from open_speech.providers.pcm import read_audio_file
from open_speech.service.clones import ACTION_KINDS as CLONE_ACTIONS
from open_speech.service.clones import is_audio_path
from open_speech.service.commands import Commands, Reply, ToAgent, Turn, voice_display
from open_speech.store.pages import host_of

if TYPE_CHECKING:
    from open_speech.service.engine import Engine
    from open_speech.service.reply_speech import ReplyStream

log = logging.getLogger("open_speech.assistant")

HandledBy = Literal["fast_path", "agent", "error"]

__all__ = ["AssistantService", "Conversation", "Reply", "voice_display"]


@dataclass
class Conversation:
    """What the app remembers while the bar stays open. Not what was said: every assistant turn stands alone (the
    agent gets no history), and a card waiting for a yes is in the database, not here."""

    last_reply_recording: str | None = None
    # Hosts the user named themselves (typed, said or attached): the links the assistant may always follow.
    hosts: set[str] = field(default_factory=set)
    # The voices on the last voices card ({id: name}): saying one of those names plays a sample of it.
    shown_voices: dict[str, str] = field(default_factory=dict)


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
        """Answer a turn, aloud. The reply stream opens before anything is decided, so the agent's words are
        spoken as they are written; the turn's text is complete (`assistant.done`) before its audio is."""
        language = use_language(resolve_language(self.e.settings.get().language))
        conversation = req.context.conversation_id or session_id
        conv = self.conversation(conversation)
        stream = await self._open_reply(turn_id, language, conv)
        touched = self.e.playback.touched
        handled: HandledBy = "fast_path"
        try:
            reply, handled = await self._answer(turn_id, conversation, conv, req, language, stream)
        except EngineError as e:
            handled = "error"
            reply = Reply(text=e.detail, error=e.problem())
        except Exception:  # noqa: BLE001
            log.exception("turn failed")
            handled = "error"
            reply = Reply(text=t("turn.failed"), error={"code": "internal"})

        await self._finish(turn_id, conv, stream, reply, handled, touched)

    async def _finish(
        self,
        turn_id: str,
        conv: Conversation,
        stream: ReplyStream,
        reply: Reply,
        handled: HandledBy,
        touched: int,
    ) -> None:
        """What every turn does with its answer: show it, say it, and say it is complete."""
        if reply.card is not None:
            self.remember_card(conv, reply.card)
            self.e.events.publish(reply.card)
        if reply.text and handled != "agent":
            self.e.events.publish(AssistantDelta(turn_id=turn_id, text=reply.text))

        # What is heard. Reading audio is the answer when the command's effect is reading and reading will
        # resume when the conversation ends ("faster" while reading, "read my clipboard"); everything else is
        # said, typed and button turns included. An agent turn that started or changed reading is spoken and
        # then gives way to it.
        reading = self.e.playback.reading_intended()
        if handled == "agent":
            audible = reading and self.e.playback.touched != touched
        else:
            audible = reply.audible_effect and reading
        if handled != "agent":
            if reply.parts is not None:
                for part in reply.parts:
                    if isinstance(part, str):
                        stream.feed(part)
                    else:
                        pcm, sr = part
                        stream.insert_audio(pcm, int(sr))
            elif reply.audio is not None:
                await self._insert_audio(stream, reply.audio, reply.audio_recording)
            elif reply.text and not audible:
                stream.feed(reply.text)
        speaks = stream.will_speak()
        self.e.db.execute(
            "UPDATE turns SET reply_text=?, handled_by=? WHERE id=?", (reply.text, handled, turn_id)
        )
        self.e.events.publish(
            AssistantDone(
                turn_id=turn_id,
                text=reply.text,
                handled_by=handled,
                error=reply.error,
                speaks=speaks,
                audible_effect=audible,
            )
        )
        stream.finish()

    def task_turn(
        self,
        *,
        session_id: str | None,
        conversation_id: str | None,
        work: Callable[[Turn], Awaitable[Reply]],
    ) -> str:
        """A turn the engine starts itself, for work the user set going outside a command (a recording uploaded to
        become a voice): it appears in the conversation of `session_id` like the answer to a button press, can
        speak while it works (`turn.stream`), and its reply is `work`'s. Returns the turn id."""
        turn_id = new_id("turn")
        session = session_id or "default"
        req = s.TurnRequest(text="", source="button", session_id=session,
                            context=s.TurnContext(conversation_id=conversation_id))
        self.e.db.execute(
            "INSERT INTO turns(id, session_id, user_text, source, user_recording_id, attachments_json, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (turn_id, session, "", "button", None, "[]", _now()),
        )
        self.e.events.publish(AssistantTurn(turn_id=turn_id, session_id=session, text="", source="button"))

        async def run() -> None:
            language = use_language(resolve_language(self.e.settings.get().language))
            conv = self.conversation(conversation_id or session)
            stream = await self._open_reply(turn_id, language, conv)
            touched = self.e.playback.touched
            turn = Turn(id=turn_id, request=req, pending=self.latest_pending(), conversation=conversation_id or session,
                        stream=stream)
            handled: HandledBy = "fast_path"
            try:
                reply = await work(turn)
            except EngineError as e:
                handled = "error"
                reply = Reply(text=e.detail, error=e.problem())
            except Exception:  # noqa: BLE001
                log.exception("task turn failed")
                handled = "error"
                reply = Reply(text=t("turn.failed"), error={"code": "internal"})
            await self._finish(turn_id, conv, stream, reply, handled, touched)

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return turn_id

    async def _open_reply(self, turn_id: str, language: str, conv: Conversation) -> ReplyStream:
        """The turn's reply stream, in the assistant voice for `language`. When "auto" has to use the local voice
        for a runtime reason, the reason is said first, once (`Transport.announce`)."""
        await self.e.voices.settle_auto()
        voice, reason = self.e.voices.reply_voice(language)
        voice = self.e.voices.for_language(voice, language)

        def ended(stream: ReplyStream, reason: str) -> None:
            if stream.recording_id:
                conv.last_reply_recording = stream.recording_id
                self.e.db.execute(
                    "UPDATE turns SET reply_recording_id=? WHERE id=?", (stream.recording_id, turn_id)
                )
            elif stream.first_audio_at is not None:
                conv.last_reply_recording = None  # cut off before the end: "say that again" re-says its text

        stream = self.e.reply_speech.open(turn_id, voice, language, kind="reply", on_end=ended)
        if reason:
            self.e.transport.announce(self.e.voices.notice_text(reason), reason)
        return stream

    async def _insert_audio(self, stream: ReplyStream, path: Path, recording_id: str | None) -> None:
        try:
            pcm, sr = await asyncio.to_thread(read_audio_file, path)
        except Exception:  # noqa: BLE001 — a missing or unreadable file: nothing to play
            log.warning("could not read %s for the reply", path)
            return
        stream.insert_audio(pcm, sr, recording_id=recording_id)

    def remember_card(self, conv: Conversation, card: AssistantCard) -> None:
        """A voices card on screen: its names become something the user can say to hear a voice."""
        if card.kind == "voices":
            conv.shown_voices = {r.id: r.title for r in card.rows if not r.data.get("command")}

    async def _answer(
        self,
        turn_id: str,
        conversation: str,
        conv: Conversation,
        req: s.TurnRequest,
        language: str,
        stream: ReplyStream | None = None,
    ) -> tuple[Reply, HandledBy]:
        text = req.text.strip()
        conv.hosts.update(_hosts_in(text))
        conv.hosts.update(host_of(a.url) for a in req.attachments if a.url)
        # An audio file is a voice to clone, not a document to read: it isn't imported.
        audio = [a.path for a in req.attachments if a.kind == "file" and a.path and is_audio_path(a.path)]
        documents, failures = await self._import(
            [a for a in req.attachments if not (a.kind == "file" and a.path and is_audio_path(a.path))]
        )
        pending = self.latest_pending()
        turn = Turn(id=turn_id, request=req, pending=pending, documents=documents, conversation=conversation,
                    audio=audio, stream=stream)

        # Badges read as their names inside the sentence ("report.pdf and notes.md, read them"); the built-in
        # commands are matched on the words around them.
        words = _without_attachments(text, req.attachments)
        if not words and audio and not documents:  # an audio file on its own: make a voice from it
            return self.e.clones.start_file_clone(turn, audio), "fast_path"
        if not words:  # attachments alone mean "read these"
            if not documents:
                raise EngineError("invalid_request", failures[0] if failures else t("turn.empty"))
            reply = await self.commands.read_documents(documents, turn_id=turn_id)
            return _with_failures(reply, failures), "fast_path"

        answered = await self._answer_clone_card(turn, words)
        if answered is not None:  # "ready" on the passage card, "the second one" for designed voices
            return answered, "fast_path"
        named = _named_voice(conv.shown_voices, words)
        if named is not None:  # "Kore": a name from the voices card on screen → hear it
            return await self.commands.preview_voice(named, turn), "fast_path"
        intent = parse(
            words,
            has_pending_action=pending is not None,
            has_attachments=bool(documents) or bool(audio),
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

        on_text = stream.feed if stream is not None else None
        return await run_agent_turn(self.e, turn=turn, conversation=conv, failures=failures, on_text=on_text), "agent"

    async def _answer_clone_card(self, turn: Turn, words: str) -> Reply | None:
        """Words that answer a cloning card and nothing else: "ready" / "start" on the passage card, "one" / "the
        second one" when designed voices wait to be chosen."""
        from open_speech.service.clones import CLONE_RECORD, KEEP_DESIGN, chosen_index, is_ready

        pending = turn.pending
        if pending is None or not words:
            return None
        if pending.kind == CLONE_RECORD and is_ready(words):
            return await self.e.clones.confirm(pending, turn)
        if pending.kind == KEEP_DESIGN:
            index = chosen_index(words)
            if index is not None:
                return await self.e.clones.choose(pending, index)
        return None

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
        """Speak a UI prompt in the assistant voice and wait until it has been heard (09 § First run). Reading
        pauses for it and resumes. Nothing is stored as a recording. It supersedes what was being said."""
        language = current_language()
        await self.e.voices.settle_auto()
        voice, reason = self.e.voices.reply_voice(language)
        stream = self.e.reply_speech.open(None, self.e.voices.for_language(voice, language), language, kind="say")
        if reason:
            self.e.transport.announce(self.e.voices.notice_text(reason), reason)
        stream.feed(text)
        stream.finish()
        await stream.wait()

    def _reply_voice(self, language: str) -> str:
        """The assistant voice for a reply in `language`; when "auto" has to use the local voice for a runtime
        reason (free characters used up, budget), the reason is said once (`Transport.announce`)."""
        voice, reason = self.e.voices.reply_voice(language)
        if reason:
            self.e.transport.announce(self.e.voices.notice_text(reason), reason)
        return self.e.voices.for_language(voice, language)

    def _announce(self, res: Any) -> None:
        if getattr(res, "notice", None):
            self.e.transport.announce(res.notice, getattr(res, "notice_reason", None))

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
        if row["kind"] in CLONE_ACTIONS:  # cloning cards: their service answers (some run for a while)
            reply = await self.e.clones.confirm(self._action_out(row), None)
            now = self.e.db.one("SELECT status FROM actions WHERE id=?", (action_id,))
            result: Literal["confirmed", "cancelled", "failed"] = (
                "confirmed" if now is not None and now["status"] == "confirmed" else "failed"
            )
            return s.ActionResult(action_id=action_id, status=result, message=reply.text)
        payload = json.loads(row["payload_json"])
        try:
            message = await self._execute(row["kind"], payload)
        except EngineError as e:
            self.mark_action(action_id, "failed", e.detail)
            return s.ActionResult(action_id=action_id, status="failed", message=e.detail)
        self.mark_action(action_id, "confirmed", message)
        return s.ActionResult(action_id=action_id, status="confirmed", message=message)

    async def cancel(self, action_id: str) -> s.ActionResult:
        row = self.e.db.one("SELECT * FROM actions WHERE id=?", (action_id,))
        if row is None:
            raise NotFound(t("what.action"))
        if row["status"] != "pending":
            return s.ActionResult(action_id=action_id, status="failed", message=t("action.already"))
        if row["kind"] in CLONE_ACTIONS:
            reply = await self.e.clones.cancel(self._action_out(row))
            return s.ActionResult(action_id=action_id, status="cancelled", message=reply.text)
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
            case "beyond_free_tier":
                # Until this billing month ends (FreeTierGuard reads "pay" only for the stored month).
                allowance = next(iter(e.pricing.free_allowances()), None)
                month = e.ledger.free_period(allowance).key if allowance is not None else p["month"]
                await e.settings.patch({"playback": {"beyond_free_tier": "pay", "beyond_free_tier_month": month}})
                doc = e.playback.current_document_id()
                if doc is not None:  # reading switched to the local voice: back to the Google voice from here
                    await e.transport.set_voice(e.documents.voice_for(doc))
                return t("done.beyond_free_tier")
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


_HEAR = re.compile(
    r"^(?:(?:play|hear|preview|listen to|let me hear|escucha(?:r)?|reproduce|quiero escuchar)\s+)?"
    r"(?:(?:the|la|el|a)\s+)?(?:voice\s+|voz\s+(?:de\s+)?)?(.+?)(?:\s+(?:voice|please|por favor))?$"
)


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def _named_voice(shown: dict[str, str], words: str) -> str | None:
    """The voice whose name was said ("Kore", "play Aoede", "escuchar Dora") among those on the last voices card."""
    if not shown or not words or len(words) > 60:
        return None
    text = re.sub(r"[¿?¡!.,\"“”']+", " ", _fold(words)).strip()
    m = _HEAR.match(re.sub(r"\s+", " ", text))
    if m is None:
        return None
    name = m.group(1).strip()
    for voice_id, title in shown.items():
        if _fold(title) == name:
            return voice_id
    return None

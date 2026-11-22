"""The one assistant behind the bar (07): fast path first, Claude second; action cards for every mutation."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import numpy as np
import soundfile as sf

from open_speech.agent.intents import Intent, parse
from open_speech.api import schemas as s
from open_speech.api.ws_schema import (
    AssistantAction,
    AssistantActionResolved,
    AssistantCard,
    AssistantDelta,
    AssistantDone,
    AssistantTurn,
    CardRow,
)
from open_speech.errors import EngineError, MissingKey, NotFound
from open_speech.ids import new_id

if TYPE_CHECKING:
    from open_speech.service.engine import Engine

log = logging.getLogger("open_speech.assistant")

HELP_TEXT = (
    "You can say: read my clipboard, pause, faster, two x, next paragraph, go to page twelve, where am I, "
    "skip footnotes, say SQL as sequel, show voices, or how much have I spent. "
    "With an Anthropic key you can ask for anything else, like writing your own rules."
)


@dataclass
class Reply:
    text: str = ""
    speak: bool = True
    card: AssistantCard | None = None
    error: dict[str, Any] | None = None
    changed_playback: bool = False


@dataclass
class _Session:
    history: list[dict[str, Any]] = field(default_factory=list)
    last_reply_recording: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _money_words(d: Decimal) -> str:
    if d < Decimal("0.01"):
        return "less than a cent"
    dollars = int(d)
    cents = int((d - dollars) * 100 + Decimal("0.5"))
    if cents == 100:
        dollars, cents = dollars + 1, 0
    if dollars and cents:
        return f"{dollars} dollar{'s' if dollars != 1 else ''} and {cents} cents"
    if dollars:
        return f"{dollars} dollar{'s' if dollars != 1 else ''}"
    return f"{cents} cent{'s' if cents != 1 else ''}"


class AssistantService:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self._sessions: dict[str, _Session] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ turns

    async def submit(self, req: s.TurnRequest) -> s.TurnAccepted:
        text = req.text.strip()
        if not text:
            raise EngineError("invalid_request", "Say or type what you'd like me to do.")
        turn_id = new_id("turn")
        session_id = req.session_id or "default"
        self.e.db.execute(
            "INSERT INTO turns(id, session_id, user_text, source, user_recording_id, created_at) VALUES(?,?,?,?,?,?)",
            (turn_id, session_id, text, req.source, req.recording_id, _now()),
        )
        self.e.events.publish(
            AssistantTurn(
                turn_id=turn_id, session_id=session_id, text=text, source=req.source, recording_id=req.recording_id
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

    async def _run(self, turn_id: str, session_id: str, req: s.TurnRequest) -> None:
        handled = "fast_path"
        try:
            pending = self.latest_pending()
            intent = parse(req.text, has_pending_action=pending is not None)
            if intent is not None:
                reply = await self._fast(intent, turn_id, req, pending)
            else:
                handled = "agent"
                reply = await self._agent(turn_id, session_id, req)
        except EngineError as e:
            handled = "error"
            reply = Reply(text=e.detail, error=e.problem())
        except Exception:  # noqa: BLE001
            log.exception("turn failed")
            handled = "error"
            reply = Reply(text="Something went wrong on my side. Please try that again.", error={"code": "internal"})

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
        sess = self._sessions.setdefault(session_id, _Session())
        if rec_id:
            sess.last_reply_recording = rec_id
        self.e.db.execute(
            "UPDATE turns SET reply_text=?, reply_recording_id=?, handled_by=? WHERE id=?",
            (reply.text, rec_id, handled, turn_id),
        )
        self.e.events.publish(
            AssistantDone(
                turn_id=turn_id, text=reply.text, reply_recording_id=rec_id, handled_by=handled, error=reply.error
            )  # type: ignore[arg-type]
        )
        if audio is not None:
            await self.e.transport.play_clip(audio, duck=True)

    async def _synth_reply(self, text: str, turn_id: str) -> tuple[Any, str | None]:
        voice = self.e.settings.get().defaults.assistant_voice
        voices = {v.id: v for v in self.e.registry.voices()}
        if voice not in voices or not voices[voice].available:
            voice = self.e.registry.default_local_voice()
        res = await self.e.synth.synth(text, voice, turn_id=turn_id, op="tts")
        rec_id = None
        if self.e.recordings.keep_recordings():
            rec_id = self.e.recordings.register(
                res.audio.path, kind="reply", duration_s=res.audio.duration_s, turn_id=turn_id
            )
        return res.audio, rec_id

    # ------------------------------------------------------------------ fast path

    async def _fast(self, it: Intent, turn_id: str, req: s.TurnRequest, pending: s.ActionOut | None) -> Reply:  # noqa: C901
        pb = self.e.playback
        p = it.params
        match it.kind:
            case "pause":
                await pb.pause()
                return Reply("Paused.", speak=False, changed_playback=True)
            case "resume" | "play":
                st = await pb.resume()
                return Reply("Playing." if st.state == "playing" else "", speak=False, changed_playback=True)
            case "stop":
                await pb.stop()
                return Reply("Stopped.", speak=False, changed_playback=True)
            case "change_speed":
                st = await pb.change_speed(float(p["delta"]))
                return Reply(f"{st.speed:g}x.", speak=False)
            case "set_speed":
                st = await pb.set_speed(float(p["speed"]))
                return Reply(f"{st.speed:g}x.", speak=False)
            case "skip":
                await pb.skip(s.SkipRequest(unit=p["unit"], count=int(p["count"])))
                return Reply("", speak=False, changed_playback=True)
            case "go_to_page":
                await pb.go_to_page(int(p["page"]))
                return Reply(f"Page {p['page']}.", speak=False, changed_playback=True)
            case "read_clipboard":
                res = await self.e.documents.create(
                    s.CreateDocumentRequest(kind="clipboard", play=True, image_mode=p.get("image_mode", "auto"))
                )
                verb = "Describing" if res.document.kind == "image" else "Reading"
                return Reply(f"{verb} your clipboard.", speak=False, changed_playback=True)
            case "open_source":
                target = str(p["target"])
                if (
                    target.startswith(("/", "~"))
                    or "." in target.rsplit("/", 1)[-1]
                    and not target.startswith("http")
                    and "/" in target
                ):
                    res = await self.e.documents.create(
                        s.CreateDocumentRequest(kind="file_path", path=target, play=bool(p["play"]))
                    )
                else:
                    res = await self.e.documents.create(
                        s.CreateDocumentRequest(kind="url", url=target, play=bool(p["play"]))
                    )
                return Reply(
                    f"Reading {res.document.title}." if p["play"] else f"Opened {res.document.title}.",
                    speak=False,
                    changed_playback=True,
                )
            case "where_am_i":
                text, data = pb.where_am_i()
                card = AssistantCard(turn_id=turn_id, kind="where", title="Where am I", data=data)
                return Reply(text, card=card)
            case "lexicon":
                term, spoken = str(p["term"]), str(p["spoken"])
                aid = self.create_action(
                    kind="add_pronunciation",
                    summary=f"Say “{term}” as “{spoken}”",
                    spoken=f"I'll say {term} as {spoken}. Save it?",
                    payload={"term": term, "spoken": spoken, "scope": ""},
                    turn_id=turn_id,
                )
                return Reply(f"I'll say {term} as {spoken}. Save it?", card=None) if aid else Reply()
            case "preset":
                prop = await self.e.rules.propose_preset(str(p["preset"]), bool(p["enabled"]), turn_id=turn_id)
                return Reply(prop.summary)
            case "propose_rule":
                prop = await self.e.rules.propose(
                    s.RuleProposalRequest(instruction=str(p["instruction"]), scope_hint=p.get("scope_hint")),
                    turn_id=turn_id,
                )
                return Reply(prop.summary)
            case "set_mode":
                mode = str(p["mode"])
                if mode == "ai_enhanced":
                    spoken = (
                        "That turns on AI Enhanced cleanup. It uses Claude and costs a little per document. Turn it on?"
                    )
                    if self.e.registry.llm() is None:
                        raise MissingKey("anthropic", "AI Enhanced cleanup")
                else:
                    spoken = "Switch to Classic cleanup? It's free and never calls an API."
                self.create_action(
                    kind="set_mode",
                    summary="AI Enhanced cleanup · everywhere"
                    if mode == "ai_enhanced"
                    else "Classic cleanup · everywhere",
                    spoken=spoken,
                    payload={"mode": mode},
                    turn_id=turn_id,
                )
                return Reply(spoken)
            case "usage":
                return self._usage_reply(turn_id, p.get("period", "month"))
            case "confirm":
                assert pending is not None
                res = await self.confirm(pending.action_id)
                return Reply(res.message)
            case "cancel":
                assert pending is not None
                res = await self.cancel(pending.action_id)
                return Reply(res.message)
            case "show":
                return await self._show(turn_id, str(p["what"]))
            case "repeat":
                sess = self._sessions.get(req.session_id or "default")
                if sess and sess.last_reply_recording:
                    await self.e.recordings.play(sess.last_reply_recording)
                    return Reply("", speak=False)
                last = self.e.db.one(
                    "SELECT reply_text FROM turns WHERE reply_text != '' ORDER BY created_at DESC LIMIT 1"
                )
                return Reply(last["reply_text"] if last else "I haven't said anything yet.")
            case "clear_cache":
                size = self.e.audio_cache.size_bytes()
                gb = size / 1e9
                amount = f"about {gb:.1f} gigabytes" if gb >= 0.1 else f"about {max(1, round(size / 1e6))} megabytes"
                spoken = (
                    f"That deletes {amount} of cached audio, so replays will need to be synthesized again. Clear it?"
                )
                self.create_action(
                    kind="clear_cache",
                    summary=f"Clear audio cache · {amount}",
                    spoken=spoken,
                    payload={},
                    turn_id=turn_id,
                )
                return Reply(spoken)
            case "set_voice_by_name":
                voice = self.e.voices.find_by_name(str(p["name"]))
                if voice is None:
                    return Reply(f"I couldn't find a voice called {p['name']}. Say “show voices” to hear the options.")
                scope = str(p.get("scope", "document"))
                if scope == "document":
                    await pb.set_voice(s.VoiceRequest(voice_id=voice.id, scope="document"))
                    return Reply(f"Switched to {voice.name}.", speak=False)
                spoken = f"Make {voice.name} the {'assistant' if scope == 'assistant' else 'default'} voice?"
                self.create_action(
                    kind="set_voice",
                    summary=f"{voice.name} · {'assistant' if scope == 'assistant' else 'default'} voice",
                    spoken=spoken,
                    payload={"voice_id": voice.id, "scope": scope},
                    turn_id=turn_id,
                )
                return Reply(spoken)
            case "help":
                return Reply(HELP_TEXT)
        return Reply("Sorry, I didn't catch that.")

    def _usage_reply(self, turn_id: str, period: str) -> Reply:
        summary = self.e.usage.summary(period=period, group_by="provider")  # type: ignore[arg-type]
        rows = [
            CardRow(
                id=f"u{i}", title=line.key, value=f"${Decimal(line.usd_estimate):.2f}", subtitle=f"{line.calls} calls"
            )
            for i, line in enumerate(summary.breakdown[:3])
        ]
        card = AssistantCard(
            turn_id=turn_id,
            kind="usage",
            title=f"Usage · {'this month' if period == 'month' else 'this week' if period == 'week' else 'today'} (estimated)",
            rows=rows,
            data=summary.model_dump(mode="json"),
        )
        return Reply(summary.spoken, card=card)

    async def _show(self, turn_id: str, what: str) -> Reply:
        if what == "voices":
            current = self.e.documents.voice_for(self.e.playback.current_document_id())
            voices = self.e.registry.voices()
            rows = [
                CardRow(
                    id=v.id,
                    title=v.name,
                    subtitle=(
                        f"{v.provider} · {', '.join(v.languages[:2])}"
                        if v.available
                        else (v.unavailable_reason or "Unavailable")
                    ),
                    selected=v.id == current,
                    data={"available": v.available, "local": v.local, "provider": v.provider},
                )
                for v in voices
            ]
            n = sum(1 for v in voices if v.available)
            card = AssistantCard(turn_id=turn_id, kind="voices", title="Voices (AI-generated)", rows=rows)
            return Reply(f"Here are {n} voices you can use. Say a name to switch, or press play to preview.", card=card)
        if what == "rules":
            listing = self.e.rules.list(document_id=self.e.playback.current_document_id())
            rows = [
                CardRow(
                    id=r.id,
                    title=r.name,
                    subtitle=f"{r.tier} · {_scope_text(r.scope)}",
                    selected=r.enabled,
                    data={"path": r.path, "preset": r.preset},
                )
                for r in listing.items
            ]
            card = AssistantCard(
                turn_id=turn_id,
                kind="rules",
                title="Rules for this document" if self.e.playback.current_document_id() else "Rules",
                rows=rows,
            )
            active = sum(1 for r in listing.items if r.enabled)
            return Reply(f"{active} rules are active.", card=card)
        if what == "usage":
            return self._usage_reply(turn_id, "month")
        if what == "recents":
            docs = await self.e.documents.list(limit=10)
            rows = [
                CardRow(id=d.id, title=d.title, subtitle=d.kind, value=f"{round(d.progress * 100)}%")
                for d in docs.items
            ]
            card = AssistantCard(turn_id=turn_id, kind="recents", title="Recent", rows=rows)
            return Reply(f"You have {len(rows)} recent documents." if rows else "Nothing recent yet.", card=card)
        if what == "models":
            models = self.e.model_manager.list()
            rows = [
                CardRow(
                    id=m.id,
                    title=m.name,
                    subtitle=f"{m.size_mb} MB · {m.ram_gb:g} GB RAM · {m.license}",
                    value=m.fit,
                    selected=m.installed,
                )
                for m in models
            ]
            card = AssistantCard(turn_id=turn_id, kind="models", title="Local models", rows=rows)
            return Reply("Here are the local models.", card=card)
        if what == "presets":
            st = self.e.rules.presets()
            rows = [CardRow(id=pr.id, title=pr.name, subtitle=pr.classic, selected=pr.enabled) for pr in st.presets]
            card = AssistantCard(
                turn_id=turn_id,
                kind="presets",
                title=f"Auto-skip · {st.mode.replace('_', ' ')}",
                rows=rows,
                data={"mode": st.mode},
            )
            return Reply(f"Cleanup is {st.mode.replace('_', ' ')}.", card=card)
        if what == "keys":
            ks = self.e.keys.state()
            rows = [CardRow(id=name, title=name.capitalize(), value=status) for name, status in ks.model_dump().items()]
            card = AssistantCard(turn_id=turn_id, kind="keys", title="Keys", rows=rows)
            return Reply("Paste a key into the bar to add or replace one.", card=card)
        return Reply("I can show voices, rules, usage, recents, presets, keys or local models.")

    # ------------------------------------------------------------------ agent

    async def _agent(self, turn_id: str, session_id: str, req: s.TurnRequest) -> Reply:
        from open_speech.agent.core import run_agent_turn

        return await run_agent_turn(
            self.e, self, turn_id=turn_id, session=self._sessions.setdefault(session_id, _Session()), req=req
        )

    # ------------------------------------------------------------------ actions

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
            "INSERT INTO actions(id, turn_id, kind, summary, payload_json, preview_json, status, created_at) VALUES(?,?,?,?,?,?, 'pending', ?)",
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
            raise NotFound("that action")
        if row["status"] != "pending":
            return s.ActionResult(action_id=action_id, status="failed", message=f"That was already {row['status']}.")
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
            raise NotFound("that action")
        if row["status"] != "pending":
            return s.ActionResult(action_id=action_id, status="failed", message=f"That was already {row['status']}.")
        self.mark_action(action_id, "cancelled", "Cancelled.")
        return s.ActionResult(action_id=action_id, status="cancelled", message="Cancelled.")

    async def _execute(self, kind: str, p: dict[str, Any]) -> str:  # noqa: C901
        from open_speech.rules.model import Rule

        match kind:
            case "save_rule":
                rule = Rule.model_validate(p["rule"])
                await self.e.rules.save_rule(rule)
                return "Saved. I'll apply it from here."
            case "set_preset":
                await self.e.rules.set_preset(str(p["preset"]), bool(p["enabled"]))
                return "Done."
            case "set_mode":
                scope = s.RuleScope.model_validate(p["scope"]) if p.get("scope") else None
                await self.e.rules.set_mode(str(p["mode"]), scope)
                return "AI Enhanced is on." if p["mode"] == "ai_enhanced" else "Classic cleanup is on."
            case "add_pronunciation":
                await self.e.rules.add_lexicon(
                    s.LexiconEntry(term=p["term"], spoken=p["spoken"], scope=p.get("scope", ""))
                )
                return "Saved."
            case "set_voice":
                await self.e.playback.set_voice(s.VoiceRequest(voice_id=p["voice_id"], scope=p.get("scope", "default")))
                return "Voice changed."
            case "set_budget":
                st = await self.e.usage.set_budget(s.BudgetUpdate(**p))
                return f"Your monthly budget is now ${Decimal(st.monthly_usd):g}."
            case "update_setting":
                await self.e.settings.set_key(str(p["key"]), p["value"])
                return "Setting changed."
            case "install_model":
                await self.e.models.install(str(p["model_id"]))
                return "Installing. I'll tell you when it's ready."
            case "clear_cache":
                n = self.e.audio_cache.clear()
                return f"Cleared the audio cache ({n} files)."
            case "delete_rule":
                await self.e.rules.delete(str(p["rule_id"]))
                return "Rule deleted."
            case "update_rule":
                await self.e.rules.patch(str(p["rule_id"]), s.RulePatch(**p.get("changes", {})))
                return "Rule updated."
        raise EngineError("invalid_request", f"Unknown action {kind}.")

    # ------------------------------------------------------------------ recordings

    def recording_from_pcm(self, pcm: np.ndarray, sr: int, *, turn_id: str | None, kind: str) -> str | None:
        if not self.e.recordings.keep_recordings():
            return None
        rec_id, path = self.e.recordings.new_path()
        sf.write(str(path), pcm, sr, format="OGG", subtype="OPUS")
        self.e.recordings.insert(rec_id, path, kind=kind, duration_s=len(pcm) / sr, turn_id=turn_id)
        return rec_id


def _scope_text(scope: s.RuleScope) -> str:
    if scope.docs:
        return "this document"
    if scope.sites:
        return ", ".join(scope.sites)
    if scope.types:
        return ", ".join(scope.types)
    return "everywhere"

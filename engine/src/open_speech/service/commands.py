"""What the built-in commands do (07 § Fast path): one handler per intent from `agent/intents`.

Everything here is deterministic, free and offline: an intent goes in, an effect happens, and a short reply
comes back in the user's language. Changes that are worth a second look (rules, pronunciations, the default
voice, budgets, the cleanup mode) become a confirmation card; changes that are instantly visible and
instantly reversible (theme, language, text size) just happen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from open_speech.agent.intents import Intent
from open_speech.api import schemas as s
from open_speech.api.ws_schema import AssistantCard, CardRow, UiOpen
from open_speech.errors import EngineError, Unavailable
from open_speech.i18n import resolve_language, say, t, tn, use_language
from open_speech.rules.presets import preset_out

if TYPE_CHECKING:
    from open_speech.service.assistant import AssistantService
    from open_speech.service.engine import Engine


@dataclass
class Reply:
    text: str = ""
    speak: bool = True
    card: AssistantCard | None = None
    error: dict[str, Any] | None = None


@dataclass
class Turn:
    """What a handler needs to know about the turn it is answering."""

    id: str
    request: s.TurnRequest
    pending: s.ActionOut | None = None
    documents: list[str] = field(default_factory=list)  # attachments, imported, in order
    conversation: str = "default"


class ToAgent(Exception):  # noqa: N818 — control flow, not an error
    """Raised by a handler that matched but can't resolve the request without the LLM."""


PROVIDER_LABELS = {
    "anthropic": "Claude",
    "openai": "OpenAI",
    "gemini": "Gemini",
    "kokoro": "Kokoro",
    "fixture": "Test",
    "openai_compatible": "Your server",
}
TEXT_SIZES = (10, 48)


def voice_display(v: Any) -> tuple[str, str]:
    """("Heart", "Kokoro · American English") from a catalog voice named "Heart (Kokoro, American English)".
    Names stay unique in the API; this is only how a row reads."""
    name: str = v.name
    title, _, rest = name.partition(" (")
    parts = [p.strip() for p in rest.rstrip(")").split(",") if p.strip()] if rest else []
    label = PROVIDER_LABELS.get(v.provider, v.provider.capitalize())
    if not parts or parts[0].lower() != label.lower():
        parts.insert(0, label)
    return title, " · ".join(dict.fromkeys(parts))


class Commands:
    def __init__(self, engine: Engine, assistant: AssistantService) -> None:
        self.e = engine
        self.a = assistant

    # ------------------------------------------------------------------ dispatch

    async def run(self, it: Intent, turn: Turn) -> Reply:  # noqa: C901 — one branch per command
        e, pb, p = self.e, self.e.playback, it.params
        match it.kind:
            # ---- playback: the effect is audible, so nothing is spoken
            case "pause":
                await pb.pause()
                return Reply(t("reply.paused"), speak=False)
            case "resume" | "play":
                st = await pb.resume()
                return Reply(t("reply.playing") if st.state == "playing" else "", speak=False)
            case "stop":
                await pb.stop()
                return Reply(t("reply.stopped"), speak=False)
            case "change_speed":
                st = await pb.change_speed(float(p["delta"]))
                return Reply(say.speed(st.speed), speak=False)
            case "set_speed":
                st = await pb.set_speed(float(p["speed"]))
                return Reply(say.speed(st.speed), speak=False)
            case "skip":
                await pb.skip(s.SkipRequest(unit=p["unit"], count=int(p["count"])))
                return Reply(speak=False)
            case "go_to_page":
                await pb.go_to_page(int(p["page"]))
                return Reply(t("reply.page", page=p["page"]), speak=False)
            case "next_document":
                st = await pb.next_document()
                return Reply(t("reply.reading", title=st.document_title or ""), speak=False)
            case "clear_queue":
                await pb.clear_queue()
                return Reply(t("reply.queue_cleared"), speak=False)

            # ---- things to read
            case "read_attachments":
                return await self.read_documents(turn.documents, turn_id=turn.id)
            case "read_clipboard":
                res = await e.documents.create(
                    s.CreateDocumentRequest(kind="clipboard", play=False, image_mode=p.get("image_mode", "auto"))
                )
                key = "reply.describing_clipboard" if res.document.kind == "image" else "reply.reading_clipboard"
                return await self.read_documents([res.document.id], turn_id=turn.id, said=t(key))
            case "open_source":
                res = await e.documents.create(_source_request(str(p["target"]), play=False))
                if not p["play"]:
                    return Reply(t("reply.opened", title=res.document.title), speak=False)
                return await self.read_documents([res.document.id], turn_id=turn.id)
            case "where_am_i":
                text, data = pb.where_am_i()
                return Reply(text, card=AssistantCard(turn_id=turn.id, kind="where", title="", data=data))

            # ---- rules and pronunciations (confirmed on a card)
            case "lexicon":
                term, spoken = str(p["term"]), str(p["spoken"])
                ask = t("ask.pronounce", term=term, spoken=spoken)
                self.a.create_action(
                    kind="add_pronunciation",
                    summary=t("action.pronounce", term=term, spoken=spoken),
                    spoken=ask,
                    payload={"term": term, "spoken": spoken, "scope": ""},
                    turn_id=turn.id,
                )
                return Reply(ask)
            case "remove_pronunciation":
                term = str(p["term"])
                entry = next((x for x in e.rules.lexicon().items if x.term.lower() == term.lower()), None)
                if entry is None:
                    return Reply(t("reply.no_pronunciation", term=term))
                ask = t("ask.forget_pronunciation", term=entry.term)
                self.a.create_action(
                    kind="remove_pronunciation",
                    summary=t("action.forget_pronunciation", term=entry.term),
                    spoken=ask,
                    payload={"term": entry.term},
                    turn_id=turn.id,
                )
                return Reply(ask)
            case "preset":
                name, enabled = str(p["preset"]), bool(p["enabled"])
                current = e.rules.find_preset(name)
                if current is not None and current.enabled == enabled:  # nothing to confirm
                    noun = e.rules.preset_noun(preset_out(current).name)
                    return Reply(t("reply.already_skipping" if enabled else "reply.already_reading", what=noun))
                prop = await e.rules.propose_preset(name, enabled, turn_id=turn.id)
                return Reply(prop.summary)
            case "propose_rule":
                prop = await e.rules.propose(
                    s.RuleProposalRequest(instruction=str(p["instruction"]), scope_hint=p.get("scope_hint")),
                    turn_id=turn.id,
                )
                return Reply(prop.summary)
            case "set_mode":
                mode = str(p["mode"])
                if mode == "ai_enhanced" and e.registry.llm() is None:
                    raise Unavailable("ai_cleanup")
                ask = t(f"ask.mode.{mode}")
                self.a.create_action(
                    kind="set_mode",
                    summary=t(f"action.mode.{mode}"),
                    spoken=ask,
                    payload={"mode": mode},
                    turn_id=turn.id,
                )
                return Reply(ask)
            case "open_rules":
                e.events.publish(UiOpen(target="rules", item=p.get("item")))
                return Reply(t("reply.rules_opened"), speak=False)

            # ---- settings that are instantly visible and instantly reversible: no card
            case "set_theme":
                theme = str(p["theme"])
                if theme == "toggle":
                    theme = "lavender" if e.effective_theme() == "ember" else "ember"
                await e.settings.set_key("appearance.theme", theme)
                return Reply(t(f"reply.theme.{theme}"))
            case "set_language":
                await e.settings.set_key("language", str(p["language"]))
                use_language(str(p["language"]))  # answer in the language just asked for
                return Reply(t("reply.language"))
            case "text_size":
                size = max(TEXT_SIZES[0], min(TEXT_SIZES[1], e.settings.get().reader.font_size + int(p["delta"])))
                await e.settings.set_key("reader.font_size", size)
                return Reply(t("reply.text_size", size=size), speak=False)
            case "set_budget":
                amount = Decimal(str(p["monthly_usd"]))
                ask = t("ask.budget", amount=say.money(amount))
                self.a.create_action(
                    kind="set_budget",
                    summary=t("action.budget", amount=say.money_figure(amount)),
                    spoken=ask,
                    payload={"monthly_usd": str(amount)},
                    turn_id=turn.id,
                )
                return Reply(ask)

            # ---- voices
            case "set_voice_by_name":
                voice = e.voices.find_by_name(str(p["name"]))
                if voice is None:
                    if e.has_agent():  # "use a calmer voice" is a description, not a name
                        raise ToAgent
                    return Reply(t("reply.no_voice", name=p["name"]))
                name = voice_display(voice)[0]
                scope = str(p.get("scope", "document"))
                if scope == "document":
                    await pb.set_voice(s.VoiceRequest(voice_id=voice.id, scope="document"))
                    return Reply(t("reply.voice", name=name), speak=False)
                ask = t(f"ask.voice.{scope}", name=name)
                self.a.create_action(
                    kind="set_voice",
                    summary=t(f"action.voice.{scope}", name=name),
                    spoken=ask,
                    payload={"voice_id": voice.id, "scope": scope},
                    turn_id=turn.id,
                )
                return Reply(ask)

            # ---- information
            case "usage":
                return self.usage(turn.id, p.get("period", "month"))
            case "show":
                return await self.show(turn.id, str(p["what"]))
            case "help":
                return Reply(t("help.with_agent" if e.has_agent() else "help.basic"))
            case "repeat":
                rec = self.a.last_reply_recording(turn.conversation)
                if rec:
                    await e.recordings.play(rec)
                    return Reply(speak=False)
                last = e.db.one("SELECT reply_text FROM turns WHERE reply_text != '' ORDER BY created_at DESC LIMIT 1")
                return Reply(last["reply_text"] if last else t("reply.nothing_said"))
            case "clear_cache":
                stored = say.size(e.audio_cache.size_bytes())
                ask = t("ask.clear_cache", amount=stored)
                self.a.create_action(
                    kind="clear_cache",
                    summary=t("action.clear_cache", amount=stored),
                    spoken=ask,
                    payload={},
                    turn_id=turn.id,
                )
                return Reply(ask)

            # ---- answering a card
            case "confirm":
                assert turn.pending is not None
                return Reply((await self.a.confirm(turn.pending.action_id)).message)
            case "cancel":
                assert turn.pending is not None
                return Reply((await self.a.cancel(turn.pending.action_id)).message, speak=False)
        return Reply(t("reply.not_understood"))

    # ------------------------------------------------------------------ several things to read

    async def read_documents(
        self, document_ids: list[str], *, when: str = "now", turn_id: str | None = None, said: str | None = None
    ) -> Reply:
        """Read documents one after another (the queue shows on every client). Reading that would cost more
        than the user's threshold is put on a confirmation card first (FR-COST-3), whichever way it was asked
        for: by voice, typed, a button, attachments or the assistant."""
        if not document_ids:
            return Reply(t("reply.nothing_attached"))
        cost = await self.cost_needing_confirmation(document_ids)
        if cost is not None:
            ask = t("ask.read_cost", amount=say.money(cost))
            self.a.create_action(
                kind="read_documents",
                summary=t("action.read_cost", amount=say.money_figure(cost)),
                spoken=ask,
                payload={"document_ids": document_ids, "when": when},
                turn_id=turn_id,
            )
            return Reply(ask)
        st = await self.e.playback.enqueue(s.QueueRequest(document_ids=document_ids, when=when))  # type: ignore[arg-type]
        if said is not None:
            return Reply(said, speak=False)
        if len(document_ids) == 1:
            return Reply(t("reply.reading", title=st.document_title or ""), speak=False)
        return Reply(tn("reply.reading_many", len(document_ids)), speak=False)

    async def cost_needing_confirmation(self, document_ids: list[str]) -> Decimal | None:
        """The estimated cost of reading these documents, when any of them is over the confirmation threshold."""
        total = Decimal(0)
        for document_id in document_ids:
            try:
                estimate = await self.e.playback.cost_to_confirm(document_id)
            except EngineError:
                continue
            if estimate is not None:
                total += Decimal(estimate.usd_estimate)
        return total if total > 0 else None

    # ------------------------------------------------------------------ cards ("rows, not screens")

    def usage(self, turn_id: str, period: str) -> Reply:
        summary = self.e.usage.summary(period=period, group_by="provider")  # type: ignore[arg-type]
        rows = [
            CardRow(
                id=f"u{i}",
                title=PROVIDER_LABELS.get(line.key, line.key),
                value=f"${Decimal(line.usd_estimate):.2f}",
                data={"provider": line.key, "calls": line.calls},
            )
            for i, line in enumerate(summary.breakdown[:3])
            if Decimal(line.usd_estimate) > 0  # free local work isn't a cost line
        ]
        card = AssistantCard(
            turn_id=turn_id,
            kind="usage",
            title=t(f"card.usage.{period}"),
            rows=rows,
            data=summary.model_dump(mode="json"),
        )
        return Reply(summary.spoken, card=card)

    async def show(self, turn_id: str, what: str) -> Reply:  # noqa: C901 — one branch per list
        e = self.e
        if what == "voices":
            current = e.documents.voice_for(e.playback.current_document_id())
            language = resolve_language(e.settings.get().language)
            voices = [v for v in e.registry.voices() if v.available]
            # The current voice first, then voices in the user's language, local before cloud (FR-VOICE-1).
            voices.sort(key=lambda v: (v.id != current, not e.voices.speaks(v, language), not v.local))
            rows = []
            for v in voices:
                title, detail = voice_display(v)
                rows.append(
                    CardRow(
                        id=v.id,
                        title=title,
                        subtitle=detail,
                        selected=v.id == current,
                        data={"local": v.local, "provider": v.provider, "languages": v.languages[:4]},
                    )
                )
            cur = next((v for v in voices if v.id == current), None)
            lead = t("reply.voice_current", name=voice_display(cur)[0]) + " " if cur else ""
            return Reply(
                lead + tn("reply.voices_ready", len(voices)),
                card=AssistantCard(turn_id=turn_id, kind="voices", title=t("card.voices"), rows=rows),
            )
        if what == "rules":
            listing = e.rules.list(document_id=e.playback.current_document_id())
            rows = [
                CardRow(
                    id=r.id,
                    title=r.description or r.name,
                    subtitle=e.rules.scope_text(r.scope),
                    selected=r.enabled,
                    data={"path": r.path, "preset": r.preset, "tier": r.tier, "name": r.name},
                )
                for r in listing.items
            ]
            active = sum(1 for r in listing.items if r.enabled)
            return Reply(
                tn("reply.rules_active", active),
                card=AssistantCard(turn_id=turn_id, kind="rules", title=t("card.rules"), rows=rows),
            )
        if what == "usage":
            return self.usage(turn_id, "month")
        if what == "recents":
            docs = await e.documents.list(limit=10)
            rows = [
                CardRow(
                    id=d.id, title=d.title, value=f"{round(d.progress * 100)}%", data={"kind": d.kind, "site": d.site}
                )
                for d in docs.items
            ]
            text = tn("reply.recents", len(rows)) if rows else t("reply.no_recents")
            return Reply(text, card=AssistantCard(turn_id=turn_id, kind="recents", title=t("card.recents"), rows=rows))
        if what == "queue":
            rows = [CardRow(id=q.document_id, title=q.title) for q in e.playback.queue()]
            text = tn("reply.queue", len(rows)) if rows else t("queue.empty")
            return Reply(text, card=AssistantCard(turn_id=turn_id, kind="queue", title=t("card.queue"), rows=rows))
        if what == "models":
            rows = [
                CardRow(
                    id=m.id,
                    title=m.name,
                    subtitle=f"{m.size_mb} MB · {m.ram_gb:g} GB RAM",
                    value=m.fit,
                    selected=m.installed,
                    data={"installed": m.installed, "fit": m.fit, "size_mb": m.size_mb, "ram_gb": m.ram_gb,
                          "kind": getattr(m, "kind", "tts")},
                )
                for m in e.model_manager.list()
            ]  # fmt: skip
            return Reply(
                t("reply.models"), card=AssistantCard(turn_id=turn_id, kind="models", title=t("card.models"), rows=rows)
            )
        if what == "presets":
            st = e.rules.presets()
            rows = [CardRow(id=pr.id, title=pr.name, selected=pr.enabled, data={"name": pr.name}) for pr in st.presets]
            card = AssistantCard(
                turn_id=turn_id, kind="presets", title=t("card.presets"), rows=rows, data={"mode": st.mode}
            )
            return Reply(t(f"reply.mode.{st.mode}"), card=card)
        if what == "settings":
            return Reply(t("reply.settings"), card=self.settings_card(turn_id))
        return Reply(t("reply.can_show"))

    def settings_card(self, turn_id: str) -> AssistantCard:
        """The handful of settings people change, as rows the bar draws with icons and switches."""
        st = self.e.settings.get()
        voices = {v.id: v for v in self.e.registry.voices()}
        voice = voices.get(st.defaults.voice)
        can_ai = self.e.registry.llm() is not None

        def row(key: str, value: object, options: list[object] | None = None, **data: object) -> CardRow:
            return CardRow(
                id=key,
                title=t(f"setting.{key}"),
                value=str(value),
                data={"key": key, "value": value, **({"options": options} if options else {}), **data},
            )

        rows = [
            row("appearance.theme", st.appearance.theme, ["system", "lavender", "ember"]),
            row("language", st.language, ["auto", "en", "es"]),
            row("defaults.voice", voice_display(voice)[0] if voice else st.defaults.voice, voice_id=st.defaults.voice),
            row("defaults.speed", st.defaults.speed, min=0.5, max=5.0, step=0.25),
            row("defaults.mode", st.defaults.mode, ["classic", "ai_enhanced"] if can_ai else ["classic"]),
            row("reader.font_size", st.reader.font_size, min=TEXT_SIZES[0], max=TEXT_SIZES[1], step=2),
        ]
        return AssistantCard(turn_id=turn_id, kind="settings", title=t("card.settings"), rows=rows)


def _source_request(target: str, *, play: bool) -> s.CreateDocumentRequest:
    """A typed or spoken thing to read: "clipboard", a file path, or a link."""
    if target.strip().lower() == "clipboard":
        return s.CreateDocumentRequest(kind="clipboard", play=play)
    looks_like_file = target.startswith(("/", "~")) or (
        "." in target.rsplit("/", 1)[-1] and not target.startswith("http") and "/" in target
    )
    if looks_like_file:
        return s.CreateDocumentRequest(kind="file_path", path=target, play=play)
    return s.CreateDocumentRequest(kind="url", url=target, play=play)


__all__ = ["Commands", "EngineError", "Reply", "ToAgent", "Turn", "voice_display"]

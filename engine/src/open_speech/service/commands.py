"""What the built-in commands do (07 § Fast path): one handler per intent from `agent/intents`.

Everything here is deterministic, free and offline: an intent goes in, an effect happens, and a short reply
comes back in the user's language. Changes that are worth a second look (rules limited to a site, document or
type, pronunciations, the default voice, budgets) become a confirmation card; changes that are instantly visible
and instantly reversible (theme, language, text size) just happen. What is read or left out everywhere is not a
command: Claude changes the reading instructions (`agent/tools.py` `edit_prompt`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from open_speech.agent.intents import Intent
from open_speech.api import schemas as s
from open_speech.api.ws_schema import AssistantCard, CardRow, UiOpen
from open_speech.config import AUTO_VOICE
from open_speech.errors import EngineError
from open_speech.i18n import resolve_language, say, t, tn, use_language
from open_speech.providers.base import Voice

if TYPE_CHECKING:
    from open_speech.service.assistant import AssistantService
    from open_speech.service.engine import Engine


@dataclass
class Reply:
    """What a turn answers. Every reply is spoken (07 § Reply speech), except when `audible_effect` is set and
    reading will resume: then reading audio is the answer ("faster" while reading, "read my clipboard"), and
    the words are only shown. `audio` is said instead of the text (a voice sample, a reply said again). `parts`
    is said instead of both, in order: text in the assistant voice, `(pcm, sample_rate)` as it is (a new voice's
    sample, then "Keep it?")."""

    text: str = ""
    audible_effect: bool = False
    card: AssistantCard | None = None
    error: dict[str, Any] | None = None
    audio: Path | None = None
    audio_recording: str | None = None  # `audio` is this recording
    parts: list[Any] | None = None


@dataclass
class Turn:
    """What a handler needs to know about the turn it is answering."""

    id: str
    request: s.TurnRequest
    pending: s.ActionOut | None = None
    documents: list[str] = field(default_factory=list)  # attachments, imported, in order
    conversation: str = "default"
    audio: list[str] = field(default_factory=list)  # attached audio files (paths): a voice to clone, not a document
    stream: Any = None  # the turn's ReplyStream: long work (a voice being made) can say something while it runs


class ToAgent(Exception):  # noqa: N818 — control flow, not an error
    """Raised by a handler that matched but can't resolve the request without the LLM."""


PROVIDER_LABELS = {
    "anthropic": "Claude",
    "openai": "OpenAI",
    "gemini": "Gemini",
    "google": "Google",
    "kokoro": "Kokoro",
    "qwen3": "Qwen",
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
            # ---- playback. Where the effect is reading audio, that is the answer when reading resumes after the
            # conversation (`audible_effect`); otherwise the words are said ("faster" with nothing playing).
            # Pause and stop are said: during a conversation reading is already paused, so words are the only
            # feedback.
            case "pause":
                await pb.pause()
                return Reply(t("reply.paused"))
            case "resume" | "play":
                await pb.resume()
                return Reply(t("reply.playing" if pb.reading_intended() else "reply.nothing_to_play"),
                             audible_effect=True)
            case "stop":
                await pb.stop()
                return Reply(t("reply.stopped"))
            case "change_speed":
                st = await pb.change_speed(float(p["delta"]))
                return Reply(say.speed(st.speed), audible_effect=True)
            case "set_speed":
                st = await pb.set_speed(float(p["speed"]))
                return Reply(say.speed(st.speed), audible_effect=True)
            case "skip":
                await pb.skip(s.SkipRequest(unit=p["unit"], count=int(p["count"])))
                return Reply(t("reply.skipped"), audible_effect=True)
            case "go_to_page":
                await pb.go_to_page(int(p["page"]))
                return Reply(t("reply.page", page=p["page"]), audible_effect=True)
            case "next_document":
                st = await pb.next_document()
                return Reply(t("reply.reading", title=st.document_title or ""), audible_effect=True)
            case "clear_queue":
                await pb.clear_queue()
                return Reply(t("reply.queue_cleared"))

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
                    return Reply(t("reply.opened", title=res.document.title))
                return await self.read_documents([res.document.id], turn_id=turn.id)
            case "where_am_i":
                text, data = pb.where_am_i()
                return Reply(text, card=AssistantCard(turn_id=turn.id, kind="where", title="", data=data))

            # ---- scoped rules and pronunciations (confirmed on a card), the rules editor
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
            case "propose_rule":  # limited to a site, a document or a type ("skip footnotes on this site")
                prop = await e.rules.propose(
                    s.RuleProposalRequest(instruction=str(p["instruction"]), scope_hint=p.get("scope_hint")),
                    turn_id=turn.id,
                )
                return Reply(prop.summary)
            case "open_rules":
                e.events.publish(UiOpen(target="rules", item=p.get("item")))
                return Reply(t("reply.rules_opened"))

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
                return Reply(t("reply.text_size", size=size))
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
                    return Reply(t("reply.voice", name=name), audible_effect=True)
                ask = t(f"ask.voice.{scope}", name=name)
                self.a.create_action(
                    kind="set_voice",
                    summary=t(f"action.voice.{scope}", name=name),
                    spoken=ask,
                    payload={"voice_id": voice.id, "scope": scope},
                    turn_id=turn.id,
                )
                return Reply(ask)

            case "keep_free_tier_voice":
                return self.keep_free_tier_voice(turn.id, str(p.get("provider", "google")))
            case "install_voice_model":
                return self.install_voice_model(turn.id, str(p.get("family") or "customvoice"), p.get("size"))

            # ---- voices of your own (service/clones.py): cards first, the work in this turn
            case "clone_voice":
                if p.get("source") == "file" or turn.audio:
                    return e.clones.start_file_clone(turn, turn.audio, name=p.get("name"))
                return e.clones.start_clone(turn, name=p.get("name"))
            case "design_voice":
                download_first = e.clones.start_design(turn, str(p["description"]), name=p.get("name"))
                if download_first is not None:
                    return download_first
                return await e.clones.design_in_turn(turn, str(p["description"]), name=p.get("name"))
            case "delete_voice":
                return Reply(e.clones.propose_delete(turn.id, str(p["name"]))[1])
            case "rename_voice":
                return Reply(await e.clones.rename(str(p["name"]), str(p["new_name"])))

            # ---- information
            case "usage":
                return self.usage(turn.id, p.get("period", "month"))
            case "show":
                return await self.show(turn.id, str(p["what"]), provider=p.get("provider"))
            case "help":
                return Reply(t("help.with_agent" if e.has_agent() else "help.basic"))
            case "repeat":
                return self.repeat(turn)
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
                if turn.pending.kind in _clone_actions():  # some run in this turn and answer with a sample
                    return await e.clones.confirm(turn.pending, turn)
                return Reply((await self.a.confirm(turn.pending.action_id)).message)
            case "cancel":
                assert turn.pending is not None
                if turn.pending.kind in _clone_actions():
                    return await e.clones.cancel(turn.pending)
                return Reply((await self.a.cancel(turn.pending.action_id)).message)
        return Reply(t("reply.not_understood"))

    def repeat(self, turn: Turn) -> Reply:
        """"Say that again": the last reply's own recording when there is one (the same audio, $0), else its text."""
        e = self.e
        rec = self.a.last_reply_recording(turn.conversation)
        if rec:
            row = e.db.one("SELECT reply_text FROM turns WHERE reply_recording_id=? LIMIT 1", (rec,))
            try:
                return Reply(row["reply_text"] if row else "", audio=e.recordings.path(rec), audio_recording=rec)
            except EngineError:
                pass  # pruned: say the text again
        last = e.db.one(
            "SELECT reply_text FROM turns WHERE reply_text != '' AND id != ? ORDER BY created_at DESC LIMIT 1",
            (turn.id,),
        )
        return Reply(last["reply_text"] if last else t("reply.nothing_said"))

    async def preview_voice(self, voice_id: str, turn: Turn) -> Reply:
        """A voice named from the voices card: its sample is the spoken answer."""
        voice = self.e.registry.voice(voice_id)
        name = voice_display(voice)[0]
        res = await self.e.voices.preview(voice_id, play=False)
        return Reply(
            t("reply.voice_sample", name=name),
            audio=self.e.recordings.path(res.recording_id),
            audio_recording=res.recording_id,
        )

    def install_voice_model(self, turn_id: str, family: str, size: str | None) -> Reply:
        """"Install the small Qwen voice": the variant of that size (the precision the settings choose) on a card with
        its download size; "yes" downloads it with progress. Asked first because it is gigabytes."""
        from open_speech.local.manager import fit_for
        from open_speech.providers.qwen3 import resolve_variant

        e = self.e
        qwen = e.registry.qwen3()
        if qwen is None or not qwen.voices():
            return Reply(t("qwen.unavailable.chip"))
        st = e.settings.get().qwen3
        res = resolve_variant(e.catalog.weights_variants("qwen3"), family, size or st.size, st.precision,
                              installed=lambda _v: False, memory_gb=qwen.memory_gb(), defaults=qwen.extra)
        if res is None:
            return Reply(t("unavailable.voice"))
        v = res.wanted
        spoken_name = t("qwen.voices_spoken", size=t("qwen.size.small" if v.size == "0.6b" else "qwen.size.large"))
        if qwen.installed(v):
            return Reply(t("reply.qwen_installed", name=spoken_name))
        fit, note = fit_for(v, e.model_manager.hardware)
        if fit == "unsupported":
            return Reply(note)
        ask = t("ask.install_qwen", name=spoken_name, size=say.size(int(v.size_mb * 1024 * 1024)))
        self.a.create_action(
            kind="install_model",
            summary=t("action.install", name=e.model_manager.display_name(v), size=int(round(v.size_mb))),
            spoken=ask,
            payload={"model_id": v.model},
            turn_id=turn_id,
        )
        return Reply(ask)

    def keep_free_tier_voice(self, turn_id: str, provider: str) -> Reply:
        """"Keep using the Google voice": past this month's free characters, keep it at its price (the budget
        still applies) instead of switching to the local voice — for this billing month only. Asked on a card
        with the rate."""
        e = self.e
        allowance = next((a for a in e.pricing.free_allowances() if a.provider == provider), None)
        if allowance is None or not e.registry.google_usable():
            return Reply(t("reply.google_unavailable"))
        use = e.free_tier.use(allowance.provider, allowance.model)
        assert use is not None
        if use.policy == "pay":
            return Reply(t("reply.beyond_free_tier.already"))
        rate = allowance.per_kchars
        ask = t("ask.beyond_free_tier", rate=say.money(rate))
        self.a.create_action(
            kind="beyond_free_tier",
            summary=t("action.beyond_free_tier", rate=f"${rate:.2f}"),
            spoken=ask,
            payload={"month": use.period.key},
            turn_id=turn_id,
        )
        return Reply(ask)

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
            return Reply(said, audible_effect=True)
        if len(document_ids) == 1:
            return Reply(t("reply.reading", title=st.document_title or ""), audible_effect=True)
        return Reply(tn("reply.reading_many", len(document_ids)), audible_effect=True)

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

    async def show(self, turn_id: str, what: str, *, provider: str | None = None) -> Reply:  # noqa: C901
        e = self.e
        if what in ("voices", "voices_more"):
            return self.voices_card(turn_id, more=what == "voices_more", provider=provider)
        if what == "rules":
            listing = e.rules.list(document_id=e.playback.current_document_id())
            rows = [
                CardRow(
                    id=r.id,
                    title=r.description or r.name,
                    subtitle=e.rules.scope_text(r.scope),
                    selected=r.enabled,
                    data={"path": r.path, "tier": r.tier, "name": r.name},
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
                    title=t(f"models.family.{m.family}") if m.family else m.name,
                    subtitle=t("models.row", size=_size_label(m.size_mb), ram=f"{m.ram_gb:g}"),
                    value=m.fit,
                    selected=m.installed,
                    data={"installed": m.installed, "fit": m.fit, "size_mb": m.size_mb, "ram_gb": m.ram_gb,
                          "kind": getattr(m, "kind", "tts"), "family": m.family, "size": m.size,
                          "precision": m.precision, "chosen": m.selected, "status": m.status},
                )
                for m in e.models.list().items
            ]  # fmt: skip
            return Reply(
                t("reply.models"), card=AssistantCard(turn_id=turn_id, kind="models", title=t("card.models"), rows=rows)
            )
        if what == "settings":
            return Reply(t("reply.settings"), card=self.settings_card(turn_id))
        return Reply(t("reply.can_show"))

    def voices_card(self, turn_id: str, *, more: bool = False, provider: str | None = None) -> Reply:
        """The voices card (09 § Voices). Not every voice: the current one, a few Google voices, the best local
        voices and the user's clones, in the interface language, and a row that lists the rest ("more voices").
        `more` is that list (optionally one provider's)."""
        e = self.e
        language = resolve_language(e.settings.get().language)
        current = e.documents.voice_for(e.playback.current_document_id())
        sl = shortlist_voices(e, language, current)
        voices = sl.rest if more else sl.short
        if provider:
            voices = [v for v in voices if v.provider == provider]
        rows = [_voice_row(v, current, e) for v in voices]
        if more:
            text = tn("reply.voices_more", len(rows)) if rows else t("reply.no_more_voices")
            title = t("card.voices_more") if not provider else t("card.voices_of", name=_provider_label(provider))
            return Reply(text, card=AssistantCard(turn_id=turn_id, kind="voices", title=title, rows=rows))
        if sl.rest:
            counts: dict[str, int] = {}
            for v in sl.rest:
                counts[v.provider] = counts.get(v.provider, 0) + 1
            rows.append(
                CardRow(
                    id="voices:more",
                    title=t("voices.more"),
                    subtitle=" · ".join(f"{n} {_provider_label(p)}" for p, n in counts.items()),
                    value=str(len(sl.rest)),
                    data={"command": t("voices.more_command"), "count": len(sl.rest)},
                )
            )
        if e.clones.available() is None:  # ＋ "Clone a voice": the same as saying "clone my voice"
            rows.append(
                CardRow(
                    id="voices:clone",
                    title=t("voices.clone"),
                    data={"command": t("voices.clone_command"), "icon": "clone"},
                )
            )
        others = len([v for v in voices if v.id != (sl.current.id if sl.current else None)])
        if sl.current is not None:
            text = t("reply.voice_current", name=voice_display(sl.current)[0]) + " " + tn("reply.voices_others", others)
        else:
            text = tn("reply.voices_short", others)
        return Reply(text, card=AssistantCard(turn_id=turn_id, kind="voices", title=t("card.voices"), rows=rows))

    def settings_card(self, turn_id: str) -> AssistantCard:
        """The handful of settings people change, as rows the bar draws with icons and switches."""
        st = self.e.settings.get()
        voices = {v.id: v for v in self.e.registry.voices()}
        effective = self.e.voices.effective(st.defaults.voice)
        voice = voices.get(effective)
        voice_name = voice_display(voice)[0] if voice else effective
        if st.defaults.voice == AUTO_VOICE:
            voice_name = t("voice.auto", name=voice_name)  # "Automatic (Zephyr)"

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
            row("defaults.voice", voice_name, voice_id=st.defaults.voice, effective_voice_id=effective),
            row("defaults.speed", st.defaults.speed, min=0.5, max=5.0, step=0.25),
            row("reader.font_size", st.reader.font_size, min=TEXT_SIZES[0], max=TEXT_SIZES[1], step=2),
            *self.qwen_setting_rows(row),
        ]
        return AssistantCard(turn_id=turn_id, kind="settings", title=t("card.settings"), rows=rows)

    def qwen_setting_rows(self, row: Any) -> list[CardRow]:
        """"Local voice size" and "Local voice precision", only while a Qwen voice is in use or Qwen weights are on
        this Mac. Each option says what switching to it would download (its size), if anything."""
        from open_speech.providers.qwen3 import FAMILY_CLONE, FAMILY_PRESET, PRECISIONS, SIZES, resolve_variant

        e = self.e
        qwen = e.registry.qwen3()
        if qwen is None or not qwen.voices():
            return []
        st = e.settings.get()
        in_use = [st.defaults.voice, st.defaults.assistant_voice, e.voices.current_default()]
        installed = [v for v in e.catalog.weights_variants("qwen3") if qwen.installed(v)]
        if not installed and not any(str(x).startswith("qwen3:") for x in in_use):
            return []
        family = FAMILY_CLONE if any(str(x).startswith("qwen3:clone:") for x in in_use) else FAMILY_PRESET
        res = qwen.resolve(family)
        if res is None:
            return []
        variants = e.catalog.weights_variants("qwen3")

        def download(size: str, precision: str) -> dict[str, Any] | None:
            r = resolve_variant(variants, family, size, precision, installed=lambda _v: False,
                                memory_gb=qwen.memory_gb(), defaults=qwen.extra)
            if r is None or qwen.installed(r.wanted):
                return None
            mb = r.wanted.size_mb
            return {"model_id": r.wanted.model, "size": _size_label(mb), "size_mb": round(mb)}

        size, precision = res.wanted.size, res.wanted.precision
        sizes = [x for x in SIZES if any(v.family == family and v.size == x for v in variants)]
        precisions = [x for x in reversed(PRECISIONS) if any(v.family == family and v.precision == x for v in variants)]
        size_dl = {x: d for x in sizes if (d := download(x, precision)) is not None}
        prec_dl = {x: d for x in precisions if (d := download(size, x)) is not None}
        return [
            row("qwen3.size", size, sizes, auto=st.qwen3.size == "auto", downloads=size_dl, family=family),
            row("qwen3.precision", precision, precisions, auto=st.qwen3.precision == "auto", downloads=prec_dl,
                family=family),
        ]


# ---------------------------------------------------------------------------------------------- the voices card

# What the short list offers, from the listener's seat (09 § Voices): six distinct Google Chirp voices (the
# default Zephyr first; bright and warm, female and male), Kokoro's best-rated voices for the language (US and
# UK English; Spanish), and for any other local provider (Qwen's presets) its first few voices in the language.
FEATURED_STARS = ("Zephyr", "Kore", "Aoede", "Charon", "Puck", "Achernar")
FEATURED_LOCAL: dict[str, tuple[str, ...]] = {
    "kokoro": ("af_heart", "af_bella", "bf_emma", "am_michael", "bm_george", "ef_dora", "em_alex"),
}
LOCAL_PER_PROVIDER = 4
CLONE_MODELS = ("clone", "clones")  # the user's own voices ("qwen3:clone:<slug>"): always on the short list


@dataclass
class VoiceShortlist:
    current: Voice | None
    short: list[Voice]  # the current voice first
    rest: list[Voice]  # every other available voice that speaks the language


def shortlist_voices(e: Engine, language: str, current_id: str | None) -> VoiceShortlist:
    """The voices card's short list in `language` and the rest (see `FEATURED_STARS`). Provider-agnostic: any
    installed or available local voice can be on it."""
    voices = [v for v in e.registry.voices() if v.available]
    by_id = {v.id: v for v in voices}
    speaks = [v for v in voices if e.voices.speaks(v, language)]
    current = by_id.get(current_id) if current_id else None
    short: list[Voice] = [current] if current is not None else []
    seen = {v.id for v in short}

    def add(v: Voice) -> None:
        if v.id not in seen:
            short.append(v)
            seen.add(v.id)

    google = [v for v in speaks if v.provider == "google" and v.persona and v.languages]
    if google:
        auto = e.registry.auto_cloud_voice(language)
        locale = by_id[auto].languages[0] if auto in by_id and by_id[auto].languages else google[0].languages[0]
        stars = {str(v.persona).rsplit(":", 1)[-1]: v for v in google if v.languages[0] == locale}
        for star in FEATURED_STARS:
            if star in stars:
                add(stars[star])
    local = [v for v in speaks if v.local and v.model not in CLONE_MODELS]
    for prov in dict.fromkeys(v.provider for v in local):
        mine = [v for v in local if v.provider == prov]
        featured = FEATURED_LOCAL.get(prov)
        if featured:
            names = {v.id.rsplit(":", 1)[-1]: v for v in mine}
            for name in featured:
                if name in names:
                    add(names[name])
        else:
            for v in mine[:LOCAL_PER_PROVIDER]:
                add(v)
    for v in voices:
        if v.model in CLONE_MODELS:
            add(v)
    rest = [v for v in speaks if v.id not in seen]
    rest.sort(key=lambda v: (v.provider != "google", not v.local, v.provider))  # Google, then local, then other
    return VoiceShortlist(current, short, rest)


def _voice_row(v: Voice, current: str | None, e: Engine | None = None) -> CardRow:
    title, detail = voice_display(v)
    data: dict[str, Any] = {"local": v.local, "provider": v.provider, "languages": v.languages[:4]}
    if v.model in CLONE_MODELS and e is not None:
        rec = e.clones.store.get(v.id.rsplit(":", 1)[-1])
        data.update(clone=True, slug=v.id.rsplit(":", 1)[-1], source=rec.source if rec else "recording",
                    delete_command=t("voices.delete_command", name=title))
    return CardRow(id=v.id, title=title, subtitle=detail, selected=v.id == current, data=data)


def _clone_actions() -> frozenset[str]:
    from open_speech.service.clones import ACTION_KINDS

    return ACTION_KINDS


def _size_label(mb: float) -> str:
    """"1.9 GB" / "642 MB" (download sizes on the models card)."""
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def _provider_label(provider: str) -> str:
    return PROVIDER_LABELS.get(provider, provider.capitalize())


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


__all__ = ["Commands", "EngineError", "Reply", "ToAgent", "Turn", "VoiceShortlist", "shortlist_voices", "voice_display"]

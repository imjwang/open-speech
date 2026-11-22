"""Rules use cases (04 § Rules and skills; 06)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from open_speech.api import schemas as s
from open_speech.errors import EngineError, NotFound

if TYPE_CHECKING:
    from open_speech.document.model import Document
    from open_speech.rules.model import Rule
    from open_speech.service.engine import Engine

log = logging.getLogger("open_speech.rules")


class RuleService:
    def __init__(self, engine: Engine) -> None:
        self.e = engine

    # ------------------------------------------------------------------ queries

    def _doc(self, document_id: str | None) -> Document | None:
        did = document_id or self.e.playback.current_document_id()
        if not did:
            return None
        try:
            return self.e.documents.load(did)
        except EngineError:
            return None

    def list(self, *, document_id: str | None = None) -> s.RuleList:
        doc = self._doc(document_id) if document_id else None
        rules = self.e.rule_store.rules_for(doc) if doc is not None else self.e.rule_store.all()
        return s.RuleList(items=[r.to_out() for r in rules], conflicts=self.e.rule_store.conflicts(rules))

    def presets(self) -> s.PresetsState:
        from open_speech.rules.presets import preset_out

        out = [preset_out(r) for r in self.e.rule_store.presets()]
        overrides = [s.ModeOverride(scope=sc, mode=m) for sc, m in self.e.rule_store.mode_overrides()]  # type: ignore[arg-type]
        return s.PresetsState(mode=self.e.settings.get().defaults.mode, overrides=overrides, presets=out)

    def lexicon(self) -> s.LexiconList:
        return s.LexiconList(items=list(self.e.rule_store.lexicon().entries))

    # ------------------------------------------------------------------ proposals (06 § Speech diff)

    async def propose(self, req: s.RuleProposalRequest, *, turn_id: str | None = None) -> s.RuleProposal:
        from open_speech.rules.compiler import compile_rule

        doc = self._doc(req.document_id)
        llm = self.e.registry.llm()
        if llm is not None and not self.e.budget.allow_paid():
            llm = None
        rule, call = await compile_rule(
            req.instruction,
            site=doc.site if doc else None,
            doc_kind=doc.kind.value if doc else None,
            doc_id=doc.id if doc else None,
            scope_hint=req.scope_hint,
            llm=llm,
        )
        if call is not None:
            self.e.record_llm("compile", call, document_id=doc.id if doc else None, turn_id=turn_id)
        if req.scope is not None:
            rule.scope = req.scope
        return await self._proposal_for(rule, doc, turn_id=turn_id, kind="save_rule")

    async def _proposal_for(
        self, rule: Rule, doc: Document | None, *, turn_id: str | None, kind: str, extra: dict[str, Any] | None = None
    ) -> s.RuleProposal:
        from open_speech.plan.diff import speech_diff

        diff = s.SpeechDiff(summary="")
        conflicts: list[str] = []
        if doc is not None:
            current = self.e.rule_store.rules_for(doc)
            llm = self.e.registry.llm() if rule.tier != "filter" and self.e.budget.allow_paid() else None
            diff, calls = await speech_diff(
                doc,
                current=current,
                proposed=rule,
                lexicon=self.e.rule_store.lexicon(),
                speech_md=self.e.rule_store.speech_md(),
                llm=llm,
            )
            for op, c in calls:
                self.e.record_llm(op, c, document_id=doc.id, turn_id=turn_id)
            conflicts = self.e.rule_store.conflicts([*current, rule])
        summary = diff.summary or _scope_summary(rule)
        if conflicts:
            summary += f" It conflicts with: {', '.join(conflicts)}. Which one should I keep?"
        payload = {"rule": rule.model_dump(mode="json"), **(extra or {})}
        action_id = self.e.assistant.create_action(
            kind=kind,
            summary=f"Save rule · {rule.description or rule.name} · {_scope_label(rule)}",
            spoken=summary,
            payload=payload,
            preview=diff,
            turn_id=turn_id,
        )
        return s.RuleProposal(
            proposal_id=action_id, rule=rule.to_out(), diff=diff, conflicts=conflicts, summary=summary
        )

    # ------------------------------------------------------------------ mutations

    async def save(self, req: s.SaveRuleRequest) -> s.RuleOut:
        from open_speech.rules.model import Rule

        if req.proposal_id:
            row = self.e.db.one("SELECT * FROM actions WHERE id=?", (req.proposal_id,))
            if row is None:
                raise NotFound("that proposal")
            if row["status"] != "pending":
                raise EngineError("conflict", f"That proposal was already {row['status']}.")
            rule = Rule.model_validate(json.loads(row["payload_json"])["rule"])
            saved = await self.save_rule(rule)
            self.e.assistant.mark_action(req.proposal_id, "confirmed", "Saved.")
            return saved
        if req.rule is None:
            raise EngineError("invalid_request", "Send a proposal_id or a rule.")
        rule = Rule.from_out(req.rule) if hasattr(Rule, "from_out") else Rule.model_validate(req.rule.model_dump())
        return await self.save_rule(rule)

    async def save_rule(self, rule: Rule) -> s.RuleOut:
        saved = self.e.rule_store.save(rule)
        await self.rules_changed("rule saved")
        return saved.to_out()

    async def patch(self, rule_id: str, p: s.RulePatch) -> s.RuleOut:
        try:
            rule = self.e.rule_store.get(rule_id)
        except KeyError as e:
            raise NotFound("that rule") from e
        upd = rule.model_copy(
            update={
                k: v
                for k, v in p.model_dump(exclude_none=True).items()
                if k in ("enabled", "tier", "instruction", "description")
            }
        )
        if p.scope is not None:
            upd.scope = p.scope
        saved = self.e.rule_store.save(upd)
        await self.rules_changed("rule edited")
        return saved.to_out()

    async def delete(self, rule_id: str) -> None:
        try:
            self.e.rule_store.delete(rule_id)
        except KeyError as e:
            raise NotFound("that rule") from e
        await self.rules_changed("rule deleted")

    async def update_presets(self, u: s.PresetsUpdate) -> s.PresetsState:
        for name, enabled in u.presets.items():
            try:
                self.e.rule_store.set_preset(name, enabled)
            except KeyError as e:
                raise NotFound(f"a preset called {name}") from e
        if u.mode is not None:
            if u.scope is None:
                await self.e.settings.set_key("defaults.mode", u.mode)
            else:
                self.e.rule_store.set_mode_override(u.scope, u.mode)
        await self.rules_changed("presets changed")
        return self.presets()

    async def set_preset(self, name: str, enabled: bool) -> None:
        await self.update_presets(s.PresetsUpdate(presets={name: enabled}))

    async def set_mode(self, mode: str, scope: s.RuleScope | None = None) -> None:
        await self.update_presets(s.PresetsUpdate(mode=mode, scope=scope))  # type: ignore[arg-type]

    async def add_lexicon(self, entry: s.LexiconEntry) -> s.LexiconList:
        if not entry.term.strip() or not entry.spoken.strip():
            raise EngineError("invalid_request", "Both the word and how to say it are needed.")
        self.e.rule_store.lexicon().add(entry)
        await self.rules_changed("lexicon changed")
        return self.lexicon()

    async def delete_lexicon(self, term: str) -> s.LexiconList:
        self.e.rule_store.lexicon().remove(term)
        await self.rules_changed("lexicon changed")
        return self.lexicon()

    async def rules_changed(self, reason: str) -> None:
        """Re-plan the open document from the playhead; others re-plan lazily on next open (plan cache)."""
        self.e.rule_store.reload()
        self.e.documents.invalidate_all()
        did = self.e.playback.current_document_id()
        if did:
            try:
                await self.e.documents.replan(did, reason)
                await self.e.playback.on_script_changed(did)
            except EngineError as err:
                log.warning("re-plan after %s failed: %s", reason, err.detail)

    # ------------------------------------------------------------------ preset proposals (fast path)

    def find_preset(self, name_or_id: str) -> Rule | None:
        from open_speech.rules.presets import preset_spec

        spec = preset_spec(name_or_id)
        key = spec.name if spec else name_or_id
        return next((r for r in self.e.rule_store.presets() if key in (r.name, r.id)), None)

    async def propose_preset(self, preset_name: str, enabled: bool, *, turn_id: str | None = None) -> s.RuleProposal:
        from open_speech.rules.presets import preset_out

        target = self.find_preset(preset_name)
        if target is None:
            raise NotFound(f"a preset called {preset_name}")
        title = preset_out(target).name
        doc = self._doc(None)
        proposed = target.model_copy(update={"enabled": enabled})
        if enabled:
            prop = await self._proposal_for(
                proposed, doc, turn_id=turn_id, kind="set_preset", extra={"preset": target.name, "enabled": True}
            )
            if not prop.diff.summary:
                prop.summary = f"That skips {_noun(title)} everywhere. Save it?"
        else:
            action_id = self.e.assistant.create_action(
                kind="set_preset",
                summary=f"Read {_noun(title)} again",
                spoken=f"I'll read {_noun(title)} again. OK?",
                payload={"preset": target.name, "enabled": False},
                preview=None,
                turn_id=turn_id,
            )
            prop = s.RuleProposal(
                proposal_id=action_id,
                rule=proposed.to_out(),
                diff=s.SpeechDiff(),
                summary=f"I'll read {_noun(title)} again. OK?",
            )
        return prop


def _scope_label(rule: Rule) -> str:
    sc = rule.scope
    if sc.docs:
        return "this document"
    if sc.sites:
        return ", ".join(sc.sites)
    if sc.types:
        return ", ".join(t.upper() if t == "pdf" else t for t in sc.types)
    return "everywhere"


def _scope_summary(rule: Rule) -> str:
    label = _scope_label(rule)
    what = rule.description or rule.name
    where = "everywhere" if label == "everywhere" else f"for {label}"
    return f"{what.rstrip('.')}, {where}. Save it?"


def _noun(title: str) -> str:
    """How a preset is said mid-sentence: \"page numbers\", but \"URLs\"."""
    return title if any(c.isupper() for c in title[1:]) else title.lower()

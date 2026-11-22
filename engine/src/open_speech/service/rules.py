"""Rules use cases (04 § Rules and skills; 06)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from open_speech.api import schemas as s
from open_speech.errors import EngineError, NotFound
from open_speech.i18n import has, t

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

    @staticmethod
    def preset_noun(title: str) -> str:
        return _noun(title)

    @staticmethod
    def _label(rule: Rule) -> str:
        if rule.preset:
            from open_speech.rules.presets import preset_out

            return t("action.skip", what=_noun(preset_out(rule).name))
        return rule.description or rule.name

    @staticmethod
    def scope_text(scope: s.RuleScope) -> str:
        """Where a rule applies, in a few words."""
        if scope.docs:
            return t("scope.document")
        if scope.sites:
            return ", ".join(scope.sites)
        if scope.types:
            return ", ".join(x.upper() if x == "pdf" else x for x in scope.types)
        if scope.languages:
            return ", ".join(scope.languages)
        return t("scope.everywhere")

    def lexicon(self) -> s.LexiconList:
        return s.LexiconList(items=list(self.e.rule_store.lexicon().entries))

    # ------------------------------------------------------------------ rule files (the rules editor)

    def _rule_file(self, rule: Rule) -> s.RuleFile:
        return s.RuleFile(
            id=rule.id,
            kind="preset" if rule.preset else "rule",
            name=rule.name,
            path=str(rule.path or self.e.paths.rules / f"{rule.name}.md"),
            description=rule.description,
            enabled=rule.enabled,
            scope=rule.scope,
            tier=rule.tier,
            updated_at=rule.to_out().updated_at,
        )

    def _special_file(self, file_id: str) -> s.RuleFile | None:
        paths = self.e.paths
        if file_id == "preferences":
            return s.RuleFile(
                id="preferences", kind="preferences", name=t("rules.preferences"), path=str(paths.speech_md)
            )
        if file_id == "lexicon":
            return s.RuleFile(id="lexicon", kind="lexicon", name=t("rules.pronunciations"), path=str(paths.lexicon))
        return None

    def files(self) -> s.RuleFileList:
        """Every text file behind the rules: preferences first, pronunciations, the user's rules, presets."""
        rules = self.e.rule_store.all()
        own = sorted((r for r in rules if not r.preset), key=lambda r: r.name)
        presets = sorted((r for r in rules if r.preset), key=lambda r: r.name)
        items = [self._special_file("preferences"), self._special_file("lexicon")]
        return s.RuleFileList(
            items=[f for f in items if f is not None] + [self._rule_file(r) for r in (*own, *presets)],
            errors=self.e.rule_store.errors(),
        )

    def read_file(self, file_id: str) -> s.RuleFileContent:
        from pathlib import Path

        special = self._special_file(file_id)
        if special is not None:
            path = Path(special.path)
            text = path.read_text("utf-8") if path.exists() else ""
            body = text
            if file_id == "preferences":
                from open_speech.rules.files import LEGACY_SPEECH_MD

                if text.strip() == LEGACY_SPEECH_MD.strip():  # only the old explanatory comment: nothing written yet
                    body = ""
            return s.RuleFileContent(**special.model_dump(), text=text, body=body)
        rule = self.e.rule_store.get(file_id)
        info = self._rule_file(rule)
        text = Path(info.path).read_text("utf-8") if Path(info.path).exists() else ""
        return s.RuleFileContent(**info.model_dump(), text=text, body=rule.instruction)

    async def write_file(self, file_id: str | None, w: s.RuleFileWrite) -> s.RuleFileContent:
        """Save a rule file from the editor (or the assistant). A rule's `body` is re-compiled, so what it says
        and how it is applied never drift apart; `text` replaces the file as written, after it parses."""
        from open_speech.rules.files import _atomic_write, rule_from_text
        from open_speech.rules.lexicon import Lexicon

        store = self.e.rule_store
        if file_id in ("preferences", "lexicon"):
            content = w.text if w.text is not None else w.body
            if content is None:
                raise EngineError("invalid_request", t("rules.nothing_to_save"))
            if file_id == "lexicon":
                try:
                    Lexicon(Lexicon.parse(content), self.e.paths.lexicon).save()
                except ValueError as e:
                    raise EngineError("invalid_rule", str(e)) from e
            else:
                _atomic_write(self.e.paths.speech_md, content if content.endswith("\n") else content + "\n")
            await self.rules_changed(f"{file_id} edited")
            return self.read_file(file_id)

        existing = store.get(file_id) if file_id else None
        rule: Rule | None
        if w.text is not None:
            try:
                rule = rule_from_text(w.text, path=existing.path if existing else None)
            except (ValueError, ValidationError) as e:
                raise EngineError("invalid_rule", t("rules.invalid_file", reason=str(e))) from e
            if existing is None and not w.name and rule.name == "rule":
                rule = rule.model_copy(update=_named(rule.instruction or rule.description))
        else:
            if w.body is None and existing is None:
                raise EngineError("invalid_request", t("rules.nothing_to_save"))
            rule = await self._rule_from_body(existing, w.body) if w.body is not None else existing
        assert rule is not None
        changes: dict[str, Any] = {}
        if w.name:
            from open_speech.rules.model import rule_id_for, slugify

            name = slugify(w.name)
            changes.update(name=name, id=rule_id_for(name))
        if w.enabled is not None:
            changes["enabled"] = w.enabled
        if w.scope is not None:
            changes["scope"] = w.scope
        if existing is not None and existing.path is not None:
            changes.setdefault("path", existing.path)  # lets the store remove the old file on a rename
        saved = store.save(rule.model_copy(update=changes))
        await self.rules_changed("rule edited")
        return self.read_file(saved.id)

    async def _rule_from_body(self, existing: Rule | None, body: str) -> Rule:
        """Work out how to apply an instruction the user typed. Without an LLM in this build, an instruction
        the offline compiler can't express is kept as written and marked for AI Enhanced cleanup."""
        from open_speech.errors import Unavailable
        from open_speech.rules.compiler import compile_rule
        from open_speech.rules.model import Rule as RuleModel

        if not body.strip():
            raise EngineError("invalid_rule", t("rules.empty"))
        llm = self.e.registry.llm() if self.e.budget.allow_paid() else None
        try:
            compiled, call = await compile_rule(
                body, site=None, doc_kind=None, doc_id=None, scope_hint=None, llm=llm, created_by="user"
            )
            if call is not None:
                self.e.record_llm("compile", call)
        except Unavailable:
            compiled = RuleModel.new(
                _named(body)["name"], instruction=body.strip(), description=_first_line(body), tier="annotate"
            )
        if existing is None:
            return compiled
        # Keep what the user set on the file; take what the instruction now means.
        return existing.model_copy(
            update={
                "instruction": body.strip(),
                "tier": compiled.tier,
                "compiled": compiled.compiled,
                "description": existing.description if existing.preset else compiled.description,
            }
        )

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
            summary += " " + t("rules.conflicts", which=", ".join(conflicts))
        payload = {"rule": rule.model_dump(mode="json"), **(extra or {})}
        action_id = self.e.assistant.create_action(
            kind=kind,
            # A built-in preset is named in the user's language; a rule the user wrote, in their own words.
            summary=f"{self._label(rule)} · {self.scope_text(rule.scope)}",
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
            raise EngineError("invalid_request", t("error.pronunciation_incomplete"))
        self.e.rule_store.lexicon().add(entry)
        await self.rules_changed("lexicon changed")
        return self.lexicon()

    async def delete_lexicon(self, term: str) -> s.LexiconList:
        self.e.rule_store.lexicon().remove(term)
        await self.rules_changed("lexicon changed")
        return self.lexicon()

    async def rules_changed(self, reason: str) -> None:
        """Re-plan the open document from the playhead; others re-plan lazily on next open (plan cache)."""
        from open_speech.api.ws_schema import RulesChanged

        self.e.rule_store.reload()
        self.e.documents.invalidate_all()
        self.e.events.publish(RulesChanged(reason=reason))
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
                prop.summary = t("ask.skip", what=_noun(title))
        else:
            action_id = self.e.assistant.create_action(
                kind="set_preset",
                summary=t("action.read_again", what=_noun(title)),
                spoken=t("ask.read_again", what=_noun(title)),
                payload={"preset": target.name, "enabled": False},
                preview=None,
                turn_id=turn_id,
            )
            prop = s.RuleProposal(
                proposal_id=action_id,
                rule=proposed.to_out(),
                diff=s.SpeechDiff(),
                summary=t("ask.read_again", what=_noun(title)),
            )
        return prop


def _first_line(text: str) -> str:
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return line if len(line) <= 120 else line[:117].rsplit(" ", 1)[0] + "…"


def _named(text: str) -> dict[str, str]:
    """A file name for a new rule, from the first words of what it says."""
    from open_speech.rules.model import rule_id_for, slugify

    name = slugify(" ".join(_first_line(text).split()[:6]), max_len=48)
    return {"name": name, "id": rule_id_for(name)}


def _scope_summary(rule: Rule) -> str:
    from open_speech.rules.scope import describe_scope

    what = (rule.description or rule.name).rstrip(".")
    return t("ask.save_rule", what=what, scope=describe_scope(rule.scope))


def _noun(title: str) -> str:
    """How a preset is said mid-sentence, in the user's language: "page numbers", but "URLs"."""
    key = f"preset.{title.lower().replace(' ', '_')}"
    if has(key):
        return t(key)
    return title if any(c.isupper() for c in title[1:]) else title.lower()

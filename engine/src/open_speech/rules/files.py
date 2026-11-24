"""Rule files on disk (06 § Three kinds of file): `rules/SPEECH.md` (the reading instructions), `rules/*.md` and
`rules/lexicon.csv`. Hand edits are picked up by `watch()` within 1 s (FR-RULE-3).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from open_speech.api.schemas import RuleScope
from open_speech.config import Paths
from open_speech.document.model import BlockType, Document, MarkKind
from open_speech.errors import EngineError, NotFound
from open_speech.rules.hashing import compute_rules_hash
from open_speech.rules.lexicon import Lexicon
from open_speech.rules.model import CompiledFilter, Rule, name_from_rule_id, rule_id_for, slugify
from open_speech.rules.prompt import default_prompt, derive_rules, prompt_from_presets
from open_speech.rules.scope import by_precedence, level_reason, scope_level, scope_matches

log = logging.getLogger("open_speech.rules")

# Earlier versions copied 13 presets into rules/ and recorded them here, and kept per-scope Classic / AI Enhanced
# overrides in modes.toml. Both are retired on upgrade: the presets become lines of the reading instructions.
INSTALLED_PRESETS_FILE = ".installed-presets"
LEGACY_MODES_FILE = "modes.toml"
_SKIP_FILES = {"speech.md", "readme.md"}

# Earlier versions started SPEECH.md with an explanatory comment (and later empty); it is recognised on upgrade
# so the user's own words, if any, are kept.
LEGACY_SPEECH_MD = """<!--
SPEECH.md: standing preferences for how Open Speech reads to you, in your own words.
Everything outside this comment is included in every AI Enhanced (Claude) plan call. Examples:

I'm a nurse; read drug names carefully.
Never read legal disclaimers.
Spell out abbreviations the first time they appear.
-->
"""

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

BLOCK_NOUNS: dict[BlockType, str] = {
    BlockType.heading: "headings",
    BlockType.paragraph: "paragraphs",
    BlockType.list_item: "list items",
    BlockType.quote: "quotes",
    BlockType.code: "code blocks",
    BlockType.table: "tables",
    BlockType.caption: "captions",
    BlockType.figure: "figures",
    BlockType.footnote: "footnotes",
    BlockType.header: "headers",
    BlockType.footer: "footers",
    BlockType.page_number: "page numbers",
    BlockType.equation: "equations",
    BlockType.toc: "table of contents entries",
    BlockType.reference_entry: "reference list entries",
}
MARK_NOUNS: dict[MarkKind, str] = {
    MarkKind.citation_ref: "citations",
    MarkKind.link: "links",
    MarkKind.url: "URLs",
    MarkKind.emphasis: "emphasized text",
    MarkKind.parenthetical: "parentheses",
    MarkKind.bracket: "brackets",
    MarkKind.brace: "braces",
    MarkKind.formula: "formulas",
    MarkKind.footnote_ref: "footnote markers",
}


# ------------------------------------------------------------------ file format


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter dict, body). Raises ValueError on malformed YAML."""
    if text.startswith("﻿"):
        text = text[1:]
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            try:
                meta = yaml.safe_load("".join(lines[1:i])) or {}
            except yaml.YAMLError as e:
                raise ValueError(f"invalid YAML frontmatter: {e}") from e
            if not isinstance(meta, dict):
                raise ValueError("frontmatter must be a mapping")
            return meta, "".join(lines[i + 1 :]).strip()
    raise ValueError("frontmatter is not closed with ---")


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


def rule_from_text(text: str, *, path: Path | None = None, mtime: float = 0.0) -> Rule:
    """Parse one rule file. Raises ValueError with a readable reason."""
    meta, body = split_frontmatter(text)
    stem = path.stem if path else "rule"
    name = slugify(str(meta.get("name") or stem))
    scope_raw = meta.get("scope") or {}
    if not isinstance(scope_raw, dict):
        raise ValueError("scope must be a mapping")
    unknown = set(scope_raw) - {"sites", "types", "docs", "languages"}
    if unknown:
        raise ValueError(f"unknown scope keys: {', '.join(sorted(unknown))}")
    scope = RuleScope(**{k: _as_list(v) for k, v in scope_raw.items()})
    tier = meta.get("tier")
    if tier is not None and tier not in ("filter", "annotate", "transform"):
        raise ValueError(f"tier must be filter, annotate or transform (got {tier!r})")
    compiled_raw = meta.get("compiled")
    try:
        compiled = CompiledFilter.model_validate(compiled_raw) if compiled_raw else None
    except ValidationError as e:
        raise ValueError(f"invalid compiled filter: {e.errors()[0]['msg']}") from e
    created_by = str(meta.get("created_by") or "user")
    enabled = meta.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be true or false")
    rule = Rule(
        id=rule_id_for(name),
        name=name,
        description=str(meta.get("description") or ""),
        instruction=body,
        scope=scope,
        enabled=enabled,
        tier=tier or "filter",
        compiled=compiled,
        created_by=created_by,
        preset=created_by == "preset",
        path=path,
        updated_at=mtime,
    )
    if tier is None or (tier == "filter" and compiled is None):
        rule = _compile_on_load(rule)
    return rule


def _compile_on_load(rule: Rule) -> Rule:
    """06: hand-written rules with no tier are compiled on first load (deterministically here; rules the
    offline compiler can't express are flagged `needs_compile` for the service to send to Claude)."""
    from open_speech.rules.compiler import compile_offline

    draft = compile_offline(rule.instruction or rule.description)
    if draft is not None and draft.compiled is not None:
        return rule.model_copy(update={"tier": "filter", "compiled": draft.compiled, "needs_compile": False})
    return rule.model_copy(update={"tier": "annotate", "compiled": None, "needs_compile": True})


def rule_to_text(rule: Rule) -> str:
    scope = {
        k: v
        for k, v in rule.scope.model_dump().items()
        if v  # omit empty keys; an empty scope is written as {}
    }
    meta: dict[str, Any] = {
        "name": rule.name,
        "description": rule.description,
        "scope": scope,
        "enabled": rule.enabled,
        "tier": rule.tier,
        "compiled": rule.compiled.to_file_dict() if rule.compiled is not None else None,
        "created_by": rule.created_by,
    }
    front = yaml.safe_dump(meta, sort_keys=False, default_flow_style=None, allow_unicode=True, width=100)
    body = rule.instruction.strip()
    return f"---\n{front}---\n{body}\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, "utf-8")
    os.replace(tmp, path)


# ------------------------------------------------------------------ store


class RuleStore:
    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self.dir = paths.rules
        self._lock = threading.RLock()
        self._rules: dict[str, Rule] = {}
        self._errors: list[str] = []
        self._lexicon = Lexicon([], paths.lexicon)
        self._speech_md = ""
        self._derived: list[Rule] = []  # filter rules from the skip lines of SPEECH.md (rules/prompt.py)
        self._speech_rest = ""  # what Claude reads of SPEECH.md: no comments, no lines that became filters

    # ------------------------------------------------------------ lifecycle

    def ensure_defaults(self) -> None:
        """Write the default reading instructions on first run (or turn an earlier version's presets into
        them), create lexicon.csv if missing, then load everything."""
        self.dir.mkdir(parents=True, exist_ok=True)
        self._retire_presets()
        if not self.paths.speech_md.exists():
            _atomic_write(self.paths.speech_md, default_prompt())
        if not self.paths.lexicon.exists():
            Lexicon([], self.paths.lexicon).save()
        self.reload()

    def _retire_presets(self) -> None:
        """Upgrade from a version with presets and Classic / AI Enhanced modes: the presets the user had on
        become lines of SPEECH.md (what they had written there is kept under "About me"), and the preset files,
        their marker and modes.toml are removed."""
        marker = self.dir / INSTALLED_PRESETS_FILE
        if not marker.exists():
            return
        enabled: dict[str, bool] = {}
        for path in sorted(self.dir.glob("*.md")):
            if path.name.lower() in _SKIP_FILES:
                continue
            try:
                meta, _ = split_frontmatter(path.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            if meta.get("created_by") != "preset":
                continue
            enabled[slugify(str(meta.get("name") or path.stem))] = meta.get("enabled", True) is True
            path.unlink(missing_ok=True)
        # A preset the user deleted is off; one never installed keeps its default.
        for name in marker.read_text("utf-8").split():
            enabled.setdefault(name, False)
        old = self.paths.speech_md.read_text("utf-8") if self.paths.speech_md.exists() else ""
        about = "" if old.strip() == LEGACY_SPEECH_MD.strip() else old
        _atomic_write(self.paths.speech_md, prompt_from_presets(enabled, about))
        marker.unlink(missing_ok=True)
        (self.dir / LEGACY_MODES_FILE).unlink(missing_ok=True)
        log.info("reading instructions written from %d presets", len(enabled))

    def reload(self) -> None:
        rules: dict[str, Rule] = {}
        errors: list[str] = []
        if self.dir.exists():
            for path in sorted(self.dir.glob("*.md")):
                if path.name.lower() in _SKIP_FILES or path.name.startswith("."):
                    continue
                try:
                    text = path.read_text("utf-8")
                    mtime = path.stat().st_mtime
                    rule = rule_from_text(text, path=path, mtime=mtime)
                except (OSError, ValueError, ValidationError) as e:
                    errors.append(f"{path.name}: {e}")
                    log.warning("skipping rule file %s: %s", path.name, e)
                    continue
                if rule.id in rules:
                    # Two files claim the same name: fall back to the file stem for the second.
                    alt = slugify(path.stem)
                    if rule_id_for(alt) in rules:
                        errors.append(f"{path.name}: duplicate rule name {rule.name!r}")
                        continue
                    rule = rule.model_copy(update={"id": rule_id_for(alt), "name": alt})
                rules[rule.id] = rule
        lexicon = Lexicon.load(self.paths.lexicon)
        speech = self.paths.speech_md.read_text("utf-8") if self.paths.speech_md.exists() else ""
        mtime = self.paths.speech_md.stat().st_mtime if speech else 0.0
        derived, rest = derive_rules(speech, mtime=mtime)
        with self._lock:
            self._rules = rules
            self._errors = errors
            self._lexicon = lexicon
            self._speech_md = _HTML_COMMENT.sub("", speech).strip()
            self._derived = derived
            self._speech_rest = rest

    def errors(self) -> list[str]:
        """Addition: files that failed to parse (shown in the rules panel)."""
        with self._lock:
            return list(self._errors)

    # ------------------------------------------------------------ rules

    def all(self) -> list[Rule]:
        with self._lock:
            return by_precedence(self._rules.values())

    def get(self, rule_id: str) -> Rule:
        with self._lock:
            rule = self._rules.get(rule_id) or self._rules.get(rule_id_for(name_from_rule_id(rule_id)))
        if rule is None:
            raise NotFound(f"a rule called {name_from_rule_id(rule_id)}")
        return rule

    def save(self, rule: Rule) -> Rule:
        """Write `rules/<name>.md` (atomically) and return the rule with its path and mtime."""
        if rule.compiled is not None and rule.tier == "filter" and rule.compiled.is_empty():
            raise EngineError("invalid_rule", "That rule doesn't change anything, so I didn't save it.")
        path = self.dir / f"{rule.name}.md"
        with self._lock:
            previous = self._rules.get(rule.id)
            _atomic_write(path, rule_to_text(rule))
            if rule.path and rule.path != path and rule.path.exists() and rule.path.parent == self.dir:
                rule.path.unlink()
            if previous and previous.path and previous.path != path and previous.path.exists():
                previous.path.unlink()
            saved = rule.model_copy(
                update={
                    "id": rule_id_for(rule.name),
                    "path": path,
                    "updated_at": path.stat().st_mtime,
                    "preset": rule.created_by == "preset",
                    "needs_compile": False if rule.tier != "filter" or rule.compiled else rule.needs_compile,
                }
            )
            self._rules[saved.id] = saved
        return saved

    def delete(self, rule_id: str) -> None:
        rule = self.get(rule_id)
        with self._lock:
            if rule.path and rule.path.exists():
                rule.path.unlink()
            self._rules.pop(rule.id, None)

    def rules_for(self, doc: Document) -> list[Rule]:
        """Enabled rules whose scope matches the document, highest precedence first (06)."""
        return [r for r in self.all() if r.enabled and scope_matches(r.scope, doc)]

    def conflicts(self, rules: Sequence[Rule]) -> list[str]:
        """Human-readable conflicts between active rules ("read footnotes" vs "skip footnotes")."""
        return find_conflicts(rules)

    # ------------------------------------------------------------ what the planner gets

    def derived_rules(self) -> list[Rule]:
        """The filter rules the skip lines of SPEECH.md stand for (in memory only, never files)."""
        with self._lock:
            return list(self._derived)

    def plan_rules(self, doc: Document) -> list[Rule]:
        """What cleans up `doc`: its matching rule files plus the filters from SPEECH.md, by precedence."""
        derived = [r for r in self.derived_rules() if r.enabled and scope_matches(r.scope, doc)]
        return by_precedence([*self.rules_for(doc), *derived])

    def plan_speech_md(self) -> str:
        """What Claude reads of SPEECH.md: the file without comments and without the lines that became filters."""
        with self._lock:
            return self._speech_rest

    # ------------------------------------------------------------ SPEECH.md / lexicon / hash

    def speech_md(self) -> str:
        with self._lock:
            return self._speech_md

    def lexicon(self) -> Lexicon:
        with self._lock:
            return self._lexicon

    def rules_hash(self, rules: Sequence[Rule], *, document: Document | None = None) -> str:
        """Covers the given (document-matching) rules, the lexicon rows that apply to `document` (all rows
        when None) and SPEECH.md. `document` is an addition to the binding signature."""
        return compute_rules_hash(rules, self.lexicon().for_document(document), self.speech_md())

    # ------------------------------------------------------------ watching

    async def watch(
        self,
        on_change: Callable[[], Any],
        *,
        stop_event: asyncio.Event | None = None,
        debounce_ms: int = 200,
    ) -> None:
        """Reload on any change under `rules/` and call `on_change` (sync or async). Runs until cancelled or
        `stop_event` is set. Debounce 200 ms + 50 ms polling step keeps reloads well under 1 s."""
        from watchfiles import awatch

        self.dir.mkdir(parents=True, exist_ok=True)

        def _relevant(_change: object, path: str) -> bool:
            name = os.path.basename(path)
            return not (name.startswith(".") or name.endswith((".tmp", "~", ".swp")))

        async for _changes in awatch(
            self.dir,
            watch_filter=_relevant,
            debounce=debounce_ms,
            step=50,
            stop_event=stop_event,
            recursive=False,
        ):
            self.reload()
            try:
                result = on_change()
                if inspect.isawaitable(result):
                    await result
            except Exception:  # a failing listener must not stop the watcher
                log.exception("rules on_change callback failed")


# ------------------------------------------------------------------ conflicts


def _decisions(rule: Rule) -> list[tuple[str, str, str]]:
    """(target key, noun, "skip"|"read") decisions a compiled rule makes."""
    c = rule.compiled
    if c is None or rule.tier != "filter":
        return []
    out: list[tuple[str, str, str]] = []
    out += [(f"block:{b}", BLOCK_NOUNS[b], "skip") for b in c.drop_blocks]
    out += [(f"block:{b}", BLOCK_NOUNS[b], "read") for b in c.keep_blocks]
    out += [(f"mark:{m}", MARK_NOUNS[m], "skip") for m in c.drop_marks]
    out += [(f"mark:{m}", MARK_NOUNS[m], "read") for m in c.keep_marks]
    if c.keep_blocks_only is not None:
        keep = set(c.keep_blocks_only)
        out += [(f"block:{b}", BLOCK_NOUNS[b], "read") for b in keep]
        out += [(f"block:{b}", BLOCK_NOUNS[b], "skip") for b in BlockType if b not in keep]
    return out


def find_conflicts(rules: Sequence[Rule]) -> list[str]:
    ordered = by_precedence(r for r in rules if r.enabled)
    seen: dict[str, tuple[Rule, str, str]] = {}  # target → first (winning) decision
    messages: list[str] = []
    reported: set[tuple[str, str, str]] = set()
    for rule in ordered:
        for target, noun, verb in _decisions(rule):
            if target not in seen:
                seen[target] = (rule, noun, verb)
                continue
            winner, _, wverb = seen[target]
            if wverb == verb or winner.id == rule.id:
                continue
            key = (winner.id, rule.id, verb)
            if key in reported:
                continue
            reported.add(key)
            wl, ll = scope_level(winner.scope), scope_level(rule.scope)
            reason = level_reason(wl) if wl != ll else "it was edited more recently"
            messages.append(
                f"“{winner.name}” {'reads' if wverb == 'read' else 'skips'} {noun} but “{rule.name}” "
                f"{'reads' if verb == 'read' else 'skips'} them; “{winner.name}” wins because {reason}."
            )
    transforms = [r for r in ordered if r.tier == "transform"]
    if len(transforms) > 1:
        names = ", ".join(f"“{r.name}”" for r in transforms)
        messages.append(f"{names} all rewrite the document; only “{transforms[0].name}” applies (highest precedence).")
    return messages

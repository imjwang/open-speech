"""The reading instructions (06 § The reading instructions).

`rules/SPEECH.md` is the one prompt that drives cleanup, the way CLAUDE.md drives Claude Code. It ships with
the sections in `data/SPEECH.default.md`, and the user changes it in their own words, typed or through the
assistant.

A bullet whose first sentence is an obvious skip ("- Skip footnotes and footnote markers. …") is also compiled
here, with no call, into a filter rule held in memory only (`derive_rules`). That rule removes what it names
before Claude sees the text (Claude sees a `⟦footnote⟧` marker in its place), keeps doing so when Claude can't
be reached, and bounds what Claude may remove on its behalf (`plan/guard.py`). The rest of the file goes to
Claude as written. Deleting the bullet removes its filter: the file stays the only place to look.
"""

from __future__ import annotations

import re
from importlib.resources import files

from open_speech.rules.model import Rule

CREATED_BY = "speech_md"
DERIVED_PREFIX = "speech_"

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BULLET = re.compile(r"^\s*[-*+]\s+(?P<text>\S.*?)\s*$")
_FIRST_SENTENCE = re.compile(r"^(?P<head>.+?[.!?])(?:\s|$)")
_CLAUSE_BREAKS = (" — ", " – ", " - ", ": ", "; ")


def default_prompt() -> str:
    return files("open_speech").joinpath("data", "SPEECH.default.md").read_text("utf-8")


def strip_comments(text: str) -> str:
    return _HTML_COMMENT.sub("", text)


def _head(text: str) -> str:
    m = _FIRST_SENTENCE.match(text)
    head = m.group("head") if m else text
    for brk in _CLAUSE_BREAKS:
        head = head.split(brk, 1)[0]
    return head.strip()


def compile_bullet(text: str, *, mtime: float = 0.0) -> Rule | None:
    """The filter rule a bullet stands for, or None when only Claude can apply it."""
    from open_speech.rules.compiler import compile_offline

    rule = compile_offline(_head(text), created_by=CREATED_BY)
    c = rule.compiled if rule is not None else None
    if rule is None or c is None or rule.tier != "filter":
        return None
    # Only skips: "read X" lines and rewrites are judgment calls the whole text already carries to Claude.
    if not (c.drop_blocks or c.drop_marks) or c.keep_blocks or c.keep_marks or c.keep_blocks_only or c.replace:
        return None
    if rule.scope.docs:  # "in this document" means nothing in a file that applies to every document
        return None
    return rule.model_copy(
        update={
            "id": DERIVED_PREFIX + rule.name,
            "instruction": text.strip(),
            "preset": True,  # a built-in kind of span: OpGuard bounds what Claude removes for it
            "updated_at": mtime,
        }
    )


def derive_rules(speech_md: str, *, mtime: float = 0.0) -> tuple[list[Rule], str]:
    """(filter rules from the skip bullets, the text Claude gets: the file without comments and those bullets)."""
    rules: list[Rule] = []
    seen: set[str] = set()
    kept: list[str] = []
    for line in strip_comments(speech_md).splitlines():
        m = _BULLET.match(line)
        rule = compile_bullet(m.group("text"), mtime=mtime) if m else None
        if rule is None:
            kept.append(line)
            continue
        if rule.id not in seen:
            seen.add(rule.id)
            rules.append(rule)
    return rules, _drop_empty_sections(kept)


def _drop_empty_sections(lines: list[str]) -> str:
    """A heading left with nothing under it (every bullet became a filter) is left out of what Claude reads."""
    def level(ln: str) -> int:
        return len(ln) - len(ln.lstrip("#"))

    out: list[str] = []
    for i, line in enumerate(lines):
        if line.startswith("#"):
            nxt = next((ln for ln in lines[i + 1 :] if ln.strip()), None)
            if nxt is None or (nxt.startswith("#") and level(nxt) <= level(line)):
                continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


# ------------------------------------------------------------------ upgrade from presets

# The presets earlier versions shipped as rule files, and the line each one becomes in "## Leave out". A group
# whose members are all on keeps the default line (with its explanation); a partial group is spelled out.
_DEFAULT_GROUPS: tuple[tuple[tuple[tuple[str, str], ...], str], ...] = (
    ((("headers", "headers"), ("footers", "footers"), ("page-numbers", "page numbers")), "- Skip headers, footers"),
    ((("footnotes", "footnotes and footnote markers"),), "- Skip footnotes and footnote markers."),
    ((("citations", "citations"),), "- Skip citations."),
    ((("urls", "URLs"),), "- Skip URLs."),
)
_EXTRA_LINES: dict[str, str] = {
    "parentheses": "- Skip text in parentheses.",
    "brackets": "- Skip text in brackets.",
    "braces": "- Skip text in braces.",
    "tables": "- Skip tables.",
    "formulas": "- Skip formulas.",
    "captions": "- Skip captions.",
    "code": "- Skip code blocks.",
}


def _join(nouns: list[str]) -> str:
    return nouns[0] if len(nouns) == 1 else ", ".join(nouns[:-1]) + " and " + nouns[-1]


def prompt_from_presets(enabled: dict[str, bool], about: str = "") -> str:
    """The default reading instructions adjusted to the presets a user had on or off, with what they had
    written in SPEECH.md kept under "## About me". `enabled` maps preset names to their state; a preset that
    isn't in it keeps its default."""
    lines = default_prompt().splitlines()
    out: list[str] = []
    extras = [line for name, line in _EXTRA_LINES.items() if enabled.get(name, False)]
    for i, line in enumerate(lines):
        group = next((g for g in _DEFAULT_GROUPS if line.startswith(g[1])), None)
        if group is not None:
            members, _ = group
            on = [noun for name, noun in members if enabled.get(name, True)]
            if len(on) == len(members):
                out.append(line)
            elif on:
                out.append(f"- Skip {_join(on)}.")
            continue
        if line in extras:  # it was on: it moves out of the comment of optional lines
            continue
        if line.startswith("<!--") and i + 1 < len(lines) and lines[i + 1].startswith("More lines you can add"):
            out.extend(extras)
        out.append(line)
    text = "\n".join(out).rstrip() + "\n"
    about = strip_comments(about).strip()
    if about:
        text += "\n" + about + "\n"
    return text

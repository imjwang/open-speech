"""The rule compiler (06 § The compiler).

`compile_rule` turns an instruction into a `Rule`:

1. A deterministic fast path recognizes the obvious phrasings ("skip footnotes", "stop reading the page
   numbers on this site", "read citations", "only read the body", "keep only paragraphs and headings") and
   compiles them with no API call, with or without a key.
2. Anything else goes to one structured Claude call (`LlmProvider.compile_rule`) that classifies the rule
   (filter / annotate / transform), compiles filters into the `compiled` schema, suggests a scope and
   names it. The engine validates the schema and test-runs the regexes; a filter that doesn't validate
   is downgraded to `annotate` rather than saved broken.
3. With no key and no fast-path match: `MissingKey`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from open_speech.api.schemas import RuleScope
from open_speech.document.model import BlockType, MarkKind
from open_speech.errors import EngineError, MissingKey
from open_speech.providers.base import CompiledRuleDraft, LlmCall, LlmProvider
from open_speech.rules.model import MAX_PATTERN_LEN, CompiledFilter, ReplaceSpec, Rule, Tier, slugify
from open_speech.rules.scope import TYPE_NOUNS, normalize_host

__all__ = ["compile_offline", "compile_rule", "validate_compiled"]

B = BlockType
M = MarkKind

# (pattern, slug, spoken noun, blocks, marks). Order matters: longer/more specific phrases first.
_NOUNS: list[tuple[str, str, str, list[BlockType], list[MarkKind]]] = [
    (r"table of contents|toc|contents( pages?)?", "toc", "the table of contents", [B.toc], []),
    (
        r"footnote (markers?|refs?|references?|numbers?|marks?)",
        "footnote-markers",
        "footnote markers",
        [],
        [M.footnote_ref],
    ),
    (r"(foot ?|end ?)notes?", "footnotes", "footnotes", [B.footnote], [M.footnote_ref]),
    (r"page numbers?|page numbering|page counts?", "page-numbers", "page numbers", [B.page_number], []),
    (r"(running |page )*(headers?|heads|header text)", "headers", "headers", [B.header], []),
    (r"(page )?footers?( text)?", "footers", "footers", [B.footer], []),
    (
        r"(in-?text )?citations?( markers?| numbers?)?|in-?text references|references in the text|"
        r"citation references",
        "citations",
        "citations",
        [],
        [M.citation_ref],
    ),
    (r"(figure |table )?captions?", "captions", "captions", [B.caption], []),
    (r"urls?|links?|web ?addresses|web links?|hyperlinks?|dois?|web sites? addresses", "urls", "URLs", [], [M.url]),
    (
        r"(text in |things in )?(round )?parenthes[ie]s|parentheticals?|parenthetical (text|remarks?|asides?)|"
        r"round brackets",
        "parentheses",
        "parentheses",
        [],
        [M.parenthetical],
    ),
    (r"(text in |things in )?(curly )?braces|curly brackets", "braces", "braces", [], [M.brace]),
    (r"(text in |things in )?(square )?brackets|bracketed text", "brackets", "brackets", [], [M.bracket]),
    (r"tables?", "tables", "tables", [B.table], []),
    (r"formulas?|formulae|equations?|math|maths|mathematics", "formulas", "formulas", [B.equation], [M.formula]),
    (r"code( blocks?| snippets?| listings?| samples?)?|source code", "code", "code blocks", [B.code], []),
    (
        r"references?( section| list)?|bibliography|bibliographies|works cited|reference lists?",
        "references",
        "the reference list",
        [B.reference_entry],
        [],
    ),
    (r"figures?|images?|pictures?|illustrations?", "figures", "figures", [B.figure], []),
    (r"(block ?)?quotes?|quotations?|blockquotes?", "quotes", "quotes", [B.quote], []),
    (r"headings?|section (titles?|headings?)|titles?|subheadings?", "headings", "headings", [B.heading], []),
    (r"paragraphs?|body text|main text|prose", "paragraphs", "paragraphs", [B.paragraph], []),
    (r"lists?|bullets?( points?)?|list items?", "lists", "lists", [B.list_item], []),
]
_NOUN_RES = [(re.compile(rf"(?:{p})"), slug, noun, blocks, marks) for p, slug, noun, blocks, marks in _NOUNS]

_SKIP_VERBS = re.compile(
    r"^(?:skip(?:ping)?|stop reading|stop saying|don'?t read|do not read|dont read|never read|no longer read|"
    r"don'?t say|do not say|never say|ignore|hide|remove|drop|omit|leave out|exclude|mute|cut|get rid of|"
    r"turn off|no more|no)\b\s*"
)
_KEEP_VERBS = re.compile(
    r"^(?:start reading|always read|resume reading|keep reading|don'?t skip|do not skip|dont skip|"
    r"never skip|stop skipping|stop ignoring|read|include|keep|say)\b\s*"
)
_ONLY_BODY = re.compile(
    r"^(?:(?:only|just) (?:read|keep|say) (?:the )?(?:body|main (?:text|body|content)|body text|content)|"
    r"(?:read|keep|say) only (?:the )?(?:body|main (?:text|body|content)|body text|content))$"
)
_ONLY_LIST = re.compile(r"^(?:(?:only|just) (?:read|keep|say)|(?:read|keep|say) only)\s+(?P<objs>.+)$")
_SAY_AS = re.compile(
    r"^(?:say|pronounce|read)\s+[\"“']?(?P<term>[^\"”']+?)[\"”']?\s+as\s+[\"“']?(?P<spoken>[^\"”']+?)[\"”']?$",
    re.IGNORECASE,
)
_POLITE = re.compile(
    r"^(?:(?:please|pls|hey|ok(?:ay)?|so|also|and|now|from now on|going forward|"
    r"can you|could you|would you|will you|i want you to|i'?d like you to|i would like you to|"
    r"i want to|i'?d like to|let'?s)\s*,?\s*)+"
)
_ARTICLES = re.compile(r"^(?:the|all|any|all the|these|those|its|their|my)\s+")

_SCOPE_SITE = re.compile(r"\b(?:on|for|in|from|at) (?:this|the current) (?:site|website|web ?site|domain)\b")
_SCOPE_DOC = re.compile(
    r"\b(?:on|for|in|from) (?:this|the current) "
    r"(?:document|doc|paper|file|book|article|page|pdf|one|chapter|report)(?: only)?\b"
)
_SCOPE_HOST = re.compile(r"\b(?:on|for|in|from|at) (?P<host>(?:\*\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+)\b")
_SCOPE_TYPE = re.compile(
    r"\b(?:on|for|in) (?:all |any )?(?P<type>pdfs?|pdf files|web ?pages?|websites?|web sites?|"
    r"word documents?|word docs?|docx(?: files)?|text files?|images?|screenshots?)\b"
)
_SCOPE_GLOBAL = re.compile(r"\b(?:everywhere|in all documents|for all documents|for everything|globally)\b")

_TYPE_WORDS = {
    "pdf": "pdf",
    "pdfs": "pdf",
    "pdf files": "pdf",
    "web": "web",
    "website": "web",
    "websites": "web",
    "web site": "web",
    "web sites": "web",
    "word": "docx",
    "docx": "docx",
    "text": "text",
    "image": "image",
    "images": "image",
    "screenshot": "image",
    "screenshots": "image",
}


def _type_from_word(word: str) -> str:
    w = word.replace("webpage", "web page")
    for key in sorted(_TYPE_WORDS, key=len, reverse=True):
        if w.startswith(key):
            return _TYPE_WORDS[key]
    return w


def _clean(instruction: str) -> str:
    s = instruction.strip().lower()
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[.!?;,\s]+$", "", s)
    return _POLITE.sub("", s).strip()


def _extract_scope(text: str) -> tuple[str, str | None, str | None]:
    """Strip a scope phrase. Returns (remaining text, scope kind, value)."""
    for regex, kind in ((_SCOPE_DOC, "document"), (_SCOPE_SITE, "site"), (_SCOPE_GLOBAL, "global")):
        m = regex.search(text)
        if m:
            return (text[: m.start()] + text[m.end() :]).strip(" ,"), kind, None
    m = _SCOPE_TYPE.search(text)
    if m:
        return (text[: m.start()] + text[m.end() :]).strip(" ,"), "type", _type_from_word(m.group("type"))
    m = _SCOPE_HOST.search(text)
    if m:
        return (text[: m.start()] + text[m.end() :]).strip(" ,"), "host", m.group("host")
    return text, None, None


def _split_objects(objs: str) -> list[str]:
    parts = re.split(r"\s*(?:,|&|/|\band\b|\bor\b|\bplus\b|\bas well as\b)\s*", objs)
    return [p.strip() for p in parts if p and p.strip()]


def _match_nouns(objs: str) -> list[tuple[str, str, list[BlockType], list[MarkKind]]] | None:
    items = _split_objects(objs)
    if not items:
        return None
    out = []
    for item in items:
        item = _ARTICLES.sub("", item).strip()
        item = re.sub(r"\s+(?:anymore|any more|please|again|too|either)$", "", item).strip()
        for regex, slug, noun, blocks, marks in _NOUN_RES:
            if regex.fullmatch(item):
                out.append((slug, noun, blocks, marks))
                break
        else:
            return None
    return out


def _scope_for(
    kind: str | None,
    value: str | None,
    *,
    site: str | None,
    doc_kind: str | None,
    doc_id: str | None,
) -> RuleScope | None:
    if kind == "site" and site:
        return RuleScope(sites=[normalize_host(site)])
    if kind == "document" and doc_id:
        return RuleScope(docs=[doc_id])
    if kind == "type":
        t = value or doc_kind
        return RuleScope(types=[t]) if t else None
    if kind == "host" and value:
        return RuleScope(sites=[value if value.startswith("*.") else normalize_host(value)])
    if kind == "global":
        return RuleScope()
    return None


def _scope_suffix(scope: RuleScope) -> tuple[str, str]:
    """(name suffix, description phrase)."""
    if scope.docs:
        return "-" + slugify(scope.docs[0], max_len=24), " in this document"
    if scope.sites:
        return "-" + slugify(scope.sites[0], max_len=32), " on " + ", ".join(scope.sites)
    if scope.types:
        return "-" + slugify(scope.types[0], max_len=16), " in " + ", ".join(TYPE_NOUNS.get(t, t) for t in scope.types)
    return "", ""


def _join(nouns: Sequence[str]) -> str:
    nouns = list(dict.fromkeys(nouns))
    if len(nouns) <= 1:
        return "".join(nouns)
    return ", ".join(nouns[:-1]) + " and " + nouns[-1]


def compile_offline(
    instruction: str,
    *,
    site: str | None = None,
    doc_kind: str | None = None,
    doc_id: str | None = None,
    scope_hint: str | None = None,
    created_by: str = "assistant",
) -> Rule | None:
    """Deterministic compiler for obvious phrasings. Returns None when the instruction needs Claude."""
    text = _clean(instruction)
    if not text:
        return None
    text, phrase_kind, phrase_value = _extract_scope(text)
    text = _clean(text)
    if scope_hint:
        scope = _scope_for(scope_hint, None, site=site, doc_kind=doc_kind, doc_id=doc_id)
    else:
        scope = _scope_for(phrase_kind, phrase_value, site=site, doc_kind=doc_kind, doc_id=doc_id)
    scope = scope or RuleScope()

    compiled: CompiledFilter | None = None
    base: str
    desc: str

    say = _SAY_AS.match(instruction.strip().rstrip(".!"))
    if _ONLY_BODY.match(text):
        compiled = CompiledFilter(keep_blocks_only=[B.heading, B.paragraph])
        base, desc = "only-read-body", "Only read headings and paragraphs"
    elif m := _ONLY_LIST.match(text):
        nouns = _match_nouns(m.group("objs"))
        if not nouns or any(not blocks for _, _, blocks, _ in nouns):
            return None
        keep = list(dict.fromkeys(b for _, _, blocks, _ in nouns for b in blocks))
        compiled = CompiledFilter(keep_blocks_only=keep)
        base = "only-read-" + "-".join(slug for slug, *_ in nouns)
        desc = "Only read " + _join([noun for _, noun, _, _ in nouns])
    elif say and not _match_nouns(_clean(say.group("term"))):
        term, spoken = say.group("term").strip(), say.group("spoken").strip()
        pattern = rf"(?<!\w){re.escape(term)}(?!\w)"
        if term == term.lower():
            pattern = "(?i)" + pattern
        compiled = CompiledFilter(replace=[ReplaceSpec(pattern=pattern, with_=spoken.replace("\\", "\\\\"))])
        base, desc = f"say-{term}", f"Say “{term}” as “{spoken}”"
    elif m := _SKIP_VERBS.match(text):
        nouns = _match_nouns(text[m.end() :])
        if not nouns:
            return None
        compiled = CompiledFilter(
            drop_blocks=list(dict.fromkeys(b for _, _, blocks, _ in nouns for b in blocks)),
            drop_marks=list(dict.fromkeys(k for _, _, _, marks in nouns for k in marks)),
        )
        base = "skip-" + "-".join(slug for slug, *_ in nouns)
        desc = "Don't read " + _join([noun for _, noun, _, _ in nouns])
    elif m := _KEEP_VERBS.match(text):
        nouns = _match_nouns(text[m.end() :])
        if not nouns:
            return None
        compiled = CompiledFilter(
            keep_blocks=list(dict.fromkeys(b for _, _, blocks, _ in nouns for b in blocks)),
            keep_marks=list(dict.fromkeys(k for _, _, _, marks in nouns for k in marks)),
        )
        base = "read-" + "-".join(slug for slug, *_ in nouns)
        desc = "Read " + _join([noun for _, noun, _, _ in nouns])
    else:
        return None

    suffix, where = _scope_suffix(scope)
    name = slugify(base[: 64 - len(suffix)] + suffix)
    return Rule.new(
        name,
        instruction=instruction.strip(),
        description=desc + where + ".",
        scope=scope,
        tier="filter",
        compiled=compiled,
        created_by=created_by,
    )


# ------------------------------------------------------------------ validation

_SAMPLES = (
    "Smith et al. (2020) showed that the effect holds [12], see https://example.org/a for details.",
    "The value x = 3.14 is used (e.g., in Table 2) and {braces} appear in [brackets] here.",
    "Page 4 of 18",
    "",
)


def validate_compiled(raw: dict[str, Any] | CompiledFilter | None) -> CompiledFilter:
    """Validate a compiled filter and test-run its regexes. Raises EngineError("invalid_rule")."""
    if raw is None:
        raise EngineError("invalid_rule", "The compiled filter is empty.")
    try:
        compiled = raw if isinstance(raw, CompiledFilter) else CompiledFilter.model_validate(raw)
    except ValidationError as e:
        raise EngineError("invalid_rule", f"The compiled filter isn't valid: {e.errors()[0]['msg']}.") from e
    for spec in compiled.replace:
        if len(spec.pattern) > MAX_PATTERN_LEN:
            raise EngineError("invalid_rule", "A replacement pattern is too long.")
        try:
            rx = re.compile(spec.pattern)
            for sample in _SAMPLES:
                rx.sub(spec.with_, sample)
        except (re.error, IndexError) as e:
            raise EngineError("invalid_rule", f"A replacement pattern doesn't work: {e}.") from e
        if rx.fullmatch(""):
            raise EngineError("invalid_rule", "A replacement pattern matches empty text.")
    if compiled.is_empty():
        raise EngineError("invalid_rule", "The compiled filter doesn't change anything.")
    return compiled


def _draft_scope(suggestion: dict[str, Any]) -> RuleScope:
    clean: dict[str, list[str]] = {}
    for key in ("sites", "types", "docs", "languages"):
        v = suggestion.get(key)
        if isinstance(v, str):
            v = [v]
        if isinstance(v, list):
            vals = [str(x) for x in v if str(x).strip()]
            if vals:
                clean[key] = vals
    return RuleScope(**clean)


async def compile_rule(
    instruction: str,
    *,
    site: str | None,
    doc_kind: str | None,
    doc_id: str | None,
    scope_hint: str | None,
    llm: LlmProvider | None,
    created_by: str = "assistant",
) -> tuple[Rule, LlmCall | None]:
    """Compile one instruction into a rule (06 § The compiler). `created_by` is an addition."""
    if not instruction or not instruction.strip():
        raise EngineError("invalid_rule", "Tell me what the rule should do.")
    fast = compile_offline(
        instruction, site=site, doc_kind=doc_kind, doc_id=doc_id, scope_hint=scope_hint, created_by=created_by
    )
    if fast is not None:
        return fast, None
    if llm is None:
        raise MissingKey("anthropic", "Understanding that rule")

    _, phrase_kind, phrase_value = _extract_scope(_clean(instruction))
    context: dict[str, Any] = {
        "site": site,
        "doc_kind": doc_kind,
        "doc_id": doc_id,
        "scope_hint": scope_hint or phrase_kind,
        "block_types": [b.value for b in BlockType],
        "mark_kinds": [m.value for m in MarkKind],
        "compiled_schema": CompiledFilter.model_json_schema(by_alias=True),
        "tiers": {
            "filter": "expressible as drop_blocks / drop_marks / replace (regex) / keep_blocks_only",
            "annotate": "needs judgment on spans of the text (skip or replace specific phrases)",
            "transform": "changes what is said (summaries, explanations, descriptions, selections)",
        },
    }
    call = await llm.compile_rule(instruction, context)
    out = call.output
    try:
        draft = out if isinstance(out, CompiledRuleDraft) else CompiledRuleDraft.model_validate(out)
    except ValidationError as e:
        raise EngineError("invalid_rule", "I couldn't turn that into a rule. Try rephrasing it.") from e

    tier: Tier = draft.tier
    compiled: CompiledFilter | None = None
    if tier == "filter":
        try:
            compiled = validate_compiled(draft.compiled)
        except EngineError:
            tier, compiled = "annotate", None  # keep the intent; AI Enhanced will apply it

    hint = scope_hint or phrase_kind
    scope = _scope_for(hint, phrase_value, site=site, doc_kind=doc_kind, doc_id=doc_id)
    if scope is None:
        scope = _draft_scope(draft.scope_suggestion)
    suffix, _ = _scope_suffix(scope)
    base = slugify(draft.name or instruction, max_len=64 - len(suffix))
    name = slugify(base + suffix) if suffix and not base.endswith(suffix) else base
    rule = Rule.new(
        name,
        instruction=(draft.instruction or instruction).strip(),
        description=draft.description.strip() or instruction.strip(),
        scope=scope,
        tier=tier,
        compiled=compiled,
        created_by=created_by,
    )
    return rule, call

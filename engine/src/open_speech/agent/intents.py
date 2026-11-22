"""Fast-path intent grammar (07 § Fast path, FR-BAR-11).

A small deterministic parser for the commands people repeat. Free, instant, offline. Anything it doesn't
recognise — or recognises ambiguously — returns None and goes to Claude.

Inputs are transcripts, so the grammar tolerates fillers ("please", "can you", "um"), number words
("one point five", "twelve") and trailing punctuation.

This module is the English grammar and the entry point: `parse()` tries the grammar of the language the app
is set to first, then the others (people mix languages; `intents_es` is the Spanish one). Every grammar
produces the same `Intent`s, so nothing downstream knows which language was spoken.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

IntentKind = Literal[
    "pause",
    "resume",
    "play",
    "stop",
    "set_speed",
    "change_speed",
    "skip",
    "go_to_page",
    "read_clipboard",
    "where_am_i",
    "lexicon",
    "preset",
    "set_mode",
    "usage",
    "confirm",
    "cancel",
    "show",
    "repeat",
    "propose_rule",
    "open_source",
    "clear_cache",
    "set_voice_by_name",
    "help",
    "read_attachments",
    "next_document",
    "clear_queue",
    "set_theme",
    "set_language",
    "text_size",
    "set_budget",
    "open_rules",
    "remove_pronunciation",
]


@dataclass
class Intent:
    kind: IntentKind
    params: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------ number words

_UNITS = {
    "zero": 0,
    "oh": 0,
    "one": 1,
    "a": 1,
    "an": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_NUMBER_WORD = "|".join(sorted([*_UNITS, *_TENS, "hundred", "point", "and", "half", "quarter"], key=len, reverse=True))
NUM = rf"(?:\d+(?:\.\d+)?|(?:(?:{_NUMBER_WORD})[\s-]*)+)"


def parse_number(text: str) -> float | None:
    """'1.5' → 1.5, 'one point five' → 1.5, 'two and a half' → 2.5, 'twenty one' → 21, 'a hundred' → 100."""
    t = text.strip().lower().replace("-", " ")
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        pass
    words = [w for w in t.split() if w]
    if not words:
        return None
    if "point" in words:
        i = words.index("point")
        whole = parse_number(" ".join(words[:i])) if i else 0.0
        digits = []
        for w in words[i + 1 :]:
            if w in _UNITS and _UNITS[w] < 10:
                digits.append(str(_UNITS[w]))
            elif w.isdigit():
                digits.append(w)
            else:
                return None
        if whole is None or not digits:
            return None
        return float(f"{int(whole)}.{''.join(digits)}")
    total = 0.0
    current = 0.0
    seen = False
    for idx, w in enumerate(words):
        if w == "and":
            continue
        if w in ("half", "quarter"):
            frac = 0.5 if w == "half" else 0.25
            # "a half" / "and a half"
            if idx > 0 and words[idx - 1] in ("a", "an"):
                current -= 1  # the "a" was counted as one
            current += frac
            seen = True
        elif w in _UNITS:
            current += _UNITS[w]
            seen = True
        elif w in _TENS:
            current += _TENS[w]
            seen = True
        elif w == "hundred":
            current = (current or 1) * 100
            seen = True
        elif w.replace(".", "", 1).isdigit():
            current += float(w)
            seen = True
        else:
            return None
    total += current
    return total if seen else None


# ------------------------------------------------------------------ normalisation

_FILLERS = re.compile(
    r"^(?:(?:hey|ok(?:ay)?|um+|uh+|so|please|can you|could you|would you|will you|i want (?:you )?to|"
    r"i'd like (?:you )?to|let's|just)\s+)+",
)
_TRAILING = re.compile(r"\s+(?:please|now|thanks|thank you)$")


def normalise(text: str) -> str:
    t = text.strip().lower()
    t = t.replace("’", "'").replace("“", '"').replace("”", '"')
    t = re.sub(r"[.!?,;:]+$", "", t)
    t = re.sub(r"\s+", " ", t)
    t = _FILLERS.sub("", t)
    t = _TRAILING.sub("", t)
    return t.strip()


# ------------------------------------------------------------------ vocabularies

PRESET_ALIASES: dict[str, str] = {
    "header": "Headers",
    "headers": "Headers",
    "running heads": "Headers",
    "running headers": "Headers",
    "footer": "Footers",
    "footers": "Footers",
    "page number": "Page numbers",
    "page numbers": "Page numbers",
    "footnote": "Footnotes",
    "footnotes": "Footnotes",
    "endnotes": "Footnotes",
    "citation": "Citations",
    "citations": "Citations",
    "references in the text": "Citations",
    "url": "URLs",
    "urls": "URLs",
    "links": "URLs",
    "web addresses": "URLs",
    "parentheses": "Parentheses",
    "parentheticals": "Parentheses",
    "parenthesis": "Parentheses",
    "brackets": "Brackets",
    "square brackets": "Brackets",
    "braces": "Braces",
    "curly braces": "Braces",
    "table": "Tables",
    "tables": "Tables",
    "formula": "Formulas",
    "formulas": "Formulas",
    "equations": "Formulas",
    "math": "Formulas",
    "caption": "Captions",
    "captions": "Captions",
    "figure captions": "Captions",
    "code": "Code",
    "code blocks": "Code",
}
_PRESET_RE = "|".join(sorted((re.escape(k) for k in PRESET_ALIASES), key=len, reverse=True))

_SCOPE_RE = re.compile(
    r"\s+(?:on|for|in)\s+(?:this|the current)\s+(site|website|page|domain|document|paper|book|file|pdf)$"
    r"|\s+(?:everywhere|always|globally|on every site)$"
)

_COUNT = r"(?:\d+|two|three|four|five|six|seven|eight|nine|ten)"
# What people call the things they attach ("read this pdf", "read these links").
_ATTACHED = (
    r"(?:files?|documents?|docs?|pdfs?|pages?|web ?pages?|links?|urls?|articles?|papers?|images?|pictures?|"
    r"photos?|screenshots?|texts?|attachments?|things?|ones?|items?)"
)

_SHOW = {
    "voices": "voices",
    "voice": "voices",
    "rules": "rules",
    "rule": "rules",
    "usage": "usage",
    "spending": "usage",
    "costs": "usage",
    "recents": "recents",
    "recent": "recents",
    "recent documents": "recents",
    "history": "recents",
    "local models": "models",
    "models": "models",
    "presets": "presets",
    "auto-skip": "presets",
    "skips": "presets",
    "settings": "settings",
    "preferences": "settings",
    "queue": "queue",
    "reading queue": "queue",
    "reading list": "queue",
}

URL_RE = re.compile(r"(https?://\S+|(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/\S*)?)", re.I)


THEMES = {"dark": "ember", "ember": "ember", "light": "lavender", "lavender": "lavender", "system": "system",
          "auto": "system", "automatic": "system"}  # fmt: skip
LANGUAGE_NAMES = {"english": "en", "inglés": "en", "ingles": "en", "spanish": "es", "español": "es", "espanol": "es"}
_LANG_RE = "|".join(LANGUAGE_NAMES)


def parse_money(text: str) -> str | None:
    """'$20' → "20", 'twenty dollars' → "20", 'one cent' → "0.01", 'fifty cents' → "0.5", '7.50' → "7.5"."""
    s = text.strip().lower().replace(",", "")
    cents = bool(re.search(r"\bcents?$", s))
    s = re.sub(r"\s*(?:us )?(?:dollars?|bucks|cents?|usd)$", "", s).strip().lstrip("$").strip()
    n = parse_number(s)
    if n is None or n < 0:
        return None
    return f"{(n / 100 if cents else n):.4f}".rstrip("0").rstrip(".") or "0"


# One-word commands are the hardest thing for a speech recognizer: there is no sentence to lean on. These are
# the mishearings measured for the on-device model, mapped back to what was said. They apply only to speech,
# only when the whole transcript is that one word, and the yes/no ones only while a card is waiting.
MISHEARD: dict[str, str] = {
    "cause": "pause", "calls": "pause", "hose": "pause", "holds": "pause", "paws": "pause", "pos": "pause",
    "clay": "play",
    "slow": "slower",
    "pauza": "pausa", "pau": "pausa",
    "continúe": "continúa",
}  # fmt: skip
MISHEARD_ANSWERS: dict[str, str] = {"now": "no", "know": "no", "see": "sí", "c": "sí", "si": "sí", "yes sir": "yes"}


def heard_as(text: str, *, has_pending_action: bool = False) -> str:
    """What a spoken one-word transcript most likely was (see MISHEARD); anything else is returned unchanged."""
    word = re.sub(r"[.!?,;:¡¿]+", "", text).strip().lower()
    if not word or " " in word:
        return text
    if has_pending_action and word in MISHEARD_ANSWERS:
        return MISHEARD_ANSWERS[word]
    return MISHEARD.get(word, text)


def parse(
    text: str,
    *,
    has_pending_action: bool = False,
    has_attachments: bool = False,
    language: str = "en",
    spoken: bool = False,
) -> Intent | None:
    """The intent of a command, or None when Claude should take it. `language` is tried first. `spoken` marks
    a transcript (rather than typed text), which may have been misheard."""
    from open_speech.agent import intents_es

    if spoken:
        text = heard_as(text, has_pending_action=has_pending_action)

    grammars = {"en": parse_en, "es": intents_es.parse_es}
    order = [language, *(code for code in grammars if code != language)]
    for code in order:
        grammar = grammars.get(code)
        if grammar is None:
            continue
        intent = grammar(text, has_pending_action=has_pending_action, has_attachments=has_attachments)
        if intent is not None:
            return intent
    return None


def parse_en(text: str, *, has_pending_action: bool = False, has_attachments: bool = False) -> Intent | None:  # noqa: C901 — a grammar is a list
    t = normalise(text)
    if not t:
        return None

    # --- confirmations (only meaningful while an action card is pending)
    if has_pending_action:
        if re.fullmatch(
            r"(?:yes|yeah|yep|yup|sure|ok|okay|do it|confirm|save it|go ahead|sounds good|"
            r"yes please|please do|correct|that's right|right)",
            t,
        ):
            return Intent("confirm")
        if re.fullmatch(
            r"(?:no|nope|nah|cancel|never ?mind|don't|do not|stop|forget it|no thanks|"
            r"not now|leave it)",
            t,
        ):
            return Intent("cancel")

    # --- transport
    if re.fullmatch(r"(?:pause|hold on|wait|hang on)(?: (?:it|reading|playback|that))?", t):
        return Intent("pause")
    if re.fullmatch(r"(?:resume|continue|keep going|carry on|unpause|go on)(?: (?:reading|playback))?", t):
        return Intent("resume")
    if re.fullmatch(r"(?:play|start)(?: (?:it|reading|playback|again))?", t):
        return Intent("play")
    if re.fullmatch(r"(?:stop)(?: (?:it|reading|playback|talking))?|shut up|be quiet|quiet", t):
        return Intent("stop")

    # --- speed
    if re.fullmatch(r"(?:go |read |a bit |a little |little )*(?:faster|speed up|quicker)(?: please)?", t):
        return Intent("change_speed", {"delta": 0.25})
    if re.fullmatch(r"(?:go |read |a bit |a little |little )*(?:slower|slow down)(?: please)?", t):
        return Intent("change_speed", {"delta": -0.25})
    m = re.fullmatch(
        rf"(?:(?:set |change )?(?:the )?(?:speed|rate)(?: to)?|read at|play at|go)? ?({NUM}) ?(?:x|times|times "
        rf"speed|speed)?",
        t,
    )
    if m and (re.search(r"\b(?:speed|rate|x|times|at)\b", t) or re.fullmatch(r"\d+(?:\.\d+)?x", t)):
        n = parse_number(m.group(1))
        if n is not None and 0.5 <= n <= 5.0:
            return Intent("set_speed", {"speed": round(n, 2)})
    m = re.fullmatch(r"(\d+(?:\.\d+)?)x", t)
    if m:
        n = float(m.group(1))
        if 0.5 <= n <= 5.0:
            return Intent("set_speed", {"speed": n})

    # --- navigation
    m = re.fullmatch(rf"(?:go to|jump to|skip to|turn to|open) page ({NUM})", t)
    if m:
        n = parse_number(m.group(1))
        if n is not None and n >= 1:
            return Intent("go_to_page", {"page": int(n)})
    m = re.fullmatch(
        rf"(?:go |skip |jump )?(back|backward|backwards|rewind|forward|ahead|skip ahead)(?: by)? ({NUM}) ?(seconds?|"
        rf"secs?|minutes?|mins?)",
        t,
    )
    if m:
        n = parse_number(m.group(2)) or 0
        secs = n * (60 if m.group(3).startswith("m") else 1)
        sign = -1 if m.group(1).startswith(("back", "rew")) else 1
        return Intent("skip", {"unit": "seconds", "count": int(sign * secs)})
    m = re.fullmatch(r"(next|previous|last|prev) (sentence|paragraph|heading|section|chapter)", t)
    if m:
        unit = {"section": "heading", "chapter": "heading"}.get(m.group(2), m.group(2))
        return Intent("skip", {"unit": unit, "count": 1 if m.group(1) == "next" else -1})
    m = re.fullmatch(
        r"(?:go |skip )?(back|forward|ahead)(?: (?:a|one))? (sentence|paragraph|heading|section|chapter)", t
    )
    if m:
        unit = {"section": "heading", "chapter": "heading"}.get(m.group(2), m.group(2))
        return Intent("skip", {"unit": unit, "count": -1 if m.group(1) == "back" else 1})
    if re.fullmatch(r"skip|next|skip (?:it|this|ahead)|skip this (?:sentence|one)", t):
        return Intent("skip", {"unit": "sentence", "count": 1})
    if re.fullmatch(r"back|go back|previous|rewind|repeat (?:the )?(?:last )?sentence", t):
        return Intent("skip", {"unit": "sentence", "count": -1})

    # --- attachments in the composer ("read these") and the reading queue
    # With something attached, "read this file" is about the attachment whatever noun is used for it.
    if has_attachments and re.fullmatch(
        r"(?:read|play|open|start)(?: me)?"
        r"(?: (?:it|this|that|these|those|them|both|all|them all|all of (?:it|them)|everything|"
        rf"(?:this|that|the|these|those|both|all|all the|all of the|all these|all of these|my)(?: {_COUNT})? "
        rf"{_ATTACHED}))?"
        r"(?: (?:aloud|out loud|to me|for me))?",
        t,
    ):
        return Intent("read_attachments")
    if re.fullmatch(
        r"(?:next|skip to the next|play the next) (?:document|article|file|item)|"
        r"skip (?:this|the) (?:document|article|file|item)|next in (?:the )?queue",
        t,
    ):
        return Intent("next_document")
    if re.fullmatch(r"(?:clear|empty) (?:the |my )?(?:queue|reading list)|forget the rest", t):
        return Intent("clear_queue")

    # --- sources
    m = re.fullmatch(r"(read|describe|transcribe)(?: (?:what's|what is))?(?: on)? (?:my |the )?clipboard", t)
    if m or re.fullmatch(r"what's on my clipboard|what is on my clipboard", t):
        verb = m.group(1) if m else "describe"
        mode = {"read": "auto", "describe": "describe", "transcribe": "transcribe"}[verb]
        return Intent("read_clipboard", {"image_mode": mode})
    # A bare link or file path, typed or pasted: read it (no verb needed, no LLM).
    raw = text.strip().strip("<>\"'")
    if " " not in raw and (re.fullmatch(r"https?://\S+", raw, re.I) or raw.startswith(("/", "~/"))):
        return Intent("open_source", {"target": raw, "play": True})
    m = re.fullmatch(r"(?:read|open|play)(?: me)? (.+)", t)
    if m:
        target = text.strip().split(None, 1)[1] if len(text.strip().split(None, 1)) > 1 else m.group(1)
        target = re.sub(r"^(?:me\s+)", "", target, flags=re.I).strip().rstrip(".")
        if (
            URL_RE.fullmatch(target)
            or target.startswith(("/", "~/"))
            or re.search(r"\.(pdf|docx|txt|md|png|jpe?g)$", target, re.I)
        ):
            return Intent("open_source", {"target": target, "play": not t.startswith("open")})

    # --- where am I
    if re.fullmatch(
        r"where am i|where are we|how much (?:is )?left|how long (?:is )?left|how much longer|"
        r"what page (?:am i on|is this)|time left",
        t,
    ):
        return Intent("where_am_i")

    # --- lexicon: "say X as Y", "pronounce X as Y"
    m = re.fullmatch(r"(?:say|pronounce|read) (.+?) (?:as|like) (.+)", t)
    if m:
        raw = text.strip()
        rm = re.search(
            r"(?:say|pronounce|read)\s+[\"']?(.+?)[\"']?\s+(?:as|like)\s+[\"']?(.+?)[\"']?[.!?]*$", raw, re.I
        )
        term, spoken = (rm.group(1), rm.group(2)) if rm else (m.group(1), m.group(2))
        if 0 < len(term) <= 60 and 0 < len(spoken) <= 80:
            return Intent("lexicon", {"term": term.strip(), "spoken": spoken.strip()})

    # --- mode
    if re.fullmatch(
        r"(?:turn )?(?:on )?(?:ai|a\.?i\.?) ?enhanced(?: mode)?(?: on)?|(?:turn on|use|switch to) (?:ai|a\.?i\.?) "
        r"?enhanced(?: mode)?|smart (?:mode|cleanup)(?: on)?|be smart(?:er)? about (?:cleanup|skipping)",
        t,
    ):
        return Intent("set_mode", {"mode": "ai_enhanced"})
    if re.fullmatch(
        r"(?:turn )?(?:off )?(?:ai|a\.?i\.?) ?enhanced(?: mode)? off|(?:turn off|disable) (?:ai|a\.?i\.?) "
        r"?enhanced(?: mode)?|(?:use |switch to )?classic(?: mode| cleanup)?",
        t,
    ):
        return Intent("set_mode", {"mode": "classic"})

    # --- presets / scoped skip rules
    scope = None
    sm = _SCOPE_RE.search(t)
    body = t
    if sm:
        word = sm.group(1) or "global"
        scope = {
            "site": "site",
            "website": "site",
            "domain": "site",
            "page": "site",
            "document": "document",
            "paper": "document",
            "book": "document",
            "file": "document",
            "pdf": "document",
        }.get(word, "global")
        body = t[: sm.start()].strip()
    m = re.fullmatch(
        rf"(?:skip|stop reading|don't read|do not read|ignore|leave out|drop|hide) (?:the |all |any )?({_PRESET_RE})",
        body,
    )
    if m:
        preset = PRESET_ALIASES[m.group(1)]
        if scope in ("site", "document"):
            return Intent("propose_rule", {"instruction": text.strip(), "scope_hint": scope})
        return Intent("preset", {"preset": preset, "enabled": True})
    m = re.fullmatch(
        rf"(?:read|start reading|include|keep|don't skip|do not skip|stop skipping) (?:the |all )?({_PRESET_RE})(?: "
        rf"again)?",
        body,
    )
    if m:
        preset = PRESET_ALIASES[m.group(1)]
        if scope in ("site", "document"):
            return Intent("propose_rule", {"instruction": text.strip(), "scope_hint": scope})
        return Intent("preset", {"preset": preset, "enabled": False})

    # --- appearance, language, text size, budget: settings people change by voice (no LLM, applied at once)
    theme_names = "dark|light|lavender|ember|system|automatic|auto"
    m = re.fullmatch(
        rf"(?:use|switch to|turn on|set|change to|go to|go|enable|try|make it) (?:the |a )?({theme_names})"
        rf"(?: (?:mode|theme|appearance|colou?rs))?|({theme_names}) (?:mode|theme)",
        t,
    )
    if m:
        return Intent("set_theme", {"theme": THEMES[m.group(1) or m.group(2)]})
    if re.fullmatch(r"(?:switch|change|toggle|flip) (?:the )?theme", t):
        return Intent("set_theme", {"theme": "toggle"})
    if re.fullmatch(r"(?:follow|match|use) (?:the |my )?(?:system|mac|computer)(?:'s)?(?: theme| appearance)?", t):
        return Intent("set_theme", {"theme": "system"})
    m = re.fullmatch(
        rf"(?:speak|talk|use|switch to|change to|answer|reply|respond|go)(?: to me)?(?: in)? ({_LANG_RE})|"
        rf"(?:in )?({_LANG_RE})|(?:change|set|switch) (?:the )?language to ({_LANG_RE})",
        t,
    )
    if m:
        return Intent("set_language", {"language": LANGUAGE_NAMES[next(g for g in m.groups() if g)]})
    m = re.fullmatch(
        r"(?:make )?(?:the )?(?:text|font|letters|words) (bigger|larger|smaller)|(bigger|larger|smaller) (?:text|font)|"
        r"(increase|decrease|reduce) (?:the )?(?:text|font)(?: size)?",
        t,
    )
    if m:
        word = next(g for g in m.groups() if g)
        return Intent("text_size", {"delta": 2 if word in ("bigger", "larger", "increase") else -2})
    m = re.fullmatch(
        r"(?:set|change|raise|lower|increase|decrease|make|put) (?:my |the )?(?:monthly )?(?:budget|spending limit|"
        r"limit)"
        r"(?: to| at)? (.+?)(?: (?:a|per) month)?",
        t,
    )
    if m is None:
        m = re.fullmatch(r"(?:my )?(?:monthly )?budget(?: of| to| at| is)? (.+?)(?: (?:a|per) month)?", t)
    if m:
        amount = parse_money(m.group(1))
        if amount is not None:
            return Intent("set_budget", {"monthly_usd": amount})

    # --- the rules editor and pronunciations
    m = re.fullmatch(
        r"(?:open|show|edit)(?: me)? (?:the |my )?(?:rules? (?:editor|files?|folder)|rule editor)|"
        r"(?:open|edit) (?:the |my )?rules|(?:open|edit) (?:the |my )?(?:speech )?(preferences|pronunciations|lexicon)",
        t,
    )
    if m:
        item = {"preferences": "preferences", "pronunciations": "lexicon", "lexicon": "lexicon"}.get(m.group(1) or "")
        return Intent("open_rules", {"item": item})
    m = re.fullmatch(
        r"(?:forget|remove|delete) (?:the )?pronunciation (?:of|for) (.+)|stop (?:saying|pronouncing) (.+?) (?:as|"
        r"like) .+|"
        r"forget how (?:to|you) (?:say|pronounce) (.+)",
        t,
    )
    if m:
        return Intent("remove_pronunciation", {"term": next(g for g in m.groups() if g).strip("\"' ")})

    # --- usage
    m = re.fullmatch(
        r"how much (?:have i|did i|i've|i have) (?:spent|used)(?: so far)?(?: (today|this week|this month|in total))?|"
        r"what(?:'s| is| have) (?:my |i )?(?:spend|spent|spending|usage|costs?)(?: so far)?(?: (today|this week|"
        r"this month))?|"
        r"how much (?:is|has) (?:this|it) cost(?: me)?(?: (today|this week|this month))?",
        t,
    )
    if m:
        p = next((g for g in m.groups() if g), "this month")
        period = {"today": "today", "this week": "week", "this month": "month", "in total": "month"}[p]
        return Intent("usage", {"period": period})

    # --- show lists
    m = re.fullmatch(
        r"(?:show|list|open|what are|which are)(?: me)?(?: my| the| all)?(?: available)? (.+?)(?: available)?", t
    )
    if m and m.group(1) in _SHOW:
        return Intent("show", {"what": _SHOW[m.group(1)]})
    if t in _SHOW:  # the bare name: "voices", "settings"
        return Intent("show", {"what": _SHOW[t]})
    if re.fullmatch(r"what voices (?:are there|do you have|can i use)|which voices", t):
        return Intent("show", {"what": "voices"})

    # --- voice by name: "use the coral voice", "switch to kore", "use voice nova"
    m = re.fullmatch(
        r"(?:use|switch to|change to|try)(?: the)?(?: voice)? ([a-z][a-z_ -]{1,30}?)(?: voice)?(?: (?:for|as) "
        r"(?:the )?(default|everything|assistant|this document))?",
        t,
    )
    if m and not m.group(1).startswith(("ai", "classic", "smart")):
        target = {"default": "default", "everything": "default", "assistant": "assistant"}.get(
            m.group(2) or "", "document"
        )
        return Intent("set_voice_by_name", {"name": m.group(1).strip(), "scope": target})

    # --- misc
    if re.fullmatch(r"(?:say that again|repeat that|what did you say|come again|pardon)", t):
        return Intent("repeat")
    if re.fullmatch(r"clear (?:the )?(?:audio )?cache", t):
        return Intent("clear_cache")
    if re.fullmatch(r"help|what can (?:you|i) (?:do|say)|commands", t):
        return Intent("help")
    return None

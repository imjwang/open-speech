"""Fast-path intent grammar (07 § Fast path, FR-BAR-11).

A small deterministic parser for the commands people repeat. Free, instant, offline. Anything it doesn't
recognise — or recognises ambiguously — returns None and goes to Claude.

Inputs are transcripts, so the grammar tolerates fillers ("please", "can you", "um"), number words
("one point five", "twelve") and trailing punctuation.
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
    "keys": "keys",
    "api keys": "keys",
    "settings": "presets",
}

URL_RE = re.compile(r"(https?://\S+|(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/\S*)?)", re.I)


def parse(text: str, *, has_pending_action: bool = False) -> Intent | None:  # noqa: C901 — a grammar is a list
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
        rf"(?:(?:set |change )?(?:the )?(?:speed|rate)(?: to)?|read at|play at|go)? ?({NUM}) ?(?:x|times|times speed|speed)?",
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
        rf"(?:go |skip |jump )?(back|backward|backwards|rewind|forward|ahead|skip ahead)(?: by)? ({NUM}) ?(seconds?|secs?|minutes?|mins?)",
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
        r"(?:turn )?(?:on )?(?:ai|a\.?i\.?) ?enhanced(?: mode)?(?: on)?|(?:turn on|use|switch to) (?:ai|a\.?i\.?) ?enhanced(?: mode)?|smart (?:mode|cleanup)(?: on)?|be smart(?:er)? about (?:cleanup|skipping)",
        t,
    ):
        return Intent("set_mode", {"mode": "ai_enhanced"})
    if re.fullmatch(
        r"(?:turn )?(?:off )?(?:ai|a\.?i\.?) ?enhanced(?: mode)? off|(?:turn off|disable) (?:ai|a\.?i\.?) ?enhanced(?: mode)?|(?:use |switch to )?classic(?: mode| cleanup)?",
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
        rf"(?:read|start reading|include|keep|don't skip|do not skip|stop skipping) (?:the |all )?({_PRESET_RE})(?: again)?",
        body,
    )
    if m:
        preset = PRESET_ALIASES[m.group(1)]
        if scope in ("site", "document"):
            return Intent("propose_rule", {"instruction": text.strip(), "scope_hint": scope})
        return Intent("preset", {"preset": preset, "enabled": False})

    # --- usage
    m = re.fullmatch(
        r"how much (?:have i|did i|i've|i have) (?:spent|used)(?: so far)?(?: (today|this week|this month|in total))?|"
        r"what(?:'s| is| have) (?:my |i )?(?:spend|spent|spending|usage|costs?)(?: so far)?(?: (today|this week|this month))?|"
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
    if re.fullmatch(r"what voices (?:are there|do you have|can i use)|which voices", t):
        return Intent("show", {"what": "voices"})

    # --- voice by name: "use the coral voice", "switch to kore", "use voice nova"
    m = re.fullmatch(
        r"(?:use|switch to|change to|try)(?: the)?(?: voice)? ([a-z][a-z_ -]{1,30}?)(?: voice)?(?: (?:for|as) (?:the )?(default|everything|assistant|this document))?",
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

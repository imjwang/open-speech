"""Spanish fast-path grammar: the same intents as `intents.parse_en`, for commands said or typed in Spanish.

Speech-to-text output is inconsistent about accents and opening punctuation ("¿dónde estoy?", "donde estoy"),
so matching happens on a folded copy of the text: lower case, no diacritics (ñ kept), no ¿ ¡. Text that is
passed on verbatim (a pronunciation, a voice name, a rule instruction) is taken from the original.
"""

from __future__ import annotations

import re
import unicodedata

from open_speech.agent.intents import LANGUAGE_NAMES, URL_RE, Intent

# ------------------------------------------------------------------ folding and number words


def fold(text: str) -> str:
    """Lower case without diacritics; ñ survives (año ≠ ano). One output character per input character."""
    out: list[str] = []
    for ch in text.lower():
        if ch == "ñ":
            out.append(ch)
            continue
        base = unicodedata.normalize("NFD", ch)[0]
        out.append(base if base.isascii() or not unicodedata.combining(ch) else ch)
    return "".join(out).replace("’", "'").replace("“", '"').replace("”", '"').replace("«", '"').replace("»", '"')


_UNITS = {
    "cero": 0, "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
    "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14, "quince": 15,
    "dieciseis": 16, "diecisiete": 17, "dieciocho": 18, "diecinueve": 19, "veinte": 20, "veintiun": 21,
    "veintiuno": 21, "veintiuna": 21, "veintidos": 22, "veintitres": 23, "veinticuatro": 24, "veinticinco": 25,
    "veintiseis": 26, "veintisiete": 27, "veintiocho": 28, "veintinueve": 29,
}  # fmt: skip
_TENS = {"treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "setenta": 70, "ochenta": 80, "noventa": 90}
_HUNDREDS = {
    "cien": 100, "ciento": 100, "doscientos": 200, "trescientos": 300, "cuatrocientos": 400, "quinientos": 500,
    "seiscientos": 600, "setecientos": 700, "ochocientos": 800, "novecientos": 900,
}  # fmt: skip
_NUMBER_WORD = "|".join(
    sorted([*_UNITS, *_TENS, *_HUNDREDS, "mil", "y", "coma", "punto", "con", "medio", "media"], key=len, reverse=True)
)
NUM = rf"(?:\d+(?:[.,]\d+)?|(?:(?:{_NUMBER_WORD})[\s-]*)+)"


def parse_number_es(text: str) -> float | None:
    """'1,5' → 1.5, 'uno coma cinco' → 1.5, 'dos y medio' → 2.5, 'treinta y uno' → 31, 'ciento veinte' → 120."""
    t = fold(text).strip().replace("-", " ")
    if not t:
        return None
    try:
        return float(t.replace(",", "."))
    except ValueError:
        pass
    words = t.split()
    for sep in ("coma", "punto", "con"):
        if sep in words:
            i = words.index(sep)
            whole = parse_number_es(" ".join(words[:i])) if i else 0.0
            digits = [str(_UNITS[w]) if w in _UNITS and _UNITS[w] < 10 else w for w in words[i + 1 :]]
            if whole is None or not digits or not all(d.isdigit() for d in digits):
                return None
            return float(f"{int(whole)}.{''.join(digits)}")
    total = 0.0
    seen = False
    for idx, w in enumerate(words):
        if w == "y":
            continue
        if w in ("medio", "media"):
            total += 0.5
            if idx == 0 or words[idx - 1] not in ("y",):
                pass  # "medio" on its own is one half
        elif w in _UNITS:
            total += _UNITS[w]
        elif w in _TENS:
            total += _TENS[w]
        elif w in _HUNDREDS:
            total += _HUNDREDS[w]
        elif w == "mil":
            total = (total or 1) * 1000
        elif w.replace(".", "", 1).isdigit():
            total += float(w)
        else:
            return None
        seen = True
    return total if seen else None


def parse_money_es(text: str) -> str | None:
    """'20 dólares' → "20", 'un centavo' → "0.01", 'cincuenta céntimos' → "0.5"."""
    s = fold(text).strip()
    cents = bool(re.search(r"\b(?:centavos?|centimos?)$", s))
    s = re.sub(r"\s*(?:de )?(?:dolares?|centavos?|centimos?|usd|pavos)$", "", s).strip().lstrip("$").strip()
    n = parse_number_es(s)
    if n is None or n < 0:
        return None
    return f"{(n / 100 if cents else n):.4f}".rstrip("0").rstrip(".") or "0"


# ------------------------------------------------------------------ normalisation

_FILLERS = re.compile(
    r"^(?:(?:oye|oiga|hola|vale|bueno|eh+|em+|este|a ver|por favor|porfa|puedes|podrias|me puedes|me podrias|"
    r"quiero que|quisiera que|me gustaria que|necesito que|haz que|solo)\s+)+"
)
_TRAILING = re.compile(r"\s+(?:por favor|porfa|gracias|ahora|ya)$")


def normalise_es(text: str) -> str:
    t = fold(text).strip()
    t = re.sub(r"^[¿¡\s]+", "", t)
    t = re.sub(r"[.!?,;:…]+$", "", t)
    t = re.sub(r"[¿¡]", "", t)
    t = re.sub(r"\s+", " ", t)
    t = _FILLERS.sub("", t)
    t = _TRAILING.sub("", t)
    return t.strip()


# ------------------------------------------------------------------ vocabularies

PRESET_ALIASES: dict[str, str] = {
    "encabezados": "Headers", "encabezado": "Headers", "cabeceras": "Headers", "cabecera": "Headers",
    "pies de pagina": "Footers", "pie de pagina": "Footers",
    "numeros de pagina": "Page numbers", "numero de pagina": "Page numbers", "paginacion": "Page numbers",
    "notas al pie": "Footnotes", "notas a pie de pagina": "Footnotes", "notas": "Footnotes",
    "citas": "Citations", "cita": "Citations", "referencias": "Citations",
    "enlaces": "URLs", "urls": "URLs", "url": "URLs", "direcciones web": "URLs", "vinculos": "URLs",
    "parentesis": "Parentheses", "corchetes": "Brackets", "llaves": "Braces",
    "tablas": "Tables", "tabla": "Tables",
    "formulas": "Formulas", "ecuaciones": "Formulas", "matematicas": "Formulas",
    "leyendas": "Captions", "pies de foto": "Captions", "pies de figura": "Captions", "pies de imagen": "Captions",
    "codigo": "Code", "bloques de codigo": "Code",
}  # fmt: skip
_PRESET_RE = "|".join(sorted((re.escape(k) for k in PRESET_ALIASES), key=len, reverse=True))

_SCOPE_RE = re.compile(
    r"\s+(?:en|para|de)\s+(?:este|esta|el|la)\s+(sitio|sitio web|web|pagina|dominio|documento|articulo|libro|"
    r"archivo|pdf)$"
    r"|\s+(?:en todas partes|siempre|en todos los sitios|en todos lados)$"
)
_SCOPES = {
    "sitio": "site", "sitio web": "site", "web": "site", "pagina": "site", "dominio": "site",
    "documento": "document", "articulo": "document", "libro": "document", "archivo": "document", "pdf": "document",
}  # fmt: skip

_COUNT = r"(?:\d+|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez)"
# Cómo se llama a lo que se adjunta («lee este pdf», «lee estos enlaces»).
_ATTACHED = (
    r"(?:archivos?|documentos?|pdfs?|paginas?|enlaces?|links?|urls?|articulos?|imagen(?:es)?|fotos?|capturas?|"
    r"textos?|adjuntos?|cosas?|elementos?)"
)

_SHOW = {
    "voces": "voices", "voz": "voices",
    "reglas": "rules", "regla": "rules",
    "gasto": "usage", "gastos": "usage", "uso": "usage", "consumo": "usage", "costes": "usage", "costos": "usage",
    "recientes": "recents", "historial": "recents", "documentos recientes": "recents",
    "modelos": "models", "modelos locales": "models",
    "omisiones": "presets", "filtros": "presets",
    "ajustes": "settings", "configuracion": "settings", "preferencias": "settings",
    "cola": "queue", "lista de lectura": "queue",
}  # fmt: skip

_THEMES = {
    "oscuro": "ember", "oscura": "ember", "noche": "ember", "ember": "ember", "brasa": "ember", "naranja": "ember",
    "claro": "lavender", "clara": "lavender", "dia": "lavender", "lavanda": "lavender", "lavender": "lavender",
    "sistema": "system", "automatico": "system", "auto": "system",
}  # fmt: skip
_THEME_RE = "|".join(sorted(_THEMES, key=len, reverse=True))
_LANG_RE = "|".join(sorted({fold(k) for k in LANGUAGE_NAMES}, key=len, reverse=True))
_UNIT = {
    "frase": "sentence", "oracion": "sentence", "parrafo": "paragraph", "titulo": "heading",
    "seccion": "heading", "capitulo": "heading", "apartado": "heading",
}  # fmt: skip
_UNIT_RE = "|".join(_UNIT)


def _language(name: str) -> str:
    return next(code for word, code in LANGUAGE_NAMES.items() if fold(word) == name)


def parse_es(text: str, *, has_pending_action: bool = False, has_attachments: bool = False) -> Intent | None:  # noqa: C901 — a grammar is a list
    t = normalise_es(text)
    if not t:
        return None

    # --- confirmaciones (solo con una tarjeta pendiente)
    if has_pending_action:
        if re.fullmatch(
            r"(?:si|claro|vale|de acuerdo|hazlo|confirma(?:r|lo)?|guarda(?:lo|la)?|adelante|correcto|eso es|dale|"
            r"ok(?:ay)?|por supuesto|si por favor|esta bien|perfecto)",
            t,
        ):
            return Intent("confirm")
        if re.fullmatch(
            r"(?:no|cancela(?:r|lo)?|olvidalo|dejalo|mejor no|ahora no|no gracias|para|nada|descarta(?:r|lo)?)", t
        ):
            return Intent("cancel")

    # --- transporte
    if re.fullmatch(r"(?:pausa|pausar|pon(?:lo)? en pausa|espera|un momento|aguarda)(?: (?:la lectura|eso))?", t):
        return Intent("pause")
    if re.fullmatch(r"(?:continua|continuar|sigue|seguir|reanuda|reanudar|prosigue)(?: (?:leyendo|la lectura))?", t):
        return Intent("resume")
    if re.fullmatch(r"(?:reproduce|reproducir|empieza|comienza|dale|play)(?: (?:la lectura|a leer|otra vez))?", t):
        return Intent("play")
    if re.fullmatch(r"(?:para|parar|detente|deten(?: la lectura)?|callate|silencio|basta|stop)(?: (?:de leer|ya))?", t):
        return Intent("stop")

    # --- velocidad
    if re.fullmatch(r"(?:un poco |algo |lee |ve |ir )*(?:mas rapido|mas deprisa|acelera|mas veloz)", t):
        return Intent("change_speed", {"delta": 0.25})
    if re.fullmatch(r"(?:un poco |algo |lee |ve |ir )*(?:mas lento|mas despacio|frena|mas lenta)", t):
        return Intent("change_speed", {"delta": -0.25})
    if re.fullmatch(r"(?:al|a|velocidad) (?:doble|el doble)(?: de velocidad)?|el doble de rapido", t):
        return Intent("set_speed", {"speed": 2.0})
    m = re.fullmatch(
        rf"(?:(?:pon|cambia|ajusta|sube|baja) )?(?:la )?(?:velocidad|ritmo)?(?: a| al| en)? ?({NUM}) ?"
        rf"(?:x|por|veces|de velocidad)?|(?:lee|reproduce|ve) a ({NUM})(?: x| por)?",
        t,
    )
    if m and re.search(r"\b(?:velocidad|ritmo|x|por|veces)\b", t) or re.fullmatch(r"\d+(?:[.,]\d+)?x", t):
        raw = (m.group(1) or m.group(2)) if m else t[:-1]
        n = parse_number_es(raw)
        if n is not None and 0.5 <= n <= 5.0:
            return Intent("set_speed", {"speed": round(n, 2)})

    # --- navegación
    m = re.fullmatch(rf"(?:ve|ir|vamos|salta|pasa|abre) (?:a|en) la pagina ({NUM})|pagina ({NUM})", t)
    if m:
        n = parse_number_es(m.group(1) or m.group(2))
        if n is not None and n >= 1:
            return Intent("go_to_page", {"page": int(n)})
    m = re.fullmatch(
        rf"(retrocede|vuelve|atras|rebobina|adelanta|avanza|salta)(?: atras| adelante)? ({NUM}) ?(segundos?|minutos?)",
        t,
    )
    if m:
        n = parse_number_es(m.group(2)) or 0
        secs = n * (60 if m.group(3).startswith("m") else 1)
        sign = -1 if m.group(1) in ("retrocede", "vuelve", "atras", "rebobina") else 1
        return Intent("skip", {"unit": "seconds", "count": int(sign * secs)})
    m = re.fullmatch(rf"(siguiente|proxim[oa]|anterior) ({_UNIT_RE})|({_UNIT_RE}) (siguiente|anterior)", t)
    if m:
        unit = _UNIT[m.group(2) or m.group(3)]
        direction = m.group(1) or m.group(4)
        return Intent("skip", {"unit": unit, "count": -1 if direction == "anterior" else 1})
    m = re.fullmatch(
        rf"(?:salta|sáltate|saltate|pasa|avanza|retrocede|vuelve)(?: (?:un|una|el|la|al|a la))? ({_UNIT_RE})", t
    )
    if m:
        back = t.startswith(("retrocede", "vuelve"))
        return Intent("skip", {"unit": _UNIT[m.group(1)], "count": -1 if back else 1})
    if re.fullmatch(r"siguiente|salta|saltar|saltate esto|pasa|adelante", t):
        return Intent("skip", {"unit": "sentence", "count": 1})
    if re.fullmatch(r"atras|vuelve|volver|anterior|retrocede|repite la (?:ultima )?frase", t):
        return Intent("skip", {"unit": "sentence", "count": -1})

    # --- adjuntos y cola de lectura
    # Con algo adjunto, «lee este archivo» se refiere al adjunto, se le llame como se le llame.
    if has_attachments and re.fullmatch(
        r"(?:lee|leer|leeme|reproduce|abre)(?:lo|los|la|las|melo|melos|mela|melas)?"
        r"(?: (?:esto|estos|estas|eso|esos|esas|todo|todos|todas|los|las|ambos|los dos|las dos|"
        rf"(?:este|esta|estos|estas|ese|esa|esos|esas|el|la|los|las|mi|mis|todos los|todas las)(?: {_COUNT})? "
        rf"{_ATTACHED}))?"
        r"(?: en voz alta)?",
        t,
    ):
        return Intent("read_attachments")
    if re.fullmatch(
        r"(?:siguiente|proximo) (?:documento|articulo|archivo|elemento)|"
        r"(?:salta|saltate|pasa) (?:este|el) (?:documento|articulo|archivo)|siguiente (?:de|en) la cola",
        t,
    ):
        return Intent("next_document")
    if re.fullmatch(r"(?:vacia|borra|limpia) (?:la |mi )?(?:cola|lista de lectura)|olvida el resto", t):
        return Intent("clear_queue")

    # --- fuentes
    m = re.fullmatch(r"(lee|describe|transcribe)(?:me)? (?:lo que hay en )?(?:mi|el) portapapeles", t)
    if m or re.fullmatch(r"que hay en (?:mi|el) portapapeles", t):
        verb = m.group(1) if m else "describe"
        mode = {"lee": "auto", "describe": "describe", "transcribe": "transcribe"}[verb]
        return Intent("read_clipboard", {"image_mode": mode})
    m = re.fullmatch(r"(?:lee|leer|abre|abrir|reproduce)(?:me)? (.+)", t)
    if m:
        target = text.strip().split(None, 1)[1].strip().rstrip(".") if " " in text.strip() else m.group(1)
        if (
            URL_RE.fullmatch(target)
            or target.startswith(("/", "~/"))
            or re.search(r"\.(pdf|docx|txt|md|png|jpe?g)$", target, re.I)
        ):
            return Intent("open_source", {"target": target, "play": not t.startswith("abr")})

    # --- dónde estoy
    if re.fullmatch(
        r"donde estoy|donde estamos|por donde voy|por donde vamos|cuanto (?:falta|queda)|cuanto tiempo (?:falta|queda)|"
        r"en que pagina (?:estoy|estamos|voy)|que pagina es(?: esta)?",
        t,
    ):
        return Intent("where_am_i")

    # --- pronunciación: "di X como Y"
    m = re.fullmatch(r"(?:di|pronuncia|lee|decir) (.+?) como (.+)", t)
    if m:
        rm = re.search(
            r"(?:di|pronuncia|lee|decir)\s+[\"'«]?(.+?)[\"'»]?\s+como\s+[\"'«]?(.+?)[\"'»]?[.!?]*$", text.strip(), re.I
        )
        term, spoken = (rm.group(1), rm.group(2)) if rm else (m.group(1), m.group(2))
        if 0 < len(term) <= 60 and 0 < len(spoken) <= 80:
            return Intent("lexicon", {"term": term.strip(), "spoken": spoken.strip()})
    m = re.fullmatch(
        r"(?:olvida|borra|quita|elimina) la pronunciacion de (.+)|deja de (?:decir|pronunciar) (.+?) como .+|"
        r"olvida como (?:se dice|se pronuncia|decir|pronunciar) (.+)",
        t,
    )
    if m:
        return Intent("remove_pronunciation", {"term": next(g for g in m.groups() if g).strip("\"' ")})

    # --- modo de limpieza
    if re.fullmatch(
        r"(?:activa|usa|pon|enciende) (?:la |el )?(?:limpieza (?:con|por) ia|limpieza inteligente|modo inteligente|"
        r"ia mejorada|ia)|modo inteligente|limpieza inteligente",
        t,
    ):
        return Intent("set_mode", {"mode": "ai_enhanced"})
    if re.fullmatch(
        r"(?:desactiva|apaga|quita) (?:la |el )?(?:limpieza (?:con|por) ia|limpieza inteligente|modo inteligente|"
        r"ia mejorada|ia)|(?:usa |pon |activa )?(?:la |el )?(?:limpieza clasica|modo clasico)",
        t,
    ):
        return Intent("set_mode", {"mode": "classic"})

    # --- omisiones (presets) y reglas con ámbito
    scope = None
    body = t
    sm = _SCOPE_RE.search(t)
    if sm:
        scope = _SCOPES.get(sm.group(1) or "", "global")
        body = t[: sm.start()].strip()
    m = re.fullmatch(
        rf"(?:omite|omitir|saltate|salta|no leas|deja de leer|ignora|quita|oculta)"
        rf"(?: (?:los|las|el|la|todos los|todas las))? ({_PRESET_RE})",
        body,
    )
    if m:
        if scope in ("site", "document"):
            return Intent("propose_rule", {"instruction": text.strip(), "scope_hint": scope})
        return Intent("preset", {"preset": PRESET_ALIASES[m.group(1)], "enabled": True})
    m = re.fullmatch(
        rf"(?:lee|leer|incluye|vuelve a leer|no omitas|no te saltes|deja de omitir)"
        rf"(?: (?:los|las|el|la|todos los|todas las))? ({_PRESET_RE})(?: otra vez| de nuevo)?",
        body,
    )
    if m:
        if scope in ("site", "document"):
            return Intent("propose_rule", {"instruction": text.strip(), "scope_hint": scope})
        return Intent("preset", {"preset": PRESET_ALIASES[m.group(1)], "enabled": False})

    # --- apariencia, idioma, tamaño del texto, presupuesto
    m = re.fullmatch(
        rf"(?:usa|pon|activa|cambia a|cambia al|ponlo en)(?: el| un)? ?(?:modo|tema)? ?(?:del |de )?({_THEME_RE})|"
        rf"(?:modo|tema) (?:del )?({_THEME_RE})",
        t,
    )
    if m:
        return Intent("set_theme", {"theme": _THEMES[m.group(1) or m.group(2)]})
    if re.fullmatch(r"cambia (?:el|de) tema|otro tema", t):
        return Intent("set_theme", {"theme": "toggle"})
    if re.fullmatch(r"(?:sigue|usa|igual que|como)(?: el| al| la| mi)? (?:sistema|mac|ordenador|computadora)", t):
        return Intent("set_theme", {"theme": "system"})
    m = re.fullmatch(
        rf"(?:habla|hablame|responde|contesta|usa|cambia a|cambia al|pon(?:lo)?)(?: en| el)? ({_LANG_RE})|"
        rf"(?:en )?({_LANG_RE})|(?:cambia|pon) el idioma (?:a|al|en) ({_LANG_RE})",
        t,
    )
    if m:
        return Intent("set_language", {"language": _language(next(g for g in m.groups() if g))})
    m = re.fullmatch(
        r"(?:pon |haz )?(?:la |el )?(?:letra|texto|fuente) mas (grande|pequeñ[ao]|chic[ao])|"
        r"(aumenta|agranda|reduce|disminuye|achica) (?:el tamaño de )?(?:la |el )?(?:letra|texto|fuente)",
        t,
    )
    if m:
        word = m.group(1) or m.group(2)
        return Intent("text_size", {"delta": 2 if word in ("grande", "aumenta", "agranda") else -2})
    m = re.fullmatch(
        r"(?:pon|fija|cambia|sube|baja|ajusta|establece) (?:mi |el )?(?:presupuesto|limite(?: de gasto)?)(?: mensual)?"
        r"(?: a| en)? (.+?)(?: al mes| mensuales)?",
        t,
    )
    if m is None:
        m = re.fullmatch(r"(?:mi )?presupuesto(?: mensual)?(?: de| a| en| es)? (.+?)(?: al mes| mensuales)?", t)
    if m:
        amount = parse_money_es(m.group(1))
        if amount is not None:
            return Intent("set_budget", {"monthly_usd": amount})

    # --- el editor de reglas
    m = re.fullmatch(
        r"(?:abre|muestra|edita|editar)(?:me)? (?:el )?editor de reglas|(?:edita|editar|abre) (?:las |mis )?reglas|"
        r"(?:abre|edita) (?:las |mis )?(preferencias|pronunciaciones)(?: de lectura)?",
        t,
    )
    if m:
        item = {"preferencias": "preferences", "pronunciaciones": "lexicon"}.get(m.group(1) or "")
        return Intent("open_rules", {"item": item})

    # --- gasto
    m = re.fullmatch(
        r"cuanto (?:he gastado|llevo gastado|gaste|llevo)(?: (hoy|esta semana|este mes|en total))?|"
        r"cual es mi (?:gasto|consumo)(?: (hoy|esta semana|este mes))?|"
        r"cuanto (?:me )?(?:ha costado|cuesta|costo)(?: esto)?(?: (hoy|esta semana|este mes))?",
        t,
    )
    if m:
        p = next((g for g in m.groups() if g), "este mes")
        return Intent("usage", {"period": {"hoy": "today", "esta semana": "week"}.get(p, "month")})

    # --- listas
    m = re.fullmatch(
        r"(?:muestra|muestrame|enseña|enseñame|lista|ver|abre|cuales son|que)(?: me)?(?: (?:las|los|mis|el|la|mi))?"
        r"(?: (?:todas|todos))? (.+?)(?: (?:hay|tienes|disponibles))?",
        t,
    )
    if m and m.group(1) in _SHOW:
        return Intent("show", {"what": _SHOW[m.group(1)]})
    if t in _SHOW:  # solo el nombre: «voces», «ajustes»
        return Intent("show", {"what": _SHOW[t]})

    # --- voz por nombre: "usa la voz de Dora", "cambia a Alex"
    m = re.fullmatch(
        r"(?:usa|cambia a|cambia al|prueba|pon)(?: la| el)?(?: voz)?(?: de)? ([a-z][a-z_ -]{1,30}?)"
        r"(?: (?:para|como|de) (?:la |el )?(todo|siempre|predeterminad[ao]|por defecto|asistente|este documento))?",
        t,
    )
    if m and "voz" in t or m and len(m.group(1).split()) == 1 and m.group(1) not in _THEMES:
        assert m is not None
        target = {
            "todo": "default", "siempre": "default", "predeterminada": "default", "predeterminado": "default",
            "por defecto": "default", "asistente": "assistant",
        }.get(m.group(2) or "", "document")  # fmt: skip
        return Intent("set_voice_by_name", {"name": m.group(1).strip(), "scope": target})

    # --- varios
    if re.fullmatch(r"repite(?:lo)?|otra vez|que (?:has dicho|dijiste)|como(?: dices)?|perdon", t):
        return Intent("repeat")
    if re.fullmatch(r"(?:borra|limpia|vacia) (?:la |el )?(?:cache|memoria)(?: de audio)?", t):
        return Intent("clear_cache")
    if re.fullmatch(r"ayuda|que puedo decir|que puedes hacer|comandos|que sabes hacer", t):
        return Intent("help")
    return None

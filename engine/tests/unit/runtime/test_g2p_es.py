"""Spanish G2P for Kokoro (providers/g2p_es.py): no weights, no network, no espeak.

The reference strings in `fixtures/providers/g2p_es_reference.json` are what the pipeline Kokoro's Spanish
voices were trained with produces for these inputs; they were frozen once from a development oracle.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from open_speech.playback.timing import WORD_RE
from open_speech.providers.g2p import G2P_LANGUAGES, LexiconPhonemizer, g2p_language, phonemizer_for
from open_speech.providers.g2p_es import (
    SYMBOLS,
    SpanishPhonemizer,
    cardinal_words,
    ordinal_words,
    spoken_words,
    word_phonemes,
)

REFERENCE = json.loads(
    (Path(__file__).parents[2] / "fixtures" / "providers" / "g2p_es_reference.json").read_text("utf-8")
)
VOCAB = set(REFERENCE["vocab"])
G = SpanishPhonemizer()


def phonemes(text: str) -> str:
    """The phoneme string as Kokoro's encoder assembles it: token phonemes, a space where one follows."""
    return "".join(t.phonemes + (" " if t.whitespace else "") for t in G.phonemize(text)).strip()


def said(text: str) -> str:
    return " ".join(spoken_words(text))


# ------------------------------------------------------------------ agreement with the reference


def test_reference_words() -> None:
    wrong = {w: (phonemes(w), want) for w, want in REFERENCE["words"].items() if phonemes(w) != want}
    assert len(REFERENCE["words"]) > 800
    assert not wrong


def test_reference_phrases() -> None:
    wrong = {p: (phonemes(p), want) for p, want in REFERENCE["phrases"].items() if phonemes(p) != want}
    assert len(REFERENCE["phrases"]) >= 150
    assert not wrong


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        # c / z / s, g / j, ll / y, ñ, ch, qu, h, x, r
        ("cielo", "θjˈelo"), ("zapato", "θapˈato"), ("casa", "kˈasa"), ("gente", "xˈɛnte"), ("jirafa", "xiɾˈafa"),
        ("guerra", "ɡˈera"), ("guitarra", "ɡitˈara"), ("pingüino", "pˌiŋɡuˈino"), ("llave", "ʎˈaβe"),
        ("yo", "ʝˈo"), ("niño", "nˈiɲo"), ("mucho", "mˈuʧo"), ("queso", "kˈeso"), ("hola", "ˈola"),
        ("examen", "eksˈamen"), ("xilófono", "silˈofono"), ("méxico", "mˈɛxiko"), ("rosa", "rˈosa"),
        ("perro", "pˈero"), ("pero", "pˈeɾo"), ("enrique", "enrˈike"), ("alrededor", "ˌalreðeðˈoɾ"),
        # b d g: stops after a pause or a nasal, fricatives elsewhere
        ("boca", "bˈoka"), ("lobo", "lˈoβo"), ("hombre", "ˈombɾe"), ("dedo", "dˈeðo"), ("andar", "andˈaɾ"),
        ("gato", "ɡˈato"), ("lago", "lˈaɣo"), ("tengo", "tˈɛŋɡo"), ("ciudad", "θjuðˈad"), ("obtener", "ˌobtenˈeɾ"),
        ("absoluto", "ˌaβsolˈuto"), ("digno", "dˈiɡno"), ("enviar", "embjˈaɾ"),
        # diphthongs and hiatus
        ("aire", "ˈIɾe"), ("causa", "kˈWsa"), ("reina", "rˈAna"), ("deuda", "dˈeʊða"), ("hoy", "ˈoɪ"),
        ("muy", "mˈuj"), ("bueno", "bwˈeno"), ("tiene", "tjˈene"), ("país", "paˈis"), ("baúl", "baˈul"),
        ("día", "dˈia"), ("poeta", "poˈeta"), ("buey", "bwˈA"), ("ahora", "aˈoɾa"), ("prohibir", "pɾˌoiβˈiɾ"),
        ("cliente", "kliˈɛnte"), ("ruido", "ruˈiðo"), ("luego", "luˈeɣo"), ("industria", "indˈustɾia"),
        # loanword letters and clusters
        ("kilo", "kˈilo"), ("whisky", "wˈiski"), ("pizza", "pˈiʦa"), ("show", "ʃˈow"), ("septiembre", "sepːtjˈembɾe"),
        ("psicología", "sˌikoloxˈia"), ("camping", "kˈampiŋ"),
    ],
)  # fmt: skip
def test_letter_rules(word: str, expected: str) -> None:
    assert phonemes(word) == expected


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        # written accent wins
        ("cantó", "kantˈo"), ("cántaro", "kˈantaɾo"), ("régimen", "rˈeximen"), ("regímenes", "rexˈimenes"),
        ("carácter", "kaɾˈakteɾ"), ("café", "kafˈe"),
        # vowel, n, s → penultimate; other consonants → last
        ("canto", "kˈanto"), ("hablan", "ˈaβlan"), ("hablas", "ˈaβlas"), ("joven", "xˈoβen"),
        ("papel", "papˈel"), ("cantar", "kantˈaɾ"), ("feliz", "felˈiθ"), ("reloj", "relˈox"), ("robots", "roβˈots"),
        ("estoy", "estˈoɪ"), ("uruguay", "ˌuɾuɣwˈI"),
        # secondary stress on alternate syllables, never next to the primary or on the last syllable
        ("universidad", "ˌuniβˌeɾsiðˈad"), ("responsabilidad", "rˌesponsˌaβiliðˈad"),
        ("internacionalización", "ˌinteɾnˌaθjonˌaliθaθjˈon"), ("dígamelo", "dˈiɣamˌelo"),
        ("espíritu", "espˈiɾitu"), ("fenómenos", "fenˈomenos"),
        # adverbs in -mente keep both stresses; other words in -mente are ordinary
        ("rápidamente", "rˈapiðamˈente"), ("generalmente", "xˌeneɾˈalmˈente"), ("lentamente", "lˈɛntamˈente"),
        ("clemente", "klemˈɛnte"),
        # stressed e opens before n + consonant only
        ("momento", "momˈɛnto"), ("tienda", "tjˈɛnda"), ("bien", "bjˈen"), ("pena", "pˈena"),
        ("tiempo", "tjˈempo"), ("péndulo", "pˈendulo"), ("pensar", "pensˈaɾ"),
    ],
)  # fmt: skip
def test_stress_and_syllables(word: str, expected: str) -> None:
    assert phonemes(word) == expected


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        # allophones and nasal place across word boundaries, inside a clause
        ("la boca", "la βˈoka"), ("un beso", "ˈum bˈeso"), ("un gato", "ˈuŋ ɡˈato"), ("el día", "el ðˈia"),
        ("un día", "ˈun dˈia"), ("con vino", "kom bˈino"), ("en casa", "en kˈasa"), ("con gente", "koŋ xˈɛnte"),
        ("un yate", "ˈun ʤˈate"), ("ciudad y campo", "θjuðˈað i kˈampo"), ("ciudad grande", "θjuðˈad ɣɾˈande"),
        # function words: unstressed in a clause, stressed again at its end
        ("para ti y para mí", "pˌaɾa tˈi i pˌaɾa mˈi"), ("lo que", "lo kˈe"), ("de la", "de lˈa"),
        ("el niño que vino ayer es mi hermano", "el nˈiɲo ke βˈino aʝˈeɾ ˈes mi eɾmˈano"),
        ("dámelo ahora mismo", "dˈamelo aˈoɾa mˈismo"), ("físico-químico", "fˈisikokˈimiko"),
    ],
)  # fmt: skip
def test_clause_rules(phrase: str, expected: str) -> None:
    assert word_phonemes(phrase) == expected
    assert phonemes(phrase) == expected


def test_punctuation_starts_a_new_clause() -> None:
    # ¿ ¡ are not in Kokoro's vocabulary: no symbol, but the word after them is clause-initial (stop "b")
    assert phonemes("Hola, ¿cómo estás? ¡Muy bien!") == "ˈola, kˈomo estˈas? mˈuj βjˈen!"
    assert phonemes("Uno, dos, tres.") == "ˈuno, dˈos, tɾˈes."
    assert phonemes("Dijo: «No lo sé».") == "dˈixo: “nˈo lo sˈe”."
    assert phonemes("Vino (creo) ayer — o antes…") == "bˈino (kɾˈeo) aʝˈeɾ — o ˈantes…"
    assert phonemes("Lo que, sin duda, vale.") == "lo kˈe, sin dˈuða, bˈale."


# ------------------------------------------------------------------ text normalisation


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "cero"), (1, "uno"), (16, "dieciséis"), (21, "veintiuno"), (22, "veintidós"), (31, "treinta y uno"),
        (100, "cien"), (101, "ciento uno"), (500, "quinientos"), (999, "novecientos noventa y nueve"),
        (1000, "mil"), (1984, "mil novecientos ochenta y cuatro"), (2026, "dos mil veintiséis"),
        (21000, "veintiún mil"), (1_000_000, "un millón"), (2_500_000, "dos millones quinientos mil"),
        (1_000_000_000, "mil millones"), (3_000_000_000, "tres mil millones"), (10**12, "un billón"),
        (-5, "menos cinco"),
    ],
)  # fmt: skip
def test_cardinals(n: int, expected: str) -> None:
    assert " ".join(cardinal_words(n)) == expected


def test_cardinal_gender_and_apocope() -> None:
    assert cardinal_words(21, apocope=True) == ["veintiún"]
    assert cardinal_words(21, fem=True) == ["veintiuna"]
    assert cardinal_words(1, apocope=True) == ["un"]
    assert cardinal_words(31, fem=True) == ["treinta", "y", "una"]
    assert cardinal_words(200, fem=True) == ["doscientas"]
    assert cardinal_words(501, fem=True) == ["quinientas", "una"]


def test_ordinals() -> None:
    assert ordinal_words(1) == ["primero"]
    assert ordinal_words(1, apocope=True) == ["primer"]
    assert ordinal_words(3, fem=True) == ["tercera"]
    assert ordinal_words(13) == ["decimotercero"]
    assert ordinal_words(18) == ["decimoctavo"]
    assert ordinal_words(21, fem=True) == ["vigésima", "primera"]
    assert ordinal_words(2, plural=True) == ["segundos"]
    assert ordinal_words(250) == ["doscientos", "cincuenta"]  # beyond 100 speakers use the cardinal


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # numbers: thousands separators, decimals with "coma"/"punto", codes digit by digit, years
        ("1.234", "mil doscientos treinta y cuatro"), ("1,200", "mil doscientos"),
        ("12.345.678", "doce millones trescientos cuarenta y cinco mil seiscientos setenta y ocho"),
        ("3,5", "tres coma cinco"), ("3.5", "tres punto cinco"), ("0,05", "cero coma cero cinco"),
        ("1.234,56", "mil doscientos treinta y cuatro coma cincuenta y seis"), ("007", "cero cero siete"),
        ("Nació en 1984.", "Nació en mil novecientos ochenta y cuatro"),
        ("-5 grados", "menos cinco grados"), ("2010–2015", "dos mil diez a dos mil quince"),
        # agreement with the noun that follows, only when it is clear
        ("tiene 21 años", "tiene veintiún años"), ("21 personas", "veintiuna personas"), ("1 hora", "una hora"),
        ("200 casas", "doscientas casas"), ("31 días", "treinta y un días"), ("21 mil", "veintiún mil"),
        ("en 2021 España ganó", "en dos mil veintiuno España ganó"), ("el 31 llegó", "el treinta y uno llegó"),
        # ordinals, percentages, currency, units
        ("1.º", "primero"), ("2.ª", "segunda"), ("3.er", "tercer"), ("1º", "primero"), ("5to", "quinto"),
        ("25 %", "veinticinco por ciento"), ("3,5%", "tres coma cinco por ciento"),
        ("20 €", "veinte euros"), ("1 €", "un euro"), ("3,50 €", "tres euros con cincuenta céntimos"),
        ("$20", "veinte dólares"), ("US$ 5.000 millones", "cinco mil millones de dólares"),
        ("21 £", "veintiuna libras"), ("2.000.000 €", "dos millones de euros"),
        ("5 km", "cinco kilómetros"), ("1 km", "un kilómetro"), ("120 km/h", "ciento veinte kilómetros por hora"),
        ("2 h", "dos horas"), ("25 °C", "veinticinco grados"), ("40º", "cuarenta grados"),
        # times and dates
        ("12:30", "doce y treinta"), ("9:05", "nueve y cinco"), ("1:15", "una y quince"), ("12:00", "doce en punto"),
        ("21:45", "veintiuna y cuarenta y cinco"), ("5/3/2020", "cinco de marzo de dos mil veinte"),
        ("2020-03-05", "cinco de marzo de dos mil veinte"),
        ("31.12.2021", "treinta y uno de diciembre de dos mil veintiuno"),
        # abbreviations
        ("El Sr. García y la Sra. López", "El señor García y la señora López"),
        ("el Dr. Pérez y la Dra. Ruiz", "el doctor Pérez y la doctora Ruiz"),
        ("Ud. y Uds.", "usted y ustedes"), ("peras, etc.", "peras etcétera"),
        ("pág. 34 y núm. 5", "página treinta y cuatro y número cinco"), ("En EE. UU. hay", "En estados unidos hay"),
        ("EE.UU.", "estados unidos"), ("p. ej. el n.º 5", "por ejemplo el número cinco"),
        ("D. Juan y D.ª Inés", "don Juan y doña Inés"), ("300 a. C.", "trescientos antes de cristo"),
        # Roman numerals where they are unambiguous
        ("siglo XX", "siglo veinte"), ("siglo xxi", "siglo veintiuno"), ("s. XVI", "siglo dieciséis"),
        ("siglos XVI y XVII", "siglos dieciséis y diecisiete"), ("Felipe VI", "Felipe sexto"),
        ("Isabel II", "Isabel segunda"), ("Alfonso XIII", "Alfonso trece"),
        ("la II Guerra Mundial", "la segunda Guerra Mundial"), ("capítulo IV", "capítulo cuatro"),
        ("plan B", "plan be"), ("rayos X", "rayos equis"), ("Anexo C", "Anexo ce"),
        # acronyms: read when they sound like a word, spelled with Spanish letter names otherwise
        ("la ONU", "la onu"), ("la OTAN", "la otan"), ("el DNI", "el de ene i"), ("la UE", "la u e"),
        ("un PDF", "un pe de efe"), ("LA CASA", "la casa"), ("HOY HAY SOL", "hoy hay sol"),
        ("COVID-19", "covid diecinueve"), ("H2O", "hache dos o"), ("3 + 2 = 5", "tres más dos igual a cinco"),
    ],
)  # fmt: skip
def test_normalisation(text: str, expected: str) -> None:
    assert said(text) == expected


def test_abbreviation_period_ends_the_sentence_only_when_it_does() -> None:
    assert phonemes("El Sr. García llegó.") == "el seɲˈoɾ ɣaɾθˈia ʎeɣˈo."  # no pause after "señor"
    toks = G.phonemize("Compró peras, etc. Luego volvió.")
    assert [t.phonemes for t in toks if not t.is_word] == [",", ".", "."]
    toks = G.phonemize("Compró peras, etc. y volvió.")
    assert [t.phonemes for t in toks if not t.is_word] == [",", "."]


def test_spelled_acronyms_are_one_run_with_the_stress_on_the_last_letter() -> None:
    assert phonemes("DNI") == "dˌeˌeneˈi"
    assert phonemes("el DNI") == "el ðˌeˌeneˈi"
    assert phonemes("PDF") == "pˌeðˌeˈɛfe"
    assert phonemes("ONU") == "ˈonu"


# ------------------------------------------------------------------ symbols, offsets, robustness

SAMPLES = [
    "Hola, ¿cómo estás? ¡Muy bien, gracias!",
    "El Sr. García y la Dra. López viven en EE. UU. desde hace 21 años; pagan 3,50 € (el 25 %).",
    "En el siglo XVI, Felipe II —según dicen— gobernaba «medio mundo»… ¿o no?",
    "El pingüino, la cigüeña y el ñandú: exámenes de lingüística a las 12:30 del 5/3/2020.",
    "WhatsApp, Google, software y jazz: McDonald's, l'Hospitalet, iPhone 15, COVID-19, H2O, km/h.",
    "hardware, Facebook, Microsoft, YouTube, online",
    "físico-químico, ex-presidente, D. Juan, n.º 5, 1.º, 2.ª, US$ 5.000 millones, -5 °C, 1936–1939.",
    "日本語 привет 😀 ½ ² ª º @ # & + = % € $ £ ° × ± [uno] {dos} <tres> 'cuatro' `cinco`",
    "",
    "   ",
    "...",
    "¿¡!?",
]


def test_every_symbol_is_in_the_kokoro_vocabulary() -> None:
    assert len(VOCAB) == 115  # the tokenizer shipped with Kokoro-82M v1.0
    assert SYMBOLS <= VOCAB
    emitted = {c for text in [*SAMPLES, *REFERENCE["phrases"], *REFERENCE["words"]] for c in phonemes(text)}
    assert emitted <= SYMBOLS


def test_vocabulary_copy_matches_the_tokenizer_when_weights_are_present() -> None:
    from open_speech.local.bundled import bundled_models_dir

    root = bundled_models_dir()
    tok = root / "kokoro" / "kokoro-82m-v1.0" / "tokenizer.json" if root else None
    if tok is None or not tok.exists():
        pytest.skip("Kokoro tokenizer not present")
    assert set(json.loads(tok.read_text("utf-8"))["model"]["vocab"]) == VOCAB


@pytest.mark.parametrize("text", SAMPLES)
def test_token_offsets_follow_the_source(text: str) -> None:
    toks = G.phonemize(text)
    pos = 0
    for t in toks:
        assert pos <= t.start < t.end <= len(text)  # in order, no overlap
        assert t.phonemes, "no empty tokens"
        assert (len(t.phonemes) == 1 and t.phonemes in ';:,.!?—…"()“”') != t.is_word
        if t.is_word:
            assert not text[t.start].isspace() and not text[t.end - 1].isspace()
        pos = t.end
    assert not toks or not toks[-1].whitespace


def test_every_written_word_gets_its_own_token() -> None:
    text = "Los científicos descubrieron una nueva especie de mariposa en la selva, ¿verdad?"
    spans = [(t.start, t.end) for t in G.phonemize(text) if t.is_word]
    assert spans == [(m.start(), m.end()) for m in WORD_RE.finditer(text)]
    # a number with its unit or sign is one token that covers the written form
    text = "Pagó 3,50 € ayer."
    toks = [t for t in G.phonemize(text) if t.is_word]
    assert [text[t.start : t.end] for t in toks] == ["Pagó", "3,50 €", "ayer"]
    assert toks[1].phonemes == "tɾˈes ˈeʊɾos kon θinkwˈɛnta θˈentimos"


def test_never_raises_on_arbitrary_text() -> None:
    rng = random.Random(3)
    alphabet = "abcdefghijklmnñopqrstuvwxyzáéíóúüABCXVI0123456789 .,;:¿?¡!()«»\"'-–—/%€$º°ª\n\t@#&+*=<>[]{}|\\~^_`ç日"
    for _ in range(400):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
        toks = G.phonemize(text)
        assert all(set(t.phonemes) <= SYMBOLS for t in toks), text
        assert all(0 <= t.start < t.end <= len(text) for t in toks), text


def test_long_text_is_linear_enough() -> None:
    text = " ".join(REFERENCE["phrases"]) * 20
    assert len(G.phonemize(text)) > 5000


# ------------------------------------------------------------------ choosing by language


def test_phonemizer_for_language() -> None:
    assert G2P_LANGUAGES == ("en", "es")
    assert isinstance(phonemizer_for("es"), SpanishPhonemizer)
    assert phonemizer_for("es-MX") is phonemizer_for("es") is phonemizer_for("ES_419")
    assert isinstance(phonemizer_for("en-GB"), LexiconPhonemizer)
    assert phonemizer_for(None) is phonemizer_for("en") is phonemizer_for("fr")  # English is the fallback
    assert [g2p_language(x) for x in ("es", "es-ES", "en-US", "", None, "pt")] == ["es", "es", "en", "en", "en", "en"]

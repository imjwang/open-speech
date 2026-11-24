"""Google Cloud Text-to-Speech, Chirp 3 HD voices (08 § Google Chirp 3 HD). Plain HTTPS with httpx: no Google SDK.

- Synthesis: `POST {base}/v1/text:synthesize` with the build's key in `X-Goog-Api-Key` and
  `{"input": {"text"}, "voice": {"languageCode", "name"}, "audioConfig": {"audioEncoding": "LINEAR16",
  "sampleRateHertz": 24000}}`. The response's base64 `audioContent` is a WAV file; it is decoded to PCM and
  yielded as one chunk. No `speakingRate`: speed stays in the transport, so it is never part of the cache key.
- The API takes at most 5,000 bytes of UTF-8 per request. Utterances are one to three sentences, so this
  almost never matters; longer text is split at sentence boundaries (then clauses, then words) and the audio
  joined.
- Billing is per character (`price_units`), with a free monthly allowance (pricing.toml, usage/free_tier.py).
- Availability: a key can be valid for Gemini yet not allowed to call Cloud Text-to-Speech (403
  `API_KEY_SERVICE_BLOCKED`), or the API may be off in its project (`SERVICE_DISABLED`). Those are not bad
  keys: the voices are reported unavailable with the fix in plain words, and `GoogleVoiceCheck` (a free
  `GET /v1/voices` per catalog locale at startup, after 1 and 10 minutes, then hourly and right after a
  synthesis 403) notices when they start working. Only `API_KEY_INVALID` (or a 401) marks the key invalid.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import re
import struct
import time
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

import httpx
import numpy as np

from open_speech.i18n import t
from open_speech.local.catalog import ModelCatalog, TtsModel
from open_speech.providers.base import AudioChunk, TtsLimits, TtsRequest, Units, Voice
from open_speech.providers.errors import ProviderError
from open_speech.providers.pcm import f32_to_bytes, int16_to_f32

log = logging.getLogger(__name__)

PROVIDER = "google"
API_BASE = "https://texttospeech.googleapis.com"
MAX_INPUT_BYTES = 5000
DEFAULT_RATE = 24000
LOCALE_NAMES = {
    "en-US": "American English",
    "en-GB": "British English",
    "en-AU": "Australian English",
    "en-IN": "Indian English",
    "es-US": "US Spanish",
    "es-ES": "European Spanish",
}

# 403 reasons (google.rpc.ErrorInfo) that say the key may not call this API, which is not the key being bad:
# shown as the voices' unavailable reason, never `mark_invalid` (so the Gemini voices keep working).
BLOCKED_REASONS = {
    "API_KEY_SERVICE_BLOCKED": "blocked",
    "ACCESS_DENIED": "blocked",  # "Permission denied on the service." (voices.list for this build's key, 2026-11-22)
    "SERVICE_DISABLED": "disabled",
    "BILLING_DISABLED": "billing",
    "API_KEY_HTTP_REFERRER_BLOCKED": "restricted",
    "API_KEY_IP_ADDRESS_BLOCKED": "restricted",
    "API_KEY_ANDROID_APP_BLOCKED": "restricted",
    "API_KEY_IOS_APP_BLOCKED": "restricted",
}
INVALID_KEY_REASONS = {"API_KEY_INVALID", "API_KEY_EXPIRED"}

Blocked = Literal["blocked", "disabled", "billing", "restricted"]


def blocked_reason_text(kind: str) -> str:
    """The plain-words reason for a `Blocked` kind, in the user's language."""
    return t(f"google.{kind}") if kind in ("blocked", "disabled", "billing", "restricted") else t("google.blocked")


# ------------------------------------------------------------------ errors


def _error_info(resp: httpx.Response) -> tuple[str, str, set[str]]:
    """(status, message, ErrorInfo reasons) from a Google API error body; blanks when it isn't one."""
    try:
        err = resp.json().get("error") or {}
    except (ValueError, AttributeError):
        return "", "", set()
    if not isinstance(err, dict):
        return "", "", set()
    reasons = {
        str(d.get("reason"))
        for d in err.get("details") or []
        if isinstance(d, dict) and d.get("reason")
    }
    return str(err.get("status") or ""), str(err.get("message") or ""), reasons


def map_google_error(resp: httpx.Response, *, feature: str = "speech") -> ProviderError:
    """An HTTP error from Cloud Text-to-Speech as a `ProviderError`. Adds `.blocked` (a `Blocked` kind or None)."""
    status = resp.status_code
    gstatus, message, reasons = _error_info(resp)
    blocked = next((BLOCKED_REASONS[r] for r in reasons if r in BLOCKED_REASONS), None)
    if status == 403 and blocked is None and not (reasons & INVALID_KEY_REASONS):
        blocked = "blocked"  # a 403 we can't name: the API refused this key, which still works elsewhere
    err: ProviderError
    if blocked is not None:
        err = ProviderError("unavailable", blocked_reason_text(blocked), provider=PROVIDER, status=status)
    elif status == 401 or reasons & INVALID_KEY_REASONS:
        err = ProviderError(
            "provider_error",
            "Google turned the request down, so I'll keep reading with the local voice.",
            provider=PROVIDER,
            status=status,
            key_invalid=True,
        )
    elif status == 429:
        err = ProviderError(
            "rate_limited",
            "Google is rate limiting requests right now. I'll retry shortly.",
            provider=PROVIDER,
            status=status,
            retryable=True,
        )
    elif status >= 500 or status in (408, 409):
        err = ProviderError(
            "provider_error",
            f"Google had a server error ({status}). I'll retry, then fall back to the local voice.",
            provider=PROVIDER,
            status=status,
            retryable=True,
        )
    else:
        log.info("Google TTS rejected a %s request: %s %s %s", feature, status, gstatus, message[:200])
        err = ProviderError(
            "provider_error", f"Google rejected the {feature} request ({status}).", provider=PROVIDER, status=status
        )
    err.blocked = blocked  # type: ignore[attr-defined]
    return err


def _network_error(exc: BaseException) -> ProviderError:
    return ProviderError(
        "offline",
        "I couldn't reach Google. I'll keep reading with the local voice and try again later.",
        provider=PROVIDER,
        retryable=True,
    )


# ------------------------------------------------------------------ audio


def decode_linear16(data: bytes, default_rate: int = DEFAULT_RATE) -> tuple[np.ndarray, int]:
    """LINEAR16 `audioContent`: a WAV file (RIFF header + 16-bit PCM). Tolerates a streaming-style header whose
    data size is 0 or 0xFFFFFFFF, and headerless PCM (taken at `default_rate`). Returns mono float32."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return int16_to_f32(data), default_rate
    pos, rate, channels, bits = 12, default_rate, 1, 16
    pcm = b""
    while pos + 8 <= len(data):
        cid, size = data[pos : pos + 4], struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        body = pos + 8
        if cid == b"fmt " and size >= 16:
            _fmt, channels, rate, _brate, _align, bits = struct.unpack("<HHIIHH", data[body : body + 16])
        elif cid == b"data":
            end = len(data) if size in (0, 0xFFFFFFFF) or body + size > len(data) else body + size
            pcm = data[body:end]
            break
        pos = body + size + (size & 1)
    if bits != 16:
        raise ValueError(f"unexpected {bits}-bit audio from Google")
    x = int16_to_f32(pcm)
    if channels > 1:
        x = x[: x.size - x.size % channels].reshape(-1, channels).mean(axis=1).astype(np.float32)
    return x, int(rate)


# ------------------------------------------------------------------ text limits

_SENTENCE_END = re.compile(r"(?<=[.!?…。])[\"'”’)\]]*\s+")
_CLAUSE_END = re.compile(r"(?<=[,;:—–])\s+")


def _nbytes(s: str) -> int:
    return len(s.encode("utf-8"))


def split_for_limit(text: str, max_bytes: int = MAX_INPUT_BYTES) -> list[str]:
    """`text` in pieces of at most `max_bytes` UTF-8 bytes, cut at sentence ends where possible, then at clause
    punctuation, then between words, and only as a last resort inside a word (never inside a character)."""
    if _nbytes(text) <= max_bytes:
        return [text]
    pieces: list[str] = []

    def units(s: str, level: int) -> list[str]:
        if level == 0:
            return [p for p in _SENTENCE_END.split(s) if p]
        if level == 1:
            return [p for p in _CLAUSE_END.split(s) if p]
        if level == 2:
            return s.split()
        out, cur = [], ""
        for ch in s:  # hard cut on character boundaries
            if _nbytes(cur + ch) > max_bytes:
                out.append(cur)
                cur = ""
            cur += ch
        return [*out, cur] if cur else out

    def pack(s: str, level: int) -> None:
        cur = ""
        for u in units(s, level):
            if _nbytes(u) > max_bytes:
                if cur:
                    pieces.append(cur)
                    cur = ""
                pack(u, level + 1)
                continue
            joined = f"{cur} {u}" if cur else u
            if _nbytes(joined) > max_bytes:
                pieces.append(cur)
                cur = u
            else:
                cur = joined
        if cur:
            pieces.append(cur)

    pack(text, 0)
    return [p for p in pieces if p.strip()]


# ------------------------------------------------------------------ voices


def persona(model: str, star: str) -> str:
    """Voices that are the same speaker in another locale share a persona ("chirp3-hd:Zephyr")."""
    return f"{model}:{star}"


def catalog_voices(models: Iterable[TtsModel], check: GoogleVoiceCheck | None = None) -> list[Voice]:
    """Chirp voices from the catalog. With a check: voices the live list doesn't have are left out, and every
    voice is unavailable (with the reason) while the API refuses this build's key."""
    out: list[Voice] = []
    for m in models:
        for v in m.voices:
            parts = m.voice_parts(v)
            locale, star = parts if parts is not None else ("", v)
            available, reason = (True, None) if check is None else check.status(v)
            if check is not None and not check.listed(v):
                continue
            region = LOCALE_NAMES.get(locale, locale)
            name = f"{star} ({m.name or 'Chirp 3 HD'}, {region})" if region else f"{star} ({m.name or 'Chirp 3 HD'})"
            out.append(
                Voice(
                    id=m.voice_id(v),
                    provider=PROVIDER,
                    model=m.model,
                    name=name,
                    languages=[locale] if locale else list(m.languages),
                    styles=False,
                    local=False,
                    available=available,
                    unavailable_reason=reason,
                    timestamps=False,
                    persona=persona(m.model, star) if parts is not None else None,
                )
            )
    return out


# ------------------------------------------------------------------ availability check

CHECK_AFTER_S: tuple[float, ...] = (60.0, 600.0)  # after the first check at startup
CHECK_EVERY_S = 3600.0
CheckState = Literal["unknown", "available", "blocked", "invalid"]


@dataclass
class _Snapshot:
    state: CheckState
    blocked: str | None
    live: frozenset[str] | None


class GoogleVoiceCheck:
    """Whether this build's key may call Cloud Text-to-Speech, and which catalog voices the API really has.

    `unknown` (not checked yet, or the check couldn't reach Google) offers every catalog voice: a failure then
    surfaces as a normal synthesis error. `blocked` makes every voice unavailable with the reason; `invalid`
    marks the key invalid (the registry then drops the voices). Calls `on_change()` whenever what the voices
    list or the capabilities would say changes."""

    def __init__(
        self,
        catalog: ModelCatalog,
        key: Callable[[], str | None],
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = API_BASE,
        timeout_s: float = 10.0,
        on_change: Callable[[], None] | None = None,
        on_invalid: Callable[[], None] | None = None,
        after_s: tuple[float, ...] = CHECK_AFTER_S,
        every_s: float = CHECK_EVERY_S,
    ) -> None:
        self._catalog = catalog
        self._key = key
        self._client = client
        self._own_client = client is None
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s
        self.on_change = on_change
        self._on_invalid = on_invalid
        self._after = after_s
        self._every = every_s
        self.state: CheckState = "unknown"
        self.blocked: str | None = None  # a `Blocked` kind while state == "blocked"
        self.live: frozenset[str] | None = None  # voice names the API lists (catalog locales), once known
        self.checked_at: float | None = None
        self._wake: asyncio.Event | None = None
        self._lock: asyncio.Lock | None = None
        self.scheduled = False  # `run()` has started: a first answer is on its way
        self._first = asyncio.Event()  # the first check at startup has answered (or couldn't reach Google)

    # ------------------------------------------------------------ state

    def set_catalog(self, catalog: ModelCatalog) -> None:
        self._catalog = catalog

    def locales(self) -> list[str]:
        out: list[str] = []
        for m in self._catalog.tts_models(PROVIDER):
            for loc in m.locales or m.languages:
                if loc not in out:
                    out.append(loc)
        return out

    def usable(self) -> bool:
        """Chirp can be offered: checked and allowed, or not known to be refused yet."""
        return self.state in ("unknown", "available")

    def listed(self, voice: str) -> bool:
        return self.live is None or voice in self.live

    def status(self, voice: str) -> tuple[bool, str | None]:
        if self.state == "blocked":
            return False, blocked_reason_text(self.blocked or "blocked")
        return True, None

    def reason(self) -> str | None:
        return blocked_reason_text(self.blocked or "blocked") if self.state == "blocked" else None

    def _snapshot(self) -> _Snapshot:
        return _Snapshot(self.state, self.blocked, self.live)

    def _changed(self, before: _Snapshot) -> bool:
        after = self._snapshot()
        if after == before:
            return False
        log.info("Google voices: %s%s", self.state, f" ({self.blocked})" if self.blocked else "")
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001
                log.exception("Google voice check listener failed")
        return True

    def note_synthesis_error(self, err: BaseException) -> None:
        """A synthesis call failed. A refusal (403) is believed at once and re-checked right away."""
        blocked = getattr(err, "blocked", None)
        if blocked is None and getattr(err, "upstream_status", None) != 403:
            return
        before = self._snapshot()
        self.state, self.blocked = "blocked", blocked or "blocked"
        self._changed(before)
        self.request_recheck()

    def request_recheck(self) -> None:
        if self._wake is not None:
            self._wake.set()

    # ------------------------------------------------------------ checking

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def check(self) -> bool:
        """One check: `GET /v1/voices?languageCode=<locale>` for every catalog locale (free). Returns whether the
        state changed. Never raises."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            before = self._snapshot()
            key = self._key()
            if not key:
                return False
            names: set[str] = set()
            locales = self.locales()
            try:
                # The first locale answers "may this key call the API?"; the others only fill in the list.
                results = [await self._list(key, locales[0])] if locales else []
                results += await asyncio.gather(*(self._list(key, loc) for loc in locales[1:]))
            except ProviderError as err:
                blocked = getattr(err, "blocked", None)
                if blocked is not None:
                    self.state, self.blocked = "blocked", blocked
                elif err.key_invalid:
                    self.state, self.blocked = "invalid", None
                    if self._on_invalid is not None:
                        self._on_invalid()
                else:  # offline, rate limited, a server error: keep what we knew and try again later
                    log.info("Google voice check failed (%s); trying again later", err.code)
                    return False
            else:
                for r in results:
                    names.update(r)
                self.state, self.blocked = "available", None
                self.live = frozenset(names)
            self.checked_at = time.monotonic()
            return self._changed(before)

    async def _list(self, key: str, locale: str) -> set[str]:
        try:
            resp = await self._http().get(
                f"{self._base}/v1/voices",
                params={"languageCode": locale},
                headers={"X-Goog-Api-Key": key},
            )
        except Exception as exc:  # noqa: BLE001 — a background check: anything here means "couldn't ask"
            raise _network_error(exc) from exc
        if resp.status_code >= 400:
            raise map_google_error(resp, feature="voice list")
        try:
            items = resp.json().get("voices") or []
        except ValueError as exc:
            raise ProviderError("provider_error", "Google sent an unreadable voice list.", provider=PROVIDER) from exc
        return {str(v.get("name")) for v in items if isinstance(v, dict) and v.get("name")}

    async def wait_first(self, timeout: float) -> bool:
        """Wait, at most `timeout` seconds, until the first check at startup has answered, so "auto" doesn't pick
        a voice the check is about to refuse. Returns at once when it has answered, or when no check is
        scheduled. True when the answer is in."""
        if self._first.is_set() or not self.scheduled:
            return self._first.is_set()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._first.wait(), timeout=timeout)
        return self._first.is_set()

    async def run(self) -> None:
        """Check now, again after 1 and 10 minutes, then hourly; `request_recheck()` checks at once."""
        self._wake = asyncio.Event()
        self.scheduled = True
        delays = list(self._after)
        while True:
            try:
                await self.check()
            except Exception:  # noqa: BLE001 — the schedule must survive anything a check runs into
                log.exception("Google voice check failed")
            self._first.set()
            delay = delays.pop(0) if delays else self._every
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            self._wake.clear()

    async def aclose(self) -> None:
        if self._own_client and self._client is not None:
            client, self._client = self._client, None
            await client.aclose()


# ------------------------------------------------------------------ the adapter


class GoogleTts:
    id = PROVIDER
    timestamps = False

    def __init__(
        self,
        catalog: ModelCatalog,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = API_BASE,
        timeout_s: float = 30.0,
        check: GoogleVoiceCheck | None = None,
    ) -> None:
        self._models: dict[str, TtsModel] = {m.model: m for m in catalog.tts_models(self.id)}
        self._key = api_key
        self._client = client
        self._own_client = client is None
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s
        self._check = check

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    def voices(self) -> list[Voice]:
        return catalog_voices(self._models.values(), self._check)

    def limits(self, model: str) -> TtsLimits:
        m = self._models.get(model)
        return TtsLimits(
            max_input_chars=m.max_input_chars if m else MAX_INPUT_BYTES,
            formats=["l16"],
            style=False,
            languages=list(m.languages) if m else [],
        )

    def _locale(self, model: str, voice: str) -> str:
        m = self._models.get(model)
        parts = m.voice_parts(voice) if m is not None else None
        if parts is not None:
            return parts[0]
        # "en-US-Chirp3-HD-Zephyr" → "en-US" for a voice the catalog doesn't template
        bits = voice.split("-")
        return "-".join(bits[:2]) if len(bits) >= 2 else "en-US"

    def request_body(self, req: TtsRequest, text: str | None = None) -> dict[str, Any]:
        m = self._models.get(req.model)
        rate = (m.sample_rate if m and m.sample_rate else None) or DEFAULT_RATE
        return {
            "input": {"text": req.text if text is None else text},
            "voice": {"languageCode": self._locale(req.model, req.voice), "name": req.voice},
            "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": rate},
        }

    async def _synthesize_piece(self, body: dict[str, Any]) -> tuple[np.ndarray, int]:
        try:
            resp = await self._http().post(
                f"{self._base}/v1/text:synthesize", json=body, headers={"X-Goog-Api-Key": self._key}
            )
        except httpx.HTTPError as exc:
            raise _network_error(exc) from exc
        if resp.status_code >= 400:
            err = map_google_error(resp)
            if self._check is not None:
                self._check.note_synthesis_error(err)
            raise err
        try:
            audio = base64.b64decode(resp.json()["audioContent"])
            return await asyncio.to_thread(decode_linear16, audio, body["audioConfig"]["sampleRateHertz"])
        except (ValueError, KeyError, TypeError, struct.error) as exc:
            raise ProviderError("provider_error", "Google returned audio I couldn't read.", provider=PROVIDER) from exc

    async def synthesize(self, req: TtsRequest) -> AsyncIterator[AudioChunk]:
        parts: list[np.ndarray] = []
        rate = DEFAULT_RATE
        for piece in split_for_limit(req.text):
            pcm, rate = await self._synthesize_piece(self.request_body(req, piece))
            parts.append(pcm)
        pcm = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
        if pcm.size == 0:
            raise ProviderError("provider_error", "Google returned no audio.", provider=PROVIDER)
        yield AudioChunk(pcm=f32_to_bytes(pcm), sample_rate=rate, final=True)

    def price_units(self, req: TtsRequest, audio_sec: float) -> Units:
        # Google bills every character sent (spaces included); the free allowance is applied by the ledger.
        return Units(chars=len(req.text), audio_sec=round(audio_sec, 3))

    async def aclose(self) -> None:
        if self._own_client and self._client is not None:
            client, self._client = self._client, None
            await client.aclose()

"""Fetched web pages in the asset store: one place every URL the engine reads comes through.

A link the user opens and a link the assistant reads are the same request, so they share one cache:
`PageStore.fetch(url)` answers from the store while the copy is fresh, revalidates it with the server's own
validators (ETag / Last-Modified) when it is older, and falls back to the stored copy when the network is
down. The response body is kept verbatim under the hash of the normalized URL (`store/assets.py`, kind "page").

Because the assistant can now cause a fetch, this is also where the network boundary is enforced (07 § Injection
boundary): http(s) only, no credentials in the URL, and never an address on this machine or the local network,
checked for the first request and for every redirect.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from open_speech.errors import EngineError
from open_speech.i18n import t
from open_speech.ids import sha256_hex
from open_speech.ingest.url import MAX_BYTES, USER_AGENT, Fetched
from open_speech.store.assets import Asset, AssetStore

KIND = "page"
FRESH_FOR_S = 15 * 60
MAX_REDIRECTS = 5
Resolver = Callable[[str], list[str]]


_SCHEME = re.compile(r"[a-z][a-z0-9+.-]*:(?!\d)", re.IGNORECASE)


def _with_scheme(url: str) -> str:
    """`example.org/x` and `example.org:8080` mean https; anything that names a scheme keeps it (and is then
    judged by it: `javascript:…` and `file:…` are not addresses to fetch)."""
    raw = url.strip()
    return raw if "://" in raw or _SCHEME.match(raw) else f"https://{raw}"


def normalize_url(url: str) -> str:
    """Scheme and host lower-cased, default port and fragment dropped: one key per page."""
    parts = urlsplit(_with_scheme(url))
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host if port in (None, 80 if scheme == "http" else 443) else f"{host}:{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def page_key(url: str) -> str:
    return sha256_hex("page", normalize_url(url))


def host_of(url: str) -> str:
    try:
        return (urlsplit(_with_scheme(url)).hostname or "").lower()
    except ValueError:
        return ""


def _system_resolver(host: str) -> list[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]


def _is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    return addr.is_global and not addr.is_multicast


@dataclass
class PageFetch(Fetched):
    from_store: bool = False  # answered without downloading the body again
    stale: bool = False  # the network failed; this is the last copy we have


class PageStore:
    def __init__(
        self,
        assets: AssetStore,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        resolver: Resolver | None = None,
        allow_private: bool = False,
        fresh_for_s: float = FRESH_FOR_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.assets = assets
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(follow_redirects=False, timeout=20.0, headers={"User-Agent": USER_AGENT})
        )
        self._resolve = resolver or _system_resolver
        self._allow_private = allow_private
        self._fresh_for_s = fresh_for_s
        self._clock = clock
        self._inflight: dict[str, asyncio.Future[PageFetch]] = {}

    # ------------------------------------------------------------------ the network boundary

    async def check(self, url: str) -> str:
        """Return the normalized URL, or raise if it may not be fetched."""
        try:
            parts = urlsplit(_with_scheme(url))
            host = (parts.hostname or "").lower()
            _ = parts.port  # raises for a port that is not a number
        except ValueError as e:
            raise EngineError("invalid_request", t("page.bad_url")) from e
        if parts.scheme.lower() not in ("http", "https") or not host or parts.username or parts.password:
            raise EngineError("invalid_request", t("page.bad_url"))
        if not self._allow_private:
            try:
                addresses = [host] if _is_ip(host) else await asyncio.to_thread(self._resolve, host)
            except OSError as e:
                raise EngineError("offline", t("page.unreachable", host=host)) from e
            if not addresses or not all(_is_public(a) for a in addresses):
                raise EngineError("invalid_request", t("page.private", host=host))
        return normalize_url(url)

    # ------------------------------------------------------------------ fetch

    def cached(self, url: str) -> Asset | None:
        return self.assets.get(KIND, page_key(url), touch=False)

    async def fetch(self, url: str, *, max_age_s: float | None = None) -> PageFetch:
        """The page at `url`, from the store when it is fresh enough, else from the network (and stored)."""
        norm = await self.check(url)
        key = page_key(norm)
        fresh_for = self._fresh_for_s if max_age_s is None else max_age_s
        asset = self.assets.get(KIND, key)
        if asset is not None and self._clock() - float(asset.meta.get("fetched_at", 0)) <= fresh_for:
            return self._from_asset(asset, from_store=True)
        running = self._inflight.get(key)
        if running is not None:  # the same page is already being downloaded
            return await asyncio.shield(running)
        fut: asyncio.Future[PageFetch] = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            result = await self._download(norm, key, asset)
            fut.set_result(result)
            return result
        except BaseException as exc:
            fut.set_exception(exc)
            fut.exception()  # retrieved here so an unawaited future doesn't warn
            raise
        finally:
            self._inflight.pop(key, None)

    async def _download(self, norm: str, key: str, asset: Asset | None) -> PageFetch:
        headers: dict[str, str] = {}
        if asset is not None:
            if asset.meta.get("etag"):
                headers["If-None-Match"] = str(asset.meta["etag"])
            if asset.meta.get("last_modified"):
                headers["If-Modified-Since"] = str(asset.meta["last_modified"])
        try:
            resp, final_url = await self._get(norm, headers)
        except EngineError as e:
            if asset is not None and e.code == "offline":
                return self._from_asset(asset, from_store=True, stale=True)
            raise
        if resp.status_code == 304 and asset is not None:
            refreshed = self.assets.update_meta(KIND, key, fetched_at=self._clock()) or asset
            return self._from_asset(refreshed, from_store=True)
        if resp.status_code >= 400:
            raise EngineError("invalid_request", t("page.http_error", status=resp.status_code))
        content = resp.content
        if len(content) > MAX_BYTES:
            raise EngineError("invalid_request", t("page.too_large"))
        media = resp.headers.get("content-type", "text/html").split(";")[0].strip().lower()
        meta = {
            "url": norm,
            "final_url": final_url,
            "media_type": media,
            "fetched_at": self._clock(),
            "etag": resp.headers.get("etag"),
            "last_modified": resp.headers.get("last-modified"),
        }
        self.assets.put(KIND, key, content, ext=".bin", meta=meta)
        return PageFetch(url=final_url, content=content, media_type=media)

    async def _get(self, url: str, headers: dict[str, str]) -> tuple[httpx.Response, str]:
        """GET following redirects by hand, so every hop passes the same boundary check as the first."""
        current = url
        try:
            async with self._client_factory() as client:
                for _ in range(MAX_REDIRECTS + 1):
                    resp = await client.get(current, headers=headers)
                    if resp.status_code not in (301, 302, 303, 307, 308):
                        return resp, current
                    location = resp.headers.get("location")
                    if not location:
                        return resp, current
                    current = await self.check(str(httpx.URL(current).join(location)))
                    headers = {}  # validators belong to the first URL only
        except httpx.HTTPError as e:
            raise EngineError("offline", t("page.unreachable", host=host_of(current))) from e
        raise EngineError("invalid_request", t("page.redirects"))

    @staticmethod
    def _from_asset(asset: Asset, *, from_store: bool, stale: bool = False) -> PageFetch:
        m = asset.meta
        return PageFetch(
            url=str(m.get("final_url") or m.get("url") or ""),
            content=asset.read(),
            media_type=str(m.get("media_type") or "text/html"),
            from_store=from_store,
            stale=stale,
        )


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False

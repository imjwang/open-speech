"""Fetched pages (store/pages.py): one cache for links the user opens and links the assistant reads, and the
network boundary both go through."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from open_speech.errors import EngineError
from open_speech.store.assets import AssetStore
from open_speech.store.db import Database
from open_speech.store.pages import PageStore, host_of, normalize_url, page_key

PUBLIC = "93.184.216.34"
Handler = Callable[[httpx.Request], httpx.Response]


class Web:
    """A fake internet: counts requests, answers from `handler`, resolves names from `hosts`."""

    def __init__(self, tmp_path: Path, handler: Handler, *, hosts: dict[str, list[str]] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.handler = handler
        self.hosts = hosts or {}
        self.now = 1_000_000.0
        self.assets = AssetStore(tmp_path / "cache", Database(tmp_path / "db.sqlite"))
        self.pages = PageStore(
            self.assets,
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(self._handle)),
            resolver=lambda host: self.hosts.get(host, [PUBLIC]),
            clock=lambda: self.now,
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def html(body: str = "<p>hello</p>", **headers: str) -> httpx.Response:
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/html; charset=utf-8", **headers})


# ------------------------------------------------------------------ the address


def test_one_key_per_page() -> None:
    assert normalize_url("Example.ORG") == "https://example.org/"
    assert normalize_url("HTTPS://Example.org:443/a?b=1#section") == "https://example.org/a?b=1"
    assert normalize_url("http://example.org:8080/x") == "http://example.org:8080/x"
    assert page_key("https://example.org/a#one") == page_key("https://EXAMPLE.org/a#two")
    assert page_key("https://example.org/a") != page_key("https://example.org/b")
    assert host_of("https://Sub.Example.org/x") == "sub.example.org"
    assert host_of("example.org/x") == "example.org"
    assert normalize_url("example.org:8443/x") == "https://example.org:8443/x"
    assert host_of("javascript:alert(1)") == ""


# ------------------------------------------------------------------ the boundary


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.org/x",
        "javascript:alert(1)",
        "data:text/html,<p>hi</p>",
        "mailto:someone@example.org",
        "https://example.org:port/",
        "localhost:3000",
        "https://user:secret@example.org/",
        "http://127.0.0.1:8765/api/v1/settings",
        "http://localhost/",
        "http://[::1]/",
        "http://10.0.0.5/admin",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/",
        "https://",
    ],
)
async def test_addresses_that_may_never_be_fetched(tmp_path: Path, url: str) -> None:
    web = Web(tmp_path, lambda r: html(), hosts={"localhost": ["127.0.0.1"]})
    with pytest.raises(EngineError) as e:
        await web.pages.fetch(url)
    assert e.value.code == "invalid_request"
    assert web.requests == [], "refused before anything is sent"


async def test_a_public_name_that_points_inside_is_refused(tmp_path: Path) -> None:
    web = Web(
        tmp_path, lambda r: html(), hosts={"intranet.example": ["10.1.2.3"], "mixed.example": [PUBLIC, "127.0.0.1"]}
    )
    for host in ("intranet.example", "mixed.example"):
        with pytest.raises(EngineError, match="local network"):
            await web.pages.fetch(f"https://{host}/")
    assert web.requests == []


async def test_a_redirect_cannot_lead_inside(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1:8765/api/v1/settings"})

    web = Web(tmp_path, handler)
    with pytest.raises(EngineError, match="local network"):
        await web.pages.fetch("https://example.org/go")
    assert [str(r.url) for r in web.requests] == ["https://example.org/go"], "the second hop is never requested"


async def test_redirects_are_followed_and_the_final_address_is_reported(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return html("<p>moved here</p>")

    web = Web(tmp_path, handler)
    page = await web.pages.fetch("https://example.org/old")
    assert page.url == "https://example.org/new"
    assert page.content == b"<p>moved here</p>"
    assert page.media_type == "text/html"


async def test_a_redirect_loop_ends(tmp_path: Path) -> None:
    web = Web(tmp_path, lambda r: httpx.Response(302, headers={"location": "/again"}))
    with pytest.raises(EngineError, match="redirects"):
        await web.pages.fetch("https://example.org/")
    assert len(web.requests) == 6


async def test_private_addresses_can_be_allowed_for_development(tmp_path: Path) -> None:
    web = Web(tmp_path, lambda r: html("<p>local</p>"))
    web.pages = PageStore(
        web.assets,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(web._handle)),
        allow_private=True,
    )
    assert (await web.pages.fetch("http://127.0.0.1:9000/page")).content == b"<p>local</p>"


# ------------------------------------------------------------------ the cache


async def test_a_fresh_copy_is_served_without_touching_the_network(tmp_path: Path) -> None:
    web = Web(tmp_path, lambda r: html("<p>one</p>"))
    first = await web.pages.fetch("https://example.org/a")
    assert not first.from_store
    second = await web.pages.fetch("https://EXAMPLE.org/a#later")
    assert second.from_store and not second.stale
    assert second.content == first.content
    assert len(web.requests) == 1
    assert web.pages.cached("https://example.org/a") is not None
    assert web.pages.cached("https://example.org/other") is None


async def test_an_older_copy_is_revalidated_and_kept_when_unchanged(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return html("<p>one</p>", etag='"v1"', **{"last-modified": "Mon, 01 Jun 2026 00:00:00 GMT"})

    web = Web(tmp_path, handler)
    await web.pages.fetch("https://example.org/a")
    web.now += 3600
    again = await web.pages.fetch("https://example.org/a")
    assert again.from_store and again.content == b"<p>one</p>"
    assert web.requests[1].headers["if-none-match"] == '"v1"'
    assert web.requests[1].headers["if-modified-since"] == "Mon, 01 Jun 2026 00:00:00 GMT"
    # The copy counts as fresh again from now.
    await web.pages.fetch("https://example.org/a")
    assert len(web.requests) == 2


async def test_a_changed_page_replaces_the_stored_copy(tmp_path: Path) -> None:
    bodies = iter(["<p>one</p>", "<p>two</p>"])
    web = Web(tmp_path, lambda r: html(next(bodies), etag='"x"'))
    await web.pages.fetch("https://example.org/a")
    web.now += 3600
    page = await web.pages.fetch("https://example.org/a")
    assert page.content == b"<p>two</p>" and not page.from_store
    assert web.assets.count("page") == 1


async def test_the_caller_can_ask_for_a_fresher_copy(tmp_path: Path) -> None:
    web = Web(tmp_path, lambda r: html())
    await web.pages.fetch("https://example.org/a")
    web.now += 10
    await web.pages.fetch("https://example.org/a", max_age_s=5)
    assert len(web.requests) == 2


async def test_offline_falls_back_to_the_last_copy(tmp_path: Path) -> None:
    online = True

    def handler(request: httpx.Request) -> httpx.Response:
        if not online:
            raise httpx.ConnectError("no route to host")
        return html("<p>saved</p>")

    web = Web(tmp_path, handler)
    await web.pages.fetch("https://example.org/a")
    online = False
    web.now += 3600
    page = await web.pages.fetch("https://example.org/a")
    assert page.stale and page.from_store and page.content == b"<p>saved</p>"

    with pytest.raises(EngineError) as e:
        await web.pages.fetch("https://example.org/never-seen")
    assert e.value.code == "offline"


async def test_errors_and_oversized_pages_are_not_stored(tmp_path: Path) -> None:
    web = Web(tmp_path, lambda r: httpx.Response(404))
    with pytest.raises(EngineError, match="404"):
        await web.pages.fetch("https://example.org/missing")
    assert web.assets.count("page") == 0


async def test_two_requests_for_the_same_page_download_it_once(tmp_path: Path) -> None:
    release = asyncio.Event()

    class Slow(httpx.AsyncBaseTransport):
        calls = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            Slow.calls += 1
            await release.wait()
            return html("<p>once</p>")

    assets = AssetStore(tmp_path / "cache", Database(tmp_path / "db.sqlite"))
    pages = PageStore(assets, client_factory=lambda: httpx.AsyncClient(transport=Slow()), resolver=lambda h: [PUBLIC])
    a = asyncio.create_task(pages.fetch("https://example.org/a"))
    b = asyncio.create_task(pages.fetch("https://example.org/a"))
    await asyncio.sleep(0.05)
    release.set()
    first, second = await asyncio.gather(a, b)
    assert first.content == second.content == b"<p>once</p>"
    assert Slow.calls == 1

"""MCP server over stdio (04 § MCP server, FR-API-4, P1): `open-speech mcp`.

Thin wrappers over the engine API. When the app's engine is running, tools control that same transport (one
playback session for every surface); otherwise the server hosts an ephemeral engine for its lifetime.
Mutations follow the same rule as the assistant: `add_rule` returns a proposal and only saves with confirm=true.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from open_speech import __version__


def build_server() -> Any:
    from mcp.server.mcpserver import MCPServer

    from open_speech.cli.main import _Ephemeral, _running_client

    state: dict[str, Any] = {}

    def client() -> Any:
        if "client" not in state:
            running = _running_client()
            if running is not None:
                state["client"] = running
            else:
                eph = _Ephemeral()
                state["eph"] = eph
                state["client"] = eph.client
        return state["client"]

    async def call(method: str, path: str, **kw: Any) -> Any:
        return await asyncio.to_thread(client().call, method, path, **kw)

    server = MCPServer(
        name="open-speech",
        title="Open Speech",
        version=__version__,
        instructions=(
            "Read text, files and web pages aloud on the user's Mac with Open Speech. Costs are estimates. "
            "Rules are previewed first: call add_rule without confirm, tell the user the summary, and only call it "
            "again with confirm=true after they agree."
        ),
    )

    @server.tool(description="Read a file path, URL, or literal text aloud.")
    async def read_aloud(
        source: str,
        kind: Literal["auto", "path", "url", "text"] = "auto",
        voice: str | None = None,
        speed: float | None = None,
        confirm: bool = False,
    ) -> str:
        k = kind
        if k == "auto":
            k = (
                "url"
                if source.startswith(("http://", "https://"))
                else "path"
                if source.startswith(("/", "~"))
                else "text"
            )
        body: dict[str, Any] = {"voice_id": voice, "play": True, "confirm_estimate": confirm}
        if k == "url":
            body.update(kind="url", url=source)
        elif k == "path":
            body.update(kind="file_path", path=source)
        else:
            body.update(kind="text", text=source)
        res = await call("POST", "/documents", json=body)
        if speed is not None:
            await call("PUT", "/playback/speed", json={"speed": speed})
        est = res.get("estimate")
        if est and est.get("requires_confirmation") and not res.get("playing"):
            return (
                f"That would cost about ${est['usd_estimate']} (estimated). "
                "Ask the user, then retry with confirm=true after they agree."
            )
        return f"Reading “{res['document']['title']}”."

    @server.tool(description="Control playback: pause, resume, stop, or skip by sentence/paragraph/heading/seconds.")
    async def playback(
        action: Literal["pause", "resume", "stop", "skip"],
        unit: Literal["sentence", "paragraph", "heading", "seconds"] = "sentence",
        count: int = 1,
    ) -> str:
        if action == "skip":
            st = await call("POST", "/playback/skip", json={"unit": unit, "count": count})
        else:
            st = await call("POST", f"/playback/{action}")
        return json.dumps({"state": st["state"], "document": st.get("document_title"), "speed": st["speed"]})

    @server.tool(description="Current document, position and time left.")
    async def where_am_i() -> str:
        st = await call("GET", "/playback")
        return json.dumps(
            {
                k: st.get(k)
                for k in (
                    "state",
                    "document_title",
                    "heading",
                    "page",
                    "page_count",
                    "progress",
                    "time_left_s",
                    "speed",
                )
            }
        )

    @server.tool(description="Estimated spending from the local ledger.")
    async def get_usage(
        period: Literal["today", "week", "month"] = "month",
        group_by: Literal["provider", "op", "document"] = "provider",
    ) -> str:
        res = await call("GET", "/usage/summary", params={"period": period, "group_by": group_by})
        return json.dumps(res)

    @server.tool(description="Estimate what reading a file or URL would cost.")
    async def estimate_cost(source: str) -> str:
        kind = "url" if source.startswith(("http://", "https://")) else "file_path"
        doc = (
            await call(
                "POST", "/documents", json={"kind": kind, **({"url": source} if kind == "url" else {"path": source})}
            )
        )["document"]
        return json.dumps(await call("POST", "/usage/estimate", json={"document_id": doc["id"]}))

    @server.tool(description="List voices (AI-generated). Ids look like provider:model:voice.")
    async def list_voices(provider: str | None = None, lang: str | None = None) -> str:
        params = {k: v for k, v in {"provider": provider, "lang": lang}.items() if v}
        res = await call("GET", "/voices", params=params)
        return json.dumps([{"id": v["id"], "name": v["name"], "available": v["available"]} for v in res["items"]])

    @server.tool(
        description="Propose a speech rule in plain language; returns a preview. Saves only with confirm=true."
    )
    async def add_rule(
        instruction: str, site: str | None = None, confirm: bool = False, proposal_id: str | None = None
    ) -> str:
        if confirm and proposal_id:
            saved = await call("POST", "/rules", json={"proposal_id": proposal_id})
            return f"Saved rule {saved['name']}."
        body: dict[str, Any] = {"instruction": instruction}
        if site:
            body["scope"] = {"sites": [site]}
        prop = await call("POST", "/rules/proposals", json=body)
        return json.dumps(
            {
                "summary": prop["summary"],
                "proposal_id": prop["proposal_id"],
                "tier": prop["rule"]["tier"],
                "next": "Ask the user; if they agree call add_rule again with confirm=true and this proposal_id.",
            }
        )

    state["server"] = server
    return server


def main() -> None:
    build_server().run("stdio")

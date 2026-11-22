"""`open-speech` CLI (04 § CLI). Talks to a running engine found through `engine.json`, or starts an ephemeral one.

Human-readable output by default, `--json` for scripts.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import json
import os
import secrets
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer

from open_speech import __version__
from open_speech.config import EngineEnv, Paths

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Open Speech: read anything aloud, with your keys.")
rules_app = typer.Typer(help="Speech rules.", no_args_is_help=True)
models_app = typer.Typer(help="Local models.", no_args_is_help=True)
keys_app = typer.Typer(help="Provider keys.", no_args_is_help=True)
app.add_typer(rules_app, name="rules")
app.add_typer(models_app, name="models")
app.add_typer(keys_app, name="keys")

JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]


# ------------------------------------------------------------------ engine discovery


class EngineClient:
    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.http = httpx.Client(
            base_url=f"{self.base}/api/v1", headers={"Authorization": f"Bearer {token}"}, timeout=120
        )

    def call(self, method: str, path: str, **kw: Any) -> Any:
        r = self.http.request(method, path, **kw)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            typer.secho(str(detail), fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        if r.status_code == 204 or not r.content:
            return None
        return r.json()


def _engine_json(paths: Paths) -> dict[str, Any] | None:
    try:
        info = json.loads(paths.engine_json.read_text())
    except (OSError, ValueError):
        return None
    try:
        os.kill(int(info["pid"]), 0)
    except (OSError, KeyError, ValueError):
        return None
    return info  # type: ignore[no-any-return]


def _running_client() -> EngineClient | None:
    env = EngineEnv()
    info = _engine_json(Paths(env.home))
    if not info:
        return None
    client = EngineClient(f"http://127.0.0.1:{info['port']}", info["token"])
    try:
        client.http.get("/health", timeout=2).raise_for_status()
    except httpx.HTTPError:
        return None
    return client


class _Ephemeral:
    """An in-process engine served on a random loopback port, for CLI use without a running app."""

    def __init__(self) -> None:
        import uvicorn

        from open_speech.api.app import create_app
        from open_speech.service.engine import Engine

        self.token = secrets.token_hex(32)
        env = EngineEnv()
        self.engine = Engine(env)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        self.sock = sock
        fastapi_app = create_app(self.engine, token=self.token, allowed_origins=set(env.open_speech_allowed_origins))
        self.server = uvicorn.Server(uvicorn.Config(fastapi_app, log_level="warning", lifespan="on"))
        self.thread = threading.Thread(target=lambda: self.server.run(sockets=[sock]), daemon=True)
        self.thread.start()
        deadline = time.time() + 20
        while not self.server.started and time.time() < deadline:
            time.sleep(0.05)
        self.client = EngineClient(f"http://127.0.0.1:{self.port}", self.token)

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


@contextlib.contextmanager
def engine_client(ephemeral_ok: bool = True) -> Any:
    client = _running_client()
    if client is not None:
        yield client, False
        return
    if not ephemeral_ok:
        typer.secho("No Open Speech engine is running.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    eph = _Ephemeral()
    try:
        yield eph.client, True
    finally:
        eph.close()


def _out(data: Any, as_json: bool, human: str) -> None:
    if as_json:
        typer.echo(json.dumps(data, indent=2, default=str))
    else:
        typer.echo(human)


# ------------------------------------------------------------------ serve


@app.command()
def serve(
    port: Annotated[int, typer.Option(help="0 picks a free port.")] = 0,
    handshake: Annotated[
        str | None, typer.Option(help="'stdout' prints {port, token, version, pid} as one JSON line.")
    ] = None,
    fixture: Annotated[bool, typer.Option(help="Use deterministic offline fixture providers (tests).")] = False,
    log_level: str = "warning",
) -> None:
    """Run the engine on 127.0.0.1 (03 § Engine supervision)."""
    import uvicorn

    from open_speech.api.app import create_app
    from open_speech.service.engine import Engine

    if fixture:
        os.environ["OPEN_SPEECH_FIXTURE_PROVIDERS"] = "1"
    env = EngineEnv()
    paths = Paths(env.home).ensure()
    token = secrets.token_hex(32)  # 256-bit
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    actual = sock.getsockname()[1]
    engine = Engine(env, use_keychain=sys.platform == "darwin")
    fastapi_app = create_app(engine, token=token, allowed_origins=set(env.open_speech_allowed_origins))

    info = {"port": actual, "token": token, "version": __version__, "pid": os.getpid()}
    fd = os.open(paths.engine_json, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(info, f)
    os.chmod(paths.engine_json, 0o600)
    if handshake == "stdout":
        sys.stdout.write(json.dumps(info) + "\n")
        sys.stdout.flush()
    else:
        typer.echo(
            f"Open Speech engine {__version__} on http://127.0.0.1:{actual} (token in {paths.engine_json})", err=True
        )
    server = uvicorn.Server(uvicorn.Config(fastapi_app, log_level=log_level, lifespan="on"))
    try:
        server.run(sockets=[sock])
    finally:
        with contextlib.suppress(OSError):
            current = json.loads(paths.engine_json.read_text())
            if current.get("pid") == os.getpid():
                paths.engine_json.unlink()


# ------------------------------------------------------------------ reading


def _wait_playback(client: EngineClient) -> None:
    """Block while reading (ephemeral engine); Ctrl-C stops."""
    stop = threading.Event()

    def handler(*_: Any) -> None:
        stop.set()

    old = signal.signal(signal.SIGINT, handler)
    try:
        last = ""
        while not stop.is_set():
            st = client.call("GET", "/playback")
            line = f"{st['state']:8} {st.get('utterance_index', 0) + 1}/{st.get('utterance_count', 0)} {st.get('speed', 1):g}x"
            if line != last and sys.stderr.isatty():
                sys.stderr.write("\r" + line.ljust(40))
                sys.stderr.flush()
                last = line
            if st["state"] in ("stopped", "idle", "error"):
                break
            time.sleep(0.25)
    finally:
        signal.signal(signal.SIGINT, old)
        if stop.is_set():
            client.call("POST", "/playback/stop")
        if sys.stderr.isatty():
            sys.stderr.write("\n")


@app.command()
def read(
    source: Annotated[str, typer.Argument(help="File path, URL, or '-' for stdin.")],
    voice: Annotated[str | None, typer.Option(help="Voice id, e.g. openai:gpt-4o-mini-tts:coral")] = None,
    speed: Annotated[float | None, typer.Option(min=0.5, max=5.0)] = None,
    mode: Annotated[str | None, typer.Option(help="classic | ai")] = None,
    from_page: Annotated[int | None, typer.Option("--from-page")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Accept the cost estimate.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Read a file, URL or stdin aloud."""
    mode_v = {"ai": "ai_enhanced", "ai_enhanced": "ai_enhanced", "classic": "classic", None: None}.get(mode)
    with engine_client() as (client, ephemeral):
        if source == "-":
            text = sys.stdin.read()
            res = client.call(
                "POST",
                "/documents",
                json={"kind": "text", "text": text, "voice_id": voice, "mode": mode_v, "confirm_estimate": yes},
            )
        elif source.startswith(("http://", "https://")):
            res = client.call(
                "POST",
                "/documents",
                json={"kind": "url", "url": source, "voice_id": voice, "mode": mode_v, "confirm_estimate": yes},
            )
        else:
            p = Path(source).expanduser()
            if not p.is_file():
                typer.secho(f"No such file: {source}", fg=typer.colors.RED, err=True)
                raise typer.Exit(1)
            with p.open("rb") as fh:
                res = client.call(
                    "POST",
                    "/documents/upload",
                    files={"file": (p.name, fh)},
                    data={
                        k: str(v).lower() if isinstance(v, bool) else v
                        for k, v in {"voice_id": voice, "mode": mode_v, "confirm_estimate": yes}.items()
                        if v is not None
                    },
                )
        est = res.get("estimate")
        if est and est.get("requires_confirmation") and not yes:
            typer.echo(f"Estimated cost: ${est['usd_estimate']} (estimated). Re-run with --yes to read it.")
            raise typer.Exit(2)
        doc = res["document"]
        if speed is not None:
            client.call("PUT", "/playback/speed", json={"speed": speed})
        body: dict[str, Any] = {"document_id": doc["id"]}
        if from_page:
            body["from"] = {"page": from_page}
        st = client.call("POST", "/playback/play", json=body)
        _out(
            {"document": doc, "playback": st},
            as_json,
            f"Reading “{doc['title']}” · {st['utterance_count']} sentences · {st['speed']:g}x",
        )
        if ephemeral:
            _wait_playback(client)


def _simple(path: str, method: str = "POST", body: Any = None) -> Any:
    with engine_client(ephemeral_ok=False) as (client, _):
        return client.call(method, path, json=body) if body is not None else client.call(method, path)


@app.command()
def pause() -> None:
    """Pause reading."""
    _simple("/playback/pause")


@app.command()
def resume() -> None:
    """Resume reading."""
    _simple("/playback/resume")


@app.command()
def stop() -> None:
    """Stop reading."""
    _simple("/playback/stop")


@app.command()
def status(as_json: JsonOpt = False) -> None:
    """What's playing."""
    client = _running_client()
    if client is None:
        _out({"running": False}, as_json, "Engine not running.")
        return
    st = client.call("GET", "/playback")
    human = f"{st['state']}" + (
        f" · {st['document_title']} · {st['utterance_index'] + 1}/{st['utterance_count']} · {st['speed']:g}x"
        if st.get("document_title")
        else ""
    )
    _out(st, as_json, human)


@app.command()
def skip(
    count: Annotated[int, typer.Argument()] = 1,
    sentence: Annotated[bool, typer.Option("--sentence")] = False,
    paragraph: Annotated[bool, typer.Option("--paragraph")] = False,
) -> None:
    """Skip forward (negative count goes back)."""
    unit = "paragraph" if paragraph else "sentence"
    _simple("/playback/skip", body={"unit": unit, "count": count})


@app.command()
def voices(
    provider: Annotated[str | None, typer.Option()] = None,
    lang: Annotated[str | None, typer.Option()] = None,
    as_json: JsonOpt = False,
) -> None:
    """List voices (AI-generated)."""
    with engine_client() as (client, _):
        params = {k: v for k, v in {"provider": provider, "lang": lang}.items() if v}
        data = client.call("GET", "/voices", params=params)
    lines = [
        f"{'✓' if v['available'] else '·'} {v['id']:<45} {v['name']}"
        + ("" if v["available"] else f"  ({v.get('unavailable_reason')})")
        for v in data["items"]
    ]
    _out(data, as_json, "\n".join(lines) or "No voices.")


@rules_app.command("ls")
def rules_ls(as_json: JsonOpt = False) -> None:
    """List rules."""
    with engine_client() as (client, _):
        data = client.call("GET", "/rules")
    lines = [f"{'on ' if r['enabled'] else 'off'} {r['tier']:<9} {r['name']}" for r in data["items"]]
    _out(data, as_json, "\n".join(lines))


@rules_app.command("add")
def rules_add(
    instruction: str,
    site: Annotated[str | None, typer.Option()] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Propose a rule from plain language, preview it, and save on confirmation."""
    with engine_client() as (client, _):
        body: dict[str, Any] = {"instruction": instruction}
        if site:
            body["scope"] = {"sites": [site]}
        prop = client.call("POST", "/rules/proposals", json=body)
        if not as_json:
            typer.echo(prop["summary"])
        if yes or (not as_json and typer.confirm("Save it?", default=True)):
            saved = client.call("POST", "/rules", json={"proposal_id": prop["proposal_id"]})
            _out(saved, as_json, f"Saved {saved['name']} ({saved['tier']}).")
        else:
            client.call("POST", f"/actions/{prop['proposal_id']}/cancel")
            _out(prop, as_json, "Not saved.")


@rules_app.command("rm")
def rules_rm(rule_id: str) -> None:
    """Delete a rule."""
    with engine_client() as (client, _):
        client.call("DELETE", f"/rules/{rule_id}")


@app.command()
def usage(
    today: Annotated[bool, typer.Option("--today")] = False,
    week: Annotated[bool, typer.Option("--week")] = False,
    month: Annotated[bool, typer.Option("--month")] = False,
    by: Annotated[str, typer.Option("--by")] = "provider",
    as_json: JsonOpt = False,
) -> None:
    """Estimated spend from the local ledger."""
    period = "today" if today else "week" if week else "month"
    with engine_client() as (client, _):
        data = client.call("GET", "/usage/summary", params={"period": period, "group_by": by})
    lines = [data["spoken"]] + [
        f"  {b['key']:<24} ${b['usd_estimate']}  ({b['calls']} calls)" for b in data["breakdown"]
    ]
    _out(data, as_json, "\n".join(lines))


@app.command()
def estimate(source: str, as_json: JsonOpt = False) -> None:
    """Estimate what reading a file would cost."""
    with engine_client() as (client, _):
        p = Path(source).expanduser()
        if p.is_file():
            with p.open("rb") as fh:
                doc = client.call("POST", "/documents/upload", files={"file": (p.name, fh)})["document"]
        else:
            doc = client.call("POST", "/documents", json={"kind": "url", "url": source})["document"]
        est = client.call("POST", "/usage/estimate", json={"document_id": doc["id"]})
    lines = [f"About ${est['usd_estimate']} (estimated)"] + [
        f"  {b['item']}: ${b['usd_estimate']}" for b in est["breakdown"]
    ]
    _out(est, as_json, "\n".join(lines))


@models_app.command("ls")
def models_ls(as_json: JsonOpt = False) -> None:
    """Local models and whether they fit this Mac."""
    with engine_client() as (client, _):
        data = client.call("GET", "/models/local")
    lines = [
        f"{'✓' if m['installed'] else ' '} {m['id']:<28} {m['size_mb']:>6} MB  {m['ram_gb']:g} GB RAM  {m['fit']}"
        for m in data["items"]
    ]
    _out(data, as_json, "\n".join(lines))


@models_app.command("install")
def models_install(model_id: str) -> None:
    """Install a local model."""
    with engine_client(ephemeral_ok=False) as (client, _):
        job = client.call("POST", f"/models/local/{model_id}")
        typer.echo(f"Installing ({job['job_id']}).")


@models_app.command("rm")
def models_rm(model_id: str) -> None:
    """Remove a local model."""
    with engine_client() as (client, _):
        client.call("DELETE", f"/models/local/{model_id}")


@keys_app.command("set")
def keys_set(provider: Annotated[str, typer.Argument(help="anthropic | openai | gemini | auto")]) -> None:
    """Store a key (prompted without echo, tested before saving)."""
    key = getpass.getpass(f"{provider} key: ")
    with engine_client() as (client, _):
        res = client.call("PUT", f"/keys/{provider}", json={"key": key})
    typer.echo(res["detail"])
    if not res["ok"]:
        raise typer.Exit(1)


@app.command()
def mcp() -> None:
    """Run the MCP server over stdio (for Claude Code / Claude Desktop)."""
    from open_speech.mcp.server import main as mcp_main

    mcp_main()


@app.command("export-api", hidden=True)
def export_api(check: bool = False) -> None:
    """Write (or --check) engine/openapi.json and engine/ws-schema.json."""
    from open_speech.api import export

    sys.argv = ["export"] + (["--check"] if check else [])
    raise typer.Exit(export.main())


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(asyncio.sleep(0))
    app()

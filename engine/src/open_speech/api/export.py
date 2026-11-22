"""Write the committed API contracts: engine/openapi.json and engine/ws-schema.json (04 § Versioning)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from open_speech.api.app import create_app
from open_speech.api.ws_schema import ws_json_schema

ENGINE_DIR = Path(__file__).resolve().parents[3]


def render() -> dict[str, str]:
    app = create_app(None, token="")
    openapi = app.openapi()
    openapi.setdefault("components", {}).setdefault("securitySchemes", {})["bearer"] = {
        "type": "http",
        "scheme": "bearer",
    }
    openapi["security"] = [{"bearer": []}]
    return {
        "openapi.json": json.dumps(openapi, indent=2, sort_keys=False) + "\n",
        "ws-schema.json": json.dumps(ws_json_schema(), indent=2) + "\n",
    }


def main() -> int:
    check = "--check" in sys.argv
    stale = []
    for name, content in render().items():
        path = ENGINE_DIR / name
        if check:
            if not path.exists() or path.read_text() != content:
                stale.append(name)
        else:
            path.write_text(content)
    if stale:
        print(f"Stale API contract(s): {', '.join(stale)}. Run: uv run python -m open_speech.api.export")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

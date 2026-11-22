import os as _os

__version__ = "0.1.0"

# Local-first: onnxruntime (local Kokoro) otherwise keeps a device ID under ~/Library and uploads usage
# telemetry. Off for every entry point (app, CLI, MCP) unless the user overrides it.
_os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")

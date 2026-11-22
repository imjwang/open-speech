"""Model weights shipped read-only inside the packaged app (FR-SETUP-3: speak from first launch).

The packaged shell starts the engine with `OPEN_SPEECH_BUNDLED_RUNTIME=<App>.app/Contents/Resources/engine`
(app/src/main/engine/spawn-spec.ts). `app/scripts/bundle-engine.sh` puts weights in `<runtime>/models/`, in the
same `<provider>/<model>/` layout as `Paths.models`. A complete copy in app data wins over the bundled one; the
bundled copy is only ever read, never written to or removed (it lives inside the signed app bundle).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

BUNDLED_RUNTIME_ENV = "OPEN_SPEECH_BUNDLED_RUNTIME"


def bundled_models_dir(env: Mapping[str, str] | None = None) -> Path | None:
    """`<runtime>/models` when running from a packaged build that ships one, else None."""
    root = (os.environ if env is None else env).get(BUNDLED_RUNTIME_ENV)
    if not root:
        return None
    d = Path(root) / "models"
    return d if d.is_dir() else None

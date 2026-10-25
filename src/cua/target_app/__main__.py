"""Standalone entrypoint: ``python -m cua.target_app``.

Env vars:
  TARGET_APP_HOST   default 127.0.0.1
  TARGET_APP_PORT   default 5055
  TARGET_APP_DEBUG  set to "1" to enable Flask debug/reload (dev only)
"""

from __future__ import annotations

import os

from .app import create_app


def main() -> None:
    app = create_app()
    host = os.environ.get("TARGET_APP_HOST", "127.0.0.1")
    port = int(os.environ.get("TARGET_APP_PORT", "5055"))
    debug = os.environ.get("TARGET_APP_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()

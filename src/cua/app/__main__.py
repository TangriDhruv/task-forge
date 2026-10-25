"""Standalone entrypoint: ``python -m cua.app``.

Env vars:
  CUA_APP_HOST   default 127.0.0.1
  CUA_APP_PORT   default 5100
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

from .server import create_app


def main() -> None:
    load_dotenv()
    app = create_app()
    host = os.environ.get("CUA_APP_HOST", "127.0.0.1")
    port = int(os.environ.get("CUA_APP_PORT", "5100"))
    print(f"Dashboard: http://{host}:{port}")
    # threaded=True: the discovery loop runs on its own background thread per
    # run, but the dashboard still needs to serve concurrent state/screenshot
    # polling and relay requests while that run is in progress.
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()

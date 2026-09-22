"""CLI entrypoint: ``python -m tree_options.trex_web``.

Mirrors the ``trex-monitor`` / ``enter`` flag shape (``--state-dir``,
``--host``, ``--port``) so the ``trex-web.service`` unit can use the
same idioms and operators can copy-paste between services. The web lane
adds ``--plans-dir`` because it reads the operator-authored TOML files;
the monitor + enter runners use the broker directly and don't need it.
"""

from __future__ import annotations

import argparse

import uvicorn

from tree_options.trex_web.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tree_options.trex_web",
        description="Read-only HTTP status panel for the trex execution lane.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (loopback only).")
    parser.add_argument("--port", type=int, default=8090, help="Bind port.")
    parser.add_argument("--state-dir", default=None, help="Override TREX_STATE.")
    parser.add_argument("--plans-dir", default=None, help="Override TREX_PLANS_DIR.")
    args = parser.parse_args()

    app = create_app(state_dir=args.state_dir, plans_dir=args.plans_dir)
    # Single worker: the lane is broker-free and small. ``reload=False`` is
    # intentional — the service is owned by systemd, hot-reload is a foot-gun
    # in this layout (the monitor and the web service both touch state files).
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    main()

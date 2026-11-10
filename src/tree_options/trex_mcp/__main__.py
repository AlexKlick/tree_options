"""Run the isolated TREX MCP research gateway; default loopback binding."""

from __future__ import annotations

import os

import uvicorn

from .server import create_app


def main() -> None:
    # Configuration is validated before the socket starts accepting clients.
    app = create_app()
    uvicorn.run(
        app,
        host=os.environ.get("TREX_MCP_BIND", "127.0.0.1"),
        port=int(os.environ.get("TREX_MCP_PORT", "8093")),
        access_log=False,
    )


if __name__ == "__main__":
    main()

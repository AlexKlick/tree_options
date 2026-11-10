"""Read-only, tenant-scoped MCP projection of TREX market research.

No TREX execution, broker, portfolio or command imports are permitted here.
"""

from .server import create_app

__all__ = ["create_app"]

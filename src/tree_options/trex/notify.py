"""Operator push notifications over ntfy (https://ntfy.sh or self-hosted).

Configured by ``~/.config/trex/notify.env`` (chmod 600, never in the repo):

    NTFY_URL=https://ntfy.sh/<random-unguessable-topic>
    NTFY_TOKEN=            # optional, for access-controlled servers

The topic URL is a capability (anyone holding it can read and post), so it
never appears in logs or exceptions. Messages are minimal by contract: the
callers send no URLs, hostnames, account ids or money. Unconfigured = a
quiet no-op; failures return False and never raise.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".config" / "trex" / "notify.env"
TIMEOUT_S = 10.0

# (url, body, headers, timeout) -> HTTP status
Transport = Callable[[str, bytes, dict[str, str], float], int]


def urllib_transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, str] | None:
    try:
        text = path.read_text()
    except OSError:
        return None
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    url = values.get("NTFY_URL", "")
    if not url.startswith("https://"):
        return None
    return {"url": url, "token": values.get("NTFY_TOKEN", "")}


def _header_safe(text: str) -> str:
    # HTTP headers are latin-1; keep titles plain ASCII (ntfy shows them as-is)
    return text.replace("—", "-").encode("ascii", "replace").decode("ascii")


def send(
    cfg: dict[str, str] | None,
    title: str,
    message: str,
    priority: str = "default",
    *,
    transport: Transport = urllib_transport,
) -> bool:
    if not cfg:
        return False
    headers = {"Title": _header_safe(title), "Priority": priority, "Tags": "warning"}
    if cfg.get("token"):
        headers["Authorization"] = f"Bearer {cfg['token']}"
    try:
        status = transport(cfg["url"], message.encode("utf-8"), headers, TIMEOUT_S)
    except Exception as exc:  # never echo: the message can embed the topic URL
        print(f"notify: push failed ({type(exc).__name__})", file=sys.stderr)
        return False
    if status >= 300:
        print(f"notify: push rejected (HTTP {status})", file=sys.stderr)
        return False
    return True

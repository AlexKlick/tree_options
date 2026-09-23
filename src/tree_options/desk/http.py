"""A keyless HTTP GET with explicit headers for the desk's daily jobs.

Every desk job takes a :class:`Get` so tests inject fakes (no network in
tests). Non-2xx statuses come back as ``(status, b"")``, never raised;
transport failures (DNS, timeout, reset) raise and the caller isolates
them per source. A body over ``max_bytes`` is refused, never truncated.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Protocol

from tree_options.trex.discovery.market import MOZILLA_UA

MAX_BYTES = 64_000_000
TIMEOUT_S = 30.0
# Plain tool identity for hosts that reset browser-looking scripted clients
# (FRED's edge answered a Chrome UA over HTTP/2 with INTERNAL_ERROR on
# 2026-09-23 and served a plain client over HTTP/1.1).
TOOL_UA = "tree-options-desk/1.0 (daily market data)"
# Nasdaq's API wants a full browser identity plus its own Origin/Referer.
BROWSER_HEADERS: Mapping[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}
CDN_HEADERS: Mapping[str, str] = {"User-Agent": MOZILLA_UA}


class BodyTooLarge(RuntimeError):
    """The response exceeded the caller's byte cap."""


class IncompleteBody(RuntimeError):
    """The body did not match its Content-Length (a cut connection)."""


class Get(Protocol):
    def __call__(
        self, url: str, *, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]: ...


def urllib_get(
    url: str, *, headers: Mapping[str, str], timeout: float = TIMEOUT_S
) -> tuple[int, bytes]:
    """GET ``url``. A bounded ``read(amt)`` returns SHORT data without raising
    when the connection ends before Content-Length (CPython http.client), so
    the advertised length is enforced here; without one (chunked), a cut
    chunk raises inside http.client. Both paths keep the MAX_BYTES cap."""
    req = urllib.request.Request(url, headers=dict(headers))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            advertised = resp.headers.get("Content-Length")
            want: int | None = None
            if advertised is not None:
                try:
                    want = int(advertised)
                except ValueError:
                    raise IncompleteBody(f"{url}: bad Content-Length {advertised!r}") from None
                if want > MAX_BYTES:
                    raise BodyTooLarge(f"{url}: Content-Length {want} over {MAX_BYTES} bytes")
            body = resp.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise BodyTooLarge(f"{url}: body over {MAX_BYTES} bytes")
            if want is not None and len(body) != want:
                raise IncompleteBody(f"{url}: {len(body)} of {want} advertised bytes")
            return int(resp.status), body
    except urllib.error.HTTPError as exc:
        return int(exc.code), b""

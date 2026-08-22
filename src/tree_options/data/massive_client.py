"""Massive (Polygon) free-tier HTTP client (M4-A plan §3 WS-D1).

The vendor is Polygon, now branded Massive; the endpoints are still on
`api.polygon.io`. This module owns TRANSPORT ONLY — key custody, request
spacing, the on-disk response cache, pagination, and the vendor's
peculiar error encodings. It never interprets option semantics; that is
`tree_options.data.massive_options`.

Four properties this module exists to guarantee (each has a test):

1. THE KEY NEVER LEAVES. `load_api_key` reads `POLYGON_API_KEY`, else the
   0600 file `~/.config/tree_options/polygon.key`. The key is appended to
   a URL only at request time, is never part of a cache key, a cache
   file's name or bytes, a `repr`, or ANY exception message — every
   outbound string goes through `redact` / `display_url` first.
2. QUOTA IS PRECIOUS. The free tier allows 5 requests/minute; the
   `RateGovernor` spaces real requests by `60 / rate` seconds against an
   injectable clock, and a CACHE HIT CONSUMES NO TOKEN (the cache is
   consulted before the governor is ever touched).
3. NOT-ENTITLED IS NOT AN HTTP ERROR. Probed 2026-08-21: a request for
   data outside the tier returns **HTTP 200** with body
   `{"status": "NOT_AUTHORIZED", "message": "You are not entitled..."}`.
   A client that only checks the HTTP status silently treats that as a
   successful empty response. `_require_ok_status` reads the BODY and
   raises `MassiveNotEntitledError`, echoing the vendor message (vendor
   text, safe to surface) and naming the endpoint.
4. NUMBERS STAY EXACT. `loads_exact` parses with `parse_float=Decimal`,
   which hands the Decimal constructor the number's RAW SOURCE TEXT — no
   float is ever constructed, so `"strike_price": 587.5` becomes
   `Decimal("587.5")` exactly, not a coerced binary approximation. This
   is the sanctioned way to satisfy the repo's no-float-prices rule
   against a JSON-number API; integer literals go through `int` (also
   exact). No `str(float)` coercion happens anywhere in this lane.

Pagination follows `next_url` (cursor-bearing, key-free as delivered) and
REFUSES a `next_url` pointing at a different host — appending the key to
a redirected host would exfiltrate it. A repeated `next_url` is likewise
refused as a cycle rather than followed forever. Exhausting `max_pages`
while a `next_url` remains raises rather than truncating: a silently
short contract master would misdescribe coverage (M1 "zero silent drops").

Each attempt is classified on its own merits: HTTP 429, HTTP 502/503/504,
and transport-level failures share ONE bounded backoff cadence (with a
retry counter each); HTTP 401/403 is a terminal
`MassiveAuthRejectedError` — never retried, never cached — and every
other non-200 is terminal. Cache writes are atomic (a dot-prefixed
staging file plus `os.replace`), and a cached body that no longer
DECODES self-heals: the entry is discarded and the wire refetches it,
so one torn write cannot poison every future call.

Timing here is wall-clock spacing in float seconds via `time.monotonic`,
NOT date arithmetic, so the repo's naive-date-arithmetic ban (no
`timedelta`/`weekday` outside `time/`) is respected by construction.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

MASSIVE_BASE_URL = "https://api.polygon.io"
MASSIVE_PROVIDER = "massive-polygon-free/1"
API_KEY_ENV_VAR = "POLYGON_API_KEY"
DEFAULT_KEY_PATH = Path.home() / ".config" / "tree_options" / "polygon.key"
MASSIVE_CACHE_DIR_ENV_VAR = "TREE_OPTIONS_MASSIVE_CACHE"


def default_cache_dir() -> Path:
    """The default cache root: `TREE_OPTIONS_MASSIVE_CACHE` when set, else
    the checkout's `artifacts/massive-cache`, derived from THIS file's
    location — never a hardcoded home path, so a second checkout (or
    another user) gets its own cache instead of sharing one. The env var
    is read at call time, so a wrapper can retarget it without
    re-importing; `DEFAULT_CACHE_DIR` is merely the import-time value."""
    from_env = os.environ.get(MASSIVE_CACHE_DIR_ENV_VAR)
    if from_env:
        return Path(from_env)
    return Path(__file__).resolve().parents[3] / "artifacts" / "massive-cache"


DEFAULT_CACHE_DIR = default_cache_dir()

# Probed 2026-08-21: the free tier admits 5 requests/minute; the 6th in a
# rolling minute answers HTTP 429.
FREE_TIER_REQUESTS_PER_MINUTE = 5

# One backoff cadence covers these: 429 is quota, 502/503/504 is the
# vendor's edge. Everything else (400/404/422/500/...) is terminal.
RETRYABLE_HTTP_STATUSES = frozenset({429, 502, 503, 504})

# The key itself was refused: terminal, never retried, never cached.
AUTH_REJECTED_HTTP_STATUSES = frozenset({401, 403})

# Vendor body statuses that mean "this is real data".
OK_STATUSES = frozenset({"OK", "DELAYED"})
NOT_AUTHORIZED_STATUS = "NOT_AUTHORIZED"

# The query parameter carrying the secret. Compared case-insensitively:
# the vendor accepts `apiKey`, and a stray `apikey` must be scrubbed too.
API_KEY_PARAM = "apiKey"

USER_AGENT = "tree-options/0.1 (research; contact via repo owner)"

REDACTED = "REDACTED"

JsonObject = Mapping[str, Any]


# ---- errors (fail closed, never key-bearing) -------------------------------


class MassiveError(RuntimeError):
    """Base class for Massive/Polygon client failures."""


class MassiveAuthError(MassiveError):
    """No usable API key (absent, empty, or unsafely stored).

    The message names the env var and the path — NEVER the key bytes."""


class MassiveAuthRejectedError(MassiveError):
    """HTTP 401/403: the vendor refused the API key itself.

    Terminal — never retried and never cached (the raise precedes the
    cache write by construction). The only cure is a new key, so the
    message says so and points at the runbook; `http_status` pins which
    of the two statuses answered, and the message appends the (redacted)
    vendor body snippet, if any."""

    def __init__(self, endpoint: str, http_status: int, detail: str = "") -> None:
        self.endpoint = endpoint
        self.http_status = http_status
        suffix = f" [{detail}]" if detail else ""
        super().__init__(
            f"{endpoint}: HTTP {http_status}: key rejected — rotate the key"
            f" (see docs/m4-massive-runbook.md){suffix}"
        )


class MassiveNotEntitledError(MassiveError):
    """HTTP 200 carrying `status: NOT_AUTHORIZED` — the free tier does not
    cover this endpoint. Carries the vendor's upgrade text verbatim."""

    def __init__(self, endpoint: str, vendor_message: str) -> None:
        self.endpoint = endpoint
        self.vendor_message = vendor_message
        super().__init__(
            f"{endpoint}: not entitled on this API tier — vendor says: {vendor_message}"
        )


class MassiveApiError(MassiveError):
    """A non-OK HTTP status, or an OK HTTP status with an unusable body."""

    def __init__(self, endpoint: str, detail: str, *, http_status: int | None = None) -> None:
        self.endpoint = endpoint
        self.http_status = http_status
        prefix = f"HTTP {http_status}: " if http_status is not None else ""
        super().__init__(f"{endpoint}: {prefix}{detail}")


class MassiveRateLimitError(MassiveError):
    """HTTP 429 survived the bounded backoff; the caller must slow down."""


class MassiveTransportError(MassiveError):
    """The HTTP layer failed (DNS/TLS/socket). Message is key-redacted."""


class MassivePaginationError(MassiveError):
    """A pagination guard tripped: a foreign `next_url` host, or a page cap
    reached with more pages pending (refused, never silently truncated)."""


# ---- key custody -----------------------------------------------------------


def load_api_key(
    *,
    env: Mapping[str, str] | None = None,
    key_path: Path | None = None,
) -> str:
    """The API key from `POLYGON_API_KEY`, else the key file.

    Whitespace-stripped (the file is written with a trailing newline). The
    key file must not be group/world readable: an over-permissive mode is
    refused rather than used, and the refusal names the MODE, not the key.
    """
    env = os.environ if env is None else env
    from_env = env.get(API_KEY_ENV_VAR, "").strip()
    if from_env:
        return from_env

    path = DEFAULT_KEY_PATH if key_path is None else key_path
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MassiveAuthError(
            f"no API key: {API_KEY_ENV_VAR} unset/empty and {path} unreadable ({exc.strerror})"
        ) from None
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise MassiveAuthError(
            f"{path}: key file mode {mode:04o} is group/world readable — chmod 600 it"
        )
    key = raw.strip()
    if not key:
        raise MassiveAuthError(f"no API key: {API_KEY_ENV_VAR} unset/empty and {path} is blank")
    return key


def redact(text: str, secret: str) -> str:
    """`text` with every occurrence of `secret` replaced by REDACTED.

    Applied to every string that can escape this module (exceptions, cached
    bytes, reprs). A blank secret is a no-op, never a global scrub."""
    if not secret:
        return text
    return text.replace(secret, REDACTED)


def redact_bytes(payload: bytes, secret: str) -> bytes:
    if not secret:
        return payload
    return payload.replace(secret.encode("utf-8"), REDACTED.encode("utf-8"))


# ---- exact JSON ------------------------------------------------------------


def loads_exact(payload: bytes | str) -> Any:
    """`json.loads` with every non-integer number parsed as `Decimal`.

    `parse_float` receives the number's raw source text, so the Decimal is
    built from the characters the vendor sent — exactness is provable, no
    float ever exists, and no `str(float)` coercion is involved."""
    text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    return json.loads(text, parse_float=Decimal)


# ---- rate governor ---------------------------------------------------------


class RateGovernor:
    """Minimum-interval request spacing.

    `requests_per_minute=None` disables spacing entirely (paid tiers). The
    clock and sleeper are injected so tests are instant and deterministic;
    a test sleeper is expected to advance its own clock, exactly as real
    `time.sleep` advances `time.monotonic`.
    """

    def __init__(
        self,
        requests_per_minute: int | None = FREE_TIER_REQUESTS_PER_MINUTE,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if requests_per_minute is not None and requests_per_minute <= 0:
            raise ValueError(
                f"requests_per_minute must be positive or None, got {requests_per_minute}"
            )
        self.requests_per_minute = requests_per_minute
        self._clock = clock
        self._sleeper = sleeper
        self._last: float | None = None
        self.sleeps = 0
        self.slept_seconds = 0.0

    @property
    def min_interval(self) -> float:
        """Seconds between consecutive real requests (0.0 when unlimited)."""
        if self.requests_per_minute is None:
            return 0.0
        return 60.0 / self.requests_per_minute

    def acquire(self) -> float:
        """Block until a token is available; returns the seconds slept."""
        interval = self.min_interval
        now = self._clock()
        if interval <= 0.0 or self._last is None:
            self._last = now
            return 0.0
        wait = interval - (now - self._last)
        if wait <= 0.0:
            self._last = now
            return 0.0
        self.sleep(wait)
        self._last = self._clock()
        return wait

    def sleep(self, seconds: float) -> float:
        """An explicit wait (429 backoff), accounted like a spacing wait."""
        if seconds <= 0.0:
            return 0.0
        self._sleeper(seconds)
        self.sleeps += 1
        self.slept_seconds += seconds
        return seconds


@dataclass(frozen=True)
class BackoffPolicy:
    """Bounded exponential backoff for HTTP 429.

    `max_attempts` counts TOTAL attempts, so `max_attempts=4` is one
    request plus three retries; exhausting them raises
    `MassiveRateLimitError` rather than retrying forever."""

    max_attempts: int = 4
    initial_seconds: float = 2.0
    multiplier: float = 2.0
    cap_seconds: float = 60.0

    def delay_for(self, attempt: int) -> float:
        """Seconds to wait after failed attempt `attempt` (1-based), capped."""
        if attempt < 1:
            raise ValueError(f"attempt must be >= 1, got {attempt}")
        raw = self.initial_seconds * (self.multiplier ** (attempt - 1))
        return min(raw, self.cap_seconds)

    def total_seconds(self) -> float:
        """Worst-case total wait across every retry (the advertised cap)."""
        return sum(self.delay_for(a) for a in range(1, self.max_attempts))


# Immutable, so one shared instance is a safe default argument.
DEFAULT_BACKOFF = BackoffPolicy()


# ---- transport -------------------------------------------------------------


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


class Transport(Protocol):
    """The one seam tests replace; no test in this repo opens a socket."""

    def __call__(self, url: str, *, timeout: float) -> HttpResponse: ...


def urllib_transport(url: str, *, timeout: float) -> HttpResponse:
    """Stdlib HTTPS GET. An HTTP error status is RETURNED, not raised, so
    the client can apply its own 429/entitlement policy."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(
                status=int(response.status),
                body=response.read(),
                headers={k.lower(): v for k, v in response.headers.items()},
            )
    except urllib.error.HTTPError as exc:
        return HttpResponse(
            status=int(exc.code),
            body=exc.read(),
            headers={k.lower(): v for k, v in exc.headers.items()} if exc.headers else {},
        )
    except urllib.error.URLError as exc:
        # `exc.reason` never contains the URL; the client redacts anyway.
        raise MassiveTransportError(f"transport failure: {exc.reason}") from None


# ---- on-disk cache ---------------------------------------------------------


def cache_key_for(path: str, params: Mapping[str, Any]) -> str:
    """sha256 over the endpoint path plus sorted query params, EXCLUDING the
    API key. The query is canonicalised with `urlencode`, so a `=` or `&`
    inside a value can never make two DIFFERENT param sets hash the same
    (`{"a": "b=c"}` vs `{"a=b": "c"}` stay distinct). Two calls that differ
    only by key hit the same cache entry, and no cache file name can carry
    the secret."""
    items = sorted(
        (str(k), str(v)) for k, v in params.items() if str(k).lower() != API_KEY_PARAM.lower()
    )
    canonical = path + "?" + urllib.parse.urlencode(items)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ResponseCache:
    """Content-addressed body cache. Bodies are stored key-redacted."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path_for(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str) -> bytes | None:
        target = self.path_for(key)
        try:
            return target.read_bytes()
        except OSError:
            return None

    def put(self, key: str, body: bytes, *, secret: str = "") -> Path:
        """Store redacted bytes ATOMICALLY: the write lands in a
        dot-prefixed staging file in the SAME directory and is then
        `os.replace`d over the target — a same-filesystem rename the
        kernel performs atomically. A crash mid-write therefore leaves
        either no entry at all (a miss; the wire is re-consulted) or an
        orphan `.tmp` invisible to `get` — never a half-written body under
        the entry's own name."""
        target = self.path_for(key)
        self.directory.mkdir(parents=True, exist_ok=True)
        # pid-qualified: two processes writing the same key cannot truncate
        # each other's staging file between the write and the rename.
        staging = self.directory / f".{key}.{os.getpid()}.json.tmp"
        staging.write_bytes(redact_bytes(body, secret))
        os.replace(staging, target)
        return target

    def discard(self, key: str) -> None:
        """Best-effort unlink of an entry (the self-heal path); an absent
        file is not an error."""
        try:
            self.path_for(key).unlink()
        except OSError:
            pass


# ---- stats -----------------------------------------------------------------


@dataclass
class MassiveStats:
    """Live request accounting (mutable, owned by one client instance).

    `requests` counts REAL network attempts; a cache hit increments only
    `cache_hits`, which is how "a cache hit consumes no token" is audited.
    The three retry counters bump only when a retry actually follows (a
    retryable outcome on the FINAL attempt is exhaustion, not a retry), and
    `cache_self_heals` counts cache entries discarded as undecodable and
    refetched from the wire."""

    requests: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    pages_fetched: int = 0
    rate_limit_retries: int = 0
    server_error_retries: int = 0
    transport_retries: int = 0
    cache_self_heals: int = 0
    governor_sleeps: int = 0
    governor_slept_seconds: float = 0.0

    def snapshot(self) -> dict[str, float | int]:
        return {
            "requests": self.requests,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "pages_fetched": self.pages_fetched,
            "rate_limit_retries": self.rate_limit_retries,
            "server_error_retries": self.server_error_retries,
            "transport_retries": self.transport_retries,
            "cache_self_heals": self.cache_self_heals,
            "governor_sleeps": self.governor_sleeps,
            "governor_slept_seconds": self.governor_slept_seconds,
        }


@dataclass(frozen=True)
class PaginatedResult:
    """Every record across the followed pages, plus how it was obtained."""

    results: tuple[JsonObject, ...]
    pages_fetched: int
    cache_hits: int
    request_ids: tuple[str, ...]


# ---- client ----------------------------------------------------------------


class MassiveClient:
    """Free-tier-safe GET client for `api.polygon.io`."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = MASSIVE_BASE_URL,
        transport: Transport = urllib_transport,
        cache_dir: Path | None = DEFAULT_CACHE_DIR,
        governor: RateGovernor | None = None,
        backoff: BackoffPolicy = DEFAULT_BACKOFF,
        timeout: float = 30.0,
        max_pages: int = 25,
    ) -> None:
        if not api_key:
            raise MassiveAuthError("MassiveClient requires a non-empty API key")
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._host = urllib.parse.urlsplit(self.base_url).netloc
        self._transport = transport
        self.cache = ResponseCache(cache_dir) if cache_dir is not None else None
        self.governor = RateGovernor() if governor is None else governor
        self.backoff = backoff
        self.timeout = timeout
        self.max_pages = max_pages
        self.stats = MassiveStats()

    def __repr__(self) -> str:  # never key-bearing
        cache = "none" if self.cache is None else self.cache.directory.as_posix()
        return f"MassiveClient(base_url={self.base_url!r}, cache={cache!r})"

    # ---- URL plumbing ------------------------------------------------------

    def display_url(self, path: str, params: Mapping[str, Any]) -> str:
        """The human/log/exception form of a request: key-free by
        construction (the key is never placed in this string at all)."""
        items = sorted(
            (str(k), str(v)) for k, v in params.items() if str(k).lower() != API_KEY_PARAM.lower()
        )
        query = urllib.parse.urlencode(items)
        return f"{path}?{query}" if query else path

    def _authorized_url(self, path: str, params: Mapping[str, Any]) -> str:
        """Full request URL with the key appended LAST, at request time."""
        items = [
            (str(k), str(v)) for k, v in params.items() if str(k).lower() != API_KEY_PARAM.lower()
        ]
        items.append((API_KEY_PARAM, self._api_key))
        return f"{self.base_url}{path}?{urllib.parse.urlencode(items)}"

    def split_url(self, url: str) -> tuple[str, dict[str, str]]:
        """(path, params) of an absolute vendor URL (e.g. a `next_url`), with
        any key parameter dropped. A foreign host is refused: appending our
        key to someone else's host would hand them the secret."""
        parts = urllib.parse.urlsplit(url)
        if parts.netloc and parts.netloc != self._host:
            raise MassivePaginationError(
                f"next_url host {parts.netloc!r} != {self._host!r} — refusing to send the key"
            )
        params = {
            k: v
            for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() != API_KEY_PARAM.lower()
        }
        return parts.path, params

    # ---- request -----------------------------------------------------------

    def get_json(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        use_cache: bool = True,
    ) -> JsonObject:
        """One GET, decoded exactly, entitlement-checked.

        Cache first (no token), then governor, then transport through the
        bounded retry loop (429/502/503/504/transport share the cadence).
        A cached body that no longer DECODES self-heals — the entry is
        discarded and the call degrades to a miss so the wire rewrites it;
        a body that decodes but refuses (NOT_AUTHORIZED) keeps raising.
        Returns the parsed body; raises on any non-usable outcome — this
        client never answers a partial or fabricated body."""
        params = {} if params is None else params
        endpoint = self.display_url(path, params)
        key = cache_key_for(path, params)

        if use_cache and self.cache is not None:
            cached = self.cache.get(key)
            if cached is not None:
                self.stats.cache_hits += 1
                try:
                    body = self._decode(cached, endpoint)
                except MassiveApiError:
                    # SELF-HEAL, and only for an undecodable body: unlinked
                    # (best-effort), re-accounted as a miss, refetched. A
                    # cached refusal decodes fine and keeps raising above.
                    self.cache.discard(key)
                    self.stats.cache_hits -= 1
                    self.stats.cache_misses += 1
                    self.stats.cache_self_heals += 1
                else:
                    self._require_ok_status(body, endpoint)
                    return body
            else:
                self.stats.cache_misses += 1

        raw = self._request_with_backoff(path, params, endpoint=endpoint)
        body = self._decode(raw, endpoint)
        self._require_ok_status(body, endpoint)
        if use_cache and self.cache is not None:
            self.cache.put(key, raw, secret=self._api_key)
        return body

    def _request_with_backoff(
        self, path: str, params: Mapping[str, Any], *, endpoint: str
    ) -> bytes:
        """One GET through the bounded attempt loop, classifying each
        outcome on its own merits.

        Retryable — HTTP 429, HTTP 502/503/504, and transport-level
        failures (DNS/TLS/timeout/socket, wrapped key-redacted) — share
        the one backoff cadence and each counts its own retry stat, only
        when a retry actually follows. Terminal on first sight: HTTP
        401/403 raises `MassiveAuthRejectedError` (the raise precedes any
        cache write, so a rejection is never cached either), any other
        non-200 raises `MassiveApiError`, and an `AssertionError` from a
        test double re-raises immediately so the fakes stay loud."""
        url = self._authorized_url(path, params)
        attempts = self.backoff.max_attempts
        last_status = 429  # the rate-limit raise below needs a status to name
        transport_failure: MassiveTransportError | None = None
        for attempt in range(1, attempts + 1):
            slept = self.governor.acquire()
            if slept:
                self.stats.governor_sleeps += 1
                self.stats.governor_slept_seconds += slept
            self.stats.requests += 1
            retry_headers: Mapping[str, str] = {}
            failed_transport = False
            try:
                response = self._transport(url, timeout=self.timeout)
            except AssertionError:
                raise  # a test double's honesty mechanism: never retried
            except MassiveTransportError as exc:
                # the transport layer never sees the key, but redact at
                # this choke point anyway, then retry like any transport blip
                transport_failure = MassiveTransportError(
                    f"{endpoint}: {redact(str(exc), self._api_key)}"
                )
                failed_transport = True
            except MassiveError:
                raise
            except Exception as exc:  # redact before wrapping: exc may echo the URL
                transport_failure = MassiveTransportError(
                    f"{endpoint}: {redact(str(exc), self._api_key)}"
                )
                failed_transport = True
            else:
                status = response.status
                if status == 200:
                    return response.body
                if status in AUTH_REJECTED_HTTP_STATUSES:
                    raise MassiveAuthRejectedError(
                        endpoint,
                        status,
                        redact(self._snippet(response.body), self._api_key),
                    )
                if status not in RETRYABLE_HTTP_STATUSES:
                    raise MassiveApiError(
                        endpoint,
                        redact(self._snippet(response.body), self._api_key),
                        http_status=status,
                    )
                last_status = status
                retry_headers = response.headers
            if attempt >= attempts:
                break
            if failed_transport:
                self.stats.transport_retries += 1
            elif last_status == 429:
                self.stats.rate_limit_retries += 1
            else:
                self.stats.server_error_retries += 1
            self.governor.sleep(self._retry_delay(retry_headers, attempt))
        if transport_failure is not None:
            raise MassiveTransportError(f"{transport_failure} after {attempts} attempts")
        if last_status == 429:
            raise MassiveRateLimitError(
                f"{endpoint}: HTTP {last_status} after {attempts} attempts"
                f" (backoff cap {self.backoff.total_seconds():g}s;"
                f" free tier is {FREE_TIER_REQUESTS_PER_MINUTE} requests/minute)"
            )
        raise MassiveApiError(endpoint, f"after {attempts} attempts", http_status=last_status)

    def _retry_delay(self, headers: Mapping[str, str], attempt: int) -> float:
        """The vendor's `Retry-After` when present and sane, else the policy
        delay; never longer than the policy cap. "Sane" excludes NaN/inf —
        `float("nan")` compares False against everything, so it would slip
        the `<= 0.0` guard and poison `sleep` — and anything unparseable."""
        policy = self.backoff.delay_for(attempt)
        raw = headers.get("retry-after") if headers else None
        if raw is None:
            return policy
        try:
            advised = float(raw)
        except ValueError:
            return policy
        if not math.isfinite(advised) or advised <= 0.0:
            return policy
        return min(advised, self.backoff.cap_seconds)

    @staticmethod
    def _snippet(body: bytes, limit: int = 200) -> str:
        text = body.decode("utf-8", errors="replace").strip()
        return text if len(text) <= limit else text[:limit] + "..."

    def _decode(self, raw: bytes, endpoint: str) -> JsonObject:
        try:
            body = loads_exact(raw)
        except ValueError as exc:
            raise MassiveApiError(
                endpoint, f"body is not JSON: {redact(str(exc), self._api_key)}"
            ) from None
        if not isinstance(body, Mapping):
            raise MassiveApiError(endpoint, f"body is {type(body).__name__}, expected an object")
        return body

    @staticmethod
    def _vendor_message(body: JsonObject) -> str:
        """The vendor's human text. `/v3` refusals use `message`, `/v2`
        errors use `error`; both are checked so a refusal is never reported
        as a bare status code."""
        for field_name in ("message", "error"):
            value = body.get(field_name)
            if isinstance(value, str) and value.strip():
                return value
        return ""

    @classmethod
    def _require_ok_status(cls, body: JsonObject, endpoint: str) -> None:
        """The entitlement gate. NOT_AUTHORIZED arrives as HTTP 200 with a
        body status — checking only the HTTP status would read a refusal as
        an empty success."""
        status = body.get("status")
        if status is None:
            raise MassiveApiError(endpoint, "body carries no `status` field")
        message = cls._vendor_message(body)
        if status == NOT_AUTHORIZED_STATUS:
            raise MassiveNotEntitledError(endpoint, message or "(no vendor message)")
        if status not in OK_STATUSES:
            detail = f"vendor status {status!r}"
            raise MassiveApiError(endpoint, f"{detail}: {message}" if message else detail)

    # ---- pagination --------------------------------------------------------

    def paginate(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        results_key: str = "results",
        max_pages: int | None = None,
        use_cache: bool = True,
    ) -> PaginatedResult:
        """Follow `next_url` until it is absent, accumulating `results`.

        The cap is a guard, not a truncation: a `next_url` still pending at
        the cap raises `MassivePaginationError`, because a short master
        would silently misdescribe the contract universe. A REPEATED
        `next_url` is a vendor cursor bug and raises as a cycle rather
        than looping until the cap."""
        cap = self.max_pages if max_pages is None else max_pages
        if cap < 1:
            raise ValueError(f"max_pages must be >= 1, got {cap}")
        collected: list[JsonObject] = []
        request_ids: list[str] = []
        next_path: str | None = path
        next_params: Mapping[str, Any] = {} if params is None else dict(params)
        pages = 0
        hits_before = self.stats.cache_hits
        visited: set[str] = set()
        while next_path is not None:
            body = self.get_json(next_path, next_params, use_cache=use_cache)
            pages += 1
            self.stats.pages_fetched += 1
            page_results = body.get(results_key, ())
            if not isinstance(page_results, list | tuple):
                raise MassiveApiError(
                    self.display_url(next_path, next_params),
                    f"{results_key!r} is {type(page_results).__name__}, expected a list",
                )
            collected.extend(page_results)
            request_id = body.get("request_id")
            if isinstance(request_id, str):
                request_ids.append(request_id)
            next_url = body.get("next_url")
            if isinstance(next_url, str) and next_url:
                next_path, next_params = self.split_url(next_url)
                if pages >= cap:
                    raise MassivePaginationError(
                        f"{self.display_url(path, next_params)}: more pages remain after"
                        f" max_pages={cap} — refusing a truncated result"
                    )
                if next_url in visited:
                    raise MassivePaginationError(
                        f"{self.display_url(next_path, next_params)}: next_url cycle"
                        f" after {pages} pages — {next_url} was already followed"
                        " once; refusing to loop"
                    )
                visited.add(next_url)
            else:
                next_path = None
        return PaginatedResult(
            results=tuple(collected),
            pages_fetched=pages,
            cache_hits=self.stats.cache_hits - hits_before,
            request_ids=tuple(request_ids),
        )


def client_from_environment(
    *,
    env: Mapping[str, str] | None = None,
    key_path: Path | None = None,
    **kwargs: Any,
) -> MassiveClient:
    """A free-tier client keyed from the environment/key file. The key is
    passed straight into the client and is never returned to the caller."""
    return MassiveClient(api_key=load_api_key(env=env, key_path=key_path), **kwargs)


__all__ = [
    "API_KEY_ENV_VAR",
    "API_KEY_PARAM",
    "AUTH_REJECTED_HTTP_STATUSES",
    "DEFAULT_BACKOFF",
    "DEFAULT_CACHE_DIR",
    "DEFAULT_KEY_PATH",
    "FREE_TIER_REQUESTS_PER_MINUTE",
    "MASSIVE_BASE_URL",
    "MASSIVE_CACHE_DIR_ENV_VAR",
    "MASSIVE_PROVIDER",
    "NOT_AUTHORIZED_STATUS",
    "OK_STATUSES",
    "RETRYABLE_HTTP_STATUSES",
    "BackoffPolicy",
    "HttpResponse",
    "MassiveApiError",
    "MassiveAuthError",
    "MassiveAuthRejectedError",
    "MassiveClient",
    "MassiveError",
    "MassiveNotEntitledError",
    "MassivePaginationError",
    "MassiveRateLimitError",
    "MassiveStats",
    "MassiveTransportError",
    "PaginatedResult",
    "RateGovernor",
    "ResponseCache",
    "Transport",
    "cache_key_for",
    "client_from_environment",
    "default_cache_dir",
    "load_api_key",
    "loads_exact",
    "redact",
    "redact_bytes",
    "urllib_transport",
]

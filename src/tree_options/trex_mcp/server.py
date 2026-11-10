"""Tenant-isolated, OAuth-authenticated, read-only MCP Streamable HTTP facade.

Only a fixed allowlist of existing TREX GET routes is reachable from tools.
Never import the supervised desk, broker adapters, command queue, or state
writers. An LLM has no permit or order authority through this process.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from datetime import date, datetime, timedelta
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .analysis import InvalidScenario, evaluate_bearish_scenario, symbol_value

MCP_VERSION = "2026-07-28"
SUPPORTED_VERSIONS = {MCP_VERSION, "2025-11-25", "2025-06-18"}
REQUIRED_SCOPE = "trex:research:read"
MAX_REQUEST_BYTES = 65_536
MAX_BODY_READ_SECONDS = 5.0
MAX_TOKEN_BYTES = 4_096
MAX_OAUTH_REPLY_BYTES = 16_384
MAX_TREX_REPLY_BYTES = 1_048_576
MAX_UPSTREAM_TOTAL_SECONDS = 8.0  # Wall-clock bound, including slow streamed chunks.
MAX_OPTION_ROWS = 80
MAX_WARNINGS = 8
MAX_INFLIGHT_CALLS = 16
ADMISSION_TIMEOUT_SECONDS = 0.1


class AccessDenied(Exception):
    """The token is inactive, underscoped or has no bound TREX instance."""


class UpstreamUnavailable(Exception):
    """The TREX read-only upstream is unavailable or returned invalid data."""


class RequestTooLarge(Exception):
    """Request stream exceeded the maximum byte budget."""


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    subject: str = ""  # nonempty for production OAuth; defaults for legacy tests.


@dataclass(frozen=True)
class Config:
    public_url: str
    authorization_server: str
    introspection_url: str
    client_id: str
    client_secret: str
    tenants: dict[str, str]
    allowed_origins: frozenset[str]

    @classmethod
    def from_env(cls) -> Config:
        required = (
            "TREX_MCP_PUBLIC_URL",
            "TREX_MCP_AUTHORIZATION_SERVER",
            "TREX_MCP_INTROSPECTION_URL",
            "TREX_MCP_CLIENT_ID",
            "TREX_MCP_CLIENT_SECRET",
            "TREX_MCP_TENANTS_JSON",
        )
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            raise ValueError("TREX MCP missing mandatory configuration: " + ", ".join(missing))
        try:
            tenants = json.loads(os.environ["TREX_MCP_TENANTS_JSON"])
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("TREX_MCP_TENANTS_JSON must be a JSON mapping") from exc
        if not isinstance(tenants, dict) or not tenants:
            raise ValueError("TREX_MCP_TENANTS_JSON must name isolated instances")
        seen_ports: set[int] = set()
        for tenant, url in tenants.items():
            if not isinstance(tenant, str) or not tenant or not isinstance(url, str):
                raise ValueError("each TREX tenant needs a literal local URL")
            parsed = urlsplit(url)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in ("localhost", "127.0.0.1", "::1")
                or parsed.path not in ("", "/")
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
                or parsed.port is None
                or parsed.port == 0
            ):
                raise ValueError("TREX upstreams must be fixed loopback HTTP instances")
            # Avoid aliases like localhost/127.0.0.1 silently mapping two
            # customer identities onto the same listener on one host.
            if parsed.port in seen_ports:
                raise ValueError("TREX tenant isolation requires unique loopback ports")
            seen_ports.add(parsed.port)
        public_url = os.environ["TREX_MCP_PUBLIC_URL"].rstrip("/")
        auth_server = os.environ["TREX_MCP_AUTHORIZATION_SERVER"].rstrip("/")
        introspection = os.environ["TREX_MCP_INTROSPECTION_URL"]
        for label, url in (("public MCP", public_url), ("authorization server", auth_server),
                           ("token introspection", introspection)):
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
                raise ValueError(f"{label} URL must be absolute HTTPS without credentials, query or fragment")
        if not public_url.endswith("/mcp"):
            raise ValueError("TREX_MCP_PUBLIC_URL must be the public HTTPS .../mcp endpoint")
        origins = frozenset(
            o.strip() for o in os.environ.get("TREX_MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()
        )
        for origin in origins:
            parsed = urlsplit(origin)
            if (parsed.scheme not in ("https", "http") or not parsed.hostname
                or parsed.username or parsed.password or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment or parsed.netloc == "*"):
                raise ValueError("TREX_MCP_ALLOWED_ORIGINS must contain exact URL origins")
            if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
                raise ValueError("plaintext origins are permitted only for localhost testing")
        return cls(
            public_url=public_url,
            authorization_server=auth_server,
            introspection_url=introspection,
            client_id=os.environ["TREX_MCP_CLIENT_ID"],
            client_secret=os.environ["TREX_MCP_CLIENT_SECRET"],
            tenants=tenants,
            allowed_origins=origins,
        )


AuthResolver = Callable[[str], Awaitable[Principal]]
ReadFetcher = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _http_client() -> httpx.AsyncClient:
    """Per-app connection pool; never honors proxy environment or redirects."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=2.0, read=4.0, write=2.0, pool=1.0),
        limits=httpx.Limits(max_connections=24, max_keepalive_connections=12),
        trust_env=False,
        follow_redirects=False,
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-JSON numeric constant: {value}")


async def _limited_json(
    client: httpx.AsyncClient, method: str, url: str, max_bytes: int, **kwargs: Any
) -> Any:
    """Budget decoded bytes before parsing; compressed/streamed replies included."""
    # Per-socket read timeouts do not constrain an upstream that continuously
    # drips bytes. Limit the entire operation in addition to decoded bytes.
    async with asyncio.timeout(MAX_UPSTREAM_TOTAL_SECONDS):
        async with client.stream(method, url, **kwargs) as response:
            if response.status_code != 200:
                raise UpstreamUnavailable(f"upstream returned HTTP {response.status_code}")
            advertised = response.headers.get("content-length")
            if advertised is not None:
                try:
                    if int(advertised) > max_bytes:
                        raise UpstreamUnavailable("upstream payload exceeds byte budget")
                except ValueError as exc:
                    raise UpstreamUnavailable("invalid upstream content-length") from exc
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                if len(chunk) > max_bytes - len(chunks):
                    raise UpstreamUnavailable("upstream payload exceeds byte budget")
                chunks.extend(chunk)
            return json.loads(chunks, parse_constant=_reject_json_constant)


async def _introspect(
    token: str, config: Config, client: httpx.AsyncClient | None = None
) -> Principal:
    # Backwards-compatible standalone access; production always provides the
    # lifespan-owned client so TLS connections are reused across calls.
    if client is None:
        async with _http_client() as owned:
            return await _introspect(token, config, owned)
    try:
        claims = await _limited_json(
            client, "POST", config.introspection_url, MAX_OAUTH_REPLY_BYTES,
            data={"token": token, "token_type_hint": "access_token"},
            auth=(config.client_id, config.client_secret),
        )
    except (httpx.HTTPError, ValueError, RecursionError, TimeoutError) as exc:
        raise UpstreamUnavailable("OAuth introspection failed closed") from exc
    if not isinstance(claims, dict) or claims.get("active") is not True:
        raise AccessDenied("inactive access token")
    # `active` is authoritative, but fail closed on an inconsistent expiry or
    # not-before claim instead of granting access from an incorrect response.
    now = time.time()
    for name in ("exp", "nbf", "iat"):
        value = claims.get(name)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or abs(value) > 1e15
            or (isinstance(value, float) and not math.isfinite(value))
            or (name == "exp" and value <= now)
            or (name in ("nbf", "iat") and value > now)
        ):
            raise AccessDenied(f"invalid {name} claim")
    audience = claims.get("aud")
    audiences = (
        {audience}
        if isinstance(audience, str)
        else {v for v in audience if isinstance(v, str)} if isinstance(audience, list) else set()
    )
    scopes = claims.get("scope")
    scope_set = (
        set(scopes.split())
        if isinstance(scopes, str)
        else {v for v in scopes if isinstance(v, str)} if isinstance(scopes, list) else set()
    )
    tenant = claims.get("trex_tenant_id")
    if (
        config.public_url not in audiences
        or REQUIRED_SCOPE not in scope_set
        or not isinstance(claims.get("sub"), str)
        or not claims["sub"]
        or not isinstance(tenant, str)
        or tenant not in config.tenants
    ):
        raise AccessDenied("token lacks permitted audience, scope or tenant")
    return Principal(tenant_id=tenant, subject=claims["sub"])


async def _read_trex(
    config: Config, tenant: str, path: str, params: dict[str, Any],
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Only allowlisted local GETs; responses and transport cost are capped."""
    if tenant not in config.tenants:
        raise AccessDenied("unknown tenant")
    match = re.fullmatch(r"/api/market/([A-Z]+(?:\.[A-Z]+)?)(?:/options)?", path)
    if match is None or len(match.group(1)) > 6:
        # Dot-only/adjacent-dot segments normalize to parent routes in HTTP.
        raise UpstreamUnavailable("endpoint not in MCP allowlist")
    if client is None:
        async with _http_client() as owned:
            return await _read_trex(config, tenant, path, params, owned)
    url = config.tenants[tenant].rstrip("/") + path
    try:
        data = await _limited_json(client, "GET", url, MAX_TREX_REPLY_BYTES, params=params)
    except (httpx.HTTPError, ValueError, RecursionError, TimeoutError) as exc:
        raise UpstreamUnavailable("TREX market read failed") from exc
    if not isinstance(data, dict):
        raise UpstreamUnavailable("TREX market response must be a JSON object")
    return data


async def _read_bounded_body(request: Request, *, limit: int = MAX_REQUEST_BYTES) -> bytes:
    """Stop consuming an unframed POST as soon as it exceeds its byte cap."""
    body = bytearray()
    async for chunk in request.stream():
        if len(chunk) > limit - len(body):
            raise RequestTooLarge("MCP request exceeds byte budget")
        body.extend(chunk)
    return bytes(body)


def _safe_number(value: Any, *, nonnegative: bool = False) -> int | float | None:
    """Preserve finite prices/counts, but never propagate NaN/Infinity/negative quotes."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # Avoid math.isfinite(int) converting huge, legal JSON integers to float.
    if abs(value) > 1e12 or (nonnegative and value < 0):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _safe_text(value: Any, *, limit: int = 128) -> str | None:
    return value if isinstance(value, str) and len(value) <= limit else None


def _verify_symbol(doc: dict[str, Any], symbol: str) -> None:
    found = doc.get("symbol")
    if found is not None and found != symbol:
        raise UpstreamUnavailable("market source returned a mismatched symbol")


def _verify_observed_time(source: object, now: object) -> None:
    """Reject a claimed observation published in the future of the TREX read.

    No timestamp is fabricated when one is missing. Naive/invalid observed
    timestamps cannot be certified and therefore fail closed when supplied.
    """
    if source is None:
        return
    if not isinstance(source, str):
        raise UpstreamUnavailable("invalid market observation timestamp")
    try:
        observed = datetime.fromisoformat(source)
        current = datetime.fromisoformat(now) if isinstance(now, str) else None
    except ValueError as exc:
        raise UpstreamUnavailable("malformed market observation timestamp") from exc
    if observed.tzinfo is None or (current is not None and current.tzinfo is None):
        raise UpstreamUnavailable("market observations must be timezone-aware")
    if current is not None and observed > current + timedelta(seconds=60):
        raise UpstreamUnavailable("future-dated market observation")


def _verify_recorded_session(session: object, now: object) -> None:
    if session is None or now is None:
        return
    try:
        if not isinstance(session, str) or not isinstance(now, str):
            raise ValueError("non-string session")
        observed_session = date.fromisoformat(session)
        today = datetime.fromisoformat(now).date()
    except ValueError as exc:
        raise UpstreamUnavailable("invalid recorded options session") from exc
    if observed_session > today:
        raise UpstreamUnavailable("recorded options session is from the future")


TOOLS = [
    {
        "name": "trex_market_snapshot",
        "description": (
            "Read a tenant's TREX recorded/delayed quote with original age and source timestamp. "
            "Never asserts a live executable price; no portfolio or order data."
        ),
        "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string", "maxLength": 6, "pattern": r"^[A-Za-z]+(?:\.[A-Za-z]+)?$", "description": "Ticker, e.g. TSLA or BRK.B"}}, "required": ["symbol"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "trex_options_surface",
        "description": (
            "Read TREX's bounded recorded-chain and delayed viewchain slices, timestamps, bid/ask, "
            "volume and open interest when available. A requested distant expiry may be absent "
            "from TREX's nearest-expiry-limited cache; absence is not zero liquidity."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "maxLength": 6, "pattern": r"^[A-Za-z]+(?:\.[A-Za-z]+)?$"},
                "expiration": {"type": "string", "description": "Optional YYYY-MM-DD expiry filter"},
                "right": {"type": "string", "enum": ["put", "call", "both"]},
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "trex_bearish_payoff",
        "description": (
            "Deterministic long-put or put-debit-spread expiration P&L from caller-supplied "
            "premium, strikes, spot and targets. Scenario only: NOT a quote, signal or order."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "maxLength": 6, "pattern": r"^[A-Za-z]+(?:\.[A-Za-z]+)?$"},
                "expiration": {"type": "string", "description": "YYYY-MM-DD"},
                "strategy": {"type": "string", "enum": ["long_put", "put_debit_spread"]},
                "spot": {"type": "number", "exclusiveMinimum": 0},
                "long_strike": {"type": "number", "exclusiveMinimum": 0},
                "short_strike": {"type": "number", "exclusiveMinimum": 0},
                "net_debit": {"type": "number", "exclusiveMinimum": 0, "description": "Per-share premium paid, NOT total dollars"},
                "contracts": {"type": "integer", "minimum": 1, "maximum": 100},
                "target_prices": {"type": "array", "items": {"type": "number", "minimum": 0}, "minItems": 1, "maxItems": 12},
                "max_risk_budget": {"type": "number", "exclusiveMinimum": 0},
            },
            "required": ["symbol", "expiration", "strategy", "spot", "long_strike", "net_debit", "target_prices"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
]


def _require_exact_fields(args: dict[str, Any], fields: set[str]) -> None:
    unexpected = set(args) - fields
    if unexpected:
        raise InvalidScenario("unexpected fields: " + ", ".join(sorted(unexpected)))


def _expiry_param(value: object) -> str | None:
    if value is None:
        return None
    from datetime import date

    if not isinstance(value, str):
        raise InvalidScenario("expiration must be YYYY-MM-DD")
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("non-canonical date")
    except ValueError as exc:
        raise InvalidScenario("expiration must be YYYY-MM-DD") from exc
    return value


def _safe_market_view(doc: dict[str, Any], symbol: str) -> dict[str, Any]:
    _verify_symbol(doc, symbol)
    # Deliberately exclude TREX's news, bars, account and portfolio metadata.
    raw_quote = doc.get("quote")
    quote: dict[str, Any] | None = None
    if isinstance(raw_quote, dict):
        nested_symbol = raw_quote.get("symbol")
        if nested_symbol is not None and nested_symbol != symbol:
            raise UpstreamUnavailable("quote symbol contradicts requested symbol")
        _verify_observed_time(raw_quote.get("source_as_of"), doc.get("now"))
        quote = {}
        for key in ("last", "price", "close", "bid", "ask", "volume", "previous_close"):
            quote[key] = _safe_number(raw_quote.get(key), nonnegative=True)
        for key in ("change", "change_pct"):
            quote[key] = _safe_number(raw_quote.get(key))
        for key in ("source_as_of", "source"):
            quote[key] = _safe_text(raw_quote.get(key))
        quote["symbol"] = symbol
    age = _safe_number(doc.get("quote_age_seconds"), nonnegative=True)
    return {
        "schema": "trex-mcp-market/1",
        "symbol": symbol,
        "trex_now": _safe_text(doc.get("now")),
        "quote": quote,
        "quote_age_seconds": age,
        "freshness_known": age is not None,
        "source": "TREX discovery cache; quote_age_seconds is not a live execution guarantee",
        "executable": False,
    }


def _safe_options_view(doc: dict[str, Any], symbol: str, exp: str | None, right: str) -> dict[str, Any]:
    _verify_symbol(doc, symbol)
    raw_warnings = doc.get("warnings")
    warnings = (
        [w for w in raw_warnings if isinstance(w, str)][:MAX_WARNINGS]
        if isinstance(raw_warnings, list) else []
    )
    warnings = [w[:200] for w in warnings]
    result: dict[str, Any] = {
        "schema": "trex-mcp-options/1",
        "symbol": symbol,
        "trex_now": _safe_text(doc.get("now")),
        "available": doc.get("available") is True,
        "warnings": warnings,
        "requested_expiration": exp,
        "right": right,
        "cache_is_not_executable_quote": True,
        "nearest_expiry_slice_is_not_a_complete_option_chain": True,
    }
    numeric_fields = {
        "strike", "bid", "ask", "mid", "iv", "delta", "gamma", "theta", "vega",
    }
    counts = {"oi", "volume", "dte"}
    cards_num = {"iv", "iv_rank", "skew25", "term_slope", "yz22_ann", "liquidity_score"}
    for kind in ("recorded", "live"):
        raw = doc.get(kind)
        if not isinstance(raw, dict):
            result[kind] = None
            continue
        if kind == "recorded":
            _verify_recorded_session(raw.get("session"), doc.get("now"))
        else:
            _verify_observed_time(raw.get("fetched_at"), doc.get("now"))
        rows = raw.get("slice")
        if not isinstance(rows, list):
            rows = []
        expiries: set[str] = set()
        filtered_count = 0
        out: list[dict[str, Any]] = []
        # Single O(n) pass over the upstream-bounded JSON document: no
        # intermediate filtered list or unsanitized provider data in output.
        for row in rows:
            if not isinstance(row, dict):
                continue
            expiry = _safe_text(row.get("exp"), limit=10)
            if expiry:
                expiries.add(expiry)
            if not expiry or (exp is not None and expiry != exp):
                continue
            direction = _safe_text(row.get("right"), limit=4)
            if not direction or (right != "both" and direction.upper() not in (
                ("P", "PUT") if right == "put" else ("C", "CALL")
            )):
                continue
            filtered_count += 1
            if len(out) >= MAX_OPTION_ROWS:
                continue
            safe_row: dict[str, Any] = {"exp": expiry, "right": direction}
            for key in numeric_fields:
                safe_row[key] = _safe_number(row.get(key), nonnegative=key not in ("delta", "gamma", "theta", "vega"))
            for key in counts:
                val = _safe_number(row.get(key), nonnegative=True)
                safe_row[key] = val if isinstance(val, int) else None
            safe_row["atm"] = row.get("atm") if isinstance(row.get("atm"), bool) else None
            bid, ask = safe_row["bid"], safe_row["ask"]
            if bid is None or ask is None:
                safe_row["mid"] = None
                safe_row["quote_quality"] = "incomplete_quote"
            elif bid > ask:
                safe_row["mid"] = None
                safe_row["quote_quality"] = "crossed_quote"
            elif safe_row["mid"] is not None and not bid <= safe_row["mid"] <= ask:
                safe_row["mid"] = None
                safe_row["quote_quality"] = "inconsistent_mid"
            else:
                safe_row["quote_quality"] = "two_sided_recorded_not_executable"
            out.append(safe_row)
        raw_cards = raw.get("cards")
        cards = None
        if kind == "recorded" and isinstance(raw_cards, dict):
            cards = {k: _safe_number(raw_cards.get(k)) for k in cards_num}
            earnings = raw_cards.get("earnings")
            cards["earnings"] = _safe_text(earnings)
        age = _safe_number(raw.get("age_seconds"), nonnegative=True)
        ttl = _safe_number(raw.get("ttl_seconds"), nonnegative=True)
        result[kind] = {
            "session": _safe_text(raw.get("session")),
            "fetched_at": _safe_text(raw.get("fetched_at")),
            "age_seconds": age,
            "ttl_seconds": ttl,
            "cache_expired": age > ttl if age is not None and ttl is not None else None,
            "spot": _safe_number(raw.get("spot"), nonnegative=True),
            "cards": cards,
            "expiries_in_retrieved_slice": sorted(expiries),
            "expiry_found": exp is None or exp in expiries,
            "matching_rows": filtered_count,
            "returned_rows": out,
            "truncated": filtered_count > MAX_OPTION_ROWS,
        }
    return result


def _tool_success(data: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(data, sort_keys=True, allow_nan=False)}], "structuredContent": data, "isError": False}


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _rpc_result(request_id: object, result: dict[str, Any], *, modern: bool = False) -> JSONResponse:
    if modern:
        result = {
            **result,
            "resultType": "complete",
            "_meta": {
                "io.modelcontextprotocol/serverInfo": {
                    "name": "trex-research-mcp", "version": "0.2.0",
                },
            },
        }
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


def _rpc_error(request_id: object, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def create_app(
    *,
    config: Config | None = None,
    authenticate: AuthResolver | None = None,
    fetcher: ReadFetcher | None = None,
    max_inflight: int = MAX_INFLIGHT_CALLS,
    quota_per_subject: int = 60,
    quota_per_tenant: int = 300,
    quota_max_identities: int = 2_048,
) -> FastAPI:
    """Build the gateway; injectable auth/fetcher are for isolated contract tests.

    Production always loads OAuth configuration and tenant->loopback bindings.
    Authentication is mandatory for *all* MCP traffic, including handshake.
    """
    if config is None:
        config = Config.from_env() if authenticate is None or fetcher is None else Config(
            public_url="https://mcp.test.example/mcp",
            authorization_server="https://auth.test.example",
            introspection_url="https://auth.test.example/introspect",
            client_id="test-only", client_secret="test-only",
            tenants={"test": "http://127.0.0.1:8090"}, allowed_origins=frozenset(),
        )
    current_config = config
    if max_inflight < 1 or max_inflight > 256:
        raise ValueError("max_inflight must be 1-256")
    inflight = asyncio.Semaphore(max_inflight)
    from .quota import QuotaLimiter

    quotas = QuotaLimiter(subject_limit=quota_per_subject, tenant_limit=quota_per_tenant,
                          max_subject_keys=quota_max_identities)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with _http_client() as pooled:
            app.state.research_http_client = pooled
            yield

    app = FastAPI(
        title="TREX read-only MCP gateway", docs_url=None, redoc_url=None,
        openapi_url=None, lifespan=lifespan,
    )

    def pooled_client() -> httpx.AsyncClient:
        client = getattr(app.state, "research_http_client", None)
        if client is None or client.is_closed:
            raise UpstreamUnavailable("TREX MCP network pool not started")
        return client

    async def _authenticate(token: str) -> Principal:
        if authenticate is not None:
            return await authenticate(token)
        return await _introspect(token, current_config, pooled_client())

    async def _fetch(tenant: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if fetcher is not None:
            return await fetcher(tenant, path, params)
        return await _read_trex(current_config, tenant, path, params, pooled_client())

    @app.middleware("http")
    async def admission(request: Request, call_next: Callable[..., Any]) -> Response:
        """Prevent unbounded concurrent model calls from queuing auth/upstream work.

        This is one-worker defense in depth. Per-subject rate limits across
        replicas still belong at the authenticated HTTPS ingress.
        """
        if request.url.path != "/mcp":
            return await call_next(request)
        try:
            await asyncio.wait_for(inflight.acquire(), timeout=ADMISSION_TIMEOUT_SECONDS)
        except TimeoutError:
            return Response(status_code=429, headers={"Retry-After": "1", "Cache-Control": "no-store"})
        try:
            return await call_next(request)
        finally:
            inflight.release()

    @app.middleware("http")
    async def origins_and_cache(request: Request, call_next: Callable[..., Any]) -> Response:
        origin = request.headers.get("origin")
        if origin is not None and origin not in current_config.allowed_origins:
            return Response(status_code=403, headers={"Cache-Control": "no-store"})
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/healthz")
    async def health() -> dict[str, object]:
        return {"service": "trex-mcp", "read_only": True, "ok": True}

    @app.get("/.well-known/oauth-protected-resource")
    @app.get("/.well-known/oauth-protected-resource/mcp")
    async def oauth_resource() -> dict[str, object]:
        return {
            "resource": current_config.public_url,
            "authorization_servers": [current_config.authorization_server],
            "scopes_supported": [REQUIRED_SCOPE],
            "bearer_methods_supported": ["header"],
        }

    async def invoke_tool(name: str, args: dict[str, Any], principal: Principal) -> dict[str, Any]:
        if name == "trex_bearish_payoff":
            return evaluate_bearish_scenario(args)
        if name == "trex_market_snapshot":
            _require_exact_fields(args, {"symbol"})
            sym = symbol_value(args.get("symbol"))
            doc = await _fetch(principal.tenant_id, f"/api/market/{sym}", {})
            return _safe_market_view(doc, sym)
        if name == "trex_options_surface":
            _require_exact_fields(args, {"symbol", "expiration", "right"})
            sym = symbol_value(args.get("symbol"))
            expiry = _expiry_param(args.get("expiration"))
            right = args.get("right", "put")
            if right not in ("put", "call", "both"):
                raise InvalidScenario("right must be put, call or both")
            doc = await _fetch(
                principal.tenant_id,
                f"/api/market/{sym}/options",
                {"window": 15, "max_expiries": 12},
            )
            return _safe_options_view(doc, sym, expiry, right)
        raise InvalidScenario("unknown MCP tool")

    @app.post("/mcp")
    async def mcp(request: Request) -> Response:
        auth = request.headers.get("authorization", "")
        if (not auth.startswith("Bearer ") or not auth[7:].strip()
            or len(auth[7:]) > MAX_TOKEN_BYTES):
            return Response(status_code=401, headers={"WWW-Authenticate": (
                f'Bearer resource_metadata="{current_config.public_url.rsplit("/mcp", 1)[0]}/.well-known/oauth-protected-resource"'
            )})
        try:
            principal = await _authenticate(auth[7:].strip())
        except AccessDenied:
            return Response(status_code=401, headers={"WWW-Authenticate": "Bearer error=\"invalid_token\""})
        except UpstreamUnavailable:
            return Response(status_code=503)
        if principal.tenant_id not in current_config.tenants:
            return Response(status_code=403)
        if not await quotas.admit(principal):
            return Response(status_code=429, headers={"Retry-After": "60", "Cache-Control": "no-store"})
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return Response(status_code=415)
        length_header = request.headers.get("content-length")
        if length_header:
            try:
                if int(length_header) < 0:
                    return Response(status_code=400)
                if int(length_header) > MAX_REQUEST_BYTES:
                    return Response(status_code=413)
            except ValueError:
                return Response(status_code=400)
        try:
            body = await asyncio.wait_for(
                _read_bounded_body(request), timeout=MAX_BODY_READ_SECONDS
            )
        except RequestTooLarge:
            return Response(status_code=413)
        except TimeoutError:
            return Response(status_code=408)
        try:
            payload = json.loads(body, parse_constant=_reject_json_constant)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return _rpc_error(None, -32700, "Parse error")
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            return _rpc_error(None, -32600, "Invalid JSON-RPC request")
        method = payload.get("method")
        request_id = payload.get("id")
        if "id" in payload and (isinstance(request_id, bool) or not isinstance(request_id, (str, int, float)) or (
            isinstance(request_id, float) and not math.isfinite(request_id)
        )):
            return _rpc_error(None, -32600, "Invalid JSON-RPC id")
        if not isinstance(method, str):
            return _rpc_error(request_id, -32600, "Invalid method")
        # New (2026-07-28) MCP transport mirrors body metadata into HTTP
        # headers. Validate both sources before dispatch; otherwise reverse
        # proxies can authorize one tool while the app executes another.
        version = request.headers.get("mcp-protocol-version")
        if version == MCP_VERSION:
            params = payload.get("params")
            meta = params.get("_meta") if isinstance(params, dict) else None
            if (
                not isinstance(meta, dict)
                or meta.get("io.modelcontextprotocol/protocolVersion") != MCP_VERSION
                or request.headers.get("mcp-method") != method
                or (method == "tools/call" and (
                    not isinstance(params, dict)
                    or request.headers.get("mcp-name") != params.get("name")
                ))
            ):
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": request_id, "error": {
                        "code": -32020, "message": "HeaderMismatch",
                    }},
                    status_code=400,
                )
            capabilities = meta.get("io.modelcontextprotocol/clientCapabilities")
            if not isinstance(capabilities, dict):
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": request_id, "error": {
                        "code": -32602, "message": "Missing clientCapabilities in request _meta",
                    }}, status_code=400,
                )
        elif version is not None and version not in SUPPORTED_VERSIONS:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": request_id, "error": {
                    "code": -32022, "message": "Unsupported protocol version",
                    "data": {"supported": sorted(SUPPORTED_VERSIONS), "requested": version},
                }}, status_code=400,
            )
        modern = version == MCP_VERSION
        if method == "notifications/initialized" and not modern:
            return Response(status_code=202)
        if "id" not in payload:
            return Response(status_code=202)
        if method == "server/discover" and modern:
            return _rpc_result(request_id, {
                "supportedVersions": sorted(SUPPORTED_VERSIONS),
                "capabilities": {"tools": {}},
                "instructions": "Read-only TREX market research and hypothetical expiration payoffs.",
                "ttlMs": 60000,
                "cacheScope": "private",
            }, modern=True)
        if method == "initialize" and not modern:
            params = payload.get("params") or {}
            if not isinstance(params, dict):
                return _rpc_error(request_id, -32602, "Invalid parameters")
            offered = params.get("protocolVersion")
            agreed = offered if offered in SUPPORTED_VERSIONS else MCP_VERSION
            return _rpc_result(request_id, {
                "protocolVersion": agreed,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "trex-research-mcp", "version": "0.2.0"},
                "instructions": "Read-only TREX data and hypothetical expiration math. No trade, approval, account, or broker operations. Always disclose quote age and source.",
            })
        if method == "ping" and not modern:
            return _rpc_result(request_id, {})
        if method == "tools/list":
            return _rpc_result(request_id, {"tools": TOOLS}, modern=modern)
        if method == "tools/call":
            params = payload.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                return _rpc_error(request_id, -32602, "Invalid tool request")
            args = params.get("arguments", {})
            if not isinstance(args, dict):
                return _rpc_error(request_id, -32602, "Tool arguments must be an object")
            try:
                result = await invoke_tool(params["name"], args, principal)
                return _rpc_result(request_id, _tool_success(result), modern=modern)
            except InvalidScenario as exc:
                return _rpc_result(request_id, _tool_error(str(exc)), modern=modern)
            except UpstreamUnavailable:
                return _rpc_result(
                    request_id, _tool_error("TREX read unavailable; no quote inferred"),
                    modern=modern,
                )
            except AccessDenied:
                return _rpc_result(request_id, _tool_error("Tenant access denied"), modern=modern)
        if version == MCP_VERSION:
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32601, "message": "Method not found",
            }}, status_code=404)
        return _rpc_error(request_id, -32601, "Method not found")

    return app

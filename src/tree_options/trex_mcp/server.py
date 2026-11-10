"""Tenant-isolated, OAuth-authenticated, read-only MCP Streamable HTTP facade.

Only a fixed allowlist of existing TREX GET routes is reachable from tools.
Never import the supervised desk, broker adapters, command queue, or state
writers. An LLM has no permit or order authority through this process.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Awaitable, Callable
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


class AccessDenied(Exception):
    """The token is inactive, underscoped or has no bound TREX instance."""


class UpstreamUnavailable(Exception):
    """The TREX read-only upstream is unavailable or returned invalid data."""


@dataclass(frozen=True)
class Principal:
    tenant_id: str


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
            ):
                raise ValueError("TREX upstreams must be fixed loopback HTTP instances")
        public_url = os.environ["TREX_MCP_PUBLIC_URL"].rstrip("/")
        auth_server = os.environ["TREX_MCP_AUTHORIZATION_SERVER"].rstrip("/")
        introspection = os.environ["TREX_MCP_INTROSPECTION_URL"]
        if not all(urlsplit(u).scheme == "https" for u in (public_url, auth_server, introspection)):
            raise ValueError("public MCP and OAuth endpoints must all use HTTPS")
        if not public_url.endswith("/mcp"):
            raise ValueError("TREX_MCP_PUBLIC_URL must be the public HTTPS .../mcp endpoint")
        origins = frozenset(
            o.strip() for o in os.environ.get("TREX_MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()
        )
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


async def _introspect(token: str, config: Config) -> Principal:
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False, follow_redirects=False) as client:
            response = await client.post(
                config.introspection_url,
                data={"token": token, "token_type_hint": "access_token"},
                auth=(config.client_id, config.client_secret),
            )
            response.raise_for_status()
            claims = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise UpstreamUnavailable("OAuth introspection failed closed") from exc
    if not isinstance(claims, dict) or claims.get("active") is not True:
        raise AccessDenied("inactive access token")
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
    return Principal(tenant_id=tenant)


async def _read_trex(config: Config, tenant: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
    """Only vetted local GET endpoints; reject redirects and downstream errors."""
    if tenant not in config.tenants:
        raise AccessDenied("unknown tenant")
    if not re.fullmatch(r"/api/market/[A-Z.]{1,6}(?:/options)?", path):
        raise UpstreamUnavailable("endpoint not in MCP allowlist")
    # The caller has already produced path from the validated symbol; no URL input is exposed.
    url = config.tenants[tenant].rstrip("/") + path
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False, follow_redirects=False) as client:
            response = await client.get(url, params=params)
            if response.status_code != 200:
                raise UpstreamUnavailable(f"TREX market read returned HTTP {response.status_code}")
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise UpstreamUnavailable("TREX market read failed") from exc
    if not isinstance(data, dict):
        raise UpstreamUnavailable("TREX market response must be a JSON object")
    return data


TOOLS = [
    {
        "name": "trex_market_snapshot",
        "description": (
            "Read a tenant's TREX recorded/delayed quote with original age and source timestamp. "
            "Never asserts a live executable price; no portfolio or order data."
        ),
        "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string", "description": "Ticker, e.g. TSLA"}}, "required": ["symbol"], "additionalProperties": False},
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
                "symbol": {"type": "string"},
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
                "symbol": {"type": "string"},
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
    # Deliberately exclude TREX's news and bars. No customer portfolio metadata.
    quote = doc.get("quote")
    if not isinstance(quote, dict):
        quote = None
    allowed_market_fields = {
        "symbol", "last", "price", "close", "bid", "ask", "volume",
        "source_as_of", "source", "change", "change_pct", "previous_close",
    }
    if quote is not None:
        quote = {k: v for k, v in quote.items() if k in allowed_market_fields}
    return {
        "schema": "trex-mcp-market/1",
        "symbol": symbol,
        "trex_now": doc.get("now"),
        "quote": quote,
        "quote_age_seconds": doc.get("quote_age_seconds"),
        "source": "TREX discovery cache; quote_age_seconds is not a live execution guarantee",
        "executable": False,
    }


def _safe_options_view(doc: dict[str, Any], symbol: str, exp: str | None, right: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": "trex-mcp-options/1",
        "symbol": symbol,
        "trex_now": doc.get("now"),
        "available": doc.get("available") is True,
        "warnings": doc.get("warnings") if isinstance(doc.get("warnings"), list) else [],
        "requested_expiration": exp,
        "right": right,
        "cache_is_not_executable_quote": True,
        "nearest_expiry_slice_is_not_a_complete_option_chain": True,
    }
    for kind in ("recorded", "live"):
        raw = doc.get(kind)
        if not isinstance(raw, dict):
            result[kind] = None
            continue
        rows = raw.get("slice")
        if not isinstance(rows, list):
            rows = []
        expiries = sorted({str(r.get("exp")) for r in rows if isinstance(r, dict) and r.get("exp")})
        filtered = [
            r for r in rows
            if isinstance(r, dict)
            and (exp is None or r.get("exp") == exp)
            and (right == "both" or str(r.get("right", "")).upper() in (("P", "PUT") if right == "put" else ("C", "CALL")))
        ]
        # Keep response bounded. Deterministic ordering and explicit truncation.
        result[kind] = {
            "session": raw.get("session"),
            "fetched_at": raw.get("fetched_at"),
            "age_seconds": raw.get("age_seconds"),
            "ttl_seconds": raw.get("ttl_seconds"),
            "spot": raw.get("spot"),
            "cards": raw.get("cards") if kind == "recorded" else None,
            "expiries_in_retrieved_slice": expiries,
            "expiry_found": exp is None or exp in expiries,
            "matching_rows": len(filtered),
            "returned_rows": filtered[:80],
            "truncated": len(filtered) > 80,
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
                    "name": "trex-research-mcp", "version": "0.1.0",
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

    async def _authenticate(token: str) -> Principal:
        if authenticate is not None:
            return await authenticate(token)
        return await _introspect(token, current_config)

    async def _fetch(tenant: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if fetcher is not None:
            return await fetcher(tenant, path, params)
        return await _read_trex(current_config, tenant, path, params)

    app = FastAPI(title="TREX read-only MCP gateway", docs_url=None, redoc_url=None, openapi_url=None)

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
        if not auth.startswith("Bearer ") or not auth[7:].strip():
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
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return Response(status_code=415)
        length_header = request.headers.get("content-length")
        if length_header:
            try:
                if int(length_header) > MAX_REQUEST_BYTES:
                    return Response(status_code=413)
            except ValueError:
                return Response(status_code=400)
        body = await request.body()
        if len(body) > MAX_REQUEST_BYTES:
            return Response(status_code=413)
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _rpc_error(None, -32700, "Parse error")
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            return _rpc_error(None, -32600, "Invalid JSON-RPC request")
        method = payload.get("method")
        request_id = payload.get("id")
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
                "serverInfo": {"name": "trex-research-mcp", "version": "0.1.0"},
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

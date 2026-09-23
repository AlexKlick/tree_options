"""Market data fetcher for the market desk (discovery lane, unsandboxed).

Sources (pinned by the M0 probes, docs/trex-market-sources-probe-2026-09-22):
- CBOE delayed quotes: keyless, carries bid/ask/close/iv30 + a payload
  ``timestamp`` used as source_as_of (DATA age, not transport age).
- CBOE full delayed option chain: per-contract rows WITH greeks - the M3
  shadow-marking universe (works post-close, unlike IBKR delayed).
- Google News RSS per symbol (Mozilla UA, stdlib ET parse, item cap).
- Polygon daily bars through the in-repo MassiveClient, fetched with
  ``use_cache=False`` for the rolling window so the permanent research
  cache can never relabel stale bars as fresh.

FLOAT CONVENTION: this lane speaks floats; nothing here may write into
book-lane files (which are Decimal-strings).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from tree_options.time.sessions import shift_instant
from tree_options.trex.clock import ET as ET_TZ
from tree_options.trex.clock import now_et

log = logging.getLogger("trex.discovery.market")

CBOE_QUOTE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/{sym}.json"
CBOE_CHAIN_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
NEWS_RSS_URL = (
    "https://news.google.com/rss/search?q={sym}+stock&hl=en-US&gl=US&ceid=US:en"
)
MOZILLA_UA = "Mozilla/5.0 (X11; Linux x86_64) trex-cockpit/1.0"

REQUEST_TIMEOUT = 10.0
CHAIN_MAX_BYTES = 16_000_000  # SPY's full chain is ~5.5MB
RSS_MAX_BYTES = 2_000_000
NEWS_ITEM_CAP = 12
BARS_WINDOW_DAYS = 200

KIND_TTL_SECONDS = {
    "quote": 60,
    "chain": 300,
    "news": 1800,
    "bars": 86400,
}


class Transport(Protocol):
    def __call__(self, url: str, *, timeout: float) -> tuple[int, bytes]: ...


def urllib_transport(url: str, *, timeout: float = REQUEST_TIMEOUT) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": MOZILLA_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read(CHAIN_MAX_BYTES)


class MarketCache:
    """TTL envelope cache: market/cache/<kind>/<KEY>.json holding
    {fetched_at, ttl_seconds, payload}. Junk entries read as misses."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def ttl(self, kind: str) -> int:
        return KIND_TTL_SECONDS.get(kind, 300)

    def _path(self, kind: str, key: str) -> Path:
        safe = "".join(c if c.isalnum() or c in ".-_" else "_" for c in key)
        return self.root / kind / f"{safe}.json"

    def get(self, kind: str, key: str, now: datetime) -> dict[str, Any] | None:
        path = self._path(kind, key)
        if not path.exists():
            return None
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        fetched = doc.get("fetched_at")
        if not isinstance(fetched, str):
            return None
        try:
            fetched_dt = datetime.fromisoformat(fetched)
        except ValueError:
            return None
        if (now - fetched_dt).total_seconds() > int(doc.get("ttl_seconds", 0)):
            return None
        payload = doc.get("payload")
        return payload if isinstance(payload, dict) else None

    def get_envelope(self, kind: str, key: str) -> dict[str, Any] | None:
        """Raw envelope regardless of TTL age. For sources where staleness
        is tolerable and disclosed (news, daily bars) - never quotes."""
        path = self._path(kind, key)
        if not path.exists():
            return None
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        return doc if isinstance(doc, dict) else None

    def put(self, kind: str, key: str, payload: dict[str, Any], now: datetime) -> None:
        path = self._path(kind, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(
                {"fetched_at": now.isoformat(), "ttl_seconds": self.ttl(kind), "payload": payload}
            )
        )
        os.replace(tmp, path)


def _get_json(url: str, transport: Transport, timeout: float = REQUEST_TIMEOUT) -> dict[str, Any]:
    status, body = transport(url, timeout=timeout)
    if status != 200:
        raise RuntimeError(f"{url} -> HTTP {status}")
    return json.loads(body)


def _source_as_of(raw: object) -> str | None:
    """CBOE's data.timestamp is a naive UTC wall stamp ("2026-09-22
    23:35:04"); normalize to an aware ISO instant so age math and UI
    rendering stay honest. Unparseable stamps pass through untouched."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.isoformat()


def fetch_equity_quote(sym: str, transport: Transport) -> dict[str, Any]:
    """CBOE delayed quote with the payload timestamp as source_as_of."""
    doc = _get_json(CBOE_QUOTE_URL.format(sym=sym), transport)
    d = doc.get("data", {})
    return {
        "bid": d.get("bid"),
        "ask": d.get("ask"),
        "close": d.get("close"),
        "iv30": d.get("iv30"),
        "change_pct": d.get("price_change_percent"),
        "source_as_of": _source_as_of(doc.get("timestamp")),
    }


def _parse_occ(option: str) -> tuple[str, float] | None:
    """OCC symbol -> ('20261218', 575.0); calls/non-options -> None.

    Format: ROOT(1-6) + YYMMDD(6) + C/P(1) + strike*1000 zero-padded to
    8. Tail-based slicing (a fixed root width is wrong for 3-char roots).
    """
    if len(option) < 16:
        return None
    strike_raw = option[-8:]
    right = option[-9]
    yymmdd = option[-15:-9]
    if right != "P":
        return None
    try:
        strike = int(strike_raw) / 1000.0
        expiry = "20" + yymmdd
    except ValueError:
        return None
    if len(yymmdd) != 6:
        return None
    return expiry, strike


def fetch_chain_puts(sym: str, transport: Transport) -> dict[str, dict[float, dict[str, Any]]]:
    """Full CBOE delayed chain filtered to puts:
    {expiry_yyyymmdd: {strike: {bid, ask, iv, delta, open_interest}}}."""
    doc = _get_json(CBOE_CHAIN_URL.format(sym=sym), transport, timeout=30.0)
    out: dict[str, dict[float, dict[str, Any]]] = {}
    for row in doc.get("data", {}).get("options", []):
        parsed = _parse_occ(str(row.get("option", "")))
        if parsed is None:
            continue
        expiry, strike = parsed
        out.setdefault(expiry, {})[strike] = {
            "bid": row.get("bid"),
            "ask": row.get("ask"),
            "iv": row.get("iv"),
            "delta": row.get("delta"),
            "open_interest": row.get("open_interest"),
        }
    return out


def fetch_news(sym: str, transport: Transport) -> list[dict[str, Any]]:
    """Google News RSS items (title/link/pubDate/source), capped; junk or
    unparseable feeds return [] (degradation, never an exception)."""
    try:
        status, body = transport(NEWS_RSS_URL.format(sym=sym), timeout=REQUEST_TIMEOUT)
        if status != 200:
            return []
        root = ET.fromstring(body[:RSS_MAX_BYTES])
    except (ET.ParseError, RuntimeError, OSError, ValueError):
        return []
    items: list[dict[str, Any]] = []
    for item in root.findall("./channel/item")[:NEWS_ITEM_CAP]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        items.append(
            {
                "title": title,
                "link": link,
                "pub": item.findtext("pubDate"),
                "source": (item.findtext("source") or "").strip(),
            }
        )
    return items


class BarsClient(Protocol):
    def get_json(
        self, path: str, params: dict[str, Any], *, use_cache: bool = True
    ) -> dict[str, Any]: ...


def _bars_client() -> Any:
    from tree_options.data.massive_client import MassiveClient, load_api_key

    return MassiveClient(api_key=load_api_key(), cache_dir=None, timeout=15.0)


def fetch_daily_bars(sym: str, client: BarsClient | None = None) -> list[dict[str, Any]]:
    """Polygon daily closes for the rolling window. use_cache=False: the
    permanent research cache must never satisfy a mutable-window refresh."""
    client = client or _bars_client()
    # Window bounds via the sanctioned instant shift (never naive date
    # arithmetic): now in ET, back BARS_WINDOW_DAYS*86400s, back to ET date.
    now = now_et()
    end = now.date()
    start = shift_instant(now, -BARS_WINDOW_DAYS * 86_400).astimezone(ET_TZ).date()
    body = client.get_json(
        f"/v2/aggs/ticker/{sym}/range/1/day/{start.isoformat()}/{end.isoformat()}",
        {"adjusted": "true", "sort": "asc", "limit": "300"},
        use_cache=False,
    )
    return [
        {
            "t": int(b["t"]),
            "c": float(b["c"]),
            # MassiveClient yields Decimal volumes (adjusted) - JSON-safe them
            "v": float(b["v"]) if b.get("v") is not None else None,
        }
        for b in body.get("results", [])
        if isinstance(b.get("t"), (int, float)) and b.get("c") is not None
    ]


def market_cycle(
    state_dir: Path,
    cfg: Any,
    now: datetime,
    transport: Transport = urllib_transport,
    symbols: list[str] | None = None,
    force: bool = False,
    bars_client: BarsClient | None = None,
) -> bool:
    """TTL-gated quote refresh across the symbol list -> market.json.

    Per-symbol failure isolation: one dead source records an error entry,
    never a failed cycle. The snapshot carries source_as_of per symbol so
    the UI shows DATA age, not transport age. A forced cycle additionally
    warms the bars + news caches for the forced symbols (symbol-detail
    pages read those envelopes).
    """
    cache = MarketCache(state_dir / "market" / "cache")
    symbols = symbols or list(getattr(cfg, "underlyings", []))
    out: dict[str, Any] = {}
    errors: dict[str, str] = {}
    changed = False
    for sym in symbols:
        cached = None if force else cache.get("quote", sym, now)
        if cached is not None:
            out[sym] = cached
            continue
        try:
            quote = fetch_equity_quote(sym, transport)
            cache.put("quote", sym, quote, now)
            out[sym] = quote
            changed = True
        except Exception as exc:  # per-symbol isolation
            errors[sym] = f"{type(exc).__name__}: {exc}"
            log.warning("quote fetch failed for %s: %s", sym, exc)
    if force and symbols:
        for sym in symbols:
            if cache.get("bars", sym, now) is None:
                try:
                    bars = fetch_daily_bars(sym, bars_client)
                    if bars:  # empty fetch never poisons the cache
                        cache.put("bars", sym, {"bars": bars}, now)
                except Exception as exc:  # per-symbol isolation
                    errors[f"{sym}:bars"] = f"{type(exc).__name__}: {exc}"
            if cache.get("news", sym, now) is None:
                try:
                    news = fetch_news(sym, transport)
                    if news:  # degraded (empty) fetches stay uncached
                        cache.put("news", sym, {"items": news}, now)
                except Exception as exc:  # per-symbol isolation
                    errors[f"{sym}:news"] = f"{type(exc).__name__}: {exc}"
        changed = True
    else:
        # keep already-warmed news envelopes fresh (expired -> refetch);
        # cold symbols wait for an explicit refresh - no ambient RSS churn
        for sym in symbols:
            if cache.get_envelope("news", sym) is None:
                continue
            if cache.get("news", sym, now) is not None:
                continue
            try:
                news = fetch_news(sym, transport)
                if news:
                    cache.put("news", sym, {"items": news}, now)
            except Exception as exc:  # per-symbol isolation
                errors[f"{sym}:news"] = f"{type(exc).__name__}: {exc}"
    if not changed and (state_dir / "market.json").exists() and not force:
        return False
    doc = {"last_refresh": now.isoformat(), "symbols": out, "errors": errors}
    path = state_dir / "market.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    os.replace(tmp, path)
    return True

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
from datetime import UTC, date, datetime
from decimal import Decimal
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
BARS_WINDOW_DAYS = 365  # ~250 sessions: symbol chart + scenario analogs

KIND_TTL_SECONDS = {
    "quote": 60,
    "chain": 300,
    "viewchain": 300,
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

    def mark_attempt(self, kind: str, key: str, now: datetime) -> None:
        """Stamp a failed/empty refresh on an existing envelope (payload and
        fetched_at untouched) so retries can back off."""
        env = self.get_envelope(kind, key)
        if env is None:
            return
        env["attempted_at"] = now.isoformat()
        path = self._path(kind, key)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(env))
        os.replace(tmp, path)

    def attempted_within(self, kind: str, key: str, now: datetime, seconds: float) -> bool:
        env = self.get_envelope(kind, key)
        stamp = (env or {}).get("attempted_at")
        if not isinstance(stamp, str):
            return False
        try:
            return (now - datetime.fromisoformat(stamp)).total_seconds() < seconds
        except (TypeError, ValueError):
            return False

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
    d = doc.get("data") or {}
    if all(d.get(k) is None for k in ("bid", "ask", "close")):
        # a 200 with no prices must not overwrite a good cached quote
        raise RuntimeError(f"{sym}: empty quote payload")
    return {
        "bid": d.get("bid"),
        "ask": d.get("ask"),
        "close": d.get("close"),
        "iv30": d.get("iv30"),
        "change_pct": d.get("price_change_percent"),
        "source_as_of": _source_as_of(doc.get("timestamp")),
    }


def parse_occ(symbol: str) -> tuple[date, str, Decimal]:
    """OCC option symbol -> (expiry, right "C"|"P", strike), both rights.

    Format: ROOT + YYMMDD(6) + C/P(1) + strike*1000 zero-padded to 8.
    Tail-based slicing (a fixed root width is wrong for 3-char roots and
    for adjusted roots like ``KO1``). The strike is an exact Decimal
    (``SPY261218P00587500`` -> ``Decimal("587.5")``). Anything malformed
    (short, unknown right, non-ASCII-digit date or strike, impossible
    calendar date) raises ValueError.
    """
    if len(symbol) < 16:
        raise ValueError(f"OCC symbol too short: {symbol!r}")
    strike_raw = symbol[-8:]
    right = symbol[-9]
    yymmdd = symbol[-15:-9]
    if right not in ("C", "P"):
        raise ValueError(f"OCC right must be C or P: {symbol!r}")
    for part in (strike_raw, yymmdd):
        if not (part.isascii() and part.isdigit()):
            raise ValueError(f"OCC date/strike not digits: {symbol!r}")
    expiry = date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:]))
    return expiry, right, Decimal(int(strike_raw)) / Decimal(1000)


def _parse_occ(option: str) -> tuple[str, float] | None:
    """OCC symbol -> ('20261218', 575.0); calls/non-options -> None.

    The puts-only view the discovery chain cache has always used, now a
    thin wrapper over :func:`parse_occ`. The float strike equals the old
    ``int(strike_raw) / 1000.0`` bit for bit (both are the correctly
    rounded value of the same rational); malformed symbols, which used to
    yield a garbage expiry string, are now None.
    """
    try:
        expiry, right, strike = parse_occ(option)
    except ValueError:
        return None
    if right != "P":
        return None
    return expiry.strftime("%Y%m%d"), float(strike)


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


VIEWCHAIN_ATM_RUNGS = 15  # ladder rungs kept per expiry around ATM (the
# cache-level band; it covers every window the viewer can request, <= 15)


def _parse_occ_both(option: str) -> tuple[str, str, float] | None:
    """OCC symbol -> ('20261218', 'C', 575.0) for BOTH rights; junk -> None."""
    try:
        expiry, right, strike = parse_occ(option)
    except ValueError:
        return None
    return expiry.strftime("%Y%m%d"), right, float(strike)


def nearest_rung(ladder: list[float], spot: float | None) -> float:
    """The rung nearest the underlying spot; without a spot, the middle
    rung (nearest the strike median) of the tight money cluster."""
    if spot is not None:
        return min(ladder, key=lambda s: (abs(s - spot), s))
    mid = (ladder[(len(ladder) - 1) // 2] + ladder[len(ladder) // 2]) / 2
    return min(ladder, key=lambda s: (abs(s - mid), s))


def _chain_spot(data: dict[str, Any]) -> float | None:
    """The chain payload's underlying price (current_price, close as the
    fallback); absent/non-numeric -> None (the median-rung fallback ATM)."""
    for key in ("current_price", "close"):
        val = data.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return float(val)
    return None


def fetch_chain_view(sym: str, transport: Transport) -> dict[str, Any]:
    """Full CBOE delayed chain REDUCED for the viewer's live panel:
    {"spot": float|None, "expiries": {yyyymmdd: {right: {strike:
    {bid, ask, iv, delta, volume, open_interest}}}}}.

    Same wire payload ``fetch_chain_puts`` reads (SPY's is ~5.5 MB); only
    strikes within ATM +/- VIEWCHAIN_ATM_RUNGS ladder rungs per expiry
    survive, BOTH rights, the six quote/greek fields (nulls pass through).
    ATM is the rung nearest the payload's underlying price; a payload
    without one falls back to the median-nearest rung. The full chain never
    touches the disk cache - the puts-only "chain" kind stays the shadow
    marker's and this stays the viewer's (calls included).
    """
    doc = _get_json(CBOE_CHAIN_URL.format(sym=sym), transport, timeout=30.0)
    data = doc.get("data") or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"{sym}: chain payload has no data object")
    spot = _chain_spot(data)
    rows: dict[str, dict[str, dict[float, dict[str, Any]]]] = {}
    for row in data.get("options", []):
        parsed = _parse_occ_both(str(row.get("option", "")))
        if parsed is None:
            continue
        expiry, right, strike = parsed
        rows.setdefault(expiry, {}).setdefault(right, {})[strike] = {
            "bid": row.get("bid"),
            "ask": row.get("ask"),
            "iv": row.get("iv"),
            "delta": row.get("delta"),
            "volume": row.get("volume"),
            "open_interest": row.get("open_interest"),
        }
    expiries: dict[str, dict[str, dict[float, dict[str, Any]]]] = {}
    for expiry, rights in rows.items():
        ladder = sorted({s for strikes in rights.values() for s in strikes})
        atm = nearest_rung(ladder, spot)
        atm_i = ladder.index(atm)
        keep = set(ladder[max(0, atm_i - VIEWCHAIN_ATM_RUNGS) : atm_i + VIEWCHAIN_ATM_RUNGS + 1])
        expiries[expiry] = {
            right: {s: quote for s, quote in strikes.items() if s in keep}
            for right, strikes in rights.items()
        }
    return {"spot": spot, "expiries": expiries}


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


NEWS_REWARM_PER_CYCLE = 2  # bound ambient RSS work per serve tick
REWARM_RETRY_SECONDS = 900  # back off a failed/empty re-warm


def _read_snapshot(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


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

    Per-symbol failure isolation: one dead source records an error entry
    and CARRIES the symbol's last good quote (its source_as_of keeps the
    age honest), never a failed cycle. ``symbols=None`` means the config
    universe; an explicit empty list means an empty watchlist. A forced
    refresh of a subset merges into the snapshot instead of replacing it,
    and additionally warms bars + news + viewchain for the forced symbols.
    ``last_refresh`` moves only on a successful fetch; ``last_attempt``
    (the runner's cadence clock) moves every cycle.
    """
    cache = MarketCache(state_dir / "market" / "cache")
    path = state_dir / "market.json"
    prior = _read_snapshot(path)
    raw_prior = prior.get("symbols")
    prior_syms: dict[str, Any] = raw_prior if isinstance(raw_prior, dict) else {}
    universe = list(symbols) if symbols is not None else list(getattr(cfg, "underlyings", []))
    out: dict[str, Any] = dict(prior_syms) if (force and symbols is not None) else {}
    errors: dict[str, str] = {}
    fetched_any = False
    for sym in universe:
        cached = None if force else cache.get("quote", sym, now)
        if cached is not None:
            out[sym] = cached
            continue
        try:
            quote = fetch_equity_quote(sym, transport)
            cache.put("quote", sym, quote, now)
            out[sym] = quote
            fetched_any = True
        except Exception as exc:  # per-symbol isolation
            errors[sym] = f"{type(exc).__name__}: {exc}"
            log.warning("quote fetch failed for %s: %s", sym, exc)
            if sym in prior_syms:
                out[sym] = prior_syms[sym]  # last good quote, age disclosed
    if force and universe:
        for sym in universe:
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
            if cache.get("viewchain", sym, now) is None:
                try:
                    view = fetch_chain_view(sym, transport)
                    if view.get("expiries"):  # empty chain stays uncached
                        cache.put("viewchain", sym, view, now)
                except Exception as exc:  # per-symbol isolation
                    errors[f"{sym}:viewchain"] = f"{type(exc).__name__}: {exc}"
    else:
        # keep already-warmed news fresh, bounded per cycle and backed off
        # after a failed/empty attempt (a dead feed must not stall the loop)
        attempts = 0
        for sym in universe:
            if attempts >= NEWS_REWARM_PER_CYCLE:
                break
            env = cache.get_envelope("news", sym)
            if env is None or cache.get("news", sym, now) is not None:
                continue
            if cache.attempted_within("news", sym, now, REWARM_RETRY_SECONDS):
                continue
            attempts += 1
            try:
                news = fetch_news(sym, transport)
            except Exception as exc:  # per-symbol isolation
                news = []
                errors[f"{sym}:news"] = f"{type(exc).__name__}: {exc}"
            if news:
                cache.put("news", sym, {"items": news}, now)
            else:
                cache.mark_attempt("news", sym, now)
    snapshot_same = set(out) == set(prior_syms) and not errors and not prior.get("errors")
    if path.exists() and not force and not fetched_any and snapshot_same:
        return False
    last_ok = now.isoformat() if (fetched_any or not path.exists()) else prior.get("last_refresh")
    doc = {
        "last_refresh": last_ok,
        "last_attempt": now.isoformat(),
        "symbols": out,
        "errors": errors,
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    os.replace(tmp, path)
    return True

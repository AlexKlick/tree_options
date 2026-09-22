"""One-shot capability probe for the free market-data sources.

Pins the exact wire shapes the market fetcher (M5) will encode: CBOE
delayed equity quote + full option chain key paths, Google News RSS item
shape, Polygon equity daily aggregates via the in-repo MassiveClient, and
the local OpenAI-compatible LLM. Writes nothing; prints JSON for the
evidence doc. Keys are never printed (the MassiveClient discipline).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
import xml.etree.ElementTree as ET

from tree_options.trex.clock import now_et

CBOE_QUOTE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/{sym}.json"
CBOE_CHAIN_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
NEWS_RSS_URL = (
    "https://news.google.com/rss/search?q={query}+stock&hl=en-US&gl=US&ceid=US:en"
)
MOZILLA_UA = "Mozilla/5.0 (X11; Linux x86_64) trex-cockpit/1.0"
LOCAL_LLM_BASE = "http://127.0.0.1:18000/v1"
LOCAL_LLM_MODEL = "Qwen/Qwen3.8-27B"
MAX_BYTES = 2_000_000


def _get(url: str, timeout: float = 10.0, max_bytes: int = MAX_BYTES) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": MOZILLA_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read(max_bytes)


def probe_cboe_quote(sym: str) -> dict[str, object]:
    status, body = _get(CBOE_QUOTE_URL.format(sym=sym))
    data = json.loads(body)
    d = data.get("data", {})
    return {
        "http_status": status,
        "top_level_keys": sorted(data.keys()),
        "data_keys": sorted(d.keys()),
        "sample": {k: d.get(k) for k in ("bid", "ask", "close", "iv30", "last_trade_time")},
        "timestamp": data.get("timestamp"),
    }


def probe_cboe_chain(sym: str) -> dict[str, object]:
    # the full delayed chain is several MB (SPY ~5.5MB); read enough to parse
    status, body = _get(CBOE_CHAIN_URL.format(sym=sym), timeout=30.0, max_bytes=16_000_000)
    data = json.loads(body)
    opts = data.get("data", {}).get("options", [])
    first = opts[0] if opts else {}
    return {
        "http_status": status,
        "bytes_read_capped": len(body),
        "options_rows": len(opts),
        "row_keys": sorted(first.keys()) if first else [],
        "first_row_sample": {
            k: first.get(k) for k in ("option", "bid", "ask", "iv", "delta", "open_interest")
        }
        if first
        else {},
    }


def probe_news_rss(query: str) -> dict[str, object]:
    status, body = _get(NEWS_RSS_URL.format(query=query))
    root = ET.fromstring(body)
    items = root.findall("./channel/item")
    first = items[0] if items else None
    shape: dict[str, object] = {}
    if first is not None:
        shape = {
            "child_tags": sorted({c.tag for c in first}),
            "title": (first.findtext("title") or "")[:80],
            "has_link": bool(first.findtext("link")),
            "pub_date": first.findtext("pubDate"),
        }
    return {"http_status": status, "items": len(items), "first_item": shape}


def probe_polygon_bars(sym: str) -> dict[str, object]:
    from tree_options.data.massive_client import MassiveClient, load_api_key

    client = MassiveClient(
        api_key=load_api_key(), cache_dir=None, timeout=15.0
    )
    body = client.get_json(
        f"/v2/aggs/ticker/{sym}/range/1/day/2026-08-01/2026-09-22",
        {"adjusted": "true", "sort": "asc", "limit": "120"},
        use_cache=False,
    )
    results = body.get("results", [])
    return {
        "status_field": body.get("status"),
        "results": len(results),
        "bar_keys": sorted(results[0].keys()) if results else [],
        "first_close": results[0].get("c") if results else None,
        "last_close": results[-1].get("c") if results else None,
        "last_bar_date_ms": results[-1].get("t") if results else None,
    }


def probe_local_llm(timeout: float = 90.0) -> dict[str, object]:
    import time

    status, body = _get(f"{LOCAL_LLM_BASE}/models", timeout=5.0)
    models = [m.get("id") for m in json.loads(body).get("data", [])]
    payload = json.dumps(
        {
            "model": LOCAL_LLM_MODEL,
            "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
            "max_tokens": 8,
            "temperature": 0,
        }
    ).encode()
    req = urllib.request.Request(
        f"{LOCAL_LLM_BASE}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        chat = json.loads(resp.read())
    elapsed = round(time.monotonic() - t0, 1)
    content = chat["choices"][0]["message"]["content"]
    return {
        "models_status": status,
        "models": models,
        "chat_reply": str(content)[:80],
        "latency_seconds": elapsed,
    }


def probe_zai_reachable() -> dict[str, object]:
    key = os.environ.get("ZAI_CODING_API_KEY", "").strip()
    if not key:
        return {"skipped": "ZAI_CODING_API_KEY not set in this shell"}
    req = urllib.request.Request(
        "https://api.z.ai/api/paas/v4/models",
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            data = json.loads(resp.read())
        ids = [m.get("id") for m in data.get("data", [])][:20]
        return {"http_status": resp.status, "model_ids_first20": ids}
    except Exception as exc:
        return {"error_type": type(exc).__name__}


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    report: dict[str, object] = {"probe": "free_sources", "when_et": now_et().isoformat()}
    for name, fn in (
        ("cboe_quote_SPY", lambda: probe_cboe_quote("SPY")),
        ("cboe_chain_SPY", lambda: probe_cboe_chain("SPY")),
        ("news_rss_NVDA", lambda: probe_news_rss("NVDA")),
        ("polygon_bars_SPY", lambda: probe_polygon_bars("SPY")),
        ("local_llm", probe_local_llm),
        ("zai_reachable", probe_zai_reachable),
    ):
        try:
            report[name] = fn()
        except Exception as exc:
            report[name] = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()

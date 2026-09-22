"""One-shot equity-quote probe for the paper gateway.

The operator added a $30/mo IBKR market-data subscription; the historic
paper-gateway finding was that equity quotes NEVER arrive (monitor spots
stayed empty; option chains flowed). This probe answers, from a live
session, whether stock quotes arrive at all and whether values move
between two reads (real-time vs frozen/delayed hint). Writes nothing —
prints JSON for the evidence doc. Re-run once during regular hours
(09:35-15:55 ET) for the definitive real-vs-delayed verdict.

 clientId 75 is reserved for probes (71 monitor, 72 enter, 74 discovery,
 73/77 reserved/legacy). HOME must be /home/alexk (state-dir trap).
"""

from __future__ import annotations

import argparse
import json
import logging
import math

from tree_options.trex.clock import now_et
from tree_options.trex.ibkr import IbkrTrex

PROBE_CLIENT_ID = 75
READ_GAP_SECONDS = 3.0
SETTLE_SECONDS = 4.0


def _num(value: object) -> float | None:
    """Ticker float -> float | None (NaN and junk become None)."""
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _read(tickers: dict[str, object]) -> dict[str, dict[str, float | None]]:
    out: dict[str, dict[str, float | None]] = {}
    for sym, t in tickers.items():
        out[sym] = {
            "bid": _num(getattr(t, "bid", None)),
            "ask": _num(getattr(t, "ask", None)),
            "last": _num(getattr(t, "last", None)),
            "close": _num(getattr(t, "close", None)),
            "market_price": _num(getattr(t, "marketPrice", lambda: None)()),
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbols", nargs="+", default=["NVDA", "QQQ", "SPY"], help="stocks to subscribe"
    )
    parser.add_argument("--client-id", type=int, default=PROBE_CLIENT_ID)
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    ibk = IbkrTrex(client_id=args.client_id)
    ibk.connect()
    try:
        assert ibk._ib is not None
        ib = ibk._ib
        from ib_async import Stock

        tickers: dict[str, object] = {}
        for sym in args.symbols:
            stock = Stock(sym, "SMART", "USD")
            ib.qualifyContracts(stock)
            tickers[sym] = ib.reqMktData(stock, "", False, False)
        ib.sleep(SETTLE_SECONDS)
        first = _read(tickers)
        ib.sleep(READ_GAP_SECONDS)
        second = _read(tickers)

        arrived = any(
            v is not None
            for row in second.values()
            for k, v in row.items()
            if k != "close"
        )
        changed = any(
            first[s][k] != second[s][k]
            for s in tickers
            for k in first[s]
            if k != "close" and first[s][k] is not None
        )
        report = {
            "probe": "ibkr_equity_quotes",
            "client_id": args.client_id,
            "market_data_type": 3,
            "when_et": now_et().isoformat(),
            "symbols": args.symbols,
            "quotes_arrived": arrived,
            "values_changed_between_reads": changed,
            "session_note": (
                "probed outside regular hours -> frozen/closed values are expected; "
                "re-probe 09:35-15:55 ET for the real-time-vs-delayed verdict"
                if arrived and not changed
                else "values moved between reads"
                if changed
                else "NO equity quotes arrived at all (the pre-subscription state)"
            ),
            "first_read": first,
            "second_read": second,
        }
        print(json.dumps(report, indent=2))
    finally:
        ibk.disconnect()


if __name__ == "__main__":
    main()

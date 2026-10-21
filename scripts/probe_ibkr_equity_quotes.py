"""Owner-run, READ-ONLY probe: which market-data type IBKR paper delivers per stock.

Wave 2 trades a stock only on a REALTIME quote (``equity_quote_not_realtime``
otherwise), and the paper account historically delivered delayed data
(10091/10167). This probe answers, per symbol, what the gateway delivers
now: it qualifies each ``Stock(symbol, SMART, USD)``, requests market-data
type 3 (IBKR then streams realtime where the account is entitled and
delayed elsewhere, and reports which on the ticker), waits ``--settle``
seconds and records the type, the NBBO in cents and its tick time.

Read-only by construction: the API session connects with ``readonly=True``,
the probe calls no order method (``orders_placed`` is always 0), and it
refuses the desk's clientId 83 (default 75, the probe id; 71 monitor, 72
enter, 74 discovery, 77 legacy), any port but the paper gateway's 4002, and
a session whose managed accounts are not all IBKR paper (``D...``). The
receipt names no account and carries error CLASSES only, never provider
text.

The receipt (``trex-ibkr-equity-quote-probe/1``) goes to ``--receipt`` or
``~/.local/state/trex/probes/ibkr-equity-quotes-<UTC stamp>.json`` and is
never overwritten. Exit codes: 0 receipt written, 2 error, 3 refused.

Run it during regular hours (09:35-15:55 ET) with HOME=/home/alexk, e.g.
``uv run python scripts/probe_ibkr_equity_quotes.py SPY QQQ``. Agents never
run it: it connects to the broker.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tree_options.trex.contracts.defaults import EQUITY_SYMBOL_ALLOWLIST
from tree_options.trex.ibkr import GATEWAY_PAPER_PORT, IbkrTrex
from tree_options.trex.supervised_ibkr import PAPER_ACCOUNT_PREFIX, SUPERVISED_CLIENT_ID

DESCRIPTION = "Read-only probe: the IBKR paper market-data type per stock (writes a receipt)."
RECEIPT_SCHEMA = "trex-ibkr-equity-quote-probe/1"
PROBE_CLIENT_ID = 75
#: IBKR reqMarketDataType 3: realtime where entitled, else delayed
REQUESTED_MARKET_DATA_TYPE = 3
SETTLE_SECONDS = 4.0
CONNECT_TIMEOUT_S = 20
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")


class ProbeRefused(ValueError):
    """The probe would leave its read-only paper envelope; nothing ran."""


def default_ib_factory() -> Any:
    from ib_async import IB  # lazy: optional broker dependency

    return IB()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(instant: datetime) -> str:
    return instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_receipt_path(now: datetime) -> Path:
    name = f"ibkr-equity-quotes-{now.astimezone(UTC):%Y%m%dT%H%M%SZ}.json"
    return Path.home() / ".local/state/trex/probes" / name


def _guard(symbols: Sequence[str], client_id: int, port: int) -> None:
    if client_id == SUPERVISED_CLIENT_ID:
        raise ProbeRefused("supervised_client_id_reserved")
    if port != GATEWAY_PAPER_PORT:
        raise ProbeRefused("not_paper_gateway_port")
    bad = [s for s in symbols if not SYMBOL.match(s)]
    if bad or not symbols:
        raise ProbeRefused("symbol_malformed")


def _row(trex: IbkrTrex, symbol: str, con_id: int | None, error: str | None) -> dict[str, Any]:
    quote = trex.equity_quote(symbol) if con_id is not None else None
    data_type = trex.equity_market_data_type(symbol) if con_id is not None else None
    return {
        "symbol": symbol,
        "qualified": con_id is not None,
        "con_id": con_id,
        "market_data_type": data_type,
        "realtime": data_type == "realtime",
        "two_sided": quote is not None,
        "bid_cents": None if quote is None else quote.bid_cents,
        "ask_cents": None if quote is None else quote.ask_cents,
        "as_of": None if quote is None else _stamp(quote.as_of),
        "error": error,
    }


def probe(
    symbols: Sequence[str],
    *,
    client_id: int = PROBE_CLIENT_ID,
    host: str = "127.0.0.1",
    port: int = GATEWAY_PAPER_PORT,
    settle_s: float = SETTLE_SECONDS,
    ib_factory: Callable[[], Any] = default_ib_factory,
    clock: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    """Probe ``symbols`` over one read-only paper session; the receipt.

    Refusals (``ProbeRefused``) happen before any connection, except the
    paper-account check, which needs the session and runs before any
    contract or market-data request."""
    _guard(symbols, client_id, port)
    started = clock()
    ib = ib_factory()
    trex = IbkrTrex(host=host, port=port, client_id=client_id)
    try:
        ib.connect(host, port, clientId=client_id, timeout=CONNECT_TIMEOUT_S, readonly=True)
        accounts = [str(a) for a in ib.managedAccounts()]
        if not accounts or not all(a.startswith(PAPER_ACCOUNT_PREFIX) for a in accounts):
            raise ProbeRefused("not_paper_session")
        trex._ib = ib
        ib.reqMarketDataType(REQUESTED_MARKET_DATA_TYPE)
        prepared: dict[str, int | None] = {}
        errors: dict[str, str | None] = {}
        for symbol in symbols:
            try:
                prepared[symbol] = trex.prepare_stock(symbol, settle_s=0)
                errors[symbol] = None
            except (ValueError, RuntimeError) as error:  # StockNotQualified included
                prepared[symbol] = None
                errors[symbol] = type(error).__name__
        trex.sleep(settle_s)
        rows = [_row(trex, s, prepared[s], errors[s]) for s in symbols]
        for symbol, con_id in prepared.items():
            if con_id is not None:
                trex.release_stock(symbol)
    finally:
        ib.disconnect()
    return {
        "schema": RECEIPT_SCHEMA,
        "probe": "ibkr_equity_quotes",
        "client_id": client_id,
        "port": port,
        "readonly": True,
        "requested_market_data_type": REQUESTED_MARKET_DATA_TYPE,
        "settle_s": settle_s,
        "started_at": _stamp(started),
        "finished_at": _stamp(clock()),
        "symbols": rows,
        "realtime_symbols": [r["symbol"] for r in rows if r["qualified"] and r["realtime"]],
        "not_realtime_symbols": [r["symbol"] for r in rows if r["qualified"] and not r["realtime"]],
        "unqualified_symbols": [r["symbol"] for r in rows if not r["qualified"]],
        "orders_placed": 0,
    }


def write_receipt(receipt: dict[str, Any], path: Path) -> Path:
    """Write ``receipt`` to ``path`` atomically; an existing file is never
    replaced (FileExistsError)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(tmp, path)  # fails if the receipt already exists
    finally:
        os.unlink(tmp)
    return path


def _emit(document: dict[str, Any]) -> None:
    print(json.dumps(document, sort_keys=True))


def main(
    argv: Sequence[str] | None = None,
    *,
    ib_factory: Callable[[], Any] = default_ib_factory,
    clock: Callable[[], datetime] = _utc_now,
) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument(
        "symbols",
        nargs="*",
        default=sorted(EQUITY_SYMBOL_ALLOWLIST),
        help="stock symbols (default: the equity allowlist)",
    )
    parser.add_argument("--client-id", type=int, default=PROBE_CLIENT_ID)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=GATEWAY_PAPER_PORT)
    parser.add_argument("--settle", type=float, default=SETTLE_SECONDS)
    parser.add_argument("--receipt", type=Path, default=None, help="receipt path")
    args = parser.parse_args(argv)
    path = args.receipt or default_receipt_path(clock())
    try:
        if path.exists():
            raise ProbeRefused("receipt_exists")
        receipt = probe(
            args.symbols,
            client_id=args.client_id,
            host=args.host,
            port=args.port,
            settle_s=args.settle,
            ib_factory=ib_factory,
            clock=clock,
        )
        write_receipt(receipt, path)
    except ProbeRefused as refused:
        _emit({"refused": str(refused)})
        return 3
    except Exception as error:  # the class only: provider text may name the account
        _emit({"error": type(error).__name__})
        return 2
    _emit(
        {
            "receipt": str(path),
            "realtime_symbols": receipt["realtime_symbols"],
            "not_realtime_symbols": receipt["not_realtime_symbols"],
            "unqualified_symbols": receipt["unqualified_symbols"],
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

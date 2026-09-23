"""Generator for the trex EXECUTION calendar (NYSE sessions through 2028).

The protocol calendar (``data/calendar/nyse_sessions_2018_01_02_2026_12_31``,
``scripts/gen_calendar.py``) is sealed for research and must stay
byte-identical, but it ends 2026-12-31: past that, trex's exit machine
sees no sessions and places no exits. trex reads this separate file
instead (``tree_options.trex.clock``). Same pinned build environment, same
payload shape; the output is named after the real first and last session:

    HOME=/home/alexk uv run --with exchange-calendars==4.5.2 --with 'pandas<3' \
        python scripts/gen_trex_calendar.py --start 2018-01-02 --end 2028-12-31

exchange_calendars is a BUILD-TIME dependency only (never in pyproject).
The output carries no wall-clock timestamp, so a rerun with the same pin
and range reproduces the same checksum. Holidays past today are rule-based
(unscheduled closures such as a national day of mourning can't be
known): regenerate when the exchange announces one, or when the monitor's
health reports ``calendar_horizon_warn``.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[1]
PROTOCOL_DIR = REPO / "data" / "calendar"
DEFAULT_OUT_DIR = PROTOCOL_DIR / "trex"
MAX_END = date(2028, 12, 31)
SCOPE = "trex-execution"


def _protocol_generator() -> ModuleType:
    """scripts/gen_calendar.py, imported by path: the pin and the start
    bound stay single-sourced there."""
    spec = importlib.util.spec_from_file_location(
        "gen_calendar", Path(__file__).with_name("gen_calendar.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_payload(calendar: object, start: date, end: date, pin: str) -> dict[str, object]:
    """The protocol generator's payload, from an exchange_calendars calendar.

    Filters the calendar's own session index rather than calling
    ``sessions_in_range``, which refuses an ``end`` that is not itself a
    session (2028-12-31 is a Sunday)."""
    all_sessions = calendar.sessions  # type: ignore[attr-defined]
    in_range = [d for d in all_sessions if start <= d.date() <= end]
    sessions = [d.date().isoformat() for d in in_range]
    # Early closes (13:00 ET): post-Thanksgiving / Christmas-eve half days.
    early_close_set = {d.date() for d in calendar.early_closes}  # type: ignore[attr-defined]
    early_closes = sorted(d.date().isoformat() for d in in_range if d.date() in early_close_set)
    return {
        "calendar": "XNYS",
        "source": f"exchange-calendars=={pin}",
        "scope": SCOPE,
        "timezone": "America/New_York",
        "open": "09:30",
        "close": "16:00",
        "early_close": "13:00",
        "early_close_sessions": early_closes,
        "sessions": sessions,
    }


def main() -> int:
    gen = _protocol_generator()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()

    if args.start > args.end:
        parser.error("--start must be <= --end")
    if args.start < gen.MIN_START or args.end > MAX_END:
        parser.error(f"range must stay within {gen.MIN_START}..{MAX_END}")
    if args.out_dir.resolve() == PROTOCOL_DIR.resolve():
        parser.error("refusing to write into the sealed protocol calendar directory")

    try:
        import exchange_calendars
    except ImportError:
        print(
            "exchange_calendars not available; run via "
            f"`uv run --with exchange-calendars=={gen.PINNED_EXCHANGE_CALENDARS}"
            f" --with 'pandas<3' python {sys.argv[0]}`",
            file=sys.stderr,
        )
        return 2
    used_version = exchange_calendars.__version__
    if used_version != gen.PINNED_EXCHANGE_CALENDARS:
        print(
            f"exchange_calendars {used_version} != pinned {gen.PINNED_EXCHANGE_CALENDARS}",
            file=sys.stderr,
        )
        return 3

    cal = exchange_calendars.get_calendar("XNYS", start=args.start, end=args.end)
    payload = build_payload(cal, args.start, args.end, gen.PINNED_EXCHANGE_CALENDARS)
    sessions = payload["sessions"]
    assert isinstance(sessions, list) and sessions
    first, last = sessions[0], sessions[-1]
    name = f"nyse_sessions_{first.replace('-', '_')}_{last.replace('-', '_')}.json"
    out = args.out_dir / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    out.with_suffix(".sha256").write_text(f"{digest}  {out.name}\n", encoding="utf-8")

    early = payload["early_close_sessions"]
    assert isinstance(early, list)
    print(f"exchange_calendars=={used_version}")
    print(f"sessions: {len(sessions)}  ({first} .. {last})")
    print(f"early closes: {len(early)}")
    print(f"wrote {out}")
    print(f"sha256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""One-time generator for the vendored NYSE session fixture.

Run with an ephemeral environment — exchange_calendars is a BUILD-TIME
dependency only and must never appear in pyproject dependencies:

    uv run --with exchange-calendars==4.5.2 --with 'pandas<3' scripts/gen_calendar.py \
        --start 2018-01-02 --end 2026-12-31 \
        --out data/calendar/nyse_sessions_2018_01_02_2026_12_31.json

The output JSON contains no wall-clock timestamp: regenerating with the same
exchange-calendars version and range reproduces the same checksum. Changing
either is a protocol-relevant change (see docs/m0-evidence.md).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

PINNED_EXCHANGE_CALENDARS = "4.5.2"
# exchange_calendars 4.5.2 is incompatible with pandas 3.x (read-only
# `.values` assignment inside _overwrite_special_dates); pin the build env.
PINNED_BUILD_ENV = "exchange-calendars==4.5.2 pandas<3"

MIN_START = date(2018, 1, 1)
MAX_END = date(2026, 12, 31)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    if args.start > args.end:
        parser.error("--start must be <= --end")
    if args.start < MIN_START or args.end > MAX_END:
        parser.error(
            f"range must stay within {MIN_START}..{MAX_END}; extending the range "
            "changes the committed checksum and must bump meta.protocol_version"
        )

    try:
        import exchange_calendars
    except ImportError:
        print(
            "exchange_calendars not available; run via "
            f"`uv run --with exchange-calendars=={PINNED_EXCHANGE_CALENDARS} {sys.argv[0]}`",
            file=sys.stderr,
        )
        return 2

    used_version = exchange_calendars.__version__
    if used_version != PINNED_EXCHANGE_CALENDARS:
        print(
            f"exchange_calendars {used_version} != pinned {PINNED_EXCHANGE_CALENDARS}; "
            "regenerating with a different version is a protocol-relevant change",
            file=sys.stderr,
        )
        return 3

    cal = exchange_calendars.get_calendar("XNYS", start=args.start, end=args.end)
    sessions = [d.date().isoformat() for d in cal.sessions_in_range(args.start, args.end)]

    payload = {
        "calendar": "XNYS",
        "source": f"exchange-calendars=={PINNED_EXCHANGE_CALENDARS}",
        "timezone": "America/New_York",
        "open": "09:30",
        "close": "16:00",
        "sessions": sessions,
    }
    body = json.dumps(payload, indent=2) + "\n"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(body, encoding="utf-8")

    digest = hashlib.sha256(args.out.read_bytes()).hexdigest()
    checksum_line = f"{digest}  {args.out.name}\n"
    (args.out.with_suffix(".sha256")).write_text(checksum_line, encoding="utf-8")

    print(f"exchange_calendars=={used_version}")
    print(f"sessions: {len(sessions)}  ({sessions[0]} .. {sessions[-1]})")
    print(f"wrote {args.out}")
    print(f"sha256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

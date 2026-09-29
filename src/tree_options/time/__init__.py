"""Time layer: session instants, calendar protocol, static/synthetic calendars."""


def calendar_days(n: int):
    """A timedelta of ``n`` calendar days — the one sanctioned constructor
    (the no-naive-arithmetic tripwire bans timedelta() outside time/)."""
    from datetime import timedelta

    return timedelta(days=n)


def weekday_index(d) -> int:
    """``d``'s weekday as Monday=0 .. Sunday=6 — the sanctioned accessor
    (the no-naive-arithmetic tripwire bans .weekday() outside time/)."""
    return d.weekday()

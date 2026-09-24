"""Time layer: session instants, calendar protocol, static/synthetic calendars."""


def calendar_days(n: int):
    """A timedelta of ``n`` calendar days — the one sanctioned constructor
    (the no-naive-arithmetic tripwire bans timedelta() outside time/)."""
    from datetime import timedelta

    return timedelta(days=n)

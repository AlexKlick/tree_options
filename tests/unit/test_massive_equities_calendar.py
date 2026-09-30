from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tree_options.data.massive_equities import MassiveEquityError, MassiveEquityResearchAdapter
from tree_options.time.calendar import StaticSessionCalendar


def test_daily_bars_require_real_session_calendar_and_enclose_ohlc(tmp_path):
    import hashlib
    import json
    day = date(2026, 9, 29)
    p = tmp_path / 'cal.json'
    p.write_text(json.dumps({'calendar': 'fixture', 'timezone': 'America/New_York', 'open': '09:30', 'close': '16:00', 'sessions': [day.isoformat()]}))
    h = tmp_path / 'cal.sha256'
    h.write_text(hashlib.sha256(p.read_bytes()).hexdigest())
    calendar = StaticSessionCalendar(p, h)
    class Fake:
        def paginate(self, *args, **kwargs):
            return SimpleNamespace(results=[{'session': str(day), 'o': '2', 'h': '3', 'l': '1', 'c': '2', 'v': '3'}], request_ids=('r',))
    with pytest.raises(MassiveEquityError, match='calendar'):
        MassiveEquityResearchAdapter(Fake()).daily_bars(ticker='A', start=day, end=day)
    bars, _ = MassiveEquityResearchAdapter(Fake(), calendar=calendar).daily_bars(ticker='A', start=day, end=day)
    assert bars[0].close == Decimal('2')
    with pytest.raises(MassiveEquityError, match='OHLC'):
        MassiveEquityResearchAdapter._parse_bar({'T': 'A', 'o': '4', 'h': '3', 'l': '1', 'c': '2', 'v': '3'}, session=day)

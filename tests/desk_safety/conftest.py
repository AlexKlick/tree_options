"""Small independent fixtures: all temporal relationships come from a calendar.

This directory can run with --confcutdir here when the full development
installation is unavailable. It does not replace the repository-wide gate.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pytest

from tree_options.desk import playbook, selection
from tree_options.desk.store import encode_document
from tree_options.time.calendar import StaticSessionCalendar
from tree_options.trex.clock import ET

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class World:
    cal: StaticSessionCalendar
    session: date
    entry: date
    deadline: date
    expiry: date
    now: datetime
    queue: dict[str, Any]
    root: Path

    @property
    def db(self) -> Path:
        return self.root / 'evidence' / 'desk.sqlite3'

    @property
    def queues(self) -> Path:
        return self.root / 'queue'

    @property
    def store(self) -> Path:
        return self.root / 'store'

    def queue_file(self, doc=None):
        import json
        doc = self.queue if doc is None else doc
        self.queues.mkdir(parents=True, exist_ok=True)
        p = self.queues / f"{doc['session']}.json"
        p.write_text(json.dumps(doc, sort_keys=True))
        return p

    def chain(self, session, long=('3.00', '3.10'), short=('0.80', '0.90'), *, fetched=None):
        at = datetime.combine(session, time(16, 15), ET)
        doc = {'header': {'schema': 'desk-chain/1', 'session': session.isoformat(),
                         'underlying': 'AAPL', 'source_as_of': at.isoformat(),
                         'fetched_at': (fetched or at).isoformat(), 'raw_sha256': 'd' * 64,
                         'underlying_quote': {'last_trade_time': at.isoformat()},
                         'n': 2},
               'columns': {'right': ['C', 'C'], 'strike': [100, 105],
                           'exp': [self.expiry.isoformat()] * 2,
                           'bid': [float(long[0]), float(short[0])],
                           'ask': [float(long[1]), float(short[1])]}}
        p = self.store / 'chains' / session.isoformat() / 'AAPL.json.gz'
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(encode_document(doc))
        return p, doc


@pytest.fixture
def world(tmp_path):
    base = ROOT / 'data' / 'calendar' / 'trex'
    cal = StaticSessionCalendar(base / 'nyse_sessions_2018_01_02_2028_12_29.json',
                               base / 'nyse_sessions_2018_01_02_2028_12_29.sha256')
    sessions = cal.sessions()
    # A fixture anchor, not wall-clock "today". Every dependent date is a session offset.
    idx = next(i for i, s in enumerate(sessions) if s.year == 2025 and s.month == 3)
    s, entry, deadline, expiry = (sessions[idx + n] for n in (0, 1, 5, 25))
    now = datetime.combine(entry, time(10), ET)
    deal_id = f'{s.isoformat()}:R1:AAPL:example'
    legs = [{'right': 'C', 'action': action, 'strike': strike, 'expiry': expiry.isoformat(),
             'ratio': 1} for action, strike in [('BUY', '100'), ('SELL', '105')]]
    structure = {'id': deal_id, 'deal_id': deal_id, 'kind': 'debit_vertical',
                 'underlying': 'AAPL', 'legs': legs, 'quantity': 1,
                 'entry_date': entry.isoformat(), 'exit_deadline': deadline.isoformat(),
                 'limit': '2.21', 'ref_mid': '2.15',
                 'exits': {'touch': True, 'breach': False}}
    row = {'deal_id': deal_id, 'rank': 1, 'status': 'admissible', 'reasons': [], 'notes': [],
           'row': 'R1', 'row_title': 'Synthetic independent test', 'tier': 'signal',
           'underlying': 'AAPL', 'kind': 'debit_vertical', 'quantity': 1,
           'entry_session': entry.isoformat(), 'exit_deadline': deadline.isoformat(),
           'legs': copy.deepcopy(legs), 'width': '5', 'ref_mid': '2.15', 'fill': '2.20',
           'limit': '2.21', 'max_loss': '221.00', 'structure': structure,
           'decision': {'basis': 'xsmom', 'ev': '12.00', 'ev_stress_fill': '3.00',
                        'ev_per_max_loss': '0.0543'}, 'rails': None}
    config, pb = selection.load_config(), playbook.load_playbook()
    queue = {'schema': 'trex.deal/1', 'session': s.isoformat(),
             'entry_session': entry.isoformat(),
             'decision_cutoff': datetime.combine(entry, time(9, 30), ET).isoformat(),
             'valid_until': datetime.combine(entry, time(11, 30), ET).isoformat(),
             'miner': {'sha256': config.sha256, 'status': config.status, 'version': config.version},
             'playbook': {'sha256': pb.sha256, 'version': pb.version},
             'inputs': {}, 'admissible': [row], 'surfaced': [], 'rows': {}}
    return World(cal, s, entry, deadline, expiry, now, queue, tmp_path)

"""Desk eod-equity: the timer job that replaces the manual Claude card chain
(CRON-paper-engine.md): panel refresh through fetch_ohlc.py, XSMOM/PEAD
signals, draft cards and an ntfy push. It never seals a card.

fetch_ohlc.py is always faked: an injected runner, or a stand-in script in
a tmp paper dir run through the real subprocess path. The real research
panel, the real notify.env and the real state root are never touched (the
autouse fixture pins every desk path and HOME to tmp).
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tree_options.desk import eod_equity, universe
from tree_options.desk.__main__ import run_cli
from tree_options.trex.alert_policy import QuietHours

ET = ZoneInfo("America/New_York")
QUIET = QuietHours(time(22, 0), time(7, 0), ZoneInfo("America/Denver"))
PANEL_END = date(2026, 9, 14)  # where the live research panel stopped


@pytest.fixture(autouse=True)
def _pinned_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DESK_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("TREX_DESK_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("DESK_PAPER_DIR", str(tmp_path / "paper"))
    monkeypatch.setenv("TREX_NOTIFY_ENV", str(tmp_path / "no-notify.env"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DESK_REPO_ROOT", raising=False)


def _growth(name: str) -> Decimal:
    return Decimal(1) + Decimal(universe.PANEL_NAMES.index(name)) / Decimal(100000)


def _bar(close: Decimal) -> dict[str, object]:
    c = str(close)
    return {"open": c, "high": c, "low": c, "close": c, "volume": 1000}


def _write_panel(paper: Path, static_calendar, *, n: int = 320) -> None:
    """37 names, distinct constant growth (SQQQ > TQQQ > GLD > ... by
    PANEL_NAMES order), ending at PANEL_END like the live panel."""
    cal = static_calendar
    i = cal.ordinal(PANEL_END)
    days = [d.isoformat() for d in cal.sessions()[i - n + 1 : i + 1]]
    panel: dict[str, dict[str, object]] = {}
    for name in universe.PANEL_NAMES:
        px = Decimal(100)
        bars = {}
        for d in days:
            bars[d] = _bar(px)
            px = (px * _growth(name)).quantize(Decimal("0.0001"))
        panel[name] = bars
    paper.mkdir(parents=True, exist_ok=True)
    (paper / "ohlc-panel.json").write_text(json.dumps(panel, indent=1, sort_keys=True))
    (paper / "earnings-calendar.json").write_text(
        json.dumps({"COST": ["2026-06-01", "2026-09-24"], "SPY": []}, indent=1)
    )


class FakeFetch:
    """Stands in for fetch_ohlc.py <D>: merges D's bars for all 37 names."""

    def __init__(
        self, paper: Path, *, rc: int = 0, jumps: dict[tuple[str, str], str] | None = None
    ):
        self.paper = paper
        self.rc = rc
        self.jumps = jumps or {}
        self.calls: list[date] = []

    def __call__(self, session: date) -> tuple[int, str]:
        self.calls.append(session)
        if self.rc != 0:
            return self.rc, "VENDOR LAG: no bars"
        path = self.paper / "ohlc-panel.json"
        panel = json.loads(path.read_text())
        d = session.isoformat()
        for name in universe.PANEL_NAMES:
            last = Decimal(panel[name][max(panel[name])]["close"])
            if (name, d) in self.jumps:
                close = Decimal(self.jumps[(name, d)])
            else:
                close = (last * _growth(name)).quantize(Decimal("0.0001"))
            panel[name][d] = _bar(close)
        path.write_text(json.dumps(panel, indent=1, sort_keys=True))
        return 0, f"wrote {d}"


class FakeNotify:
    def __init__(self, ok: bool = True, crash: bool = False) -> None:
        self.ok = ok
        self.crash = crash  # accepted by ntfy, then the process dies
        self.sent: list[tuple[str, str, str]] = []

    def __call__(self, title: str, message: str, priority: str) -> bool:
        self.sent.append((title, message, priority))
        if self.crash:
            raise KeyboardInterrupt("killed right after ntfy accepted the push")
        return self.ok


def _run(
    tmp_path: Path,
    static_calendar,
    *,
    session: date | None,
    now: datetime,
    fetch,
    notify=None,
    quiet=None,
    dry_run: bool = False,
    clock=None,
    lock_timeout_s: float = 5.0,
) -> eod_equity.EodResult:
    return eod_equity.run_eod_equity(
        session=session,
        now=now,
        cal=static_calendar,
        state=tmp_path / "state",
        paper=tmp_path / "paper",
        fetch=fetch,
        notify=notify,
        quiet=quiet,
        dry_run=dry_run,
        clock=clock,
        lock_timeout_s=lock_timeout_s,
    )


def _outbox(tmp_path: Path, pattern: str) -> list[Path]:
    return sorted((tmp_path / "state" / "outbox").glob(pattern))


class TestRebalanceDay:
    def test_gap_fill_signals_draft_push_marker(self, tmp_path: Path, static_calendar) -> None:
        _write_panel(tmp_path / "paper", static_calendar)
        fetch = FakeFetch(tmp_path / "paper")
        notify = FakeNotify()
        d = date(2026, 10, 1)
        res = _run(
            tmp_path,
            static_calendar,
            session=d,
            now=datetime(2026, 10, 1, 16, 40, tzinfo=ET),
            fetch=fetch,
            notify=notify,
        )
        assert (res.exit_code, res.status, res.push) == (0, "ok", "sent")
        # full-tail gap fill: every session after the panel's end, in order
        assert fetch.calls == [s for s in static_calendar.sessions() if PANEL_END < s <= d]
        doc = json.loads((tmp_path / "state/signals/2026-10-01.json").read_text())
        assert doc["session"] == "2026-10-01" and doc["panel_last_session"] == "2026-10-01"
        xs = doc["xsmom"]
        assert xs["is_rebalance_day"] is True and xs["fires"] is True
        assert xs["top3"] == ["SQQQ", "TQQQ", "GLD"]
        assert xs["no_options_expression"] == ["SQQQ", "TQQQ"]
        assert len(xs["scores"]) == 36 and "SPY" not in xs["scores"]
        assert all(isinstance(v, str) for v in xs["scores"].values())
        assert doc["pead"] == [] and doc["pead_evaluated"] == []
        prov = doc["provenance"]
        assert prov["fetched_sessions"][0] == "2026-09-15" and len(prov["fetched_sessions"]) == 13
        assert len(prov["panel_sha256"]) == 64
        assert prov["panel_holes"] == [] and prov["panel_holes_n"] == 0
        assert prov["panel_last_before"] == "2026-09-14"
        draft = (tmp_path / "state/card-drafts/2026-10-01-xsmom-top3.md").read_text()
        assert "DRAFT" in draft and "NOT sealed" in draft
        assert "XSMOM-TOP3 monthly" in draft and "SQQQ" in draft
        assert "2026-10-29" in draft  # exit = the 20th session after entry
        assert draft.isascii()
        (title, message, priority) = notify.sent[0]
        assert len(notify.sent) == 1 and title == "trex desk" and priority == "default"
        assert message == (
            "XSMOM rebalance for session 2026-10-01. Draft card(s) ready for review."
        )
        marker = json.loads((tmp_path / "state/stages/2026-10-01/eod-equity.done.json").read_text())
        assert marker["session"] == "2026-10-01" and marker["push"] == "sent"

        # idempotent: a second run is a no-op (no fetch, no push, no rewrite)
        before = (tmp_path / "state/signals/2026-10-01.json").read_bytes()
        again = _run(
            tmp_path,
            static_calendar,
            session=d,
            now=datetime(2026, 10, 1, 20, 40, tzinfo=ET),
            fetch=fetch,
            notify=notify,
        )
        assert (again.exit_code, again.status) == (0, "already_done")
        assert len(fetch.calls) == 13 and len(notify.sent) == 1
        assert (tmp_path / "state/signals/2026-10-01.json").read_bytes() == before


class TestPeadBeat:
    def test_cost_beat_fires(self, tmp_path: Path, static_calendar) -> None:
        _write_panel(tmp_path / "paper", static_calendar)
        # COST reports 2026-09-24 after the close; 09-25 is the first post-report session
        fetch = FakeFetch(tmp_path / "paper")
        fetch(date(2026, 9, 15))  # warm: know the 09-24 close before scripting the jump
        panel = json.loads((tmp_path / "paper/ohlc-panel.json").read_text())
        g = _growth("COST")
        c = Decimal(panel["COST"]["2026-09-15"]["close"])
        for _ in range(7):  # 09-16 .. 09-24
            c = (c * g).quantize(Decimal("0.0001"))
        fetch.jumps[("COST", "2026-09-25")] = str((c * Decimal("1.02")).quantize(Decimal("0.0001")))
        fetch.calls.clear()
        notify = FakeNotify()
        res = _run(
            tmp_path,
            static_calendar,
            session=None,
            now=datetime(2026, 9, 25, 16, 40, tzinfo=ET),
            fetch=fetch,
            notify=notify,
        )
        assert (res.exit_code, res.status) == (0, "ok")
        assert fetch.calls[0] == date(2026, 9, 16) and fetch.calls[-1] == date(2026, 9, 25)
        doc = json.loads((tmp_path / "state/signals/2026-09-25.json").read_text())
        assert doc["xsmom"]["is_rebalance_day"] is False and doc["xsmom"]["fires"] is False
        after = json.loads((tmp_path / "paper/ohlc-panel.json").read_text())["COST"]
        want = Decimal(after["2026-09-25"]["close"]) / Decimal(after["2026-09-24"]["close"]) - 1
        (beat,) = doc["pead"]
        assert beat["name"] == "COST" and beat["report_date"] == "2026-09-24"
        assert beat["move"] == str(want.quantize(Decimal("0.000001")))
        assert abs(want - Decimal("0.02")) < Decimal("0.00001")
        assert [e["name"] for e in doc["pead_evaluated"]] == ["COST"]
        draft = (tmp_path / "state/card-drafts/2026-09-25-pead-cost.md").read_text()
        assert "PEAD-BIGSURPRISE" in draft and "2026-09-24" in draft
        assert f"+{want * 100:.2f}%" in draft
        assert notify.sent[0][1] == (
            "PEAD beat: 1 name for session 2026-09-25. Draft card(s) ready for review."
        )
        assert not list((tmp_path / "state/card-drafts").glob("*xsmom*"))


class TestFailureModes:
    def test_vendor_lag_exit_3_writes_nothing(self, tmp_path: Path, static_calendar) -> None:
        _write_panel(tmp_path / "paper", static_calendar)
        before = (tmp_path / "paper/ohlc-panel.json").read_bytes()
        res = _run(
            tmp_path,
            static_calendar,
            session=date(2026, 9, 16),
            now=datetime(2026, 9, 16, 16, 40, tzinfo=ET),
            fetch=FakeFetch(tmp_path / "paper", rc=3),
            notify=FakeNotify(),
        )
        assert (res.exit_code, res.status) == (3, "vendor_lag")
        assert not (tmp_path / "state/stages").exists()
        assert not (tmp_path / "state/signals").exists()
        assert (tmp_path / "paper/ohlc-panel.json").read_bytes() == before

    def test_fetch_failure_exit_1(self, tmp_path: Path, static_calendar) -> None:
        _write_panel(tmp_path / "paper", static_calendar)
        res = _run(
            tmp_path,
            static_calendar,
            session=date(2026, 9, 16),
            now=datetime(2026, 9, 16, 16, 40, tzinfo=ET),
            fetch=FakeFetch(tmp_path / "paper", rc=1),
            notify=FakeNotify(),
        )
        assert (res.exit_code, res.status) == (1, "fetch_failed")

    def test_non_session_and_too_early(self, tmp_path: Path, static_calendar) -> None:
        _write_panel(tmp_path / "paper", static_calendar)
        fetch = FakeFetch(tmp_path / "paper")
        res = _run(
            tmp_path,
            static_calendar,
            session=date(2026, 9, 26),
            now=datetime(2026, 9, 26, 16, 40, tzinfo=ET),
            fetch=fetch,
        )
        assert (res.exit_code, res.status) == (0, "not_a_session")
        res = _run(
            tmp_path,
            static_calendar,
            session=date(2026, 9, 16),
            now=datetime(2026, 9, 16, 15, 0, tzinfo=ET),
            fetch=fetch,
        )
        assert (res.exit_code, res.status) == (3, "too_early")
        assert fetch.calls == [] and not (tmp_path / "state").exists()

    def test_gap_too_large_and_missing_panel(self, tmp_path: Path, static_calendar) -> None:
        fetch = FakeFetch(tmp_path / "paper")
        res = _run(
            tmp_path,
            static_calendar,
            session=date(2026, 9, 16),
            now=datetime(2026, 9, 16, 16, 40, tzinfo=ET),
            fetch=fetch,
        )
        assert (res.exit_code, res.status) == (1, "panel_missing")
        _write_panel(tmp_path / "paper", static_calendar)
        res = _run(
            tmp_path,
            static_calendar,
            session=date(2026, 11, 2),
            now=datetime(2026, 11, 2, 16, 40, tzinfo=ET),
            fetch=fetch,
        )
        assert (res.exit_code, res.status) == (1, "gap_too_large")
        assert fetch.calls == []

    def test_panel_still_short_after_fetch(self, tmp_path: Path, static_calendar) -> None:
        _write_panel(tmp_path / "paper", static_calendar)

        def lazy(_session: date) -> tuple[int, str]:
            return 0, "wrote nothing"

        res = _run(
            tmp_path,
            static_calendar,
            session=date(2026, 9, 15),
            now=datetime(2026, 9, 15, 16, 40, tzinfo=ET),
            fetch=lazy,
        )
        assert (res.exit_code, res.status) == (1, "panel_incomplete")

    def test_dry_run_fetches_and_writes_nothing(self, tmp_path: Path, static_calendar) -> None:
        _write_panel(tmp_path / "paper", static_calendar)
        fetch = FakeFetch(tmp_path / "paper")
        res = _run(
            tmp_path,
            static_calendar,
            session=date(2026, 9, 16),
            now=datetime(2026, 9, 16, 16, 40, tzinfo=ET),
            fetch=fetch,
            dry_run=True,
        )
        assert (res.exit_code, res.status) == (0, "dry_run")
        assert "2026-09-15" in res.detail and fetch.calls == []
        assert not (tmp_path / "state").exists()


class TestPushDelivery:
    def test_quiet_hours_hold_then_flush(self, tmp_path: Path, static_calendar) -> None:
        _write_panel(tmp_path / "paper", static_calendar)
        fetch = FakeFetch(tmp_path / "paper")
        notify = FakeNotify()
        # Thu 10-01 is a rebalance day; the 08:40 ET retry the next morning is 06:40 MDT
        res = _run(
            tmp_path,
            static_calendar,
            session=None,
            now=datetime(2026, 10, 2, 8, 40, tzinfo=ET),
            fetch=fetch,
            notify=notify,
            quiet=QUIET,
        )
        assert res.session == date(2026, 10, 1) and res.push == "held_quiet"
        assert notify.sent == []
        assert len(_outbox(tmp_path, "*.json")) == 1
        # next run, outside quiet hours: the owed push goes out first
        res = _run(
            tmp_path,
            static_calendar,
            session=None,
            now=datetime(2026, 10, 2, 16, 40, tzinfo=ET),
            fetch=fetch,
            notify=notify,
            quiet=QUIET,
        )
        assert res.session == date(2026, 10, 2)
        assert [m for _t, m, _p in notify.sent] == [
            "XSMOM rebalance for session 2026-10-01. Draft card(s) ready for review."
        ]
        assert not _outbox(tmp_path, "*.json")
        assert [p.name for p in _outbox(tmp_path, "*.sent")] == ["2026-10-01-eod-equity.sent"]

    def test_failed_push_is_owed_and_unconfigured_is_not(
        self, tmp_path: Path, static_calendar
    ) -> None:
        _write_panel(tmp_path / "paper", static_calendar)
        fetch = FakeFetch(tmp_path / "paper")
        res = _run(
            tmp_path,
            static_calendar,
            session=date(2026, 10, 1),
            now=datetime(2026, 10, 1, 16, 40, tzinfo=ET),
            fetch=fetch,
            notify=FakeNotify(ok=False),
        )
        assert res.push == "failed"
        assert len(_outbox(tmp_path, "*.json")) == 1

        other = tmp_path / "other"
        _write_panel(other / "paper", static_calendar)
        res = _run(
            other,
            static_calendar,
            session=date(2026, 10, 1),
            now=datetime(2026, 10, 1, 16, 40, tzinfo=ET),
            fetch=FakeFetch(other / "paper"),
            notify=None,
        )
        assert res.push == "unconfigured"
        assert not (other / "state/outbox").exists()

    def test_nothing_fires_nothing_pushed(self, tmp_path: Path, static_calendar) -> None:
        _write_panel(tmp_path / "paper", static_calendar)
        notify = FakeNotify()
        res = _run(
            tmp_path,
            static_calendar,
            session=date(2026, 9, 15),
            now=datetime(2026, 9, 15, 16, 40, tzinfo=ET),
            fetch=FakeFetch(tmp_path / "paper"),
            notify=notify,
        )
        assert (res.status, res.push) == ("ok", "none") and notify.sent == []
        assert not (tmp_path / "state/card-drafts").exists()

    # --- P2 (Codex): delivery is crash-idempotent

    def test_crash_after_send_is_never_reposted(self, tmp_path: Path, static_calendar) -> None:
        _write_panel(tmp_path / "paper", static_calendar)
        fetch = FakeFetch(tmp_path / "paper")
        d, at = date(2026, 10, 1), datetime(2026, 10, 1, 16, 40, tzinfo=ET)
        crashing = FakeNotify(crash=True)
        with pytest.raises(KeyboardInterrupt):
            _run(tmp_path, static_calendar, session=d, now=at, fetch=fetch, notify=crashing)
        assert len(crashing.sent) == 1
        assert _outbox(tmp_path, "*.sending")  # the attempt was persisted before sending
        assert not (tmp_path / "state/stages/2026-10-01/eod-equity.done.json").exists()

        notify = FakeNotify()
        res = _run(
            tmp_path,
            static_calendar,
            session=d,
            now=datetime(2026, 10, 1, 20, 40, tzinfo=ET),
            fetch=fetch,
            notify=notify,
        )
        assert (res.status, res.push) == ("ok", "ambiguous_not_resent")
        assert notify.sent == []  # never reposted automatically
        assert res.ambiguous == ("2026-10-01-eod-equity",)
        assert "ambiguous_pushes=1" in res.line()
        log = (tmp_path / "state/push-ambiguous.jsonl").read_text().splitlines()
        assert len(log) == 1 and json.loads(log[0])["key"] == "2026-10-01-eod-equity"
        marker = json.loads((tmp_path / "state/stages/2026-10-01/eod-equity.done.json").read_text())
        assert marker["push"] == "ambiguous_not_resent"

    def test_rerun_after_a_delivered_push_does_not_resend(
        self, tmp_path: Path, static_calendar
    ) -> None:
        _write_panel(tmp_path / "paper", static_calendar)
        fetch = FakeFetch(tmp_path / "paper")
        d = date(2026, 10, 1)
        _run(
            tmp_path,
            static_calendar,
            session=d,
            now=datetime(2026, 10, 1, 16, 40, tzinfo=ET),
            fetch=fetch,
            notify=FakeNotify(),
        )
        # the process died after the receipt but before the stage marker
        (tmp_path / "state/stages/2026-10-01/eod-equity.done.json").unlink()
        notify = FakeNotify()
        res = _run(
            tmp_path,
            static_calendar,
            session=d,
            now=datetime(2026, 10, 1, 20, 40, tzinfo=ET),
            fetch=fetch,
            notify=notify,
        )
        assert (res.status, res.push) == ("ok", "sent") and notify.sent == []

    def test_crash_while_flushing_an_owed_push_is_not_reposted(
        self, tmp_path: Path, static_calendar
    ) -> None:
        _write_panel(tmp_path / "paper", static_calendar)
        fetch = FakeFetch(tmp_path / "paper")
        _run(
            tmp_path,
            static_calendar,
            session=None,
            now=datetime(2026, 10, 2, 8, 40, tzinfo=ET),
            fetch=fetch,
            notify=FakeNotify(),
            quiet=QUIET,
        )  # held (quiet)
        with pytest.raises(KeyboardInterrupt):
            _run(
                tmp_path,
                static_calendar,
                session=None,
                now=datetime(2026, 10, 2, 16, 40, tzinfo=ET),
                fetch=fetch,
                notify=FakeNotify(crash=True),
                quiet=QUIET,
            )
        notify = FakeNotify()
        res = _run(
            tmp_path,
            static_calendar,
            session=None,
            now=datetime(2026, 10, 2, 20, 40, tzinfo=ET),
            fetch=fetch,
            notify=notify,
            quiet=QUIET,
        )
        assert notify.sent == [] and res.ambiguous == ("2026-10-01-eod-equity",)

    # --- P2 (Codex): quiet hours are judged at delivery time, not job start

    def test_quiet_hours_use_the_clock_at_delivery(self, tmp_path: Path, static_calendar) -> None:
        _write_panel(tmp_path / "paper", static_calendar)
        notify = FakeNotify()
        # the job starts 16:40 ET, but the gap fill runs until 02:10 ET (00:10 MDT: quiet)
        res = _run(
            tmp_path,
            static_calendar,
            session=date(2026, 10, 1),
            now=datetime(2026, 10, 1, 16, 40, tzinfo=ET),
            fetch=FakeFetch(tmp_path / "paper"),
            notify=notify,
            quiet=QUIET,
            clock=lambda: datetime(2026, 10, 2, 2, 10, tzinfo=ET),
        )
        assert res.push == "held_quiet" and notify.sent == []

        other = tmp_path / "other"
        _write_panel(other / "paper", static_calendar)
        notify = FakeNotify()
        # starts 08:40 ET (06:40 MDT: quiet), delivers 09:05 ET (07:05 MDT: not quiet)
        res = _run(
            other,
            static_calendar,
            session=None,
            now=datetime(2026, 10, 2, 8, 40, tzinfo=ET),
            fetch=FakeFetch(other / "paper"),
            notify=notify,
            quiet=QUIET,
            clock=lambda: datetime(2026, 10, 2, 9, 5, tzinfo=ET),
        )
        assert res.push == "sent" and len(notify.sent) == 1


class TestInputsAndLocks:
    def test_data_gap_exclusion_is_flagged_in_the_draft(
        self, tmp_path: Path, static_calendar
    ) -> None:
        _write_panel(tmp_path / "paper", static_calendar)
        path = tmp_path / "paper/ohlc-panel.json"
        panel = json.loads(path.read_text())
        del panel["GLD"]["2026-06-01"]  # inside 10-01's 273-session window
        path.write_text(json.dumps(panel, indent=1, sort_keys=True))
        res = _run(
            tmp_path,
            static_calendar,
            session=date(2026, 10, 1),
            now=datetime(2026, 10, 1, 16, 40, tzinfo=ET),
            fetch=FakeFetch(tmp_path / "paper"),
            notify=FakeNotify(),
        )
        assert res.status == "ok"
        doc = json.loads((tmp_path / "state/signals/2026-10-01.json").read_text())
        assert doc["xsmom"]["data_gaps"] == ["GLD"]
        assert doc["xsmom"]["top3"] == ["SQQQ", "TQQQ", "XLF"]
        draft = (tmp_path / "state/card-drafts/2026-10-01-xsmom-top3.md").read_text()
        assert "DATA GAP: GLD" in draft

    # --- P2 (Codex): an unreadable earnings calendar must not seal the stage

    def test_unreadable_earnings_calendar_is_an_error_without_marker(
        self, tmp_path: Path, static_calendar
    ) -> None:
        _write_panel(tmp_path / "paper", static_calendar)
        good = (tmp_path / "paper/earnings-calendar.json").read_text()
        (tmp_path / "paper/earnings-calendar.json").write_text("{torn")
        fetch = FakeFetch(tmp_path / "paper")
        at = datetime(2026, 9, 25, 16, 40, tzinfo=ET)
        res = _run(tmp_path, static_calendar, session=date(2026, 9, 25), now=at, fetch=fetch)
        assert (res.exit_code, res.status) == (1, "earnings_calendar_unreadable")
        assert not (tmp_path / "state/stages/2026-09-25/eod-equity.done.json").exists()
        assert not (tmp_path / "state/signals/2026-09-25.json").exists()
        (tmp_path / "paper/earnings-calendar.json").write_text(good)
        res = _run(tmp_path, static_calendar, session=date(2026, 9, 25), now=at, fetch=fetch)
        assert (res.exit_code, res.status) == (0, "ok")
        assert len(fetch.calls) == 9  # the repaired rerun did not refetch

    # --- P1 (Codex): the desk reads the panel under the writers' lock

    def test_panel_lock_is_shared_with_fetch_ohlc(self, tmp_path: Path, static_calendar) -> None:
        import fcntl

        from tree_options.desk import panel as panel_mod

        assert panel_mod.lock_path(tmp_path / "ohlc-panel.json").name == "ohlc-panel.json.lock"
        _write_panel(tmp_path / "paper", static_calendar)
        lock = tmp_path / "paper" / "ohlc-panel.json.lock"
        with open(lock, "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a fetch_ohlc.py merge in flight
            res = _run(
                tmp_path,
                static_calendar,
                session=date(2026, 9, 16),
                now=datetime(2026, 9, 16, 16, 40, tzinfo=ET),
                fetch=FakeFetch(tmp_path / "paper"),
                lock_timeout_s=0.2,
            )
        assert (res.exit_code, res.status) == (3, "panel_locked")
        assert not (tmp_path / "state/stages").exists()


FAKE_SCRIPT = """\
import json, os, sys
from decimal import Decimal
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}) + "\\n")
rc = int(os.environ.get("FAKE_FETCH_RC", "0"))
if rc == 0:
    path = os.path.join(here, "ohlc-panel.json")
    panel = json.load(open(path))
    d = sys.argv[1]
    for name, bars in panel.items():
        c = str((Decimal(bars[max(bars)]["close"]) * Decimal("1.001")).quantize(Decimal("0.0001")))
        bars[d] = {"open": c, "high": c, "low": c, "close": c, "volume": 1}
    json.dump(panel, open(path, "w"))
print("fake fetch_ohlc", sys.argv[1:])
sys.exit(rc)
"""


class TestSubprocessPath:
    def test_runner_argv_cwd_and_rc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, static_calendar
    ) -> None:
        paper = tmp_path / "paper"
        _write_panel(paper, static_calendar)
        (paper / "fetch_ohlc.py").write_text(FAKE_SCRIPT)
        repo = tmp_path / "repo"
        repo.mkdir()
        log = tmp_path / "state" / "logs" / "eod.log"
        run = eod_equity.subprocess_fetch_runner(paper, repo, log)
        rc, out = run(date(2026, 9, 15))
        assert rc == 0 and "fake fetch_ohlc" in out
        call = json.loads((paper / "calls.jsonl").read_text().splitlines()[0])
        assert call == {"argv": ["2026-09-15"], "cwd": str(repo)}
        assert "fake fetch_ohlc" in log.read_text()
        monkeypatch.setenv("FAKE_FETCH_RC", "3")
        assert run(date(2026, 9, 16))[0] == 3

    def test_cli_end_to_end_through_the_script(
        self, tmp_path: Path, static_calendar, capsys: pytest.CaptureFixture[str]
    ) -> None:
        paper = tmp_path / "paper"
        _write_panel(paper, static_calendar)
        (paper / "fetch_ohlc.py").write_text(FAKE_SCRIPT)
        notify = FakeNotify()
        rc = run_cli(
            ["eod-equity", "--session", "2026-09-16"],
            now=datetime(2026, 9, 16, 16, 40, tzinfo=ET),
            cal=static_calendar,
            notify=notify,
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "eod-equity session=2026-09-16 status=ok" in out
        calls = [json.loads(x)["argv"] for x in (paper / "calls.jsonl").read_text().splitlines()]
        assert calls == [["2026-09-15"], ["2026-09-16"]]
        assert (tmp_path / "state/signals/2026-09-16.json").exists()
        assert sys.executable  # the runner uses the current interpreter

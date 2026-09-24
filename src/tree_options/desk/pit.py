"""Point-in-time loaders for the deal miner (plan D6).

A desk decision for session D is taken after D's close and before the next
session opens (the queue is admitted from 09:50 ET), so its cutoff is the
NEXT session's 09:30 ET open (:func:`decision_cutoff`). Every loader of
:class:`PointInTime` returns only what was knowable at that cutoff, and the
pricing path (``desk.distribution``, ``desk.pricing``) reads the sources
through nothing else. This fixes the flaw in ``discovery/backtest.py``,
which prices history with today's IV. The future-poison test
(``tests/unit/test_desk_pricing_poison.py``) pins every rule below.

What "knowable" means, per source:

* research panel bars (``ohlc-panel.json``, split-adjusted, read-only):
  dated <= D. A later split rescales earlier bars, but ratios (returns, the
  variance proxy) are invariant to it; the desk never reads a price LEVEL
  from the panel (spot is the recorded chain's unadjusted close).
* HAR forecasts: FORECAST-001's pooled log-HAR (:mod:`desk.har`) on the
  panel cut at D, fit on the sealed calendar's report dates <= D (the fit
  reads nothing later: its rows are realized before D's month, so this is
  the validated fit), with the forward event count on the report dates
  known at the cutoff (below). Also the no-event baseline: the same fit
  and regressors with N_earn = 0.
* IV history (the ``desk.ivhist`` document): sessions <= D.
* recorded chains: only the canonical ``chains/<D>/<SYM>.json.gz`` (a
  ``.conflict`` file is a later vendor state; other sessions are never
  read), and only when its header session is D and its ``source_as_of``
  (the vendor's snapshot instant) is at or before the cutoff.
* earnings (:mod:`desk.events` inputs), kept apart by status:
  ``past``: reports dated <= D from the sealed calendar and EDGAR-confirmed
  timing entries (the record; the 8-K acceptance stamp proves when it was
  public); ``past_estimated``: timing-file estimates dated <= D that no
  record backs (never a record); ``upcoming``: dated > D, only timing-file
  entries whose ``fetched_at`` is at or before the cutoff, with their
  status. The sealed calendar's future dates carry no vintage and are NOT
  knowable, unless the caller opts into ``schedule_assumption``
  (FORECAST-001's declared assumption), which marks each such event
  ``sealed-assumed``.
  Declared limitation: the timing file is not a vintage store. An estimate
  the vendor later moved was deleted, and one re-timed or confirmed later
  carries the later ``fetched_at``; a HISTORICAL D therefore sees only the
  estimates that survived unchanged. A live run (D = the latest session,
  file read at decision time) is exact.
* indices (the market lane's stored CSVs): rows dated <= D (CBOE posts D's
  row the evening of D); DTB3 rows dated <= the session BEFORE D (FRED
  posts D's rate the next afternoon, after the cutoff).
"""

from __future__ import annotations

import bisect
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from tree_options.desk import econ_jobs, events, har, indices, paths
from tree_options.desk.events import EarningsEvent
from tree_options.desk.ivhist import history_series
from tree_options.desk.panel import read_panel
from tree_options.desk.sessions import Calendar, first_session_after, previous_session
from tree_options.desk.store import read_chain
from tree_options.desk.universe import CHAIN_UNIVERSE
from tree_options.trex.clock import ET

DECISION_OPEN = time(9, 30)  # the next session's open: the decision cutoff
PANEL_FILE = "ohlc-panel.json"
# sessions a stored index trails D by at the cutoff (FRED posts a day late)
PUBLICATION_LAG_SESSIONS: Mapping[str, int] = {"DTB3": 1}


class NotEvaluable(ValueError):
    """A desk input is missing or not knowable at the decision cutoff: the
    computation is refused with the reason (fail closed)."""


def decision_cutoff(session: date, cal: Calendar) -> datetime:
    """09:30 ET on the first session after ``session`` (aware)."""
    nxt = first_session_after(session, cal)
    if nxt is None:
        raise NotEvaluable(f"the session calendar ends at {session}: no decision cutoff")
    return datetime.combine(nxt, DECISION_OPEN, tzinfo=ET)


@dataclass(frozen=True)
class Sources:
    """The raw inputs as loaded, unfiltered; only :class:`PointInTime`
    decides what a decision may see."""

    panel: Mapping[str, Mapping[str, Mapping[str, Any]]]  # ohlc-panel.json
    sealed: Mapping[str, Sequence[str]]  # earnings-calendar.json
    timing: Mapping[str, Mapping[str, Mapping[str, str]]]  # earnings-timing.json
    iv_history: Mapping[str, Any] | None  # iv-history/vwap_atm.json
    store: Path  # DESK_STORE: chains/ and indices/


def load_sources(*, paper: Path | None = None, store: Path | None = None) -> Sources:
    """Read the live sources (read-only; the panel under its shared lock).
    A missing or unreadable sealed calendar raises: silence must never read
    as "no reports"; a missing timing file or IV history is empty."""
    paper = paper or paths.paper_dir()
    store = store or paths.store_root()
    panel = read_panel(paper / PANEL_FILE)
    try:
        raw = json.loads((paper / events.SEALED_CALENDAR).read_text())
    except (OSError, ValueError) as exc:
        raise NotEvaluable(f"{events.SEALED_CALENDAR} unreadable ({type(exc).__name__})") from exc
    if not isinstance(raw, dict):
        raise NotEvaluable(f"{events.SEALED_CALENDAR} is not an object")
    sealed = {str(k): [str(d) for d in v] for k, v in raw.items() if isinstance(v, list)}
    timing = events.load_timing(paper / events.TIMING_FILE)
    history_path = econ_jobs.iv_history_dir(store) / econ_jobs.HISTORY_FILE
    try:
        history = json.loads(history_path.read_text())
    except (OSError, ValueError):
        history = None
    return Sources(
        panel=panel,
        sealed=sealed,
        timing=timing,
        iv_history=history if isinstance(history, dict) else None,
        store=store,
    )


@dataclass(frozen=True)
class EarningsPit:
    """Report dates of one name as known at the cutoff (see the module
    docstring for the rules)."""

    name: str
    reporter: bool
    past: tuple[EarningsEvent, ...]  # dated <= D: sealed or confirmed (the record)
    past_estimated: tuple[EarningsEvent, ...]  # dated <= D: an estimate only
    upcoming: tuple[EarningsEvent, ...]  # dated > D, known at the cutoff
    schedule_assumption: bool

    def all_known(self) -> tuple[EarningsEvent, ...]:
        return tuple(
            sorted((*self.past, *self.past_estimated, *self.upcoming), key=lambda e: e.date)
        )

    def known_dates(self) -> list[str]:
        """Every known report date (ISO), for schedule-completeness checks."""
        return sorted({e.date.isoformat() for e in self.all_known()})


@dataclass(frozen=True)
class HarPit:
    """FORECAST-001's HAR at D for one name."""

    name: str
    session: date
    h: int
    forecast: float  # variance over (D, D+h], known events counted
    forecast_ex_events: float  # the same fit and regressors with no event
    n_earn: float
    schedule: str  # n/a | complete | incomplete | unavailable
    schedule_reason: str
    fit_through: date
    be: float | None


def _instant(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else None  # a naive stamp proves nothing


class PointInTime:
    """The sources as a decision for ``session`` could know them."""

    def __init__(
        self,
        session: date,
        cal: Calendar,
        sources: Sources,
        *,
        cutoff: datetime | None = None,
        har_names: Sequence[str] | None = None,
        schedule_assumption: bool = False,
    ) -> None:
        if not cal.is_session(session):
            raise NotEvaluable(f"{session} is not an NYSE session")
        self.session = session
        self.cal = cal
        self.sources = sources
        self.cutoff = cutoff if cutoff is not None else decision_cutoff(session, cal)
        if self.cutoff.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware")
        names = har_names if har_names is not None else CHAIN_UNIVERSE
        self.har_names: tuple[str, ...] = tuple(n for n in names if n in sources.panel)
        self.schedule_assumption = schedule_assumption
        self._iso = session.isoformat()
        self._har: dict[tuple[int, int], dict[str, HarPit]] = {}

    # ------------------------------------------------------------ prices

    def bars(self, name: str) -> dict[str, Mapping[str, Any]]:
        """The name's panel bars dated <= D."""
        raw = self.sources.panel.get(name) or {}
        return {d: b for d, b in raw.items() if d <= self._iso}

    # ---------------------------------------------------------- earnings

    def earnings(self, name: str) -> EarningsPit:
        reporter = name not in har.ETF_NAMES
        if not reporter:
            return EarningsPit(name, False, (), (), (), self.schedule_assumption)
        sealed = self.sources.sealed.get(name, ())
        timing = self.sources.timing.get(name, {})
        past: dict[str, EarningsEvent] = {}
        estimated: dict[str, EarningsEvent] = {}
        upcoming: dict[str, EarningsEvent] = {}
        for d in sealed:
            if d <= self._iso:
                e = timing.get(d)
                src = events.SEALED_CALENDAR + (
                    f" (sealed); timing: {e['source']}" if e else " (sealed)"
                )
                timed = e["timing"] if e else "unknown"
                past[d] = EarningsEvent(date.fromisoformat(d), timed, "sealed", src, False)
        for d, e in timing.items():
            status = e.get("status")
            if d <= self._iso:
                if d in past:
                    continue
                if status == "confirmed":
                    past[d] = EarningsEvent(
                        date.fromisoformat(d), e["timing"], "confirmed", e["source"], False
                    )
                elif self._fetched_by_cutoff(e):
                    estimated[d] = EarningsEvent(
                        date.fromisoformat(d), e["timing"], "estimated", e["source"], True
                    )
            elif self._fetched_by_cutoff(e):
                upcoming[d] = EarningsEvent(
                    date.fromisoformat(d),
                    e["timing"],
                    str(status),
                    e["source"],
                    status != "confirmed",
                )
        if self.schedule_assumption:
            for d in sealed:
                if d > self._iso and d not in upcoming:
                    upcoming[d] = EarningsEvent(
                        date.fromisoformat(d),
                        "unknown",
                        "sealed-assumed",
                        f"{events.SEALED_CALENDAR} (schedule assumption: no vintage)",
                        True,
                    )

        def ordered(x: dict[str, EarningsEvent]) -> tuple[EarningsEvent, ...]:
            return tuple(x[d] for d in sorted(x))

        return EarningsPit(
            name,
            True,
            ordered(past),
            ordered(estimated),
            ordered(upcoming),
            self.schedule_assumption,
        )

    def _fetched_by_cutoff(self, entry: Mapping[str, str]) -> bool:
        ts = _instant(entry.get("fetched_at"))
        return ts is not None and ts <= self.cutoff

    # -------------------------------------------------------- IV history

    def iv_history(self, name: str) -> dict[date, tuple[float, str]]:
        """{session: (iv30, method)} of the evaluable sessions <= D."""
        if self.sources.iv_history is None:
            return {}
        return {
            d: v
            for d, v in history_series(self.sources.iv_history, name).items()
            if d <= self.session
        }

    # ------------------------------------------------------------ chains

    def chain(self, sym: str) -> dict[str, Any]:
        """D's canonical recorded chain; NotEvaluable when absent, labeled
        for another session, or snapshotted after the cutoff."""
        path = self.sources.store / "chains" / self._iso / f"{sym}.json.gz"
        if not path.exists():
            raise NotEvaluable(f"{sym}: no recorded chain for {self._iso}")
        try:
            doc = read_chain(path)
        except (OSError, ValueError) as exc:
            raise NotEvaluable(f"{sym}: recorded chain unreadable ({type(exc).__name__})") from exc
        header = doc["header"]
        if header.get("session") != self._iso:
            raise NotEvaluable(
                f"{sym}: the chain in {self._iso}/ describes session {header.get('session')}"
            )
        as_of = _instant(header.get("source_as_of"))
        if as_of is None:
            raise NotEvaluable(f"{sym}: chain has no aware source_as_of")
        if as_of > self.cutoff:
            raise NotEvaluable(
                f"{sym}: vendor snapshot {as_of.isoformat()} is after the decision cutoff "
                f"{self.cutoff.isoformat()}"
            )
        return doc

    # ----------------------------------------------------------- indices

    def index(self, name: str) -> dict[date, str]:
        """A stored index series (close values verbatim) as knowable at the
        cutoff: rows dated <= D, less the source's publication lag."""
        path = self.sources.store / "indices" / f"{name}.csv"
        try:
            rows = indices.read_store(path)
        except (OSError, ValueError):
            return {}
        limit: date | None = self.session
        for _ in range(PUBLICATION_LAG_SESSIONS.get(name, 0)):
            limit = previous_session(limit, self.cal) if limit is not None else None
        if limit is None:
            return {}
        cut = limit.isoformat()
        return {
            date.fromisoformat(d): close
            for d, _o, _h, _lo, close in rows
            if d <= cut and close != ""
        }

    def rate(self) -> float | None:
        """DTB3 as a decimal rate (percent / 100): the latest knowable row."""
        series = self.index("DTB3")
        if not series:
            return None
        return float(series[max(series)]) / 100.0

    # --------------------------------------------------------------- HAR

    def har(self, h: int = 20, *, min_event_rows: int = har.MIN_EVENT_ROWS) -> dict[str, HarPit]:
        """Every modelable name's HAR at D (one pooled fit; cached). Mirrors
        ``har.forecasts_at`` step for step (a test pins the equality) and
        adds the no-event baseline."""
        key = (h, min_event_rows)
        if key not in self._har:
            self._har[key] = self._fit_har(h, min_event_rows)
        return self._har[key]

    def _fit_har(self, h: int, min_event_rows: int) -> dict[str, HarPit]:
        panel = {n: self.bars(n) for n in self.har_names}
        sealed = {
            n: [d for d in self.sources.sealed.get(n, ()) if d <= self._iso] for n in self.har_names
        }
        try:
            data = har.build_har_data(panel, sealed, self.cal, self.har_names, through=self.session)
            t = data.index(self.session)
        except (KeyError, ValueError):
            return {}
        m0 = bisect.bisect_left(data.sessions, date(self.session.year, self.session.month, 1))
        if m0 < 1:
            return {}
        fit = har.fit_har(har.build_rows(data, h), through=m0 - 1, min_event_rows=min_event_rows)
        if fit is None:
            return {}
        try:
            end: date | None = self.cal.nth_after(self.session, h)
        except (LookupError, ValueError, RuntimeError):  # past the calendar end
            end = None
        out: dict[str, HarPit] = {}
        for name, ns in data.names.items():
            x = har.regressors(ns, t)
            if x is None:
                continue
            status, reason = "n/a", ""
            n_value = har.n_earn_used(ns, t, h, data.coverage_idx)
            if ns.reporter:
                dates = self.earnings(name).known_dates()
                if end is None:
                    status, reason = "unavailable", "the session calendar ends inside the horizon"
                else:
                    status, reason = har.schedule_status(dates, self.session, end, self.cal)
                fwd = har.NameSeries.from_proxy(
                    name,
                    ns.v,
                    reporter=True,
                    pairs=har.event_pairs(dates, self.cal, data.sessions[0]),
                )
                n_value = float(har.n_earn(fwd, t, h))
            f = har.predict(fit, name, x, n_value)
            f0 = har.predict(fit, name, x, 0.0)
            if f is None or f0 is None:
                continue
            out[name] = HarPit(
                name=name,
                session=self.session,
                h=h,
                forecast=f,
                forecast_ex_events=f0,
                n_earn=n_value,
                schedule=status,
                schedule_reason=reason,
                fit_through=data.sessions[m0 - 1],
                be=fit.be,
            )
        return out

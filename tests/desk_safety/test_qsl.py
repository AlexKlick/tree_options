"""QSL queue-builder tests: picker, gate oracle, determinism, lane acceptance.

Everything here is PAPER research: no broker, no orders, no live state. The
chains are synthetic Black-Scholes documents so the gate arithmetic can be
checked against numbers computed by hand in the test (never through the
builder's own helpers), and the lane acceptance runs the REAL
``desk.contracts`` + ``desk.shadows`` code against the builder's output.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tree_options.desk import qsl
from tree_options.desk.contracts import parse_deal, parse_queue
from tree_options.desk.evidence import EvidenceStore
from tree_options.desk.sessions import cutoff_instant, previous_session
from tree_options.desk.store import encode_document
from tree_options.synth_options.greeks import bs_abs_delta, bs_price
from tree_options.time.calendar import StaticSessionCalendar
from tree_options.trex.clock import ET

ROOT = Path(__file__).resolve().parents[2]
CAL_JSON = ROOT / "data" / "calendar" / "trex" / "nyse_sessions_2018_01_02_2028_12_29.json"
CAL_SHA = CAL_JSON.with_suffix(".sha256")
IV = 0.32
SPOT = 100.0
RATE = 0.04
HALF_SPREAD = 0.02  # a $0.04 wide book: tight, gate-passing


def calendar() -> StaticSessionCalendar:
    return StaticSessionCalendar(CAL_JSON, CAL_SHA)


@dataclass
class QslWorld:
    cal: StaticSessionCalendar
    session: date
    entry: date
    root: Path

    @property
    def store(self) -> Path:
        return self.root / "store"

    @property
    def queues(self) -> Path:
        return self.root / "queue-qsl"

    @property
    def db(self) -> Path:
        return self.root / "evidence" / "qsl.sqlite3"

    def chain_path(self, name: str, day: date) -> Path:
        return self.store / "chains" / day.isoformat() / f"{name}.json.gz"

    def write_chain(
        self,
        day: date,
        *,
        name: str = "AAPL",
        spot: float = SPOT,
        half_spread: float = HALF_SPREAD,
        expiry_dtes: tuple[int, ...] = (8, 22),
        bid_size: int = 25,
        ask_size: int = 25,
        oi: int = 500,
    ) -> dict[str, Any]:
        """A synthetic desk-chain/1 document priced by Black-Scholes at IV.

        bid/ask = mid -/+ half_spread, so the IV solved at the mid round-trips
        the generatrix and every analytic quantity is knowable in the test
        without touching the builder. Expiry DATES anchor on the queue's
        session (the same absolute contracts at every later snapshot)."""
        cols: dict[str, list[Any]] = {
            k: []
            for k in (
                "occ",
                "exp",
                "strike",
                "right",
                "bid",
                "bid_size",
                "ask",
                "ask_size",
                "last",
                "last_time",
                "iv",
                "volume",
                "oi",
                "delta",
                "gamma",
                "theta",
                "vega",
                "rho",
                "theo",
            )
        }
        for dte in sorted(set(d for d in expiry_dtes if d >= 1)):
            exp = self.session + timedelta(days=dte)
            for right in ("C", "P"):
                for k in range(70, 131):
                    mid = bs_price(
                        spot=spot,
                        strike=k,
                        dte_calendar_days=dte,
                        iv=IV,
                        risk_free=RATE,
                        dividend_yield=0.0,
                        call_put=right,
                    )
                    bid, ask = round(mid - half_spread, 2), round(mid + half_spread, 2)
                    if bid < 0.01:
                        bid = 0.0  # a real unbidgable wing
                    for col, val in (
                        ("occ", f"X{int(k * 1000):08d}{'C' if right == 'C' else 'P'}"),
                        ("exp", exp.isoformat()),
                        ("strike", k),
                        ("right", right),
                        ("bid", bid),
                        ("bid_size", bid_size),
                        ("ask", ask),
                        ("ask_size", ask_size),
                        ("last", round(mid, 2)),
                        ("last_time", f"{day.isoformat()}T17:46:00-04:00"),
                        ("iv", IV * 100),
                        ("volume", 100),
                        ("oi", oi),
                        ("delta", 0.0),
                        ("gamma", 0.0),
                        ("theta", 0.0),
                        ("vega", 0.0),
                        ("rho", 0.0),
                        ("theo", round(mid, 2)),
                    ):
                        cols[col].append(val)
        at = datetime.combine(day, time(16, 15), ET)
        doc = {
            "header": {
                "schema": "desk-chain/1",
                "session": day.isoformat(),
                "underlying": name,
                "source_as_of": at.isoformat(),
                "fetched_at": at.isoformat(),
                "raw_sha256": "e" * 64,
                "underlying_quote": {
                    "close": spot,
                    "bid": spot - 0.02,
                    "ask": spot + 0.02,
                    "last_trade_time": at.isoformat(),
                },
                "n": len(cols["occ"]),
            },
            "columns": cols,
        }
        p = self.chain_path(name, day)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(encode_document(doc))
        return doc

    def write_dtb3(self, rows: tuple[tuple[str, str], ...]) -> None:
        d = self.store / "indices"
        d.mkdir(parents=True, exist_ok=True)
        (d / "DTB3.csv").write_text(
            "date,open,high,low,close\n" + "".join(f"{day},,,,{close}\n" for day, close in rows)
        )

    def build(self, day: date | None = None, **kw: Any) -> dict[str, Any]:
        chains = qsl.chain_files(self.store, day or self.session)
        return qsl.build_queue(
            day or self.session, chains=chains, cal=self.cal, rate=kw.pop("rate", RATE), **kw
        )

    def write_queue(self, payload: dict[str, Any]) -> Path:
        self.queues.mkdir(parents=True, exist_ok=True)
        p = self.queues / f"{payload['session']}.json"
        p.write_bytes(qsl.encode(payload))
        return p


@pytest.fixture()
def world(tmp_path):
    cal = calendar()
    sessions = cal.sessions()
    idx = next(i for i, s in enumerate(sessions) if s.isoformat() == "2026-09-22")
    w = QslWorld(cal, sessions[idx], sessions[idx + 1], tmp_path)
    w.write_chain(w.session)
    w.write_dtb3(((previous_session(w.session, cal).isoformat(), "4.08"),))
    return w


def oracle_pick(expiry_dte: int, right: str, spot: float = SPOT) -> float:
    """The strike whose ANALYTIC |delta| is nearest 0.30 (ties -> lower)."""
    best = None
    for k in range(70, 131):
        d = bs_abs_delta(
            spot=spot,
            strike=k,
            dte_calendar_days=expiry_dte,
            iv=IV,
            risk_free=RATE,
            dividend_yield=0.0,
            call_put="C" if right == "C" else "P",
        )
        key = (abs(d - 0.30), k)
        if best is None or key < best[0]:
            best = (key, k)
    return float(best[1])


def _shadows(world: QslWorld, as_of: date, *, now: datetime | None = None) -> dict[str, Any]:
    from tree_options.desk import shadows

    return shadows.update_shadows(
        session=as_of,
        now=now or cutoff_instant(as_of),
        cal=world.cal,
        database=world.db,
        store_root=world.store,
        queue_dir=world.queues,
    )


# ------------------------------------------------------------------ picker


def test_picker_nearest_delta_nearest_expiry_nearest_width(world):
    payload = world.build()
    assert payload["qsl"]["family_pass"] == {"put_credit": 1, "call_debit": 1}
    by_family = {r["qsl"]["family"]: r for r in payload["admissible"]}
    for family, right in (("put_credit", "P"), ("call_debit", "C")):
        row = by_family[family]
        k_short = oracle_pick(8, right)  # DTE 8 is the nearest in [7, 35]
        assert Decimal(row["legs"][0]["strike"]) == Decimal(str(k_short))
        assert Decimal(row["legs"][1]["strike"]) == Decimal(str(k_short - 5))
        assert row["qsl"]["dte"] == 8
        assert row["entry_session"] == world.entry.isoformat()
        assert row["qsl"]["family"] == family


def test_expiry_out_of_range_is_not_picked(world):
    world.write_chain(world.session, expiry_dtes=(3, 22))  # 3 DTE is out of range
    payload = world.build()
    assert payload["admissible"]
    assert all(r["qsl"]["dte"] == 22 for r in payload["admissible"])


def test_deadline_is_the_last_session_the_lane_permits(world):
    payload = world.build()
    sessions = world.cal.sessions()
    for row in payload["admissible"]:
        expiry = date.fromisoformat(row["qsl"]["expiry"])
        deadline = date.fromisoformat(row["exit_deadline"])
        before = sessions.index(expiry)
        assert deadline == sessions[before - 2]
        assert deadline < previous_session(expiry, world.cal)  # parse_deal's hard rule


# ----------------------------------------------------- gate: hand oracle


def test_gate_arithmetic_matches_a_hand_computed_oracle(world):
    """mid, crossing and penalty recomputed with plain Decimal math in the
    test (never the builder's helpers) and compared to the recorded row."""
    payload = world.build()
    by_family = {r["qsl"]["family"]: r for r in payload["admissible"]}
    doc = json.loads(gzip.decompress(world.chain_path("AAPL", world.session).read_bytes()))
    exp = (world.session + timedelta(days=8)).isoformat()
    for family, right in (("put_credit", "P"), ("call_debit", "C")):
        row = by_family[family]
        k_short, k_long = oracle_pick(8, right), oracle_pick(8, right) - 5
        quotes = {}
        for i, strike in enumerate(doc["columns"]["strike"]):
            if doc["columns"]["right"][i] == right and doc["columns"]["exp"][i] == exp:
                quotes[strike] = (
                    Decimal(str(doc["columns"]["bid"][i])),
                    Decimal(str(doc["columns"]["ask"][i])),
                )
        bid_s, ask_s = quotes[k_short]
        bid_l, ask_l = quotes[k_long]
        if family == "put_credit":  # sell short at bid, wing at ask
            mid, cross = (bid_s + ask_s) / 2 - (bid_l + ask_l) / 2, bid_s - ask_l
            penalty = mid - cross
        else:  # buy long at ask, wing at bid
            mid, cross = (bid_l + ask_l) / 2 - (bid_s + ask_s) / 2, ask_l - bid_s
            penalty = cross - mid
        assert Decimal(row["ref_mid"]) == mid
        assert Decimal(row["fill"]) == cross
        assert Decimal(row["qsl"]["crossing_penalty"]) == penalty
        assert Decimal(row["qsl"]["crossing_penalty_frac"]) == round(penalty / mid, 4)
        assert penalty <= Decimal("0.10") * mid


def test_limit_is_the_first_cent_beyond_the_crossing_fill(world):
    payload = world.build()
    for row in payload["admissible"]:
        fill, limit = Decimal(row["fill"]), Decimal(row["limit"])
        cent = Decimal("0.01")
        if row["qsl"]["family"] == "put_credit":  # the floor below the credit
            assert limit == fill - cent and limit <= fill
        else:  # the cap above the debit
            assert limit == fill + cent and limit >= fill
        assert Decimal(row["width"]) > limit > 0


def test_put_family_fails_theta_on_a_moderately_wide_book(world):
    world.write_chain(world.session, half_spread=0.05)  # penalty 0.10 > 0.10 x mid 0.89
    payload = world.build()
    assert payload["qsl"]["family_pass"] == {"put_credit": 0, "call_debit": 1}
    assert payload["qsl"]["refused"]["put_credit:crossing_penalty_above_theta"] == 1


def test_call_family_fails_theta_on_a_very_wide_book(world):
    world.write_chain(world.session, half_spread=0.25)  # penalty 0.50 > 0.10 x mid 2.30
    payload = world.build()
    assert payload["admissible"] == []
    assert payload["qsl"]["refused"]["call_debit:crossing_penalty_above_theta"] == 1


@pytest.mark.parametrize(
    "bid_size,ask_size,oi,clause",
    [
        (25, 2, 500, "touch_size_below_floor"),  # ask touch too thin
        (2, 25, 500, "touch_size_below_floor"),  # bid touch too thin
        (25, 25, 40, "open_interest_below_floor"),  # below rails' 100
    ],
)
def test_size_and_oi_floors_refuse(world, bid_size, ask_size, oi, clause):
    world.write_chain(world.session, bid_size=bid_size, ask_size=ask_size, oi=oi)
    payload = world.build()
    assert payload["admissible"] == []
    assert payload["qsl"]["refused"][f"put_credit:{clause}"] == 1


# ------------------------------------------------------------- determinism


def test_same_inputs_give_byte_identical_queues(world):
    assert world.build() == world.build()
    assert qsl.encode(world.build()) == qsl.encode(world.build())


def test_wall_clock_never_enters_the_payload(world, monkeypatch):
    monkeypatch.setenv("TREX_DESK_STATE", str(world.root / "state"))
    monkeypatch.setenv("DESK_STORE", str(world.store))
    a = qsl.run_qsl(
        session=world.session, now=cutoff_instant(world.entry), cal=world.cal, dry_run=True
    )
    later = datetime.combine(world.entry + timedelta(days=3), time(23), ET)
    b = qsl.run_qsl(session=world.session, now=later, cal=world.cal, dry_run=True)
    assert a.payload == b.payload
    assert "done_at" not in json.dumps(a.payload)


def test_ranking_is_deterministic_and_dense(world):
    world.write_chain(world.session, name="SPY")
    payload = world.build()
    ranks = [r["rank"] for r in payload["admissible"]]
    assert ranks == list(range(1, len(ranks) + 1))
    keys = [(r["underlying"], r["row"]) for r in payload["admissible"]]
    assert keys == sorted(keys)


# --------------------------------------------------------- lane acceptance


def test_queue_parses_and_lanes_forward_into_episodes_and_marks(world):
    sessions = world.cal.sessions()
    expiry = world.session + timedelta(days=8)
    deadline = sessions[sessions.index(expiry) - 2]
    world.write_chain(world.entry)  # the entry-session snapshot
    world.write_chain(deadline)  # the resolution snapshot
    payload = world.build()
    world.write_queue(payload)
    doc = parse_queue(
        json.loads(world.queues.joinpath(f"{world.session.isoformat()}.json").read_text()),
        world.cal,
        expected_session=world.session,
    )
    deals = [parse_deal(r, doc, world.cal) for r in doc.rows]
    assert len(deals) == 2  # put_credit + call_debit
    # adopt inside the entry window: the forward-registration path
    morning = datetime.combine(world.entry, time(10), ET)
    assert _shadows(world, world.session, now=morning)["episodes_created"] == 2
    with EvidenceStore(world.db, readonly=True) as store:
        assert {e["registration_timing"] for e in store.all("episode")} == {
            "before_entry_window_end"
        }
    report = _shadows(world, deadline)  # the resolution run
    assert report["episodes_created"] == 0  # adoption happened in the window
    assert report["marks_created"] >= 2  # entry + deadline marks per episode
    # no queue file for the deadline session itself: never stops mark work
    assert report["status"] == "partial_missing_queue"
    with EvidenceStore(world.db, readonly=True) as store:
        terminal = [m for m in store.all("mark") if m["session"] == deadline.isoformat()]
        assert len(terminal) == 2
        for m in terminal:
            assert m["execution_observed"] is False
            assert Decimal(m["modeled_fees_dollars"]) == Decimal("2.60")
            assert Decimal(m["modeled_net_pnl_dollars"]) == Decimal(
                m["modeled_gross_pnl_dollars"]
            ) - Decimal("2.60")


def test_terminal_mark_net_matches_a_hand_computed_crossing(world):
    """The deadline mark's gross, recomputed by hand from the queue's recorded
    entry fill and the deadline snapshot's own legs (never the lane's helpers
    beyond the raw columns)."""
    sessions = world.cal.sessions()
    expiry = world.session + timedelta(days=8)
    deadline = sessions[sessions.index(expiry) - 2]
    world.write_chain(deadline, spot=SPOT * 1.06)  # spot moved: a real P&L
    payload = world.build()
    world.write_queue(payload)
    _shadows(world, deadline)
    doc = json.loads(gzip.decompress(world.chain_path("AAPL", deadline).read_bytes()))
    with EvidenceStore(world.db, readonly=True) as store:
        for mark in (m for m in store.all("mark") if m["session"] == deadline.isoformat()):
            row = next(r for r in payload["admissible"] if r["deal_id"] == mark["deal_id"])
            quotes = {}
            for i, strike in enumerate(doc["columns"]["strike"]):
                if doc["columns"]["exp"][i] == row["qsl"]["expiry"]:
                    quotes[(doc["columns"]["right"][i], strike)] = (
                        Decimal(str(doc["columns"]["bid"][i])),
                        Decimal(str(doc["columns"]["ask"][i])),
                    )
            right = row["legs"][0]["right"]
            short_q = quotes[(right, int(Decimal(row["legs"][0]["strike"])))]
            long_q = quotes[(right, int(Decimal(row["legs"][1]["strike"])))]
            # the lane's CLOSE-side crossing: a held BUY leg is sold at its bid,
            # a held SELL leg is bought back at its ask (shadows.package_prices)
            if row["qsl"]["family"] == "put_credit":
                realistic = short_q[1] - long_q[0]  # ask(short) - bid(long): buy-back cost
                gross = Decimal(row["fill"]) - realistic
            else:
                realistic = long_q[0] - short_q[1]  # bid(long) - ask(short): close proceeds
                gross = realistic - Decimal(row["fill"])
            assert Decimal(mark["realistic"]) == realistic
            assert Decimal(mark["modeled_gross_pnl_dollars"]) == gross * 100


def test_two_sessions_one_week_one_name_one_representative(world):
    """The lane's cohort rule: one usable representative per name/row/week."""
    second = world.cal.sessions()[world.cal.sessions().index(world.session) + 2]
    world.write_chain(second, spot=SPOT * 1.01)
    world.write_queue(world.build(world.session))
    world.write_queue(world.build(second))
    sessions = world.cal.sessions()
    expiry = second + timedelta(days=8)
    deadline = sessions[sessions.index(expiry) - 2]
    world.write_chain(deadline)
    report = _shadows(world, deadline)
    assert report["episodes_created"] == 2  # one per FAMILY, not per session
    with EvidenceStore(world.db, readonly=True) as store:
        assert len({e["cohort"] for e in store.all("episode")}) == 2
        assert len(store.all("candidate")) == 4  # every row stays visible


def test_missing_deadline_chain_is_censored(world):
    world.write_queue(world.build())
    sessions = world.cal.sessions()
    expiry = world.session + timedelta(days=8)
    deadline = sessions[sessions.index(expiry) - 2]
    report = _shadows(world, deadline)  # no deadline chain written
    with EvidenceStore(world.db, readonly=True) as store:
        assert report["episodes_created"] == 2
        assert all(m["session"] != deadline.isoformat() for m in store.all("mark"))
        assert any(
            q["code"] == "chain_missing" and q["session"] == deadline.isoformat()
            for q in store.all("quality")
        )


# ------------------------------------------------------------- separation


def test_the_qsl_lane_never_touches_the_miner_queue_or_live_db(monkeypatch, tmp_path):
    from tree_options.desk import paths, shadows

    monkeypatch.delenv("TREX_DESK_QSL_QUEUE", raising=False)
    monkeypatch.delenv("TREX_DESK_QSL_DB", raising=False)
    monkeypatch.setenv("TREX_DESK_STATE", str(tmp_path))
    assert qsl.queue_dir() != paths.queue_dir()
    assert qsl.queue_dir() == tmp_path / "queue-qsl"
    assert qsl.database_path() != shadows.database_path()
    assert qsl.database_path() == tmp_path / "evidence" / "qsl.sqlite3"


def test_run_qsl_reads_rate_with_the_desk_lag_rule(world, monkeypatch):
    monkeypatch.setenv("TREX_DESK_STATE", str(world.root / "state"))
    monkeypatch.setenv("DESK_STORE", str(world.store))
    result = qsl.run_qsl(session=world.session, now=cutoff_instant(world.entry), cal=world.cal)
    assert result.exit_code == 0 and result.status == "written"
    assert result.path == world.root / "state" / "queue-qsl" / f"{world.session.isoformat()}.json"
    payload = json.loads(result.path.read_text())
    assert payload["inputs"]["dtb3_rate"] == pytest.approx(0.0408)
    again = qsl.run_qsl(session=world.session, now=cutoff_instant(world.entry), cal=world.cal)
    assert again.status == "already_done" and again.exit_code == 0


def test_run_qsl_not_ready_without_chains(world, monkeypatch):
    monkeypatch.setenv("TREX_DESK_STATE", str(world.root / "state"))
    monkeypatch.setenv("DESK_STORE", str(world.root / "empty-store"))
    result = qsl.run_qsl(session=world.session, now=cutoff_instant(world.entry), cal=world.cal)
    assert result.exit_code == 3 and result.status == "not_ready"


# -------------------------------------------------------- pre-registration


def test_preregistration_sha_is_pinned():
    doc = (ROOT / qsl.PREREG_DOC).read_bytes()
    assert hashlib.sha256(doc).hexdigest() == qsl.PREREG_SHA256
    sidecar = (ROOT / qsl.PREREG_DOC).with_suffix(".md.sha256")
    assert sidecar.exists()
    assert sidecar.read_text().split()[0] == qsl.PREREG_SHA256


def test_prereg_id_is_stamped_on_every_row(world):
    payload = world.build()
    assert payload["qsl"]["prereg_sha256"] == qsl.PREREG_SHA256
    for row in payload["admissible"]:
        assert row["qsl"]["prereg_id"] == qsl.PREREG_ID
        assert row["qsl"]["prereg_sha256"] == qsl.PREREG_SHA256


@pytest.mark.parametrize(
    "corruption", ["late_fetched", "future_source", "naive", "missing", "wrong_session"]
)
def test_chain_available_by_decision_is_required(world, corruption):
    doc = world.write_chain(world.session)
    header = doc["header"]
    if corruption == "late_fetched":
        header["fetched_at"] = datetime.combine(world.entry, time(12), ET).isoformat()
    elif corruption == "future_source":
        header["source_as_of"] = datetime.combine(world.entry, time(12), ET).isoformat()
    elif corruption == "naive":
        header["fetched_at"] = f"{world.session}T17:00:00"
    elif corruption == "missing":
        header.pop("fetched_at")
    else:
        header["session"] = world.entry.isoformat()
    world.chain_path("AAPL", world.session).write_bytes(encode_document(doc))
    payload = world.build()
    assert payload["admissible"] == []
    assert payload["qsl"]["refused"]["chain_availability_invalid"] == 1


def test_qsl_rows_are_simulated_and_have_no_execution_authority(world):
    payload = world.build()
    assert payload["qsl"]["evidence_kind"] == "SIMULATED EXECUTION"
    assert payload["qsl"]["execution_authorized"] is False
    assert payload["qsl"]["exact_external_economics"] is False
    for row in payload["admissible"]:
        assert row["qsl"]["evidence_kind"] == "SIMULATED EXECUTION"
        assert row["qsl"]["execution_authorized"] is False
        assert "modeled shadow fill" in row["rationale"]


def test_future_capture_is_not_available_before_runtime_observation(world, monkeypatch):
    doc = world.write_chain(world.session)
    doc["header"]["fetched_at"] = datetime.combine(world.session, time(18), ET).isoformat()
    world.chain_path("AAPL", world.session).write_bytes(encode_document(doc))
    result = qsl.run_qsl(
        session=world.session,
        now=cutoff_instant(world.session),
        cal=world.cal,
        store=world.store,
        dry_run=True,
    )
    assert result.payload["admissible"] == []
    assert result.payload["qsl"]["refused"]["chain_availability_invalid"] == 1


def test_canonical_chain_document_hash_changes_even_when_declared_raw_hash_does_not(world):
    before = world.build()
    doc = world.write_chain(world.session)
    doc["columns"]["theo"][0] += 0.1
    world.chain_path("AAPL", world.session).write_bytes(encode_document(doc))
    after = world.build()
    assert before["inputs"]["chains_raw_sha256"] == after["inputs"]["chains_raw_sha256"]
    assert before["inputs"]["chain_document_sha256"] != after["inputs"]["chain_document_sha256"]


@pytest.mark.parametrize(
    "close", ["malformed", "NaN", "Infinity", "-Infinity", "10001", "-10001", ""]
)
def test_invalid_latest_dtb3_rate_refuses_without_stale_fallback(world, monkeypatch, close):
    monkeypatch.setenv("TREX_DESK_STATE", str(world.root / "state"))
    monkeypatch.setenv("DESK_STORE", str(world.store))
    latest = previous_session(world.session, world.cal)
    older = previous_session(latest, world.cal)
    world.write_dtb3(((older.isoformat(), "4.08"), (latest.isoformat(), close)))
    result = qsl.run_qsl(session=world.session, now=cutoff_instant(world.entry), cal=world.cal)
    assert result.status == "not_ready" and result.exit_code == 3
    assert "invalid DTB3" in result.detail
    assert not world.queues.exists()


def test_qsl_payload_binds_real_engine_source_and_dependency_bytes(world, tmp_path, monkeypatch):
    payload = world.build()
    assert payload["engine"]["sha256"] == qsl.qsl_engine_manifest()["sha256"]
    original = qsl.qsl_engine_manifest()
    own = tmp_path / "qsl-source"
    own.write_bytes(b"first QSL source")
    monkeypatch.setattr(qsl, "__file__", str(own))
    first = qsl.qsl_engine_manifest()
    own.write_bytes(b"changed QSL source")
    second = qsl.qsl_engine_manifest()
    assert first["sha256"] != second["sha256"] != original["sha256"]
    dependency = tmp_path / "surface-source"
    dependency.write_bytes(b"first surface source")
    monkeypatch.setattr(qsl.surface, "__file__", str(dependency))
    third = qsl.qsl_engine_manifest()
    dependency.write_bytes(b"changed surface source")
    assert qsl.qsl_engine_manifest()["sha256"] != third["sha256"]

"""Ideas endpoint: the advisory idea-context contract of
/api/market/{sym}/ideas — signals projection (string scores coerced,
per-name PEAD filtering, evaluated cap), the queue reduced to the name
(other underlyings filtered out, string money as floats), paper
positions joined with the book, word-boundary ledger matching, the
research section caps, the null branches, and the protocol boundary."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tree_options.desk import signals as desk_signals
from tree_options.trex.clock import ET
from tree_options.trex.state import BookState, Status
from tree_options.trex_web.app import create_app
from tree_options.trex_web.ideas_view import ideas_payload

SYM = "NVDA"
SESSION = "2026-09-23"

PLAN_TOML_OPEN = """\
id = "putspread-test"
account_mode = "paper"
total_debit_cap = 1840.00
entry_window_start = "09:45"
entry_window_end = "12:00"

[[structures]]
id = "nvda-oct"
underlying = "NVDA"
entry_date = 2026-09-18
expiry = 2026-10-16
long_strike = 185.0
short_strike = 150.0
quantity = 5
limit_cap = 0.50
exit_deadline = 2026-10-09

[[structures]]
id = "qqq-nov"
underlying = "QQQ"
entry_date = 2026-09-18
expiry = 2026-11-20
long_strike = 600.0
short_strike = 475.0
quantity = 4
limit_cap = 2.40
exit_deadline = 2026-11-06
"""

# same underlying, CLOSED: proves closed rows ride along, open sorts first
# (and alphabetical plan order alone would put this one first)
PLAN_TOML_CLOSED = """\
id = "putspread-closed"
account_mode = "paper"
total_debit_cap = 920.00
entry_window_start = "09:45"
entry_window_end = "12:00"

[[structures]]
id = "nvda-dec"
underlying = "NVDA"
entry_date = 2026-09-18
expiry = 2026-12-18
long_strike = 180.0
short_strike = 150.0
quantity = 2
limit_cap = 0.40
exit_deadline = 2026-12-04
"""

LEDGER_MD = """\
# Paper-trading ledger — scratch lane (no money, NOT the sealed program)

| 1 | 2026-09-08-spy-card.md | R1 pin+drift+lowvol (long) | SPY 09-04 -> 09-08 | RESOLVED 09-08 | -54.92 |
| 4 | 2026-09-10-r3fup-bounce.md | R3f+up | LONG: MSFT 493.95 | RESOLVED 09-10 | -228.62 |
| 7 | 2026-09-22-nvda-card.md | NVDA bounce entry | NVDA 183.20, 09-22 -> open | OPEN |
Card 7 seal sha256 below (NVDA row appended 09-22):
deadbeefdeadbeef
"""

RESEARCH_MD = """\
# RESEARCH-LEDGER — what this program actually knows (2026-09-11)

## Scope
Panel: ohlc-panel.json — NVDA is one of the 37 names.

## SURVIVORS (live, actionable-as-information) — REVISED 2026-09-12 by the XU wave
- XSMOM-TOP3 monthly (2026-09-12): NVDA in the ranked 36, +0.19% gross.
- unrelated survivor line.

## DEFLATED (looked real; the pre-committed checks killed them)
- NVDAX cross-edge deflated 2026-09-10 (boundary guard).
- NVDA mean-reversion deflated 2026-09-11.

## DEAD
- NVDA overnight gap edge dead 2026-09-01.

## campaign-2026-09 (inner-fold) — 2026-09-24
- NVDA leg re-checked 2026-09-24 (inner fold).

## DATA INTEGRITY (what the program caught)
- NVDA vendor hole 2026-02..06 caught 2026-09-05.
"""


def _signals_doc() -> dict[str, Any]:
    evaluated_nvda = [
        {
            "name": SYM,
            "report_date": f"2026-08-{d:02d}",
            "prior_session": f"2026-08-{d - 1:02d}",
            "move": "0.0100" if d < 6 else None,
            "fires": False,
            "reason": "below_threshold" if d < 6 else "no_session_bar",
        }
        for d in range(1, 7)
    ]
    return {
        "schema": "desk-signals/1",
        "session": SESSION,
        "panel_last_session": SESSION,
        "xsmom": {
            "convention": "close(t)/close(t-273)-1",
            "conventions_agree": True,
            "is_rebalance_day": False,
            "n_ranked": 36,
            "scores": {SYM: "0.288776", "AAA": "1.500000", "BBB": "-0.200000"},
            "top3": [SYM, "AAA", "BBB"],
            "top3_skip21": [SYM, "AAA", "BBB"],
        },
        "pead": [
            {"name": SYM, "report_date": "2026-09-22", "move": "0.0210"},
            {"name": "OTHER", "report_date": "2026-09-22", "move": "0.0300"},
        ],
        "pead_evaluated": [
            {
                "name": "OTHER",
                "report_date": "2026-09-22",
                "prior_session": "2026-09-21",
                "move": "0.0300",
                "fires": True,
                "reason": "beat",
            },
            *evaluated_nvda,
        ],
        "provenance": {"generated_at": "2026-09-23T20:40:00+00:00"},
    }


def _queue_deal(underlying: str, rank: int, status: str) -> dict[str, Any]:
    return {
        "deal_id": f"d-20260923-{underlying.lower()}-{rank}",
        "rank": rank,
        "status": status,
        "reasons": ["matched row R1"],
        "notes": ["note one"],
        "row": "r1",
        "row_title": "put debit spread",
        "tier": "t2",
        "underlying": underlying,
        "kind": "put_debit",
        "quantity": 1,
        "legs": [
            {
                "right": "P",
                "action": "BUY",
                "strike": "185",
                "expiry": "2026-10-16",
                "bid": "1.10",
                "ask": "1.20",
                "oi": 5915,
                "iv": 0.42,
                "delta": -0.31,
            },
            {
                "right": "P",
                "action": "SELL",
                "strike": "150",
                "expiry": "2026-10-16",
                "bid": "0.10",
                "ask": "0.15",
                "oi": 1201,
                "iv": 0.38,
                "delta": -0.08,
            },
        ],
        "entry_session": "2026-09-24",
        "exit_deadline": "2026-10-09",
        "width": "35",
        "ref_mid": "1.025",
        "fill": "1.05",
        "limit": "1.06",
        "max_loss": "1.06",
        "signal": (
            {"name": "xsmom_top3", "excess_20": "0.0123", "weight": "0.5"}
            if status == "admissible"
            else None
        ),
        "structure": {"kind": "put_debit"},
        "valuation": {"base_fill": "1.05"},
        "decision": None,
        "rails": None,
        "rationale": "text",
    }


def _queue_doc() -> dict[str, Any]:
    return {
        "schema": "trex.deal/1",
        "session": SESSION,
        "entry_session": "2026-09-24",
        "valid_until": "2026-09-24T11:30:00-04:00",
        "miner": {"file": "m.toml", "version": "v1", "status": "PROPOSED"},
        "admissible": [_queue_deal(SYM, 1, "admissible"), _queue_deal("OTHER", 2, "admissible")],
        "surfaced": [_queue_deal(SYM, 9, "not_selected"), _queue_deal("OTHER", 10, "refused")],
    }


def _write_json(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))


def _seed_all(tmp_path: Path) -> None:
    """One hermetic desk: signals, queue, features, plans+books, ledgers."""
    _write_json(tmp_path / "desk-state" / "signals" / f"{SESSION}.json", _signals_doc())
    _write_json(tmp_path / "desk-state" / "queue" / f"{SESSION}.json", _queue_doc())
    # two features sessions: the newest one's next_report wins
    _write_json(
        tmp_path / "store" / "features" / f"{SESSION}.json",
        {
            "schema": "desk-features/1",
            "session": SESSION,
            "names": {SYM: {"spot": 183.2, "earnings": {"next_report": "2026-10-29"}}},
        },
    )
    _write_json(
        tmp_path / "store" / "features" / "2026-09-22.json",
        {
            "schema": "desk-features/1",
            "session": "2026-09-22",
            "names": {SYM: {"spot": 180.0, "earnings": {"next_report": "2026-09-25"}}},
        },
    )
    # plans: open NVDA (+QQQ that must be filtered) and a closed NVDA plan
    plans = tmp_path / "plans"
    plans.mkdir(parents=True)
    (plans / "putspread-test.toml").write_text(PLAN_TOML_OPEN)
    (plans / "putspread-closed.toml").write_text(PLAN_TOML_CLOSED)
    run = tmp_path / "state" / "putspread-test"
    run.mkdir(parents=True)
    book = BookState(["nvda-oct", "qqq-nov"])
    st = book.structures["nvda-oct"]
    st.status = Status.OPEN
    st.entry_fill = Decimal("0.21")
    st.filled_qty = 5
    book.save(run / "book.json")
    run2 = tmp_path / "state" / "putspread-closed"
    run2.mkdir(parents=True)
    book2 = BookState(["nvda-dec"])
    book2.structures["nvda-dec"].status = Status.CLOSED
    book2.save(run2 / "book.json")
    # ledgers
    paper = tmp_path / "paper"
    paper.mkdir()
    (paper / "LEDGER.md").write_text(LEDGER_MD)
    (paper / "RESEARCH-LEDGER.md").write_text(RESEARCH_MD)


def _client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            state_dir=str(tmp_path / "state"),
            plans_dir=str(tmp_path / "plans"),
            discovery_dir=str(tmp_path / "discovery"),
            desk_state_dir=str(tmp_path / "desk-state"),
            desk_paper_dir=str(tmp_path / "paper"),
            desk_store_dir=str(tmp_path / "store"),
        )
    )


class TestIdeasFullContract:
    def test_every_section_and_coercion(self, tmp_path: Path) -> None:
        _seed_all(tmp_path)
        payload = _client(tmp_path).get(f"/api/market/{SYM}/ideas").json()

        assert payload["symbol"] == SYM
        assert payload["now"]
        assert set(payload) == {
            "now",
            "symbol",
            "signals",
            "queue",
            "paper_positions",
            "cards",
            "research",
            "protocol",
        }

        sig = payload["signals"]
        assert sig["session"] == SESSION
        assert isinstance(sig["age_seconds"], int)
        assert sig["xsmom"] == {
            "score": pytest.approx(0.288776),
            "in_top3": True,
            "top3": [SYM, "AAA", "BBB"],
            "is_rebalance_day": False,
            "n_ranked": 36,
            "conventions_agree": True,
        }
        # per-name filtering: OTHER's beat/evaluated never ride along
        assert sig["pead"]["beats"] == [
            {"report_date": "2026-09-22", "move": pytest.approx(0.021)}
        ]
        ev = sig["pead"]["evaluated"]
        assert len(ev) == 5  # 6 kept -> latest 5
        assert ev[0]["report_date"] == "2026-08-02"  # oldest dropped
        assert ev[-1]["move"] is None  # string numerics coerce, nulls pass
        assert ev[-1]["fires"] is False
        assert ev[-1]["reason"] == "no_session_bar"
        assert sig["next_report"] == "2026-10-29"  # newest features wins

        q = payload["queue"]
        assert q["session"] == SESSION
        assert q["entry_session"] == "2026-09-24"
        assert q["valid_until"] == "2026-09-24T11:30:00-04:00"
        assert q["miner_status"] == "PROPOSED"
        assert [d["underlying"] for d in q["deals"]] == [SYM, SYM]  # OTHER filtered
        d0 = q["deals"][0]
        assert d0["deal_id"] == "d-20260923-nvda-1"
        assert d0["rank"] == 1
        assert d0["status"] == "admissible"
        assert d0["row_title"] == "put debit spread"
        assert d0["kind"] == "put_debit"
        assert d0["quantity"] == 1
        assert d0["ref_mid"] == pytest.approx(1.025)
        assert d0["fill"] == pytest.approx(1.05)
        assert d0["limit"] == pytest.approx(1.06)
        assert d0["max_loss"] == pytest.approx(1.06)
        assert d0["signal"] == {"name": "xsmom_top3", "excess_20": pytest.approx(0.0123)}
        assert d0["reasons"] == ["matched row R1"]
        assert d0["notes"] == ["note one"]
        assert set(d0) == {
            "deal_id",
            "rank",
            "status",
            "row_title",
            "kind",
            "underlying",
            "quantity",
            "legs",
            "ref_mid",
            "fill",
            "limit",
            "max_loss",
            "signal",
            "reasons",
            "notes",
        }
        legs = d0["legs"]
        assert len(legs) == 2
        assert legs[0] == {
            "right": "P",
            "action": "BUY",
            "strike": 185.0,
            "expiry": "2026-10-16",
            "bid": pytest.approx(1.10),
            "ask": pytest.approx(1.20),
            "oi": 5915.0,
            "iv": pytest.approx(0.42),
            "delta": pytest.approx(-0.31),
        }
        assert q["deals"][1]["status"] == "not_selected"
        assert q["deals"][1]["signal"] is None

        rows = payload["paper_positions"]
        assert len(rows) == 2  # qqq-nov filtered; closed NVDA kept
        assert rows[0]["plan_id"] == "putspread-test"  # open first...
        assert rows[0]["structure_id"] == "nvda-oct"
        assert rows[0]["account_mode"] == "paper"
        assert rows[0]["expiry"] == "2026-10-16"
        assert rows[0]["long_strike"] == 185.0
        assert rows[0]["short_strike"] == 150.0
        assert rows[0]["quantity"] == 5
        assert rows[0]["open_qty"] == 5
        assert rows[0]["status"] == "open"
        assert rows[0]["entry_fill"] == pytest.approx(0.21)
        assert rows[0]["exit_deadline"] == "2026-10-09"
        assert rows[1]["plan_id"] == "putspread-closed"  # ...then by plan id
        assert rows[1]["status"] == "closed"
        assert rows[1]["open_qty"] == 0
        assert rows[1]["entry_fill"] is None

        cards = payload["cards"]
        raw = (tmp_path / "paper" / "LEDGER.md").read_bytes()
        assert cards["ledger_sha256_12"] == hashlib.sha256(raw).hexdigest()[:12]
        assert cards["lines"] == [
            "| 7 | 2026-09-22-nvda-card.md | NVDA bounce entry | NVDA 183.20, 09-22 -> open | OPEN |",
            "Card 7 seal sha256 below (NVDA row appended 09-22):",
        ]  # the MSFT line never matches \bNVDA\b

        res = payload["research"]
        rraw = (tmp_path / "paper" / "RESEARCH-LEDGER.md").read_bytes()
        assert res["sha256_12"] == hashlib.sha256(rraw).hexdigest()[:12]
        assert res["ledger_date"] == "2026-09-12"  # newest date in kept lines
        assert [e["section"] for e in res["entries"]] == ["SURVIVORS", "DEFLATED", "DEAD"]
        assert res["entries"][0]["line"].startswith("- XSMOM-TOP3 monthly")
        assert res["entries"][1]["line"].startswith("- NVDA mean-reversion")
        assert all("NVDAX" not in e["line"] for e in res["entries"])

    def test_protocol_is_the_desk_constants(self, tmp_path: Path) -> None:
        _seed_all(tmp_path)
        payload = _client(tmp_path).get(f"/api/market/{SYM}/ideas").json()
        assert payload["protocol"] == {
            "allowed_direction": sorted(desk_signals.ALLOWED_DIRECTION),
            "context_only": sorted(desk_signals.CONTEXT_ONLY),
            "advisory": True,
        }
        # pin the live values too, so a constant rename surfaces here
        assert payload["protocol"]["allowed_direction"] == ["pead_beat", "xsmom_top3"]
        assert payload["protocol"]["context_only"] == ["breadth", "trend", "vix_term"]

    def test_lowercase_symbol_is_upcased(self, tmp_path: Path) -> None:
        _seed_all(tmp_path)
        assert _client(tmp_path).get(f"/api/market/{SYM.lower()}/ideas").json()["symbol"] == SYM

    def test_bad_symbol_404s(self, tmp_path: Path) -> None:
        _seed_all(tmp_path)
        assert _client(tmp_path).get("/api/market/nvda!!/ideas").status_code == 404


class TestIdeasNullBranches:
    def test_empty_desk_degrades_every_section(self, tmp_path: Path) -> None:
        client = _client(tmp_path)  # nothing seeded
        payload = client.get(f"/api/market/{SYM}/ideas").json()
        assert payload["signals"] is None
        assert payload["queue"] is None
        assert payload["paper_positions"] == []
        assert payload["cards"] is None
        assert payload["research"] is None
        assert payload["protocol"]["advisory"] is True

    def test_name_absent_from_scores_keeps_the_xsmom_section(self, tmp_path: Path) -> None:
        _seed_all(tmp_path)
        payload = _client(tmp_path).get("/api/market/MSF/ideas").json()
        sig = payload["signals"]
        assert sig is not None  # the file exists; the section survives
        assert sig["xsmom"]["score"] is None
        assert sig["xsmom"]["in_top3"] is False
        assert sig["pead"] == {"beats": [], "evaluated": []}

    def test_word_boundary_never_matches_a_longer_ticker(self, tmp_path: Path) -> None:
        _seed_all(tmp_path)
        payload = _client(tmp_path).get("/api/market/MSF/ideas").json()
        assert payload["cards"] is not None
        assert payload["cards"]["lines"] == []  # the MSFT line must not match
        assert payload["research"]["entries"] == []


class TestIdeasNextReport:
    def test_calendar_fallback_first_future_then_last(self, tmp_path: Path) -> None:
        _write_json(tmp_path / "desk-state" / "signals" / f"{SESSION}.json", _signals_doc())
        _write_json(
            tmp_path / "paper" / "earnings-calendar.json",
            {SYM: ["2026-01-15", "2099-12-15"], "OLD": ["2025-01-01", "2025-06-01"]},
        )
        client = _client(tmp_path)
        assert client.get(f"/api/market/{SYM}/ideas").json()["signals"]["next_report"] == (
            "2099-12-15"  # first future date
        )
        assert client.get("/api/market/OLD/ideas").json()["signals"]["next_report"] == (
            "2025-06-01"  # all past -> the last known
        )

    def test_unknown_name_is_null(self, tmp_path: Path) -> None:
        _write_json(tmp_path / "desk-state" / "signals" / f"{SESSION}.json", _signals_doc())
        _write_json(tmp_path / "paper" / "earnings-calendar.json", {SYM: ["2026-12-15"]})
        assert (
            _client(tmp_path).get("/api/market/ZZZ/ideas").json()["signals"]["next_report"]
            is None
        )


class TestIdeasPurePayload:
    """Direct ideas_payload calls with a pinned clock."""

    def _payload(self, tmp_path: Path, now: datetime) -> dict[str, Any]:
        return ideas_payload(
            SYM,
            tmp_path / "desk-state" / "signals",
            tmp_path / "desk-state" / "queue",
            tmp_path / "store",
            tmp_path / "paper",
            tmp_path / "state",
            tmp_path / "plans",
            now,
        )

    def test_age_seconds_math_against_a_pinned_now(self, tmp_path: Path) -> None:
        _seed_all(tmp_path)
        now = datetime(2026, 9, 24, 12, 0, tzinfo=ET)
        sig = self._payload(tmp_path, now)["signals"]
        # generated 2026-09-23T20:40Z == 16:40 EDT; now 12:00 EDT next day
        assert sig["age_seconds"] == 19 * 3600 + 20 * 60

    def test_research_per_section_line_cap(self, tmp_path: Path) -> None:
        _seed_all(tmp_path)
        research = tmp_path / "paper" / "RESEARCH-LEDGER.md"
        body = ["# RESEARCH-LEDGER", "", "## SURVIVORS (live)"]
        body += [f"- NVDA survivor line {i} (2026-09-0{i % 9 + 1})" for i in range(13)]
        body += ["", "## DEAD", "- NVDA dead line (2026-09-02)"]
        research.write_text("\n".join(body) + "\n")
        entries = self._payload(tmp_path, datetime(2026, 9, 24, 12, 0, tzinfo=ET))["research"]
        assert [e["section"] for e in entries["entries"]].count("SURVIVORS") == 12
        assert entries["entries"][-1]["section"] == "DEAD"

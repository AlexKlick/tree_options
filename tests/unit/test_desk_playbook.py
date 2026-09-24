"""Desk D5 playbook: the sealed ``data/desk/playbook/v1.toml``, its loader
and the rules the loader enforces by construction.

The expectations below are written from the approved plan (D5 rows, the
[limits] table the rails lane loads with the same schema) and the lane
brief, never read back from the implementation. Seal checks recompute the
sha256 of the file bytes in the test.
"""

from __future__ import annotations

import copy
import hashlib
import shutil
import tomllib
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from tree_options.desk import playbook, signals
from tree_options.desk.universe import CHAIN_UNIVERSE, NO_OPTIONS_EXPRESSION, PANEL_ETFS
from tree_options.trex.plan import ExitRules, StopLoss, TakeProfit

REPO = Path(__file__).resolve().parents[2]
PB_DIR = REPO / "data" / "desk" / "playbook"
PB_FILE = PB_DIR / "v1.toml"


@pytest.fixture(scope="module")
def raw() -> dict[str, Any]:
    return tomllib.loads(PB_FILE.read_text())


@pytest.fixture(scope="module")
def pb() -> playbook.Playbook:
    return playbook.load_playbook(PB_FILE)


def _parse(doc: dict[str, Any]) -> playbook.Playbook:
    return playbook.parse_playbook(doc, sha256="0" * 64)


def _row(doc: dict[str, Any], rid: str) -> dict[str, Any]:
    (row,) = [r for r in doc["rows"] if r["id"] == rid]
    return row


# ------------------------------------------------------------------ seal


class TestSeal:
    def test_real_playbook_is_sealed(self, pb) -> None:
        sha = hashlib.sha256(PB_FILE.read_bytes()).hexdigest()
        assert pb.sha256 == sha
        assert (PB_DIR / "v1.sha256").read_text() == f"{sha}  v1.toml\n"
        rows = [
            line
            for line in (PB_DIR / "SEALS.md").read_text().splitlines()
            if line.startswith("| ") and "| v1.toml |" in line
        ]
        assert any(f"| {sha} |" in line for line in rows)

    def _copy(self, tmp_path: Path) -> Path:
        for name in ("v1.toml", "v1.sha256", "SEALS.md"):
            shutil.copy(PB_DIR / name, tmp_path / name)
        return tmp_path / "v1.toml"

    def test_hash_mismatch_is_refused(self, tmp_path) -> None:
        path = self._copy(tmp_path)
        playbook.load_playbook(path)  # the copy loads
        path.write_bytes(path.read_bytes() + b"\n# one more line\n")
        with pytest.raises(playbook.PlaybookSealError, match="sha256"):
            playbook.load_playbook(path)

    def test_missing_sidecar_is_refused(self, tmp_path) -> None:
        path = self._copy(tmp_path)
        (tmp_path / "v1.sha256").unlink()
        with pytest.raises(playbook.PlaybookSealError):
            playbook.load_playbook(path)

    def test_a_sha_without_a_seal_row_is_refused(self, tmp_path) -> None:
        """Rewriting the file AND its sidecar is not enough: the append-only
        SEALS.md must carry the row."""
        path = self._copy(tmp_path)
        data = path.read_bytes() + b"\n# edited\n"
        path.write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        (tmp_path / "v1.sha256").write_text(f"{sha}  v1.toml\n")
        with pytest.raises(playbook.PlaybookSealError, match="SEALS"):
            playbook.load_playbook(path)

    def test_seal_writes_sidecar_and_appends(self, tmp_path) -> None:
        path = self._copy(tmp_path)
        data = path.read_bytes() + b"\n# a v1 variant for this test only\n"
        path.write_bytes(data)
        before = (tmp_path / "SEALS.md").read_text()
        sha = playbook.seal_playbook(path, basis="test")
        assert sha == hashlib.sha256(data).hexdigest()
        after = (tmp_path / "SEALS.md").read_text()
        assert after.startswith(before) and after.count(sha) == 1
        assert playbook.load_playbook(path).sha256 == sha

    def test_seal_refuses_an_invalid_playbook(self, tmp_path) -> None:
        path = self._copy(tmp_path)
        path.write_text(path.read_text().replace('max_book_loss_usd = "5000"', ""))
        before = (tmp_path / "SEALS.md").read_text()
        with pytest.raises(playbook.PlaybookError):
            playbook.seal_playbook(path, basis="test")
        assert (tmp_path / "SEALS.md").read_text() == before

    def test_default_path_follows_the_env(self, tmp_path, monkeypatch) -> None:
        path = self._copy(tmp_path)
        monkeypatch.setenv("DESK_PLAYBOOK_DIR", str(tmp_path))
        assert playbook.load_playbook().sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- limits

LIMITS = {
    "max_loss_per_trade_usd": "500",
    "max_book_loss_usd": "5000",
    "max_per_underlying": 2,
    "max_net_beta_delta_usd_per_1pct_spy": "250",
    "max_admissions_per_session": 3,
    "max_roundtrip_cost_frac_of_max_loss": "0.15",
    "min_leg_open_interest": 100,
    "max_leg_spread_frac_of_mid": "0.10",
    "min_long_single_abs_delta": "0.30",
    "max_chain_age_sessions": 1,
    "max_book_short_vega_usd_per_volpt": "100",
}


class TestLimits:
    def test_table_is_exactly_the_brief(self, raw, pb) -> None:
        assert raw["limits"] == LIMITS  # money as strings on the wire, counts as ints
        for key, value in LIMITS.items():
            got = getattr(pb.limits, key)
            if isinstance(value, str):
                assert isinstance(got, Decimal) and got == Decimal(value)
            else:
                assert type(got) is int and got == value

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda t: t.pop("max_book_loss_usd"),
            lambda t: t.update(extra_key="1"),
            lambda t: t.update(max_loss_per_trade_usd=500.0),  # a float is never money
            lambda t: t.update(max_loss_per_trade_usd=500),  # nor an int
            lambda t: t.update(max_per_underlying="2"),
            lambda t: t.update(max_per_underlying=True),
            lambda t: t.update(max_leg_spread_frac_of_mid="ten percent"),
            lambda t: t.update(max_loss_per_trade_usd="-500"),
        ],
    )
    def test_schema_is_refused_when_off(self, raw, mutate) -> None:
        doc = copy.deepcopy(raw)
        mutate(doc["limits"])
        with pytest.raises(playbook.PlaybookError):
            _parse(doc)


# ------------------------------------------------------------- vol state


class TestVolStatePolicy:
    def test_declared_before_any_state(self, pb) -> None:
        v = pb.vol_state
        assert v.metric == "iv30_over_har20"
        assert v.source == "chain"
        assert v.features_schema == "desk-features/1"
        assert v.cross_source == "forbidden"
        assert (v.window_sessions, v.min_history) == (252, 120)
        assert (v.validated.cheap_below, v.validated.rich_above) == (Fraction(1, 3), Fraction(2, 3))
        assert (v.unvalidated.cheap_below, v.unvalidated.rich_above) == (
            Fraction(1, 5),
            Fraction(4, 5),
        )
        assert v.validated_names == frozenset({"IWM"})  # IVHIST-001 run 2 (verdict of record)
        assert v.har_status_required == "validated"

    def test_term_and_event_policy(self, pb) -> None:
        t, e = pb.term, pb.events
        assert t.steep_above == Fraction(2, 3)
        assert (t.window_sessions, t.min_history) == (252, 120)
        assert (t.market_front, t.market_back) == ("VIX", "VIX3M")
        assert (t.event_front_min_dte, t.event_back_min_gap_days) == (7, 21)
        assert e.window_sessions == 5
        assert e.macro_kinds == frozenset({"fomc", "fomc_unscheduled", "cpi", "nfp"})

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("rich_above", "1/4"),  # below cheap_below
            ("cheap_below", "0"),
            ("unvalidated_rich_above", "3/5"),  # a NARROWER band than validated
            ("min_history", 0),
            ("cross_source", "offset"),
            ("validated_names", ["IWM", "TQQQ"]),
        ],
    )
    def test_bad_policy_is_refused(self, raw, key, value) -> None:
        doc = copy.deepcopy(raw)
        doc["vol_state"][key] = value
        with pytest.raises(playbook.PlaybookError):
            _parse(doc)


# ------------------------------------------------------------------- rows

D = Decimal
ROWS: dict[str, dict[str, Any]] = {
    "R1": {
        "number": 1,
        "tier": "signal-validated",
        "status": "active",
        "kind": "debit_vertical",
        "direction": "bull",
        "signals": ("xsmom_top3",),
        "vol": frozenset({"cheap", "fair"}),
        "fidelity": "unvalidated_wide_band",
        "legs": {"long": ("C", "BUY", (D("0.55"), D("0.70"))), "short": ("C", "SELL", (D("0.30"), D("0.40")))},
        "dte": (120, 180),
        "exits": ExitRules(
            touch=False,
            breach=False,
            take_profit=TakeProfit(basis="width_frac", value=D("0.90")),
            stop_loss=None,
            stop_confirm_ticks=3,
        ),
        "time": (20, 90, None),
        "max_open": 3,
        "drift": D("0.5"),
    },
    "R2": {
        "number": 2,
        "tier": "signal-validated",
        "status": "active",
        "kind": "credit_vertical",
        "direction": "bull",
        "signals": ("xsmom_top3",),
        "vol": frozenset({"rich"}),
        "fidelity": "unvalidated_wide_band",
        "legs": {"short": ("P", "SELL", (D("0.25"), D("0.35"))), "long": ("P", "BUY", (D("0.10"), D("0.20")))},
        "dte": (45, 90),
        "exits": ExitRules(
            touch=False,
            breach=True,
            take_profit=TakeProfit(basis="credit_frac", value=D("0.50")),
            stop_loss=StopLoss(basis="credit_mult", value=D("2.0")),
            stop_confirm_ticks=3,
        ),
        "time": (20, 21, None),
        "max_open": 3,
        "drift": D("0.5"),
    },
    "R3": {
        "number": 3,
        "tier": "signal-validated",
        "status": "active",
        "kind": "debit_vertical",
        "direction": "bull",
        "signals": ("pead_beat",),
        "vol": None,
        "fidelity": "not_used",
        "legs": {"long": ("C", "BUY", (D("0.55"), D("0.70"))), "short": ("C", "SELL", (D("0.30"), D("0.40")))},
        "dte": (60, 120),
        "exits": ExitRules(
            touch=False,
            breach=False,
            take_profit=TakeProfit(basis="width_frac", value=D("0.90")),
            stop_loss=None,
            stop_confirm_ticks=3,
        ),
        "time": (20, None, None),
        "max_open": 4,
        "drift": D("0.5"),
    },
    "R4": {
        "number": 4,
        "tier": "textbook-prior",
        "status": "active",
        "kind": "iron_condor",
        "direction": "none",
        "signals": (),
        "vol": frozenset({"rich"}),
        "fidelity": "validated_only",
        "legs": {
            "long_put": ("P", "BUY", (D("0.05"), D("0.10"))),
            "short_put": ("P", "SELL", (D("0.15"), D("0.20"))),
            "short_call": ("C", "SELL", (D("0.15"), D("0.20"))),
            "long_call": ("C", "BUY", (D("0.05"), D("0.10"))),
        },
        "dte": (30, 60),
        "exits": ExitRules(
            touch=False,
            breach=True,
            take_profit=TakeProfit(basis="credit_frac", value=D("0.50")),
            stop_loss=StopLoss(basis="credit_mult", value=D("2.0")),
            stop_confirm_ticks=3,
        ),
        "time": (None, 21, None),
        "max_open": 2,
        "drift": D("0"),
    },
    "R5": {
        "number": 5,
        "tier": "textbook-prior",
        "status": "active",
        "kind": "calendar",
        "direction": "any",
        "signals": (),
        "vol": None,
        "fidelity": "not_used",
        "legs": {"front": ("P", "SELL", (D("0.40"), D("0.60"))), "back": ("P", "BUY", None)},
        "dte": (7, 45),
        "exits": ExitRules(
            touch=False,
            breach=False,
            take_profit=TakeProfit(basis="gain_frac", value=D("0.25")),
            stop_loss=StopLoss(basis="debit_frac", value=D("0.50")),
            stop_confirm_ticks=3,
        ),
        "time": (None, 2, 1),
        "max_open": 1,
        "drift": D("0"),
    },
    "R6": {
        "number": 6,
        "tier": "textbook-prior",
        "status": "active",
        "kind": "diagonal",
        "direction": "bull",
        "signals": ("pead_beat", "xsmom_top3"),
        "vol": None,
        "fidelity": "not_used",
        "legs": {"back": ("C", "BUY", (D("0.60"), D("0.75"))), "front": ("C", "SELL", (D("0.25"), D("0.35")))},
        "dte": (90, 180),
        "exits": ExitRules(
            touch=False,
            breach=False,
            take_profit=TakeProfit(basis="gain_frac", value=D("0.30")),
            stop_loss=StopLoss(basis="debit_frac", value=D("0.50")),
            stop_confirm_ticks=3,
        ),
        "time": (20, 7, None),
        "max_open": 2,
        "drift": D("0.5"),
    },
    "R7": {
        "number": 7,
        "tier": "risk-control",
        "status": "active",
        "kind": "debit_vertical",
        "direction": "any",
        "signals": (),
        "vol": None,
        "fidelity": "not_used",
        "legs": {"long": ("P", "BUY", (D("0.40"), D("0.55"))), "short": ("P", "SELL", (D("0.15"), D("0.25")))},
        "dte": (60, 120),
        "exits": ExitRules(
            touch=False,
            breach=False,
            take_profit=TakeProfit(basis="width_frac", value=D("0.80")),
            stop_loss=None,
            stop_confirm_ticks=3,
        ),
        "time": (20, 30, None),
        "max_open": 1,
        "drift": D("0"),
    },
    "R8a": {
        "number": 8,
        "tier": "textbook-prior",
        "status": "dormant",
        "kind": "debit_vertical",
        "direction": "bear",
        "signals": (),
        "vol": frozenset({"cheap", "fair"}),
        "fidelity": "unvalidated_wide_band",
        "legs": {"long": ("P", "BUY", (D("0.55"), D("0.70"))), "short": ("P", "SELL", (D("0.30"), D("0.40")))},
        "dte": (120, 180),
        "exits": ExitRules(
            touch=False,
            breach=False,
            take_profit=TakeProfit(basis="width_frac", value=D("0.90")),
            stop_loss=None,
            stop_confirm_ticks=3,
        ),
        "time": (20, 90, None),
        "max_open": 0,
        "drift": D("0"),
    },
    "R8b": {
        "number": 8,
        "tier": "textbook-prior",
        "status": "dormant",
        "kind": "credit_vertical",
        "direction": "bear",
        "signals": (),
        "vol": frozenset({"rich"}),
        "fidelity": "unvalidated_wide_band",
        "legs": {"short": ("C", "SELL", (D("0.25"), D("0.35"))), "long": ("C", "BUY", (D("0.10"), D("0.20")))},
        "dte": (45, 90),
        "exits": ExitRules(
            touch=False,
            breach=True,
            take_profit=TakeProfit(basis="credit_frac", value=D("0.50")),
            stop_loss=StopLoss(basis="credit_mult", value=D("2.0")),
            stop_confirm_ticks=3,
        ),
        "time": (20, 21, None),
        "max_open": 0,
        "drift": D("0"),
    },
}  # fmt: skip


class TestRowsAsThePlanListsThem:
    def test_row_set(self, pb) -> None:
        assert [r.id for r in pb.rows] == list(ROWS)
        assert sorted({r.number for r in pb.rows}) == list(range(1, 9))

    @pytest.mark.parametrize("rid", list(ROWS))
    def test_row(self, pb, rid: str) -> None:
        want = ROWS[rid]
        (row,) = [r for r in pb.rows if r.id == rid]
        assert row.number == want["number"]
        assert row.tier == want["tier"]
        assert row.status == want["status"]
        assert row.kind == want["kind"]
        assert row.when.direction == want["direction"]
        assert tuple(sorted(row.when.signals)) == want["signals"]
        assert row.when.vol == want["vol"]
        assert row.iv_fidelity == want["fidelity"]
        assert {g.role: (g.right, g.action, g.abs_delta) for g in row.legs} == want["legs"]
        assert row.legs[0].dte == want["dte"]
        assert row.exits.rules == want["exits"]  # the engine's own model, every field explicit
        ts = row.exits.time_stop
        assert (ts.hold_sessions, ts.min_dte, ts.sessions_after_event) == want["time"]
        assert row.max_open == want["max_open"]
        assert row.drift_weight == want["drift"]
        assert row.evidence  # every row cites its evidence

    def test_specific_plan_details(self, pb) -> None:
        by = {r.id: r for r in pb.rows}
        assert by["R4"].universe.names == tuple(n for n in CHAIN_UNIVERSE if n in PANEL_ETFS)
        assert (by["R4"].when.name_term, by["R4"].when.market_term) == ("contango", "contango")
        assert by["R4"].when.events == "no_event"
        assert by["R5"].when.events == "macro_event_ahead"
        assert by["R5"].when.front_over_back == "required"
        assert by["R5"].expiry_gap_days == (21, 63)
        assert by["R6"].when.name_term == "steep_contango"
        assert by["R6"].expiry_gap_days == (30, 150)
        assert by["R7"].universe.names == ("SPY", "QQQ")
        assert by["R7"].when.book == "net_delta_over_cap"
        assert by["R7"].when.news == "ignore"  # a hedge is never vetoed by news
        for r in pb.rows:
            if r.id != "R7":
                assert r.when.news == "block_if_flagged"
            assert r.universe.min_liquidity_score == 10
        for rid in ("R1", "R2", "R3", "R6", "R8a", "R8b"):
            assert by[rid].universe.names == CHAIN_UNIVERSE


# ------------------------------------------------------- enforcement rules


class TestDirectionByConstruction:
    def test_active_rows_point_only_with_allowed_signals(self, pb) -> None:
        for r in pb.active_rows():
            assert r.when.direction in ("bull", "none", "any")
            assert set(r.when.signals) <= signals.ALLOWED_DIRECTION
            assert bool(r.when.signals) == (r.when.direction == "bull")

    @pytest.mark.parametrize(
        "name", sorted(signals.BANNED | signals.CONTEXT_ONLY | {"made_up", "xsmom_skip21"})
    )
    def test_any_other_signal_is_refused(self, raw, name: str) -> None:
        doc = copy.deepcopy(raw)
        _row(doc, "R1")["when"]["signals"] = [name]
        with pytest.raises(playbook.PlaybookError):
            _parse(doc)
        # also as an extra signal beside an allowed one, and on a dormant row
        doc = copy.deepcopy(raw)
        _row(doc, "R3")["when"]["signals"] = ["pead_beat", name]
        with pytest.raises(playbook.PlaybookError):
            _parse(doc)
        doc = copy.deepcopy(raw)
        _row(doc, "R8a")["when"]["signals"] = [name]
        with pytest.raises(playbook.PlaybookError):
            _parse(doc)

    def test_bear_rows_cannot_be_activated(self, raw) -> None:
        for rid in ("R8a", "R8b"):
            doc = copy.deepcopy(raw)
            row = _row(doc, rid)
            row["status"] = "active"
            row["max_open"] = 1
            row.pop("dormant_reason")
            with pytest.raises(playbook.PlaybookError, match="bear"):
                _parse(doc)

    def test_bull_without_a_signal_is_refused(self, raw) -> None:
        doc = copy.deepcopy(raw)
        _row(doc, "R1")["when"]["signals"] = []
        with pytest.raises(playbook.PlaybookError):
            _parse(doc)

    def test_a_signal_on_a_no_view_row_is_refused(self, raw) -> None:
        doc = copy.deepcopy(raw)
        _row(doc, "R4")["when"]["signals"] = ["xsmom_top3"]
        with pytest.raises(playbook.PlaybookError):
            _parse(doc)

    def test_dormant_rows_have_a_reason_and_no_capacity(self, raw, pb) -> None:
        dormant = [r for r in pb.rows if r.status == "dormant"]
        assert {r.id for r in dormant} == {"R8a", "R8b"}
        for r in dormant:
            assert r.max_open == 0 and "refuted" in r.dormant_reason
        assert {r.id for r in pb.active_rows()} == set(ROWS) - {"R8a", "R8b"}
        doc = copy.deepcopy(raw)
        _row(doc, "R8a")["max_open"] = 1
        with pytest.raises(playbook.PlaybookError):
            _parse(doc)


class TestNoOptionsExpression:
    def test_tqqq_sqqq_are_logged_never_substituted(self, raw, pb) -> None:
        assert pb.xsmom.no_options_expression == NO_OPTIONS_EXPRESSION
        assert pb.xsmom.policy == "log_never_substitute"
        for r in pb.rows:
            assert not set(r.universe.names) & NO_OPTIONS_EXPRESSION

    def test_a_row_listing_a_leveraged_etf_is_refused(self, raw) -> None:
        doc = copy.deepcopy(raw)
        _row(doc, "R7")["universe"]["names"] = ["SPY", "TQQQ"]
        with pytest.raises(playbook.PlaybookError):
            _parse(doc)

    @pytest.mark.parametrize(
        ("key", "value"), [("policy", "substitute_next"), ("no_options_expression", ["TQQQ"])]
    )
    def test_the_rule_cannot_be_changed(self, raw, key, value) -> None:
        doc = copy.deepcopy(raw)
        doc["xsmom"][key] = value
        with pytest.raises(playbook.PlaybookError):
            _parse(doc)


class TestDrift:
    def test_pinned_excess_and_weights(self, pb) -> None:
        assert pb.drift.signal_weight == Decimal("0.5")
        assert pb.drift.horizon_sessions == 20
        assert pb.drift.excess_20 == {
            "xsmom_top3": Decimal("0.00962"),  # XSMOM-12-1.md no-skip orig-36 top3 h20 holdout cond
            "pead_beat": Decimal("0.0389"),  # PROTOCOL-PEAD.md beat-proxy +6120 USD / 63 / 2500
        }
        for r in pb.rows:
            signal_row = r.status == "active" and bool(r.when.signals)
            assert r.drift_view == ("signal" if signal_row else "none")
            assert r.drift_weight == (Decimal("0.5") if signal_row else Decimal(0))

    @pytest.mark.parametrize(
        ("rid", "view", "weight"),
        [("R4", "none", "0.5"), ("R4", "signal", "0.5"), ("R1", "signal", "0"), ("R1", "none", "0"), ("R1", "signal", "0.6")],
    )  # fmt: skip
    def test_mismatched_drift_is_refused(self, raw, rid, view, weight) -> None:
        doc = copy.deepcopy(raw)
        _row(doc, rid)["drift"] = {"view": view, "weight": weight}
        with pytest.raises(playbook.PlaybookError):
            _parse(doc)

    def test_excess_must_cover_exactly_the_allowed_signals(self, raw) -> None:
        doc = copy.deepcopy(raw)
        doc["drift"]["excess_20"]["volspike"] = "0.01"
        with pytest.raises(playbook.PlaybookError):
            _parse(doc)


class TestExitsHaveNoDefaults:
    @pytest.mark.parametrize(
        "key", ["touch", "breach", "take_profit", "stop_loss", "stop_confirm_ticks", "time_stop"]
    )
    def test_every_exit_key_is_required(self, raw, key: str) -> None:
        for rid in ROWS:
            doc = copy.deepcopy(raw)
            _row(doc, rid)["exits"].pop(key)
            with pytest.raises(playbook.PlaybookError):
                _parse(doc)

    @pytest.mark.parametrize("key", ["hold_sessions", "min_dte", "sessions_after_event"])
    def test_every_time_stop_key_is_required(self, raw, key: str) -> None:
        doc = copy.deepcopy(raw)
        _row(doc, "R1")["exits"]["time_stop"].pop(key)
        with pytest.raises(playbook.PlaybookError):
            _parse(doc)

    def test_a_time_stop_needs_a_bound(self, raw) -> None:
        doc = copy.deepcopy(raw)
        _row(doc, "R4")["exits"]["time_stop"] = {
            "hold_sessions": "none",
            "min_dte": "none",
            "sessions_after_event": "none",
        }
        with pytest.raises(playbook.PlaybookError):
            _parse(doc)

    @pytest.mark.parametrize(
        ("rid", "key", "value"),
        [
            ("R2", "touch", True),  # touch is for long singles and debit verticals
            ("R1", "breach", True),  # breach is for credit kinds
            ("R1", "take_profit", {"basis": "credit_frac", "value": "0.5"}),
            ("R2", "stop_loss", {"basis": "debit_frac", "value": "0.5"}),
            ("R1", "take_profit", {"basis": "width_frac", "value": "1.5"}),  # out of range
            ("R1", "stop_confirm_ticks", 0),
            ("R1", "take_profit", "default"),
        ],
    )
    def test_exits_the_engine_would_refuse_are_refused(self, raw, rid, key, value) -> None:
        doc = copy.deepcopy(raw)
        _row(doc, rid)["exits"][key] = value
        with pytest.raises(playbook.PlaybookError):
            _parse(doc)


class TestSchema:
    @pytest.mark.parametrize(
        "mutate",
        [
            lambda d: d.update(surprise=1),
            lambda d: _row(d, "R1").update(surprise=1),
            lambda d: _row(d, "R1")["when"].update(surprise=1),
            lambda d: _row(d, "R1")["legs"][0].update(surprise=1),
            lambda d: _row(d, "R1").pop("evidence"),
            lambda d: _row(d, "R1").pop("max_open"),
            lambda d: _row(d, "R1").update(tier="gut-feel"),
            lambda d: _row(d, "R4").update(expiry_gap_days=[21, 63]),  # single expiry kind
            lambda d: _row(d, "R5").pop("expiry_gap_days"),
            lambda d: d.update(schema="desk-playbook/2"),
            lambda d: d["rows"].append(copy.deepcopy(_row(d, "R1"))),  # duplicate id
        ],
    )
    def test_off_schema_is_refused(self, raw, mutate) -> None:
        doc = copy.deepcopy(raw)
        mutate(doc)
        with pytest.raises(playbook.PlaybookError):
            _parse(doc)

    @pytest.mark.parametrize(
        ("rid", "index", "key", "value"),
        [
            ("R1", 0, "abs_delta", ["0.30", "0.40"]),  # long leg no longer above the short
            ("R2", 1, "abs_delta", ["0.30", "0.40"]),  # protective leg overlaps the short
            ("R1", 0, "abs_delta", ["0.70", "0.55"]),  # inverted range
            ("R1", 0, "abs_delta", ["0", "0.40"]),
            ("R1", 0, "dte", [180, 120]),
            ("R1", 1, "dte", [120, 180]),  # a vertical's legs share one expiry
            ("R6", 0, "abs_delta", ["0.20", "0.30"]),  # diagonal not protective
            ("R5", 1, "strike", "by_delta"),  # a calendar shares one strike
        ],
    )
    def test_leg_shapes_are_checked(self, raw, rid, index, key, value) -> None:
        doc = copy.deepcopy(raw)
        _row(doc, rid)["legs"][index][key] = value
        with pytest.raises(playbook.PlaybookError):
            _parse(doc)

    def test_vol_condition_needs_a_fidelity_rule(self, raw) -> None:
        doc = copy.deepcopy(raw)
        _row(doc, "R1")["iv_fidelity"] = "not_used"
        with pytest.raises(playbook.PlaybookError):
            _parse(doc)
        doc = copy.deepcopy(raw)
        _row(doc, "R3")["iv_fidelity"] = "validated_only"  # no vol condition to apply it to
        with pytest.raises(playbook.PlaybookError):
            _parse(doc)

    def test_only_risk_control_rows_may_ignore_news(self, raw) -> None:
        doc = copy.deepcopy(raw)
        _row(doc, "R1")["when"]["news"] = "ignore"
        with pytest.raises(playbook.PlaybookError):
            _parse(doc)

"""Desk D6 deal miner: enumeration, entry terms, refusals, selection, the
queue document and its writer.

Oracles are computed here, never by calling the miner: session windows by
slicing the calendar's session list by hand, Black-Scholes deltas from the
fixture's declared smile with math.erf, entry fills and limits in Decimal
from literal quotes, max losses from the kinds' definitions. The integration
tests run the whole miner on the synthetic world of
``tests/fixtures/desk_miner.py`` with every desk path pinned to tmp.
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures import desk_miner as wm
from tests.fixtures import desk_pricing as fx
from tree_options.desk import miner, playbook, pricing, regime, selection
from tree_options.desk.__main__ import run_cli
from tree_options.time.calendar import StaticSessionCalendar
from tree_options.trex.plan import Leg, LegStructure

REPO = Path(__file__).resolve().parents[2]
D = Decimal
SESSION, ENTRY = wm.D, wm.ENTRY
RATE = wm.RATE_PCT / 100.0


@pytest.fixture(scope="module")
def cal() -> StaticSessionCalendar:
    return wm.calendar()


@pytest.fixture(scope="module")
def pb() -> playbook.Playbook:
    return playbook.load_playbook(REPO / "data" / "desk" / "playbook" / "v2.toml")


def _row(pb: playbook.Playbook, rid: str) -> playbook.Row:
    return next(r for r in pb.rows if r.id == rid)


@pytest.fixture(scope="module")
def fast_cfg() -> selection.MinerConfig:
    """The sealed v1 rules at the smallest path count the loader allows."""
    return dataclasses.replace(selection.load_config(REPO / "data" / "desk" / "miner" / "v1.toml"),
                               n_paths=1000)  # fmt: skip


@pytest.fixture(scope="module")
def open_cfg(fast_cfg: selection.MinerConfig) -> selection.MinerConfig:
    """Thresholds nobody would trade on: every rail-passing deal is eligible
    (the queue mechanics are under test, not the proposed thresholds)."""
    return dataclasses.replace(
        fast_cfg,
        selection=selection.Selection(
            min_stress_fill_ev_usd=D("-100000"), min_ev_per_max_loss=D("-100")
        ),
    )


# ------------------------------------------------------------ exit deadline


def _sessions_before(cal: StaticSessionCalendar, d: date) -> list[date]:
    return [s for s in cal.sessions() if s < d]


def test_deadline_is_the_hold_when_it_binds(cal, pb) -> None:
    s = list(cal.sessions())
    first = date(2025, 8, 15)
    got, why = miner.exit_deadline(_row(pb, "R1"), ENTRY, first, cal)
    hold = s[s.index(ENTRY) + 20]
    min_dte = max(x for x in s if x < first and (first - x).days >= 90)
    engine = _sessions_before(cal, first)[-2]
    assert got == min(hold, min_dte, engine) == hold and why == ""


def test_deadline_is_the_min_dte_when_it_binds(cal, pb) -> None:
    s = list(cal.sessions())
    first = date(2025, 6, 20)  # 99 days out: 90 DTE left after 9 days
    got, _ = miner.exit_deadline(_row(pb, "R1"), ENTRY, first, cal)
    want = max(x for x in s if x < first and (first - x).days >= 90)
    assert got == want == date(2025, 3, 21)


def test_deadline_stays_a_session_before_the_engines_expiry_safety(cal, pb) -> None:
    """R3 has no min_dte: the engine's expiry safety (from the last session
    before the first expiry) would pre-empt the 09:45 time stop, so the
    deadline is the session before that last hold session."""
    first = date(2025, 3, 21)
    got, _ = miner.exit_deadline(_row(pb, "R3"), ENTRY, first, cal)
    assert got == _sessions_before(cal, first)[-2] == date(2025, 3, 19)


def test_no_hold_window_is_refused(cal, pb) -> None:
    got, why = miner.exit_deadline(_row(pb, "R3"), ENTRY, date(2025, 3, 17), cal)
    assert got is None and "hold window" in why


def test_event_rows_close_after_the_event_and_need_one(cal, pb) -> None:
    r5 = _row(pb, "R5")
    first = date(2025, 4, 17)
    got, _ = miner.exit_deadline(r5, ENTRY, first, cal, next_event=date(2025, 3, 19))
    assert got == date(2025, 3, 20)  # the first session after the event
    got, why = miner.exit_deadline(r5, ENTRY, first, cal, next_event=None)
    assert got is None and "event" in why


# -------------------------------------------------------------- entry terms


def _leg(right: str, action: str, strike: str, expiry: date = date(2025, 8, 15)) -> Leg:
    return Leg(right=right, action=action, strike=D(strike), expiry=expiry)  # type: ignore[arg-type]


def _q(bid: str | None, ask: str | None) -> tuple[Decimal | None, Decimal | None]:
    return (D(bid) if bid is not None else None, D(ask) if ask is not None else None)


def test_debit_limit_is_the_first_cent_above_the_modeled_fill() -> None:
    legs = [_leg("C", "BUY", "45"), _leg("C", "SELL", "50")]
    t = miner.entry_terms("debit_vertical", legs, [_q("3.10", "3.30"), _q("1.00", "1.10")],
                          fill_k=D("0.5"))  # fmt: skip
    # mid 3.20 - 1.05 = 2.15; half-spreads 0.10 + 0.05; fill 2.15 + 0.075
    assert (t.mid, t.half_spread, t.fill, t.limit) == (D("2.15"), D("0.15"), D("2.225"), D("2.23"))
    whole = miner.entry_terms("debit_vertical", legs, [_q("3.10", "3.30"), _q("1.00", "1.20")],
                              fill_k=D("0.5"))  # fmt: skip
    assert (whole.fill, whole.limit) == (D("2.20"), D("2.21"))  # strictly above


def test_credit_floor_is_the_first_cent_below_the_modeled_credit() -> None:
    legs = [_leg("P", "SELL", "50"), _leg("P", "BUY", "48")]
    t = miner.entry_terms("credit_vertical", legs, [_q("2.00", "2.10"), _q("0.50", "0.60")],
                          fill_k=D("0.5"))  # fmt: skip
    # debit orientation: buy the 50 (2.05), sell the 48 (0.55): 1.50 - 0.05
    assert (t.mid, t.fill, t.limit) == (D("1.50"), D("1.45"), D("1.44"))


@pytest.mark.parametrize(
    ("kind", "legs", "quotes", "code"),
    [
        # the pricing smoke's SPY 735/737: a $3.70 fill on a $2 width
        ("debit_vertical", [("C", "BUY", "735"), ("C", "SELL", "737")],
         [("5.00", "5.60"), ("1.70", "1.90")], "debit_fill_at_or_above_width"),
        # fill 1.995: the cap would be 2.00, the width
        ("debit_vertical", [("C", "BUY", "735"), ("C", "SELL", "737")],
         [("2.50", "2.60"), ("0.565", "0.625")], "cap_at_or_above_width"),
        ("credit_vertical", [("P", "SELL", "50"), ("P", "BUY", "48")],
         [("0.50", "0.60"), ("0.55", "0.65")], "credit_at_or_below_zero"),
        # a credit of exactly 0 after the modeled fill: 0.05 - 0.5 x 0.10
        ("credit_vertical", [("P", "SELL", "50"), ("P", "BUY", "48")],
         [("0.50", "0.60"), ("0.45", "0.55")], "credit_at_or_below_zero"),
        ("credit_vertical", [("P", "SELL", "50"), ("P", "BUY", "48")],
         [("3.00", "3.10"), ("0.10", "0.20")], "credit_at_or_above_width"),
        ("debit_vertical", [("C", "BUY", "45"), ("C", "SELL", "47")],
         [(None, "0.30"), ("0.10", "0.20")], "no_two_sided_quote"),
        ("calendar", [("P", "SELL", "50", date(2025, 4, 17)), ("P", "BUY", "50")],
         [("2.00", "2.10"), ("1.80", "1.90")], "no_debit"),
    ],
)  # fmt: skip
def test_impossible_entries_are_refused_before_any_valuation(
    kind: str, legs: list[tuple[Any, ...]], quotes: list[tuple[str | None, str]], code: str
) -> None:
    with pytest.raises(miner.Refusal) as err:
        miner.entry_terms(kind, [_leg(*g) for g in legs], [_q(*q) for q in quotes],
                          fill_k=D("0.5"))  # fmt: skip
    assert err.value.code == code


def test_a_condors_width_is_its_wider_wing() -> None:
    legs = [_leg("P", "BUY", "40"), _leg("P", "SELL", "45"), _leg("C", "SELL", "55"),
            _leg("C", "BUY", "58")]  # fmt: skip
    # a 5.10 credit (mid 5.20 - 0.5 x 0.20) on the 5-wide put wing (the
    # call wing is 3 wide) is refused
    q = [_q("0.10", "0.20"), _q("2.70", "2.80"), _q("2.70", "2.80"), _q("0.10", "0.20")]
    with pytest.raises(miner.Refusal) as err:
        miner.entry_terms("iron_condor", legs, q, fill_k=D("0.5"))
    assert err.value.code == "credit_at_or_above_width"


# --------------------------------------------------------------- enumerate


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _call_delta(spot: float, k: float, days: int, iv: float, r: float) -> float:
    t = days / 365.0
    return _ncdf((math.log(spot / k) + (r + 0.5 * iv * iv) * t) / (iv * math.sqrt(t)))


def _surface(doc: dict[str, Any]) -> pricing.EntrySurface:
    return pricing.EntrySurface(doc, session=SESSION, rate=RATE)


def test_r1_grid_is_every_long_short_pair_in_the_delta_ranges_per_expiry(pb) -> None:
    doc = wm.chain("AAPL", SESSION, "2025-03-13T03:49:00+00:00")
    protos = miner.enumerate_row(_row(pb, "R1"), "AAPL", _surface(doc), ENTRY)
    spot = wm.SPOT["AAPL"]
    want = set()
    for e in wm.EXPIRIES:
        if not 120 <= (e - ENTRY).days <= 180:
            continue
        deltas = {}
        for k in range(int(spot * 0.6), int(spot * 1.45) + 1):
            dl = _call_delta(spot, k, (e - SESSION).days, wm.smile(math.log(k / spot)), RATE)
            for edge in (0.30, 0.40, 0.55, 0.70):
                assert abs(dl - edge) > 1e-4  # no strike rides a range edge
            deltas[k] = dl
        longs = [k for k, dl in deltas.items() if 0.55 <= dl <= 0.70]
        shorts = [k for k, dl in deltas.items() if 0.30 <= dl <= 0.40]
        want |= {(e, D(f"{a}.0"), D(f"{b}.0")) for a in longs for b in shorts}
    got = {(p.legs[0].expiry, p.legs[0].strike, p.legs[1].strike) for p in protos}
    assert got == want and len(protos) == len(want) and len(want) >= 8
    for p in protos:
        long_, short = p.legs
        assert (long_.right, long_.action, short.right, short.action) == ("C", "BUY", "C", "SELL")
        assert long_.expiry == short.expiry  # same_as_long


def test_duplicate_chain_rows_enumerate_twice_and_dedupe_once(pb) -> None:
    doc = wm.chain("AAPL", SESSION, "2025-03-13T03:49:00+00:00")
    cols = doc["columns"]
    n = len(cols["occ"])
    for c in cols:
        cols[c] = cols[c] + cols[c]  # every row listed twice
    assert len(cols["occ"]) == 2 * n
    protos = miner.enumerate_row(_row(pb, "R1"), "AAPL", _surface(doc), ENTRY)
    unique, dropped = miner.dedupe(protos)
    assert dropped == len(protos) - len(unique) > 0
    assert len({p.key for p in unique}) == len(unique)
    single = miner.enumerate_row(
        _row(pb, "R1"), "AAPL", _surface(wm.chain("AAPL", SESSION, "x")), ENTRY
    )
    assert {p.key for p in unique} == {p.key for p in single}


def _two_iv_chain(sym: str, spot: float, fronts: list[date], backs: list[date],
                  front_iv: float, back_iv: float) -> dict[str, Any]:  # fmt: skip
    """One chain document whose expiries carry different flat IVs."""
    strikes = [float(k) for k in range(int(spot * 0.5), int(spot * 1.6) + 1)]
    docs = [
        fx.chain_doc(sym=sym, session=SESSION, spot=spot, rate=RATE, expiries=exps,
                     strikes=strikes, iv=lambda m, v=v: v, half_spread=wm.half_spread,
                     source_as_of="2025-03-13T03:49:00+00:00")
        for exps, v in ((fronts, front_iv), (backs, back_iv))
    ]  # fmt: skip
    out = docs[0]
    for c in out["columns"]:
        out["columns"][c] = out["columns"][c] + docs[1]["columns"][c]
    return out


def test_r6_diagonal_pairs_respect_the_gap_and_the_actual_strike_rule(pb) -> None:
    r6 = _row(pb, "R6")
    front, back = date(2025, 4, 25), date(2025, 7, 18)  # 43 and 127 DTE, 84 days apart
    far_back = date(2025, 9, 19)  # 190 DTE: outside the back range
    # the playbook's own warning: a 0.60-delta call at a high back IV sits
    # ABOVE a 0.25-0.35 delta call at a low front IV, so |delta| ranges
    # cannot certify the diagonal; only the actual strikes can
    doc = _two_iv_chain("NVDA", 100.0, [front], [back, far_back], 0.20, 1.50)
    protos = miner.enumerate_row(r6, "NVDA", _surface(doc), ENTRY)
    assert protos
    seen = set()
    for p in protos:
        by = {g.action: g for g in p.legs}
        assert (by["BUY"].expiry, by["SELL"].expiry) == (back, front)
        protective = by["BUY"].strike < by["SELL"].strike  # long call strike below
        want = None if protective else "protective_strike_rule"
        assert miner.structural_refusal(r6, p.legs) == want
        seen.add(protective)
    assert seen == {True, False}  # both cases occur on this surface
    calm = _two_iv_chain("NVDA", 100.0, [front], [back], 0.30, 0.30)
    ok = miner.enumerate_row(r6, "NVDA", _surface(calm), ENTRY)
    assert ok and {miner.structural_refusal(r6, p.legs) for p in ok} == {None}


def test_r5_calendar_shares_the_front_strike_within_the_gap(pb) -> None:
    r5 = _row(pb, "R5")
    # fronts take 7-45 DTE, backs 28-108 DTE, 21-63 days after the front
    f1, b1, b2 = date(2025, 3, 28), date(2025, 5, 2), date(2025, 6, 20)  # 15, 50, 99 DTE
    doc = _two_iv_chain("SPY", 60.0, [f1], [b1, b2], 0.20, 0.20)
    protos = miner.enumerate_row(r5, "SPY", _surface(doc), ENTRY)
    assert protos
    gaps = set()
    for p in protos:
        by = {g.action: g for g in p.legs}
        assert by["BUY"].strike == by["SELL"].strike and by["SELL"].expiry == f1
        assert (by["BUY"].right, by["SELL"].right) == ("P", "P")
        gaps.add((by["BUY"].expiry - f1).days)
    assert gaps == {35}  # 06-20 is 84 days after the front: beyond the gap


def test_r4_condor_takes_four_delta_picked_legs_of_one_expiry(pb) -> None:
    r4 = _row(pb, "R4")
    doc = _two_iv_chain("IWM", 100.0, [date(2025, 4, 25)], [date(2025, 7, 18)], 0.30, 0.30)
    protos = miner.enumerate_row(r4, "IWM", _surface(doc), ENTRY)
    assert protos
    for p in protos:
        assert len(p.legs) == 4 and {g.expiry for g in p.legs} == {date(2025, 4, 25)}
        puts = sorted(g.strike for g in p.legs if g.right == "P")
        calls = sorted(g.strike for g in p.legs if g.right == "C")
        assert puts[0] < puts[1] < calls[0] < calls[1]


# ------------------------------------------------------------- the vol gate


def _vol(state: str, reason: str, ratio: float | None, n: int) -> regime.VolState:
    band = playbook.VolBand(cheap_below=Fraction(1, 5), rich_above=Fraction(4, 5))
    return regime.VolState(state, reason, ratio, None, n, "unvalidated", band)


def test_r1_skips_the_vol_gate_only_during_the_warm_up(pb) -> None:
    r1 = _row(pb, "R1")
    warm = _vol("NOT_EVALUABLE", "warm-up: 6/120 sessions of history", 0.84, 6)
    assert miner.vol_gate(r1, warm, pb) is None
    for bad in (
        _vol("NOT_EVALUABLE", "no HAR forecast", None, 0),
        _vol("NOT_EVALUABLE", "HAR status 'degraded', not 'validated'", None, 0),
    ):
        why = miner.vol_gate(r1, bad, pb)
        assert (
            why is not None and why.startswith("vol_not_evaluable_not_warmup") and bad.reason in why
        )
    # an evaluable state is the regime's to judge; rows without the
    # exemption never reach the miner with NOT_EVALUABLE
    assert miner.vol_gate(r1, _vol("fair", "", 0.8, 150), pb) is None


# -------------------------------------------------------- the whole miner


def _run(tmp_path: Path, monkeypatch, cal, w: wm.World, cfg: selection.MinerConfig,
         **kw: Any) -> miner.MineResult:  # fmt: skip
    where = wm.materialize(w, tmp_path / "world")
    wm.env(monkeypatch, where)
    kw.setdefault("dry_run", True)
    return miner.run_mine(session=SESSION, now=wm.NOW, cal=cal, config=cfg, **kw)


@pytest.fixture(scope="module")
def base_run(cal, open_cfg, tmp_path_factory) -> dict[str, Any]:
    mp = pytest.MonkeyPatch()
    try:
        res = _run(tmp_path_factory.mktemp("base"), mp, cal, wm.base(cal), open_cfg)
    finally:
        mp.undo()
    assert res.exit_code == 0, res.detail
    assert res.payload is not None
    return res.payload


def test_the_queue_names_its_session_window_and_digests(base_run) -> None:
    q = base_run
    assert q["schema"] == "trex.deal/1"
    assert (q["session"], q["entry_session"]) == ("2025-03-12", "2025-03-13")
    assert q["decision_cutoff"] == "2025-03-13T09:30:00-04:00"
    assert q["valid_until"] == "2025-03-13T11:30:00-04:00"
    assert q["playbook"]["sha256"] == playbook.APPROVED["v2.toml"]
    assert q["miner"]["sha256"] == selection.APPROVED["v1.toml"]
    assert q["miner"]["status"] == "PROPOSED"
    assert q["inputs"]["timing_vintage"]["session"] == "2025-03-12"
    # TQQQ has no options expression: logged, QQQ (ranked after it) not promoted
    assert q["no_options_expression"] == {"names": ["TQQQ"], "policy": "log_never_substitute"}
    assert set(q["rows"]["R1"]["matched"]) == {"AAPL", "QQQ"}


def _money(x: Any) -> bool:
    return isinstance(x, str) and Decimal(x).is_finite()


def test_every_admissible_deal_is_a_valid_spec_with_its_stated_max_loss(base_run) -> None:
    adm = base_run["admissible"]
    assert adm, "the fixture must queue at least one deal"
    assert [d["rank"] for d in adm] == list(range(1, len(adm) + 1))
    for d in adm:
        spec = LegStructure.model_validate(d["structure"])
        assert spec.deal_id == d["deal_id"] == spec.id
        assert spec.ref_mid == Decimal(d["ref_mid"]) and spec.limit == Decimal(d["limit"])
        for k in ("limit", "ref_mid", "max_loss", "fill"):
            assert _money(d[k]), k
        # recomputed == stated: debit kinds lose the cap x 100 x qty
        assert spec.kind == "debit_vertical"
        assert Decimal(d["max_loss"]) == spec.limit * 100 * spec.quantity
        assert spec.entry_date == ENTRY and spec.exit_deadline.isoformat() == d["exit_deadline"]
        assert d["rails"]["ok"] is True and d["status"] == "admissible"
        assert d["valuation"]["limit_ok"] is True
        assert any("warm-up" in n for n in d["notes"])  # R1's vol gate skipped, said so
        assert _money(d["decision"]["ev"]) and d["decision"]["basis"] == "signal"


def test_the_rationale_is_the_fixed_template(base_run) -> None:
    d = base_run["admissible"][0]
    r = d["rationale"]
    v = d["valuation"]
    assert r.startswith(
        f"R1 (signal-validated, playbook v2): debit_vertical x1 on {d['underlying']}"
    )
    for phrase in (
        f"limit of {d['limit']} per package",
        f"max loss ${d['max_loss']}",
        f"to the {d['exit_deadline']} CLOSE",
        "the engine's time stop fires at 09:45 ET",
        "European Black-Scholes, q = 0",
        f"IV mean reversion {v['iv_mean_reversion']['status']}",
        f"No-view EV ${v['ev']} beside signal EV ${v['ev_signal']}",
    ):
        assert phrase in r, phrase
    assert re.fullmatch(r"[ -~]+", r)  # ASCII, one line, no model text


def test_every_candidate_is_surfaced_with_its_reasons(base_run) -> None:
    q = base_run
    rows = q["rows"]["R1"]
    listed = [d for d in q["admissible"] + q["surfaced"] if d["row"] == "R1"]
    assert len(listed) == rows["candidates"]
    assert rows["valued"] + sum(rows["refused"].values()) == rows["candidates"]
    assert rows["admissible"] == len([d for d in q["admissible"] if d["row"] == "R1"])
    for d in q["surfaced"]:
        assert d["status"] in ("refused", "rail_failed", "not_selected", "capacity")
        assert d["reasons"]
        if d["status"] == "rail_failed":
            assert d["valuation"] is not None and d["rails"]["ok"] is False
    # wide spreads risk more than $500: the per-trade rail says so
    assert rows["rail_failed"].get("max_loss_per_trade", 0) > 0


def test_the_queue_is_jointly_admissible(base_run) -> None:
    """R1 (max_open 3) and R3 (max_open 4) both have eligible deals; the
    queue holds at most 3 (admissions per session), at most 2 per
    underlying, and whatever lost out says which joint rule stopped it."""
    q = base_run
    adm = q["admissible"]
    assert len(adm) == 3
    per_name = {
        n: sum(1 for d in adm if d["underlying"] == n) for n in {d["underlying"] for d in adm}
    }
    assert max(per_name.values()) <= 2
    assert {d["row"] for d in q["admissible"] + q["surfaced"]} >= {"R1", "R3"}
    joint = [r for d in q["surfaced"] if d["status"] == "capacity" for r in d["reasons"]]
    assert all(r.split(":")[0] in ("joint_rails", "max_open", "duplicate_structure") for r in joint)
    assert any("admissions_per_session: FAIL" in r for r in joint)
    assert any("per_underlying: FAIL" in r for r in joint)


def test_the_proposed_thresholds_are_what_gate_the_queue(
    cal, fast_cfg, tmp_path, monkeypatch
) -> None:
    res = _run(tmp_path, monkeypatch, cal, wm.base(cal), fast_cfg)
    assert res.exit_code == 0 and res.payload is not None
    for d in res.payload["admissible"]:
        dec = d["decision"]
        assert Decimal(dec["ev_stress_fill"]) > 0
        assert Decimal(dec["ev"]) >= Decimal("0.05") * Decimal(d["max_loss"])
    for d in res.payload["surfaced"]:
        if d["status"] == "not_selected":
            assert set(d["reasons"]) <= {"stress_fill_ev_not_positive", "ev_per_max_loss_below_min"}


def test_a_structure_two_rows_share_is_queued_once(cal, open_cfg, tmp_path, monkeypatch) -> None:
    """QQQ's only expiry sits 120 days out: inside R1's 120-180 and R3's
    60-120 DTE windows, with the same |delta| legs, so both rows enumerate
    the same structures. The queue takes each structure once."""
    w = wm.base(cal)
    only = (date(2025, 7, 11),)
    assert (only[0] - ENTRY).days == 120
    w.chains = [
        (wm.chain("QQQ", SESSION, doc["header"]["source_as_of"], expiries=only), c)
        if doc["header"]["underlying"] == "QQQ"
        else (doc, c)
        for doc, c in w.chains
    ]
    res = _run(tmp_path, monkeypatch, cal, w, open_cfg)
    assert res.payload is not None
    q = res.payload

    def legs(d: dict[str, Any]) -> tuple[Any, ...]:
        return tuple((g["right"], g["action"], g["strike"], g["expiry"]) for g in d["legs"])

    queued = {legs(d): d for d in q["admissible"] if d["underlying"] == "QQQ"}
    dups = [
        d
        for d in q["surfaced"]
        if d["status"] == "capacity" and d["reasons"][0].startswith("duplicate_structure")
    ]
    assert dups
    for d in dups:
        twin = queued[legs(d)]
        assert twin["row"] != d["row"] and d["reasons"][0].endswith(twin["deal_id"])
    for s in queued:  # no structure is queued twice
        assert sum(1 for d in q["admissible"] if legs(d) == s) == 1


def test_a_valuation_whose_fill_breaches_the_limit_is_refused(
    cal, open_cfg, tmp_path, monkeypatch
) -> None:
    """The pricer reports limit_ok; the miner enforces it (refuse, never
    re-limit). A limit 2 cents inside the modeled fill must refuse every
    deal that would otherwise have been valued."""
    real = miner.entry_terms

    def tight(*a: Any, **kw: Any) -> miner.EntryTerms:
        t = real(*a, **kw)
        return dataclasses.replace(t, limit=t.limit - Decimal("0.02"))

    monkeypatch.setattr(miner, "entry_terms", tight)
    res = _run(tmp_path, monkeypatch, cal, wm.base(cal), open_cfg)
    assert res.payload is not None and res.payload["admissible"] == []
    refused = [d for d in res.payload["surfaced"] if d["reasons"][0].startswith("limit_not_ok")]
    assert refused and all(d["status"] == "refused" for d in refused)
    assert all(d["valuation"]["limit_ok"] is False for d in refused)
    r1 = res.payload["rows"]["R1"]
    assert r1["valued"] == 0 and r1["refused"]["limit_not_ok"] == r1["candidates"]


def test_a_vol_state_not_evaluable_for_another_reason_refuses_r1(
    cal, open_cfg, tmp_path, monkeypatch
) -> None:
    w = wm.base(cal)
    w.features[SESSION.isoformat()]["names"]["AAPL"]["forecast"]["har_status"] = "degraded"
    res = _run(tmp_path, monkeypatch, cal, w, open_cfg)
    assert res.payload is not None
    r1 = res.payload["rows"]["R1"]
    assert "AAPL" not in r1["matched"]
    [refused] = [m for m in r1["match_refused"] if m["name"] == "AAPL"]
    assert refused["reason"].startswith("vol_not_evaluable_not_warmup")
    assert "degraded" in refused["reason"]
    assert all(d["underlying"] != "AAPL" for d in res.payload["admissible"])


def test_a_rows_open_positions_count_against_its_max_open(
    cal, open_cfg, tmp_path, monkeypatch
) -> None:
    specs = tmp_path / "specs"
    specs.mkdir()
    rules = _row(playbook.load_playbook(REPO / "data" / "desk" / "playbook" / "v2.toml"),
                 "R1").exits.rules  # fmt: skip
    exp = date(2025, 7, 18)  # on the fixture's SPY chain: the specs' greeks price
    for k in range(3):  # R1 allows 3 open: all taken by desk positions (on SPY)
        sid = f"d-20250301-R1-SPY-{k:012d}"
        spec = LegStructure(
            id=sid, underlying="SPY", kind="debit_vertical",
            legs=(_leg("C", "BUY", f"{60 + k}", exp), _leg("C", "SELL", f"{64 + k}", exp)),
            quantity=1, entry_date=date(2025, 3, 3), exit_deadline=date(2025, 4, 1),
            limit=D("1.00"), exits=rules, deal_id=sid, ref_mid=D("0.95"),
        )  # fmt: skip
        (specs / f"{sid}.json").write_text(spec.model_dump_json())
    res = _run(tmp_path, monkeypatch, cal, wm.base(cal), open_cfg, desk_specs=specs)
    assert res.payload is not None
    assert [d["row"] for d in res.payload["admissible"]] and all(
        d["row"] != "R1" for d in res.payload["admissible"]
    )  # R3 (its own capacity) still queues
    assert res.payload["rows"]["R1"]["open"] == 3
    assert res.payload["rows"]["R1"]["capacity"].get("max_open", 0) > 0
    assert res.payload["rows"]["R3"]["open"] == 0


# ------------------------------------------------------------ the writer


def test_a_live_run_writes_the_queue_once_and_marks_the_stage(
    cal, fast_cfg, tmp_path, monkeypatch
) -> None:
    res = _run(tmp_path, monkeypatch, cal, wm.base(cal), fast_cfg, dry_run=False)
    assert (res.exit_code, res.status) == (0, "written"), res.detail
    queue = tmp_path / "world" / "state" / "queue" / "2025-03-12.json"
    marker = tmp_path / "world" / "state" / "stages" / "2025-03-12" / "mine.done.json"
    first = queue.read_bytes()
    assert json.loads(first)["schema"] == "trex.deal/1" and marker.exists()
    again = miner.run_mine(session=SESSION, now=wm.NOW, cal=cal, config=fast_cfg)
    assert (again.exit_code, again.status) == (0, "already_done")
    # a lost marker: the identical recomputation re-marks, never rewrites
    marker.unlink()
    same = miner.run_mine(session=SESSION, now=wm.NOW, cal=cal, config=fast_cfg)
    assert (same.exit_code, same.status) == (0, "exists") and marker.exists()
    assert queue.read_bytes() == first


def test_a_differing_rerun_is_a_conflict_beside_the_queue(
    cal, fast_cfg, tmp_path, monkeypatch
) -> None:
    res = _run(tmp_path, monkeypatch, cal, wm.base(cal), fast_cfg, dry_run=False)
    assert res.status == "written"
    queue = tmp_path / "world" / "state" / "queue" / "2025-03-12.json"
    first = queue.read_bytes()
    (tmp_path / "world" / "state" / "stages" / "2025-03-12" / "mine.done.json").unlink()
    other = dataclasses.replace(fast_cfg, n_paths=1001)  # a different valuation
    bad = miner.run_mine(session=SESSION, now=wm.NOW, cal=cal, config=other)
    assert (bad.exit_code, bad.status) == (1, "conflict")
    assert queue.read_bytes() == first
    conflicts = list((tmp_path / "world" / "state" / "queue-conflicts").glob("2025-03-12.*.json"))
    assert len(conflicts) == 1


def test_a_dry_run_writes_nothing_but_its_out_file(cal, fast_cfg, tmp_path, monkeypatch) -> None:
    w = wm.base(cal)
    w.vintages = {}  # no vintage yet: a live run would snapshot one
    where = wm.materialize(w, tmp_path / "world")
    wm.env(monkeypatch, where)

    def files() -> dict[str, bytes]:
        # the panel's shared-lock file is created by every panel reader
        return {
            str(p): p.read_bytes()
            for p in (tmp_path / "world").rglob("*")
            if p.is_file() and p.name != "ohlc-panel.json.lock"
        }

    before = files()
    out = tmp_path / "out" / "q.json"
    res = miner.run_mine(session=SESSION, now=wm.NOW, cal=cal, config=fast_cfg,
                         dry_run=True, out=out)  # fmt: skip
    assert res.exit_code == 0 and res.status == "dry_run"
    assert files() == before
    doc = json.loads(out.read_text())
    # the in-memory vintage stood in for the one a live run would write
    assert doc["inputs"]["timing_vintage"]["session"] == "2025-03-12"
    assert res.timing_status == "dry_run"


def test_a_run_after_the_cutoff_cannot_snapshot_and_says_so(
    cal, fast_cfg, tmp_path, monkeypatch
) -> None:
    w = wm.base(cal)
    w.vintages = {}
    late = datetime(2025, 3, 13, 10, 0, tzinfo=wm.ET)
    where = wm.materialize(w, tmp_path / "world")
    wm.env(monkeypatch, where)
    res = miner.run_mine(session=SESSION, now=late, cal=cal, config=fast_cfg, dry_run=True)
    assert res.payload is not None and res.timing_status == "too_late"
    # no vintage on or before D: no timing knowledge at all (never today's
    # file), so the reporter's schedule is unknown and AAPL cannot be valued
    assert res.payload["inputs"]["timing_vintage"] == {"session": None, "sha256": None}
    assert not (where["store"] / "earnings-timing").exists()
    aapl = [d for d in res.payload["surfaced"] if d["underlying"] == "AAPL"]
    assert aapl and all(d["status"] == "refused" for d in aapl)
    assert all("earnings schedule" in " ".join(d["reasons"]) for d in aapl)


def test_a_slot_without_chains_still_takes_the_vintage(
    cal, fast_cfg, tmp_path, monkeypatch
) -> None:
    """The vintage of D can only be taken before D's cutoff: a 07:15 slot
    whose chains are not in yet must still record it (a chain landing at
    12:30 is mined against it, not against an older one)."""
    w = wm.base(cal)
    w.vintages, w.chains = {}, []
    res = _run(tmp_path, monkeypatch, cal, w, fast_cfg, dry_run=False)
    assert (res.exit_code, res.status, res.timing_status) == (3, "not_ready", "written")
    vintage = tmp_path / "world" / "store" / "earnings-timing" / "2025-03-12.json"
    assert json.loads(vintage.read_text())["timing"] == wm.TIMING


def test_inputs_not_ready_is_exit_3(cal, fast_cfg, tmp_path, monkeypatch) -> None:
    w = wm.base(cal)
    w.features.pop(SESSION.isoformat())
    res = _run(tmp_path, monkeypatch, cal, w, fast_cfg)
    assert (res.exit_code, res.status) == (3, "not_ready") and "features" in res.detail
    w2 = wm.base(cal)
    w2.chains = []
    res2 = _run(tmp_path, monkeypatch, cal, w2, fast_cfg)
    assert (res2.exit_code, res2.status) == (3, "not_ready") and "chains" in res2.detail


# ------------------------------------------------- D's signals must prove themselves


def _stale(w: wm.World) -> None:  # written before the panel reached D
    prev = date(2025, 3, 11)
    w.signals[SESSION.isoformat()] = wm.signals_doc(
        SESSION, ["AAPL", "TQQQ", "QQQ"], ["QQQ"], panel_last=prev
    )


def _other_session(w: wm.World) -> None:  # another session's file under D's name
    other = date(2025, 3, 11)
    w.signals[SESSION.isoformat()] = wm.signals_doc(other, ["AAPL", "TQQQ", "QQQ"], ["QQQ"])


def _garbage(w: wm.World) -> None:
    w.signals[SESSION.isoformat()] = ["not", "a", "signals", "document"]  # type: ignore[assignment]


def _missing(w: wm.World) -> None:
    w.signals.pop(SESSION.isoformat())


@pytest.mark.parametrize(
    ("spoil", "why"),
    [
        (_missing, "no signals file"),
        (_stale, "panel_last_session 2025-03-11"),
        (_other_session, "session '2025-03-11'"),
        (_garbage, "not a signals document"),
    ],
)
def test_signals_not_ready_write_no_queue_and_no_marker(
    cal, fast_cfg, tmp_path, monkeypatch, spoil: Any, why: str
) -> None:
    """eod-equity writes D's signals at 16:40/20:40 ET with an 08:40 ET
    catch-up; a slot before them must NOT finalize a signal-less queue (the
    signal rows are the desk's only live rows): exit 3, no queue, no
    marker, so the next slot retries. The vintage is still taken."""
    w = wm.base(cal)
    w.vintages = {}
    spoil(w)
    res = _run(tmp_path, monkeypatch, cal, w, fast_cfg, dry_run=False)
    assert (res.exit_code, res.status) == (3, "not_ready")
    assert "signals" in res.detail and why in res.detail
    assert res.payload is None and res.timing_status == "written"
    state = tmp_path / "world" / "state"
    assert not (state / "queue").exists()
    assert not (state / "stages" / SESSION.isoformat() / "mine.done.json").exists()
    assert (tmp_path / "world" / "store" / "earnings-timing" / "2025-03-12.json").exists()


def test_a_dry_run_does_not_mine_without_ready_signals(
    cal, fast_cfg, tmp_path, monkeypatch
) -> None:
    w = wm.base(cal)
    _missing(w)
    res = _run(tmp_path, monkeypatch, cal, w, fast_cfg, out=tmp_path / "q.json")
    assert (res.exit_code, res.status) == (3, "not_ready")
    assert not (tmp_path / "q.json").exists()


def test_the_retry_after_the_signals_land_writes_the_queue(
    cal, fast_cfg, tmp_path, monkeypatch
) -> None:
    w = wm.base(cal)
    _missing(w)
    first = _run(tmp_path, monkeypatch, cal, w, fast_cfg, dry_run=False)
    assert (first.exit_code, first.status) == (3, "not_ready")
    # eod-equity's 08:40 catch-up writes the file; the 09:15 slot retries
    doc = wm.signals_doc(SESSION, ["AAPL", "TQQQ", "QQQ"], ["QQQ"])
    signals = tmp_path / "world" / "state" / "signals" / "2025-03-12.json"
    signals.parent.mkdir(parents=True, exist_ok=True)
    signals.write_text(json.dumps(doc))
    later = datetime(2025, 3, 13, 9, 15, tzinfo=wm.ET)
    res = miner.run_mine(session=SESSION, now=later, cal=cal, config=fast_cfg)
    assert (res.exit_code, res.status) == (0, "written"), res.line()
    assert res.payload is not None
    assert res.payload["inputs"]["signals"]["status"] == "ok"
    assert set(res.payload["rows"]["R1"]["matched"]) == {"AAPL", "QQQ"}
    marker = tmp_path / "world" / "state" / "stages" / "2025-03-12" / "mine.done.json"
    assert marker.exists()


def test_current_signals_proceed(base_run) -> None:
    q = base_run
    assert q["inputs"]["signals"]["status"] == "ok" and q["inputs"]["signals"]["sha256"]


# --------------------------------------------------------------------- CLI


def test_cli_mine_dry_run_to_an_out_path(cal, tmp_path, monkeypatch, capsys) -> None:
    where = wm.materialize(wm.base(cal), tmp_path / "world")
    wm.env(monkeypatch, where)
    out = tmp_path / "q.json"
    rc = run_cli(["mine", "--session", "2025-03-12", "--dry-run", "--names", "AAPL,SPY",
                  "--out", str(out)], now=wm.NOW, cal=cal)  # fmt: skip
    assert rc == 0
    doc = json.loads(out.read_text())
    assert doc["inputs"]["universe"] == ["AAPL", "SPY"]
    assert "R1" in capsys.readouterr().out
    assert not (where["state"] / "queue").exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["mine", "--session", "2025-03-15"],  # a Saturday
        ["mine", "--session", "2025-03-13"],  # not closed at NOW
        ["mine", "--session", "2025-03-12", "--names", "AAPL"],  # a partial LIVE queue
        ["mine", "--session", "2025-03-12", "--names", "AAPL,tqqq!", "--dry-run"],
    ],
)
def test_cli_mine_refuses_bad_arguments(cal, tmp_path, monkeypatch, argv: list[str]) -> None:
    where = wm.materialize(wm.base(cal), tmp_path / "world")
    wm.env(monkeypatch, where)
    assert run_cli(argv, now=wm.NOW, cal=cal) == 2

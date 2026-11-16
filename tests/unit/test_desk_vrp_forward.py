"""VRP-COND-FORWARD-001: the report-only forward gate-state emitter
(desk/vrp_forward.py, `python -m tree_options.desk vrp-cond-forward`).

Two layers:

* MACHINERY (fast, synthetic): the percentile pools and their boundaries,
  the 2/3 threshold semantics, write-once publication, the refuse-and-mark
  path, the card-event annotation, and the report-only construction
  (import lint + write confinement). Every desk path is pinned to tmp.
* ACCEPTANCE (real inputs, read-only, skipped when the serving checkout is
  absent): the frozen-base derivation must reproduce the sealed anchors of
  VRP-COND-FORWARD-001 §3.3 EXACTLY -- n=452, mean 0.973, p25 0.903,
  p50 0.966, p75 1.030, p90 1.103 -- and the forward tail must land on the
  pre-seal RT-5 measurement (mean 0.949 on its 32 sessions). Constants are
  never tuned to fit: this test FAILS on any upstream calendar/HAR/IV-window
  drift, by design.
"""

from __future__ import annotations

import ast
import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from tree_options.desk import vrp_forward
from tree_options.desk.vrp_forward import (
    FORWARD_FIRST,
    PCT_MIN_N,
    PCT_WINDOW,
    TER_HI,
    EmitOutcome,
    EmitterRefusal,
    annotate_card_events,
    builder_sha256,
    emit_session,
    gate_path,
    load_base,
    pool_forward_only,
    pool_mixed_fullbase,
    pool_primary,
)

SERVE = Path("/home/alexk/documents/tree_options")
LIVE_STATE = Path("/home/alexk/.local/state/trex-desk")
# RT-5's pre-seal forward tail: the chain corpus minus the 2026-10-23
# features-gap session its features-side iv30 source did not carry
RT5_FORWARD_N = 32
RT5_EXCLUDED = {date(2026, 10, 23)}
CORPUS_LAST = date(2026, 11, 13)  # the chain corpus as sealed alongside the prereg


@pytest.fixture(autouse=True)
def _pinned_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DESK_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("TREX_DESK_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("DESK_PAPER_DIR", str(tmp_path / "paper"))
    monkeypatch.delenv("DESK_REPO_ROOT", raising=False)


# ---------------------------------------------------------------- helpers


def _fake_inputs(static_calendar) -> vrp_forward.Inputs:
    from tree_options.desk import ivhist
    from tree_options.time.calendar import calendar_content_sha256

    return vrp_forward.Inputs(
        cal=static_calendar,
        panel={},
        panel_sha256="0" * 64,
        earnings={},
        earnings_sha256="0" * 64,
        ivhist={"names": {"IWM": {"sessions": {}}}},
        ivhist_sha256=vrp_forward.IVHIST_SHA256,
        rates=ivhist.RateSource.constant(0.04),
        rate_label="declared-constant 0.04 (test)",
        calendar_sha256=calendar_content_sha256(static_calendar),
    )


def _emit(
    state: Path,
    inputs,
    base_series: dict[date, float],
    session: date,
    *,
    iv30: float | None = 0.2,
    harvol: float | None = 0.21,
    prior: dict[date, float] | None = None,
    builder: str | None = None,
) -> EmitOutcome:
    """emit_session with both legs injected (the real legs are exercised by
    the acceptance test against the serving checkout)."""
    base = vrp_forward.BaseRecord(series=base_series, doc={"anchors": {"pass": True}})
    harvol_map = {session: (harvol, {"h": 20})} if harvol is not None else {}
    chain = state / "chains" / session.isoformat() / "IWM.json.gz"
    chain.parent.mkdir(parents=True, exist_ok=True)
    chain.write_bytes(b"")
    real_chain_iv30 = vrp_forward.chain_iv30
    real_builder = vrp_forward.builder_sha256

    def fake_chain(path, session_, rates):
        if iv30 is None:
            return None, {"reason": "no ATM 30d IV on this chain (no bracketing expiry)"}
        return iv30, {"chain_file": str(path), "rate": 0.04}

    vrp_forward.chain_iv30 = fake_chain
    if builder is not None:
        vrp_forward.builder_sha256 = lambda: builder  # type: ignore[assignment]
    try:
        return emit_session(
            session,
            inputs=inputs,
            base=base,
            state_root=state,
            harvol=harvol_map,
            forward_prior=prior or {},
            chain_path=chain,
        )
    finally:
        vrp_forward.chain_iv30 = real_chain_iv30
        vrp_forward.builder_sha256 = real_builder  # type: ignore[assignment]


def _tail_sessions(static_calendar, n: int) -> list[date]:
    return list(static_calendar.sessions()[-n:])


# ---------------------------------------------------------------- pools


def test_primary_pool_min_n_boundary_and_window_inclusive(static_calendar) -> None:
    sessions = _tail_sessions(static_calendar, 420)
    at = sessions[-1]
    window = sessions[-PCT_WINDOW:]
    # evaluable r on exactly the last PCT_MIN_N sessions of the window
    series: dict[date, float] = {s: 1.0 for s in window[-PCT_MIN_N:]}
    assert len([s for s in window if s in series]) == PCT_MIN_N
    read = pool_primary(series, at, static_calendar)
    assert read["status"] == "evaluable"
    assert read["n"] == PCT_MIN_N
    # strictly-lower: the other 125 below, today's own value equal -> 125/126
    for s in window[-PCT_MIN_N:-1]:
        series[s] = 0.5
    read = pool_primary(series, at, static_calendar)
    assert read["p"] == pytest.approx(125 / 126)
    # one fewer evaluable -> n/a (the registered n >= 126 floor)
    del series[window[-2]]
    read = pool_primary(series, at, static_calendar)
    assert read["p"] is None
    assert read["status"].startswith("n/a")
    assert read["n"] == PCT_MIN_N - 1
    # a session with no evaluable r refuses, never guesses
    read = pool_primary(series, window[-2], static_calendar)
    assert read["p"] is None
    assert read["status"] == "no evaluable r at session"


def test_threshold_is_two_thirds_inclusive() -> None:
    # mixed pool with n=3: exactly 2 strictly lower -> p == 2/3 -> ON
    frozen = {date(2026, 1, 1): 0.9, date(2026, 1, 2): 0.95}
    forward = {date(2026, 1, 3): 1.0}
    read = pool_mixed_fullbase(frozen, forward, date(2026, 1, 3))
    assert read["p"] == pytest.approx(TER_HI)
    assert read["on"] is True  # ON iff p >= 2/3: >=, never >
    assert read["within_band"] is True  # sitting on the threshold is inside the band
    # p == 1/3 -> OFF, and far enough from 2/3 to be pool-choice-stable
    forward = {date(2026, 1, 3): 0.92}
    read = pool_mixed_fullbase(frozen, forward, date(2026, 1, 3))
    assert read["p"] == pytest.approx(1 / 3)
    assert read["on"] is False
    assert read["within_band"] is False


def test_mixed_pool_ranks_against_frozen_base_and_forward_to_date() -> None:
    frozen = {date(2026, 1, 1): 0.9, date(2026, 1, 2): 1.1, date(2026, 1, 3): 1.2}
    forward = {date(2026, 1, 4): 1.0, date(2026, 1, 5): 1.15}
    read = pool_mixed_fullbase(frozen, forward, date(2026, 1, 5))
    # pool = the frozen base + all forward obs to date (window-inclusive):
    # 1.15 ranks above 0.9, 1.0 and 1.1 -> 3/5
    assert read["n"] == 5
    assert read["p"] == pytest.approx(3 / 5)


def test_forward_only_pool_is_na_until_126(static_calendar) -> None:
    sessions = _tail_sessions(static_calendar, 320)
    at = sessions[-1]
    forward = {s: 1.0 for s in sessions[-(PCT_MIN_N - 1) :]}
    read = pool_forward_only(forward, at, static_calendar)
    assert read["p"] is None
    assert "126" in read["status"]
    forward[sessions[-PCT_MIN_N]] = 1.0
    read = pool_forward_only(forward, at, static_calendar)
    assert read["status"] == "evaluable"
    assert read["n"] == PCT_MIN_N


# ---------------------------------------------------------------- write-once


def test_emit_writes_gate_once_and_same_builder_rerun_is_noop(
    static_calendar, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    inputs = _fake_inputs(static_calendar)
    at = FORWARD_FIRST
    oc = _emit(state, inputs, {}, at, prior={})
    assert (oc.action, oc.status) == ("written", "ok")
    first = gate_path(state, at).read_bytes()
    doc = json.loads(first)
    assert doc["builder_sha256"] == builder_sha256()
    oc = _emit(state, inputs, {}, at, prior={at: 0.95})
    assert oc.action == "no-op"
    assert gate_path(state, at).read_bytes() == first  # history is never rewritten


def test_different_builder_sha_refuses_to_rewrite_history(static_calendar, tmp_path: Path) -> None:
    state = tmp_path / "state"
    inputs = _fake_inputs(static_calendar)
    at = FORWARD_FIRST
    _emit(state, inputs, {}, at)
    first = gate_path(state, at).read_bytes()
    oc = _emit(state, inputs, {}, at, builder="f" * 64)
    assert oc.action == "conflict"
    assert "history is never rewritten" in oc.detail
    assert gate_path(state, at).read_bytes() == first


def test_builder_sha_applies_forward_only(static_calendar, tmp_path: Path) -> None:
    state = tmp_path / "state"
    inputs = _fake_inputs(static_calendar)
    d1 = FORWARD_FIRST
    d2 = static_calendar.sessions()[static_calendar.ordinal(d1) + 1]
    _emit(state, inputs, {}, d1)
    # a NEW builder may emit the NEXT session...
    oc = _emit(state, inputs, {}, d2, prior={}, builder="a" * 64)
    assert (oc.action, oc.status) == ("written", "ok")
    assert json.loads(gate_path(state, d2).read_text())["builder_sha256"] == "a" * 64
    # ...but never rewrite the old one
    oc = _emit(state, inputs, {}, d1, prior={}, builder="a" * 64)
    assert oc.action == "conflict"
    assert json.loads(gate_path(state, d1).read_text())["builder_sha256"] == builder_sha256()


# ------------------------------------------------------------- refuse-and-mark


def test_session_without_usable_surface_is_refused_and_marked(
    static_calendar, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    inputs = _fake_inputs(static_calendar)
    at = FORWARD_FIRST
    oc = _emit(state, inputs, {}, at, iv30=None)  # chain exists, surface unusable
    assert oc.status == "refused"
    doc = json.loads(gate_path(state, at).read_text())
    assert doc["status"] == "refused"
    assert "no ATM 30d IV" in doc["reason"]
    assert doc["r"] is None
    assert all(p["p"] is None for p in doc["pools"].values())  # never stale carry
    assert doc["warmup"]["n_forward_r_to_date"] == 0
    oc = _emit(state, inputs, {}, at, iv30=None)  # the refusal itself is write-once
    assert oc.action == "no-op"
    # a missing HAR forecast refuses the same way
    other = static_calendar.sessions()[static_calendar.ordinal(at) + 2]
    oc = _emit(state, inputs, {}, other, harvol=None, prior={at: 0.95})
    assert oc.status == "refused"
    assert "HAR" in json.loads(gate_path(state, other).read_text())["reason"]


def test_session_before_the_declared_break_refuses(static_calendar, tmp_path: Path) -> None:
    state = tmp_path / "state"
    inputs = _fake_inputs(static_calendar)
    before = static_calendar.sessions()[static_calendar.ordinal(FORWARD_FIRST) - 1]
    oc = _emit(state, inputs, {}, before)
    assert oc.action == "refused"
    assert not gate_path(state, before).exists()


# ------------------------------------------------------------------ anchors


def test_a_base_that_misses_the_sealed_anchors_refuses_everything(
    static_calendar, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    inputs = _fake_inputs(static_calendar)
    monkeypatch.setattr(vrp_forward, "frozen_base_series", lambda inputs: {date(2026, 1, 1): 1.0})
    with pytest.raises(EmitterRefusal, match="sealed anchors"):
        load_base(inputs, state)
    assert not vrp_forward.base_r_path(state).exists()  # never written, never tuned


def test_a_pinned_base_is_reused_and_never_rewritten(
    static_calendar, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    inputs = _fake_inputs(static_calendar)
    # pin a base with a FOREIGN builder sha; a new run reuses it untouched
    path = vrp_forward.base_r_path(state)
    pinned = {
        "schema": vrp_forward.BASE_SCHEMA,
        "study": vrp_forward.STUDY,
        "series": [["2026-01-05", 0.97]],
        "n": 1,
        "anchors": {"pass": True},
        "builder_sha256": "9" * 64,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pinned, indent=1, sort_keys=True) + "\n")
    monkeypatch.setattr(vrp_forward, "frozen_base_series", lambda inputs: {date(2026, 1, 1): 1.0})
    base = load_base(inputs, state)
    assert base.series == {date(2026, 1, 5): 0.97}
    assert json.loads(path.read_text())["builder_sha256"] == "9" * 64


# --------------------------------------------------------------- card events


def _write_gate(state: Path, session: date, *, r: float, p: float, n_forward: int) -> None:
    path = gate_path(state, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": vrp_forward.GATE_SCHEMA,
                "study": vrp_forward.STUDY,
                "session": session.isoformat(),
                "status": "ok",
                "r": r,
                "pools": {"primary": {"p": p, "n": 200, "status": "evaluable", "on": p >= TER_HI}},
                "warmup": {
                    "met": n_forward >= vrp_forward.WARMUP_MIN_FORWARD,
                    "card_disposition": "conditionable"
                    if n_forward >= vrp_forward.WARMUP_MIN_FORWARD
                    else "DEFERRED-UNCONDITIONED",
                },
                "builder_sha256": builder_sha256(),
            }
        )
    )


def test_card_events_annotate_xe_at_t_and_xp_at_t_minus_1(static_calendar, tmp_path: Path) -> None:
    state = tmp_path / "state"
    t = date(2026, 11, 2)
    prev = static_calendar.sessions()[static_calendar.ordinal(t) - 1]  # 2026-10-30
    _write_gate(state, t, r=1.0346, p=0.807, n_forward=24)
    _write_gate(state, prev, r=1.0345, p=0.905, n_forward=23)
    picks = ["AMD", "NVDA", "TSLA"]
    sig = state / "signals" / f"{t.isoformat()}.json"
    sig.parent.mkdir(parents=True)
    sig.write_text(
        json.dumps(
            {
                "schema": "desk-signals/1",
                "session": t.isoformat(),
                "xsmom": {"fires": True, "is_rebalance_day": True, "top3": picks},
                "provenance": {"generated_at": "2026-11-02T21:00:00+00:00"},
            }
        )
    )
    # a non-fired session and a pre-break session annotate nothing
    for name, doc in (
        ("2026-11-03", {"xsmom": {"fires": False, "top3": []}}),
        ("2026-09-22", {"xsmom": {"fires": True, "top3": ["AMD"]}}),
    ):
        p = state / "signals" / name
        p.write_text(json.dumps({"schema": "desk-signals/1", "xsmom": doc["xsmom"]}))
    annotate_card_events(state, static_calendar)
    lines = [
        json.loads(line) for line in vrp_forward.card_events_path(state).read_text().splitlines()
    ]
    assert [line["name"] for line in lines] == picks
    for line in lines:
        assert line["xe_mkt_hi"] == {
            "read_at": "t",
            "pool": "primary",
            "p": pytest.approx(0.807),
            "on": True,
            "gate_status": "ok",
            "within_band": None,
        }
        assert line["xp_size_mkt"]["read_at"] == "t-1"
        assert line["xp_size_mkt"]["prev_session"] == prev.isoformat()
        assert line["xp_size_mkt"]["p"] == pytest.approx(0.905)
        assert line["xp_size_mkt"]["dictated_size"] == 0.5  # p(t-1) >= 2/3 -> half size
        assert line["warmup"]["disposition"] == "DEFERRED-UNCONDITIONED"
        assert "excluded-pre-registration" in line["accrual"]  # §4: t <= registration
    # idempotent: a second pass appends nothing
    annotate_card_events(state, static_calendar)
    assert len(vrp_forward.card_events_path(state).read_text().splitlines()) == len(picks)
    # a post-registration rebalance lands in the accrual
    t2 = date(2026, 12, 1)
    prev2 = static_calendar.sessions()[static_calendar.ordinal(t2) - 1]
    _write_gate(state, t2, r=1.0, p=0.30, n_forward=40)
    _write_gate(state, prev2, r=1.0, p=0.31, n_forward=39)
    p = state / "signals" / f"{t2.isoformat()}.json"
    p.write_text(json.dumps({"xsmom": {"fires": True, "top3": ["KO"]}}))
    annotate_card_events(state, static_calendar)
    lines = [
        json.loads(line) for line in vrp_forward.card_events_path(state).read_text().splitlines()
    ]
    last = lines[-1]
    assert last["accrual"] == "in-accrual"
    assert last["xe_mkt_hi"]["on"] is False
    assert last["xp_size_mkt"]["dictated_size"] == 1.0
    assert last["xp_size_mkt"]["prev_session"] == prev2.isoformat()


def test_card_events_without_a_gate_report_the_gap(tmp_path, static_calendar) -> None:
    state = tmp_path / "state"
    t = date(2026, 12, 1)
    sig = state / "signals" / f"{t.isoformat()}.json"
    sig.parent.mkdir(parents=True)
    sig.write_text(json.dumps({"xsmom": {"fires": True, "top3": ["KO"]}}))
    notes = annotate_card_events(state, static_calendar)
    assert any("no gate file" in note for note in notes)
    assert not vrp_forward.card_events_path(state).exists()


# ----------------------------------------------------- report-only by construction


def test_no_card_path_imports_the_emitter() -> None:
    """REPORT-ONLY by construction: nothing under the eod_equity / enter /
    seal paths (nor anything else outside desk/__main__.py) imports
    vrp_forward."""
    offenders: list[str] = []
    for py in (REPO_SRC := Path(__file__).resolve().parents[2] / "src").rglob("*.py"):
        rel = py.relative_to(REPO_SRC).as_posix()
        if rel in ("tree_options/desk/vrp_forward.py", "tree_options/desk/__main__.py"):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any("vrp_forward" in n for n in names):
                offenders.append(rel)
    assert not offenders


def test_the_emitter_imports_nothing_that_mutates_desk_state() -> None:
    src = (Path(__file__).resolve().parents[2] / "src/tree_options/desk/vrp_forward.py").read_text()
    tree = ast.parse(src)
    allowed = {
        "tree_options.desk",
        "tree_options.desk.har",
        "tree_options.desk.ivhist",
        "tree_options.desk.paths",
        "tree_options.desk.surface",
        "tree_options.desk.panel",
        "tree_options.desk.sessions",
        "tree_options.desk.store",
        "tree_options.time.calendar",
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("tree_options")
        ):
            assert node.module in allowed, f"unexpected import {node.module}"
        elif isinstance(node, ast.Import):
            for a in node.names:
                assert not a.name.startswith("tree_options"), f"unexpected import {a.name}"


def test_the_emitter_writes_only_inside_its_own_state_directory(
    static_calendar, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    inputs = _fake_inputs(static_calendar)
    at = FORWARD_FIRST
    # fixtures first (the injected chain stand-in and a signals doc)...
    chain = state / "chains" / at.isoformat() / "IWM.json.gz"
    chain.parent.mkdir(parents=True)
    chain.write_bytes(b"")
    t = static_calendar.sessions()[static_calendar.ordinal(at) + 1]
    (state / "signals").mkdir(parents=True, exist_ok=True)
    (state / "signals" / f"{t.isoformat()}.json").write_text(
        json.dumps({"xsmom": {"fires": True, "top3": ["KO"]}})
    )
    _write_gate(state, t, r=1.0, p=0.5, n_forward=1)
    monkeypatch.setattr(
        vrp_forward, "chain_iv30", lambda path, session, rates: (0.2, {"rate": 0.04})
    )
    before = {p for p in state.rglob("*") if p.is_file()}
    # ...then the emitter's own actions may add nothing outside vrp-cond-forward/
    from tree_options.desk.vrp_forward import forward_r_from_gates

    emit_session(
        at,
        inputs=inputs,
        base=vrp_forward.BaseRecord(series={date(2026, 1, 1): 0.9}, doc={}),
        state_root=state,
        harvol={at: (0.21, {"h": 20})},
        forward_prior=forward_r_from_gates(state),
        chain_path=chain,
    )
    annotate_card_events(state, static_calendar)
    added = {p for p in state.rglob("*") if p.is_file()} - before
    assert added, "the emitter wrote nothing"
    for p in added:
        assert p.is_relative_to(vrp_forward.state_dir(state)), f"write outside the state area: {p}"


# ------------------------------------------------------------------ the CLI


def test_cli_requires_exactly_one_mode(tmp_path: Path) -> None:
    from tree_options.desk.__main__ import run_cli

    assert run_cli(["vrp-cond-forward"]) == 2
    assert run_cli(["vrp-cond-forward", "--session", "2026-11-02", "--catch-up", "2026-09-22"]) == 2


def test_cli_session_without_a_recorded_chain_writes_nothing(
    tmp_path: Path, static_calendar
) -> None:
    from tree_options.desk.__main__ import run_cli

    rc = run_cli(["vrp-cond-forward", "--session", "2026-11-02"], cal=static_calendar)
    assert rc == 3
    # the CLI's own per-command lock may exist; the emitter itself wrote nothing
    assert not vrp_forward.state_dir(tmp_path / "state").exists()


def test_cli_catch_up_with_no_recorded_sessions(tmp_path: Path, static_calendar) -> None:
    from tree_options.desk.__main__ import run_cli

    rc = run_cli(["vrp-cond-forward", "--catch-up", "2026-09-22"], cal=static_calendar)
    assert rc == 3


# ------------------------------------------------------- acceptance (real inputs)


@pytest.mark.skipif(
    not (SERVE / "artifacts/desk-store/iv-history/vwap_atm.json").is_file(),
    reason="the serving checkout is not present on this host",
)
def test_frozen_base_reproduces_the_sealed_anchors_and_the_forward_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing acceptance gate (lane brief): derive the frozen base
    from the serving checkout's real inputs and check the sealed anchors of
    VRP-COND-FORWARD-001 §3.3/§3.4/§4 EXACTLY; then catch up the whole
    recorded chain corpus and check the forward tail and the two named gate
    reads. All writes land in a tmp TREX_DESK_STATE; the serving checkout is
    read-only."""
    monkeypatch.setenv("DESK_STORE", str(SERVE / "artifacts/desk-store"))
    monkeypatch.setenv("DESK_PAPER_DIR", str(SERVE / "artifacts/paper-trades"))
    state = tmp_path / "state"
    monkeypatch.setenv("TREX_DESK_STATE", str(state))
    from tree_options.trex.clock import session_calendar

    cal = session_calendar()
    sessions = [
        d
        for d in vrp_forward.recorded_chain_sessions(SERVE / "artifacts/desk-store")
        if FORWARD_FIRST <= d <= CORPUS_LAST
    ]
    assert len(sessions) == 33  # 2026-09-22..2026-11-13, the corpus at seal
    assert date(2026, 9, 23) not in sessions  # never recorded
    assert date(2026, 10, 23) in sessions  # the features-gap session HAS chains

    # the recorded XSMOM signal docs of the two rebalances (read-only copies)
    sig_dir = state / "signals"
    sig_dir.mkdir(parents=True)
    copied: list[date] = []
    for d in (date(2026, 10, 1), date(2026, 11, 2)):
        src = LIVE_STATE / "signals" / f"{d.isoformat()}.json"
        if src.is_file():
            shutil.copy(src, sig_dir / src.name)
            copied.append(d)

    outcomes = vrp_forward.run_emit(sessions=sessions, cal=cal, state_root=state)
    assert {oc.action for oc in outcomes} == {"written"}

    base = json.loads(vrp_forward.base_r_path(state).read_text())
    assert base["schema"] == vrp_forward.BASE_SCHEMA
    assert base["anchors"]["pass"] is True
    obs = base["anchors"]["observed"]
    assert obs["n"] == 452
    for key, sealed in (
        ("mean", 0.973),
        ("p25", 0.903),
        ("p50", 0.966),
        ("p75", 1.030),
        ("p90", 1.103),
    ):
        assert abs(obs[key] - sealed) <= 5.001e-4, (key, obs[key], sealed)

    gates = {oc.session: json.loads(gate_path(state, oc.session).read_text()) for oc in outcomes}
    assert all(doc["status"] == "ok" for doc in gates.values())
    rs = [gates[d]["r"] for d in sessions]
    # RT-5's pre-seal tail: the 32 sessions its features-side iv30 carried
    rt5 = [gates[d]["r"] for d in sessions if d not in RT5_EXCLUDED]
    assert len(rt5) == RT5_FORWARD_N
    assert abs(sum(rt5) / len(rt5) - 0.949) <= 6e-4  # RT-5 measured 0.9490
    assert abs(min(rs) - 0.781) <= 1.5e-3  # 2026-11-04
    assert abs(max(rs) - 1.091) <= 1.5e-3  # 2026-11-03

    # the two named gate reads (§4): OFF at 2026-10-01, ON at 2026-11-02,
    # under BOTH the primary pool and the mixed sensitivity pool
    off = gates[date(2026, 10, 1)]["pools"]
    assert off["primary"]["on"] is False
    assert abs(off["a_mixed_fullbase"]["p"] - 0.546) <= 3e-3
    on = gates[date(2026, 11, 2)]["pools"]
    assert on["primary"]["on"] is True
    assert abs(on["a_mixed_fullbase"]["p"] - 0.772) <= 3e-3

    # warm-up (§3.5): 33 < 50 forward r-obs -> every card is DEFERRED-UNCONDITIONED
    for doc in gates.values():
        assert doc["warmup"]["met"] is False
        assert doc["warmup"]["card_disposition"] == "DEFERRED-UNCONDITIONED"

    if copied:
        lines = [
            json.loads(line)
            for line in vrp_forward.card_events_path(state).read_text().splitlines()
        ]
        assert {line["session"] for line in lines} == {d.isoformat() for d in copied}
        for line in lines:
            assert "excluded-pre-registration" in line["accrual"]  # §4 disclosure
            assert line["xe_mkt_hi"]["p"] is not None
        by_session = {line["session"]: line for line in lines}
        # 2026-10-01's t-1 (09-30) is recorded: xp reads a real percentile
        line = by_session["2026-10-01"]
        assert line["xp_size_mkt"]["prev_session"] == "2026-09-30"
        assert line["xp_size_mkt"]["p"] is not None
        assert line["xp_size_mkt"]["dictated_size"] in (0.5, 1.0)
        # 2026-11-02's t-1 (10-30) is a corpus-gap session: no gate, never
        # stale carry -- the annotation records the gap, size undictated
        line = by_session["2026-11-02"]
        assert line["xp_size_mkt"]["prev_session"] == "2026-10-30"
        assert line["xp_size_mkt"]["p"] is None
        assert line["xp_size_mkt"]["dictated_size"] is None
        assert "no gate" in line["xp_size_mkt"]["status"]

    # re-running the whole catch-up with the same builder is a pure no-op
    again = vrp_forward.run_emit(sessions=sessions, cal=cal, state_root=state)
    assert {oc.action for oc in again} == {"no-op"}

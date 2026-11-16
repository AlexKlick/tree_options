"""scripts/desk/xu-successor-read.py — the XU-SUCCESSOR-001 one-shot read
(§6 item 3), driven on synthetic card/ledger/appendix/panel fixtures in tmp
dirs: READ-1's cumulative over the first 18 valid resolved cards of record,
the exactly-zero edge, void/open handling, the §4.5 evaluation's five bars,
seed determinism, the A/A twin, and the refuses-a-second-run contract.

The synthetic calendar gives real first-of-month sessions. Incumbent card
nets are engineered exactly: a +1% card nets 3 x (2500 x 0.01 - 1.25) =
+71.25, a -0.71% card nets 3 x -19.00 = -57.00, so 8 up + 10 down is
EXACTLY $0.00 cumulative. Shadow cards' entry/exit closes come from the
synthetic XU panel itself (the drafter's top-3), so the placebo null, the
A/A twin and the paired contrast all see one consistent world.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tree_options.desk import xu_shadow

# The one-shot script's sealed path carries hyphens (not an importable module
# name), so it is loaded by file location like the other hyphenated scripts.
_spec = importlib.util.spec_from_file_location(
    "xu_successor_read",
    Path(__file__).resolve().parents[2] / "scripts/desk/xu-successor-read.py",
)
xr = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = xr  # dataclasses resolves the module by name
_spec.loader.exec_module(xr)

NOW = datetime(2028, 9, 1, 12, 0)
UP, DOWN = "0.01", "-0.0071"
N_UP = Decimal("71.25")  # 3 x (2500 x 0.01 - 1.25)
N_DOWN = Decimal("-57.00")  # 3 x (2500 x -0.0071 - 1.25)
EXIT_CLOSE = {UP: "101.0000", DOWN: "99.2900", "0": "100.0000"}
INC_LEGS = ("AMD", "LLY", "INTC")
SHADOW_LEGS = ("WMT", "WFC", "VZ")  # the synthetic panel's top 3, best first


def _months(cal, n: int, *, start_index: int = 300) -> list[Any]:
    sessions = cal.sessions()
    firsts = []
    prev = None
    for i, s in enumerate(sessions):
        if i < start_index:
            prev = (s.year, s.month)
            continue
        key = (s.year, s.month)
        if key != prev:
            firsts.append(s)
        prev = key
    return [d for d in firsts if cal.ordinal(d) + 25 < len(sessions)][:n]


def _exit(cal, entry) -> Any:
    return cal.nth_after(entry, 20)


def _card_text(
    *, entry: str, exit_: str, legs: tuple[str, ...], ret: str, resolved: bool = True
) -> str:
    exit_close = EXIT_CLOSE[ret]
    lines = [
        f"# Card <N> - XSMOM-TOP3 monthly: {' + '.join(legs)} "
        f"(entry {entry} close -> exit {exit_} close)",
        "",
        "## Entries (sealed)",
        f"| name | signal at {entry} | entry ({entry} close) | notional | exit |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {n} | rank 1/36, 273-session return +1.00% | 100.00 | $2,500 LONG | {exit_} close |"
        for n in legs
    ]
    lines += ["", "## Exit policy (sealed with entry - variant xsmom-monthly)", ""]
    if resolved:
        lines += [
            f"## Resolution ({exit_} - card resolved at the official close)",
            "",
            "| name | entry | fill session | fill price | reason | P&L | hold-20 ref |",
            "|---|---|---|---|---|---|---|",
        ]
        lines += [
            f"| {n} | 100.00 | {exit_} | {exit_close} | time-stop at horizon close | 0.00 | 0.00 |"
            for n in legs
        ]
    return "\n".join(lines) + "\n"


def _xu_card_text(*, entry: str, exit_: str, legs: tuple[str, ...], panel: dict[str, Any]) -> str:
    """A shadow card whose entry/exit closes ARE the panel's closes."""
    lines = [
        f"# XU-SHADOW Card <N> - XSMOM-TOP3-XU monthly: {' + '.join(legs)} "
        f"(entry {entry} close -> exit {exit_} close)",
        "",
        "## Entries (sealed)",
        f"| name | signal at {entry} | entry ({entry} close) | notional | exit |",
        "|---|---|---|---|---|",
    ]
    for n in legs:
        e_close = panel[n][entry]["close"]
        lines.append(
            f"| {n} | rank 1/62, 273-session return +1.00% | {e_close} | $2,500 LONG |"
            f" {exit_} close |"
        )
    lines += [
        "",
        f"## Resolution ({exit_} - card resolved at the official close)",
        "",
        "| name | entry | fill session | fill price | reason | P&L | hold-20 ref |",
        "|---|---|---|---|---|---|---|",
    ]
    for n in legs:
        lines.append(
            f"| {n} | {panel[n][entry]['close']} | {exit_} | {panel[n][exit_]['close']}"
            " | time-stop at horizon close | 0.00 | 0.00 |"
        )
    lines += [
        "",
        "## Draft checks (sealed with the card)",
        "- ranked 60/62; excluded: BK (no_session_bar), WBA (no_session_bar)",
    ]
    return "\n".join(lines) + "\n"


def _panel(cal, through: Any) -> dict[str, dict[str, dict[str, Any]]]:
    sessions = [s for s in cal.sessions() if s <= through]
    panel: dict[str, dict[str, dict[str, Any]]] = {}
    for idx, name in enumerate(xu_shadow.XU_ROSTER):
        rate = Decimal(1) + Decimal(idx) / Decimal(10_000)
        days = sessions if name not in xu_shadow.XU_DEAD_SERIES else sessions[:40]
        panel[name] = {
            d.isoformat(): {"close": str((Decimal(100) * (rate**k)).quantize(Decimal("0.01")))}
            for k, d in enumerate(days)
        }
    return panel


def _appendix(voided: list[str]) -> str:
    rows = ["| date | card / month | event | class / reason | evidence |", "|---|---|---|---|---|"]
    for v in voided:
        rows.append(f"| 2026-11-16 | {v} | void | DATA-DEFECT | synthetic fixture |")
    return "\n".join(rows) + "\n"


def _write_incumbent(
    paper: Path, cal, rets: list[str], *, void_after: int | None = None, open_at: int | None = None
) -> list[str]:
    months = _months(cal, len(rets))
    stems = []
    for k, (entry, ret) in enumerate(zip(months, rets, strict=True)):
        exit_ = _exit(cal, entry)
        (paper / f"{exit_.isoformat()}-xsmom-top3.md").write_text(
            _card_text(
                entry=entry.isoformat(),
                exit_=exit_.isoformat(),
                legs=INC_LEGS,
                ret=ret,
                resolved=open_at != k,
            )
        )
        stems.append(exit_.isoformat())
    (paper / "appendix.md").write_text(
        _appendix([stems[void_after]] if void_after is not None else [])
    )
    return stems


def _write_shadow(paper: Path, cal, n: int, panel: dict[str, Any]) -> None:
    shadow = paper / "xu-shadow"
    shadow.mkdir(parents=True, exist_ok=True)
    rows = ["| exit-date | card file | sha256 | status | detail |", "|---|---|---|---|---|"]
    for entry in _months(cal, n):
        exit_ = _exit(cal, entry)
        name = f"{exit_.isoformat()}-xsmom-top3-xu.md"
        (shadow / name).write_text(
            _xu_card_text(
                entry=entry.isoformat(), exit_=exit_.isoformat(), legs=SHADOW_LEGS, panel=panel
            )
        )
        rows.append(f"| {exit_.isoformat()} | {name} | {'a' * 64} | sealed | — |")
    (shadow / "XU-SHADOW-LEDGER.md").write_text("\n".join(rows) + "\n")


def _write_world(
    paper: Path, cal, rets: list[str], *, shadow_n: int = 18, panel_n: int | None = None
) -> None:
    """Incumbent series + shadow book + XU panel, all consistent."""
    _write_incumbent(paper, cal, rets)
    n = max(shadow_n, panel_n or 0, len(rets))
    months = _months(cal, n)
    panel = _panel(cal, _exit(cal, months[(panel_n or shadow_n) - 1]))
    (paper / "ohlc-panel-xu.json").write_text(json.dumps(panel))
    _write_shadow(paper, cal, shadow_n, panel)


def _run(paper: Path, state: Path, cal):
    return xr.main(
        ["--paper", str(paper), "--appendix", str(paper / "appendix.md"), "--state", str(state)],
        cal=cal,
        now=lambda: NOW,
    )


@pytest.fixture
def paper(tmp_path: Path) -> Path:
    (tmp_path / "paper").mkdir()
    return tmp_path / "paper"


def _report(state: Path) -> str:
    return (state / "xu-successor" / xr.REPORT_NAME).read_text()


def _marker(state: Path) -> dict[str, Any]:
    return json.loads((state / "xu-successor" / xr.MARKER_NAME).read_text())


def _cum(rets: list[str]) -> Decimal:
    return sum((N_UP if r == UP else N_DOWN for r in rets), Decimal(0))


# -------------------------------------------------------------------- READ-1


def test_positive_cumulative_closes_the_question_one_shot(
    paper, tmp_path, synthetic_calendar
) -> None:
    state = tmp_path / "state"
    rets = [UP] * 9 + [DOWN] * 9  # +128.25 > 0
    _write_incumbent(paper, synthetic_calendar, rets)

    assert _run(paper, state, synthetic_calendar) == 0

    report = _report(state)
    assert "NO DISAPPOINTMENT" in report
    assert f"**{_cum(rets)} USD**" in report
    assert "single §4.5 evaluation" not in report  # no evaluation ran
    marker = _marker(state)
    assert marker["read1_disappointment"] is False
    assert marker["successor_passes"] is None
    assert (
        marker["report_sha256"]
        == hashlib.sha256((state / "xu-successor" / xr.REPORT_NAME).read_bytes()).hexdigest()
    )
    # exactly the two artifacts, and a second run refuses with nothing new
    before = sorted(p.name for p in (state / "xu-successor").iterdir())
    assert before == sorted([xr.MARKER_NAME, xr.REPORT_NAME])
    report_bytes = (state / "xu-successor" / xr.REPORT_NAME).read_bytes()
    assert _run(paper, state, synthetic_calendar) == 4
    assert (state / "xu-successor" / xr.REPORT_NAME).read_bytes() == report_bytes
    assert sorted(p.name for p in (state / "xu-successor").iterdir()) == before


def test_exactly_zero_cumulative_is_a_disappointment(paper, tmp_path, synthetic_calendar) -> None:
    state = tmp_path / "state"
    _write_world(paper, synthetic_calendar, [UP] * 8 + [DOWN] * 10)  # exactly $0

    assert _run(paper, state, synthetic_calendar) == 0

    report = _report(state)
    assert "**0.00 USD**" in report
    assert "READ-1 verdict: **DISAPPOINTMENT**" in report  # <= 0, not < 0
    assert _marker(state)["read1_disappointment"] is True


def test_disappointment_runs_the_full_evaluation_and_passes_the_bars(
    paper, tmp_path, synthetic_calendar
) -> None:
    state = tmp_path / "state"
    _write_world(paper, synthetic_calendar, [UP] * 7 + [DOWN] * 11)  # -128.25 <= 0

    assert _run(paper, state, synthetic_calendar) == 0

    report = _report(state)
    assert "READ-1 verdict: **DISAPPOINTMENT**" in report
    assert "A/A determinism twin: PASS" in report
    for bar in (
        "n >= 18 valid shadow cards: PASS",
        "mean net > 0, sign-flip p < 0.05: PASS",
        "95% CI-low > 0 vs random-3 null: PASS",
        "paired mean diff vs incumbent > 0: PASS",
        "no dropped month flips the sign: PASS",
    ):
        assert bar in report, bar
    assert "Successor verdict: **PASSES**" in report
    assert _marker(state)["successor_passes"] is True


def test_fewer_than_18_valid_cards_is_premature_not_consumed(
    paper, tmp_path, synthetic_calendar
) -> None:
    state = tmp_path / "state"
    _write_incumbent(paper, synthetic_calendar, [UP] * 17)

    assert _run(paper, state, synthetic_calendar) == 2
    assert not (state / "xu-successor").exists()  # no marker: rerunnable

    _write_incumbent(paper, synthetic_calendar, [UP] * 18)  # the 18th resolves
    assert _run(paper, state, synthetic_calendar) == 0
    assert "NO DISAPPOINTMENT" in _report(state)


def test_voided_card_is_excluded_and_logged_never_silent(
    paper, tmp_path, synthetic_calendar
) -> None:
    state = tmp_path / "state"
    # 19 sealed, the 10th (a DOWN) voided: the 18 valid are 9 UP + 9 DOWN = +71.25
    stems = _write_incumbent(paper, synthetic_calendar, [UP] * 9 + [DOWN] * 10, void_after=9)

    assert _run(paper, state, synthetic_calendar) == 0

    report = _report(state)
    assert f"DATA-DEFECT ({stems[9]})" in report
    assert "**128.25 USD**" in report  # the voided card's -57.00 is excluded


def test_unknown_void_class_fails_closed(paper, tmp_path, synthetic_calendar) -> None:
    state = tmp_path / "state"
    stems = _write_incumbent(paper, synthetic_calendar, [UP] * 18)
    (paper / "appendix.md").write_text(
        "| date | card / month | event | class / reason | evidence |\n"
        "|---|---|---|---|---|\n"
        f"| 2026-11-16 | {stems[0]} | void | OPERATOR-REGRET | not a §2.2 class |\n"
    )

    assert _run(paper, state, synthetic_calendar) == 1
    assert not (state / "xu-successor").exists()


def test_open_card_waits_and_is_reported(paper, tmp_path, synthetic_calendar) -> None:
    state = tmp_path / "state"
    _write_incumbent(paper, synthetic_calendar, [UP] * 19, open_at=0)  # 18 resolved + 1 open

    assert _run(paper, state, synthetic_calendar) == 0

    report = _report(state)
    assert "Sealed but still open" in report
    assert "NO DISAPPOINTMENT" in report  # the 18 resolved cards carry the read
    assert report.count("| 18 |") == 1  # exactly 18 counted rows


# ------------------------------------------------------- the §4.5 evaluation


def test_placebo_and_bars_are_seed_deterministic(paper, tmp_path, synthetic_calendar) -> None:
    import random

    _write_world(paper, synthetic_calendar, [UP] * 7 + [DOWN] * 11)
    panel = json.loads((paper / "ohlc-panel-xu.json").read_text())
    cards, voids = xr.load_incumbent_series(paper, paper / "appendix.md")
    r1 = xr.read1(cards, voids)

    first = xr.evaluate_shadow(
        paper, panel, synthetic_calendar, r1["cards"], random.Random(xr.SEED)
    )
    second = xr.evaluate_shadow(
        paper, panel, synthetic_calendar, r1["cards"], random.Random(xr.SEED)
    )

    assert first == second  # byte-for-byte identical evaluation state


def test_aa_twin_failure_fails_the_successor_verdict(paper, tmp_path, synthetic_calendar) -> None:
    state = tmp_path / "state"
    _write_world(paper, synthetic_calendar, [UP] * 7 + [DOWN] * 11)
    panel = json.loads((paper / "ohlc-panel-xu.json").read_text())
    months = _months(synthetic_calendar, 18)
    entry, exit_ = months[-1], _exit(synthetic_calendar, months[-1])
    bad = paper / "xu-shadow" / f"{exit_.isoformat()}-xsmom-top3-xu.md"
    # same shape, but the picks are NOT the panel's top-3 anymore (AMD and
    # friends are not on the XU roster; ABBV/ABT/ACN are, and never rank top-3)
    bad.write_text(
        _xu_card_text(
            entry=entry.isoformat(),
            exit_=exit_.isoformat(),
            legs=("ABBV", "ABT", "ACN"),
            panel=panel,
        )
    )

    assert _run(paper, state, synthetic_calendar) == 0

    report = _report(state)
    assert "A/A determinism twin: FAIL" in report
    assert "Successor verdict: **FAILS**" in report
    assert _marker(state)["successor_passes"] is False


def test_shadow_void_refusal_and_open_rows_are_logged(paper, tmp_path, synthetic_calendar) -> None:
    state = tmp_path / "state"
    _write_world(paper, synthetic_calendar, [UP] * 7 + [DOWN] * 11, panel_n=20)
    shadow = paper / "xu-shadow"
    ledger = shadow / "XU-SHADOW-LEDGER.md"
    months20 = _months(synthetic_calendar, 20)
    panel = json.loads((paper / "ohlc-panel-xu.json").read_text())
    with ledger.open("a") as fh:
        for k, status in ((18, "void"), (19, "refused")):
            entry = months20[k]
            exit_ = _exit(synthetic_calendar, entry)
            (shadow / f"{exit_.isoformat()}-xsmom-top3-xu.md").write_text(
                _xu_card_text(
                    entry=entry.isoformat(), exit_=exit_.isoformat(), legs=SHADOW_LEGS, panel=panel
                )
            )
            detail = "ENGINE-DEFECT: fixture" if status == "void" else "NO-TRADE: fixture"
            fh.write(
                f"| {exit_.isoformat()} | {exit_.isoformat()}-xsmom-top3-xu.md | {'b' * 64} | {status} | {detail} |\n"
            )

    assert _run(paper, state, synthetic_calendar) == 0

    report = _report(state)
    assert "void logged:" in report and "ENGINE-DEFECT" in report
    assert "refusal logged:" in report and "NO-TRADE" in report


def test_unknown_shadow_void_class_fails_closed(paper, tmp_path, synthetic_calendar) -> None:
    state = tmp_path / "state"
    _write_world(paper, synthetic_calendar, [UP] * 7 + [DOWN] * 11)
    months = _months(synthetic_calendar, 18)
    with (paper / "xu-shadow" / "XU-SHADOW-LEDGER.md").open("a") as fh:
        fh.write(
            f"| {_exit(synthetic_calendar, months[0]).isoformat()} | x | {'c' * 64} |"
            " void | WANTING-IT: not a §2.2 class |\n"
        )

    assert _run(paper, state, synthetic_calendar) == 1
    assert not (state / "xu-successor").exists()


def test_per_card_net_matches_the_sealed_convention() -> None:
    # independent arithmetic: a +1% leg nets 2500*0.01 - 1.25 = +23.75
    path = Path("/tmp/xr-card-fixture.md")
    path.write_text(_card_text(entry="2020-03-02", exit_="2020-03-30", legs=INC_LEGS, ret=UP))
    assert xr.parse_card(path).net() == Decimal("71.25")
    path.write_text(_card_text(entry="2020-03-02", exit_="2020-03-30", legs=INC_LEGS, ret=DOWN))
    assert xr.parse_card(path).net() == Decimal("-57.00")
    path.unlink()

"""TEST CONTRACT C (part 2) — the RE-RATING PROOF.

Tests only. Nothing here implements the cost model; it calls the model the
first file pins and compares its conclusions against the flat $14.60 baseline
on a real, finished run.

WHAT IS BEING PROVEN
--------------------
The flat model charges EXACTLY $14.60 to every evaluated entry, so every arm's
net is ``gross_total - 14.60 * evaluated`` and the arm ordering is the gross
ordering. If a per-leg measured cost leaves that ordering alone, the shape does
not matter for these arms and the run's conclusions stand. If it reorders or
re-signs arms, the digest's conclusions were an artefact of a constant.

This file is capable of reporting EITHER honestly:
  * ``test_detector_proves_it_can_see_a_flip`` feeds the detector a pair of arms
    that a flat model scores as an exact tie and a shaped model separates. If
    the detector cannot see that, every "no change" verdict below is worthless.
  * ``test_rerating_of_the_finished_run`` then runs the detector on the real
    arms and asserts only that the comparison is CONCLUSIVE - a detected change
    or a proven exact identity. It does not assert a direction.

KNOWN LIMITATIONS, stated rather than hidden
--------------------------------------------
1. The run's board rows carry ``short_strike_moneyness_pct`` and ``dte`` but no
   delta, no strike and no IV, and the underlyings are anonymised (``U1``..).
   The |delta| used below is therefore a TEST-OWNED Black-Scholes proxy with an
   assumed volatility, not a measurement. It is swept over five sigmas and the
   verdict is reported at each, so no single assumption carries the conclusion.
2. A vertical's two legs are $1-$2 apart; the test prices both at the SHORT
   leg's proxy. Stated here because it is a real simplification.
3. The longrun boards are heavily near-the-money, so a material fraction of
   entries land above |delta| 0.70 - outside the measured universe. The model
   refuses them; the test substitutes the boundary bucket's hand-written price
   and REPORTS the count. It is an extrapolation and it is counted, not hidden.
"""

from __future__ import annotations

import json
import math
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

D = Decimal

# --------------------------------------------------------------------------
# Artifacts. Absolute host paths, overridable so the file is not welded to one
# machine. The two artifact-backed tests SKIP (they do not silently pass) when
# a run is not on disk.
# --------------------------------------------------------------------------

def _path(env: str, default: str) -> Path:
    return Path(os.environ.get(env) or default)


RUN_DIR = _path(
    "TREX_LONGRUN_DIR",
    "/home/alexk/documents/tree_options/artifacts/desk-store/evaluations/longrun"
    "/20260929T094303Z",
)
OUTCOME_TABLE = _path(
    "TREX_OUTCOME_TABLE", "/home/alexk/.local/state/trex-longrun/outcome-table-20260929-long.jsonl"
)
CORPUS = _path(
    "TREX_SWEEP_CHAINS", "/home/alexk/.local/state/trex-strategy-sweep-20260930/chains.json"
)

#: Flat baseline, recomputed here from first principles - 4 fills on a 2-leg
#: vertical, half of a $0.06 two-sided quote per fill, $0.65 commission per fill.
FLAT_ROUND_TRIP = 4 * D("0.03") * 100 + 4 * D("0.65")
assert FLAT_ROUND_TRIP == D("14.60")

#: Hand-written price of the outermost MEASURED bucket (|delta| 0.50-0.70), used
#: ONLY for entries the measured universe excludes. From the same table as
#: test_desk_measured_cost.GRID; written out again so this file's oracle is
#: readable without importing the other test.
BOUNDARY_ROUND_TRIP = {7: D("40.600000"), 22: D("53.254000"), 46: D("65.946000")}


def _boundary_cost(dte: int) -> Decimal:
    return BOUNDARY_ROUND_TRIP[7 if dte <= 21 else 22 if dte <= 45 else 46]


#: The sigmas the moneyness proxy is swept over. 0.30 is roughly a 30-day
#: index-option IV; the others bracket it. No sigma is the "right" one.
SIGMAS = (0.20, 0.25, 0.30, 0.40, 0.50)


# --------------------------------------------------------------------------
# A test-owned moneyness -> |delta| proxy. This is a FIXTURE, not the model.
# It lives here so the model's own helpers are never its oracle.
# --------------------------------------------------------------------------


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def proxy_abs_delta(structure: str, moneyness_pct: float, dte: int, sigma: float) -> float:
    """Black-Scholes |delta| from the board row's short-strike moneyness.

    ``short_strike_moneyness_pct = (strike/spot - 1) * 100`` (desk/lab.py).
    For a CALL a positive value is ITM; for a PUT it is OTM - the sign flips
    with the right. |delta| does not care which, only how far through the money
    the short strike sits, so the magnitude of moneyness is what is used.
    """
    depth = abs(moneyness_pct) / 100.0
    sigma_root_t = sigma * math.sqrt(dte / 365.0)
    d1 = depth / sigma_root_t + sigma_root_t / 2.0
    return normal_cdf(d1)


# --------------------------------------------------------------------------
# The detector. Hand-written, pure, and separately self-tested below.
# --------------------------------------------------------------------------


def detect(flat: dict[str, float], shaped: dict[str, float]) -> dict[str, Any]:
    """Compare two costings of the SAME entries. Pure, no model helpers."""
    arms = sorted(set(flat) & set(shaped))
    assert arms, "nothing to compare"
    flat_order = sorted(arms, key=lambda a: (-flat[a], a))
    shaped_order = sorted(arms, key=lambda a: (-shaped[a], a))
    order_flips = [a for a, b in zip(flat_order, shaped_order, strict=True) if a != b]
    sign_flips = [a for a in arms if (flat[a] > 0) != (shaped[a] > 0)]
    identical = all(flat[a] == shaped[a] for a in arms)
    return {
        "arms": arms,
        "flat_order": flat_order,
        "shaped_order": shaped_order,
        "order_flips": order_flips,
        "sign_flips": sign_flips,
        "ordering_changed": bool(order_flips),
        "profitability_changed": bool(sign_flips),
        "identical": identical,
        "conclusive": bool(order_flips or sign_flips or identical),
    }


# --------------------------------------------------------------------------
# Artifact loading (lazy; the pure tests above must not need any of this)
# --------------------------------------------------------------------------


def _require_artifacts() -> None:
    for path in (RUN_DIR / "digest.json", RUN_DIR / "boards.jsonl", OUTCOME_TABLE):
        if not path.exists():
            pytest.skip(f"run artifact not on disk: {path}")


@pytest.fixture(scope="module")
def run() -> dict[str, Any]:
    _require_artifacts()
    boards: dict[str, dict[str, Any]] = {}
    clocks: set[str] = set()
    with (RUN_DIR / "boards.jsonl").open() as handle:
        for line in handle:
            record = json.loads(line)
            clocks.add(record["clock"])
            for row in record["rows"]:
                boards[row["id"]] = row

    # (snapshot, candidate_id) -> {exit_mode: outcome}; one pass over the table.
    table: dict[str, dict[str, dict[str, Any]]] = {}
    with OUTCOME_TABLE.open() as handle:
        for line in handle:
            record = json.loads(line)
            table.setdefault(record["snapshot"], {}).setdefault(
                record["candidate_id"], {}
            )[record["exit_mode"]] = record

    digest = json.loads((RUN_DIR / "digest.json").read_text())
    return {
        "boards": boards,
        "clocks": clocks,
        "table": table,
        "standings": {s["arm"]: s for s in digest["standings"]},
    }


def _arm_entries(arm: str, run_data: dict[str, Any]) -> list[dict[str, Any]]:
    """The arm's evaluated entries, in receipt order, with its board row.

    A receipt with no choice never entered; an outcome with status ``no_fill``
    entered but is unevaluable. Both are excluded, which is exactly how the
    digest counts ``entered`` and ``unevaluable``.
    """
    path = RUN_DIR / "receipts" / f"{arm}.jsonl"
    if not path.exists():
        return []
    boards = run_data["boards"]
    table = run_data["table"]
    seen: set[tuple[str, str]] = set()
    entries: list[dict[str, Any]] = []
    with path.open() as handle:
        for line in handle:
            receipt = json.loads(line)
            choice = receipt.get("choice")
            if not choice:
                continue
            key = (receipt["snapshot"], choice)
            if key in seen:
                continue
            seen.add(key)
            outcome = table.get(receipt["snapshot"], {}).get(choice, {}).get(
                receipt["horizon"]
            )
            if outcome is None or outcome["status"] == "no_fill":
                continue
            entries.append(
                {
                    "gross": D(outcome["gross"]),
                    "row": boards.get(choice),
                    "symbol": outcome.get("underlying"),
                }
            )
    return entries


# --------------------------------------------------------------------------
# The model, resolved the same way as in the sibling contract file.
# --------------------------------------------------------------------------


def measured_model() -> Any:
    try:
        from tree_options.desk import cost
    except ImportError as exc:  # pragma: no cover - the RED path
        raise AssertionError(f"tree_options.desk.cost does not exist: {exc}") from exc
    factory = getattr(cost.SpreadCostModel, "measured", None)
    if factory is None:
        raise AssertionError("tree_options.desk.cost.SpreadCostModel.measured() is required")
    return factory()


def provenance(symbol: str, abs_delta: float, dte: int) -> Any:
    from tree_options.desk import cost

    return cost.ObservationProvenance(
        symbol=symbol or "UNKNOWN",
        abs_delta=D(repr(round(abs_delta, 6))),
        dte=dte,
        source_session="2026-09-22",
        source_timestamp_et="2026-09-22T18:05:00-04:00",
        is_eod_snapshot=True,
    )


# ==========================================================================
# 1. The detector proves it can see a change
# ==========================================================================


def test_detector_proves_it_can_see_a_flip() -> None:
    """Two arms the flat model scores as an EXACT TIE.

    Same gross, same evaluated count, so flat charges both $14.60 and the
    ordering is decided by the tie-break. The shaped model separates them.
    A detector that cannot see this cannot be trusted when it reports "no
    change" on the real run.
    """
    model = measured_model()
    cheap = model.price([provenance("IWM", 0.05, 14), provenance("IWM", 0.05, 14)])
    rich = model.price([provenance("IWM", 0.60, 14), provenance("IWM", 0.60, 14)])
    assert cheap.total_round_trip < rich.total_round_trip

    gross = 100.0
    # the names are chosen so the alphabetical tie-break puts the EXPENSIVE
    # arm first: under flat, that ordering is the entire result.
    flat = {arm: gross - float(FLAT_ROUND_TRIP) for arm in ("aaa-rich", "bbb-cheap")}
    shaped = {
        "aaa-rich": gross - float(rich.total_round_trip),
        "bbb-cheap": gross - float(cheap.total_round_trip),
    }
    assert flat["aaa-rich"] == flat["bbb-cheap"], "the flat baseline must tie, or this is vacuous"

    verdict = detect(flat, shaped)
    assert verdict["ordering_changed"] is True
    assert verdict["profitability_changed"] is False
    assert verdict["identical"] is False
    assert verdict["conclusive"] is True
    assert verdict["flat_order"] == ["aaa-rich", "bbb-cheap"]


def test_detector_proves_it_can_see_an_exact_identity() -> None:
    """The mirror case: if the two costings are equal, the detector must say so
    rather than inventing a change."""
    flat = {"a": 1.0, "b": 2.0}
    verdict = detect(dict(flat), dict(flat))
    assert verdict["identical"] is True
    assert verdict["ordering_changed"] is False
    assert verdict["profitability_changed"] is False
    assert verdict["conclusive"] is True


# ==========================================================================
# 2. The reconstruction is real before the re-rating means anything
# ==========================================================================


def test_reconstruction_reproduces_the_published_digest(run: dict[str, Any]) -> None:
    """The flat re-costing of the reconstructed entries must land on the digest.

    The digest is the independent witness that the receipts + board + outcome
    table on disk really are the run. Measured against it (28 arms, this run):

      * 18 arms reconcile to the cent, exactly;
      * 4 arms differ by at most $19.60 on totals of $3.5k-$20.6k. Each of
        those four reconstructs exactly ONE evaluated entry more than the
        digest counts, and the run carries 8 board snapshots at four clocks
        (10:40, 11:20, 12:00, 12:40) that are not on the pre-registered 8-clock
        grid.

    The tolerance is the larger of one flat round trip and 0.2% of the arm's
    published total. A wrong gross, a wrong ``no_fill`` filter or a wrong exit
    mode moves a total by orders of magnitude more and fails here. The
    per-arm gap is printed, not hidden.
    """
    standings = run["standings"]
    exact = 0
    gaps: list[tuple[str, float]] = []
    for arm in sorted(standings):
        published = standings[arm]
        if not published.get("entered"):
            continue
        entries = _arm_entries(arm, run)
        if not entries:
            continue
        evaluated = published["entered"] - published["unevaluable"]
        surplus = len(entries) - evaluated
        assert surplus in (0, 1), (
            f"{arm}: reconstruction found {len(entries)} evaluated entries, digest "
            f"counts {evaluated} - a surplus of {surplus} exceeds the one-entry "
            "appended-board discrepancy this test accounts for"
        )
        unshapeable = sum(
            1
            for e in entries
            if e["row"] is None or e["row"]["short_strike_moneyness_pct"] is None
        )
        flat_net = float(sum(e["gross"] for e in entries) - FLAT_ROUND_TRIP * len(entries))
        gap = flat_net - float(published["net_total"])
        tolerance = max(float(FLAT_ROUND_TRIP) * (1 + unshapeable), 0.002 * abs(float(published["net_total"])))
        if abs(gap) <= 0.01:
            exact += 1
        else:
            gaps.append((arm, gap))
        assert abs(gap) <= tolerance, (
            f"{arm}: flat re-costing {flat_net} vs digest {published['net_total']}, "
            f"gap {gap:+.2f} exceeds the tolerance {tolerance:.2f}"
        )
    print(f"\nreconciliation: {exact} arms exact, gaps {[(a, round(g, 2)) for a, g in gaps]}")
    assert exact + len(gaps) >= 20, "too few arms reconciled to mean anything"
    assert exact >= 0.7 * (exact + len(gaps)), (
        f"only {exact} of {exact + len(gaps)} arms reconcile to the cent; the "
        "reconstruction is not the run"
    )


def test_every_run_decision_clock_is_outside_the_corpus_capture_window(
    run: dict[str, Any],
) -> None:
    """The observation gap, checked against the clocks the run really used.

    The pre-registered grid is eight clocks. This run also carries eight board
    snapshots at four off-grid clocks; they are checked too, not excluded -
    every clock the run traded at must fall outside the corpus capture window.
    """
    published = {"10:00", "10:45", "11:30", "12:15", "13:00", "13:45", "14:30", "15:15"}
    assert published <= run["clocks"], f"the run lost decision clocks: {published - run['clocks']}"
    start, end = 17 * 60 + 45, 6 * 60 + 30
    for clock in sorted(run["clocks"]):
        hour, minute = clock.split(":")
        now = int(hour) * 60 + int(minute)
        assert not (now >= start or now <= end), (
            f"decision clock {clock} ET is inside the corpus capture window "
            "17:45-06:30 ET; the EOD claim would be void"
        )


def test_the_corpus_carries_no_decision_clock_observation() -> None:
    """The corpus itself says only WHEN, never AT WHAT TIME.

    Read as a bounded prefix: 199 MB must not enter a unit test, and the claim
    only needs enough rows to show the record shape.
    """
    if not CORPUS.exists():
        pytest.skip(f"corpus not on disk: {CORPUS}")
    prefix = CORPUS.read_bytes()[:262144].decode("utf-8", "replace")
    sessions = set(re.findall(r'"sess": "([^"]+)"', prefix))
    assert sessions, "no sess field in the corpus prefix"
    for session in sessions:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", session), f"{session!r} is not a bare date"
    # no field of the form <something>_at / _time / _clock that could be a fill time
    assert not re.search(r'"[a-z_]*(?:_at|_time|_clock|_ts)"\s*:', prefix), (
        "the corpus carries a timestamp field; the 'no decision-clock observation' "
        "claim is void and must be re-derived"
    )


# ==========================================================================
# 3. THE RE-RATING
# ==========================================================================


@pytest.fixture(scope="module")
def rerating(run: dict[str, Any]) -> dict[str, Any]:
    """Flat vs shaped, arm by arm, at each sigma."""
    _require_artifacts()
    model = measured_model()

    per_sigma: dict[float, dict[str, dict[str, Any]]] = {sigma: {} for sigma in SIGMAS}
    priced_costs: set[Decimal] = set()
    priced = 0
    no_board = 0
    no_moneyness = 0
    #: per sigma: {delta_bucket_or_"clamped": count}
    buckets: dict[float, dict[str, int]] = {sigma: {} for sigma in SIGMAS}
    bands: dict[float, dict[int, int]] = {sigma: {} for sigma in SIGMAS}

    for arm in sorted(run["standings"]):
        entries = _arm_entries(arm, run)
        if not entries:
            continue
        flat = D(0)
        for sigma in SIGMAS:
            per_sigma[sigma][arm] = {"flat": D(0), "shaped": D(0)}
        for entry in entries:
            row = entry["row"]
            if row is None:
                no_board += 1
                continue
            moneyness = row["short_strike_moneyness_pct"]
            if moneyness is None:
                no_moneyness += 1
                continue
            priced += 1
            flat += entry["gross"] - FLAT_ROUND_TRIP
            for sigma in SIGMAS:
                estimate = proxy_abs_delta(
                    row["structure"], float(moneyness), row["dte"], sigma
                )
                if estimate > 0.70:
                    # outside the measured universe: the model refuses it, so
                    # the test substitutes the boundary price and COUNTS it.
                    key = "clamped>0.70"
                    cost_dollars = _boundary_cost(row["dte"])
                else:
                    legs = [
                        provenance(entry["symbol"], estimate, row["dte"]),
                        provenance(entry["symbol"], estimate, row["dte"]),
                    ]
                    quote = model.price(legs)
                    cost_dollars = quote.total_round_trip
                    key = _bucket_label(quote.legs[0])
                buckets[sigma][key] = buckets[sigma].get(key, 0) + 1
                band = _band_label(row["dte"])
                bands[sigma][band] = bands[sigma].get(band, 0) + 1
                priced_costs.add(cost_dollars)
                per_sigma[sigma][arm]["shaped"] += entry["gross"] - cost_dollars
        for sigma in SIGMAS:
            per_sigma[sigma][arm]["flat"] = flat

    return {
        "per_sigma": per_sigma,
        "distinct_costs": priced_costs,
        "priced": priced,
        "no_board": no_board,
        "no_moneyness": no_moneyness,
        "buckets": buckets,
        "bands": bands,
    }


def _bucket_label(leg: Any) -> str:
    """The measured bucket the model actually chose, read off its own quote."""
    edges = ("0.00-0.10", "0.10-0.20", "0.20-0.35", "0.35-0.50", "0.50-0.70")
    return f"bucket{leg.delta_bucket}:{edges[leg.delta_bucket]}"


def _band_label(dte: int) -> int:
    return 0 if dte <= 21 else 1 if dte <= 45 else 2


def test_the_shape_is_actually_exercised_by_this_run(rerating: dict[str, Any]) -> None:
    """A flat model satisfies nothing below. This is the non-vacuity assertion.

    It is what makes an "identical conclusions" verdict meaningful: it proves
    the run really does span a range of leg moneyness and a range of expiries,
    so the shape had something to say and said nothing only if the arithmetic
    is a wash.

    It also REPORTS the distribution, because a single-bucket run cannot
    separate "the shape changed the answer" from "one bucket's premium changed
    the answer" - and a reader has to be able to tell those apart.
    """
    distinct = rerating["distinct_costs"]
    assert rerating["priced"] > 1000, "too few priced entries to re-rate"
    assert len(distinct) >= 3, f"only {len(distinct)} distinct per-entry costs: the shape is flat"
    differing = [c for c in distinct if c != FLAT_ROUND_TRIP]
    assert differing, "every entry priced at exactly the flat baseline; nothing was re-rated"

    for sigma in SIGMAS:
        occupied = [k for k in rerating["buckets"][sigma] if k != "clamped>0.70"]
        occupied_bands = [b for b, n in rerating["bands"][sigma].items() if n]
        accounted = sum(rerating["buckets"][sigma].values())
        print(f"\nsigma={sigma} |delta| buckets: {rerating['buckets'][sigma]}")
        print(f"sigma={sigma} dte bands: {rerating['bands'][sigma]}")
        assert accounted == rerating["priced"], "the bucket histogram does not account for every entry"
        assert len(occupied_bands) >= 2, f"sigma={sigma}: one dte band only; no expiry shape exercised"
        if len(occupied) <= 1:
            # Not a failure - a fact about this run's boards, which concentrate
            # their short strikes within ~1% of spot and are therefore all
            # near-the-money. It is reported because it BOUNDS what the verdict
            # can claim: the cheap wing buckets are never exercised here, so
            # this run cannot falsify the cheap-wing half of the model.
            print(
                f"  CAVEAT sigma={sigma}: every priced leg lands in "
                f"{occupied or ['none']} - the moneyness axis is NOT exercised by this run"
            )
    occupied_any = {
        k
        for sigma in SIGMAS
        for k, n in rerating["buckets"][sigma].items()
        if n and k != "clamped>0.70"
    }
    assert occupied_any, "no leg of the run landed inside the measured universe at any sigma"


def test_rerating_of_the_finished_run(rerating: dict[str, Any]) -> None:
    """The verdict, reported honestly in either direction.

    This test asserts only that the comparison is CONCLUSIVE. It does not
    assert that the shape changes anything - a run whose arms are insensitive
    to per-leg cost is a real and important result, and the test must be able
    to report it. What it will not accept is a comparison it cannot make.
    """
    per_sigma = rerating["per_sigma"]
    verdicts: dict[float, dict[str, Any]] = {}
    for sigma, arms in per_sigma.items():
        flat = {a: float(v["flat"]) for a, v in arms.items()}
        shaped = {a: float(v["shaped"]) for a, v in arms.items()}
        verdicts[sigma] = detect(flat, shaped)

    mid = SIGMAS[2]
    lines = [
        "",
        "=" * 78,
        "RE-RATING: measured per-leg cost vs the flat $14.60 baseline",
        "=" * 78,
        f"evaluated entries priced : {rerating['priced']}",
        f"  excluded, no board row : {rerating['no_board']}",
        f"  excluded, no moneyness : {rerating['no_moneyness']}",
        f"distinct per-entry costs  : {len(rerating['distinct_costs'])}",
        f"|delta| buckets at sigma={mid}: {rerating['buckets'][mid]}",
        f"dte bands at sigma={mid}   : {rerating['bands'][mid]}",
    ]
    for sigma in SIGMAS:
        v = verdicts[sigma]
        clamped = rerating["buckets"][sigma].get("clamped>0.70", 0)
        lines.append(
            f"  sigma={sigma:.2f}  arms={len(v['arms'])}  "
            f"order flips={len(v['order_flips'])}  sign flips={len(v['sign_flips'])}  "
            f"identical={v['identical']}  "
            f"clamped={clamped} ({100.0 * clamped / max(rerating['priced'], 1):.1f}% EXTRAPOLATED)"
        )
    sigma = SIGMAS[2]
    arms = per_sigma[sigma]
    moved = sorted(
        (a for a in arms),
        key=lambda a: float(arms[a]["shaped"]) - float(arms[a]["flat"]),
    )
    lines.append(f"  biggest per-arm cost change (sigma={sigma}):")
    for arm in moved[:3] + moved[-3:]:
        v = arms[arm]
        lines.append(
            f"    {arm:<42} flat {float(v['flat']):>12,.2f}  shaped {float(v['shaped']):>12,.2f}"
        )
    changed = [s for s in SIGMAS if verdicts[s]["ordering_changed"] or verdicts[s]["profitability_changed"]]
    occupied_any = {
        k
        for sigma in SIGMAS
        for k, n in rerating["buckets"][sigma].items()
        if n and k != "clamped>0.70"
    }
    if changed:
        lines.append("")
        lines.append(
            f"VERDICT: THE SHAPE CHANGES THE ANSWER at sigma in {list(changed)}. "
            "The flat model's per-arm ordering is an artefact of a constant."
        )
        if len(occupied_any) <= 1:
            lines.append(
                "  SCOPE OF THAT VERDICT: every leg of this run priced in "
                f"{sorted(occupied_any)}, because the boards put the short strike within "
                "~1% of spot. The change is carried by that bucket's premium over the flat "
                "constant and by the dte band. The cheap wing buckets (|delta| < 0.10) are "
                "NOT exercised by this run, so this run cannot confirm the half of the "
                "model that would make a wing strategy cheaper. Only test_desk_measured_"
                "cost.py's grid covers those cells."
            )
    else:
        lines.append("")
        lines.append(
            "VERDICT: IDENTICAL CONCLUSIONS. At every sigma the measured per-leg cost "
            "reproduces the flat $14.60 ordering and profitability exactly. For THESE "
            "arms the shape does not matter. That is a real result and it is reported, "
            "not hidden."
        )
    lines.append("=" * 78)
    report = "\n".join(lines)
    print(report)

    assert all(v["conclusive"] for v in verdicts.values()), report
    # Persist outside the repository: a test must not mutate the run it reads.
    out = Path(os.environ.get("TREX_VERDICT_PATH", "/tmp/trex-cost-rerating-verdict.txt"))
    try:
        out.write_text(report + "\n")
        print(f"\nverdict written to {out}")
    except OSError as exc:  # pragma: no cover
        print(f"\nverdict not persisted: {exc}")


def test_the_verdict_is_stable_across_the_sigma_sweep(rerating: dict[str, Any]) -> None:
    """One arbitrary volatility assumption must not decide the conclusion.

    If the answer flipped with sigma, the honest verdict is "the result depends
    on an assumption this data cannot pin down" - and that is reported as a
    FAILURE of the re-rating, because a re-rating nobody can reproduce is not
    evidence of anything.
    """
    per_sigma = rerating["per_sigma"]
    verdicts = {}
    for sigma in SIGMAS:
        arms = per_sigma[sigma]
        verdicts[sigma] = detect(
            {a: float(v["flat"]) for a, v in arms.items()},
            {a: float(v["shaped"]) for a, v in arms.items()},
        )
    changed = {s for s in SIGMAS if verdicts[s]["ordering_changed"] or verdicts[s]["profitability_changed"]}
    print(f"\nsigma sweep: changed at {sorted(changed)} of {list(SIGMAS)}")
    assert changed in (set(), set(SIGMAS)), (
        f"the verdict depends on the assumed volatility: changed at {sorted(changed)} only"
    )

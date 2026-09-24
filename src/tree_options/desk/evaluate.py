"""FORECAST-001: the pre-registered out-of-sample scoring of the pooled
log-HAR (``docs/desk/FORECAST-001.md``, sealed before this ran).

* walk-forward monthly refits from 2024-09 (:func:`har.walk_forward`);
* test origins t >= 2024-09-03 with t + h <= cutoff (the earliest last
  session among the names);
* benchmarks RV22, EWMA(0.94) and IV^2 (IVHIST-001 ``ok`` names only);
* QLIKE (primary) and MSE, Diebold-Mariano on the per-date cross-sectional
  mean loss differential with a Newey-West lag of h-1;
* PASS iff HAR beats RV22 on QLIKE at one-sided p < 0.05 at h = 20 AND 63;
* the IV blend: encompassing regression on blend-train (origins with
  t + h <= 2025-08-29), Driscoll-Kraay lag h-1; allowed iff c2 > 0 at
  p < 0.05; if allowed, the frozen blend is scored against HAR on origins
  from 2025-09-02 (information only).

Every cell is reported, pass or fail.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np

from tree_options.desk import har, stats
from tree_options.desk.ivhist import days_between, history_series
from tree_options.desk.sessions import Calendar

STUDY = "FORECAST-001"
SCHEMA = "desk-forecast-eval/1"
TEST_START = date(2024, 9, 1)
BLEND_TRAIN_END = date(2025, 8, 29)
BLEND_TEST_START = date(2025, 9, 1)
ALPHA = 0.05
PRIMARY_HORIZONS: tuple[int, ...] = (20, 63)
BENCHMARKS: tuple[str, ...] = ("rv22", "ewma", "iv2")
LOSSES: dict[str, Callable[[float, float], float]] = {"qlike": stats.qlike, "mse": stats.mse}


@dataclass(frozen=True)
class ScoreRow:
    name: str
    t: int  # grid index of the origin
    rv: float
    har: float
    rv22: float | None
    ewma: float | None
    iv2: float | None


def compare(rows: Sequence[ScoreRow], bench: str, loss: str, *, lag: int) -> dict[str, Any]:
    """Mean losses and the DM test of HAR vs ``bench`` (d = L(bench) - L(HAR),
    averaged across names per date, H1: HAR better)."""
    fn = LOSSES[loss]
    by_t: dict[int, list[float]] = {}
    lh: list[float] = []
    lb: list[float] = []
    for r in rows:
        f = getattr(r, bench)
        if f is None or not f > 0.0:
            continue
        a, b = fn(r.rv, r.har), fn(r.rv, f)
        lh.append(a)
        lb.append(b)
        by_t.setdefault(r.t, []).append(b - a)
    if not lh:
        return {"status": "NOT_EVALUABLE", "n_rows": 0, "n_dates": 0}
    series = [sum(v) / len(v) for _t, v in sorted(by_t.items())]
    dm = stats.dm_test(series, lag=lag)
    return {
        "status": "OK",
        "n_rows": len(lh),
        "n_dates": len(series),
        "mean_loss_har": sum(lh) / len(lh),
        "mean_loss_bench": sum(lb) / len(lb),
        "mean_diff": dm.mean if dm else sum(series) / len(series),
        "dm": dm.stat if dm else None,
        "p_one_sided": dm.p_one_sided if dm else None,
    }


def verdict(cells: Mapping[str, Any]) -> str:
    for h in PRIMARY_HORIZONS:
        p = cells.get(str(h), {}).get("rv22", {}).get("qlike", {}).get("p_one_sided")
        if p is None or not p < ALPHA:
            return "FAIL"
    return "PASS"


def encompassing(rows: Sequence[ScoreRow], *, lag: int) -> dict[str, Any]:
    """ln RV = a + c1 ln F_HAR + c2 ln F_IV + u (pooled OLS, Driscoll-Kraay)."""
    use: list[ScoreRow] = []
    xs: list[list[float]] = []
    for r in rows:
        if r.iv2 is not None and r.iv2 > 0.0:
            use.append(r)
            xs.append([1.0, math.log(r.har), math.log(r.iv2)])
    if not use:
        return {"status": "NOT_EVALUABLE", "allowed": False, "reason": "no IV-ok rows"}
    design = np.array(xs)
    y = np.array([math.log(r.rv) for r in use])
    try:
        beta, resid = stats.ols(design, y)
    except np.linalg.LinAlgError:
        return {"status": "NOT_EVALUABLE", "allowed": False, "reason": "rank-deficient"}
    cov = stats.driscoll_kraay(design, resid, [r.t for r in use], lag=lag)
    se = [math.sqrt(cov[i, i]) if cov[i, i] > 0 else math.nan for i in range(3)]
    z2 = beta[2] / se[2] if se[2] > 0 else math.nan
    p2 = stats.norm_sf(z2) if math.isfinite(z2) else None
    n = len(use)
    return {
        "status": "OK",
        "n_rows": n,
        "n_dates": len({r.t for r in use}),
        "a": float(beta[0]),
        "c1": float(beta[1]),
        "c2": float(beta[2]),
        "se_c1": se[1],
        "se_c2": se[2],
        "z_c2": z2,
        "p_c2_one_sided": p2,
        "s2_u": float(resid @ resid) / (n - 3) if n > 3 else None,
        "allowed": bool(beta[2] > 0.0 and p2 is not None and p2 < ALPHA),
    }


def blend_rows(rows: Sequence[ScoreRow], enc: Mapping[str, Any]) -> list[ScoreRow]:
    """Rows carrying the frozen blend forecast in the ``har`` slot and the
    HAR forecast in the ``rv22`` slot, so ``compare(.., "rv22", ..)`` tests
    H1 'the blend beats HAR' with the same DM machinery."""
    out = []
    for r in rows:
        if r.iv2 is None or r.iv2 <= 0.0:
            continue
        lb = enc["a"] + enc["c1"] * math.log(r.har) + enc["c2"] * math.log(r.iv2)
        fb = math.exp(lb + enc["s2_u"] / 2.0)
        out.append(ScoreRow(r.name, r.t, r.rv, fb, r.har, None, None))
    return out


def cutoff_index(data: har.HarData) -> int:
    lasts = []
    for ns in data.names.values():
        present = [i for i, x in enumerate(ns.v) if x is not None]
        if present:
            lasts.append(present[-1])
    if not lasts:
        raise ValueError("no name has a variance proxy")
    return min(lasts)


def score_rows(
    data: har.HarData,
    wf: har.WalkForward,
    *,
    cutoff: int,
    start: int,
    iv: Mapping[str, Mapping[date, float]],
) -> list[ScoreRow]:
    h = wf.h
    rows = []
    for name, ns in data.names.items():
        for t, f in sorted(wf.forecasts[name].items()):
            if t < start or t + h > cutoff:
                continue
            realized = har.realized(ns, t, h)
            if realized is None or realized <= 0.0:
                continue
            iv2 = None
            level = iv.get(name, {}).get(data.sessions[t])
            if level is not None:
                iv2 = level * level * days_between(data.sessions[t], data.sessions[t + h]) / 365.0
            rows.append(
                ScoreRow(
                    name=name,
                    t=t,
                    rv=realized,
                    har=f,
                    rv22=har.rv22_forecast(ns, t, h),
                    ewma=har.ewma_forecast(ns, t, h),
                    iv2=iv2,
                )
            )
    return rows


def _fit_doc(sessions: Sequence[date], m0: int, fit: har.HarFit | None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "month": f"{sessions[m0].year:04d}-{sessions[m0].month:02d}",
        "through": sessions[m0 - 1].isoformat() if m0 >= 1 else None,
    }
    if fit is None:
        doc["status"] = "NO_FIT"
        return doc
    doc.update(
        n=fit.n,
        k=fit.k,
        s2=fit.s2,
        bd=fit.bd,
        bw=fit.bw,
        bm=fit.bm,
        be=fit.be,
        be_estimated=fit.be is not None,
        n_event_rows=fit.n_event_rows,
        n_names=len(fit.intercepts),
    )
    return doc


def run_forecast_001(
    panel: Mapping[str, Mapping[str, Mapping[str, Any]]],
    earnings: Mapping[str, Sequence[str]],
    cal: Calendar,
    *,
    names: Sequence[str],
    iv_history: Mapping[str, Any] | None,
    iv_labels: Mapping[str, str],
    provenance: Mapping[str, Any],
    horizons: Sequence[int] = har.HORIZONS,
    min_event_rows: int = har.MIN_EVENT_ROWS,
) -> dict[str, Any]:
    data = har.build_har_data(panel, earnings, cal, names)
    cutoff = cutoff_index(data)
    start = bisect.bisect_left(data.sessions, TEST_START)
    blend_end = bisect.bisect_right(data.sessions, BLEND_TRAIN_END) - 1
    blend_start = bisect.bisect_left(data.sessions, BLEND_TEST_START)
    iv_ok = sorted(n for n, lab in iv_labels.items() if lab == "ok" and n in data.names)
    iv = {n: {d: v for d, (v, _m) in history_series(iv_history or {}, n).items()} for n in iv_ok}
    cells: dict[str, Any] = {}
    fits: dict[str, Any] = {}
    blend: dict[str, Any] = {}
    per_name: dict[str, Any] = {}
    for h in horizons:
        rows_h = har.build_rows(data, h)
        wf = har.walk_forward(
            data,
            h,
            start=TEST_START,
            through_idx=cutoff,
            min_event_rows=min_event_rows,
            rows=rows_h,
        )
        fits[str(h)] = [_fit_doc(data.sessions, m0, fit) for m0, fit in sorted(wf.fits.items())]
        rows = score_rows(data, wf, cutoff=cutoff, start=start, iv=iv)
        cells[str(h)] = {
            b: {loss: compare(rows, b, loss, lag=h - 1) for loss in LOSSES} for b in BENCHMARKS
        }
        sums: dict[str, list[float]] = {}
        for r in rows:
            if r.rv22 is not None and r.rv22 > 0.0:
                acc = sums.setdefault(r.name, [0.0, 0.0, 0.0])
                acc[0] += 1.0
                acc[1] += stats.qlike(r.rv, r.har)
                acc[2] += stats.qlike(r.rv, r.rv22)
        per_name[str(h)] = {
            name: {"n": int(n), "har_qlike": a / n, "rv22_qlike": b / n}
            for name, (n, a, b) in sums.items()
        }
        train = [r for r in rows if r.t + h <= blend_end]
        enc = encompassing(train, lag=h - 1)
        entry: dict[str, Any] = {"encompassing": enc}
        if enc.get("allowed"):
            test = [r for r in rows if r.t >= blend_start]
            entry["oos_blend_vs_har_qlike"] = compare(
                blend_rows(test, enc), "rv22", "qlike", lag=h - 1
            )
        blend[str(h)] = entry
    return {
        "schema": SCHEMA,
        "study": STUDY,
        "provenance": dict(provenance),
        "names": list(data.names),
        "cutoff": data.sessions[cutoff].isoformat(),
        "test_start": data.sessions[start].isoformat() if start < len(data.sessions) else None,
        "iv_ok_names": iv_ok,
        "min_event_rows": min_event_rows,
        "cells": cells,
        "verdict": verdict(cells),
        "fits": fits,
        "blend": blend,
        "per_name": per_name,
    }


def coverage_sensitivity(
    panel: Mapping[str, Mapping[str, Mapping[str, Any]]],
    earnings: Mapping[str, Sequence[str]],
    cal: Calendar,
    *,
    names: Sequence[str],
    iv_history: Mapping[str, Any] | None,
    iv_labels: Mapping[str, str],
    horizons: Sequence[int] = har.HORIZONS,
    min_event_rows: int = har.MIN_EVENT_ROWS,
) -> dict[str, Any]:
    """Disclosed sensitivity (Codex P1-3), NOT a re-score: the scored cells
    (name, origin, h) of the HAR-vs-RV22 QLIKE comparison whose window
    (t, t+h] runs past the end of the name's sealed calendar coverage (no
    sealed report whose event session s(D) lies after t+h), and the primary
    cells recomputed without them. The verdict of record is the run's."""
    data = har.build_har_data(panel, earnings, cal, names)
    cutoff = cutoff_index(data)
    start = bisect.bisect_left(data.sessions, TEST_START)
    sessions = cal.sessions()
    last_event: dict[str, date | None] = {}
    for name, ns in data.names.items():
        if not ns.reporter:
            continue
        s_of = []
        for rep in earnings.get(name, ()):
            try:
                i = bisect.bisect_left(sessions, date.fromisoformat(rep))
            except (TypeError, ValueError):
                continue
            if i < len(sessions):
                s_of.append(sessions[i])
        last_event[name] = max(s_of) if s_of else None
    out: dict[str, Any] = {
        "study": STUDY,
        "kind": "coverage-sensitivity (disclosed; the verdict of record stays as run)",
        "rule": "reporter cell uncovered iff no sealed report has s(D) > t+h",
        "horizons": {},
    }
    cells_all: dict[str, Any] = {}
    cells_cov: dict[str, Any] = {}
    for h in horizons:
        wf = har.walk_forward(
            data, h, start=TEST_START, through_idx=cutoff, min_event_rows=min_event_rows
        )
        rows = [
            r
            for r in score_rows(data, wf, cutoff=cutoff, start=start, iv={})
            if r.rv22 is not None and r.rv22 > 0.0
        ]
        uncovered: dict[str, int] = {}
        covered: list[ScoreRow] = []
        for r in rows:
            if r.name in last_event:
                end_event = last_event[r.name]
                if end_event is None or end_event <= data.sessions[r.t + h]:
                    uncovered[r.name] = uncovered.get(r.name, 0) + 1
                    continue
            covered.append(r)
        c_all = compare(rows, "rv22", "qlike", lag=h - 1)
        c_cov = compare(covered, "rv22", "qlike", lag=h - 1)
        cells_all[str(h)] = {"rv22": {"qlike": c_all}}
        cells_cov[str(h)] = {"rv22": {"qlike": c_cov}}
        out["horizons"][str(h)] = {
            "n_rows": len(rows),
            "n_uncovered": sum(uncovered.values()),
            "by_name": dict(sorted(uncovered.items())),
            "dm_all": c_all.get("dm"),
            "p_all": c_all.get("p_one_sided"),
            "dm_covered_only": c_cov.get("dm"),
            "p_covered_only": c_cov.get("p_one_sided"),
            "n_rows_covered_only": c_cov.get("n_rows"),
        }
    out["verdict_all"] = verdict(cells_all)
    out["verdict_covered_only"] = verdict(cells_cov)
    out["verdict_changes"] = out["verdict_all"] != out["verdict_covered_only"]
    return out


# ---------------------------------------------------------------- report


def _f(x: Any, nd: int = 4) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, float):
        if not math.isfinite(x):
            return "n/a"
        return f"{x:.{nd}g}" if abs(x) < 1e-3 or abs(x) >= 1e4 else f"{x:.{nd}f}"
    return str(x)


def render_markdown(res: Mapping[str, Any]) -> str:
    lines = [
        f"## {res['study']} cells (all pre-registered cells, pass or fail)",
        "",
        f"Cutoff {res['cutoff']}; test origins from {res['test_start']}; "
        f"IV-ok names: {', '.join(res['iv_ok_names']) or 'none'}.",
        "",
        "| h | benchmark | loss | n dates | n rows | mean loss HAR | mean loss bench"
        " | DM | p (one-sided) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for h, by_b in res["cells"].items():
        for b, by_l in by_b.items():
            for loss, c in by_l.items():
                lines.append(
                    f"| {h} | {b} | {loss} | {c.get('n_dates', 0)} | {c.get('n_rows', 0)}"
                    f" | {_f(c.get('mean_loss_har'), 6)} | {_f(c.get('mean_loss_bench'), 6)}"
                    f" | {_f(c.get('dm'), 3)} | {_f(c.get('p_one_sided'), 4)} |"
                )
    lines += [
        "",
        f"**Verdict: {res['verdict']}** (HAR vs RV22, QLIKE, p < 0.05 at h=20 AND h=63).",
        "",
    ]
    lines += [
        "### IV blend (encompassing on blend-train)",
        "",
        "| h | n rows | c1 | c2 | se c2 | p c2 | allowed | OOS blend vs HAR DM | p |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for h, entry in res["blend"].items():
        e = entry["encompassing"]
        o = entry.get("oos_blend_vs_har_qlike") or {}
        lines.append(
            f"| {h} | {e.get('n_rows', 0)} | {_f(e.get('c1'))} | {_f(e.get('c2'))}"
            f" | {_f(e.get('se_c2'))} | {_f(e.get('p_c2_one_sided'))} | {e.get('allowed')}"
            f" | {_f(o.get('dm'), 3)} | {_f(o.get('p_one_sided'))} |"
        )
    lines += ["", "### Refits (first and last per horizon)", ""]
    lines += [
        "| h | month | through | n | names | s2 | bd | bw | bm | be | event rows |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for h, fl in res["fits"].items():
        for fd in (fl[0], fl[-1]) if len(fl) > 1 else fl:
            lines.append(
                f"| {h} | {fd['month']} | {fd['through']} | {fd.get('n', 'n/a')}"
                f" | {fd.get('n_names', 'n/a')} | {_f(fd.get('s2'))} | {_f(fd.get('bd'))}"
                f" | {_f(fd.get('bw'))} | {_f(fd.get('bm'))} | {_f(fd.get('be'))}"
                f" | {fd.get('n_event_rows', 'n/a')} |"
            )
    lines += ["", "### Per-name mean QLIKE, HAR vs RV22", ""]
    hs = list(res["per_name"])
    lines.append("| name | " + " | ".join(f"h={h} HAR | h={h} RV22" for h in hs) + " |")
    lines.append("|---|" + "---|---|" * len(hs))
    for name in res["names"]:
        cells = []
        for h in hs:
            row = res["per_name"][h].get(name)
            cells += [_f(row["har_qlike"]), _f(row["rv22_qlike"])] if row else ["n/a", "n/a"]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"

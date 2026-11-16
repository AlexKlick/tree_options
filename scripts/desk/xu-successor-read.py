#!/usr/bin/env python3
"""XU-SUCCESSOR-001's ONE-SHOT read: READ-1, and on DISAPPOINTMENT the shadow
book's single §4.5 evaluation (§6 item 3; the path is sealed verbatim).

Aggregation happens exactly once, here (§4.3): no other code path may
compute the READ-1 cumulative or any shadow aggregate.

READ-1 (§2.4): cumulative net P&L over the first 18 valid sealed cards of
record counting from Card 8 — the incumbent sealed 36-name rule's cards
``<paper>/????-??-??-xsmom-top3.md`` — under the cards' own sealed house
convention ($2,500 x 3 legs, 5bp round-trip on each leg's entry notional,
exit at the 20th-session close). DISAPPOINTMENT iff cumulative <= $0
(exactly $0 IS a disappointment). Valid = sealed, resolved (a Resolution
section), and not voided under a §2.2 class logged in the registration's
Appendix A (ENGINE-DEFECT / DATA-DEFECT / SEAL-TIMING BREACH — anything
else in a void row is a hard error, never silently ignored). Fewer than 18
valid resolved cards: READ-1 is not due (exit 2, no marker, re-runnable).

On DISAPPOINTMENT only, the single §4.5 evaluation over the shadow
namespace ``<paper>/xu-shadow/`` + ``XU-SHADOW-LEDGER.md``:
  - random-3 placebo: per shadow month, 3 names drawn uniformly from that
    month's rankable set (the draw pool INCLUDES the top-3), seed 20261114,
    1,000 draws, identical costs ($2,500/leg, 5bp RT, 20th-session close);
  - incumbent-paired contrast: per-calendar-month difference (shadow -
    incumbent) over common months, month-clustered bootstrap CI;
  - A/A determinism twin: signals.xsmom_rank re-run per sealed month must
    reproduce the card's top-3 exactly;
  - the five bars (n >= 18 valid shadow cards; mean net > 0 with one-sided
    sign-flip p < 0.05 over 10,000 draws; 95% CI-low > 0 vs the random-3
    null; paired mean difference vs incumbent > 0; no single dropped month
    flips the sign), Holm-corrected within the 2-claim family {incumbent
    verdict, successor verdict}.

RNG discipline: ONE random.Random(20261114) stream consumed in a fixed
order (sign-flip, then placebo draws card by card, then the two bootstrap
resamplings), so the evaluation is reproducible byte-for-byte from the same
inputs. Holm note (recorded ambiguity): the incumbent verdict is a
deterministic threshold with no p-value; it enters the family as p = 0
having already fired, so Holm leaves the successor's p unchanged — both raw
and adjusted are reported.

Outputs (into <state>/xu-successor/): ONE report artifact
``xu-successor-read-report.md`` plus the completion marker
``xu-successor-read.done.json`` carrying the report's own sha256. A second
run REFUSES (exit 4, nothing written). Exit codes: 0 read completed (either
verdict); 1 contract/input error; 2 READ-1 not due yet; 4 already run.

The sealed card formats parsed here: the incumbent series follows
artifacts/paper-trades/2026-12-01-xsmom-top3.md (Card 8) with Card 6's
Resolution table; the shadow series follows the §6 item-2 drafter's output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tree_options.desk import signals, xu_shadow
from tree_options.desk.paths import paper_dir, state_root
from tree_options.trex.clock import session_calendar

READ1_N_CARDS = 18
NOTIONAL = Decimal(2500)  # $2,500 per leg, 3 legs (the sealed house convention)
RT_COST = Decimal("0.0005")  # 5bp round trip on each leg's entry notional
SIGN_FLIP_DRAWS = 10_000
PLACEBO_DRAWS = 1_000
BOOTSTRAP_DRAWS = 10_000
SEED = 20261114
VOID_CLASSES = ("ENGINE-DEFECT", "DATA-DEFECT", "SEAL-TIMING BREACH")
REFUSAL_REASONS = ("EARNINGS-BLOCK", "NAME-HELD", "NO-TRADE", "DEFECT")
XU_SHADOW_DIR = "xu-shadow"
XU_SHADOW_LEDGER = "XU-SHADOW-LEDGER.md"

REPORT_NAME = "xu-successor-read-report.md"
MARKER_NAME = "xu-successor-read.done.json"

_TITLE = re.compile(
    r"^# .*XSMOM-TOP3(?:-XU)? monthly: (?P<legs>[A-Z.]+(?: \+ [A-Z.]+)*) "
    r"\(entry (?P<entry>\d{4}-\d{2}-\d{2}) close (?:->|→) exit (?P<exit>\d{4}-\d{2}-\d{2}) close\)"
)
_ENTRY_ROW = re.compile(
    r"^\| (?P<name>[A-Z.]+) \| [^|]*\| (?P<entry>[0-9.]+) \| \$(?P<notional>[0-9,]+) LONG \|"
)
_RESOLUTION_ROW = re.compile(
    r"^\| (?P<name>[A-Z.]+) \| (?P<entry>[0-9.]+) \| \d{4}-\d{2}-\d{2} "
    r"\| (?P<fill>[0-9.]+) \|"
)
_LEDGER_ROW = re.compile(
    r"^\| (?P<exit>\d{4}-\d{2}-\d{2}) \| (?P<card>[^|]+) \| (?P<sha>[0-9a-f]{6,64}) "
    r"\| (?P<status>\w+) \|(?P<detail>[^|]*)\|"
)
_APPENDIX_ROW = re.compile(
    r"^\| (?P<when>[^|]+) \| (?P<card>[^|]+) \| (?P<event>[^|]+) \|(?P<cls>[^|]*)\|"
)


class ReadContractError(RuntimeError):
    """An input violates the registration's contract (fail closed)."""


@dataclass(frozen=True)
class Card:
    path: Path
    entry: str
    exit: str
    legs: tuple[tuple[str, Decimal], ...]  # (name, entry close)
    exit_closes: dict[str, Decimal]
    resolved: bool = True

    @property
    def month(self) -> str:
        return self.entry[:7]

    def net(self) -> Decimal:
        """House convention: $2,500/leg x 3, 5bp RT on each leg's entry
        notional, at the sealed exit fills."""
        if not self.resolved:
            raise ReadContractError(f"{self.path.name}: net P&L of an unresolved card")
        total = Decimal(0)
        for name, entry in self.legs:
            exit_ = self.exit_closes[name]
            total += NOTIONAL * (exit_ / entry - 1) - NOTIONAL * RT_COST
        return total.quantize(Decimal("0.01"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _legs_from_title(match: re.Match) -> tuple[str, ...]:
    return tuple(match.group("legs").split(" + "))


def parse_card(path: Path, *, require_resolution: bool = True) -> Card:
    """A sealed card file -> entries + resolution fills. An unresolved card
    (still open: no Resolution fills yet) parses with ``resolved=False``
    unless ``require_resolution``; anything else the contract cannot consume
    raises ReadContractError."""
    lines = path.read_text(encoding="utf-8").splitlines()
    title = next((ln for ln in lines if ln.startswith("# ") and "monthly:" in ln), None)
    if title is None:
        raise ReadContractError(f"{path.name}: no card title line")
    m = _TITLE.match(title)
    if m is None:
        raise ReadContractError(f"{path.name}: unparseable title: {title!r}")
    legs = _legs_from_title(m)
    entries: dict[str, Decimal] = {}
    in_entries = False
    fills: dict[str, Decimal] = {}
    resolved = False
    for ln in lines:
        if ln.startswith("## "):
            in_entries = ln.startswith("## Entries")
            resolved = resolved or ln.startswith("## Resolution")
            continue
        if in_entries:
            em = _ENTRY_ROW.match(ln)
            if em:
                name = em.group("name")
                if name not in legs:
                    raise ReadContractError(f"{path.name}: entry row {name} not in title legs")
                notional = Decimal(em.group("notional").replace(",", ""))
                if notional != NOTIONAL:
                    raise ReadContractError(
                        f"{path.name}: {name} notional {notional} is not the sealed ${NOTIONAL}"
                    )
                entries[name] = Decimal(em.group("entry"))
        elif resolved:
            rm = _RESOLUTION_ROW.match(ln)
            if rm and rm.group("name") in legs:
                fills[rm.group("name")] = Decimal(rm.group("fill"))
    if set(entries) != set(legs):
        raise ReadContractError(
            f"{path.name}: missing entry rows for {sorted(set(legs) - set(entries))}"
        )
    for name, entry in entries.items():
        if entry <= 0 or (fills.get(name, Decimal(1)) <= 0):
            raise ReadContractError(f"{path.name}: non-positive price for {name}")
    if not resolved or set(fills) != set(legs):
        if require_resolution:
            raise ReadContractError(f"{path.name}: no resolved fills for every leg")
        fills = {}
    return Card(
        path=path,
        entry=m.group("entry"),
        exit=m.group("exit"),
        legs=tuple((n, entries[n]) for n in legs),
        exit_closes=fills,
        resolved=bool(fills),
    )


def parse_appendix_voids(path: Path) -> list[dict[str, str]]:
    """Appendix A void rows: [{card_ref, cls, when, evidence}]. A void row
    whose class is not a §2.2 class is a hard error (never silent)."""
    voids: list[dict[str, str]] = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        m = _APPENDIX_ROW.match(ln)
        if not m or set(cell.strip() for cell in m.groups()) == {"—"}:
            continue
        event = m.group("event").strip().lower()
        if "void" not in event:
            continue  # refusal/opening log rows are not incumbent-series voids
        cls = m.group("cls").strip()
        if cls not in VOID_CLASSES:
            raise ReadContractError(
                f"{path.name}: void row for {m.group('card').strip()!r} carries class {cls!r};"
                f" only {list(VOID_CLASSES)} may void a card (§2.2)"
            )
        voids.append(
            {
                "card_ref": m.group("card").strip(),
                "cls": cls,
                "when": m.group("when").strip(),
                "evidence": cls,
            }
        )
    return voids


def _void_applies(card: Card, void: dict[str, str]) -> bool:
    return card.exit in void["card_ref"] or card.entry in void["card_ref"]


def load_incumbent_series(paper: Path, appendix: Path) -> tuple[list[Card], list[dict[str, str]]]:
    cards = sorted(
        (parse_card(p, require_resolution=False) for p in paper.glob("????-??-??-xsmom-top3.md")),
        key=lambda c: (c.entry, c.exit),
    )
    voids = parse_appendix_voids(appendix)
    return cards, voids


def read1(cards: list[Card], voids: list[dict[str, str]]) -> dict[str, Any] | None:
    """The READ-1 aggregation, or None when the 18th valid card has not
    resolved yet (open cards wait; the clock extends across voids)."""
    valid: list[Card] = []
    applied: list[dict[str, str]] = []
    open_cards: list[str] = []
    for card in cards:
        hit = next((v for v in voids if _void_applies(card, v)), None)
        if hit is not None:
            applied.append({**hit, "exit": card.exit})
            continue
        if not card.resolved:
            open_cards.append(card.exit)  # sealed but still open: not yet a
            continue  # resolved card of record; it waits
        valid.append(card)
        if len(valid) == READ1_N_CARDS:
            break
    if len(valid) < READ1_N_CARDS:
        return None
    nets = [c.net() for c in valid]
    cumulative = sum(nets, Decimal(0))
    return {
        "cards": valid,
        "nets": nets,
        "cumulative": cumulative,
        "disappointment": cumulative <= 0,  # exactly $0 IS a disappointment
        "voids": applied,
        "open": open_cards,
    }


# ------------------------------------------------- the shadow-book evaluation


def parse_shadow_ledger(path: Path) -> dict[str, dict[str, str]]:
    """exit-date -> {status, detail, sha}. Unknown void classes / refusal
    reasons are hard errors (exclusions logged, never silent)."""
    rows: dict[str, dict[str, str]] = {}
    for ln in path.read_text(encoding="utf-8").splitlines():
        m = _LEDGER_ROW.match(ln)
        if not m or set(c.strip() for c in m.groups()) <= {"—", ""}:
            continue
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", m.group("exit")):
            continue  # the table's header row
        status, detail = m.group("status"), m.group("detail").strip()
        if status == "void":
            cls = detail.split(":", 1)[0].strip()
            if cls not in VOID_CLASSES:
                raise ReadContractError(
                    f"{path.name}: void row {m.group('exit')} class {cls!r} is not a §2.2 class"
                )
        elif status == "refused":
            reason = detail.split(":", 1)[0].strip()
            if reason not in REFUSAL_REASONS:
                raise ReadContractError(
                    f"{path.name}: refusal row {m.group('exit')} reason {reason!r}"
                    f" is not one of {list(REFUSAL_REASONS)}"
                )
        elif status != "sealed":
            raise ReadContractError(f"{path.name}: unknown status {status!r} on {m.group('exit')}")
        rows[m.group("exit")] = {"status": status, "detail": detail, "sha": m.group("sha")}
    return rows


def _rankable(card: Card) -> tuple[str, ...]:
    """The month's rankable set: the frozen roster minus the card's excluded
    names (its checks section names them with the ranking code's reasons)."""
    text = card.path.read_text(encoding="utf-8")
    m = re.search(r"excluded: (?P<names>.+)$", text, re.MULTILINE)
    excluded: set[str] = set()
    if m:
        excluded = set(re.findall(r"([A-Z.]+) \([a-z_/]+\)", m.group("names")))
    unknown = excluded - set(xu_shadow.XU_ROSTER)
    if unknown:
        raise ReadContractError(
            f"{card.path.name}: excluded names not on the roster: {sorted(unknown)}"
        )
    return tuple(n for n in xu_shadow.XU_ROSTER if n not in excluded)


def _leg_pnl(entry: Decimal, exit_: Decimal) -> Decimal:
    return NOTIONAL * (exit_ / entry - 1) - NOTIONAL * RT_COST


def placebo_month_mean(
    rng: random.Random, card: Card, rankable: tuple[str, ...], panel: dict[str, Any]
) -> float:
    """Mean net P&L over PLACEBO_DRAWS random-3 picks from the rankable set
    (pool includes the top-3), identical costs and data."""
    closes = {}
    for name in rankable:
        entry = Decimal(str(panel[name][card.entry]["close"]))
        exit_ = Decimal(str(panel[name][card.exit]["close"]))
        closes[name] = float(_leg_pnl(entry, exit_))
    total = 0.0
    for _ in range(PLACEBO_DRAWS):
        pick = rng.sample(list(rankable), 3)
        total += sum(closes[n] for n in pick)
    return total / PLACEBO_DRAWS


def _bootstrap_low(rng: random.Random, values: list[float]) -> float:
    means = []
    for _ in range(BOOTSTRAP_DRAWS):
        draw = [values[rng.randrange(len(values))] for _ in values]
        means.append(sum(draw) / len(draw))
    means.sort()
    return means[int(0.025 * len(means))]


def _sign_flip_p(rng: random.Random, values: list[float]) -> float:
    observed = sum(values) / len(values)
    hits = 0
    for _ in range(SIGN_FLIP_DRAWS):
        flipped = [v if rng.random() < 0.5 else -v for v in values]
        if sum(flipped) / len(flipped) >= observed:
            hits += 1
    return hits / SIGN_FLIP_DRAWS


def evaluate_shadow(
    paper: Path, panel: dict[str, Any], cal: Any, incumbent: list[Card], rng: random.Random
) -> dict[str, Any]:
    """The single §4.5 evaluation (only ever called on DISAPPOINTMENT)."""
    shadow_dir = paper / XU_SHADOW_DIR
    ledger = parse_shadow_ledger(shadow_dir / XU_SHADOW_LEDGER)
    sealed: list[Card] = []
    voids: list[dict[str, str]] = []
    refusals: list[dict[str, str]] = []
    for path in sorted(shadow_dir.glob("????-??-??-xsmom-top3-xu.md")):
        row = ledger.get(path.stem[:10])
        if row is None:
            raise ReadContractError(f"{path.name}: sealed card file with no ledger row")
        if row["status"] == "void":
            voids.append({"exit": path.stem[:10], **row})
            continue
        if row["status"] == "refused":
            refusals.append({"exit": path.stem[:10], **row})
            continue
        card = parse_card(path, require_resolution=False)
        if not card.resolved:
            refusals.append(
                {"exit": path.stem[:10], "detail": "sealed but still open (not counted)"}
            )
            continue
        sealed.append(card)
    sealed.sort(key=lambda c: (c.entry, c.exit))

    # A/A determinism twin: re-rank each sealed month; the top-3 must
    # reproduce the card's picks exactly (a mechanical check, no model)
    aa_failures: list[str] = []
    for card in sealed:
        ranked, _ = signals.xsmom_rank(
            panel, date.fromisoformat(card.entry), cal, skip=0, names=xu_shadow.XU_ROSTER
        )
        top3 = tuple(n for n, _ in ranked[: signals.XSMOM_TOPK])
        if top3 != tuple(n for n, _ in card.legs):
            aa_failures.append(f"{card.entry}: {top3} != {tuple(n for n, _ in card.legs)}")

    nets = [float(c.net()) for c in sealed]
    mean = sum(nets) / len(nets) if nets else 0.0
    bars: dict[str, bool] = {}
    detail: dict[str, Any] = {}

    # bar 1: n >= 18 valid shadow cards
    bars["n_cards"] = len(sealed) >= 18
    detail["n_cards"] = len(sealed)

    # bar 2: mean net > 0, one-sided sign-flip p < 0.05 (10,000 draws)
    p_sign = _sign_flip_p(rng, nets) if nets else 1.0
    bars["mean_positive_signflip"] = bool(nets) and mean > 0 and p_sign < 0.05
    detail["mean_net"] = mean
    detail["sign_flip_p"] = p_sign

    # bar 3: 95% CI-low > 0 vs the random-3 null (placebo diffs, clustered)
    diffs = []
    for card in sealed:
        placebo = placebo_month_mean(rng, card, _rankable(card), panel)
        diffs.append(float(card.net()) - placebo)
    ci_low = _bootstrap_low(rng, diffs) if diffs else -1.0
    bars["ci_low_vs_placebo"] = ci_low > 0
    detail["placebo_ci_low"] = ci_low

    # bar 4: paired mean difference vs incumbent > 0 (month-clustered)
    incumbent_by_month = {c.month: c for c in incumbent}
    paired = [
        (float(c.net()) - float(incumbent_by_month[c.month].net()))
        for c in sealed
        if c.month in incumbent_by_month
    ]
    paired_mean = sum(paired) / len(paired) if paired else 0.0
    paired_ci = _bootstrap_low(rng, paired) if paired else -1.0
    bars["paired_vs_incumbent"] = bool(paired) and paired_mean > 0
    detail["paired_mean"] = paired_mean
    detail["paired_ci_low"] = paired_ci
    detail["paired_months"] = len(paired)

    # bar 5: no single dropped month flips the sign of the mean
    dropped_ok = bool(nets) and mean > 0
    if dropped_ok:
        for i in range(len(nets)):
            rest = [v for k, v in enumerate(nets) if k != i]
            if sum(rest) / len(rest) <= 0:
                dropped_ok = False
                break
    bars["no_dropped_month_flips"] = dropped_ok
    detail["dropped_month_worst"] = (
        min(
            (
                sum(v for k, v in enumerate(nets) if k != i) / (len(nets) - 1)
                for i in range(len(nets))
            ),
            default=0.0,
        )
        if len(nets) > 1
        else 0.0
    )

    # Holm within the 2-claim family {incumbent verdict, successor verdict}:
    # the incumbent verdict is the deterministic READ-1 threshold (p = 0
    # having fired); the successor claim's p is its worst stochastic p.
    p_successor = max(p_sign, 0.0 if ci_low > 0 else 1.0, 0.0 if paired_mean > 0 else 1.0)
    family = sorted([("incumbent", 0.0), ("successor", p_successor)], key=lambda kv: kv[1])
    adjusted: dict[str, float] = {}
    running = 0.0
    for k, (claim, p) in enumerate(family):
        running = max(running, (len(family) - k) * p)
        adjusted[claim] = min(1.0, running)
    detail["holm_raw"] = dict(family)
    detail["holm_adjusted"] = adjusted

    return {
        "n_sealed": len(sealed),
        "voids": voids,
        "refusals": refusals,
        "aa_failures": aa_failures,
        "bars": bars,
        "detail": detail,
        "nets": nets,
        "passes": all(bars.values()) and not aa_failures,
    }


# ----------------------------------------------------------------- reporting


def _fmt(x: Decimal | float) -> str:
    return f"{x:.2f}" if isinstance(x, float) else str(x)


def build_report(
    inputs: dict[str, str],
    r1: dict[str, Any] | None,
    evaluation: dict[str, Any] | None,
    generated_at: datetime,
) -> str:
    lines = [
        "# XU-SUCCESSOR-001 READ-1 + successor evaluation (the one-shot read)",
        "",
        f"generated {generated_at.isoformat()} by scripts/desk/xu-successor-read.py",
        "Aggregation happens exactly once, here (§4.3). This marker-bearing run",
        "is the only read; a second run refuses.",
        "",
        "## Inputs",
        *(f"- {name}: {sha[:16]}… ({path})" for name, (path, sha) in inputs.items()),
        "",
    ]
    if r1 is None:
        lines += [
            "## READ-1: NOT DUE",
            "Fewer than 18 valid resolved cards of record exist; this run wrote no",
            "completion marker and may be repeated when the 18th resolves.",
            "",
        ]
        return "\n".join(lines)
    lines += ["## READ-1", ""]
    if r1.get("open"):
        lines.append(
            "Sealed but still open (waited past the 18 valid resolved cards): "
            + ", ".join(r1["open"])
        )
        lines.append("")
    if r1["voids"]:
        lines.append("Voids applied (§2.2, logged - the clock extends across them):")
        for v in r1["voids"]:
            lines.append(f"- {v['exit']}: {v['cls']} ({v['card_ref']})")
        lines.append("")
    lines += ["| # | entry | exit | legs | net P&L (USD) |", "|---|---|---|---|---|"]
    for k, (card, net) in enumerate(zip(r1["cards"], r1["nets"], strict=True), start=1):
        legs = " + ".join(n for n, _ in card.legs)
        lines.append(f"| {k} | {card.entry} | {card.exit} | {legs} | {_fmt(net)} |")
    lines += [
        "",
        f"Cumulative net P&L over the first {READ1_N_CARDS} valid sealed cards: "
        f"**{_fmt(r1['cumulative'])} USD**",
        "(house convention: $2,500 x 3 legs, 5bp RT per leg, 20th-session close)",
        f"READ-1 verdict: **{'DISAPPOINTMENT' if r1['disappointment'] else 'NO DISAPPOINTMENT'}**"
        " (cumulative <= 0)",
        "",
    ]
    if not r1["disappointment"]:
        lines += [
            "## Successor question CLOSED (§4.4)",
            "No DISAPPOINTMENT: the incumbent continues under its own kill-switches;",
            "the shadow book is reported descriptively elsewhere; no evaluation ran.",
            "",
        ]
        return "\n".join(lines)
    assert evaluation is not None
    d = evaluation["detail"]
    lines += [
        "## The single §4.5 evaluation (shadow book)",
        "",
        f"- valid shadow cards: {evaluation['n_sealed']}",
    ]
    for v in evaluation["voids"]:
        lines.append(f"- void logged: {v['exit']}: {v['detail']}")
    for r in evaluation["refusals"]:
        lines.append(f"- refusal logged: {r['exit']}: {r['detail']}")
    if evaluation["aa_failures"]:
        lines.append("- A/A determinism twin: FAIL")
        lines.extend(f"  - {f}" for f in evaluation["aa_failures"])
    else:
        lines.append("- A/A determinism twin: PASS (xsmom_rank reproduced every top-3)")
    lines += [
        "",
        "The five bars (all must hold; one evaluation):",
        f"- n >= 18 valid shadow cards: {'PASS' if evaluation['bars']['n_cards'] else 'FAIL'}"
        f" ({d['n_cards']})",
        f"- mean net > 0, sign-flip p < 0.05: "
        f"{'PASS' if evaluation['bars']['mean_positive_signflip'] else 'FAIL'}"
        f" (mean {d['mean_net']:.2f}, p {d['sign_flip_p']:.4f}, {SIGN_FLIP_DRAWS} draws)",
        f"- 95% CI-low > 0 vs random-3 null: "
        f"{'PASS' if evaluation['bars']['ci_low_vs_placebo'] else 'FAIL'}"
        f" (CI-low {d['placebo_ci_low']:.2f}; seed {SEED}, {PLACEBO_DRAWS} draws)",
        f"- paired mean diff vs incumbent > 0: "
        f"{'PASS' if evaluation['bars']['paired_vs_incumbent'] else 'FAIL'}"
        f" (mean {d['paired_mean']:.2f} over {d['paired_months']} common months,"
        f" month-clustered CI-low {d['paired_ci_low']:.2f})",
        f"- no dropped month flips the sign: "
        f"{'PASS' if evaluation['bars']['no_dropped_month_flips'] else 'FAIL'}"
        f" (worst dropped-mean {d['dropped_month_worst']:.2f})",
        "",
        "Holm within the 2-claim family {incumbent verdict, successor verdict}:",
        "- incumbent verdict: deterministic READ-1 threshold (p taken as 0 having fired)",
        f"- successor verdict: raw p {d['holm_raw']['successor']:.4f},"
        f" Holm-adjusted {d['holm_adjusted']['successor']:.4f}",
        "",
        f"Successor verdict: **{'PASSES' if evaluation['passes'] else 'FAILS'}** the bars.",
        "A PASS is only an owner-ruling request for the §6 item-4 re-seal (§7).",
        "",
    ]
    return "\n".join(lines)


def main(
    argv: list[str] | None = None,
    *,
    cal: Any | None = None,
    panel_override: dict[str, Any] | None = None,
    rng_seed: int = SEED,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--paper", default=None, help="paper-trades dir (default: DESK_PAPER_DIR)")
    ap.add_argument(
        "--appendix", default=None, help="XU-SUCCESSOR-001.md (voids live in Appendix A)"
    )
    ap.add_argument("--state", default=None, help="state root (default: TREX_DESK_STATE)")
    args = ap.parse_args(argv)

    paper = Path(args.paper) if args.paper else paper_dir()
    state = (Path(args.state) if args.state else state_root()) / "xu-successor"
    appendix = Path(args.appendix) if args.appendix else _default_appendix()
    out_report = state / REPORT_NAME
    marker = state / MARKER_NAME
    if marker.exists():
        print(f"REFUSED: the one-shot read already completed ({marker}); nothing written")
        return 4

    try:
        cards, voids = load_incumbent_series(paper, appendix)
        r1 = read1(cards, voids)
        if r1 is None:
            print(
                f"READ-1 not due: {len(cards)} sealed card(s) of record, fewer than"
                f" {READ1_N_CARDS} valid resolved; no marker written"
            )
            return 2
        inputs: dict[str, tuple[str, str]] = {
            "incumbent cards dir": (str(paper), _dir_sha(paper, "????-??-??-xsmom-top3.md")),
            "appendix (voids)": (str(appendix), _sha256(appendix)),
        }
        evaluation = None
        if r1["disappointment"]:
            panel = panel_override if panel_override is not None else _load_xu_panel(paper)
            evaluation = evaluate_shadow(
                paper, panel, cal or session_calendar(), r1["cards"], random.Random(rng_seed)
            )
            inputs["shadow ledger"] = (
                str(paper / XU_SHADOW_DIR / XU_SHADOW_LEDGER),
                _sha256(paper / XU_SHADOW_DIR / XU_SHADOW_LEDGER),
            )
            inputs["xu panel"] = (str(paper / xu_shadow.XU_PANEL_NAME), _panel_sha(paper))
    except ReadContractError as exc:
        print(f"CONTRACT ERROR (nothing written): {exc}")
        return 1

    report = build_report(
        {k: v for k, v in inputs.items()}, r1, evaluation, now().replace(microsecond=0)
    )
    state.mkdir(parents=True, exist_ok=True)
    out_report.write_text(report, encoding="utf-8")
    report_sha = _sha256(out_report)
    marker.write_text(
        json.dumps(
            {
                "schema": "xu-successor-read/1",
                "completed_at": now().replace(microsecond=0).isoformat(),
                "report": REPORT_NAME,
                "report_sha256": report_sha,
                "read1_cumulative_usd": str(r1["cumulative"]),
                "read1_disappointment": r1["disappointment"],
                "successor_passes": None if evaluation is None else evaluation["passes"],
            },
            indent=1,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {out_report} (sha256 {report_sha[:16]}…) + {marker}")
    print(
        f"READ-1 cumulative {r1['cumulative']} USD -> "
        f"{'DISAPPOINTMENT' if r1['disappointment'] else 'NO DISAPPOINTMENT'}"
        + (
            f"; successor {'PASSES' if evaluation['passes'] else 'FAILS'} the bars"
            if evaluation is not None
            else ""
        )
    )
    return 0


def _default_appendix() -> Path:
    from tree_options.desk.paths import repo_root

    return repo_root() / "docs" / "desk" / "XU-SUCCESSOR-001.md"


def _dir_sha(directory: Path, pattern: str) -> str:
    h = hashlib.sha256()
    for p in sorted(directory.glob(pattern)):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def _load_xu_panel(paper: Path) -> dict[str, Any]:
    from tree_options.desk.panel import read_panel

    return read_panel(paper / xu_shadow.XU_PANEL_NAME)


def _panel_sha(paper: Path) -> str:
    return _sha256(paper / xu_shadow.XU_PANEL_NAME)


if __name__ == "__main__":
    sys.exit(main())

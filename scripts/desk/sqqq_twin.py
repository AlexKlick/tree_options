#!/usr/bin/env python3
"""SQQQ-HEDGE-001 deterministic twin generator (research lane, paper-only).

Implements the sealed preregistration ``docs/desk/SQQQ-HEDGE-001.md``
verbatim: at an XSMOM-TOP3 card seal, buy an SQQQ hedge twin sized by the
trailing beta of the card's equal-weight top-3 basket on QQQ, and resolve it
at the card's own exit close. The twin READS the sealed card chain and never
writes it; everything this script writes lives under
``<paper-dir>/sqqq-twins/`` (twin card + a parallel twin ledger).

    .venv/bin/python scripts/desk/sqqq_twin.py \\
        --entry-card artifacts/paper-trades/2026-12-01-xsmom-top3.md \\
        --paper-dir  artifacts/paper-trades --write          # generate

    ... --resolve --write                                    # after the exit
                                                             # close is in the
                                                             # panel

Without ``--write`` the artifacts are printed to stdout and nothing touches
disk (the default; the serving checkout is only ever written by an
owner-authorized coordinator run). Refusals (sha mismatch, duplicate twin,
exit close not yet in the panel, a moved entry basis, a missing or
non-OPEN twin ledger row, a twin written by a different generator sha256)
raise :class:`Refusal` -> exit code 2 from the CLI wrapper; ``--resolve``
raises every one of them before its first write. Both writes (generate and
``--resolve``) commit the twin card and the twin ledger as one step: both
tmp files fsynced before either rename (see :func:`_commit_twin_pair`).

FREEZE: the twin records this script's sha256 when it is written and
``--resolve`` refuses unless it is the same script — so this file must not
change between a twin's write and its resolve.

Sealed-rule conventions implemented here, each pinned by a unit test:

* beta_hat window: the 60 panel sessions ending at the session BEFORE entry
  (QQQ's own session calendar), daily simple returns over the consecutive
  in-window close pairs (59 returns, NO anchor close one session earlier —
  this reproduces the sealed prereg's RT-4 verification stats: min 0.18,
  p10 1.11, median 1.63, p90 2.50, max 2.85 across the 46 historical
  month-start entries; the anchored house convention of desk/beta.py does
  NOT reproduce them).
* basket: equal-weight mean of the full-window names' daily returns; a name
  missing any window session drops out; >= 2 of 3 full windows required,
  else NO HEDGE (h_notional = 0, still recorded).
* OLS slope with intercept, math.fsum throughout; A/A deterministic: the
  twin card is a pure function of (card bytes, panel bytes, script bytes) —
  no wall-clock anywhere, so a re-derivation is byte-identical.
* costs: 5bp RT on the SQQQ leg; the unhedged card P&L is carried NET of
  the card legs' own 5bp RT ("same convention as card legs"); both gross
  and net are printed so either reading can be re-derived.
* financing/drag: NO separate decay charge — hold-to-exit is declared, the
  SQQQ close-to-close leg P&L already carries the 0.13%/day drag, and the
  RT-4 sub-era regime table travels with every artifact (the mean cost is
  regime-dependent, never a stable "decay + fees" constant).
"""

from __future__ import annotations

# ruff: noqa: RUF001
# The sealed prereg's text blocks below are quoted VERBATIM into the twin
# artifacts: β̂, U+2212 minus signs, em-dashes and ≈ are load-bearing bytes.
import argparse
import contextlib
import hashlib
import math
import os
import re
import shlex
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

# the house tmp writer (store._write_tmp: tmp file + flush + fsync) and its
# tmp naming, used through the module so the twin's commit shares one
# data-write primitive with every other desk atomic write
from tree_options.desk import store as _store  # noqa: E402
from tree_options.desk.panel import read_panel_with_sha256  # noqa: E402
from tree_options.desk.paths import paper_dir  # noqa: E402

# ------------------------------------------------ sealed pins (prereg L21-27)
CARD_PINS: dict[str, str] = {
    "2026-12-01-xsmom-top3.md": (
        "c70acd6883b8b27f89436154f1f080a52143b4911d31b7dbe3408bc070eb1891"
    ),
}
TWIN_NO = 1  # Card 8's twin = twin #1 of record (prereg clocks, L152-157)
PINNED_LEGS = (
    ("AMD", 466.84, 2500.0),
    ("LLY", 1410.99, 2500.0),
    ("INTC", 59.97, 2500.0),
)
BENCHMARK = "QQQ"
HEDGE_INSTRUMENT = "SQQQ"
WINDOW_SESSIONS = 60
MIN_FULL_NAMES = 2
BETA_CAP = 3.0
BOOK_NOTIONAL = 7500.0
RT = 0.0005  # 5bp round trip, charged once per leg at resolution
TWIN_SUBDIR = "sqqq-twins"

# --------------------------------------------- verbatim blocks (the prereg)
GROSS_EXPOSURE_HEADER = """\
**Gross-exposure disclosure (header, per RT-4 revision 6):** the twin's gross
exposure is <= 4x book notional at the β̂=3 cap (never reached in-sample; max
≈ $21.4k short-index-equivalent vs $7,500 long); net market exposure =
(β̂ − min(β̂, 3)) x $7,500 >= 0 — the twin is NEVER net short the market."""

RULE_BLOCK = """\
```
beta_hat   = OLS slope of the card's equal-weight top-3 basket daily returns
             on QQQ daily returns over the trailing 60 sessions ending at the
             session BEFORE entry (panel closes; names missing any session ->
             use the basket of names with full windows, require >= 2 of 3,
             else NO HEDGE)
h_notional = min( max(beta_hat, 0) , 3.0 ) / 3.0  *  $7,500   # SQQQ dollars to BUY
             (beta_hat <= 0 -> no hedge; cap = full $7,500)
entry      = buy h_notional of SQQQ at the card entry close; exit at the card exit close
costs      = 5bp RT on the SQQQ leg (same convention as card legs), crossing at closes
twin card  = (unhedged card P&L) + (SQQQ leg P&L) - 5bp RT on h_notional
```"""

BETA_CAP_FACTS = """\
β̂ reconstructed at all 46 historical XSMOM-TOP3 month-start entries (sealed
36-name roster, EW basket, 60-session OLS ending the session before entry):
**min 0.18, p10 1.11, median 1.63, p90 2.50, max 2.85; β̂ > 3 in 0/46; β̂ <= 0
(no-hedge branch) in 0/46; mean h/7500 = 0.56 (≈ $4,200)**. The cap is an
a-priori prudence bound (hedge notional <= book notional) that the panel era
never reaches; the no-hedge branch is equally inert. Nothing was selectable
post-hoc: no hedge outcome data existed when the rule was fixed."""

COSTS_BLOCK = """\
- **5bp RT on SQQQ is a CONVENTION, not a measurement** (RT-4 revision 4): no
  SQQQ spread corpus exists anywhere (SQQQ is `no_options_expression`; the
  measured desk corpus is CBOE option quotes — 18,783 on IWM/QQQ/SPY). Sanity
  arithmetic: SQQQ last panel close $42.46; a penny spread is ~4.7bp RT, so
  5bp ($3.75 on $7,500) is plausible-to-mildly-conservative for one of the
  most liquid ETFs. At paper level fees are 2.8% of the per-card gap noise.
- **Daily-reset decay (measured):** a −3x daily-reset ETF's log-growth ≈
  −3x QQQ log-growth − 6σ²(QQQ) per day; measured QQQ daily σ 1.46%
  full-panel / 1.36% last-252 → drag ≈ 0.13%/day ≈ 32%/yr (27.8%/yr at
  last-252 vol), before the ~0.95%/yr expense ratio.
- **The 20-session tracking gap — pooled numbers AND the sub-era table
  (RT-4 revision 3).** Pooled (1,270 overlapping windows, 2021-09..2026-11):
  mean +0.06%, median +0.08%, p10 −3.35% / p90 +3.83%, extremes ±17.8%,
  47.9% of windows underperform linear −3x. **The pooled mean is a regime
  cancellation and must not be quoted without this table:**

| era | mean 20-sess gap | sd | mechanism |
|---|---|---|---|
| 2021-09..2022-12 (zero-rate) | **−1.10%** | 3.69% | pure variance drag |
| 2023-2024 (high-rate) | **+0.78%** | 2.20% | daily replication gap runs +2.6bp/day ≈ **+6.5%/yr SQQQ financing tailwind** at 4-5% policy rates, offsetting the drag |
| 2025-2026 | **+0.03%** | 4.39% | includes the 2025-04-09 SQQQ −35.3% day |

  The hedge's **mean cost Y is regime-dependent** — "financing-vs-drag net,
  CI from the sub-era table, plus $3.75 fees" — NOT a stable "decay + fees"
  constant (the wave-1 wording was mis-signed; corrected here). A forward
  zero-rate regime restores ~−1.1% per 20 sessions (~−$46/card at mean h) of
  pure drag. Hold-to-exit (no daily rebalance) is declared: the twin
  measures the tradeable version, decay and all, dispersion ±3.5% ≈ ±0.5
  beta units per card."""

REGIME_TABLE = """\
| era | mean 20-sess gap | sd | mechanism |
|---|---|---|---|
| 2021-09..2022-12 (zero-rate) | **−1.10%** | 3.69% | pure variance drag |
| 2023-2024 (high-rate) | **+0.78%** | 2.20% | daily replication gap runs +2.6bp/day ≈ **+6.5%/yr SQQQ financing tailwind** at 4-5% policy rates, offsetting the drag |
| 2025-2026 | **+0.03%** | 4.39% | includes the 2025-04-09 SQQQ −35.3% day |"""

CLOCKS_BLOCK = """\
- **Clocks:** the incumbent book's own — read at the WP1 trigger's 18/20
  valid sealed XSMOM card clocks, **counting from Card 8 (card #1 of record,
  resolves 2026-12-01)**; twin #1 = Card 8's twin; report-only before that;
  ONE evaluation. Mean twin P&L > 0 is NOT required (the twin can
  legitimately lose the decay premium in a bull tape — that is the
  measurement working). The only promotion-shaped claims are the two readout
  forms above, with CIs."""

REGIME_MARKER = (
    "> regime (travels with every artifact, RT-4 revisions 3-4): −1.10% "
    "zero-rate era / +0.78% high-rate era (+2.6bp/day ≈ +6.5%/yr SQQQ "
    "financing tailwind) / +0.03% 2025-26; drag ≈ 0.13%/day ≈ 32%/yr; 5bp RT "
    "convention. Full sub-era table + gross-exposure disclosure: this row's "
    "twin card."
)

LEDGER_HEADER = f"""\
# SQQQ-HEDGE-001 twin ledger — parallel to the card chain, never part of it

One row per hedge twin (docs/desk/SQQQ-HEDGE-001.md, sealed). The sealed
card chain (../LEDGER.md and every *.md card file) is READ-ONLY to this
lane: this ledger and the twin cards under sqqq-twins/ are the only files
the twin generator ever writes. PAPER-ONLY.

{REGIME_MARKER}

| twin | card (sha256 16) | entry → exit | β̂ | h_notional | status | twin P&L | unhedged P&L |
|---|---|---|---:|---:|---|---:|---:|
"""


class Refusal(Exception):
    """A refusal that must leave every incumbent file untouched."""


class Stranded(Exception):
    """A twin commit failed AND its own rollback failed: the twin card and the
    twin ledger disagree. The message names the one command that restores the
    pre-commit state; nothing else is attempted."""


@dataclass(frozen=True)
class CardSpec:
    filename: str
    sha256: str
    entry_date: str
    exit_date: str
    legs: tuple[tuple[str, float, float], ...]  # (name, entry close, notional)

    @property
    def twin_filename(self) -> str:
        return f"{self.exit_date}-sqqq-twin.md"


@dataclass(frozen=True)
class BetaResult:
    beta: float | None
    reason: str
    window: tuple[str, ...]
    full_names: tuple[str, ...]
    partial_names: tuple[str, ...]
    n_returns: int


# ------------------------------------------------------------------- close


def _close_ok(rows, session: str) -> bool:
    bar = rows.get(session) if isinstance(rows, dict) else None
    if not isinstance(bar, dict):
        return False
    try:
        px = float(str(bar.get("close")))
    except (TypeError, ValueError):
        return False
    return math.isfinite(px) and px > 0.0


def _close(rows, session: str, what: str) -> float:
    if not _close_ok(rows, session):
        raise Refusal(f"no usable {what} close at session {session}")
    return float(str(rows[session]["close"]))


def same_cents(panel_px: float, sealed: str) -> bool:
    """The panel close, rounded to cents exactly as the sealed displays were
    (``:.2f`` on the float), equals the sealed cents value. Compared as
    parsed :class:`Decimal` numbers, never as formatted strings: a thousands
    separator on either side (``1,410.99``) must not decide the match — the
    pre-WT2-c string compare kept the comma on the panel side only, so any
    close >= $1,000 could never match."""
    return Decimal(f"{panel_px:.2f}") == Decimal(sealed.replace(",", ""))


def check_leg_entry_basis(spec: CardSpec, panel) -> None:
    """Every card leg's sealed entry close must still be the panel's close at
    the entry session, to the cent (the SQQQ cross-check's tolerance).
    ``resolution_block`` prices exit_px from the panel and entry_px from the
    sealed card: a split between entry and exit with a rebased panel would
    put the two on different bases and mis-state the leg P&L by the split
    ratio — refuse instead."""
    for name, sealed_px, _notional in spec.legs:
        panel_px = _close(panel.get(name), spec.entry_date, f"{name} entry-session")
        if not same_cents(panel_px, repr(sealed_px)):
            raise Refusal(
                f"panel {name} close at {spec.entry_date} (${panel_px:,.2f}) no "
                f"longer matches the sealed card entry (${sealed_px:,.2f}) — a "
                f"split or rebase would put entry and exit on different bases; "
                f"refusing to resolve against a moved panel"
            )


def resolved_ledger_bytes(ledger_path: Path, row: str) -> bytes:
    """The twin ledger with twin #N's OPEN row resolved, computed in memory —
    nothing is written. ``row`` is the resolved row's status + P&L cells; the
    OPEN row's identity cells (twin #, card + sha256 16, entry → exit, β̂,
    h_notional) are KEPT, so the resolved row still says which card it
    resolves and every number stays under its own header (WT2-c-fix F-C3).
    A missing ledger or one without the OPEN row refuses here, BEFORE the
    twin card is touched (a resolved card beside an OPEN row would block
    every re-run at the already-resolved check)."""
    if not ledger_path.is_file():
        raise Refusal(
            f"{ledger_path} does not exist — twin #{TWIN_NO} has no OPEN ledger "
            f"row to resolve; refusing with nothing written"
        )
    ledger = ledger_path.read_bytes().decode("utf-8")
    open_re = re.compile(rf"(\| #{TWIN_NO} \| [^\n]*?)\| OPEN \| — \| — \|", re.MULTILINE)
    if not open_re.search(ledger):
        raise Refusal(f"no OPEN ledger row for twin #{TWIN_NO} — refusing to touch {ledger_path}")
    return open_re.sub(lambda m: m.group(1) + row, ledger, count=1).encode("utf-8")


def _twin_row(ledger: bytes) -> str:
    return next(
        ln for ln in ledger.decode("utf-8").splitlines() if ln.startswith(f"| #{TWIN_NO} |")
    )


def _commit_twin_pair(
    card_path: Path, card_bytes: bytes, ledger_path: Path, ledger_bytes: bytes, *, new_card: bool
) -> None:
    """Commit the twin card and the twin ledger as ONE step — both of the
    lane's writes: generate (a NEW card, 11-27) and --resolve (the card
    replaced by itself + its RESOLUTION block, 12-01). WT2-c-fix F-C1/F-C2.

    1. BOTH new files are written as tmp files and fsynced (the house
       ``store._write_tmp``) while both real files are still untouched, so
       the I/O that can fail with ENOSPC fails before anything is published.
    2. Then the two renames run back to back: the card, then the ledger. A
       new card is published with ``os.link``, which never clobbers (the old
       ``open("x")``); a replaced card first keeps its pre-image as a hard
       link — no data write.
    3. If the ledger rename fails, the card is rolled back WITHOUT a data
       write (a new card is unlinked; a replaced card's pre-image is renamed
       back), so the rollback needs no free space and both files end
       byte-identical to before. If the rollback fails too, :class:`Stranded`
       names the one command that recovers, and the pre-image is kept.

    The one window this cannot close is a process kill between the two
    renames; the fsynced tmp / pre-image files left behind then hold both
    states."""
    card_tmp, ledger_tmp = _store._tmp_for(card_path), _store._tmp_for(ledger_path)
    pre = card_path.with_name(f".{card_path.name}.{os.getpid()}.pre")
    debris = [card_tmp, ledger_tmp]
    try:
        _store._write_tmp(card_path, card_bytes)
        _store._write_tmp(ledger_path, ledger_bytes)
        if new_card:
            try:
                os.link(card_tmp, card_path)
            except FileExistsError:
                raise Refusal(
                    f"{card_path} already exists — one twin per card, rerun refused"
                ) from None
        else:
            os.link(card_path, pre)
            debris.append(pre)
            os.replace(card_tmp, card_path)
        try:
            os.replace(ledger_tmp, ledger_path)
        except BaseException:
            try:
                if new_card:
                    card_path.unlink()  # rollback: the card did not exist before
                else:
                    os.replace(pre, card_path)  # rollback: by rename, no data write
            except BaseException as failed:
                if new_card:
                    fix = f"rm {shlex.quote(str(card_path))}"
                else:
                    debris.remove(pre)  # the pre-resolve card: keep it for the fix
                    fix = f"mv {shlex.quote(str(pre))} {shlex.quote(str(card_path))}"
                raise Stranded(
                    f"twin ledger {ledger_path} was not written and rolling the twin "
                    f"card back failed ({failed}): the card and the ledger disagree — "
                    f"run `{fix}` to restore the state before this run, then re-run"
                ) from failed
            raise
    finally:
        for leftover in debris:
            with contextlib.suppress(OSError):
                leftover.unlink()


def commit_resolution(
    twin_path: Path, card_raw: bytes, block: str, ledger_path: Path, ledger_bytes: bytes
) -> None:
    """The resolve's two writes, committed as one (:func:`_commit_twin_pair`):
    the card gains its RESOLUTION block BELOW its unchanged bytes
    (byte-identical to an append), the ledger its resolved row; neither is
    renamed into place before both are fsynced, and a failed ledger rename
    puts the card back as its exact pre-resolve file."""
    _commit_twin_pair(
        twin_path, card_raw + block.encode("utf-8"), ledger_path, ledger_bytes, new_card=False
    )


# ------------------------------------------------------------------- parse

_TITLE = re.compile(
    r"^#\s+Card \d+ .*\(entry (\d{4}-\d{2}-\d{2}) close -> exit "
    r"(\d{4}-\d{2}-\d{2}) close\)\s*$",
    re.MULTILINE,
)
_LEG = re.compile(
    r"^\|\s*([A-Z.]+)\s*\|[^|]+\|\s*([\d.]+)\s*\|\s*\$\s*([\d,]+)\s*(LONG|SHORT)"
    r"\s*\|\s*(\d{4}-\d{2}-\d{2}) close\s*\|\s*$",
    re.MULTILINE,
)


def parse_card(text: str, filename: str, *, sha256: str = "") -> CardSpec:
    m = _TITLE.search(text)
    if not m:
        raise Refusal(f"{filename}: no 'entry ... -> exit ...' clock in the title")
    entry_date, exit_date = m.group(1), m.group(2)
    legs = []
    for m in _LEG.finditer(text):
        name, price, notional, side, leg_exit = m.groups()
        if side != "LONG":
            raise Refusal(f"{filename}: leg {name} is {side} — the XSMOM-TOP3 book is LONG-only")
        if leg_exit != exit_date:
            raise Refusal(f"{filename}: leg {name} exits {leg_exit}, card clock says {exit_date}")
        legs.append((name, float(price), float(notional.replace(",", ""))))
    if tuple(legs) != PINNED_LEGS:
        pinned = " / ".join(f"{n} {p}" for n, p, _ in PINNED_LEGS)
        raise Refusal(
            f"{filename}: parsed legs differ from the pinned legs "
            f"({pinned}, 3 x $2,500, exit {exit_date})"
        )
    return CardSpec(
        filename=filename,
        sha256=sha256,
        entry_date=entry_date,
        exit_date=exit_date,
        legs=tuple(legs),
    )


# --------------------------------------------------------------- beta_hat


def ols_slope(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    mx = math.fsum(xs) / n
    my = math.fsum(ys) / n
    sxx = math.fsum((x - mx) ** 2 for x in xs)
    sxy = math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    if sxx <= 0.0:
        raise Refusal("QQQ returns have no variance in the beta window")
    return sxy / sxx


def beta_hat(
    panel,
    names: tuple[str, ...],
    entry_date: str,
    *,
    window_sessions: int = WINDOW_SESSIONS,
    benchmark: str = BENCHMARK,
    min_full: int = MIN_FULL_NAMES,
) -> BetaResult:
    """The sealed β̂: OLS slope of the EW basket's daily simple returns on the
    benchmark's, over the consecutive close pairs of the ``window_sessions``
    panel sessions ending the session before ``entry_date``."""
    bench_rows = panel.get(benchmark)
    if not isinstance(bench_rows, dict) or not bench_rows:
        raise Refusal(f"panel has no {benchmark} series")
    cal = sorted(bench_rows)
    window = [s for s in cal if s < entry_date][-window_sessions:]
    if len(window) < window_sessions:
        raise Refusal(
            f"only {len(window)} {benchmark} sessions before entry {entry_date} "
            f"(need {window_sessions})"
        )
    bench = [_close(bench_rows, s, benchmark) for s in window]
    full, partial = [], []
    for n in names:
        rows = panel.get(n)
        if isinstance(rows, dict) and all(_close_ok(rows, s) for s in window):
            full.append(n)
        else:
            partial.append(n)
    if len(full) < min_full:
        return BetaResult(
            beta=None,
            reason=(
                f"only {len(full)} of {len(names)} names with full windows "
                f"(need >= {min_full}) -> NO HEDGE"
            ),
            window=tuple(window),
            full_names=tuple(full),
            partial_names=tuple(partial),
            n_returns=0,
        )
    xs, ys = [], []
    for i in range(len(window) - 1):
        x = bench[i + 1] / bench[i] - 1.0
        rets = []
        for n in full:
            rows = panel[n]
            rets.append(
                float(str(rows[window[i + 1]]["close"])) / float(str(rows[window[i]]["close"]))
                - 1.0
            )
        xs.append(x)
        ys.append(math.fsum(rets) / len(rets))
    slope = ols_slope(xs, ys)
    reason = "hedged" if slope > 0.0 else f"beta_hat {slope:.6f} <= 0 -> no hedge"
    return BetaResult(
        beta=slope,
        reason=reason,
        window=tuple(window),
        full_names=tuple(full),
        partial_names=tuple(partial),
        n_returns=len(xs),
    )


def hedge_notional(beta: float | None) -> float:
    """min(max(beta,0),3)/3 x $7,500 — 0 for any beta <= 0 or None."""
    b = 0.0 if beta is None else beta
    h = min(max(b, 0.0), BETA_CAP) / BETA_CAP * BOOK_NOTIONAL
    return h


def _usd(x: float) -> str:
    return f"${x:,.2f}"


def _pnl(x: float) -> str:
    return f"{x:+,.2f}"


# -------------------------------------------------------------- artifacts


def twin_card_text(
    spec: CardSpec,
    res: BetaResult,
    h: float,
    sqqq_entry_close: float,
    *,
    panel_sha: str,
    script_sha: str,
) -> str:
    legs = " + ".join(n for n, _, _ in spec.legs)
    beta_cell = f"{res.beta:.6f}" if res.beta is not None else "NO HEDGE"
    branch = res.reason  # self-describing: hedged / no-hedge branch + why
    return f"""# SQQQ-HEDGE-001 twin #{TWIN_NO} — beta-hygiene hedge twin of Card 8 (XSMOM-TOP3 {legs})

PAPER-ONLY research artifact under docs/desk/SQQQ-HEDGE-001.md (sealed
preregistration). The twin READS the sealed card chain and never writes it;
this file and sqqq-twins/LEDGER.md are the twin lane's only writes.

{GROSS_EXPOSURE_HEADER}

## Provenance
- entry card: {spec.filename} (Card 8, card #1 of record) — sha256 {spec.sha256}
  (verified against the prereg pin before anything was computed)
- panel: ohlc-panel.json — sha256 {panel_sha}
- generator: scripts/desk/sqqq_twin.py — sha256 {script_sha}
- clock: entry {spec.entry_date} close -> exit {spec.exit_date} close (the card's own)

## The rule (sealed, verbatim)
{RULE_BLOCK}

## β̂ and hedge sizing
| field | value |
|---|---|
| window | {res.window[0]} .. {res.window[-1]} ({len(res.window)} sessions ending the session before entry) |
| returns used | {res.n_returns} consecutive in-window close pairs (no anchor session) |
| full-window names | {", ".join(res.full_names) or "—"} ({len(res.full_names)} of {len(spec.legs)}) |
| dropped (partial windows) | {", ".join(res.partial_names) or "—"} |
| beta_hat | {beta_cell} |
| branch | {branch} |
| h_notional | {_usd(h)} |
| formula | h_notional = min(max(β̂, 0), 3)/3 x $7,500 |
| SQQQ entry close | {_usd(sqqq_entry_close)} |
| entry session | {spec.entry_date} (SQQQ panel close; the card legs use the card's sealed entry prices) |

{BETA_CAP_FACTS}

## Costs, and the regime table that must travel with every artifact (sealed, verbatim)
{COSTS_BLOCK}

## Clocks (sealed, verbatim)
{CLOCKS_BLOCK}

## Status
OPEN — resolves at the {spec.exit_date} close, once that close is in the panel.
"""


def ledger_row(spec: CardSpec, res: BetaResult, h: float) -> str:
    beta_cell = f"{res.beta:.6f}" if res.beta is not None else "NO HEDGE"
    return (
        f"| #{TWIN_NO} | {spec.filename} ({spec.sha256[:16]}) | "
        f"{spec.entry_date} → {spec.exit_date} | {beta_cell} | {_usd(h)} | "
        f"OPEN | — | — |"
    )


def resolution_block(
    spec: CardSpec,
    panel,
    h: float,
    sq_entry: float,
    *,
    panel_sha: str,
    entry_panel_sha: str,
    script_sha: str,
) -> tuple[str, str, float, float]:
    """(block text, the resolved row's status + P&L cells, twin total,
    unhedged net). The cells REPLACE only the OPEN row's ``| OPEN | — | — |``
    tail — :func:`resolved_ledger_bytes` keeps its identity cells."""
    names = [n for n, _, _ in spec.legs]
    for n in [HEDGE_INSTRUMENT, *names]:
        rows = panel.get(n)
        if not isinstance(rows, dict) or spec.exit_date not in rows:
            raise Refusal(
                f"exit close {spec.exit_date} not in the panel yet "
                f"(needed for {n}) — re-arm once the exit session lands"
            )
    sq_exit = _close(panel[HEDGE_INSTRUMENT], spec.exit_date, HEDGE_INSTRUMENT)
    leg_rows = []
    leg_pnls = []
    for name, entry_px, notional in spec.legs:
        exit_px = _close(panel[name], spec.exit_date, name)
        leg_pnls.append(notional * (exit_px / entry_px - 1.0))
        leg_rows.append(
            f"| {name} (card) | {entry_px:.2f} | {exit_px:.2f} | "
            f"{_usd(notional)} | {_pnl(leg_pnls[-1])} |"
        )
    sqqq_leg = round(h * (sq_exit / sq_entry - 1.0), 2)
    leg_rows.append(
        f"| {HEDGE_INSTRUMENT} (hedge) | {sq_entry:.2f} | {sq_exit:.2f} | "
        f"{_usd(h)} | {_pnl(sqqq_leg)} |"
    )
    gross = round(math.fsum(leg_pnls), 2)
    card_rt = round(RT * math.fsum(n for _, _, n in spec.legs), 2)
    unhedged_net = round(gross - card_rt, 2)
    hedge_cost = round(RT * h, 2)
    twin_total = round(unhedged_net + sqqq_leg - hedge_cost, 2)
    text = f"""
## RESOLUTION — exit {spec.exit_date} close

| leg | entry | exit | notional | P&L |
|---|---:|---:|---:|---:|
{chr(10).join(leg_rows)}

- unhedged card P&L: gross {_pnl(gross)}; card-leg 5bp RT on {_usd(7500.0)} = −{_usd(card_rt)}; net {_pnl(unhedged_net)}
- SQQQ leg P&L: {_pnl(sqqq_leg)} (hold-to-exit, decay and all — regime table above governs the mean cost)
- hedge 5bp RT on h_notional: −{_usd(hedge_cost)}
- **twin total = unhedged net + SQQQ leg − hedge RT = {_pnl(twin_total)} USD**

No separate decay/financing charge is applied: hold-to-exit is declared, the
SQQQ close-to-close leg P&L already carries the measured drag, and the mean
cost stays regime-dependent (RT-4 revisions 3-4) — never "decay + fees".

resolution provenance: card sha256 {spec.sha256} (re-verified); panel sha256
{panel_sha} (panel at generation: {entry_panel_sha}); generator sha256
{script_sha} (re-verified: the twin's own generator).
"""
    row = f"| RESOLVED {spec.exit_date} | {_pnl(twin_total)} | {_pnl(unhedged_net)} |"
    return text, row, twin_total, unhedged_net


# ------------------------------------------------------------------- main


def _verify_card(path: Path) -> CardSpec:
    if path.name not in CARD_PINS:
        raise Refusal(f"{path.name}: not a pinned twin-eligible card {sorted(CARD_PINS)}")
    got = hashlib.sha256(path.read_bytes()).hexdigest()
    want = CARD_PINS[path.name]
    if got != want:
        raise Refusal(f"{path.name}: sha256 {got} is not the pinned {want}")
    return parse_card(path.read_text(), path.name, sha256=got)


def generate(spec: CardSpec, paper: Path, *, write: bool) -> int:
    twins = paper / TWIN_SUBDIR
    twin_path = twins / spec.twin_filename
    ledger_path = twins / "LEDGER.md"
    if twin_path.exists():
        raise Refusal(f"{twin_path} already exists — one twin per card, rerun refused")
    prior = ledger_path.read_bytes() if ledger_path.exists() else b""
    if f"| #{TWIN_NO} |".encode() in prior:
        raise Refusal(f"twin #{TWIN_NO} already has a ledger row — rerun refused")
    panel, panel_sha = read_panel_with_sha256(paper / "ohlc-panel.json")
    sq_rows = panel.get(HEDGE_INSTRUMENT)
    if not isinstance(sq_rows, dict) or not _close_ok(sq_rows, spec.entry_date):
        raise Refusal(
            f"no usable {HEDGE_INSTRUMENT} close at the entry session "
            f"{spec.entry_date} — re-arm when the panel catches up"
        )
    sq_entry = float(str(sq_rows[spec.entry_date]["close"]))
    names = tuple(n for n, _, _ in spec.legs)
    res = beta_hat(panel, names, spec.entry_date)
    h = hedge_notional(res.beta)
    script_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    card = twin_card_text(spec, res, h, sq_entry, panel_sha=panel_sha, script_sha=script_sha)
    row = ledger_row(spec, res, h) + "\n" + REGIME_MARKER + "\n"
    # append-only: the new ledger is the prior bytes + this twin's row (the
    # header only for a missing or empty ledger), committed WITH the card
    ledger_bytes = prior + (row if prior else LEDGER_HEADER + row).encode("utf-8")
    if write:
        twins.mkdir(parents=True, exist_ok=True)
        _commit_twin_pair(twin_path, card.encode("utf-8"), ledger_path, ledger_bytes, new_card=True)
    print(card)
    print(row)
    if not write:
        print("(dry run: nothing written; pass --write to create the twin artifacts)")
    return 0


def resolve(spec: CardSpec, paper: Path, *, write: bool) -> int:
    twins = paper / TWIN_SUBDIR
    twin_path = twins / spec.twin_filename
    ledger_path = twins / "LEDGER.md"
    if not twin_path.exists():
        raise Refusal(f"{twin_path} does not exist — generate the twin before resolving it")
    card_raw = twin_path.read_bytes()
    text = card_raw.decode("utf-8")
    if "## RESOLUTION" in text:
        raise Refusal(f"{twin_path} already carries a RESOLUTION block — one per twin")
    h_m = re.search(r"\| h_notional \| \$([\d,]+\.\d{2}) \|", text)
    sq_m = re.search(r"\| SQQQ entry close \| \$([\d,]+\.\d{2}) \|", text)
    if not h_m or not sq_m:
        raise Refusal(f"{twin_path}: cannot re-read the sealed h_notional / SQQQ entry")
    # identity (WT2-c-fix): the resolver must be the very script that wrote
    # the twin — the twin records its generator's sha256 at the 11-27 write
    script_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    recorded_gen = re.findall(
        r"^- generator: scripts/desk/sqqq_twin\.py — sha256 ([0-9a-f]{64})$", text, re.MULTILINE
    )
    if len(recorded_gen) != 1:
        raise Refusal(f"{twin_path}: cannot re-read the generator sha256 the twin was written by")
    if recorded_gen[0] != script_sha:
        raise Refusal(
            f"{twin_path.name} records generator sha256 {recorded_gen[0]} but this resolver "
            f"is sha256 {script_sha} — scripts/desk/sqqq_twin.py changed between the twin "
            f"write and its resolve; refusing with nothing written (the script is frozen "
            f"from the twin write until the twin resolves)"
        )
    h = float(h_m.group(1).replace(",", ""))
    panel, panel_sha = read_panel_with_sha256(paper / "ohlc-panel.json")
    sq_rows = panel.get(HEDGE_INSTRUMENT)
    if not isinstance(sq_rows, dict) or not _close_ok(sq_rows, spec.entry_date):
        raise Refusal(
            f"no usable {HEDGE_INSTRUMENT} close at the entry session "
            f"{spec.entry_date} — the twin card's sealed entry cannot be priced"
        )
    sq_entry = float(str(sq_rows[spec.entry_date]["close"]))
    # cross-check the sealed display against the panel (immutable history):
    # h stays SEALED from the twin card (never recomputed), the entry close is
    # priced exactly from the panel; compared as parsed Decimals, so a
    # >= $1,000 close (comma in the sealed display) can match
    if not same_cents(sq_entry, sq_m.group(1)):
        raise Refusal(
            f"panel {HEDGE_INSTRUMENT} close at {spec.entry_date} "
            f"(${sq_entry:,.2f}) no longer matches the sealed twin card "
            f"(${sq_m.group(1)}) — refusing to resolve against a moved panel"
        )
    # ...and every card leg's sealed entry close, at the same tolerance
    check_leg_entry_basis(spec, panel)
    entry_panel_sha = re.search(r"ohlc-panel\.json — sha256 ([0-9a-f]{64})", text)
    block, row, _twin_total, _unhedged_net = resolution_block(
        spec,
        panel,
        h,
        sq_entry,
        panel_sha=panel_sha,
        entry_panel_sha=entry_panel_sha.group(1) if entry_panel_sha else "unknown",
        script_sha=script_sha,
    )
    # validate-before-write (WT2-c): the ledger is read, its OPEN row required
    # and the resolved ledger built IN MEMORY before the first byte is written
    ledger_bytes = resolved_ledger_bytes(ledger_path, row)
    if write:
        commit_resolution(twin_path, card_raw, block, ledger_path, ledger_bytes)
    print(block)
    print(_twin_row(ledger_bytes))
    if not write:
        print("(dry run: nothing written; pass --write to record the resolution)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--entry-card", required=True, type=Path, help="the sealed card file (sha256-pinned)"
    )
    ap.add_argument(
        "--paper-dir",
        type=Path,
        default=None,
        help="the paper-trades tree (default: the desk paper dir)",
    )
    ap.add_argument(
        "--resolve", action="store_true", help="resolve the twin at the card exit close"
    )
    ap.add_argument(
        "--write",
        action="store_true",
        help="write the artifacts (default: print only, touch nothing)",
    )
    args = ap.parse_args(argv)
    paper = args.paper_dir or paper_dir()
    spec = _verify_card(args.entry_card)
    if args.resolve:
        return resolve(spec, paper, write=args.write)
    return generate(spec, paper, write=args.write)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Refusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except Stranded as exc:
        print(f"STRANDED: {exc}", file=sys.stderr)
        raise SystemExit(3) from None

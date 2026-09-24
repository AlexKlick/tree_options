"""The deal miner's sealed selection rules (plan D6 / E6):
``data/desk/miner/v<N>.toml``.

The playbook has no selection thresholds, so the rule that turns valued
deals into the entry queue (what Wave 3's desk-enter will auto-enter) is a
sealed, versioned file of its own, with the playbook's seal mechanism:
``<name>.sha256`` holds the sha256 of the file bytes, ``SEALS.md`` one
append-only row per version, and :data:`APPROVED` pins the digest in
reviewed code, so a coordinated edit of file, sidecar and log still fails
to load. v1 is PROPOSED (the lane's conservative default) pending an
operator ruling before E6 goes live.

The loader is fail-closed with no defaults: every key is stated, unknown
keys are refused, money is a string, and the policy strings name the only
behavior the miner implements (a file must describe what the code does;
the pinned fills must be :mod:`desk.pricing`'s own constants).

The pure decision helpers live here too: :func:`decision_of` (which EV a
row decides on), :func:`selection_failures` (the thresholds) and
:func:`rank_key` (the queue order).
"""

from __future__ import annotations

import hashlib
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from tree_options.desk import paths, pricing
from tree_options.desk.store import atomic_write_bytes

SCHEMA = "desk-miner/1"
SEALS_FILE = "SEALS.md"
# approved versions, pinned by sha256 in reviewed, gated code
APPROVED: Mapping[str, str] = MappingProxyType(
    {
        # PROPOSED 2026-09-24, pending an operator ruling before E6 goes live
        "v1.toml": "f34c96fc23bd73a75bbc932b6c053b9134e6a2a96da535f075c666697e5f826b",
    }
)
ACTIVE_FILE = "v1.toml"
STATUSES = frozenset({"PROPOSED", "RULED"})
# the only behaviors the miner implements (desk.miner)
LIMIT_POLICY = "first_cent_beyond_base_fill"
ON_LIMIT_NOT_OK = "refuse"
RAILS = "all_pass"
DECISION_EV = "signal_ev_on_signal_rows_else_no_view_ev"
RANK = "ev_per_max_loss_desc"
TIE_BREAKS = ("ev_desc", "max_loss_asc", "deal_id_asc")
JOINT = "greedy_rails_recheck"
MIN_N_PATHS = 1000
_RATIO = Decimal("0.0001")

# the refusal codes of selection_failures
STRESS_EV_NOT_POSITIVE = "stress_fill_ev_not_positive"
RATIO_BELOW_MIN = "ev_per_max_loss_below_min"


class MinerConfigError(ValueError):
    pass


class MinerSealError(MinerConfigError):
    pass


@dataclass(frozen=True)
class Selection:
    min_stress_fill_ev_usd: Decimal  # the stress-fill decision EV must be STRICTLY above
    min_ev_per_max_loss: Decimal  # the base-fill decision EV / max loss must be AT LEAST


@dataclass(frozen=True)
class MinerConfig:
    sha256: str
    version: str
    written: str
    status: str  # PROPOSED | RULED
    ruling: str
    n_paths: int
    fill_k: Decimal
    stress_fill_k: Decimal
    max_valued_per_name_row: int
    selection: Selection


# ------------------------------------------------------------------ parse


def _table(obj: Any, where: str, keys: Sequence[str]) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise MinerConfigError(f"{where}: not a table")
    missing = set(keys) - set(obj)
    unknown = set(obj) - set(keys)
    if missing or unknown:
        raise MinerConfigError(
            f"{where}: missing {sorted(missing)}, unknown {sorted(unknown)} (no defaults)"
        )
    return obj


def _str(v: Any, where: str, choices: Sequence[str] | frozenset[str] | None = None) -> str:
    if not isinstance(v, str) or not v:
        raise MinerConfigError(f"{where}: a non-empty string")
    if choices is not None and v not in choices:
        raise MinerConfigError(f"{where}: {v!r} not in {sorted(choices)}")
    return v


def _int(v: Any, where: str, lo: int) -> int:
    if type(v) is not int or v < lo:
        raise MinerConfigError(f"{where}: an integer >= {lo}")
    return v


def _dec(v: Any, where: str) -> Decimal:
    if not isinstance(v, str):
        raise MinerConfigError(f"{where}: a decimal string (never a TOML float)")
    try:
        d = Decimal(v)
    except InvalidOperation as exc:
        raise MinerConfigError(f"{where}: {v!r} is not a decimal") from exc
    if not d.is_finite():
        raise MinerConfigError(f"{where}: not finite")
    return d


def parse_config(doc: Mapping[str, Any], *, sha256: str) -> MinerConfig:
    """Validate a parsed selection document (fail-closed, no defaults)."""
    t = _table(
        dict(doc),
        "miner",
        (
            "schema",
            "version",
            "written",
            "status",
            "ruling",
            "plan",
            "valuation",
            "limit",
            "selection",
        ),
    )
    _str(t["schema"], "schema", (SCHEMA,))
    _str(t["plan"], "plan")
    v = _table(
        t["valuation"],
        "[valuation]",
        ("n_paths", "fill_k", "stress_fill_k", "max_valued_per_name_row"),
    )
    fill_k = _dec(v["fill_k"], "[valuation] fill_k")
    stress_k = _dec(v["stress_fill_k"], "[valuation] stress_fill_k")
    if fill_k != Decimal(repr(pricing.FILL_K)) or stress_k != Decimal(repr(pricing.STRESS_FILL_K)):
        raise MinerConfigError(
            f"[valuation] fills {fill_k}/{stress_k} are not the pricer's "
            f"{pricing.FILL_K}/{pricing.STRESS_FILL_K}"
        )
    lim = _table(t["limit"], "[limit]", ("policy", "on_limit_not_ok"))
    _str(lim["policy"], "[limit] policy", (LIMIT_POLICY,))
    _str(lim["on_limit_not_ok"], "[limit] on_limit_not_ok", (ON_LIMIT_NOT_OK,))
    s = _table(
        t["selection"],
        "[selection]",
        (
            "rails",
            "decision_ev",
            "min_stress_fill_ev_usd",
            "min_ev_per_max_loss",
            "rank",
            "tie_breaks",
            "joint",
        ),
    )
    _str(s["rails"], "[selection] rails", (RAILS,))
    _str(s["decision_ev"], "[selection] decision_ev", (DECISION_EV,))
    _str(s["rank"], "[selection] rank", (RANK,))
    _str(s["joint"], "[selection] joint", (JOINT,))
    if tuple(s["tie_breaks"]) != TIE_BREAKS or not isinstance(s["tie_breaks"], list):
        raise MinerConfigError(f"[selection] tie_breaks: must be {list(TIE_BREAKS)}")
    return MinerConfig(
        sha256=sha256,
        version=_str(t["version"], "version"),
        written=_str(t["written"], "written"),
        status=_str(t["status"], "status", STATUSES),
        ruling=_str(t["ruling"], "ruling"),
        n_paths=_int(v["n_paths"], "[valuation] n_paths", MIN_N_PATHS),
        fill_k=fill_k,
        stress_fill_k=stress_k,
        max_valued_per_name_row=_int(
            v["max_valued_per_name_row"], "[valuation] max_valued_per_name_row", 1
        ),
        selection=Selection(
            min_stress_fill_ev_usd=_dec(
                s["min_stress_fill_ev_usd"], "[selection] min_stress_fill_ev_usd"
            ),
            min_ev_per_max_loss=_dec(s["min_ev_per_max_loss"], "[selection] min_ev_per_max_loss"),
        ),
    )


# ------------------------------------------------------------------- seal


def _seal_rows(seals: Path, name: str) -> list[str]:
    try:
        lines = seals.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("| ") and len(cells) >= 3 and cells[1] == name:
            out.append(cells[2])
    return out


def _sidecar(path: Path) -> str | None:
    try:
        fields = path.with_suffix(".sha256").read_text().split()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise MinerSealError(f"{path.name}: unreadable sha256 sidecar") from exc
    if len(fields) != 2 or fields[1] != path.name:
        raise MinerSealError(f"{path.name}: malformed sha256 sidecar")
    return fields[0]


def _parse_file(path: Path, data: bytes, sha: str) -> MinerConfig:
    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise MinerConfigError(f"{path.name}: not TOML ({exc})") from exc
    cfg = parse_config(doc, sha256=sha)
    if f"{cfg.version}.toml" != path.name:
        raise MinerConfigError(f"{path.name}: declares version {cfg.version!r}")
    return cfg


def load_config(path: Path | None = None) -> MinerConfig:
    """A sealed, approved selection version (default: the active one,
    ``<DESK_MINER_DIR>/`` :data:`ACTIVE_FILE`). Refuses bytes that differ
    from the sidecar, from the digest pinned in :data:`APPROVED`, or from
    the one SEALS.md row the version must have."""
    path = path or paths.miner_dir() / ACTIVE_FILE
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise MinerSealError(f"{path.name}: unreadable ({type(exc).__name__})") from exc
    got = hashlib.sha256(data).hexdigest()
    sealed = _sidecar(path)
    if sealed is None:
        raise MinerSealError(f"{path.name}: no sha256 sidecar")
    if got != sealed:
        raise MinerSealError(f"{path.name}: sha256 {got[:12]} != sealed {sealed[:12]}")
    approved = APPROVED.get(path.name)
    if approved != got:
        raise MinerSealError(
            f"{path.name}: sha256 {got[:12]} is not the approved digest"
            + (f" {approved[:12]}" if approved else " (no approved version by this name)")
        )
    rows = _seal_rows(path.parent / SEALS_FILE, path.name)
    if got not in rows:
        raise MinerSealError(f"{path.name}: sha256 {got[:12]} has no row in {SEALS_FILE}")
    if len(rows) != 1:
        raise MinerSealError(f"{path.name}: {len(rows)} rows in {SEALS_FILE}; a version has one")
    return _parse_file(path, data, got)


_SEALS_HEADER = """\
# Desk deal-miner selection rules: append-only seal log

Each row seals one version (sha256 of the file bytes, also in the
``.sha256`` sidecar). ``tree_options.desk.selection.load_config`` refuses a
file whose bytes do not match its sidecar, whose sha256 is not pinned in
``APPROVED``, or that has no row (or more than one) here. Rows are only
ever appended; never edit or delete a row.

| sealed (UTC) | file | sha256 | status | basis |
|---|---|---|---|---|
"""


def seal_config(path: Path, *, basis: str, sealed_at: datetime | None = None) -> str:
    """Validate a NEW version, write its sidecar and append its one seal
    row. Sealing identical bytes again changes nothing; other bytes under a
    sealed name are refused (a change is a new version). The version loads
    only once its digest is pinned in :data:`APPROVED`."""
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    cfg = _parse_file(path, data, sha)
    seals = path.parent / SEALS_FILE
    prior = {s for s in (_sidecar(path),) if s is not None} | set(_seal_rows(seals, path.name))
    if prior - {sha}:
        raise MinerSealError(f"{path.name} is already sealed with other bytes; a new version")
    if sha in _seal_rows(seals, path.name):
        if _sidecar(path) is None:
            atomic_write_bytes(path.with_suffix(".sha256"), f"{sha}  {path.name}\n".encode("ascii"))
        return sha
    atomic_write_bytes(path.with_suffix(".sha256"), f"{sha}  {path.name}\n".encode("ascii"))
    if not seals.exists():
        atomic_write_bytes(seals, _SEALS_HEADER.encode("ascii"))
    at = (sealed_at or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(seals, "a") as fh:
        fh.write(f"| {at} | {path.name} | {sha} | {cfg.status} | {basis} |\n")
    return sha


# ---------------------------------------------------------------- decide


class ValuationLike(Protocol):
    @property
    def ev(self) -> Decimal: ...
    @property
    def ev_fill_stress(self) -> Decimal: ...
    @property
    def ev_signal(self) -> Decimal | None: ...
    @property
    def ev_signal_fill_stress(self) -> Decimal | None: ...
    @property
    def max_loss(self) -> Decimal: ...


@dataclass(frozen=True)
class Decision:
    """The EV a row decides on (money in dollars, per deal)."""

    basis: str  # signal | no_view
    ev: Decimal  # base fills
    ev_stress_fill: Decimal  # stress fills, same paths
    max_loss: Decimal
    ev_per_max_loss: Decimal  # ev / max loss, 4 places (display); rank and gate use it exact

    @property
    def exact_ratio(self) -> Decimal:
        return self.ev / self.max_loss


def decision_of(val: ValuationLike, *, signal_view: bool) -> Decision | None:
    """The signal EV on a signal row, the no-view EV elsewhere; None when
    a signal row's view was not valued or the max loss is not positive."""
    if signal_view:
        if val.ev_signal is None or val.ev_signal_fill_stress is None:
            return None
        basis, ev, stress = "signal", val.ev_signal, val.ev_signal_fill_stress
    else:
        basis, ev, stress = "no_view", val.ev, val.ev_fill_stress
    if not val.max_loss > 0:
        return None
    ratio = (ev / val.max_loss).quantize(_RATIO, rounding=ROUND_HALF_UP)
    return Decision(basis, ev, stress, val.max_loss, ratio)


def selection_failures(dec: Decision, cfg: MinerConfig) -> list[str]:
    """The thresholds a deal misses (empty: it passes them)."""
    out = []
    if not dec.ev_stress_fill > cfg.selection.min_stress_fill_ev_usd:
        out.append(STRESS_EV_NOT_POSITIVE)
    if not dec.ev >= cfg.selection.min_ev_per_max_loss * dec.max_loss:
        out.append(RATIO_BELOW_MIN)
    return out


def rank_key(
    dec: Decision, max_loss: Decimal, deal_id: str
) -> tuple[Decimal, Decimal, Decimal, str]:
    """Sort ascending: ratio desc, EV desc, max loss asc, deal id asc."""
    return (-dec.exact_ratio, -dec.ev, max_loss, deal_id)

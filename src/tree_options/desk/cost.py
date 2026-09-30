"""Opt-in modeled package costs derived from reported EOD quote marginals.

Ported from peer dec366635025404af77ad879d605f70c90269748. Its reported
18,783-row calibration supplies delta-band and DTE-band marginals, not jointly
measured cells. The multiplicative surface is an assumption for sensitivity
research. These constants have not been revalidated against the underlying
corpus in this integration; they supply neither historical decision-clock
quotes nor exact fill economics. Unknown inputs refuse the entire package.

A leg's source fields describe the supplied delta classification. They do not
make the separate EOD cost calibration an observation of that leg's execution.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

__all__ = [
    "COMMISSION_PER_LEG",
    "CONTRACT_MULTIPLIER",
    "COST_SCHEMA",
    "DELTA_BUCKET_EDGES",
    "DELTA_BUCKET_LABELS",
    "DTE_BANDS",
    "DTE_BAND_LABELS",
    "DTE_BAND_MULTIPLIER",
    "FILLS_PER_LEG",
    "HALF_SPREAD_QUANTUM",
    "MAX_ABS_DELTA",
    "MAX_DTE",
    "MEASURED_MEDIAN_FULL_SPREAD",
    "MIN_DTE",
    "REASON_TOKENS",
    "TRADEABLE_SYMBOLS",
    "CostModelError",
    "CostProvenance",
    "Leg",
    "LegOutsideTradeableUniverseError",
    "LegQuote",
    "LegRef",
    "NoPriceLedger",
    "ProvenanceError",
    "SpreadCostModel",
    "SpreadQuote",
    "UnpricedCostError",
    "delta_bucket",
    "dte_bucket",
]

#: pinned in every :meth:`SpreadQuote.to_json` payload
COST_SCHEMA: str = "desk-derived-cost/1"

#: the universe the corpus measured. The marginals are POOLED across the
#: three -- there is no per-symbol table and none may be invented -- but an
#: unmeasured symbol is still refused rather than borrowed from a neighbour.
TRADEABLE_SYMBOLS: frozenset[str] = frozenset({"IWM", "QQQ", "SPY"})

COMMISSION_PER_LEG: Decimal = Decimal("0.65")
CONTRACT_MULTIPLIER: int = 100
#: every leg fills twice: open and close
FILLS_PER_LEG: int = 2
HALF_SPREAD_QUANTUM: Decimal = Decimal("0.000001")

MAX_ABS_DELTA: Decimal = Decimal("0.70")
MIN_DTE: int = 7
MAX_DTE: int = 60

#: MEASURED median FULL two-sided quoted spread ($/share) per |delta| band.
MEASURED_MEDIAN_FULL_SPREAD: tuple[Decimal, ...] = (
    Decimal("0.020"),
    Decimal("0.030"),
    Decimal("0.050"),
    Decimal("0.060"),
    Decimal("0.190"),
)
DELTA_BUCKET_EDGES: tuple[Decimal, ...] = (
    Decimal("0.10"),
    Decimal("0.20"),
    Decimal("0.35"),
    Decimal("0.50"),
    Decimal("0.70"),
)
DELTA_BUCKET_LABELS: tuple[str, ...] = (
    "0.00-0.10",
    "0.10-0.20",
    "0.20-0.35",
    "0.35-0.50",
    "0.50-0.70",
)

#: MEASURED dte marginals, as the ratio to the shortest band, at 3 dp.
#: 0.030 : 0.040 : 0.050 == 1.000 : 1.333 : 1.667
DTE_BANDS: tuple[tuple[int, int], ...] = ((7, 21), (22, 45), (46, 60))
DTE_BAND_LABELS: tuple[str, ...] = ("7-21", "22-45", "46-60")
DTE_BAND_MULTIPLIER: tuple[Decimal, ...] = (
    Decimal("1.000"),
    Decimal("1.333"),
    Decimal("1.667"),
)

#: the refusal vocabulary, in DETECTION ORDER -- the first match wins.
REASON_TOKENS: tuple[str, ...] = (
    "no_legs",
    "no_delta",
    "delta_unavailable",
    "unknown_symbol",
    "delta_out_of_universe",
    "dte_out_of_universe",
    "unmeasured_cell",
    "symbol_pool_unknown",
    "invalid_delta",
    "incomplete_package",
    "inconsistent_dte",
    "invalid_provenance",
    "future_cost_input",
)


# --------------------------------------------------------------- refusals


class CostModelError(ValueError):
    """Base for every cost-model refusal.

    It subclasses ``ValueError`` on purpose: ``outcomes.main`` already wraps
    its body in ``except (ValueError, ArithmeticError, OSError, KeyError)``
    and turns a hit into ``refused: ... / return 2``. A ``LookupError`` here
    would escape the CLI as an unhandled traceback instead.
    """


class UnpricedCostError(CostModelError):
    """A measurement gap. Carries the reason and the offending key.

    ``reason`` is one of :data:`REASON_TOKENS``. A gap is never priced at a
    default, clamped to a boundary, or dropped from the package.

    ``key`` is the offending :class:`Leg` when the caller supplied one, and a
    bare :class:`LegRef` when the standalone surfaces (``delta_bucket``,
    ``dte_bucket``, ``half_spread_per_share``) refuse before any leg exists.
    Both name the failing inputs, so a caller never has to re-derive which
    half of the key it passed.
    """

    def __init__(self, reason: str, key: Leg | LegRef) -> None:
        if reason not in REASON_TOKENS:
            raise ValueError(f"unknown refusal reason {reason!r}; known: {list(REASON_TOKENS)}")
        super().__init__(f"no price ({reason}) for {key.describe()}")
        self.reason = reason
        self.key = key

    def as_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "key": self.key.to_json()}


class LegOutsideTradeableUniverseError(UnpricedCostError):
    """|delta| or dte outside the measured universe.

    It is an :class:`UnpricedCostError` so a caller that catches the general
    refusal still works, and it names WHICH field was out so the operator can
    act on it.
    """


class ProvenanceError(CostModelError):
    """A provenance field is blank, or an observation claims a decision clock
    that this corpus cannot support."""


# ------------------------------------------------------------------- buckets


def _delta_index(abs_delta: Decimal) -> int:
    """Right-closed ``(lo, hi]`` banding: the band is the count of edges
    STRICTLY below ``abs_delta``, so 0.00 and 0.10 are both in band 0.

    Right-closed is the convention that reproduces the measured per-bucket
    row counts exactly (8,504 / 2,717 / 2,723 / 2,223 / 2,616).
    """
    index = 0
    for edge in DELTA_BUCKET_EDGES:
        if abs_delta > edge:
            index += 1
    return index


def _dte_index(dte: int) -> int:
    for band, (low, high) in enumerate(DTE_BANDS):
        if low <= dte <= high:
            return band
    raise LegOutsideTradeableUniverseError("dte_out_of_universe", LegRef("", Decimal("0"), dte))


def _checked_delta_index(abs_delta: Decimal) -> int:
    """``_delta_index`` with the range refusal attached to a probe key.

    The standalone surfaces (``delta_bucket``, ``half_spread_per_share``) have
    no caller-supplied leg to name, so the error carries what they know.
    ``SpreadCostModel.price`` does the same check against the REAL leg.
    """
    if not isinstance(abs_delta, Decimal) or not abs_delta.is_finite():
        raise UnpricedCostError("invalid_delta", LegRef("", abs_delta, MIN_DTE))
    if abs_delta < 0 or abs_delta > MAX_ABS_DELTA:
        raise LegOutsideTradeableUniverseError(
            "delta_out_of_universe", LegRef("", abs_delta, MIN_DTE)
        )
    return _delta_index(abs_delta)


def _checked_dte_index(dte: int) -> int:
    if isinstance(dte, bool) or not isinstance(dte, int) or dte < MIN_DTE or dte > MAX_DTE:
        raise LegOutsideTradeableUniverseError("dte_out_of_universe", LegRef("", Decimal("0"), dte))
    return _dte_index(dte)


def delta_bucket(abs_delta: Decimal) -> str:
    """The measured |delta| band label for ``abs_delta``.

    Raises :class:`LegOutsideTradeableUniverseError` outside ``[0, 0.70]``:
    above the ceiling the corpus was never measured, and a bucket for those
    rows would be an invention.
    """
    return DELTA_BUCKET_LABELS[_checked_delta_index(abs_delta)]


def dte_bucket(dte: int) -> str:
    """The measured dte band label for ``dte``; inclusive at both ends."""
    return DTE_BAND_LABELS[_checked_dte_index(dte)]


@dataclass(frozen=True)
class LegRef:
    """The minimal stand-in that lets a standalone refusal name its inputs.

    It answers ``describe`` and ``to_json`` and nothing else, so a caller
    inspecting a refusal learns which half of the key was out of range
    without being handed a constructible observation it might mistake for a
    real one.
    """

    symbol: str
    abs_delta: Decimal | None
    dte: int

    def describe(self) -> str:
        return f"symbol={self.symbol!r} |delta|={self.abs_delta} dte={self.dte}"

    def to_json(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "abs_delta": str(self.abs_delta),
            "dte": self.dte,
            "source_session": "",
            "source_timestamp_et": "",
            "is_eod_snapshot": None,
        }


# --------------------------------------------------------------- the leg


@dataclass(frozen=True)
class Leg:
    """An option leg's supplied delta classification and its declared source.

    EVERY field is required and ``is_eod_snapshot`` has NO DEFAULT: a cost
    cannot be constructed without declaring whether its delta source was EOD.
    An intraday delta source does not turn the separate cost calibration into
    an intraday quote or an observed fill.

    ``__post_init__`` validates identifiers and the explicit source flag. It does not
    range-check ``abs_delta`` or ``dte`` -- an out-of-universe leg must be
    CONSTRUCTIBLE (a miner row is data, not a programming error) so that its
    refusal is testable. Refusal belongs to :meth:`SpreadCostModel.price`.
    """

    symbol: str
    abs_delta: Decimal | None
    dte: int
    source_session: str
    source_timestamp_et: str
    is_eod_snapshot: bool

    def __post_init__(self) -> None:
        if type(self.is_eod_snapshot) is not bool:
            raise ProvenanceError("is_eod_snapshot must be explicitly boolean")
        for name in ("source_session", "source_timestamp_et"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ProvenanceError(f"{name} must be a non-blank string; got {value!r}")
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ProvenanceError(f"symbol must be a non-blank string; got {self.symbol!r}")

    def describe(self) -> str:
        delta = "None" if self.abs_delta is None else str(self.abs_delta)
        return f"symbol={self.symbol!r} |delta|={delta} dte={self.dte}"

    def to_json(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "abs_delta": None if self.abs_delta is None else str(self.abs_delta),
            "dte": self.dte,
            "source_session": self.source_session,
            "source_timestamp_et": self.source_timestamp_et,
            "is_eod_snapshot": self.is_eod_snapshot,
        }


# ------------------------------------------------------------- the quotes


@dataclass(frozen=True)
class LegQuote:
    """One leg's modeled cost, with its derived cell and delta input."""

    leg_index: int
    abs_delta: Decimal
    dte: int
    delta_bucket: int
    dte_band: int
    measured_full_spread: Decimal
    dte_multiplier: Decimal
    half_spread_per_share: Decimal
    provenance: Leg
    commission_per_leg: Decimal
    multiplier: int

    def fill_cost(self) -> Decimal:
        """One fill: half the quote, on ``multiplier`` shares, plus commission."""
        return self.half_spread_per_share * self.multiplier + self.commission_per_leg

    def round_trip(self) -> Decimal:
        return self.fill_cost() * FILLS_PER_LEG

    def to_json(self) -> dict[str, Any]:
        return {
            "leg_index": self.leg_index,
            "abs_delta": str(self.abs_delta),
            "dte": self.dte,
            "delta_bucket": self.delta_bucket,
            "delta_bucket_label": DELTA_BUCKET_LABELS[self.delta_bucket],
            "dte_band": self.dte_band,
            "dte_band_label": DTE_BAND_LABELS[self.dte_band],
            "measured_full_spread": str(self.measured_full_spread),
            "dte_multiplier": str(self.dte_multiplier),
            "half_spread_per_share": str(self.half_spread_per_share),
            "commission_per_leg": str(self.commission_per_leg),
            "multiplier": self.multiplier,
            "provenance": self.provenance.to_json(),
        }


@dataclass(frozen=True)
class SpreadQuote:
    """A whole package priced leg by leg. Cost is per-moneyness, never a scalar."""

    legs: tuple[LegQuote, ...]
    commission_per_leg: Decimal
    multiplier: int

    @property
    def total_round_trip(self) -> Decimal:
        """Every leg, both fills. The quantized scale is preserved so that
        ``str()`` of the total is the same string on every call site."""
        return sum((leg.round_trip() for leg in self.legs), Decimal("0")).quantize(
            HALF_SPREAD_QUANTUM
        )

    @property
    def spread_total(self) -> Decimal:
        return sum(
            (leg.half_spread_per_share * leg.multiplier * FILLS_PER_LEG for leg in self.legs),
            Decimal("0"),
        ).quantize(HALF_SPREAD_QUANTUM)

    @property
    def commission_total(self) -> Decimal:
        return (Decimal(len(self.legs)) * FILLS_PER_LEG * self.commission_per_leg).quantize(
            HALF_SPREAD_QUANTUM
        )

    @property
    def provenance(self) -> tuple[Leg, ...]:
        return tuple(leg.provenance for leg in self.legs)

    @property
    def is_eod_snapshot(self) -> bool:
        """Whether every DELTA input is EOD; says nothing about execution."""
        return all(leg.provenance.is_eod_snapshot for leg in self.legs)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": COST_SCHEMA,
            "cost_model": "derived-spread/1",
            "cost_provenance": CostProvenance.measured_corpus().as_dict(),
            "commission_per_leg": str(self.commission_per_leg),
            "multiplier": self.multiplier,
            "fills_per_leg": FILLS_PER_LEG,
            "total_round_trip": str(self.total_round_trip),
            "spread_total": str(self.spread_total),
            "commission_total": str(self.commission_total),
            "delta_inputs_all_eod": self.is_eod_snapshot,
            "legs": [leg.to_json() for leg in self.legs],
        }

    def canonical_json(self) -> str:
        """Stable bytes for hashing and diffing.

        ``leg_index`` is IN the serialisation for a reason: it is what keeps
        ``price([0.05, 0.60])`` and ``price([0.60, 0.05])`` from colliding
        at an equal total.

        ``json`` is imported HERE, not at module scope, so that pricing pulls
        in nothing it was not handed.
        """
        import json

        return json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))


# ------------------------------------------------------------- the model


@dataclass(frozen=True)
class SpreadCostModel:
    """The derived sensitivity model. Coexists with ``outcomes.CostModel`` BY NAME:
    the flat model is the legacy baseline the 25-arm digest is calibrated on
    and is frozen; this one is selected explicitly, never by a default that
    changes silently."""

    commission_per_leg: Decimal = COMMISSION_PER_LEG
    multiplier: int = CONTRACT_MULTIPLIER

    def __post_init__(self) -> None:
        if (
            not isinstance(self.commission_per_leg, Decimal)
            or not self.commission_per_leg.is_finite()
            or self.commission_per_leg < 0
        ):
            raise CostModelError("commission_per_leg must be non-negative")
        if type(self.multiplier) is not int or self.multiplier < 1:
            raise CostModelError("multiplier must be positive")

    @classmethod
    def measured(cls) -> SpreadCostModel:
        """Compatibility constructor; returns the DERIVED approximation."""
        return cls()

    def half_spread_per_share(self, abs_delta: Decimal, dte: int) -> Decimal:
        """The DERIVED half-spread per share per fill for one leg.

        ``MEASURED_MEDIAN_FULL_SPREAD[delta bucket] / 2 * DTE_BAND_MULTIPLIER[dte band]``,
        quantized. It takes BOTH inputs on purpose: a zero-argument form would
        be the flat constant wearing a new name, which is the one thing this
        model must not become. It RAISES outside the measured universe rather
        than answering.
        """
        bucket = _checked_delta_index(abs_delta)
        band = _checked_dte_index(dte)
        return (MEASURED_MEDIAN_FULL_SPREAD[bucket] / 2 * DTE_BAND_MULTIPLIER[band]).quantize(
            HALF_SPREAD_QUANTUM
        )

    def price(self, legs: Sequence[Leg], *, delta_unavailable: Sequence[Leg] = ()) -> SpreadQuote:
        """Price a whole package, or refuse it. One unpriceable leg refuses
        every leg: pricing the cheap one and charging for it alone is exactly
        the flattering error this gate exists to prevent.

        ``delta_unavailable`` is how a delta SOURCE says "I declined". A
        derivation component that has a strike and an expiry but cannot
        produce an ``|delta|`` -- outside its domain, or its inversion did
        not converge -- passes the leg here and the package is refused with
        reason ``delta_unavailable`` rather than priced from the boundary
        bucket. Without this, the largest gap in the programme (the board
        carries no delta; see the module docstring) would have no way to be
        counted from inside the pricer, and a clamp would be indistinguishable
        from a measurement.

        It is checked BEFORE the per-leg loop, matching the detection order:
        ``no_legs`` first, then this, then each leg's own refusals.
        """
        if not legs:
            raise UnpricedCostError("no_legs", LegRef("", Decimal("0"), MIN_DTE))
        for declined in delta_unavailable:
            raise UnpricedCostError("delta_unavailable", declined)
        quoted: list[LegQuote] = []
        for index, leg in enumerate(legs):
            if leg.abs_delta is None:
                raise UnpricedCostError("no_delta", leg)
            if leg.symbol not in TRADEABLE_SYMBOLS:
                raise UnpricedCostError("unknown_symbol", leg)
            if not isinstance(leg.abs_delta, Decimal) or not leg.abs_delta.is_finite():
                raise UnpricedCostError("invalid_delta", leg)
            if leg.abs_delta < 0 or leg.abs_delta > MAX_ABS_DELTA:
                raise LegOutsideTradeableUniverseError("delta_out_of_universe", leg)
            bucket = _delta_index(leg.abs_delta)
            if type(leg.dte) is not int or leg.dte < MIN_DTE or leg.dte > MAX_DTE:
                raise LegOutsideTradeableUniverseError("dte_out_of_universe", leg)
            band = _dte_index(leg.dte)
            quoted.append(
                LegQuote(
                    leg_index=index,
                    abs_delta=leg.abs_delta,
                    dte=leg.dte,
                    delta_bucket=bucket,
                    dte_band=band,
                    measured_full_spread=MEASURED_MEDIAN_FULL_SPREAD[bucket],
                    dte_multiplier=DTE_BAND_MULTIPLIER[band],
                    half_spread_per_share=(
                        MEASURED_MEDIAN_FULL_SPREAD[bucket] / 2 * DTE_BAND_MULTIPLIER[band]
                    ).quantize(HALF_SPREAD_QUANTUM),
                    provenance=leg,
                    commission_per_leg=self.commission_per_leg,
                    multiplier=self.multiplier,
                )
            )
        return SpreadQuote(
            legs=tuple(quoted),
            commission_per_leg=self.commission_per_leg,
            multiplier=self.multiplier,
        )

    def round_trip(self, legs: Sequence[Leg], *, delta_unavailable: Sequence[Leg] = ()) -> Decimal:
        """The whole package's round trip in dollars.

        ``legs`` is a REQUIRED POSITIONAL argument: ``outcomes._evaluate``
        prices per CANDIDATE inside a loop over the board, so the legs are a
        property of the candidate, not of the model. A model with the legs
        bound at construction would need one instance per candidate.
        """
        return self.price(legs, delta_unavailable=delta_unavailable).total_round_trip


# ------------------------------------------------------------ provenance


@dataclass(frozen=True)
class CostProvenance:
    """Corpus-level record of where the measured marginals came from.

    ``describes_fill_clock`` is always ``False``: these are EOD snapshots and
    the run trades intraday. That is a disclosure, not a claim, and it must
    travel with every artifact built on these numbers.
    """

    source: str
    snapshot_window_et: str
    universe_filter: str
    n_rows: int
    decision_clocks_et: tuple[str, ...]

    @classmethod
    def measured_corpus(cls) -> CostProvenance:
        """Reported peer calibration metadata, not an independent corpus receipt."""
        return cls(
            source="cboe-delayed-eod-chains",
            snapshot_window_et="17:45-06:30",
            universe_filter=(
                "symbol in IWM/QQQ/SPY, 7 <= dte <= 60, volume > 0, oi > 0, |delta| <= 0.70"
            ),
            n_rows=18_783,
            decision_clocks_et=("10:00", "10:15", "15:15"),
        )

    def as_dict(self) -> dict[str, Any]:
        gap = (
            f"the {self.source} corpus was captured {self.snapshot_window_et} ET, "
            f"outside every decision clock this desk trades "
            f"({', '.join(self.decision_clocks_et)} ET); no instant of the capture "
            "window describes a fill at a decision clock, and this gap is not "
            "falsifiable from the corpus"
        )
        return {
            "source": self.source,
            "surface_kind": "derived_from_reported_marginals",
            "joint_cells_measured": False,
            "corpus_revalidated": False,
            "reported_source_commit": "dec366635025404af77ad879d605f70c90269748",
            "calibration_period": "2026-09 peer EOD corpus",
            "exact_execution_economics": False,
            "execution_authorized": False,
            "snapshot_window_et": self.snapshot_window_et,
            "universe_filter": self.universe_filter,
            "n_rows": self.n_rows,
            "decision_clocks_et": list(self.decision_clocks_et),
            "describes_fill_clock": False,
            "gap": gap,
        }


# --------------------------------------------------------------- the ledger


class NoPriceLedger:
    """Selected-decision refusals; idempotent per arm/board/candidate/horizon.

    Menu probes do not record here. Call ``record_outcome`` only after the
    selected candidate is known, including after an outcome cache hit.
    "total" counts distinct facts; "snapshots" separately names board coverage.
    """

    def __init__(self) -> None:
        self._by_arm: dict[str, dict[tuple[str, str, str], dict[str, Any]]] = {}

    def _put(
        self, arm: str, snapshot: str, candidate_id: str, exit_mode: str, fact: dict[str, Any]
    ) -> None:
        import json

        if not arm or not snapshot:
            raise CostModelError("refusal requires explicit arm and snapshot")
        identity = (snapshot, candidate_id, exit_mode)
        frozen = json.loads(json.dumps(fact, sort_keys=True, allow_nan=False))
        facts = self._by_arm.setdefault(arm, {})
        existing = facts.get(identity)
        if existing is not None and existing != frozen:
            raise CostModelError("NO_PRICE identity collision")
        facts[identity] = frozen

    def record(
        self,
        *,
        arm: str,
        snapshot: str,
        key: Leg | LegRef,
        reason: str,
        candidate_id: str = "",
        exit_mode: str = "",
    ) -> None:
        if reason not in REASON_TOKENS:
            raise CostModelError(f"unknown refusal reason {reason!r}")
        self._put(arm, snapshot, candidate_id, exit_mode, {"reason": reason, "key": key.to_json()})

    def record_outcome(
        self,
        *,
        arm: str,
        snapshot: str,
        candidate_id: str,
        exit_mode: str,
        outcome: Mapping[str, Any],
    ) -> None:
        if outcome.get("status") != "no_price":
            return
        reason = outcome.get("pricing_reason")
        if (
            not candidate_id
            or not exit_mode
            or reason not in REASON_TOKENS
            or outcome.get("pricing_status") != "NO_PRICE"
            or outcome.get("exit_reason") != reason
            or outcome.get("gross") is not None
            or outcome.get("net") is not None
        ):
            raise CostModelError("invalid NO_PRICE outcome")
        self._put(
            arm,
            snapshot,
            candidate_id,
            exit_mode,
            {
                "reason": reason,
                "key": outcome.get("pricing_key"),
                "cost_model": outcome.get("cost_model"),
                "cost_provenance": outcome.get("cost_provenance"),
            },
        )

    def for_arm(self, arm: str) -> Mapping[str, Any]:
        import copy

        facts = self._by_arm.get(arm, {})
        entries = [
            {
                "snapshot": identity[0],
                "candidate_id": identity[1],
                "exit_mode": identity[2],
                **copy.deepcopy(fact),
            }
            for identity, fact in sorted(facts.items())
        ]
        return {
            "total": len(entries),
            "snapshots": sorted({identity[0] for identity in facts}),
            "entries": entries,
        }

    def as_dict(self) -> Mapping[str, Any]:
        by_reason: dict[str, int] = {}
        for facts in self._by_arm.values():
            for fact in facts.values():
                reason = fact["reason"]
                by_reason[reason] = by_reason.get(reason, 0) + 1
        return {
            "schema": "desk-no-price-ledger/1",
            "total": sum(len(facts) for facts in self._by_arm.values()),
            "by_arm": {arm: len(facts) for arm, facts in sorted(self._by_arm.items())},
            "by_reason": dict(sorted(by_reason.items())),
            "arms": {arm: self.for_arm(arm) for arm in sorted(self._by_arm)},
        }

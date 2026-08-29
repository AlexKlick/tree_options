"""Option candidate filters (§9.2, audit §5.2): tri-state, provenance-aware.

Every rule emits PASS / FAIL / NOT_EVALUABLE / NOT_APPLICABLE; the decision
records ALL rule outcomes (an audit, not a first-failure gate). A candidate
is accepted only if no rule FAILs or is NOT_EVALUABLE — missing or
future-available inputs never silently pass. An optional rule whose input is
legitimately not yet available (same-day volume early in the session) is
recorded NOT_APPLICABLE and does not block acceptance.

Time-varying inputs (delta, OI, volume, quote, earnings, liquidity) each
carry an `available_at` instant; an input available AFTER the decision
instant is rejected as future data (INV-03 at the candidate layer).

DTE convention: CALENDAR days between decision session and expiration
(market convention for "days to expiration").

Nonclaim: synthetic M0 plumbing — historical option-chain selection is NOT
implemented here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from tree_options.protocol.schema import ResearchProtocol, protocol_version_at_least
from tree_options.schemas.options import OptionContract
from tree_options.time.calendar import NotASessionError

PASS = "PASS"
FAIL = "FAIL"
NOT_EVALUABLE = "NOT_EVALUABLE"
NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class AsOf:
    """A value plus the instant it became available (provenance + payload).

    `provenance` names WHERE the value came from (G3, protocol 0.2.0):
    "vendor" = observed by the data provider; "model-derived-from-vwap" =
    computed by the repo's own pricer from a bar VWAP. The filter checks
    the stamp against the protocol's accepted classes — an unaccepted
    provenance is NOT_EVALUABLE, never a silent pass."""

    value: object
    available_at: datetime
    provenance: str = "vendor"

    def available_by(self, decision_at: datetime) -> bool:
        return self.available_at <= decision_at


@dataclass(frozen=True)
class CandidateSnapshot:
    """Point-in-time candidate inputs.

    `contract` is the CONTRACT OBJECT: standardness is derived from it
    (flag + deliverable + corporate-action provenance), never from a
    caller-supplied boolean. `decision_at` must equal the calendar's close
    of `decision_session` (early closes included); the filter checks this
    itself rather than trusting the label.
    """

    contract: OptionContract
    underlying_security_id: str
    decision_session: date
    decision_at: datetime
    expiration: date
    abs_delta: AsOf | None
    open_interest: AsOf | None
    same_day_volume: AsOf | None
    same_day_volume_applicable: bool  # False: volume legitimately not yet published
    bid: AsOf | None
    ask: AsOf | None
    underlying_20d_median_dollar_volume: AsOf | None
    spans_earnings: AsOf | None

    @property
    def contract_id(self) -> str:
        return self.contract.contract_id


@dataclass(frozen=True)
class RuleResult:
    rule: str
    status: str
    detail: str


@dataclass(frozen=True)
class CandidateDecision:
    contract_id: str
    accepted: bool
    results: tuple[RuleResult, ...]

    def failed(self) -> tuple[RuleResult, ...]:
        return tuple(r for r in self.results if r.status in {FAIL, NOT_EVALUABLE})


def _tz_aware(ts: datetime) -> bool:
    return getattr(ts, "tzinfo", None) is not None


def _earnings_disclosed_absence_declared(protocol: ResearchProtocol) -> bool:
    """THE 0.2.2 VERSION GATE (owner ruling m4-022-ruling-20260828,
    declaration 2): the earnings disclosed-absence pass activates ONLY on a
    >=0.2.2 protocol that DECLARES `earnings_evaluation="disclosed_absence"`.
    Either half alone keeps the honest dark lane — a 0.2.1 protocol carrying
    the declaration refuses exactly as today (the version bump is what turns
    the lane on), and a 0.2.2 protocol still declaring "evaluated" refuses
    because nothing was declared absent. Both protocol factories compute the
    flag through this one function so the gate cannot drift between regimes."""
    return protocol.option_candidate_defaults.earnings_evaluation == "disclosed_absence" and (
        protocol_version_at_least(protocol.meta.protocol_version, 0, 2, 2)
    )


class CandidateFilter:
    def __init__(
        self,
        calendar,
        *,
        dte_min: int,
        dte_max: int,
        abs_delta_min: Decimal,
        abs_delta_max: Decimal,
        standard_deliverable_only: bool,
        min_open_interest: int,
        min_same_day_volume: int,
        volume_only_if_already_available: bool,
        max_spread_fraction_of_midpoint: Decimal,
        min_underlying_20d_median_dollar_volume: Decimal,
        exclude_earnings_spanning_hold: bool,
        liquidity_regime: str = "two_sided",
        flow_min_session_volume: int | None = None,
        underlying_liquidity_term: str = "evaluated",
        accepted_delta_provenance: tuple[str, ...] = ("vendor",),
        earnings_disclosed_absence: bool = False,
    ) -> None:
        self.calendar = calendar
        self.dte_min = dte_min
        self.dte_max = dte_max
        self.abs_delta_min = abs_delta_min
        self.abs_delta_max = abs_delta_max
        self.standard_deliverable_only = standard_deliverable_only
        self.min_open_interest = min_open_interest
        self.min_same_day_volume = min_same_day_volume
        self.volume_only_if_already_available = volume_only_if_already_available
        self.max_spread_fraction_of_midpoint = max_spread_fraction_of_midpoint
        self.min_underlying_20d_median_dollar_volume = min_underlying_20d_median_dollar_volume
        self.exclude_earnings_spanning_hold = exclude_earnings_spanning_hold
        if liquidity_regime not in {"two_sided", "volume_flow"}:
            raise ValueError(f"unknown liquidity_regime {liquidity_regime!r}")
        if liquidity_regime == "volume_flow":
            # The era-pending threshold may never default to 0: an unset
            # flow threshold would accept every candidate (G3 Ask D), and
            # neither may a non-positive value sneak in past the None check
            # (review P0). The type gate is explicit because bool IS an int
            # (True == 1 passes < 1) and float NaN compares false against
            # everything — both would activate a fully-accepting filter
            # (review round 2).
            if (
                not isinstance(flow_min_session_volume, int)
                or isinstance(flow_min_session_volume, bool)
                or flow_min_session_volume < 1
            ):
                raise ValueError(
                    "volume_flow regime requires flow_min_session_volume to be"
                    " an int >= 1 — the protocol's threshold is PENDING-era;"
                    " set it from the coverage-era census before building this"
                    " filter"
                )
        self.liquidity_regime = liquidity_regime
        self.flow_min_session_volume = flow_min_session_volume
        # 0.2.2 pre-draft machinery (theory-panel §2 P0-1(a)): the declared
        # disposition of the underlying-liquidity term. "evaluated" is the
        # standing default both regimes carry today; the
        # "dropped_no_equity_aggregates" Literal is the ruled fallback and
        # belongs to the volume_flow regime only — an unknown token refuses
        # here exactly as the regime name does.
        if underlying_liquidity_term not in {"evaluated", "dropped_no_equity_aggregates"}:
            raise ValueError(f"unknown underlying_liquidity_term {underlying_liquidity_term!r}")
        self.underlying_liquidity_term = underlying_liquidity_term
        self.accepted_delta_provenance = tuple(accepted_delta_provenance)
        # 0.2.2 pre-draft machinery (owner ruling m4-022-ruling-20260828,
        # declaration 2): the earnings disclosed-absence pass. Computed ONLY
        # by the protocol factories below — a direct constructor defaults to
        # False, so every existing construction keeps today's behavior
        # byte-identically. The gate is the CONJUNCTION: a >=0.2.2 protocol
        # AND the declared "disclosed_absence" disposition. Either half
        # alone keeps the honest dark lane (NOT_EVALUABLE).
        if not isinstance(earnings_disclosed_absence, bool):
            raise ValueError(
                f"earnings_disclosed_absence must be a bool, got {earnings_disclosed_absence!r}"
            )
        self.earnings_disclosed_absence = earnings_disclosed_absence

    @classmethod
    def from_protocol(cls, calendar, protocol: ResearchProtocol) -> CandidateFilter:
        d = protocol.option_candidate_defaults
        return cls(
            calendar,
            dte_min=d.dte_min,
            dte_max=d.dte_max,
            abs_delta_min=d.abs_delta_min,
            abs_delta_max=d.abs_delta_max,
            standard_deliverable_only=d.standard_deliverable_only,
            min_open_interest=d.min_open_interest,
            min_same_day_volume=d.min_same_day_volume,
            volume_only_if_already_available=d.volume_only_if_already_available,
            max_spread_fraction_of_midpoint=d.max_spread_fraction_of_midpoint,
            min_underlying_20d_median_dollar_volume=d.min_underlying_20d_median_dollar_volume,
            exclude_earnings_spanning_hold=d.exclude_earnings_spanning_hold,
            earnings_disclosed_absence=_earnings_disclosed_absence_declared(protocol),
        )

    @classmethod
    def from_protocol_volume_flow(
        cls,
        calendar,
        protocol: ResearchProtocol,
        *,
        flow_min_session_volume: int | None = None,
    ) -> CandidateFilter:
        """Build the G3 volume-flow filter from the protocol's ratified
        regime block. Fails closed (ValueError) while the protocol's
        flow_min_session_volume is still PENDING-era (None).

        `flow_min_session_volume` is the trial runner's EXPLICIT hashed
        config key (G2, mirroring max_quote_age_seconds): when supplied it
        replaces the protocol value — every deviation rides the caller's
        config hash, and an illegal override (non-int, bool, < 1) refuses
        here exactly as `__init__`'s threshold gate does. A supplied
        override is also the PENDING-era escape hatch: it is the caller
        owning the threshold, not the protocol defaulting one."""
        d = protocol.option_candidate_defaults
        lf = d.liquidity_volume_flow
        if lf is None:
            raise ValueError(
                "protocol carries no liquidity_volume_flow block: the "
                "volume-flow regime is not ratified"
            )
        threshold = (
            lf.flow_min_session_volume
            if flow_min_session_volume is None
            else flow_min_session_volume
        )
        base = cls.from_protocol(calendar, protocol)
        return cls(
            calendar,
            dte_min=base.dte_min,
            dte_max=base.dte_max,
            abs_delta_min=base.abs_delta_min,
            abs_delta_max=base.abs_delta_max,
            standard_deliverable_only=base.standard_deliverable_only,
            min_open_interest=base.min_open_interest,
            min_same_day_volume=base.min_same_day_volume,
            volume_only_if_already_available=base.volume_only_if_already_available,
            max_spread_fraction_of_midpoint=base.max_spread_fraction_of_midpoint,
            min_underlying_20d_median_dollar_volume=(base.min_underlying_20d_median_dollar_volume),
            exclude_earnings_spanning_hold=base.exclude_earnings_spanning_hold,
            liquidity_regime="volume_flow",
            flow_min_session_volume=threshold,
            underlying_liquidity_term=lf.underlying_liquidity_term,
            accepted_delta_provenance=lf.abs_delta_provenance_accepted,
            earnings_disclosed_absence=_earnings_disclosed_absence_declared(protocol),
        )

    def evaluate(self, snap: CandidateSnapshot) -> CandidateDecision:
        results: list[RuleResult] = []

        # Decision coherence FIRST: a post-close or naive decision instant
        # invalidates the whole evaluation — nothing downstream may run on a
        # mislabeled decision time. An unknown decision_session is the same
        # class of incoherence (the tri-state promise: NOT_EVALUABLE, not a
        # crash — review round 2, P2).
        coherent = True
        if not _tz_aware(snap.decision_at):
            coherent = False
            results.append(RuleResult("decision_coherence", NOT_EVALUABLE, "naive decision_at"))
        else:
            try:
                expected_close = self.calendar.session_close(snap.decision_session)
            except NotASessionError:
                coherent = False
                results.append(
                    RuleResult(
                        "decision_coherence",
                        NOT_EVALUABLE,
                        f"decision_session {snap.decision_session} not in calendar",
                    )
                )
            else:
                if snap.decision_at != expected_close:
                    coherent = False
                    results.append(
                        RuleResult(
                            "decision_coherence",
                            NOT_EVALUABLE,
                            f"decision_at {snap.decision_at} != session close {expected_close}",
                        )
                    )

        # Contract coherence: the snapshot must describe the contract it
        # carries — duplicated expiration/underlier fields that disagree with
        # the CONTRACT OBJECT make every rule unevaluable (the contract is
        # authoritative; the duplicates exist only for audit convenience).
        if snap.expiration != snap.contract.expiration or snap.underlying_security_id != (
            snap.contract.underlying_security_id
        ):
            coherent = False
            results.append(
                RuleResult(
                    "contract_coherence",
                    NOT_EVALUABLE,
                    f"snapshot expiration/underlier disagree with contract "
                    f"{snap.contract.contract_id}",
                )
            )
        if not coherent:
            # Nothing is evaluable at a mislabeled decision time: fail closed
            # for every rule rather than comparing against a bad instant.
            rule_names = [
                "dte",
                "delta",
                "deliverable",
                "open_interest",
                "session_volume_flow"
                if self.liquidity_regime == "volume_flow"
                else "same_day_volume",
                "spread",
                "underlying_liquidity",
                "earnings_span",
            ]
            results.extend(
                RuleResult(r, NOT_EVALUABLE, "decision instant incoherent") for r in rule_names
            )
            return CandidateDecision(
                contract_id=snap.contract_id, accepted=False, results=tuple(results)
            )

        # DTE: calendar days (explicit convention), derived from the
        # CONTRACT's expiration — never the caller-duplicated snapshot field.
        dte = (snap.contract.expiration - snap.decision_session).days
        if self.dte_min <= dte <= self.dte_max:
            results.append(RuleResult("dte", PASS, f"dte {dte}"))
        else:
            results.append(
                RuleResult("dte", FAIL, f"dte {dte} not in [{self.dte_min}, {self.dte_max}]")
            )

        # Delta. The provenance stamp is checked FIRST (G3 0.2.0): a value
        # whose provenance the protocol does not accept is NOT_EVALUABLE —
        # a model-derived delta may never pass through a vendor-only regime
        # by omission of the check.
        if snap.abs_delta is None:
            results.append(RuleResult("delta", NOT_EVALUABLE, "abs_delta missing"))
        elif not _tz_aware(snap.abs_delta.available_at):
            results.append(RuleResult("delta", NOT_EVALUABLE, "naive timestamp"))
        elif snap.abs_delta.provenance not in self.accepted_delta_provenance:
            results.append(
                RuleResult(
                    "delta",
                    NOT_EVALUABLE,
                    f"provenance {snap.abs_delta.provenance!r} not accepted "
                    f"(accepted: {list(self.accepted_delta_provenance)})",
                )
            )
        elif not snap.abs_delta.available_by(snap.decision_at):
            results.append(RuleResult("delta", NOT_EVALUABLE, "future-available delta"))
        elif not (self.abs_delta_min <= _v(snap.abs_delta) <= self.abs_delta_max):
            results.append(RuleResult("delta", FAIL, f"{_v(snap.abs_delta)} out of band"))
        else:
            results.append(
                RuleResult(
                    "delta",
                    PASS,
                    f"in band (provenance {snap.abs_delta.provenance})",
                )
            )

        # Deliverable standardness, derived from the CONTRACT OBJECT (never a
        # caller boolean): standard == flag set AND no corporate-action
        # provenance AND schema-validated 100-share deliverable.
        if not self.standard_deliverable_only:
            results.append(RuleResult("deliverable", NOT_APPLICABLE, "filter disabled"))
        elif (
            snap.contract.standard_contract_flag
            and snap.contract.corporate_action_id is None
            and snap.contract.deliverable.shares_per_contract == Decimal("100")
        ):
            results.append(RuleResult("deliverable", PASS, "standard 100 shares"))
        else:
            results.append(
                RuleResult(
                    "deliverable",
                    FAIL,
                    f"nonstandard/adjusted deliverable rejected ({snap.contract.contract_id})",
                )
            )

        # Open interest. In the volume_flow regime the term is DROPPED WITH
        # DISCLOSURE (G3 Ask D): tiers without two-sided markets carry no
        # OI, and the audit says so instead of fabricating a threshold pass.
        # A snapshot that SUPPLIES OI contradicts the regime's premise and
        # is incoherent — the disclosure may not paper over real inputs
        # (review P1).
        if self.liquidity_regime == "volume_flow" and snap.open_interest is not None:
            results.append(
                RuleResult(
                    "open_interest",
                    NOT_EVALUABLE,
                    "regime incoherent: volume_flow drops OI as absent, but"
                    " the snapshot supplies open_interest (value withheld:"
                    " its availability was never checked, and a future value"
                    " must not leak into the audit)",
                )
            )
        elif self.liquidity_regime == "volume_flow":
            results.append(
                RuleResult(
                    "open_interest",
                    NOT_APPLICABLE,
                    "dropped: no open interest on a no-two-sided-market tier",
                )
            )
        elif snap.open_interest is None:
            results.append(RuleResult("open_interest", NOT_EVALUABLE, "missing"))
        elif not _tz_aware(snap.open_interest.available_at):
            results.append(RuleResult("open_interest", NOT_EVALUABLE, "naive timestamp"))
        elif not snap.open_interest.available_by(snap.decision_at):
            results.append(RuleResult("open_interest", NOT_EVALUABLE, "future-available"))
        elif _v(snap.open_interest) < self.min_open_interest:
            results.append(RuleResult("open_interest", FAIL, "below min"))
        else:
            results.append(RuleResult("open_interest", PASS, "above min"))

        # Same-day volume: optional rule — unavailable-but-applicable is
        # recorded NOT_APPLICABLE (never silently skipped, never fabricated).
        # A SUPPLIED volume is always evaluated — the applicability flag only
        # excuses a missing one (F4: the flag must not hide a future input).
        # In the volume_flow regime this IS the liquidity term (G3 Ask D):
        # the session's traded contracts against flow_min_session_volume,
        # under its own rule name so the audit cannot misread it as the
        # two-sided same-day-volume screen.
        volume_rule = (
            "session_volume_flow" if self.liquidity_regime == "volume_flow" else "same_day_volume"
        )
        volume_min = (
            self.flow_min_session_volume
            if self.liquidity_regime == "volume_flow"
            else self.min_same_day_volume
        )
        if snap.same_day_volume is not None:
            if not _tz_aware(snap.same_day_volume.available_at):
                results.append(RuleResult(volume_rule, NOT_EVALUABLE, "naive timestamp"))
            elif not snap.same_day_volume.available_by(snap.decision_at):
                results.append(RuleResult(volume_rule, NOT_EVALUABLE, "future-available volume"))
            elif _v(snap.same_day_volume) < volume_min:
                results.append(
                    RuleResult(volume_rule, FAIL, f"below flow min {volume_min}")
                    if self.liquidity_regime == "volume_flow"
                    else RuleResult(volume_rule, FAIL, "below min")
                )
            else:
                results.append(
                    RuleResult(volume_rule, PASS, f"at/above flow min {volume_min}")
                    if self.liquidity_regime == "volume_flow"
                    else RuleResult(volume_rule, PASS, "above min")
                )
        elif self.liquidity_regime == "volume_flow":
            # In the flow regime the session volume IS the liquidity term:
            # missing is NOT_EVALUABLE — the candidate cannot be judged
            # liquid (review P0: NOT_APPLICABLE here accepted candidates
            # with no volume evidence at all). The two-sided regime keeps
            # the historical optional-volume semantics below.
            results.append(
                RuleResult(
                    volume_rule,
                    NOT_EVALUABLE,
                    "missing: session volume is the liquidity term in the volume_flow regime",
                )
            )
        elif self.volume_only_if_already_available and not snap.same_day_volume_applicable:
            results.append(RuleResult(volume_rule, NOT_APPLICABLE, "not yet published at decision"))
        else:
            results.append(RuleResult(volume_rule, NOT_EVALUABLE, "missing"))

        # Spread fraction of midpoint. DROPPED WITH DISCLOSURE in the
        # volume_flow regime: with no bid/ask there is no spread to bound,
        # and no $0 substitute is approximated (G3 Ask D). Supplied quotes
        # contradict the premise and are incoherent (review P1).
        if self.liquidity_regime == "volume_flow" and (
            snap.bid is not None or snap.ask is not None
        ):
            results.append(
                RuleResult(
                    "spread",
                    NOT_EVALUABLE,
                    "regime incoherent: volume_flow drops the spread as"
                    " absent, but the snapshot supplies a two-sided quote",
                )
            )
        elif self.liquidity_regime == "volume_flow":
            results.append(
                RuleResult(
                    "spread",
                    NOT_APPLICABLE,
                    "dropped: no two-sided market on this tier",
                )
            )
        elif snap.bid is None or snap.ask is None:
            results.append(RuleResult("spread", NOT_EVALUABLE, "bid/ask missing"))
        elif not _tz_aware(snap.bid.available_at) or not _tz_aware(snap.ask.available_at):
            results.append(RuleResult("spread", NOT_EVALUABLE, "naive quote timestamp"))
        elif not snap.bid.available_by(snap.decision_at) or not snap.ask.available_by(
            snap.decision_at
        ):
            results.append(RuleResult("spread", NOT_EVALUABLE, "future-available quote"))
        else:
            bid_v, ask_v = _v(snap.bid), _v(snap.ask)
            if bid_v > ask_v:
                results.append(RuleResult("spread", NOT_EVALUABLE, f"crossed {bid_v}/{ask_v}"))
            else:
                mid = (bid_v + ask_v) / 2
                if mid <= 0:
                    results.append(RuleResult("spread", NOT_EVALUABLE, f"midpoint {mid}"))
                else:
                    fraction = (ask_v - bid_v) / mid
                    if fraction > self.max_spread_fraction_of_midpoint:
                        results.append(RuleResult("spread", FAIL, f"{fraction:.4f} exceeds limit"))
                    else:
                        results.append(RuleResult("spread", PASS, f"{fraction:.4f}"))

        # Underlying liquidity. In the volume_flow regime the term may be
        # DROPPED WITH DISCLOSURE (0.2.2 pre-draft machinery, theory-panel
        # §2 P0-1(a)): a lane with no equity-aggregates dollar-volume source
        # cannot evaluate the 20d median, and the audit says the term is off
        # instead of failing forever on the declared sentinel — the same
        # disclosure the OI/spread drops carry. The branch fires ONLY when
        # the protocol declares the term dropped; a snapshot that SUPPLIES a
        # dollar-volume value contradicts the regime's premise and is
        # incoherent — the disclosure may not paper over real inputs
        # (mirroring the OI/spread incoherence branches, review P1).
        if (
            self.liquidity_regime == "volume_flow"
            and self.underlying_liquidity_term == "dropped_no_equity_aggregates"
            and snap.underlying_20d_median_dollar_volume is not None
        ):
            results.append(
                RuleResult(
                    "underlying_liquidity",
                    NOT_EVALUABLE,
                    "regime incoherent: the protocol drops the underlying-liquidity"
                    " term as having no equity-aggregates source, but the snapshot"
                    " supplies a dollar-volume value (value withheld: its"
                    " availability was never checked, and a future value must not"
                    " leak into the audit)",
                )
            )
        elif (
            self.liquidity_regime == "volume_flow"
            and self.underlying_liquidity_term == "dropped_no_equity_aggregates"
        ):
            results.append(
                RuleResult(
                    "underlying_liquidity",
                    NOT_APPLICABLE,
                    "dropped: no equity-aggregates dollar volume on this tier",
                )
            )
        elif snap.underlying_20d_median_dollar_volume is None:
            results.append(RuleResult("underlying_liquidity", NOT_EVALUABLE, "missing"))
        elif not _tz_aware(snap.underlying_20d_median_dollar_volume.available_at):
            results.append(RuleResult("underlying_liquidity", NOT_EVALUABLE, "naive timestamp"))
        elif not snap.underlying_20d_median_dollar_volume.available_by(snap.decision_at):
            results.append(RuleResult("underlying_liquidity", NOT_EVALUABLE, "future-available"))
        elif (
            _v(snap.underlying_20d_median_dollar_volume)
            < self.min_underlying_20d_median_dollar_volume
        ):
            results.append(RuleResult("underlying_liquidity", FAIL, "below min"))
        else:
            results.append(RuleResult("underlying_liquidity", PASS, "above min"))

        # Earnings spanning hold. (0.2.2 declaration 2, owner ruling
        # m4-022-ruling-20260828) Under a >=0.2.2 protocol that DECLARES
        # `earnings_evaluation: "disclosed_absence"`, an ABSENT
        # spans_earnings is a PASS WITH DISCLOSURE: the protocol records
        # that earnings spans are not evaluable on this data tier (no
        # vendor events feed; the $0 purchase ruling), and this counted
        # NOT_APPLICABLE row names the absence on every passed candidate —
        # never a silent pass. The disclosure excuses an ABSENT input only:
        # a SUPPLIED spans_earnings is evaluated exactly as today. Under
        # 0.2.1 (or without the declaration) the honest dark lane stands:
        # NOT_EVALUABLE, byte-identical to the standing behavior.
        if not self.exclude_earnings_spanning_hold:
            results.append(RuleResult("earnings_span", NOT_APPLICABLE, "filter disabled"))
        elif snap.spans_earnings is None and self.earnings_disclosed_absence:
            results.append(
                RuleResult(
                    "earnings_span",
                    NOT_APPLICABLE,
                    "earnings span not evaluable: no events source on this tier"
                    " (0.2.2 disclosed-absence, owner ruling m4-022-ruling-20260828)",
                )
            )
        elif snap.spans_earnings is None:
            results.append(RuleResult("earnings_span", NOT_EVALUABLE, "missing"))
        elif not _tz_aware(snap.spans_earnings.available_at):
            results.append(RuleResult("earnings_span", NOT_EVALUABLE, "naive timestamp"))
        elif not snap.spans_earnings.available_by(snap.decision_at):
            results.append(RuleResult("earnings_span", NOT_EVALUABLE, "future-available"))
        elif _v(snap.spans_earnings):
            results.append(RuleResult("earnings_span", FAIL, "earnings span the hold"))
        else:
            results.append(RuleResult("earnings_span", PASS, "no spanning earnings"))

        accepted = not any(r.status in {FAIL, NOT_EVALUABLE} for r in results)
        return CandidateDecision(
            contract_id=snap.contract_id, accepted=accepted, results=tuple(results)
        )


def _v(as_of: AsOf):
    return as_of.value

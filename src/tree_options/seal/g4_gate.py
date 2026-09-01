"""The G4 sealed-gate verdict machinery: the six PRE-DECLARED criteria
(``docs/m4-g4-sealed-gate-plan.md`` §4, transcribed verbatim at
``data/g4/sealed-criteria.json``) evaluated ONLY from stamped payload
files, verdict recorded VERBATIM, one-shot.

The decision rule (the transcription's own binding note: NO verdict logic
and NO thresholds beyond what the plan already states):

- evaluation source: stamped payload files only — the lane censuses and
  the trial payloads the machinery stamped, plus the auxiliary STAMPED
  artifacts of prior events (the era census, the clean-clone replay
  payloads, the mutation registry);
- verdict: PASS or FAIL, recorded verbatim whatever it is;
- one-shot: the gate runs once; no re-run inside the campaign regardless
  of outcome; a FAIL triggers a remediation packet + a NEW pre-declared
  gate, never an in-place re-run;
- the manifest verifies are a PRECONDITION, not a criterion (any verify
  failure = the gate does not start — ``load_derived_surface`` /
  ``verify_real_options_manifest`` already refuse fail-closed inside the
  machinery);
- the head is declared before the run and written into the evidence.

Lane applicability is DECLARED, never silently skipped: lane 1 (the
retained one-session Cboe capture) seals the ADAPTER, so the criteria that
need an execution session or a volume-flow trial run are marked
``declared_inapplicable`` with the reason, in the record itself.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tree_options.protocol.loader import load_protocol_bytes
from tree_options.protocol.schema import ResearchProtocol
from tree_options.protocol.stamping import build_stamp, write_artifact
from tree_options.seal.verified_inputs import HeldVerifiedSealedInputs
from tree_options.time.calendar import SessionCalendar
from tree_options.trials.g4_event import (
    FLOW_MIN_SESSION_VOLUME_AMENDMENT,
    G4_SEALED_EVENT_ID,
    G4SealedRun,
)

# the pre-declared pooled floor (plan §4 criterion 4, owner decision
# 2026-08-26 re-set BEFORE any sealed run): >= 50 per lane under the STRICT
# per-lane class map. A parameter only so fixture-scale MINI gates can
# exercise the machinery — the sealed default is exactly 50.
REJECTION_FLOOR = 50

CRITERION_IDS = (
    "manifest_integrity",
    "candidate_discipline",
    "fill_discipline",
    "rejection_paths_live",
    "determinism",
    "mutation_campaign",
)

_LANE1_INAPPLICABLE = {
    "candidate_discipline": (
        "lane-1-inapplicable: the T+1 publication wall means a close(t)"
        " decision on the ONE retained session sees no published file, so no"
        " accepted-candidate cross-section exists to judge — the volume-flow"
        " threshold pin and the NOT_APPLICABLE/OI-withheld disclosure family"
        " are the lane-2 regime's clauses"
    ),
    "fill_discipline": (
        "lane-1-inapplicable: one retained session forbids an execution"
        " session (the purchase lane is closed; the $0 ruling stands) — no"
        " stamped fill can exist on lane 1"
    ),
}


@dataclass(frozen=True)
class CriterionOutcome:
    """One criterion's evaluation: PASS/FAIL, the failure lines (recorded
    verbatim in the evidence), the reported facts, and lane 1's declared
    applicability."""

    criterion_id: str
    verdict: str
    failures: tuple[str, ...] = ()
    reported: dict[str, object] = field(default_factory=dict)
    lane1_applicability: str = "applied"
    lane1_inapplicable_reason: str = ""


@dataclass(frozen=True)
class G4GateEvaluation:
    verdict: str
    criteria: tuple[CriterionOutcome, ...]
    reported: dict[str, object] = field(default_factory=dict)

    def by_id(self, criterion_id: str) -> CriterionOutcome:
        for outcome in self.criteria:
            if outcome.criterion_id == criterion_id:
                return outcome
        raise KeyError(criterion_id)


def _histogram_count(payload: Mapping[str, Any], rule: str, status: str) -> int:
    hist = payload.get("counters", {}).get("rule_histogram", {})
    return int(hist.get(rule, {}).get(status, 0))


def _criterion_manifest_integrity(
    lane1_census: Mapping[str, Any],
    lane2_census: Mapping[str, Any],
    era_target: Mapping[str, Any],
) -> CriterionOutcome:
    failures: list[str] = []
    reported: dict[str, object] = {}
    if isinstance(era_target.get("__malformed__"), str):
        # the TOCTOU backstop's era-census residue: the census passed
        # preflight but changed shape by evaluation time — an honest FAIL,
        # never a post-consumption exception
        return CriterionOutcome(
            criterion_id="manifest_integrity",
            verdict="FAIL",
            failures=(f"the era census can no longer be evaluated: {era_target['__malformed__']}",),
            reported={"era_census_malformed": True},
        )
    lane2_manifest = lane2_census.get("manifest", {})
    if not lane2_manifest.get("verified"):
        failures.append("lane 2: the typed manifest verify did not pass")
    listed = lane2_manifest.get("listed_files")
    held = lane2_manifest.get("held_payloads")
    if listed != held:
        failures.append(f"lane 2: listed files {listed} != held payloads {held}")
    expected_masters = int(era_target["expected_masters"])
    masters_files = int(lane2_manifest.get("masters_files", -1))
    if masters_files != expected_masters:
        failures.append(
            f"lane 2: verified master files {masters_files} != the era's stamped"
            f" expected_masters {expected_masters}"
        )
    verified_series = int(lane2_manifest.get("verified_series", -1))
    era_contracts = int(era_target["distinct_contracts"])
    if verified_series != era_contracts:
        failures.append(
            f"lane 2: verified series {verified_series} != the era's stamped"
            f" distinct_contracts {era_contracts}"
        )
    if not lane1_census.get("manifest", {}).get("verified"):
        failures.append("lane 1: the Cboe manifest verify did not pass")
    reported = {
        "lane2": {
            "masters_files": masters_files,
            "bars_files": lane2_manifest.get("bars_files"),
            "verified_series": verified_series,
            "era_target": dict(era_target),
        },
        "lane1": {
            "verified_series": lane1_census.get("manifest", {}).get("verified_series"),
            "contract_count": lane1_census.get("contract_count"),
        },
    }
    return CriterionOutcome(
        criterion_id="manifest_integrity",
        verdict=("PASS" if not failures else "FAIL"),
        failures=tuple(failures),
        reported=reported,
    )


def _criterion_candidate_discipline(
    protocol: ResearchProtocol,
    lane2_census: Mapping[str, Any],
    trial_payloads: Mapping[str, Mapping[str, Any]],
) -> CriterionOutcome:
    failures: list[str] = []
    reported: dict[str, object] = {"per_trial": {}}
    lf = protocol.option_candidate_defaults.liquidity_volume_flow
    protocol_threshold = lf.flow_min_session_volume if lf is not None else None
    if protocol_threshold != FLOW_MIN_SESSION_VOLUME_AMENDMENT:
        failures.append(
            f"the held protocol's flow_min_session_volume {protocol_threshold} is not"
            f" the 0.2.1 amendment value {FLOW_MIN_SESSION_VOLUME_AMENDMENT}"
        )
    accepted = list(
        lane2_census.get("declared_configuration", {}).get("accepted_delta_provenance", [])
    )
    derivation_token = lane2_census.get("declared_configuration", {}).get("derivation_provenance")
    if derivation_token not in accepted:
        failures.append(
            f"the lane's derivation provenance {derivation_token!r} is not in the"
            f" regime's accepted set {accepted}"
        )
    for arm, payload in sorted(trial_payloads.items()):
        key = f"lane2|{arm}"
        stamped_threshold = payload.get("flow_min_session_volume")
        if stamped_threshold != FLOW_MIN_SESSION_VOLUME_AMENDMENT:
            failures.append(
                f"{key}: stamped flow_min_session_volume {stamped_threshold} != the"
                f" 0.2.1 amendment value {FLOW_MIN_SESSION_VOLUME_AMENDMENT}"
            )
        if stamped_threshold != protocol_threshold:
            failures.append(
                f"{key}: stamped flow_min_session_volume {stamped_threshold} != the"
                f" held protocol's value {protocol_threshold}"
            )
        hist = payload.get("counters", {}).get("rule_histogram", {})
        oi_dropped = int(hist.get("open_interest", {}).get("NOT_APPLICABLE", 0))
        earnings_disclosed = int(hist.get("earnings_span", {}).get("NOT_APPLICABLE", 0))
        if oi_dropped <= 0:
            failures.append(
                f"{key}: no counted open_interest NOT_APPLICABLE disclosure row — the"
                " dropped-with-disclosure term must never be a silent pass"
            )
        if earnings_disclosed <= 0:
            failures.append(
                f"{key}: no counted earnings_span NOT_APPLICABLE disclosed-absence row"
                " (owner ruling m4-022-ruling-20260828) — the 0.2.2 disclosure"
                " family is present + counted, never a silent pass"
            )
        positions = payload.get("pooled", {}).get("positions", [])
        with_oi = [p for p in positions if "open_interest" in p]
        if with_oi:
            failures.append(
                f"{key}: {len(with_oi)} stamped position(s) carry an open_interest"
                " value — OI is withheld on this regime"
            )
        delta_pass = _histogram_count(payload, "delta", "PASS")
        if delta_pass < len(positions):
            failures.append(
                f"{key}: {len(positions)} stamped position(s) exceed the {delta_pass}"
                " delta PASS rows — an accepted candidate without an accepted-set"
                " delta provenance cannot exist"
            )
        reported["per_trial"][key] = {  # type: ignore[index]
            "flow_min_session_volume": stamped_threshold,
            "open_interest_not_applicable_rows": oi_dropped,
            "earnings_span_not_applicable_rows": earnings_disclosed,
            "n_positions": len(positions),
            "delta_pass_rows": delta_pass,
        }
    reported["accepted_delta_provenance"] = accepted  # type: ignore[index]
    reported["derivation_provenance"] = derivation_token  # type: ignore[index]
    return CriterionOutcome(
        criterion_id="candidate_discipline",
        verdict=("PASS" if not failures else "FAIL"),
        failures=tuple(failures),
        reported=reported,
        lane1_applicability=(
            "declared_inapplicable" if "candidate_discipline" in _LANE1_INAPPLICABLE else "applied"
        ),
        lane1_inapplicable_reason=_LANE1_INAPPLICABLE.get("candidate_discipline", ""),
    )


def _criterion_fill_discipline(
    trial_payloads: Mapping[str, Mapping[str, Any]],
    execution_calendar: SessionCalendar,
) -> CriterionOutcome:
    failures: list[str] = []
    total_fills = 0
    participation: dict[tuple[str, str], int] = {}
    bar_volumes: dict[tuple[str, str], int] = {}
    for arm, payload in sorted(trial_payloads.items()):
        key = f"lane2|{arm}"
        for fill in payload.get("fills_log", []):
            total_fills += 1
            fill_id = fill.get("fill_id")
            if not fill.get("decision_session", "~") < fill.get("execution_session"):
                failures.append(
                    f"{key}: fill {fill_id} does not execute strictly after its"
                    f" decision session ({fill.get('decision_session')} ->"
                    f" {fill.get('execution_session')})"
                )
                continue
            if fill.get("quote_received_at", "~") > fill.get("execution_at"):
                failures.append(
                    f"{key}: fill {fill_id} selected a bar not yet received at the"
                    f" execution instant ({fill.get('quote_received_at')} >"
                    f" {fill.get('execution_at')})"
                )
            bar_session = fill.get("bar_session")
            if bar_session is None:
                failures.append(
                    f"{key}: fill {fill_id} carries no stamped bar session — the"
                    " ordinal-difference clause is not evaluable"
                )
                continue
            try:
                ordinal_gap = execution_calendar.ordinal(
                    date_from_iso(fill["execution_session"])
                ) - execution_calendar.ordinal(date_from_iso(bar_session))
            except Exception:
                failures.append(
                    f"{key}: fill {fill_id} bar session {bar_session} or execution"
                    f" session {fill.get('execution_session')} is not an execution"
                    " calendar session"
                )
                continue
            if ordinal_gap != 1:
                failures.append(
                    f"{key}: fill {fill_id} executed against a bar {ordinal_gap}"
                    f" session(s) before execution ({bar_session} ->"
                    f" {fill.get('execution_session')}), not exactly 1"
                )
            contract_id = str(fill["contract_id"])
            pair = (contract_id, str(bar_session))
            participation[pair] = participation.get(pair, 0) + int(fill["quantity"])
            bar_volumes[pair] = int(fill.get("bar_volume", -1))
    over = [
        f"{contract}/{session}: cumulative {quantity} > observed {bar_volumes[(contract, session)]}"
        for (contract, session), quantity in sorted(participation.items())
        if quantity > bar_volumes[(contract, session)]
    ]
    for line in over:
        failures.append(f"lane2: participation cap exceeded — {line}")
    return CriterionOutcome(
        criterion_id="fill_discipline",
        verdict=("PASS" if not failures else "FAIL"),
        failures=tuple(failures),
        reported={
            "n_fills": total_fills,
            "participation_pairs": len(participation),
            "over_participation_pairs": len(over),
            "sample_participation": [
                {
                    "contract": c,
                    "bar_session": s,
                    "cumulative": participation[(c, s)],
                    "observed": bar_volumes[(c, s)],
                }
                for (c, s) in sorted(participation)[:5]
            ],
        },
        lane1_applicability="declared_inapplicable",
        lane1_inapplicable_reason=_LANE1_INAPPLICABLE["fill_discipline"],
    )


def _criterion_rejection_paths(
    lane1_census: Mapping[str, Any],
    lane2_census: Mapping[str, Any],
    trial_payloads: Mapping[str, Mapping[str, Any]],
    floor: int,
) -> CriterionOutcome:
    """The STRICT per-lane class map (plan §4 criterion 4, owner decision
    2026-08-26). Lane 1 counts FIRING parse refusals ONLY (zero-bid rows
    are an audit statistic, reported never counted). Lane 2 counts
    zero-volume-bar refusals + MassiveDerivationError + master-row
    refusals + session_volume_flow below-min FAIL, pooled across the
    lane's arms; ``no_bar`` NOT_EVALUABLE rows are disclosed, NEVER
    counted (~availability disclosure, never pooled into the floor)."""
    failures: list[str] = []
    lane1_classes = lane1_census.get("rejection_classes", {})
    lane1_counted = int(lane1_classes.get("firing_parse_refusals", 0))
    if lane1_counted < floor:
        failures.append(
            f"lane 1: pooled FIRING parse refusals {lane1_counted} < {floor}"
            f" (zero-bid rows {lane1_classes.get('zero_bid_rows_disclosed')}"
            " are the disclosed audit statistic, NOT counted)"
        )
    lane2_classes = lane2_census.get("rejection_classes", {})
    flow_fails = sum(
        _histogram_count(payload, "session_volume_flow", "FAIL")
        for payload in trial_payloads.values()
    )
    lane2_counted = (
        int(lane2_classes.get("zero_volume_bar_refusals", 0))
        + int(lane2_classes.get("massive_derivation_error_refusals", 0))
        + int(lane2_classes.get("master_row_refusals", 0))
        + flow_fails
    )
    if lane2_counted < floor:
        failures.append(
            f"lane 2: pooled counted rejections {lane2_counted} < {floor}"
            f" (zero-volume {lane2_classes.get('zero_volume_bar_refusals')},"
            f" MassiveDerivationError {lane2_classes.get('massive_derivation_error_refusals')},"
            f" master-row {lane2_classes.get('master_row_refusals')},"
            f" session_volume_flow FAIL {flow_fails}; no_bar"
            f" {lane2_classes.get('no_bar_not_evaluable_disclosed')} is the"
            " disclosed availability row, NOT counted)"
        )
    return CriterionOutcome(
        criterion_id="rejection_paths_live",
        verdict=("PASS" if not failures else "FAIL"),
        failures=tuple(failures),
        reported={
            "floor": floor,
            "lane1": {
                "counted": lane1_counted,
                "class_map": "FIRING parse refusals only",
                "zero_bid_rows_disclosed": lane1_classes.get("zero_bid_rows_disclosed"),
            },
            "lane2": {
                "counted": lane2_counted,
                "class_map": (
                    "zero-volume-bar refusals + MassiveDerivationError +"
                    " master-row refusals + session_volume_flow below-min FAIL"
                ),
                "zero_volume_bar_refusals": lane2_classes.get("zero_volume_bar_refusals"),
                "massive_derivation_error_refusals": lane2_classes.get(
                    "massive_derivation_error_refusals"
                ),
                "master_row_refusals": lane2_classes.get("master_row_refusals"),
                "session_volume_flow_fail": flow_fails,
                "no_bar_not_evaluable_disclosed": lane2_classes.get(
                    "no_bar_not_evaluable_disclosed"
                ),
            },
        },
    )


def _criterion_determinism(
    stamped_hashes: Mapping[str, str],
    replay_hashes: Mapping[str, str] | None,
    replay_aliased: bool = False,
) -> CriterionOutcome:
    failures: list[str] = []
    if replay_aliased:
        # round-8 P0: a "replay" that symlinks back onto the run's own
        # artifacts (or IS the artifacts dir) self-compares byte-identical
        # by construction — determinism certified by self-comparison is no
        # certification at all
        failures.append(
            "the replay tree is not independent (symlinked payloads or the"
            " artifacts dir itself) — criterion 5 cannot be certified by"
            " self-comparison"
        )
        reported: dict[str, object] = {
            "replayed": False,
            "payload_count": len(stamped_hashes),
            "replay_aliased": True,
        }
    elif replay_hashes is None:
        failures.append(
            "no clean-clone replay payload hashes were supplied — criterion 5 is"
            " never silently skipped"
        )
        reported = {"replayed": False, "payload_count": len(stamped_hashes)}
    else:
        missing = sorted(set(stamped_hashes) - set(replay_hashes))
        extra = sorted(set(replay_hashes) - set(stamped_hashes))
        diverged = sorted(
            name
            for name in set(stamped_hashes) & set(replay_hashes)
            if stamped_hashes[name] != replay_hashes[name]
        )
        for name in missing:
            failures.append(f"the replay lacks the stamped payload {name}")
        for name in extra:
            failures.append(f"the replay carries an unstamped payload {name}")
        for name in diverged:
            failures.append(
                f"payload {name} hash diverged between the sealed run and the clean-clone replay"
            )
        reported = {
            "replayed": True,
            "payload_count": len(stamped_hashes),
            "missing_in_replay": missing[:5],
            "extra_in_replay": extra[:5],
            "diverged": diverged[:5],
        }
    return CriterionOutcome(
        criterion_id="determinism",
        verdict=("PASS" if not failures else "FAIL"),
        failures=tuple(failures),
        reported=reported,
    )


def _criterion_mutation_campaign(
    mutation_report: Mapping[str, Any] | None,
    mutation_registry_ids: frozenset[str] | None,
    mutation_registry_digest: str | None,
    head: str | None = None,
) -> CriterionOutcome:
    failures: list[str] = []
    if mutation_report is None:
        failures.append(
            "no mutation registry report was supplied — criterion 6 is never silently skipped"
        )
        reported: dict[str, object] = {"supplied": False}
    elif isinstance(mutation_report.get("__malformed__"), str):
        # the TOCTOU residue (round-4 P0): the report passed preflight but
        # changed shape before evaluation — an honest criterion FAIL, never
        # a post-consumption exception
        failures.append(
            f"the mutation report can no longer be evaluated: {mutation_report['__malformed__']}"
        )
        reported = {"supplied": True, "malformed": True}
    else:
        total = int(mutation_report.get("total", -1))
        killed = int(mutation_report.get("totals", {}).get("KILLED", -1))
        # STRICT boolean (round-4 P0): bool("false") is True — a string is a
        # lie, not a pass; the preflight's schema validation rejects the
        # type outright, and this is the evaluation-side backstop
        restoration = mutation_report.get("restoration_suite_passed") is True
        if killed != total:
            failures.append(f"mutation registry {killed}/{total} KILLED — not N/N")
        if not restoration:
            failures.append("the mutation registry's restoration suite did not pass")
        entries = mutation_report.get("mutants", [])
        report_ids = [str(entry.get("id")) for entry in entries]
        if total != len(report_ids):
            failures.append(
                f"the mutation report declares total={total} but carries"
                f" {len(report_ids)} mutant entries — a self-inconsistent report"
                " is not this head's campaign"
            )
        if len(set(report_ids)) != len(report_ids):
            failures.append(
                "the mutation report carries duplicate mutant ids — padding"
                " cannot stand in for a campaign"
            )
        # count KILLED from the ENTRIES themselves (round-2 P0): a report
        # whose totals claim N/N while its own entries say otherwise is a
        # forgery, not a campaign
        killed_entries = sum(1 for entry in entries if str(entry.get("verdict")) == "KILLED")
        if killed_entries != total:
            failures.append(
                f"the report's entries carry {killed_entries} KILLED verdicts"
                f" but declare total={total} — the declared N/N is not what"
                " the entries say"
            )
        covering = [
            str(entry.get("id"))
            for entry in entries
            if "g4" in str(entry.get("file", "")) + str(entry.get("invariant", "")).lower()
        ]
        if not covering:
            failures.append(
                "no registry mutant covers the gate's own verdict logic — the"
                " plan requires at least one"
            )
        if mutation_registry_ids is None or mutation_registry_digest is None:
            failures.append(
                "the live mutation registry was not supplied — criterion 6"
                " cannot bind the report to the registry at this head (never"
                " silently skipped)"
            )
        else:
            # None-safe by construction (the branch is entered only when
            # both are supplied — but a mutant that drops the absence check
            # must degrade to a behavioral failure, never a TypeError)
            registry_ids = mutation_registry_ids or frozenset()
            missing = sorted(registry_ids - set(report_ids))
            foreign = sorted(set(report_ids) - registry_ids)
            if missing:
                failures.append(
                    f"the mutation report omits {len(missing)} of the"
                    f" registry's {len(mutation_registry_ids)} mutants (e.g."
                    f" {missing[0]}) — a stale report is not this head's"
                    " campaign"
                )
            if foreign:
                failures.append(
                    f"the mutation report carries {len(foreign)} ids foreign"
                    f" to the registry (e.g. {foreign[0]}) — a stale or"
                    " foreign report is not this head's campaign"
                )
            stamped_digest = mutation_report.get("registry_digest")
            if not isinstance(stamped_digest, str) or stamped_digest != mutation_registry_digest:
                failures.append(
                    "the report's registry_digest does not match the live"
                    " registry — the campaign was not run against this"
                    " registry revision (or was not run at all)"
                )
        if head is not None:
            # bind the report to the SEALED head (round-3 P0): the registry
            # can be identical across commits while the guarded code moved,
            # so a registry-bound report from another head is still stale
            stamped_head = mutation_report.get("head")
            if not isinstance(stamped_head, str) or stamped_head != head:
                failures.append(
                    f"the report's head {stamped_head!r} is not the sealed head"
                    f" {head!r} — the campaign ran at a different head"
                )
        reported = {
            "supplied": True,
            "total": total,
            "killed": killed,
            "killed_entries": killed_entries,
            "restoration_suite_passed": restoration,
            "verdict_logic_mutants": covering[:5],
            "registry_supplied": mutation_registry_ids is not None,
            "registry_total": (
                len(mutation_registry_ids) if mutation_registry_ids is not None else None
            ),
            "registry_digest_match": (
                mutation_report.get("registry_digest") == mutation_registry_digest
                if mutation_registry_digest is not None
                else None
            ),
            "report_head": mutation_report.get("head"),
            "sealed_head": head,
        }
    return CriterionOutcome(
        criterion_id="mutation_campaign",
        verdict=("PASS" if not failures else "FAIL"),
        failures=tuple(failures),
        reported=reported,
    )


def date_from_iso(value: str) -> Any:
    from datetime import date

    return date.fromisoformat(value)


def evaluate_g4_criteria(
    *,
    protocol: ResearchProtocol,
    lane1_census: Mapping[str, Any],
    lane2_census: Mapping[str, Any],
    trial_payloads: Mapping[str, Mapping[str, Any]],
    trial_statuses: Mapping[str, str],
    execution_calendar: SessionCalendar,
    stamped_hashes: Mapping[str, str],
    replay_hashes: Mapping[str, str] | None,
    mutation_report: Mapping[str, Any] | None,
    era_target: Mapping[str, int],
    replay_aliased: bool = False,
    mutation_registry_ids: frozenset[str] | None = None,
    mutation_registry_digest: str | None = None,
    head: str | None = None,
    rejection_floor: int = REJECTION_FLOOR,
) -> G4GateEvaluation:
    """Evaluate the six pre-declared criteria from the stamped payloads.

    ``trial_payloads`` maps the lane-2 arm id to its STAMPED payload (the
    file's ``payload`` object); the censuses are the stamped lane census
    payloads; ``era_target`` carries the era's stamped counts (expected
    masters, distinct contracts) the plan's criterion 1 targets;
    ``replay_hashes`` the clean-clone replay's payload hashes; and
    ``mutation_report`` the registry report (N/N KILLED + restoration).
    ``mutation_registry_ids`` / ``mutation_registry_digest`` are the LIVE
    registry's id set and content digest at this head — criterion 6 binds
    the report to them (a report that omits registry mutants, carries
    foreign or duplicate ids, disagrees with its own entries' verdicts, or
    lacks the matching registry digest is stale or forged and FAILs;
    absent registry = FAIL, never silently skipped). ``head`` (normally
    always the sealed head) additionally binds the report to the exact
    commit the campaign ran at."""
    outcomes = (
        _criterion_manifest_integrity(lane1_census, lane2_census, era_target),
        _criterion_candidate_discipline(protocol, lane2_census, trial_payloads),
        _criterion_fill_discipline(trial_payloads, execution_calendar),
        _criterion_rejection_paths(lane1_census, lane2_census, trial_payloads, rejection_floor),
        _criterion_determinism(stamped_hashes, replay_hashes, replay_aliased),
        _criterion_mutation_campaign(
            mutation_report, mutation_registry_ids, mutation_registry_digest, head
        ),
    )
    verdict = "PASS" if all(o.verdict == "PASS" for o in outcomes) else "FAIL"
    return G4GateEvaluation(
        verdict=verdict,
        criteria=outcomes,
        reported={
            "trial_statuses": dict(trial_statuses),
            "rejection_floor": rejection_floor,
            "flow_amendment_value": FLOW_MIN_SESSION_VOLUME_AMENDMENT,
            "era_target": dict(era_target),
        },
    )


def payload_hashes(paths: Mapping[str, Path]) -> dict[str, str]:
    """sha256 per stamped payload file, keyed by file name (the comparison
    set criterion 5 consumes — the M3 cleanclone pattern)."""
    import hashlib

    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for _, path in sorted(paths.items())
    }


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ---- the production path set (one factory, shared by the CLI + the runner) -------


@dataclass(frozen=True)
class G4GatePaths:
    """Every auxiliary input + output location the sealed gate consumes,
    derived from one repo root (checkout-relative, like the M3 defaults)."""

    evidence_root: Path
    registry: Path
    artifacts_dir: Path
    scratch_root: Path
    era_census: Path
    replay_artifacts: Path
    mutation_report: Path
    spot_proxy_v2: Path | None = None


def production_gate_paths(repo_root: Path) -> G4GatePaths:
    repo_root = Path(repo_root)
    return G4GatePaths(
        evidence_root=repo_root / "docs" / "evidence-logs" / "m4",
        registry=repo_root / "artifacts" / "g4-sealed.db",
        artifacts_dir=repo_root / "artifacts" / "g4-sealed",
        scratch_root=repo_root / "artifacts" / "g4-sealed-scratch",
        era_census=repo_root / "artifacts" / "census" / "43b0b040ea3c" / "census.json",
        replay_artifacts=repo_root / "artifacts" / "g4-sealed-replay",
        mutation_report=repo_root / "artifacts" / "m0-mutations.json",
        # the OPTIONAL declared v2 dollar-volume source (the post-closeout
        # recapture's natural sidecar seat): absent = the protocol's declared
        # term fails honestly on the sentinel, never a fabricated median
        spot_proxy_v2=repo_root / "artifacts" / "spot-proxy-v2.json",
    )


def era_target_of(era: Mapping[str, Any]) -> dict[str, int]:
    """The era's stamped counts the plan's criterion 1 targets.

    Every count must be a TRUE int (round-6 P0): a JSON float — including
    ``1e309``'s ``inf`` — passes a plain ``isinstance(payload, dict)`` check
    and then raises ``OverflowError`` at ``int()`` only after the one-shot
    has run. Refused here, where the preflight calls it."""

    def _count(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"era census count {value!r} is not an integer")
        return value

    return {
        "expected_masters": _count(era["coverage"]["expected_masters"]),
        "distinct_contracts": _count(
            era["values"]["observed_census_fact"]["distinct_contracts"]["v"]
        ),
    }


def live_mutation_registry(repo_root: Path) -> tuple[frozenset[str], str] | None:
    """The LIVE mutation registry at this head: the authored ``MUTANTS``
    list in ``scripts/mutate.py`` (the JSON artifact is generated output),
    as (id set, registry digest). The digest is sha256 over the canonical
    MUTANTS list — the same value the mutation runner stamps into its
    report — so a report generated from a different registry revision (or
    hand-forged) cannot present it. Loaded by file path — scripts/ is not
    an importable package. None when the registry file is absent (criterion
    6 then FAILs loudly rather than certifying an unbound report)."""
    registry_path = repo_root / "scripts" / "mutate.py"
    if not registry_path.is_file():
        return None
    import hashlib
    import importlib.util
    import json

    spec = importlib.util.spec_from_file_location("tree_options_mutation_registry", registry_path)
    if spec is None or spec.loader is None:  # pragma: no cover - malformed path only
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        mutants = module.MUTANTS
        digest = hashlib.sha256(
            json.dumps(mutants, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        # ID extraction INSIDE the wrap (round-7 P0): a syntactically
        # loadable registry whose MUTANTS entries are malformed (no "id")
        # raises KeyError here — a preflight fact, never a post-consumption
        # escape the evaluation's GatePreflightError handler cannot contain
        ids = frozenset(str(m["id"]) for m in mutants)
    except Exception as exc:  # an unloadable or malformed registry is a preflight fact
        raise GatePreflightError(
            f"the live mutation registry {registry_path} failed to load ({exc!r}) —"
            " refusing BEFORE the one-shot event runs"
        ) from None
    return ids, digest


def live_mutation_registry_ids(repo_root: Path) -> frozenset[str] | None:
    """The LIVE registry's id set alone (see ``live_mutation_registry``)."""
    loaded = live_mutation_registry(repo_root)
    return None if loaded is None else loaded[0]


class GatePreflightError(RuntimeError):
    """An auxiliary gate input cannot possibly evaluate. Raised BEFORE the
    one-shot event runs so a malformed report or an unloadable registry can
    never burn the sealed workspace (or, under the seal, the CONSUMPTION)
    without a verdict — the 2026-08-31 failure mode, never again."""


class MutationReportSchemaError(GatePreflightError):
    """A PRESENT mutation report whose shape cannot be evaluated: wrong
    types where the evaluation would raise (a non-int total, a non-list
    mutants array, a non-boolean restoration flag). Refused at preflight —
    BEFORE anything the event creates — and surfaced as an honest criterion
    FAIL if the file changed shape only after the preflight (the TOCTOU
    residue), never as a post-consumption exception."""


def validate_mutation_report(payload: object) -> None:
    """Strict shape validation of a mutation report (round-4 P0s).

    Every field the evaluation CASTS or branches on must already be the
    right TYPE: a present-but-malformed report refuses at preflight, never
    raises at evaluation time after the one-shot has run. (An ABSENT report
    is different: that is an honest criterion FAIL, a verdict.)"""
    if not isinstance(payload, dict):
        raise MutationReportSchemaError("the mutation report is not a JSON object")
    mutants = payload.get("mutants")
    if not isinstance(mutants, list) or not all(isinstance(entry, dict) for entry in mutants):
        raise MutationReportSchemaError("the mutation report's 'mutants' is not a list of objects")
    for entry in mutants:
        if not isinstance(entry.get("id"), str):
            raise MutationReportSchemaError("a mutant entry carries a non-string id")
        if not isinstance(entry.get("verdict"), str):
            raise MutationReportSchemaError("a mutant entry carries a non-string verdict")
    totals = payload.get("totals")
    if not isinstance(totals, dict) or not isinstance(totals.get("KILLED"), int):
        raise MutationReportSchemaError("the mutation report's 'totals.KILLED' is not an integer")
    if not isinstance(payload.get("total"), int):
        raise MutationReportSchemaError("the mutation report's 'total' is not an integer")
    restoration = payload.get("restoration_suite_passed")
    if not isinstance(restoration, bool):
        raise MutationReportSchemaError(
            "the mutation report's 'restoration_suite_passed' is not a boolean"
            f" (got {type(restoration).__name__!r}: bool('false') is True — the"
            " string is a lie, not a pass)"
        )
    for key in ("head", "registry_digest"):
        value = payload.get(key)
        if value is not None and not isinstance(value, str):
            raise MutationReportSchemaError(f"the mutation report's {key!r} is not a string")


def preflight_gate_auxiliaries(*, paths: G4GatePaths, repo_root: Path) -> None:
    """Refuse BEFORE the one-shot event when the auxiliary criterion inputs
    would raise at evaluation time.

    An ABSENT report or a stale one is an honest criterion FAIL (a verdict,
    recorded verbatim); a report that cannot be PARSED or whose SHAPE cannot
    be evaluated, or a registry that cannot be LOADED, is an exception — and
    an exception after the event has run leaves consumed authority with no
    verdict. All are checked here, ahead of anything the event creates."""
    live_mutation_registry(repo_root)  # raises GatePreflightError when unloadable
    if not paths.era_census.is_file():
        raise GatePreflightError(
            f"the era census {paths.era_census} is absent — criterion 1's count"
            " target is a REQUIRED stamped input and its absence would raise"
            " only AFTER the one-shot event ran; refusing before anything is"
            " created"
        )
    # round-5/6 P0: existence alone is not evaluability — a PRESENT census
    # that cannot be parsed, or whose counts are not true ints (a JSON
    # float like 1e309 passes a shape check and raises OverflowError only
    # at the post-event int()), refuses here, with nothing spent
    try:
        era_payload = json.loads(paths.era_census.read_text(encoding="utf-8"))
    except (OSError, RecursionError, ValueError) as exc:
        raise GatePreflightError(
            f"the era census {paths.era_census} cannot be parsed ({exc!r}) —"
            " refusing BEFORE the one-shot event runs"
        ) from None
    if not isinstance(era_payload, dict):
        raise GatePreflightError(
            f"the era census {paths.era_census} is not a JSON object — refusing"
            " BEFORE the one-shot event runs"
        )
    try:
        era_target_of(era_payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise GatePreflightError(
            f"the era census {paths.era_census} cannot be evaluated ({exc!r}) —"
            " refusing BEFORE the one-shot event runs"
        ) from None
    if paths.mutation_report.is_file():
        try:
            loaded = json.loads(paths.mutation_report.read_text(encoding="utf-8"))
        except (OSError, RecursionError, ValueError) as exc:
            raise GatePreflightError(
                f"the mutation report {paths.mutation_report} cannot be parsed"
                f" ({exc!r}) — refusing BEFORE the one-shot event runs"
            ) from None
        try:
            validate_mutation_report(loaded)
        except MutationReportSchemaError as exc:
            raise GatePreflightError(
                f"{paths.mutation_report}: {exc} — refusing BEFORE the one-shot event runs"
            ) from None


def evaluate_and_record(
    run: G4SealedRun,
    held: HeldVerifiedSealedInputs,
    *,
    paths: G4GatePaths,
    repo_root: Path,
    head: str,
    allow_dirty: bool = False,
    log_lines: Sequence[str] = (),
    mutation_registry_ids: frozenset[str] | None = None,
    mutation_registry_digest: str | None = None,
    rejection_floor: int = REJECTION_FLOOR,
) -> G4GateEvaluation:
    """Evaluate the six criteria from the run's stamped payloads, write the
    evidence triple + the stamped summary, and return the evaluation.

    The auxiliary criterion inputs are the prior events' STAMPED artifacts:
    the era census (criterion 1's targets), the clean-clone replay payload
    dir (criterion 5; absent = an honest FAIL, never a skip), and the
    mutation registry report at the sealed head (criterion 6 — bound to
    ``mutation_registry_ids``, the LIVE registry at this head; a stale
    report FAILs). ``rejection_floor`` defaults to the pre-declared 50; it
    is a parameter so a fixture-scale MINI gate can exercise the machinery."""
    protocol = load_protocol_bytes(held.protocol_bytes)
    lane1_census = load_json(run.census_payload_paths["lane1"])["payload"]
    lane2_census = load_json(run.census_payload_paths["lane2"])["payload"]
    trial_payloads = {
        arm: load_json(path)["payload"] for (_lane, arm), path in run.trial_payload_paths.items()
    }
    trial_statuses = {
        f"lane{lane}|{arm}": status for (lane, arm), status in run.trial_statuses.items()
    }
    stamped_paths = {
        **run.census_payload_paths,
        **{f"{lane}|{arm}": path for (lane, arm), path in run.trial_payload_paths.items()},
    }
    stamped_hashes = payload_hashes(stamped_paths)
    # round-6 P0: a PRESENT but partial or unreadable replay dir must FAIL
    # criterion 5 as a verdict — payload_hashes reads eagerly, so an
    # absent/unreadable replay payload would raise FileNotFoundError/OSError
    # AFTER consumption; None is criterion 5's honest absent-replay failure.
    # round-8 P0: an ALIASED "replay" — payload symlinks onto the run's own
    # artifacts, or the replay dir IS the artifacts dir — compares
    # byte-identical BY CONSTRUCTION; criterion 5 must name the aliasing,
    # never certify determinism by self-comparison
    replay_aliased = False
    if paths.replay_artifacts.is_dir():
        replay_map = {
            name: paths.replay_artifacts / path.relative_to(run.artifacts_dir)
            for name, path in stamped_paths.items()
        }
        replay_aliased = paths.replay_artifacts.resolve() == run.artifacts_dir.resolve() or any(
            path.is_symlink() for path in replay_map.values()
        )
        if replay_aliased:
            replay_hashes = None
        else:
            try:
                replay_hashes = payload_hashes(replay_map)
            except OSError:
                replay_hashes = None
    else:
        replay_hashes = None
    if paths.mutation_report.is_file():
        # TOCTOU backstop (round-4 P0): preflight validated this file before
        # the event ran; if it changed shape since, that is an honest
        # criterion FAIL — never an exception after consumption
        try:
            loaded_report = json.loads(paths.mutation_report.read_text(encoding="utf-8"))
            validate_mutation_report(loaded_report)
            mutation_report = loaded_report
        except (OSError, RecursionError, ValueError) as exc:
            mutation_report = {"__malformed__": f"unparseable ({exc!r})"}
        except MutationReportSchemaError as exc:
            mutation_report = {"__malformed__": str(exc)}
    else:
        mutation_report = None
    if mutation_registry_ids is None:
        # the LIVE registry at this head, derived from the repo being sealed
        # (absent registry file = None = criterion 6 FAILs, never a skip).
        # Round-6 P0: the derive itself must be exception-safe HERE — the
        # preflight is the refusal point, and a registry that became
        # unloadable after it is a post-spend shape change: a criterion-6
        # FAIL verdict, never a propagated GatePreflightError
        try:
            loaded_registry = live_mutation_registry(repo_root)
        except GatePreflightError:
            loaded_registry = None
        if loaded_registry is not None and mutation_registry_digest is None:
            mutation_registry_ids, mutation_registry_digest = loaded_registry
    if not paths.era_census.is_file():
        # round-6 P0: the preflight required this file; its removal after
        # the preflight is a post-spend shape change — an honest criterion-1
        # FAIL, never an exception after consumption
        era_target: dict[str, Any] = {"__malformed__": "absent (removed after the preflight)"}
    else:
        # TOCTOU backstop (round-5): the preflight parsed this file before
        # the event ran; if it changed shape since, criterion 1 FAILs as a
        # verdict — never an exception after consumption
        try:
            era_target = era_target_of(load_json(paths.era_census))
        except (OSError, RecursionError, ValueError, KeyError, TypeError) as exc:
            era_target = {"__malformed__": f"unparseable ({exc!r})"}
    assert run.execution_calendar is not None
    evaluation = evaluate_g4_criteria(
        protocol=protocol,
        lane1_census=lane1_census,
        lane2_census=lane2_census,
        trial_payloads=trial_payloads,
        trial_statuses=trial_statuses,
        execution_calendar=run.execution_calendar,
        stamped_hashes=stamped_hashes,
        replay_hashes=replay_hashes,
        replay_aliased=replay_aliased,
        mutation_report=mutation_report,
        era_target=era_target,
        mutation_registry_ids=mutation_registry_ids,
        mutation_registry_digest=mutation_registry_digest,
        head=head,
        rejection_floor=rejection_floor,
    )
    write_gate_evidence(
        evaluation,
        run=run,
        held=held,
        evidence_root=paths.evidence_root,
        repo_root=repo_root,
        head=head,
        run_log=log_lines,
        allow_dirty=allow_dirty,
    )
    return evaluation


def write_gate_evidence(
    evaluation: G4GateEvaluation,
    *,
    run: G4SealedRun,
    held: HeldVerifiedSealedInputs,
    evidence_root: Path,
    repo_root: Path,
    head: str,
    run_log: Sequence[str],
    allow_dirty: bool = False,
) -> dict[str, Path]:
    """Write the evidence triple (md/json/log) + the stamped summary.

    House rule: counts + samples in the evidence log, full lists elided
    (the stamped payloads under the artifacts dir carry the full rows)."""
    evidence_root.mkdir(parents=True, exist_ok=True)
    protocol = load_protocol_bytes(held.protocol_bytes)
    written: dict[str, Path] = {}
    summary = {
        "gate": G4_SEALED_EVENT_ID,
        "head": head,
        "verdict": evaluation.verdict,
        "criteria": [
            {
                "id": o.criterion_id,
                "verdict": o.verdict,
                "failures": list(o.failures),
                "reported": o.reported,
                "lane1_applicability": o.lane1_applicability,
                "lane1_inapplicable_reason": o.lane1_inapplicable_reason,
            }
            for o in evaluation.criteria
        ],
        "reported": evaluation.reported,
        "stamped_payloads": {
            name: str(path) for name, path in sorted(run.census_payload_paths.items())
        },
        "trial_payloads": {
            f"lane{lane}|{arm}": str(path)
            for (lane, arm), path in sorted(run.trial_payload_paths.items())
        },
    }
    _stamp = build_stamp(
        protocol,
        trial_id=f"{G4_SEALED_EVENT_ID}-summary",
        config={"gate": G4_SEALED_EVENT_ID, "head": head},
        dataset_manifest_hash=held.packet.lane2_manifest.typed_manifest_content_hash,
        repo=repo_root,
        allow_dirty=allow_dirty,
    )
    write_artifact(run.artifacts_dir / "sealed-gate-summary.json", summary, _stamp)
    json_path = evidence_root / "m4-g4-sealed-gate.json"
    json_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    written["json"] = json_path
    log_path = evidence_root / "m4-g4-sealed-gate.log"
    lines = [*run_log, f"SEALED_GATE_VERDICT={evaluation.verdict}"]
    for outcome in evaluation.criteria:
        if outcome.verdict == "PASS":
            lines.append(f"SEALED_CHECK PASS {outcome.criterion_id}")
        else:
            lines.append(f"SEALED_CHECK FAIL {outcome.criterion_id}")
            for failure in outcome.failures:
                lines.append(f"SEALED_CHECK FAIL {outcome.criterion_id}: {failure}")
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    written["log"] = log_path
    md_lines = [
        f"# {G4_SEALED_EVENT_ID} — sealed real-data gate (evidence log)",
        "",
        f"- head: `{head}`",
        f"- verdict: **{evaluation.verdict}** (recorded verbatim; one-shot — no"
        " re-run inside the campaign regardless of outcome)",
        f"- trial statuses: `{evaluation.reported['trial_statuses']}`",
        "",
        "| # | criterion | verdict | lane-1 applicability |",
        "|---|-----------|---------|----------------------|",
    ]
    for index, outcome in enumerate(evaluation.criteria, start=1):
        md_lines.append(
            f"| {index} | `{outcome.criterion_id}` | {outcome.verdict}"
            f" | {outcome.lane1_applicability} |"
        )
    md_lines.append("")
    for outcome in evaluation.criteria:
        md_lines.append(f"## {outcome.criterion_id} — {outcome.verdict}")
        md_lines.append("")
        if outcome.lane1_applicability == "declared_inapplicable":
            md_lines.append(f"- lane 1: {outcome.lane1_inapplicable_reason}")
        if outcome.failures:
            md_lines.append("- failures (verbatim):")
            for failure in outcome.failures[:10]:
                md_lines.append(f"  - {failure}")
            if len(outcome.failures) > 10:
                md_lines.append(f"  - … {len(outcome.failures) - 10} more (full list in the .json)")
        else:
            md_lines.append("- no failures")
        reported = json.dumps(outcome.reported, indent=2, sort_keys=True, default=str)
        md_lines.append(f"- reported (counts + samples):\n\n```json\n{reported}\n```")
        md_lines.append("")
    md_path = evidence_root / "m4-g4-sealed-gate.md"
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    written["md"] = md_path
    return written


__all__ = [
    "CRITERION_IDS",
    "REJECTION_FLOOR",
    "CriterionOutcome",
    "G4GateEvaluation",
    "G4GatePaths",
    "era_target_of",
    "evaluate_and_record",
    "evaluate_g4_criteria",
    "load_json",
    "payload_hashes",
    "production_gate_paths",
    "write_gate_evidence",
]

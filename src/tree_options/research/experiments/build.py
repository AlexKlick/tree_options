"""Project original artifacts without running studies or touching trading authority.

This runs in a bounded publisher, never in an HTTP request. Every source fails
independently. SQLite is read through verified snapshots; mutable source files
are captured once. No inferred zero, research promotion, or portfolio return.
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tree_options.research.experiments.contracts import (
    Artifact,
    Experiment,
    Index,
    Metric,
    PilotProgram,
    Source,
    digest,
)
from tree_options.research.experiments.io import Unavailable, load

# Explicit accounting lineage, verified against the campaign exit packet.
# Correction is post-hoc; it is not a new independent or confirmatory study.
ORIGINAL = "0570b72e91574bb841c3ff7b3dde694dd971c985385415a6033130c74958312b"
CORRECTED = "248a63f98bc9ec23919a7cc65e0b3017dc71cff914ba9e7e67222694028df4da"
MAX_FILES = 2000


def count(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise Unavailable("invalid_count")
    return value


def money(value: Any) -> str:
    if not isinstance(value, str) or not Decimal(value).is_finite():
        raise Unavailable("invalid_decimal_money")
    return value


def files(root: Path, pattern: str) -> list[Path]:
    if not root.is_dir():
        raise FileNotFoundError
    result = []
    for path in root.glob(pattern):
        result.append(path)
        if len(result) > MAX_FILES:
            raise Unavailable("artifact_count_limit")
    return sorted(result)


def artifact(path: Path, schema: str) -> tuple[dict[str, Any], Artifact]:
    doc, sha = load(path)
    if doc.get("schema") != schema:
        raise Unavailable("unsupported_artifact_schema")
    return doc, Artifact(label=path.name, sha256=sha)


def metric(label: str, value: Any, unit: str) -> Metric:
    return Metric(label=label, value=value, unit=unit)


def unknown(label: str, unit: str, reason: str) -> Metric:
    return Metric(label=label, unit=unit, reason=reason)


def observed_time(value: Any, now: datetime) -> dict[str, Any]:
    try:
        at = datetime.fromisoformat(value)
        if at.tzinfo is None:
            raise ValueError
        age = (now - at).total_seconds()
        return {
            "source_time": at.isoformat(),
            "freshness": "observed" if 0 <= age <= 26 * 3600 else "stale",
            "freshness_reason": "Source observation time; publication does not refresh observations.",
        }
    except (ValueError, TypeError):
        return {"freshness": "unavailable", "freshness_reason": "Source timestamp unavailable."}


def historical(**kwargs: Any) -> Experiment:
    return Experiment(
        evidence_class="historical_simulation",
        freshness="archived",
        freshness_reason="Archived result; rereading it adds no observations.",
        **kwargs,
    )


def challenge(root: Path) -> list[Experiment]:
    doc, ref = artifact(root / "standings.json", "desk-challenge-standings/1")
    rows = []
    for row in doc["policies"]:
        sessions = {date.fromisoformat(key.split(":", 1)[1]) for key in row["session_pnl"]}
        if count(row["sessions_distinct"]) != len(sessions):
            raise Unavailable("challenge_session_count_mismatch")
        rows.append(
            historical(
                id=f"challenge-{row['policy']}",
                source="challenge",
                title=f"Nightly challenge: {row['policy']}",
                hypothesis="Can this decision policy outperform the registered controls after modeled costs?",
                status="historical_replay",
                metrics=[
                    metric("Repeated games", count(row["games"]), "games"),
                    metric("Distinct historical market sessions", len(sessions), "sessions"),
                    metric("Cumulative replay entries", count(row["entered"]), "entries"),
                    metric("Cumulative replay P&L", money(row["closed_pnl_sum"]), "USD modeled"),
                    metric("Model calls", count(row["model_calls"]), "calls"),
                    unknown(
                        "Actual research cost", "USD", "Provider invoices are not in standings."
                    ),
                ],
                limitations=[
                    "Repeated games reuse historical sessions; cumulative P&L is not an account return.",
                    "Session count is a resampling unit, not a claim of statistical independence.",
                    "The original registration, retirement rule and operator review still apply.",
                ],
                next_decision="Review the sealed challenge rule and session-clustered comparisons.",
                artifacts=[ref],
                advanced_link="desk/evidence",
            )
        )
    return rows


def longrun(path: Path, source: str) -> Experiment:
    doc, ref = artifact(path, "desk-longrun-digest/1")
    if doc["promotion"]["promoted"] is not False:
        raise Unavailable("longrun_authority_conflict")
    boards = doc["boards"]
    total, scored = count(boards["total"]), count(boards["scored"])
    if scored > total or type(doc["complete"]) is not bool:
        raise Unavailable("longrun_counts_invalid")
    return historical(
        id=f"longrun-{doc['run_id']}-{ref.sha256[:12]}",
        source=source,
        title=f"Options long run {doc['run_id']}",
        hypothesis="Can a policy beat the incumbent and random control on the frozen test sessions?",
        status="incomplete" if not doc["complete"] else "recorded",
        source_time=doc["at"],
        metrics=[
            metric("Scored boards", scored, "boards"),
            metric("Missing boards", total - scored, "boards"),
            metric(
                "Distinct historical market sessions",
                count(boards["sessions"]["count"]),
                "sessions",
            ),
        ],
        limitations=[
            doc["headline"],
            "Historical modeled economics; no forward qualification or account return.",
            "Confidence and selection rules remain in the original report; no new statistical claim.",
        ],
        next_decision="Inspect the closed campaign and corrected accounting; a successor needs a new registration.",
        artifacts=[ref],
        advanced_link="desk/evidence",
    )


def qsl(path: Path, now: datetime) -> list[Experiment]:
    from tree_options.desk.evidence import EvidenceStore
    from tree_options.desk.qsl import PREREG_ID, PREREG_SHA256
    from tree_options.desk.scorecards import build_scorecards

    with tempfile.TemporaryDirectory(prefix="trex-qsl-read-") as scratch:
        backup = Path(scratch) / "qsl.sqlite3"
        with EvidenceStore(path, readonly=True) as store:
            store.backup(backup)
        cards = build_scorecards(backup)
        with EvidenceStore(backup, readonly=True) as store:
            episodes = store.all("episode")
            evaluations = store.all_at("evaluation")
    timestamps = [at for _, at in evaluations]
    families = cards["families"]
    metrics = [
        metric(
            "Episodes within evaluation horizon",
            sum(count(f["n_episodes"]) for f in families),
            "episodes",
        ),
        metric(
            "Retrospective registrations in evaluation",
            sum(count(f["n_retrospective_registrations"]) for f in families),
            "episodes",
        ),
        metric("Registered episodes", len(episodes), "episodes"),
        metric(
            "Distinct entry sessions",
            len({date.fromisoformat(e["entry_session"]) for e in episodes}),
            "sessions",
        ),
    ]
    for label, key in (
        ("Resolved episodes", "n_resolved"),
        ("Missing outcomes", "n_censored"),
        ("Open episodes", "n_open"),
    ):
        metrics.append(
            metric(label, sum(count(f[key]) for f in families), "episodes")
            if evaluations
            else unknown(label, "episodes", "No evaluation is recorded.")
        )
    return [
        Experiment(
            id="qsl",
            source="qsl",
            title="Quote-bearing options observation",
            evidence_class="forward_shadow",
            hypothesis="Do the frozen put-credit and call-debit candidates retain value after snapshot crossing costs?",
            status=cards["status"],
            **observed_time(max(timestamps) if timestamps else None, now),
            metrics=metrics,
            limitations=[
                "Snapshot-crossing modeled fills, not broker executions or intraday fill evidence.",
                "Retrospective registrations may be present; the sample floor is not statistical confidence.",
                "Missing marks censor outcomes; they do not mean zero loss.",
            ],
            blockers=[] if evaluations else ["No QSL evaluation recorded."],
            next_decision="Continue the original observation registration; review resolved and censored outcomes.",
            artifacts=[
                Artifact(label=PREREG_ID, sha256=PREREG_SHA256),
                Artifact(label="verified evidence audit", sha256=cards["audit"]["head_sha256"]),
            ],
            advanced_link="desk/evidence",
        )
    ]


def campaigns(root: Path) -> list[Experiment]:
    from tree_options.research.catalog.sealed_round import build_candidate

    rows = []
    for scope in files(root, "*"):
        if not scope.is_dir():
            continue
        candidate = build_candidate(scope)
        rows.append(
            historical(
                id=f"campaign-{digest(candidate.id)[:24]}",
                source="campaigns",
                title=f"Sealed study: {candidate.family}",
                hypothesis="Unavailable: inspect the original sealed study for its registered hypothesis.",
                status=f"closed_record: {candidate.disposition.value}",
                limitations=[
                    candidate.funded_history_reason or "Funded history unavailable.",
                    "This view preserves the recorded verdict and cannot reopen the campaign.",
                ],
                metrics=[
                    unknown(
                        "Independent sample count",
                        "observations",
                        "Not declared by the sealed catalog adapter.",
                    )
                ],
                next_decision="Retain the sealed verdict. Register any successor as a new study.",
                artifacts=[
                    Artifact(label=k, sha256=v)
                    for k, v in candidate.artifact_hashes.items()
                    if len(v) == 64
                ],
                advanced_link="#/research",
            )
        )
    return rows


def research(root: Path, now: datetime) -> list[Experiment]:
    from tree_options.research.runstate.store import RunstateStore

    if (root / "runstate.sqlite3").stat().st_size > 128_000_000:
        raise Unavailable("research_snapshot_size_limit")
    # Supported SQLite backup into memory: verify and project the SAME immutable
    # snapshot. Opening the source read-only neither creates nor migrates it.
    with sqlite3.connect(
        (root / "runstate.sqlite3").resolve().as_uri() + "?mode=ro", uri=True
    ) as source:
        store = RunstateStore(Path(":memory:"))
        try:
            source.backup(store.conn)
            audit = store.verify()
            runs = store.all("run")
            specs = {r.get("id", r.get("run_id")): r for r in store.all("spec")}
            rows = []
            for run in runs:
                key = run["run_id"]
                spec = specs.get(run.get("spec_hash", key), {})
                rows.append(
                    Experiment(
                        id=f"worker-{key}",
                        source="research",
                        title=f"ResearchWorker: {run.get('kind', 'comparison')}",
                        hypothesis=run.get("hypothesis")
                        or spec.get("hypothesis")
                        or spec.get("notes")
                        or "Unavailable: inspect the original run specification.",
                        evidence_class="historical_simulation",
                        status=run["status"],
                        **observed_time(run.get("completed_at", run.get("queued_at")), now),
                        metrics=[
                            unknown(
                                "Distinct market sessions",
                                "sessions",
                                "Not established by run lifecycle metadata.",
                            ),
                            unknown(
                                "Realized research cost",
                                "USD",
                                "No cost receipt in the lifecycle record.",
                            ),
                        ],
                        limitations=[
                            "Lifecycle completion is not strategy qualification.",
                            "Synthetic and unqualified inputs remain research-only; see the original result.",
                            "Daily equity roundtrips do not establish persistent portfolio performance.",
                        ],
                        next_decision="Inspect the frozen input, original result and holdout disposition.",
                        artifacts=[
                            Artifact(
                                label="verified ResearchWorker audit", sha256=audit["head_sha256"]
                            ),
                            Artifact(label="run record", sha256=digest(run)),
                        ],
                        advanced_link="#/workspace",
                    )
                )
            return rows
        finally:
            store.close()


def datasets(root: Path) -> list[Experiment]:
    from tree_options.research.quant_jobs import dataset_catalog

    rows = []
    for dataset in dataset_catalog(root):
        rows.append(
            historical(
                id=f"dataset-{dataset['dataset_id']}",
                source="datasets",
                title=dataset["label"],
                hypothesis="Input qualification for equity control and challenger studies.",
                status=dataset["data_class"],
                metrics=[metric("Universe members", count(dataset["universe_count"]), "members")],
                limitations=["A frozen input hash is not independent point-in-time qualification."],
                blockers=[
                    "Qualified point-in-time universe, corporate actions, prices, calendar and cost provenance are required."
                ],
                next_decision="Qualify a frozen real dataset before registering the equity pilot.",
                advanced_link="#/workspace",
            )
        )
    return rows


def forward(root: Path, now: datetime) -> list[Experiment]:
    from tree_options.desk.forward_coverage import clock_coverage
    from tree_options.desk.forward_minutes import BARS_SCHEMA, DECISION_CLOCKS, _clock_covered

    rows = []
    for path in files(root / "bars", "*.json"):
        doc, ref = artifact(path, BARS_SCHEMA)
        session = date.fromisoformat(doc["session"])
        coverage = clock_coverage(doc)
        # Three-clock validation uses an exact minute; eight-clock coverage uses
        # [clock, clock+15min). Do NOT substitute one denominator for the other.
        contracts = doc["contracts"]
        three = sum(
            all(_clock_covered(e.get("bars", []), session, c) for c in DECISION_CLOCKS)
            for e in contracts.values()
            if e["status"] == "ok"
        )
        gaps: dict[str, int] = {}
        for entry in contracts.values():
            status = entry["status"]
            if status == "ok" and entry.get("bars"):
                continue
            reason = {"budget": "Budget exhaustion", "invalid": "Capture validation failure"}.get(
                status
            )
            reason = reason or (
                "No qualifying observation recorded"
                if status == "ok"
                else "Unclassified capture failure"
            )
            gaps[reason] = gaps.get(reason, 0) + 1
        rows.append(
            Experiment(
                id=f"forward-{session}",
                source="forward",
                title=f"Forward capture {session}",
                hypothesis="Can the existing feed supply the independently required three-clock and eight-clock coverage?",
                evidence_class="forward_shadow",
                status="coverage_only",
                **observed_time(doc.get("captured_at"), now),
                metrics=[
                    metric("Three-clock fully covered contracts", three, "contracts"),
                    metric("Attempted contracts", len(contracts), "contracts"),
                    metric(
                        "Eight-clock covered points",
                        count(coverage["summary"]["covered_points"]),
                        "contract-clock points",
                    ),
                    metric(
                        "Eight-clock scheduled points",
                        count(coverage["summary"]["scheduled_points"]),
                        "contract-clock points",
                    ),
                    *[metric(k, v, "contracts") for k, v in gaps.items()],
                ],
                limitations=[
                    "Coverage is not a restart verdict; the sealed 60-session requirement remains unchanged.",
                    "Provider and entitlement failures cannot be distinguished without explicit receipts.",
                ],
                next_decision="Resolve capture gaps; evaluate each clock requirement under the original registration.",
                artifacts=[ref],
                advanced_link="desk/evidence",
            )
        )
    return rows


def allocations(path: Path) -> list[Experiment]:
    doc, sha = load(path)
    sleeves = doc["sleeves"]
    if not isinstance(sleeves, list) or len({s["sleeve_id"] for s in sleeves}) != len(sleeves):
        raise Unavailable("allocation_identity_invalid")
    total = sum((Decimal(money(s["capital_usd"])) for s in sleeves), Decimal(0))
    return [
        Experiment(
            id="virtual-allocations",
            source="allocations",
            title="Preserved virtual allocations",
            hypothesis="Resource planning only; virtual capital is not a strategy result.",
            evidence_class="unavailable",
            status="virtual_allocation_plan",
            freshness="archived",
            freshness_reason="Recorded allocation plan; broker balances are separate.",
            metrics=[
                metric("Virtual allocations", len(sleeves), "allocations"),
                metric("Virtual capital", str(total), "USD virtual"),
                metric(
                    "Broker account unassigned",
                    sum(s["account_alias"] is None for s in sleeves),
                    "allocations",
                ),
            ],
            limitations=[
                "Virtual capital is not broker buying power or realized performance.",
                "Research assignment cannot be inferred from an account binding.",
            ],
            next_decision="Keep unused allocations unassigned; qualify the three-program pilot first.",
            artifacts=[Artifact(label="allocation plan", sha256=sha)],
            advanced_link="#/workspace",
        )
    ]


def pilot() -> list[PilotProgram]:
    return [
        PilotProgram(
            id="qsl",
            title="Existing QSL observation",
            state="existing_observation",
            hypothesis="Measure the original fixed candidates at recorded two-sided snapshots.",
            blockers=[
                "See QSL source freshness and missing outcomes. This index grants no additional resources."
            ],
            next_decision="Continue under its original owner and registration.",
        ),
        *[
            PilotProgram(
                id=key,
                title=title,
                state="blocked_draft",
                hypothesis=hypothesis,
                blockers=[
                    "Qualified real dataset unavailable.",
                    "Frozen registration and request, compute and storage budgets required.",
                ],
                next_decision="Freeze the dataset, control, challenger, holdout and budgets in the existing research owner before enqueueing.",
            )
            for key, title, hypothesis in (
                (
                    "equity-control",
                    "Equity baseline / control",
                    "Measure equal-weight US equities after the registered costs.",
                ),
                (
                    "equity-challenger",
                    "Preregistered equity challenger",
                    "Test one frozen challenger against the same control and protected holdout.",
                ),
            )
        ],
    ]


def build(config: dict[str, Any], now: datetime, source_release: str) -> Index:
    adapters: dict[str, Callable[[Path], list[Experiment]]] = {
        "challenge": challenge,
        "qsl": lambda p: qsl(p, now),
        "longrun": lambda p: [longrun(f, "longrun") for f in files(p, "*/digest.json")],
        "longrun_correction": lambda p: [longrun(p, "longrun_correction")],
        "campaigns": campaigns,
        "research": lambda p: research(p, now),
        "datasets": datasets,
        "forward": lambda p: forward(p, now),
        "allocations": allocations,
    }
    if set(config) != {"schema", "sources"} or config["schema"] != "trex-experiment-sources/1":
        raise Unavailable("source_configuration_invalid")
    if not isinstance(config["sources"], dict) or set(config["sources"]) - set(adapters):
        raise Unavailable("source_configuration_invalid")
    rows: list[Experiment] = []
    sources = []
    for key, adapter in adapters.items():
        raw = config["sources"].get(key)
        try:
            if not isinstance(raw, str) or not Path(raw).is_absolute():
                raise Unavailable("source_not_configured")
            result = adapter(Path(raw))
            rows.extend(result)
            sources.append(Source(id=key, status="available", count=len(result)))
        except Exception as exc:
            # Per-source failure never blanks other studies or exposes paths,
            # SQL internals, provider payloads or credentials to the browser.
            reason = (
                str(exc)
                if isinstance(exc, Unavailable)
                else (
                    "source_missing"
                    if isinstance(exc, FileNotFoundError)
                    else "source_integrity_or_schema_failed"
                )
            )
            sources.append(Source(id=key, status="unavailable", reason=reason))
    originals = [r for r in rows if r.source == "longrun" and r.artifacts[0].sha256 == ORIGINAL]
    corrections = [
        r for r in rows if r.source == "longrun_correction" and r.artifacts[0].sha256 == CORRECTED
    ]
    if originals and corrections:
        old, new = originals[0], corrections[0]
        old.status = "superseded_accounting"
        old.superseded_by = new.id
        new.supersedes = old.id
        new.status = "posthoc_correction_incomplete"
        new.limitations.append(
            "Corrected accounting supersedes the recorded original; amended rules remain post-hoc."
        )
    else:
        for row in rows:
            if row.source in {"longrun", "longrun_correction"}:
                row.blockers.append(
                    "Accounting lineage not reconciled: both pinned original and correction are required."
                )
    return Index(
        published_at=now.astimezone(UTC).isoformat(),
        source_release=source_release,
        sources=sources,
        experiments=rows,
        pilot=pilot(),
    )

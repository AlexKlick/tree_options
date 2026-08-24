"""Protocol 0.2.1 amendment BUILDER — dry-run only (PR A3).

This module is structurally incapable of landing anything. It never chooses
a threshold value, never writes a tracked file, and every packet it emits
says ``landed: false``. It consumes three inputs — a coverage-era census
artifact, OWNER-SUPPLIED values, and OWNER-RATIFIED derivation rules — and
emits a proposal under ``artifacts/`` only, for the owner to read.

Pipeline (first failure wins; every failure is a refusal, never a landing):

1.  the census parses and passes its fail-closed verification (content hash
    recomputed, taxonomy intact);
2.  the census still describes the capture manifest ON DISK NOW (staleness
    double-check against ``provenance.input_manifest_sha256``);
3.  the owner-values doc parses strictly (NaN/Infinity refused, bools never
    pass as ints) and binds itself to the census content hash;
4.  every ratified rule binds itself to the same census content hash;
5.  the base protocol loads through the real loader and is exactly 0.2.0,
    and the target is exactly its patch+1 (0.2.1);
6.  every derived value recomputes exactly from facts the census classes
    ``observed_census_fact`` (anything else is future-derived and refused),
    and every owner deviation carries a recorded decision reference;
7.  ``flow_min_session_volume`` is present as a real int > 0 — a missing or
    zero threshold is exactly the silent default this builder exists to
    prevent;
8.  the output root resolves under the repo's ``artifacts/`` directory;
9.  the packet is emitted under ``<out-root>/<census-hash[:12]>/``, and the
    proposed protocol is re-loaded through TODAY'S loader as proof it
    round-trips.

Output is byte-identical across re-runs over identical inputs: no clock, no
timestamps, no absolute paths in any emitted byte.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal, NoReturn

import yaml
from pydantic import Field, StrictInt, field_validator, model_validator

from tree_options.data.coverage_census import (
    CoverageCensus,
    census_content_sha256,
    verify_census,
)
from tree_options.protocol.loader import load_protocol, protocol_hash
from tree_options.protocol.schema import ResearchProtocol
from tree_options.schemas.common import StrictModel

OWNER_VALUES_SCHEMA_VERSION = "m4-owner-values/1"
AMENDMENT_PACKET_SCHEMA_VERSION = "m4-amendment-packet/1"
BASE_PROTOCOL_VERSION = "0.2.0"
PROPOSED_PROTOCOL_VERSION = "0.2.1"
FLOW_MIN_SESSION_VOLUME_ID = "flow_min_session_volume"
# Determinism: the amendment date is a pending marker, never a clock read.
AMENDMENT_DATE_PENDING = "PENDING-OWNER-RATIFICATION"


# ---- errors ------------------------------------------------------------------------


class AmendmentError(Exception):
    """Base: the builder refused. Every failure is a refusal, never a landing."""


class StaleCensusError(AmendmentError):
    """The census is invalid, tampered, or no longer describes the manifest."""


class OwnerValuesError(AmendmentError):
    """The owner-values or ratified-rules input is invalid or unbound."""


class VersionError(AmendmentError):
    """The base protocol is not 0.2.0, or the target is not its patch+1."""


class DerivationMismatchError(AmendmentError):
    """A supplied value disagrees with its rule, or derives from the future."""


class OutputRefusedError(AmendmentError):
    """The requested output root is outside artifacts/ (tracked-file protection)."""


# ---- owner-supplied values ---------------------------------------------------------


class OwnerValue(StrictModel):
    """One owner-supplied policy value: a number plus WHY it is that number.

    Either a derivation from census-observed facts under a ratified rule, or
    a recorded owner deviation. The value is a STRICT int: a bool is not a
    threshold, and neither is a float or a string.
    """

    id: str = Field(min_length=1)
    value: int
    provenance: Literal["derivation", "owner_deviation"]
    rule_id: str | None = None
    deviation_record: str | None = None

    @field_validator("value", mode="before")
    @classmethod
    def _value_is_strict_int(cls, v: object) -> int:
        if isinstance(v, bool):
            raise ValueError(f"OwnerValue.value: bool {v!r} is not a threshold value")
        if not isinstance(v, int):
            raise ValueError(f"OwnerValue.value must be a strict int, got {type(v).__name__}")
        return v

    @model_validator(mode="after")
    def _provenance_carries_its_evidence(self) -> OwnerValue:
        if self.provenance == "derivation":
            if self.rule_id is None:
                raise ValueError("provenance=derivation requires rule_id")
            if self.deviation_record is not None:
                raise ValueError("provenance=derivation must not carry deviation_record")
        else:
            if self.deviation_record is None:
                raise ValueError("provenance=owner_deviation requires deviation_record")
            if self.rule_id is not None:
                raise ValueError("provenance=owner_deviation must not carry rule_id")
        return self


class OwnerValuesDoc(StrictModel):
    """The owner's value sheet, bound to exactly one census by content hash."""

    census_content_sha256: str = Field(min_length=1)
    values: tuple[OwnerValue, ...] = Field(min_length=1)

    @field_validator("values")
    @classmethod
    def _ids_unique(cls, v: tuple[OwnerValue, ...]) -> tuple[OwnerValue, ...]:
        ids = [ov.id for ov in v]
        if len(set(ids)) != len(ids):
            duplicated = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"duplicate owner value ids: {duplicated}")
        return v


# ---- ratified derivation rules -----------------------------------------------------


class FactRef(StrictModel):
    """A reference to one census fact id."""

    fact: str = Field(min_length=1)


class OpNode(StrictModel):
    """One whitelisted combinator node. Integer arithmetic only."""

    op: Literal["max", "min", "floor_div", "mul"]
    args: tuple[Expr, ...] = Field(min_length=2)


Expr = StrictInt | FactRef | OpNode
OpNode.model_rebuild()


class DerivationRule(StrictModel):
    """An owner-ratified derivation: an expression bound to one census."""

    rule_id: str = Field(min_length=1)
    census_binding: str = Field(min_length=1)
    expression: Expr


class RatifiedRulesDoc(StrictModel):
    rules: tuple[DerivationRule, ...] = Field(min_length=1)

    @field_validator("rules")
    @classmethod
    def _rule_ids_unique(cls, v: tuple[DerivationRule, ...]) -> tuple[DerivationRule, ...]:
        ids = [r.rule_id for r in v]
        if len(set(ids)) != len(ids):
            duplicated = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"duplicate rule ids: {duplicated}")
        return v


def referenced_facts(node: Expr) -> tuple[str, ...]:
    """Every fact id an expression can reach, in evaluation order."""
    if isinstance(node, FactRef):
        return (node.fact,)
    if isinstance(node, OpNode):
        collected: list[str] = []
        for arg in node.args:
            collected.extend(referenced_facts(arg))
        return tuple(collected)
    return ()


def evaluate_expression(node: Expr, facts: dict[str, int]) -> int:
    """Walk one expression node. Integer arithmetic only."""
    if isinstance(node, int):
        return node
    if isinstance(node, FactRef):
        try:
            return facts[node.fact]
        except KeyError as exc:
            raise AmendmentError(f"derivation references unknown fact {node.fact!r}") from exc
    values = (evaluate_expression(arg, facts) for arg in node.args)
    if node.op == "max":
        return max(values)
    if node.op == "min":
        return min(values)
    if node.op == "mul":
        product = 1
        for v in values:
            product *= v
        return product
    # floor_div: left fold over the arguments
    quotient: int | None = None
    for v in values:
        quotient = v if quotient is None else quotient // v
    if quotient is None:  # pragma: no cover - min_length=2 keeps this unreachable
        raise AmendmentError("floor_div needs at least two arguments")
    return quotient


def evaluate(rule: DerivationRule, facts: dict[str, int]) -> int:
    """Evaluate a rule's expression over int-valued facts."""
    try:
        return evaluate_expression(rule.expression, facts)
    except ZeroDivisionError as exc:
        raise OwnerValuesError(f"rule {rule.rule_id!r} divides by zero") from exc


# ---- emitted packet ----------------------------------------------------------------


class AmendmentInputs(StrictModel):
    """Raw-byte SHA-256 of every input the builder consumed."""

    census_file_sha256: str
    owner_values_file_sha256: str
    rules_file_sha256: str
    protocol_file_sha256: str
    capture_manifest_file_sha256: str


class EmittedArtifact(StrictModel):
    name: str
    sha256: str


class AmendmentPacket(StrictModel):
    """The typed record of one dry-run build. ``landed`` is pinned false by
    the type itself: no value of this model can claim to have landed."""

    schema_version: str
    base_version: str
    proposed_version: str
    census_content_sha256: str
    protocol_hash_base: str
    flow_min_session_volume: int
    owner_values_schema_version: str
    inputs: AmendmentInputs
    emitted: tuple[EmittedArtifact, ...]
    landed: Literal[False] = False


# ---- build -------------------------------------------------------------------------


def _repo_root() -> Path:
    # src/tree_options/protocol/amendment.py -> repo root is parents[3].
    return Path(__file__).resolve().parents[3]


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"non-finite JSON constant {name!r} refused")


def _load_json_strict(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)


def _patch_plus_one(version: str) -> str:
    major, minor, patch = (int(part) for part in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def _flow_source_note(flow: OwnerValue) -> str:
    if flow.provenance == "derivation":
        return f"derivation rule {flow.rule_id} over observed census facts"
    return f"owner deviation {flow.deviation_record}"


def _render_schema_addition_proposal(
    *, census: CoverageCensus, census_hash: str, flow_value: int
) -> str:
    header = "\n".join(
        [
            "# NOT LANDED - PROPOSAL ONLY.",
            "#",
            '# src/tree_options/protocol/schema.py models are extra="forbid":',
            "# adding a final-holdout-window field to the protocol schema is a",
            "# schema change this PR must not make. This file records the",
            "# proposal for a later, owner-ratified PR; nothing here is",
            "# loadable protocol content.",
        ]
    )
    doc = {
        "NOT_LANDED": True,
        "proposed_schema_addition": {
            "field": "final_holdout_window",
            "target": "protocol holdout declaration (exact location to be ratified by the owner)",
            "rationale": (
                "the G3 packet Ask E declares the holdout only after real"
                " coverage inspection; the proposal must cite this census's"
                " observed grid"
            ),
            "census_content_sha256": census_hash,
            "expected_masters": census.coverage.expected_masters,
            "base_version": BASE_PROTOCOL_VERSION,
            "proposed_version": PROPOSED_PROTOCOL_VERSION,
            "flow_min_session_volume": flow_value,
            "emitted_by": "scripts/build_protocol_amendment.py (dry-run)",
        },
    }
    return header + "\n" + yaml.safe_dump(doc, sort_keys=True, default_flow_style=False)


def _render_diff(
    *,
    base: ResearchProtocol,
    census_hash: str,
    flow_value: int,
    flow_source: str,
) -> str:
    return (
        f"# Amendment diff (PROPOSED — NOT LANDED)\n"
        "\n"
        f"Base: research protocol {base.meta.protocol_version}, loaded and verified through\n"
        "the real loader. Proposed: "
        f"{PROPOSED_PROTOCOL_VERSION}, emitted by scripts/build_protocol_amendment.py.\n"
        "\n"
        f'- meta.protocol_version: "{base.meta.protocol_version}" -> "{PROPOSED_PROTOCOL_VERSION}"\n'
        f"- meta.amendments: {len(base.meta.amendments)} -> {len(base.meta.amendments) + 1} records\n"
        "  (the new record's date is the pending marker "
        f"{AMENDMENT_DATE_PENDING!r}, never a clock read)\n"
        "- option_candidate_defaults.liquidity_volume_flow.flow_min_session_volume:\n"
        f"  null -> {flow_value}\n"
        f"  provenance: {flow_source}\n"
        "- invariants: UNCHANGED (INV-01..INV-14, statements untouched)\n"
        "- schema additions: NONE in this packet. final_holdout_window is proposed\n"
        "  in schema-addition-proposal.yaml under NOT_LANDED: true; protocol models\n"
        '  are extra="forbid" and this PR must not touch them.\n'
        "\n"
        f"Census binding: {census_hash}\n"
        "\n"
        "This file is a proposal. Nothing is landed: research_protocol.yaml is not\n"
        "modified; landed:false in amendment-packet.json is the machine-readable pin.\n"
    )


def build_proposed_amendment(
    census_path: Path,
    owner_values_path: Path,
    rules_path: Path,
    *,
    protocol_path: Path,
    capture_manifest_path: Path,
    out_root: Path,
) -> AmendmentPacket:
    """Build the 0.2.1 proposal packet. Returns the packet (landed: false)."""
    # 1. census: parse + fail-closed verification (recomputed content hash)
    try:
        census = CoverageCensus.model_validate_json(census_path.read_text(encoding="utf-8"))
        verify_census(census)
    except ValueError as exc:
        raise StaleCensusError(f"census invalid or tampered: {exc}") from exc

    # 2. staleness double-check: the census must describe the manifest on disk NOW
    manifest_bytes = capture_manifest_path.read_bytes()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if manifest_sha256 != census.provenance.input_manifest_sha256:
        raise StaleCensusError(
            "capture manifest drifted since the census: census bound "
            f"{census.provenance.input_manifest_sha256[:12]}…, on disk now "
            f"{manifest_sha256[:12]}…"
        )

    census_hash = census_content_sha256(census)

    # 3. owner values: strict JSON, model validation, census binding
    try:
        owner_doc = OwnerValuesDoc.model_validate(_load_json_strict(owner_values_path))
    except ValueError as exc:
        raise OwnerValuesError(f"owner values doc invalid: {exc}") from exc
    if owner_doc.census_content_sha256 != census_hash:
        raise OwnerValuesError(
            "owner values doc is not bound to this census: doc says "
            f"{owner_doc.census_content_sha256[:12]}…, census is {census_hash[:12]}…"
        )

    # 4. ratified rules: strict JSON, every rule bound to this census
    try:
        rules_doc = RatifiedRulesDoc.model_validate(_load_json_strict(rules_path))
    except ValueError as exc:
        raise OwnerValuesError(f"ratified rules doc invalid: {exc}") from exc
    for rule in rules_doc.rules:
        if rule.census_binding != census_hash:
            raise OwnerValuesError(
                f"rule {rule.rule_id!r} is not bound to this census: binding "
                f"{rule.census_binding[:12]}…, census {census_hash[:12]}…"
            )

    # 5. base protocol through the real loader; target = its patch+1
    try:
        base = load_protocol(protocol_path)
    except ValueError as exc:
        raise VersionError(
            f"base protocol does not load as {BASE_PROTOCOL_VERSION}: {exc}"
        ) from exc
    base_version = base.meta.protocol_version

    # 5b. Round-1 review fix (2026-08-23, probe NOT_EVALUABLE_FACT_DERIVED +
    # INCOMPLETE_CENSUS_VERIFIED): amendment MUST refuse to derive from an
    # incomplete census (any pair in INCOMPLETE_CLASSES) or from a fact the
    # census classes NOT_EVALUABLE / PARTIAL. The previous code admitted
    # every observed_census_fact int and could `max(bar_volume_observations,
    # 1)` over a zero-marked-NOT_EVALUABLE fact, papering over the exact
    # bar-volume contradiction the runbook says must become an owner
    # deviation. Two new gates below:
    #   a) coverage must be COMPLETE (zero INCOMPLETE_CLASSES pairs)
    #   b) every observed fact used as an operand must have
    #      confidence == "EXACT" (PARTIAL or NOT_EVALUABLE is refused)
    from tree_options.data.coverage_census import INCOMPLETE_CLASSES

    incomplete = sum(getattr(census.coverage.observed, cls) for cls in INCOMPLETE_CLASSES)
    if incomplete > 0:
        raise StaleCensusError(
            f"census is coverage-INCOMPLETE: {incomplete} pair(s) in "
            f"INCOMPLETE_CLASSES ({sorted(INCOMPLETE_CLASSES)}); an "
            "amendment may only be built against a whole census"
        )
    if base_version != BASE_PROTOCOL_VERSION:
        raise VersionError(
            f"base protocol version must be exactly {BASE_PROTOCOL_VERSION!r}, got {base_version!r}"
        )
    target = _patch_plus_one(base_version)
    if target != PROPOSED_PROTOCOL_VERSION:
        raise VersionError(
            f"non-monotonic target: patch+1 of {base_version} is {target!r}, "
            f"expected {PROPOSED_PROTOCOL_VERSION!r}"
        )

    # 6. derivation: observed facts only; computed must equal supplied
    rules_by_id = {r.rule_id: r for r in rules_doc.rules}
    facts: dict[str, int] = {}
    for fact_id, fact in census.values.observed_census_fact.items():
        if type(fact.v) is int:
            # Round-1 review fix: refuse NOT_EVALUABLE / PARTIAL facts as
            # derivation operands. Confidence is part of the census content
            # (the fact's hash binds it); admit only EXACT.
            if fact.confidence != "EXACT":
                raise DerivationMismatchError(
                    f"observed fact {fact_id!r} has confidence "
                    f"{fact.confidence!r}; only EXACT may be used as a "
                    "derivation operand (census MUST be repaired or the "
                    "value MUST be a non-derivation owner_deviation)"
                )
            # exact int only: bools are not facts, textual observations are not numeric
            facts[fact_id] = fact.v
    for ov in owner_doc.values:
        if ov.provenance == "derivation":
            if ov.rule_id is None:  # the OwnerValue contract already refuses this
                raise OwnerValuesError(f"value {ov.id!r} derivation carries no rule_id")
            ov_rule = rules_by_id.get(ov.rule_id)
            if ov_rule is None:
                raise OwnerValuesError(f"value {ov.id!r} cites unknown rule {ov.rule_id!r}")
            for fid in referenced_facts(ov_rule.expression):
                if census.value_registry.get(fid) != "observed_census_fact":
                    declared = census.value_registry.get(fid, "<not in registry>")
                    raise DerivationMismatchError(
                        f"value {ov.id!r} rule {ov_rule.rule_id!r} references fact "
                        f"{fid!r} classed {declared!r}: only observed_census_fact "
                        "facts exist yet (future-derived)"
                    )
            computed = evaluate(ov_rule, facts)
            if computed != ov.value:
                raise DerivationMismatchError(
                    f"value {ov.id!r}: owner supplied {ov.value}, rule "
                    f"{ov_rule.rule_id!r} computes {computed}"
                )
        elif not ov.deviation_record:
            raise OwnerValuesError(f"value {ov.id!r} owner_deviation has an empty deviation_record")

    # 7. hidden-default refusal: the flow threshold must be a real positive int
    flow = next((ov for ov in owner_doc.values if ov.id == FLOW_MIN_SESSION_VOLUME_ID), None)
    if flow is None or flow.value <= 0:
        raise OwnerValuesError(
            f"{FLOW_MIN_SESSION_VOLUME_ID} must be supplied by the owner as a "
            "real int > 0; a missing or zero threshold is exactly the silent "
            "default this builder exists to prevent"
        )
    flow_value = flow.value
    flow_source = _flow_source_note(flow)

    # 8. tracked-file write protection: proposals live under artifacts/ only
    artifacts_root = (_repo_root() / "artifacts").resolve()
    resolved_out_root = out_root.expanduser().resolve()
    if not resolved_out_root.is_relative_to(artifacts_root):
        raise OutputRefusedError(
            f"output root {out_root} resolves outside {artifacts_root}: the "
            "builder writes proposals under artifacts/ only"
        )

    # 9. emit under <out-root>/<census-hash[:12]>/
    out_dir = resolved_out_root / census_hash[:12]
    out_dir.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = base.model_dump(mode="json")
    data["meta"]["protocol_version"] = PROPOSED_PROTOCOL_VERSION
    amendments = list(data["meta"]["amendments"])
    amendments.append(
        {
            "version": PROPOSED_PROTOCOL_VERSION,
            "date": AMENDMENT_DATE_PENDING,
            "decision": (
                f"coverage-era amendment PROPOSAL built from census "
                f"{census_hash[:12]} (dry-run: not landed, owner GO required)"
            ),
            "changes": (
                f"set liquidity_volume_flow.flow_min_session_volume = "
                f"{flow_value} ({flow_source}); final_holdout_window schema "
                "addition proposed separately (NOT_LANDED) because protocol "
                'models are extra="forbid"'
            ),
        }
    )
    data["meta"]["amendments"] = amendments
    data["option_candidate_defaults"]["liquidity_volume_flow"]["flow_min_session_volume"] = (
        flow_value
    )

    proposed_path = out_dir / f"protocol-{PROPOSED_PROTOCOL_VERSION}-proposed.yaml"
    proposed_path.write_text(
        yaml.safe_dump(data, sort_keys=False, default_flow_style=False, width=1000),
        encoding="utf-8",
    )

    # PROOF STEP: the proposal must load through TODAY'S real loader
    try:
        parsed = load_protocol(proposed_path)
    except ValueError as exc:
        raise AmendmentError(
            f"proposed protocol does not load through the current schema: {exc}"
        ) from exc
    parsed_flow = parsed.option_candidate_defaults.liquidity_volume_flow
    if (
        parsed.meta.protocol_version != PROPOSED_PROTOCOL_VERSION
        or parsed_flow is None
        or parsed_flow.flow_min_session_volume != flow_value
        or len(parsed.meta.amendments) != len(base.meta.amendments) + 1
    ):
        raise AmendmentError("proposed protocol round-trip lost the amendment content")

    schema_path = out_dir / "schema-addition-proposal.yaml"
    schema_path.write_text(
        _render_schema_addition_proposal(
            census=census, census_hash=census_hash, flow_value=flow_value
        ),
        encoding="utf-8",
    )

    diff_path = out_dir / "amendment-diff.md"
    diff_path.write_text(
        _render_diff(
            base=base, census_hash=census_hash, flow_value=flow_value, flow_source=flow_source
        ),
        encoding="utf-8",
    )

    packet = AmendmentPacket(
        schema_version=AMENDMENT_PACKET_SCHEMA_VERSION,
        base_version=base_version,
        proposed_version=PROPOSED_PROTOCOL_VERSION,
        census_content_sha256=census_hash,
        protocol_hash_base=protocol_hash(base),
        flow_min_session_volume=flow_value,
        owner_values_schema_version=OWNER_VALUES_SCHEMA_VERSION,
        inputs=AmendmentInputs(
            census_file_sha256=hashlib.sha256(census_path.read_bytes()).hexdigest(),
            owner_values_file_sha256=hashlib.sha256(owner_values_path.read_bytes()).hexdigest(),
            rules_file_sha256=hashlib.sha256(rules_path.read_bytes()).hexdigest(),
            protocol_file_sha256=hashlib.sha256(protocol_path.read_bytes()).hexdigest(),
            capture_manifest_file_sha256=manifest_sha256,
        ),
        emitted=tuple(
            EmittedArtifact(name=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            for path in sorted((proposed_path, schema_path, diff_path), key=lambda p: p.name)
        ),
    )
    packet_path = out_dir / "amendment-packet.json"
    packet_path.write_text(
        json.dumps(json.loads(packet.model_dump_json()), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return packet

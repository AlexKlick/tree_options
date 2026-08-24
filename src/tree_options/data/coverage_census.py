"""Coverage-era census artifact (PR A/A2): observed facts, never policy.

The census is the bridge between a COMPLETED capture era and the protocol
0.2.1 amendment. It reports what the era actually captured — nothing more.
Two rules are structural:

1. **No policy value is ever chosen here.** The four-class taxonomy is
   enforced by construction: every fact id appears in exactly one of
   `observed_census_fact` / `predeclared_derivation_input` /
   `owner_ratified_policy_value` / `not_yet_decided`, and the
   `value_registry` must agree with that placement. The amendment builder
   refuses to derive from anything not classed `observed_census_fact`.
2. **The derivation source contradiction is recorded, not papered over.**
   The G3 packet derives `flow_min_session_volume` from "era bar-volume
   distributions", but the coverage era ran `--bars 0` — no bar exists
   until the bars era, which the declared sequence orders AFTER 0.2.1.
   The census emits bar-volume facts as `NOT_EVALUABLE` with that
   contradiction verbatim and leaves the derivation slot
   `AWAITING_OWNER_RULE` (owner decision 2026-08-23).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from tree_options.data.digest import canonical_bytes, sha256_hex
from tree_options.schemas.common import StrictModel

UNIVERSE_SCHEMA_VERSION = "m4-coverage-universe/1"
UNIVERSE_DOMAIN = b"tree-options-m4-coverage-universe-v1"

CENSUS_SCHEMA_VERSION = "m4-coverage-census/1"
CENSUS_DOMAIN = b"tree-options-m4-coverage-census-v1"

ValueClass = Literal[
    "observed_census_fact",
    "predeclared_derivation_input",
    "owner_ratified_policy_value",
    "not_yet_decided",
]

VALUE_CLASSES: tuple[ValueClass, ...] = (
    "observed_census_fact",
    "predeclared_derivation_input",
    "owner_ratified_policy_value",
    "not_yet_decided",
)

G3_DERIVATION_CONTRADICTION = (
    "G3 packet Ask D derives flow_min_session_volume from 'era bar-volume"
    " distributions', but the coverage era captured --bars 0: no option bar"
    " exists until the ATM-grid bars era, which the declared sequence orders"
    " AFTER protocol 0.2.1. No derivation rule is repo-declared; the rule is"
    " an owner-ratified input bound to this census's content hash"
    " (owner decision 2026-08-23, to be resolved at era-results)."
)


class CensusTaxonomyError(ValueError):
    """A fact id moved class implicitly, or the registry disagrees."""


# ---- universe manifest ------------------------------------------------------------


class CoverageUniverse(StrictModel):
    """Declared universe x Friday grid, generated from the era wrapper ONCE.

    Per owner decision 2026-08-23 the census counts expected masters from
    THIS manifest (29 x 105 = 3,045), never from the docs' 30/3,150 —
    the discrepancy is flagged for owner reconciliation at era-results.
    """

    schema_version: str
    source: str
    source_sha256: str
    underlyings: tuple[str, ...]
    as_of_fridays: tuple[str, ...]
    expected_masters: int = Field(ge=1)
    notes: tuple[str, ...] = ()
    content_sha256: str


def universe_content_sha256(universe: CoverageUniverse) -> str:
    core = universe.model_copy(update={"content_sha256": ""})
    return sha256_hex(UNIVERSE_DOMAIN + canonical_bytes(core))


def verify_universe(universe: CoverageUniverse) -> None:
    """Refuse a tampered or self-inconsistent universe manifest."""
    # Round-3 review fix (2026-08-23, finding 5): a content hash binds
    # CONTENT, not COMPATIBILITY — a correctly-rehashed artifact from a
    # foreign schema version passed every hash check. Pin the version.
    if universe.schema_version != UNIVERSE_SCHEMA_VERSION:
        raise CensusTaxonomyError(
            f"universe schema_version {universe.schema_version!r} != "
            f"{UNIVERSE_SCHEMA_VERSION!r}: this census era consumes exactly that version"
        )
    actual = universe_content_sha256(universe)
    if universe.content_sha256 != actual:
        raise CensusTaxonomyError(
            "universe manifest content hash mismatch: "
            f"declared {universe.content_sha256[:12]}…, computed {actual[:12]}…"
        )
    if len(set(universe.underlyings)) != len(universe.underlyings):
        raise CensusTaxonomyError("universe underlyings contain duplicates")
    if len(set(universe.as_of_fridays)) != len(universe.as_of_fridays):
        raise CensusTaxonomyError("universe as_of fridays contain duplicates")
    expected = len(universe.underlyings) * len(universe.as_of_fridays)
    if universe.expected_masters != expected:
        raise CensusTaxonomyError(
            f"expected_masters {universe.expected_masters} != "
            f"{len(universe.underlyings)} x {len(universe.as_of_fridays)} = {expected}"
        )


# ---- census artifact ---------------------------------------------------------------


# Round-3 review fix (2026-08-23, finding 1): StrictModel is frozen +
# extra-forbid but NOT strict, so pydantic lax mode coerced `true` -> int 1
# and `1.0` -> int 1 at parse time — the amendment builder's
# `type(observed.v) is int` derivation gates then saw an already-coerced
# int and could not detect the boolean/float origin. The int branch is
# strict so bools/floats are REFUSED at this parse boundary; textual
# observations still parse through the str branch.
StrictObservedValue = Annotated[int, Field(strict=True)]


class CensusFact(StrictModel):
    v: StrictObservedValue | str
    support: dict[str, int] = Field(default_factory=dict)
    confidence: Literal["EXACT", "PARTIAL", "NOT_EVALUABLE"]


class CensusProvenance(StrictModel):
    code_sha: str
    protocol_hash: str
    protocol_raw_sha256: str
    input_manifest_sha256: str
    universe_manifest_sha256: str
    uv_lock_sha256: str
    command: tuple[str, ...]
    # Round-4 review fix (2026-08-23, finding 3): report_version is REQUIRED.
    # A default equal to the current token let a census whose provenance
    # LACKED the field parse with the token re-inserted, so canonical hashing
    # reproduced the original content_sha256 and verify_census accepted an
    # artifact that never declared its report version. Audited the other
    # fields: none carries a default, so none can smuggle an undeclared
    # value into the hash; this was the only latent one.
    report_version: str


class PairCoverage(StrictModel):
    COMPLETE: int = 0
    TRUNCATED: int = 0
    ERROR: int = 0
    MISSING: int = 0
    SPOT_MISSING_SESSION: int = 0
    SPOT_MISSING_HOLIDAY: int = 0


class PairFinding(StrictModel):
    underlying: str
    as_of: str
    classification: str
    detail: str = ""


class CoverageBlock(StrictModel):
    expected_masters: int
    observed: PairCoverage
    findings: tuple[PairFinding, ...] = ()
    holiday_fridays: tuple[str, ...] = ()
    session_spot_gaps: tuple[PairFinding, ...] = ()


class CensusValues(StrictModel):
    observed_census_fact: dict[str, CensusFact]
    predeclared_derivation_input: dict[str, CensusFact | str]
    owner_ratified_policy_value: dict[str, CensusFact | str] = Field(default_factory=dict)
    not_yet_decided: dict[str, str]


class CoverageCensus(StrictModel):
    schema_version: str
    provenance: CensusProvenance
    coverage: CoverageBlock
    values: CensusValues
    value_registry: dict[str, ValueClass]
    content_sha256: str


def census_content_sha256(census: CoverageCensus) -> str:
    core = census.model_copy(update={"content_sha256": ""})
    return sha256_hex(CENSUS_DOMAIN + canonical_bytes(core))


def validate_value_taxonomy(census: CoverageCensus) -> None:
    """Every fact id sits in EXACTLY ONE class section, and the registry
    agrees. No value may move class implicitly."""
    sections = {
        "observed_census_fact": set(census.values.observed_census_fact),
        "predeclared_derivation_input": set(census.values.predeclared_derivation_input),
        "owner_ratified_policy_value": set(census.values.owner_ratified_policy_value),
        "not_yet_decided": set(census.values.not_yet_decided),
    }
    seen: set[str] = set()
    for _name, ids in sections.items():
        overlap = seen & ids
        if overlap:
            raise CensusTaxonomyError(f"fact ids in more than one value class: {sorted(overlap)}")
        seen |= ids
    registry = census.value_registry
    if set(registry) != seen:
        raise CensusTaxonomyError(
            "value_registry does not match the class sections exactly: "
            f"registry-only {sorted(set(registry) - seen)}, "
            f"section-only {sorted(seen - set(registry))}"
        )
    for fact_id, declared_class in registry.items():
        placed = next(n for n, ids in sections.items() if fact_id in ids)
        if placed != declared_class:
            raise CensusTaxonomyError(
                f"fact {fact_id!r} registry says {declared_class!r} but it sits in {placed!r}"
            )


def verify_census(census: CoverageCensus) -> None:
    """Fail-closed load-time verification: hash binding + taxonomy."""
    # Round-3 review fix (2026-08-23, finding 5): same reasoning as
    # verify_universe — pin the artifact and report version tokens; a
    # rehashed foreign-era census must refuse on its face, not parse.
    if census.schema_version != CENSUS_SCHEMA_VERSION:
        raise CensusTaxonomyError(
            f"census schema_version {census.schema_version!r} != "
            f"{CENSUS_SCHEMA_VERSION!r}: refusing a foreign-era census"
        )
    if census.provenance.report_version != CENSUS_SCHEMA_VERSION:
        raise CensusTaxonomyError(
            f"census report_version {census.provenance.report_version!r} != "
            f"{CENSUS_SCHEMA_VERSION!r}: refusing a foreign-era report"
        )
    actual = census_content_sha256(census)
    if census.content_sha256 != actual:
        raise CensusTaxonomyError(
            f"census content hash mismatch: declared {census.content_sha256[:12]}…,"
            f" computed {actual[:12]}…"
        )
    validate_value_taxonomy(census)
    if census.values.owner_ratified_policy_value:
        raise CensusTaxonomyError(
            "owner_ratified_policy_value must be EMPTY in a PR-A-era census:"
            " no policy value may appear ratified before the owner ratifies"
            " the 0.2.1 amendment"
        )


def classify_pair(
    *,
    underlying: str,
    as_of: str,
    entry_complete: bool | None,
    entry_truncated: bool,
    entry_error: str,
    has_file: bool,
    spot_present: bool,
    is_session: bool,
) -> str:
    """One (underlying, as_of) pair -> exactly one coverage class.

    `entry_complete is None` means the manifest has no entry for the pair
    (never requested / never attempted)."""
    if entry_complete is None or not has_file:
        return "MISSING"
    if entry_error:
        return "ERROR"
    if entry_truncated or not entry_complete:
        return "TRUNCATED"
    if not spot_present:
        # A holiday Friday has no close BY DEFINITION (the exchange was
        # closed); a session Friday without a close is a vendor availability
        # gap (see the 2026-08-21 reconciliation finding D3).
        return "SPOT_MISSING_HOLIDAY" if not is_session else "SPOT_MISSING_SESSION"
    return "COMPLETE"


INCOMPLETE_CLASSES = frozenset({"MISSING", "TRUNCATED", "ERROR", "SPOT_MISSING_SESSION"})

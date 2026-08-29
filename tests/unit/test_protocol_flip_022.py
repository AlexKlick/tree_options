"""The 0.2.2 FLIP itself (owner ruling m4-022-ruling-20260828, ratified
2026-08-28).

The lane-on CODE merged in PR #19 (ceb92ca); this is the landing of the
ratified amendment: research_protocol.yaml moved 0.2.1 -> 0.2.2 hand-applied
preserving comments (precedent: the 0.2.1 landing cdf38c8). The canonical
hash is over the VALIDATED MODEL, not raw bytes, so a comment-preserving hand
edit is correct as long as the model is right — which is exactly what this
module pins:

- the landed model is IDENTICAL to the builder packet's proposed model
  (the packet emission under artifacts/amendment/022-declaration/
  5caf56568941/, read as a raw-sha-pinned fixture copy and loaded through
  the real loader) except the last amendment record's ``date`` and
  ``decision`` (the ratification record; the packet's PENDING markers
  bound the PROPOSAL, not the landed protocol);
- the landed identity is pinned absolutely (LANDED_PROTOCOL_SHA256_022);
- the pre-flip yaml is snapshotted byte-exact as the 0.2.1 fixture and
  still hashes to the ledger-bound 0.2.1 identity every prior ledger
  stamped (cfafc884…) — proof the fixture IS the standing 0.2.1, not a
  reconstruction.
"""

from __future__ import annotations

from tests.conftest import REPO_ROOT

PROTOCOL_PATH = REPO_ROOT / "research_protocol.yaml"
FIXTURE_021 = REPO_ROOT / "tests" / "fixtures" / "protocol-0.2.1.yaml"
# The packet's proposed yaml is READ-ONLY provenance under artifacts/ (which
# the mutation harness's disposable copy excludes), so the model-identity
# proof reads a BYTE-IDENTICAL fixture copy pinned by the raw sha256 the
# packet's amendment-packet.json records for the emission — and re-ties to
# the provenance file itself whenever it is present on the host.
PACKET_DIR = REPO_ROOT / "artifacts" / "amendment" / "022-declaration" / "5caf56568941"
PACKET_PROPOSED_022 = PACKET_DIR / "protocol-0.2.2-proposed.yaml"
PROPOSED_022 = REPO_ROOT / "tests" / "fixtures" / "protocol-0.2.2-proposed.yaml"
# the emission's sha256 exactly as recorded in the packet's
# amendment-packet.json (emitted[0] for protocol-0.2.2-proposed.yaml)
PACKET_PROPOSED_022_RAW_SHA256 = "5f4bef6eabb80f8c2f76d4a465854be3057ae2ddc4bbcfd500f4ea3bfda4ab5e"

# The pre-flip ledger-bound 0.2.1 identity (bars-authority binding + every
# cross-branch trial identity; the absolute pin TestProtocolIdentityPin held
# until the flip).
LEDGER_BOUND_PROTOCOL_SHA256_021 = (
    "cfafc884d9c45d805f6d6028d6991daf9e2e1751d91823306d780506bbaffeb7"
)

# The builder packet's PROJECTION (5caf56568941fa1fca4c59da65db327f07f3ef0
# 94fb7bbfa6c60309b5fd31f8c): the identity the packet computed over its own
# PROPOSED yaml, whose amendment record carried the pending markers
# (date PENDING-OWNER-RATIFICATION, decision "…PROPOSAL — …dry-run: not
# landed…"). The landed protocol records the RATIFICATION in exactly those
# two fields, so its identity necessarily differs from the projection.
PACKET_PROJECTED_SHA256_022 = "5caf56568941fa1fca4c59da65db327f07f3ef094fb7bbfa6c60309b5fd31f8c"

# The LANDED 0.2.2 identity: computed by execution on this branch after the
# hand-applied flip, verified model-identical to the packet modulo the
# ratification record (see test_landed_model_is_the_packets_model_except_the
# _ratification_record). This is the absolute pin from the flip forward.
LANDED_PROTOCOL_SHA256_022 = "22c782313865fadd37fd18a3ff95ac449dc4e9740102f54f828219c354614521"


def test_the_live_protocol_is_022_and_carries_the_ruled_declarations() -> None:
    from tree_options.protocol.loader import load_protocol

    landed = load_protocol(PROTOCOL_PATH)
    assert landed.meta.protocol_version == "0.2.2"
    lf = landed.option_candidate_defaults.liquidity_volume_flow
    assert lf is not None
    assert lf.underlying_liquidity_term == "evaluated"
    assert landed.option_candidate_defaults.earnings_evaluation == "disclosed_absence"
    assert landed.fills.fill_door_decision_close == "decision_grid"
    # 0.2.0 + 0.2.1 + 0.2.2 records
    assert len(landed.meta.amendments) == 3
    assert [a.version for a in landed.meta.amendments] == ["0.2.0", "0.2.1", "0.2.2"]


def test_landed_model_is_the_packets_model_except_the_ratification_record() -> None:
    """Field-level proof: walking the two model dumps, the ONLY differences
    are the last amendment record's ``date`` and ``decision`` — the
    ratification. Everything else (the changes text, all three declarations,
    every invariant) is equal, so the hand-applied flip landed exactly the
    packet's model. The proposed model is loaded from the fixture copy of
    the packet's emission (raw-sha-pinned to the packet's own record, and
    byte-compared against the provenance file when the host carries it)."""
    import hashlib

    from tree_options.protocol.loader import load_protocol

    raw = PROPOSED_022.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PACKET_PROPOSED_022_RAW_SHA256, (
        "the fixture copy must be the packet's emission byte-for-byte"
    )
    if PACKET_PROPOSED_022.exists():
        assert raw == PACKET_PROPOSED_022.read_bytes(), "drift against the provenance file"
    landed = load_protocol(PROTOCOL_PATH)
    proposed = load_protocol(PROPOSED_022)
    a = landed.model_dump(mode="json")
    b = proposed.model_dump(mode="json")
    # normalize ONLY the two ratification fields
    for d in (a, b):
        d["meta"]["amendments"][-1] = {
            **d["meta"]["amendments"][-1],
            "date": "<RATIFICATION>",
            "decision": "<RATIFICATION>",
        }
    assert a == b


def test_the_landed_identity_is_pinned_and_differs_from_the_packets_projection() -> None:
    """The absolute landed pin. It does NOT equal the packet's projected
    5caf5656… — that projection hashed the packet's own PROPOSED yaml whose
    record carried the pending markers; the landed protocol replaces exactly
    those two fields with the ratification (2026-08-28, the owner ruling
    date), so the identity moves with the record."""
    from tree_options.protocol.loader import load_protocol, protocol_hash

    landed_hash = protocol_hash(load_protocol(PROTOCOL_PATH))
    assert landed_hash == LANDED_PROTOCOL_SHA256_022
    assert landed_hash != PACKET_PROJECTED_SHA256_022
    assert landed_hash != LEDGER_BOUND_PROTOCOL_SHA256_021


def test_the_021_fixture_is_the_standing_protocol_every_prior_ledger_stamped() -> None:
    """Fix 3: the pre-flip yaml snapshotted byte-for-byte from git HEAD
    (git show HEAD:research_protocol.yaml, at merge ceb92ca) before the
    flip — byte-exact by the raw sha256 literal recorded at snapshot time,
    and loading it answers version 0.2.1. The CANONICAL-hash half of the
    fixture pin (== cfafc884…) lives in TestProtocolIdentityPin
    (test_protocol_loader.py): it is the mutation owner for the
    below-0.2.2 strip, so it stays in the selectors file."""
    import hashlib

    from tree_options.protocol.loader import load_protocol

    fixture = load_protocol(FIXTURE_021)
    assert fixture.meta.protocol_version == "0.2.1"
    # byte-exact: the raw sha256 of the pre-flip git blob, recorded at
    # snapshot time (the canonical hash pins the model; this literal pins
    # the BYTES the snapshot promised — no git-object dependency)
    assert (
        hashlib.sha256(FIXTURE_021.read_bytes()).hexdigest()
        == "838013660bb965b1c11c364c025e9637574d645e9e3f6e538bf2fa63f962a8b2"
    )

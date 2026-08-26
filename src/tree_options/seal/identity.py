"""G4 sealed-run identity: what the one-shot authority is spent AGAINST.

Two ids over the same identity tuple:

* ``sealed_run_id`` — pins the exact checkout (``code_sha`` included). Two
  checkouts of the same research content are DIFFERENT sealed runs.
* ``content_identity`` — the same payload with the checkout-bound
  ``code_sha`` and ``verified_packet_sha256`` BLANKED, under a different
  domain. Two checkouts of the same research content SHARE it, so a second
  consumption under EITHER id is refused: the seal is one-shot per sealed
  CONTENT, not per checkout (a fresh clone of the same head is not fresh
  authority).

Domain separation is load-bearing: an id from one domain can never collide
with (or be substituted for) an id from the other.
"""

from __future__ import annotations

from tree_options.data.digest import canonical_bytes, sha256_hex
from tree_options.schemas.common import StrictModel

SEALED_RUN_DOMAIN = b"tree-options-g4-seal-run-v1"
CONTENT_IDENTITY_DOMAIN = b"tree-options-g4-content-v1"

RUNNER_VERSION = "m4-g4-runner/1"


class SealedIdentity(StrictModel):
    """The full tuple a G4 sealed run is identified by.

    Every field is an input the sealed run requires; none has a default that
    could silently stand in for an undecided prerequisite (except
    ``runner_version``, which names THIS machinery, not research content).
    """

    code_sha: str
    protocol_hash: str
    lane1_manifest_sha256: str
    lane2_manifest_sha256: str
    calendar_decision_artifact_sha256: str
    runner_version: str = RUNNER_VERSION
    criteria_artifact_sha256: str
    verified_packet_sha256: str


def sealed_run_id(identity: SealedIdentity) -> str:
    """Identity of the exact sealed run (code_sha included)."""
    return sha256_hex(SEALED_RUN_DOMAIN + canonical_bytes(identity))


def content_identity(identity: SealedIdentity) -> str:
    """Identity of sealed CONTENT (checkout-bound fields blanked)."""
    # The packet hash itself includes code_sha. Blank both checkout-bound
    # fields so a fresh checkout of the same research content is not fresh
    # one-shot authority; all content-bearing raw hashes remain pinned.
    blanked = identity.model_copy(update={"code_sha": "", "verified_packet_sha256": ""})
    return sha256_hex(CONTENT_IDENTITY_DOMAIN + canonical_bytes(blanked))

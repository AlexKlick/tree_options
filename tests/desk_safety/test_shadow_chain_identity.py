"""WT4-a: the chain-identity check guards BOTH of a mark's chain reads.

Wave 3 extracted ``shadows._read_chain`` and now applies it twice per mark:
to the mark session's chain (``_read_mark``) and to the source session's
chain the entry fill is verified against (``_verified_entry``). Its identity
check (``chain_identity``: the header must name the session and underlying
the file is filed under) had no test — yet the canonical OCC filter's
fail-open branch (a document that names no underlying is served whole,
orphan junk rows included; ``test_shadows_occ_integrity``) is unreachable
from the shadow lane ONLY because of it. These tests make that true by test.

The source-side test pins "no mark", never the stored code: how an
entry-side failure is labelled is the subject of a separate change.
"""

from __future__ import annotations

import gzip
import json

import pytest

from tree_options.desk import shadows
from tree_options.desk.evidence import EvidenceStore
from tree_options.desk.sessions import cutoff_instant
from tree_options.desk.store import encode_document, standard_row_indexes


def _run(world, as_of):
    return shadows.update_shadows(
        session=as_of,
        now=cutoff_instant(as_of),
        cal=world.cal,
        database=world.db,
        store_root=world.store,
        queue_dir=world.queues,
    )


def _occ(root: str, world, right: str, strike: int) -> str:
    return f"{root}{world.expiry:%y%m%d}{right}{strike * 1000:08d}"


def _rename_underlying(path, mode: str) -> dict:
    """Rewrite a stored chain's header: drop its underlying, or name another."""
    doc = json.loads(gzip.decompress(path.read_bytes()))
    if mode == "absent":
        doc["header"].pop("underlying")
    else:
        doc["header"]["underlying"] = "MSFT"
    path.write_bytes(encode_document(doc))
    return doc


@pytest.mark.parametrize("mode", ["absent", "another_name"])
def test_a_mark_chain_that_does_not_name_the_underlying_is_refused_as_chain_identity(
    world, mode
) -> None:
    """The deadline chain is filed as AAPL but its header names no
    underlying (the OCC filter's fail-open branch: an orphan B1 row at the
    SELL 105C key would be priced as that leg) or names MSFT. Either way the
    mark is refused as ``chain_identity`` — never priced."""
    world.queue_file()
    occ = [_occ("AAPL", world, "C", 100), _occ("B1APL", world, "C", 105)]
    path, _ = world.chain(world.deadline, occ=occ if mode == "absent" else None)
    doc = _rename_underlying(path, mode)
    if mode == "absent":
        # the branch this check keeps unreachable: every row, the orphan included
        assert standard_row_indexes(doc) == [0, 1]

    result = _run(world, world.deadline)

    assert result["marks_created"] == 0
    with EvidenceStore(world.db, readonly=True) as ev:
        assert ev.all("mark") == []
        at_deadline = {
            q["code"] for q in ev.all("quality") if q["session"] == world.deadline.isoformat()
        }
    assert at_deadline == {"chain_identity"}


def test_an_entry_cannot_be_verified_against_a_source_chain_that_names_no_underlying(
    world,
) -> None:
    """The source-session chain (the entry fill's provenance) names no
    underlying, so the OCC filter would serve it whole and the frozen quotes
    would "match" unfiltered rows. The entry is refused: no mark at all,
    though the deadline chain itself is clean."""
    world.queue_file()
    source = world.store / "chains" / world.session.isoformat() / "AAPL.json.gz"
    assert source.is_file()  # the world fixture's source-session chain
    _rename_underlying(source, "absent")
    world.chain(world.deadline)  # a clean exit chain: only the entry side is wrong

    result = _run(world, world.deadline)

    assert result["marks_created"] == 0
    with EvidenceStore(world.db, readonly=True) as ev:
        assert ev.all("mark") == []
        assert any(q["session"] == world.deadline.isoformat() for q in ev.all("quality"))


def test_the_same_chains_with_their_own_underlying_mark_as_before(world) -> None:
    """The control: the identical fixture chains, headers untouched, mark."""
    world.queue_file()
    world.chain(world.deadline)

    assert _run(world, world.deadline)["marks_created"] == 1
    with EvidenceStore(world.db, readonly=True) as ev:
        assert [m["session"] for m in ev.all("mark")] == [world.deadline.isoformat()]

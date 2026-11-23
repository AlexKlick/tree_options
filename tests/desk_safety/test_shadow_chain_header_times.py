"""A chain header's two times: two causes, two codes (WT6-a).

``shadows._read_chain`` used to refuse three header-time conditions under ONE
code, ``chain_not_known_at_asof``:

* ``fetched_at > now``: the chain was fetched after the run's as-of, so it
  was not yet known. It is censored, and a later run accepts it (transient);
* ``source_as_of > fetched_at``: the header stamps its source after its own
  fetch. It is corrupt, and no later run repairs it (permanent);
* ``source_as_of > now``: this one never decides alone. If the fetch is after
  the as-of, the first condition holds; if it is at or before it, the second
  one does.

A code that means two things gives a scorecard row nobody can reason about,
so the permanent cause now has its own code, ``chain_header_source_after_fetch``.
Two orderings are pinned here:

* a chain not yet fetched at the as-of is reported as not yet known, even when
  its header is also corrupt: nothing about a document is recorded before it
  was fetched;
* a header whose source stamp is after both its fetch and the as-of gets the
  permanent code, which every later retry reports again.

The entry side needs no code of its own. ``_source_chain``'s prefix makes the
same failure ``entry_source_chain_header_source_after_fetch``
(test_shadow_entry_occ.test_a_failed_source_chain_keeps_its_own_cause).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tree_options.desk import shadows
from tree_options.desk.contracts import ContractError
from tree_options.desk.evidence import EvidenceStore
from tree_options.desk.sessions import cutoff_instant
from tree_options.desk.store import encode_document

NOT_KNOWN = "chain_not_known_at_asof"
CORRUPT = "chain_header_source_after_fetch"
H = timedelta(hours=1)


def _deadline_chain(world, *, fetched: datetime, source: datetime) -> dict:
    """The world's deadline chain with its two header times set as given."""
    path, doc = world.chain(world.deadline, fetched=fetched)
    doc["header"]["source_as_of"] = source.isoformat()
    path.write_bytes(encode_document(doc))
    return doc


def _read(world, now: datetime):
    return shadows._read_chain("AAPL", world.deadline, world.store, now)


def _refusal(world, now: datetime) -> str:
    with pytest.raises(ContractError) as exc:
        _read(world, now)
    return str(exc.value)


def _code_at(world, *, fetched: int, source: int, now: int) -> str:
    """The refusal for a deadline chain read at ``now``. Each argument is in
    hours after T, the deadline session's 16:15 ET cutoff (the fixture
    chain's own stamp)."""
    t = cutoff_instant(world.deadline)
    _deadline_chain(world, fetched=t + fetched * H, source=t + source * H)
    return _refusal(world, t + now * H)


def test_a_chain_fetched_after_the_as_of_is_not_yet_known(world) -> None:
    """The transient cause keeps its code."""
    assert _code_at(world, fetched=2, source=0, now=1) == NOT_KNOWN


def test_a_source_stamp_after_the_fetch_has_its_own_code(world) -> None:
    """The permanent cause, both times known at the as-of: its own code, not
    the transient one."""
    assert _code_at(world, fetched=0, source=1, now=2) == CORRUPT


def test_a_chain_not_yet_fetched_is_not_known_even_if_its_header_is_corrupt(world) -> None:
    """Both causes: the not-yet-fetched one wins. Nothing about a document,
    its header's defects included, is recorded before it was fetched."""
    assert _code_at(world, fetched=2, source=3, now=1) == NOT_KNOWN


def test_a_source_stamp_after_the_fetch_and_the_as_of_is_corrupt(world) -> None:
    """The overlap case: fetched at or before the as-of, source stamp after
    both. It is the permanent code: the fetch is known, so the header's
    defect is too, and a later run would only report it again. The old
    ``source > now`` clause would call it not yet known."""
    assert _code_at(world, fetched=0, source=2, now=1) == CORRUPT


def test_a_header_known_at_the_as_of_is_read(world) -> None:
    """The control: source at or before the fetch, fetch at or before the
    as-of. The chain is read, with its own two times."""
    t = cutoff_instant(world.deadline)
    _deadline_chain(world, fetched=t + H, source=t)
    _doc, source, fetched = _read(world, t + 2 * H)
    assert (source, fetched) == (t, t + H)


def test_a_later_as_of_accepts_a_chain_fetched_after_the_first(world) -> None:
    """Transient: the same bytes refused as not yet known are read once the
    as-of reaches the fetch."""
    t = cutoff_instant(world.deadline)
    _deadline_chain(world, fetched=t + 2 * H, source=t)
    assert _refusal(world, t + H) == NOT_KNOWN
    _read(world, t + 2 * H)  # no refusal


def test_no_later_as_of_repairs_a_source_stamp_after_the_fetch(world) -> None:
    """Permanent: before its fetch the chain is not yet known. From the fetch
    on, every later as-of reports the corrupt header under its own code. It
    is never read."""
    t = cutoff_instant(world.deadline)
    _deadline_chain(world, fetched=t + 2 * H, source=t + 3 * H)
    assert _refusal(world, t + H) == NOT_KNOWN
    for now in (t + 2 * H, t + 3 * H, t + 1000 * H):
        assert _refusal(world, now) == CORRUPT


# ------------------------------------------------------------ the lane itself


def _run(world, as_of):
    return shadows.update_shadows(
        session=as_of,
        now=cutoff_instant(as_of),
        cal=world.cal,
        database=world.db,
        store_root=world.store,
        queue_dir=world.queues,
    )


def _deadline_codes(world) -> set[str]:
    with EvidenceStore(world.db, readonly=True) as ev:
        assert ev.all("mark") == []
        return {q["code"] for q in ev.all("quality") if q["session"] == world.deadline.isoformat()}


def test_the_shadow_lane_records_a_corrupt_header_under_its_own_code(world) -> None:
    """End to end, the exit side: the deadline chain was fetched five minutes
    before the source stamp it carries. No mark is written. The quality event
    names the corrupt header, not "not yet known", and so does a run at a
    later as-of."""
    world.queue_file()
    t = cutoff_instant(world.deadline)
    _deadline_chain(world, fetched=t - timedelta(minutes=5), source=t)
    assert _run(world, world.deadline)["marks_created"] == 0
    assert _deadline_codes(world) == {CORRUPT}
    later = world.cal.nth_after(world.deadline, 1)
    assert _run(world, later)["marks_created"] == 0
    assert _deadline_codes(world) == {CORRUPT}


def test_the_shadow_lane_records_a_late_fetch_as_not_yet_known(world) -> None:
    """End to end, the transient control: a chain fetched after the as-of is
    recorded as not yet known, and the next session's run marks it."""
    world.queue_file()
    later = world.cal.nth_after(world.deadline, 1)
    world.chain(world.deadline, fetched=cutoff_instant(later))
    assert _run(world, world.deadline)["marks_created"] == 0
    assert _deadline_codes(world) == {NOT_KNOWN}
    assert _run(world, later)["marks_created"] == 1

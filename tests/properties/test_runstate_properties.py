"""Property tests: journal replay == projection; tamper detection; determinism."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from uuid import uuid4

from hypothesis import given
from hypothesis import strategies as st

from tree_options.runstate import RunIdentity, RunState, RunStore, canonical_run_id, is_legal
from tree_options.runstate import journal as J
from tree_options.runstate.errors import JournalCorruptError

# Round-1 review migration (2026-08-23): pytest's tmp_path AND
# tempfile.TemporaryDirectory both resolve under /tmp on this host; the
# runstate root refusal (RunStore.create/open) rejects /tmp paths.
# Property tests used `with tempfile.TemporaryDirectory()` directly —
# patch the module's `tempfile.TemporaryDirectory` to default to a durable
# scratch under the repo's gitignored artifacts/runstate-tests/.
_PROP_TESTS_ROOT = Path(__file__).resolve().parents[2] / "artifacts" / "runstate-tests"
_PROP_TESTS_ROOT.mkdir(parents=True, exist_ok=True)
_OrigTemporaryDirectory = tempfile.TemporaryDirectory


def TemporaryDirectory(*args, **kwargs):  # type: ignore[no-untyped-def]
    parent = _PROP_TESTS_ROOT / f"prop-{uuid4().hex}"
    parent.mkdir(parents=True, exist_ok=True)
    kwargs.setdefault("dir", str(parent))
    return _OrigTemporaryDirectory(*args, **kwargs)


tempfile.TemporaryDirectory = TemporaryDirectory  # type: ignore[assignment]

BOOT = "prop-boot"
T0 = 1_800_000_000


@st.composite
def legal_walks(draw: st.DrawFn) -> list[RunState]:
    state = RunState.PLANNED
    walk: list[RunState] = []
    for _ in range(draw(st.integers(min_value=0, max_value=12))):
        options = [t for t in RunState if is_legal(state, t)]
        if not options:
            break
        state = draw(st.sampled_from(options))
        walk.append(state)
    return walk


def _identity() -> RunIdentity:
    candidate = RunIdentity(
        run_id="pending-canonical-id",
        campaign="prop",
        protocol_hash="a" * 64,
        code_sha="b" * 40,
        provider="p/1",
        capture_version="c/1",
        universe_manifest_sha256="c" * 64,
        boot_id=BOOT,
        pid=1,
        pid_start_ticks=1,
        started_epoch=T0,
        args_hash="d" * 64,
    )
    return candidate.model_copy(update={"run_id": canonical_run_id(candidate)})


def _walk_store(root: Path, walk: list[RunState]) -> RunStore:
    store = RunStore.create(root, _identity(), now_epoch=T0)
    for i, state in enumerate(walk, start=1):
        store.transition(state, reason="walk", now_epoch=T0 + i, actor_pid=1, actor_boot_id=BOOT)
    return store


@given(walk=legal_walks())
def test_projection_always_equals_journal_replay(walk: list[RunState]) -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _walk_store(root, walk)
        run_id = _identity().run_id
        reopened = RunStore.open(root, run_id)
        expected = walk[-1] if walk else RunState.PLANNED
        assert reopened.state == expected
        projection = J.load_projection(root / run_id)
        assert projection.state == expected


@given(walk=legal_walks(), salt=st.integers(min_value=0, max_value=1 << 30))
def test_single_record_tamper_always_detected(walk: list[RunState], salt: int) -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _walk_store(root, walk)
        journal_path = root / _identity().run_id / J.JOURNAL_FILENAME
        lines = journal_path.read_text().splitlines()
        if len(lines) < 2:
            return  # nothing non-final to tamper
        victim_index = salt % (len(lines) - 1)  # never the final line
        victim = json.loads(lines[victim_index])
        victim["reason"] = f"tampered {salt}"
        lines[victim_index] = json.dumps(victim, sort_keys=True, separators=(",", ":"))
        journal_path.write_text("\n".join(lines) + "\n")
        try:
            view = J.replay(root / "prop-run")
        except JournalCorruptError:
            return  # detected: the property holds
        assert not view.tail_damaged, "mid-file tamper must not pass silently"


@given(walk=legal_walks())
def test_journal_bytes_are_pure_function_of_history(walk: list[RunState]) -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _walk_store(base / "one", walk)
        _walk_store(base / "two", walk)
        run_id = _identity().run_id
        assert (base / "one" / run_id / "journal.jsonl").read_bytes() == (
            base / "two" / run_id / "journal.jsonl"
        ).read_bytes()

"""launch_bars_era: exit-code contract, read-only preflight, gated execute.

The full valid scenario is synthetic end to end (tests/fixtures/bars_sample.py):
a capture dir + capture manifest + census bound to those exact bytes, a
0.2.1-shaped protocol built through the repo's own models, a work manifest
REGENERATED from the captures via the declared profile, a 0600 key file, and
a run store walked to BARS_READY. Authority ledger + run store live under
scratch roots in the repo's gitignored artifacts/ (never /tmp — the ledger
refuses it, and durable run state may not live where a reboot wipes it).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import launch_bars_era as launch  # noqa: E402
from tests.fixtures.bars_sample import (  # noqa: E402
    BOOT,
    CENSUS_CODE_SHA,
    CENSUS_UNIVERSE_MANIFEST_SHA256,
    RUN_ID,
    T0,
    census_bytes,
    make_matching_run_identity,
    make_run_identity,
    write_021_protocol,
    write_bars_capture,
    write_capture_manifest,
)
from tests.unit.test_protocol_amendment import _base_020_protocol_bytes  # noqa: E402
from tree_options.data.bars_manifest import (  # noqa: E402
    BARS_LEDGER_FILENAME,
    KIND_BARS_LAUNCH_CONSUMED,
    append_bars_launch_approval,
    append_bars_launch_consumed,
    build_bars_work_manifest,
    load_selection_profile,
    read_bars_ledger,
)
from tree_options.protocol.loader import load_protocol, protocol_hash  # noqa: E402
from tree_options.runstate import RunState, RunStore  # noqa: E402
from tree_options.runstate import lease as lease_module  # noqa: E402

REAL_PROTOCOL = REPO_ROOT / "research_protocol.yaml"
COMMITTED_PROFILE = REPO_ROOT / "data" / "bars" / "selection-profile.json"
PINNED_UNIVERSE = ",".join(
    json.loads((REPO_ROOT / "data" / "coverage" / "coverage_universe.json").read_text())[
        "underlyings"
    ]
)
BARS_READY_WALK = (
    RunState.CAPTURING,
    RunState.CAPTURE_COMPLETE,
    RunState.INSPECTION_RUNNING,
    RunState.INSPECTED,
    RunState.AMENDMENT_PENDING_OWNER,
    RunState.AMENDMENT_READY,
    RunState.BARS_READY,
)


@pytest.fixture()
def scratch_root() -> Iterator[Path]:
    root = REPO_ROOT / "artifacts" / "bars-a4-tests" / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _create_bars_ready_store(
    protocol_path: Path,
    capture_manifest: Path,
    store_root: Path,
    *,
    code_sha: str = CENSUS_CODE_SHA,
    universe_manifest_sha256: str = CENSUS_UNIVERSE_MANIFEST_SHA256,
) -> RunStore:
    """A store walked to BARS_READY, identity consistent with the scenario
    census provenance by default. Round-2 (finding 5): execute cross-joins
    the store's code_sha / universe_manifest_sha256 against the VERIFIED
    census, so the mismatch tests override them here (placeholder hashes,
    exactly like the probe store)."""
    capture_manifest_sha = hashlib.sha256(capture_manifest.read_bytes()).hexdigest()
    identity = make_matching_run_identity(
        protocol_hash=protocol_hash(load_protocol(protocol_path)),
        code_sha=code_sha,
        universe_manifest_sha256=universe_manifest_sha256,
        capture_manifest_sha256=capture_manifest_sha,
    )
    store = RunStore.create(store_root, identity, now_epoch=T0)
    # Round-1 review fix: pin the manifest so the execute identity cross-join
    # passes (store.pinned_manifest_sha256 == the capture manifest pin).
    store.pin_manifest(
        capture_manifest_sha,
        now_epoch=T0 + 100,
        actor_pid=identity.pid,
        actor_boot_id=BOOT,
    )
    for step, state in enumerate(BARS_READY_WALK):
        store.transition(
            state,
            reason=f"fixture walk to BARS_READY ({step})",
            now_epoch=T0 + step,
            actor_pid=identity.pid,
            actor_boot_id=BOOT,
        )
    return store


@pytest.fixture()
def scenario(
    tmp_path: Path, scratch_root: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    capture_dir = write_bars_capture(tmp_path / "capture")
    # Round-1 review fix: the manifest must live INSIDE the capture_dir at
    # the standard path; the regenerate-and-compare check looks for it there.
    manifest_path = write_capture_manifest(capture_dir, capture_dir / "capture_manifest.json")
    external_manifest_path = tmp_path / "capture_manifest.json"
    external_manifest_path.write_bytes(manifest_path.read_bytes())
    census_path = tmp_path / "census.json"
    census_path.write_bytes(census_bytes(manifest_path.read_bytes()))
    protocol_path = write_021_protocol(tmp_path / "protocol-0.2.1.yaml", REAL_PROTOCOL)
    work_manifest = build_bars_work_manifest(
        capture_dir,
        profile=load_selection_profile(COMMITTED_PROFILE),
        capture_manifest=manifest_path,
        budget_limit=45,
    )
    work_path = tmp_path / "work-manifest.json"
    work_path.write_text(work_manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    packet_path = tmp_path / "amendment-packet.json"
    packet_path.write_text(
        json.dumps({"landed": False, "proposed_version": "0.2.1"}, sort_keys=True),
        encoding="utf-8",
    )
    key_path = tmp_path / "polygon.key"
    key_path.write_bytes(b"fixture-key-never-read-by-preflight\n")
    key_path.chmod(0o600)

    authority_root = scratch_root / "bars-authority"
    store_root = scratch_root / "runstate"
    # Round-1 review fix: the store's identity is cross-joined against the
    # approval record (protocol hash + capture-manifest pin). Round-2 review
    # fix (finding 5): execute additionally cross-joins code_sha and
    # universe_manifest_sha256 against the VERIFIED census provenance — the
    # helper's defaults match the scenario census exactly; the mismatch
    # tests build mismatched stores through the same helper.
    store = _create_bars_ready_store(protocol_path, manifest_path, store_root)
    return {
        "capture_dir": capture_dir,
        "capture_manifest": manifest_path,
        "census": census_path,
        "protocol": protocol_path,
        "work_manifest": work_path,
        "work_manifest_model": work_manifest,
        "packet": packet_path,
        "key": key_path,
        "authority_root": authority_root,
        "store_root": store_root,
        "run_id": store.identity.run_id,
    }


def _approve(scenario: dict[str, Path], **overrides: str) -> None:
    fields = dict(
        protocol_hash=protocol_hash(load_protocol(scenario["protocol"])),
        amendment_packet_sha256=hashlib.sha256(scenario["packet"].read_bytes()).hexdigest(),
        census_sha256=str(
            json.loads(scenario["census"].read_text(encoding="utf-8"))["content_sha256"]
        ),
        work_manifest_sha256=hashlib.sha256(scenario["work_manifest"].read_bytes()).hexdigest(),
    )
    fields.update(overrides)
    append_bars_launch_approval(
        scenario["authority_root"], reason="owner approved the bars grid", at_epoch=T0, **fields
    )


def _argv(
    scenario: dict[str, Path],
    *,
    protocol: Path | None = None,
    drop_protocol: bool = False,
    extra: list[str] | None = None,
) -> list[str]:
    argv = [
        "--run-id",
        str(scenario["run_id"]),
        "--census",
        str(scenario["census"]),
        "--capture-manifest",
        str(scenario["capture_manifest"]),
        "--capture-dir",
        str(scenario["capture_dir"]),
        "--work-manifest",
        str(scenario["work_manifest"]),
        "--vendor-key",
        str(scenario["key"]),
        "--store-root",
        str(scenario["store_root"]),
        "--authority-root",
        str(scenario["authority_root"]),
        "--amendment-packet",
        str(scenario["packet"]),
        "--boot-id-override",
        BOOT,
    ]
    if drop_protocol:
        pass  # the CLI default is the REAL repo protocol (0.2.1 today)
    else:
        argv += ["--protocol", str(protocol if protocol else scenario["protocol"])]
    if extra:
        argv += extra
    return argv


def _tree_state(*roots: Path) -> dict[Path, tuple[int, int]]:
    state: dict[Path, tuple[int, int]] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                stat = path.stat()
                state[path] = (stat.st_mtime_ns, stat.st_size)
    return state


def _authority_tree_state(root: Path) -> tuple[bool, bytes | None]:
    """The read-only isolation oracle for a REAL authority tree: its
    existence plus the ledger's exact bytes.

    Host absence is NOT part of the contract — the live bars era minted a
    real ledger under ``artifacts/bars-authority/``, so ``not
    root.exists()`` is an assertion about this HOST, not about the tool.
    The checkable claim is that a preflight refusal never WRITES there:
    the same (exists, ledger-bytes) pair before and after."""
    ledger = root / BARS_LEDGER_FILENAME
    return (root.exists(), ledger.read_bytes() if ledger.is_file() else None)


def _real_preflight_argv(scratch_root: Path) -> list[str]:
    """The read-only preflight argv against the REAL repo protocol (the CLI
    default): the documented exit-2 refusal when no BARS_LAUNCH_APPROVAL
    record binds the loaded protocol hash."""
    return [
        "--run-id",
        RUN_ID,
        "--census",
        "census-not-even-read.json",
        "--capture-manifest",
        "manifest-not-even-read.json",
        "--work-manifest",
        "work-not-even-read.json",
        "--store-root",
        str(scratch_root / "runstate"),
        "--authority-root",
        str(scratch_root / "bars-authority"),
    ]


def _sole_store_run_id(store_root: Path) -> str:
    run_ids = sorted(path.name for path in store_root.iterdir() if path.is_dir())
    assert len(run_ids) == 1, f"expected one run store under {store_root}, got {run_ids}"
    return run_ids[0]


# ---- preflight: the record half of the protocol gate is closed on main ---------------
# (0.2.1 landed in cdf38c8, so the version clause passes on the REAL repo
# protocol today; the refusal is the missing BARS_LAUNCH_APPROVAL record.)


def test_preflight_exit_2_on_real_021_protocol_today_without_an_approval_record(
    scratch_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The documented correct answer on main after 0.2.1 landed: the loaded
    protocol IS 0.2.1, so the version clause passes and the refusal is the
    missing BARS_LAUNCH_APPROVAL record — read-only, through the REAL loader."""
    assert load_protocol(REAL_PROTOCOL).meta.protocol_version == "0.2.1"
    real_authority = REPO_ROOT / "artifacts" / "bars-authority"
    before = _authority_tree_state(real_authority)
    assert launch.main(_real_preflight_argv(scratch_root)) == 2
    err = capsys.readouterr().err
    assert "no BARS_LAUNCH_APPROVAL record binds the loaded protocol hash" in err
    # the version clause PASSED (the protocol is 0.2.1 now): the record is
    # the refusal, not the version
    assert "protocol version" not in err
    # read-only: nothing was created anywhere the tool knows about. The real
    # authority tree EXISTS on this host (the live bars era minted a ledger
    # there), so the oracle is UNCHANGED-ness — never host absence
    assert not (scratch_root / "bars-authority").exists()
    assert _authority_tree_state(real_authority) == before


def test_preflight_refusal_never_writes_a_pre_existing_real_authority_ledger(
    scratch_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The read-only claim, proven on a PRE-EXISTING authority tree.

    The live bars era minted a real ledger under the real
    ``artifacts/bars-authority/``, so a tmp fixture under the real path is
    impossible (and unwanted). Here REPO_ROOT points at a scratch repo that
    ALREADY carries an authority ledger with known bytes — exactly the host
    condition today — and the refusal must leave it untouched: the same
    (exists, ledger-bytes) pair before and after, and never an append, a
    rewrite, or a truncation. The old assertion form
    (``not (REPO_ROOT / "artifacts" / "bars-authority").exists()``) fails
    this fixture by construction: the tree exists going in."""
    fake_repo = tmp_path / "repo"
    authority = fake_repo / "artifacts" / "bars-authority"
    authority.mkdir(parents=True)
    ledger_bytes = b'{"kind": "BARS_LAUNCH_APPROVAL", "note": "pre-existing live-era ledger"}\n'
    (authority / BARS_LEDGER_FILENAME).write_bytes(ledger_bytes)
    monkeypatch.setattr(f"{__name__}.REPO_ROOT", fake_repo)
    before = _authority_tree_state(authority)

    assert launch.main(_real_preflight_argv(scratch_root)) == 2
    err = capsys.readouterr().err
    assert "no BARS_LAUNCH_APPROVAL record binds the loaded protocol hash" in err
    assert "protocol version" not in err
    # the isolation oracle: the pre-existing REAL-tree ledger is unchanged
    assert _authority_tree_state(authority) == before
    assert (authority / BARS_LEDGER_FILENAME).read_bytes() == ledger_bytes
    # and the refusal created nothing anywhere else it knows about
    assert not (scratch_root / "bars-authority").exists()


def test_preflight_exit_2_wrong_version_even_with_matching_record(
    scenario: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A record binding the loaded protocol's hash does not open the gate
    when the version is wrong: the 0.2.1 requirement is its own refusal.
    The repo protocol is 0.2.1 now (0.2.1 landed in cdf38c8), so the
    wrong-version leg is the pre-amendment 0.2.0 shape, rebuilt through the
    loader's own models, with a record binding exactly its hash."""
    pre_amendment = tmp_path / "protocol-0.2.0.yaml"
    pre_amendment.write_bytes(_base_020_protocol_bytes())
    _approve(scenario, protocol_hash=protocol_hash(load_protocol(pre_amendment)))
    assert launch.main(_argv(scenario, protocol=pre_amendment)) == 2
    assert "protocol version" in capsys.readouterr().err


def test_preflight_exit_2_without_any_record(scenario: dict[str, Path]) -> None:
    assert launch.main(_argv(scenario)) == 2


# ---- preflight: census currency ------------------------------------------------------


def test_preflight_exit_3_on_census_stale_vs_manifest(scenario: dict[str, Path]) -> None:
    _approve(scenario)
    scenario["capture_manifest"].write_bytes(b'{"drifted": true}\n')
    assert launch.main(_argv(scenario)) == 3


def test_preflight_exit_3_on_census_tampered(scenario: dict[str, Path]) -> None:
    _approve(scenario)
    doc = json.loads(scenario["census"].read_text(encoding="utf-8"))
    doc["content_sha256"] = "0" * 64
    scenario["census"].write_text(json.dumps(doc), encoding="utf-8")
    assert launch.main(_argv(scenario)) == 3


# ---- preflight: work manifest / vendor key -------------------------------------------


def test_preflight_exit_8_on_foreign_selection_profile(scenario: dict[str, Path]) -> None:
    _approve(scenario)
    doc = json.loads(COMMITTED_PROFILE.read_text(encoding="utf-8"))
    doc["sides"]["value"] = "call"  # a DIFFERENT, self-consistent profile
    doc["content_sha256"] = ""
    from tree_options.data.bars_manifest import SelectionProfile, profile_content_sha256

    other = SelectionProfile.model_validate(doc)
    other = other.model_copy(update={"content_sha256": profile_content_sha256(other)})
    other_path = scenario["work_manifest"].parent / "other-profile.json"
    other_path.write_text(other.model_dump_json(indent=2), encoding="utf-8")
    assert launch.main(_argv(scenario, extra=["--selection-profile", str(other_path)])) == 8


def test_preflight_exit_9_on_missing_key(scenario: dict[str, Path]) -> None:
    _approve(scenario)
    scenario["key"].unlink()
    assert launch.main(_argv(scenario)) == 9


def test_preflight_exit_9_on_world_readable_key(scenario: dict[str, Path]) -> None:
    _approve(scenario)
    scenario["key"].chmod(0o644)
    assert launch.main(_argv(scenario)) == 9


# ---- preflight: run state / duplicate launch -----------------------------------------


def test_preflight_exit_5_on_missing_store(scenario: dict[str, Path]) -> None:
    _approve(scenario)
    shutil.rmtree(scenario["store_root"])
    assert launch.main(_argv(scenario)) == 5


def test_preflight_exit_5_on_state_not_bars_ready(scenario: dict[str, Path]) -> None:
    """A store that stopped one transition short of BARS_READY refuses."""
    _approve(scenario)
    other_root = scenario["store_root"].parent / "runstate-not-ready"
    store = RunStore.create(other_root, make_run_identity(), now_epoch=T0)
    for step, state in enumerate(BARS_READY_WALK[:-1]):  # stops at AMENDMENT_READY
        store.transition(
            state,
            reason="fixture: one short of BARS_READY",
            now_epoch=T0 + step,
            actor_pid=store.identity.pid,
            actor_boot_id=BOOT,
        )
    argv = _argv(scenario)
    argv[argv.index("--store-root") + 1] = str(other_root)
    argv[argv.index("--run-id") + 1] = store.identity.run_id
    assert launch.main(argv) == 5


def test_preflight_exit_5_on_held_lease_duplicate_launch(
    scenario: dict[str, Path],
) -> None:
    """A live owner holds the run's lease: a second launcher is a duplicate."""
    _approve(scenario)
    store_dir = scenario["store_root"] / str(scenario["run_id"])
    owner = lease_module.current_owner(now_epoch=T0).model_copy(update={"boot_id": BOOT})
    lease_module.acquire(store_dir, owner, boot_id_now=BOOT)
    assert launch.main(_argv(scenario)) == 5


# ---- preflight: refuse-fallback (exit 4) ----------------------------------------------

_REFUSALS: list[tuple[list[str], str]] = [
    (["--vendor-host", "evil.example.com"], "api.polygon.io"),
    (["--endpoint-template", "/v9/evil"], "/v2/aggs/ticker/"),
    (["--calendar-token", "someone-elses-calendar"], "nyse_sessions_2018_01_02_2026_12_31"),
    (["--universe", "SPY,TSLA"], "AAPL"),
    (["--selection-rule", "representative"], "atm-grid"),
]


@pytest.mark.parametrize("flag,pinned_fragment", _REFUSALS, ids=[f[0][0][2:] for f in _REFUSALS])
def test_every_override_flag_refused_exit_4(
    scenario: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    flag: list[str],
    pinned_fragment: str,
) -> None:
    _approve(scenario)
    assert launch.main(_argv(scenario, extra=flag)) == 4
    # the message NAMES the pinned value (these are not secrets)
    assert pinned_fragment in capsys.readouterr().err


def test_override_equal_to_pinned_value_accepted(scenario: dict[str, Path]) -> None:
    """The pinned constants are the ONLY accepted values — supplying exactly
    one is an identity no-op, never a fallback."""
    _approve(scenario)
    extra = [
        "--vendor-host",
        launch.PINNED_VENDOR_HOST,
        "--endpoint-template",
        launch.PINNED_ENDPOINT_TEMPLATES["contracts"],
        "--endpoint-template",
        launch.PINNED_ENDPOINT_TEMPLATES["aggs"],
        "--calendar-token",
        launch.PINNED_CALENDAR_TOKEN,
        "--universe",
        PINNED_UNIVERSE,
        "--selection-rule",
        launch.PINNED_SELECTION_RULE,
    ]
    assert launch.main(_argv(scenario, extra=extra)) == 0


def test_pinned_universe_is_the_committed_29() -> None:
    assert len(PINNED_UNIVERSE.split(",")) == 29
    assert launch.PINNED_VENDOR_HOST == "api.polygon.io"


# ---- preflight: happy path is green AND mutates nothing -------------------------------


def test_preflight_green_and_mutates_nothing(
    scenario: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _approve(scenario)
    before = _tree_state(
        scenario["authority_root"],
        scenario["store_root"],
        scenario["capture_dir"],
        scenario["capture_manifest"].parent,
    )
    artifacts_before = sorted(p.name for p in (REPO_ROOT / "artifacts").iterdir())
    assert launch.main(_argv(scenario)) == 0
    after = _tree_state(
        scenario["authority_root"],
        scenario["store_root"],
        scenario["capture_dir"],
        scenario["capture_manifest"].parent,
    )
    assert before == after, "preflight must not create, modify, or touch anything"
    assert sorted(p.name for p in (REPO_ROOT / "artifacts").iterdir()) == artifacts_before
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "preflight"
    assert all(check["ok"] for check in report["checks"].values())


def test_cli_default_mode_is_preflight(scenario: dict[str, Path]) -> None:
    """Omitting the mode flag runs preflight (here: green, with a record)."""
    _approve(scenario)
    assert launch.main(_argv(scenario)) == 0


# ---- execute ---------------------------------------------------------------------------


def _fake_runner(scenario: dict[str, Path], calls: list[str]):
    def runner(context) -> str:
        calls.append("runner")
        store = RunStore.open(scenario["store_root"], str(scenario["run_id"]))
        # ORDERING ASSERT: by the time the runner runs, the transition is
        # journaled, the lease is acquired, and the consumption is durable.
        assert store.state is RunState.BARS_CAPTURING
        assert (store.dir / lease_module.LEASE_DIRNAME / lease_module.OWNER_FILENAME).exists()
        view = read_bars_ledger(scenario["authority_root"])
        assert any(r.kind == KIND_BARS_LAUNCH_CONSUMED for r in view.records)
        assert (
            context.work_manifest_sha256
            == hashlib.sha256(scenario["work_manifest"].read_bytes()).hexdigest()
        )
        return "fake-runner-ok"

    return runner


def _execute(scenario: dict[str, Path], *, runner) -> tuple[int, object]:
    args = launch._parse_args(_argv(scenario))
    return launch.run_execute(args, runner=runner, now_epoch=T0 + 100, boot_id_now=BOOT)


def test_execute_exit_6_without_authority_record(scenario: dict[str, Path]) -> None:
    calls: list[str] = []
    code, summary = _execute(scenario, runner=_fake_runner(scenario, calls))
    assert code == 6 and summary is None
    assert calls == []  # the runner was never invoked
    assert not (scenario["authority_root"]).exists()  # nothing consumed


def test_execute_exit_6_when_record_binds_other_work_manifest(
    scenario: dict[str, Path],
) -> None:
    """The approval exists for this protocol but binds a DIFFERENT work
    manifest: the launch authority does not transfer."""
    _approve(scenario, work_manifest_sha256="f" * 64)
    calls: list[str] = []
    code, summary = _execute(scenario, runner=_fake_runner(scenario, calls))
    assert code == 6 and summary is None
    assert calls == []
    assert read_bars_ledger(scenario["authority_root"]).records[0].kind == (
        "BARS_LAUNCH_APPROVAL"
    )  # only the approval exists: nothing was consumed


def test_execute_exit_6_on_packet_hash_mismatch(scenario: dict[str, Path]) -> None:
    _approve(scenario, amendment_packet_sha256="e" * 64)
    calls: list[str] = []
    code, _ = _execute(scenario, runner=_fake_runner(scenario, calls))
    assert code == 6
    assert calls == []


# ---- round-4 (finding 6): the authority join searches EVERY approval -----------------
#
# Round-4 review fix (2026-08-23): _matching_approval returned the FIRST
# record binding the protocol hash and execute compared THAT record's
# work_manifest_sha256 — so a ledger carrying APPROVAL(P, M1) then
# APPROVAL(P, M2) refused (exit 6, "no approval binds protocol AND
# manifest") an execute with M2's exact inputs, although APPROVAL(P, M2)
# grants exactly that. The protocol gate still opens on ANY record binding
# the protocol hash; the execute-side join narrows ALL such records by every
# field the site compares (work manifest, then packet, then census).


def test_execute_authority_searches_all_approvals_not_just_the_first(
    scenario: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """The reviewer's exact ledger: APPROVAL(P, M1) appended first, then
    APPROVAL(P, M2) for the scenario's own work manifest. Executing with
    M2's exact inputs must pass the authority gate — the earlier approval
    of a DIFFERENT work manifest does not shadow M2's grant."""
    _approve(scenario, work_manifest_sha256="f" * 64)  # M1: an earlier, different grid
    _approve(scenario)  # M2: binds this scenario's exact inputs
    calls: list[str] = []
    code, summary = _execute(scenario, runner=_fake_runner(scenario, calls))
    err = capsys.readouterr().err
    assert "no BARS_LAUNCH_APPROVAL record binds" not in err, (
        "APPROVAL(P, M2) binds the verified protocol hash AND work manifest"
    )
    assert code == 0, "the authority refusal is gone; every other gate is green here"
    assert calls == ["runner"]
    assert summary is not None and summary.runner_outcome == "fake-runner-ok"
    view = read_bars_ledger(scenario["authority_root"])
    assert [r.kind for r in view.records] == [
        "BARS_LAUNCH_APPROVAL",
        "BARS_LAUNCH_APPROVAL",
        "BARS_LAUNCH_CONSUMED",
    ], "one consumption, after both approvals"


def test_execute_still_refuses_when_no_approval_binds_the_manifest(
    scenario: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """The narrowing must not weaken the refusal: approvals exist for the
    protocol but NONE binds the verified work manifest — exit 6 stands, and
    the message still names the tuple."""
    _approve(scenario, work_manifest_sha256="1" * 64)
    _approve(scenario, work_manifest_sha256="2" * 64)
    calls: list[str] = []
    code, summary = _execute(scenario, runner=_fake_runner(scenario, calls))
    assert code == 6 and summary is None and calls == []
    assert "no BARS_LAUNCH_APPROVAL record binds" in capsys.readouterr().err
    view = read_bars_ledger(scenario["authority_root"])
    assert [r.kind for r in view.records] == ["BARS_LAUNCH_APPROVAL"] * 2


# ---- round-2 (finding 5): execute-time census + identity cross-join -------------------
#
# Probe /tmp/pr-a-bars-execute-binding-probe.log: a BARS_READY store with
# placeholder code_sha/universe hashes and a DELETED census file used to
# consume authority, transition, and invoke the runner with exit 0. The
# census is now verified AT EXECUTE TIME and cross-joined against the
# approval record and the store identity — every refusal below happens
# BEFORE any lease, consumption, transition, or runner invocation.


def _execute_with_store(
    scenario: dict[str, Path], store_root: Path, *, runner
) -> tuple[int, object]:
    argv = _argv(scenario)
    argv[argv.index("--store-root") + 1] = str(store_root)
    argv[argv.index("--run-id") + 1] = _sole_store_run_id(store_root)
    args = launch._parse_args(argv)
    return launch.run_execute(args, runner=runner, now_epoch=T0 + 100, boot_id_now=BOOT)


def _assert_nothing_consumed(scenario: dict[str, Path], store_root: Path) -> None:
    view = read_bars_ledger(scenario["authority_root"])
    assert [r.kind for r in view.records] == ["BARS_LAUNCH_APPROVAL"]
    store = RunStore.open(store_root, _sole_store_run_id(store_root))
    assert store.state is RunState.BARS_READY


def test_execute_exit_6_on_store_code_sha_mismatch_vs_census(
    scenario: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """The store's code_sha is a placeholder (the exact probe shape): it does
    not match the census provenance's code_sha, so the identity cross-join
    refuses before anything is consumed."""
    _approve(scenario)
    mismatch_root = scenario["store_root"].parent / "runstate-code-mismatch"
    _create_bars_ready_store(
        scenario["protocol"],
        scenario["capture_manifest"],
        mismatch_root,
        code_sha="0" * 40,  # placeholder, exactly like the probe store
    )
    calls: list[str] = []
    code, summary = _execute_with_store(
        scenario, mismatch_root, runner=_fake_runner(scenario, calls)
    )
    assert code == 6 and summary is None
    assert calls == []
    err = capsys.readouterr().err
    assert "EXECUTE REFUSED (identity)" in err
    assert ("0" * 12) in err  # both 12-char prefixes are printed
    assert CENSUS_CODE_SHA[:12] in err
    _assert_nothing_consumed(scenario, mismatch_root)


def test_execute_exit_6_on_store_universe_mismatch_vs_census(
    scenario: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _approve(scenario)
    mismatch_root = scenario["store_root"].parent / "runstate-universe-mismatch"
    _create_bars_ready_store(
        scenario["protocol"],
        scenario["capture_manifest"],
        mismatch_root,
        universe_manifest_sha256="9" * 64,  # the probe's placeholder universe
    )
    calls: list[str] = []
    code, summary = _execute_with_store(
        scenario, mismatch_root, runner=_fake_runner(scenario, calls)
    )
    assert code == 6 and summary is None
    assert calls == []
    err = capsys.readouterr().err
    assert "EXECUTE REFUSED (identity)" in err
    assert ("9" * 12) in err
    assert CENSUS_UNIVERSE_MANIFEST_SHA256[:12] in err
    _assert_nothing_consumed(scenario, mismatch_root)


def test_execute_exit_6_on_census_deleted_before_execute(
    scenario: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """The exact probe: census_exists_at_execute false. The census is
    re-verified AT EXECUTE TIME — a deleted census refuses before any
    authority is consumed."""
    _approve(scenario)
    scenario["census"].unlink()
    calls: list[str] = []
    code, summary = _execute(scenario, runner=_fake_runner(scenario, calls))
    assert code == 6 and summary is None
    assert calls == []
    assert "EXECUTE REFUSED (census)" in capsys.readouterr().err
    _assert_nothing_consumed(scenario, scenario["store_root"])


def test_execute_exit_6_on_approval_naming_a_different_census(
    scenario: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """The approval must name THIS census: a census_sha256 that differs from
    the verified census's content hash refuses the launch."""
    _approve(scenario, census_sha256="0" * 64)
    calls: list[str] = []
    code, summary = _execute(scenario, runner=_fake_runner(scenario, calls))
    assert code == 6 and summary is None
    assert calls == []
    err = capsys.readouterr().err
    assert "EXECUTE REFUSED (identity)" in err
    assert ("0" * 12) in err
    # the verified census's content hash prefix is printed too
    census_hash = str(json.loads(scenario["census"].read_text(encoding="utf-8"))["content_sha256"])
    assert census_hash[:12] in err
    _assert_nothing_consumed(scenario, scenario["store_root"])


def test_execute_happy_path_consumes_then_transitions_before_runner(
    scenario: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _approve(scenario)
    calls: list[str] = []
    code, summary = _execute(scenario, runner=_fake_runner(scenario, calls))
    assert code == 0
    assert calls == ["runner"]
    assert summary is not None
    assert summary.runner_outcome == "fake-runner-ok"
    assert summary.state == "BARS_CAPTURING"
    assert (
        summary.work_manifest_sha256
        == hashlib.sha256(scenario["work_manifest"].read_bytes()).hexdigest()
    )
    store = RunStore.open(scenario["store_root"], str(scenario["run_id"]))
    assert store.state is RunState.BARS_CAPTURING
    view = read_bars_ledger(scenario["authority_root"])
    kinds = [r.kind for r in view.records]
    assert kinds == ["BARS_LAUNCH_APPROVAL", "BARS_LAUNCH_CONSUMED"]
    record = json.loads(capsys.readouterr().out)
    assert record["state"] == "BARS_CAPTURING"


def test_execute_duplicate_exit_7_after_crash_style_consumption(
    scenario: dict[str, Path],
) -> None:
    """A crash between consumption and the runner leaves a durable CONSUMED
    with the store still BARS_READY: a re-execute refuses, never retries."""
    _approve(scenario)
    append_bars_launch_consumed(
        scenario["authority_root"],
        protocol_hash=protocol_hash(load_protocol(scenario["protocol"])),
        amendment_packet_sha256=hashlib.sha256(scenario["packet"].read_bytes()).hexdigest(),
        census_sha256=str(
            json.loads(scenario["census"].read_text(encoding="utf-8"))["content_sha256"]
        ),
        work_manifest_sha256=hashlib.sha256(scenario["work_manifest"].read_bytes()).hexdigest(),
        reason="crash-after-consumption fixture",
        at_epoch=T0 + 50,
    )
    calls: list[str] = []
    code, summary = _execute(scenario, runner=_fake_runner(scenario, calls))
    assert code == 7 and summary is None
    assert calls == []
    store = RunStore.open(scenario["store_root"], str(scenario["run_id"]))
    assert store.state is RunState.BARS_READY  # never transitioned on the refusal


def test_execute_two_store_interleaving_refuses_the_second_runner(
    scenario: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Round-3 review fix (2026-08-23, P1 finding 1): the duplicate scan is
    not atomic ACROSS run stores.

    Two valid BARS_READY stores for the same approved work manifest: store A
    consumes; store B's scan saw only the PRE-A ledger view (the exact race
    window — the scan runs before the store-specific lease, so A's append
    lands between B's scan and B's append). The append path used to reread
    A's new tail and chain a SECOND consumption, invoking BOTH runners. The
    uniqueness recheck now happens INSIDE the ledger append under the flock:
    B refuses with exit 7, exactly one consumption exists, one runner ran."""
    _approve(scenario)
    # The ledger view as B saw it at scan time: approval only, before A's
    # append (captured now, before A executes).
    stale_view = read_bars_ledger(scenario["authority_root"])
    assert [r.kind for r in stale_view.records] == ["BARS_LAUNCH_APPROVAL"]

    # A executes for real: consumes once, runner runs once.
    a_calls: list[str] = []
    code_a, _ = _execute(scenario, runner=_fake_runner(scenario, a_calls))
    assert code_a == 0 and a_calls == ["runner"]

    # Store B: an equally valid BARS_READY store for the SAME work manifest
    # under a different root. B's ledger read returns the stale (pre-A)
    # view — the interleaving — while the append path reads the real file.
    store_b_root = scenario["store_root"].parent / "runstate-b"
    _create_bars_ready_store(scenario["protocol"], scenario["capture_manifest"], store_b_root)
    monkeypatch.setattr(launch, "read_bars_ledger", lambda root: stale_view)

    b_calls: list[str] = []

    def b_runner(context) -> str:
        b_calls.append("runner")
        return "should-never-run"

    code_b, summary_b = _execute_with_store(scenario, store_b_root, runner=b_runner)
    assert code_b == 7 and summary_b is None
    assert b_calls == []  # exactly ONE runner invocation (A's), never B's
    # exactly one consumption in the authority ledger
    view = read_bars_ledger(scenario["authority_root"])
    consumptions = [r for r in view.records if r.kind == KIND_BARS_LAUNCH_CONSUMED]
    assert len(consumptions) == 1
    assert (
        consumptions[0].work_manifest_sha256
        == hashlib.sha256(scenario["work_manifest"].read_bytes()).hexdigest()
    )
    assert "EXECUTE REFUSED (duplicate)" in capsys.readouterr().err
    # B's store is untouched: still BARS_READY, no lease left behind by the
    # refused launcher.
    store_b = RunStore.open(store_b_root, _sole_store_run_id(store_b_root))
    assert store_b.state is RunState.BARS_READY
    assert not (store_b.dir / lease_module.LEASE_DIRNAME / lease_module.OWNER_FILENAME).exists()


def _swapped_manifest_bytes(scenario: dict[str, Path]) -> bytes:
    """A parse-valid but UNVERIFIABLE work manifest (one entry dropped: both
    the Budget restatement and the regenerate-and-compare refuse it), with a
    self-consistent content hash — exactly what a swap attack needs."""
    base = scenario["work_manifest_model"]
    from tree_options.data.bars_manifest import work_manifest_content_sha256

    swapped = base.model_copy(update={"entries": base.entries[:-1], "content_sha256": ""})
    swapped = swapped.model_copy(update={"content_sha256": work_manifest_content_sha256(swapped)})
    return (swapped.model_dump_json(indent=2) + "\n").encode("utf-8")


def test_execute_refuses_a_work_manifest_swapped_after_verification(
    scenario: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-3 review fix (2026-08-23, P1 finding 2): verification and the
    authority hash were DIFFERENT reads of the work-manifest path.

    Attack: verify manifest A, atomically swap in unverifiable manifest B
    before the hash read, approval names B's hash — execute used to consume
    B (its later re-parse read the path again, without full verification).
    The bytes are now read ONCE: the verified bytes are hashed and consumed,
    so the approval-bound hash can only be the hash of the VERIFIED bytes —
    B's hash no longer matches anything that was verified, and the launch
    refuses with exit 6 before any authority is spent."""
    swapped = _swapped_manifest_bytes(scenario)
    # The approval names the SWAPPED manifest's file hash.
    _approve(scenario, work_manifest_sha256=hashlib.sha256(swapped).hexdigest())
    real_sha = launch._sha256_file

    def sha_with_swap(path: Path) -> str:
        # The attacker's atomic swap lands on the SECOND read of the
        # work-manifest path (between verify and hash in the old code).
        if path == scenario["work_manifest"]:
            path.write_bytes(swapped)
        return real_sha(path)

    monkeypatch.setattr(launch, "_sha256_file", sha_with_swap)

    calls: list[str] = []
    code, summary = _execute(scenario, runner=_fake_runner(scenario, calls))
    assert code == 6 and summary is None
    assert calls == []
    # nothing was consumed and the store never left BARS_READY
    view = read_bars_ledger(scenario["authority_root"])
    assert [r.kind for r in view.records] == ["BARS_LAUNCH_APPROVAL"]
    store = RunStore.open(scenario["store_root"], str(scenario["run_id"]))
    assert store.state is RunState.BARS_READY


def test_execute_reads_the_work_manifest_bytes_exactly_once(
    scenario: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structural bytes-once (round-3 finding 2): across the whole execute,
    the work-manifest path is read exactly ONCE — the hash bound into the
    consumption is the hash of the verified bytes BY CONSTRUCTION, not
    because a second read happened to agree."""
    _approve(scenario)
    reads: list[bytes] = []
    real_read = Path.read_bytes

    def counting_read(self: Path) -> bytes:
        data = real_read(self)
        if self == scenario["work_manifest"]:
            reads.append(data)
        return data

    monkeypatch.setattr(Path, "read_bytes", counting_read)
    calls: list[str] = []

    def runner(context) -> str:
        calls.append("runner")
        return "plain-runner-ok"

    code, _ = _execute(scenario, runner=runner)
    assert code == 0 and calls == ["runner"]
    assert len(reads) == 1
    # ... and the single read's bytes are what the consumption binds
    view = read_bars_ledger(scenario["authority_root"])
    consumed = next(r for r in view.records if r.kind == KIND_BARS_LAUNCH_CONSUMED)
    assert consumed.work_manifest_sha256 == hashlib.sha256(reads[0]).hexdigest()


# ---- round-5 (finding 4): regeneration consumes the bytes verification hashed -------
#
# Round-5 review fix (2026-08-24): rebuild_master_captures verified the
# capture manifest (hashing the masters) and then re-read master bytes from
# DISK. A swap in that window fed the selection different bytes than the
# sealed manifest pins — and with semantically-identical swapped bytes the
# regenerated manifest still verified, so authority was consumed against a
# capture dir that no longer matched its manifest.


def test_execute_refuses_a_master_swapped_after_manifest_verification(
    scenario: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attack: every read of the SPY master AFTER the verification read
    returns semantically-identical, byte-different JSON (one trailing
    newline). The regeneration read must re-hash against the manifest's
    pinned sha and refuse — exit 6 before any authority moves."""
    _approve(scenario)
    master = scenario["capture_dir"] / "masters" / "spy_2025-03-05.json"
    original = master.read_bytes()
    swapped = original + b"\n"
    assert swapped != original
    reads = {"master": 0}
    real_read = Path.read_bytes

    def read_bytes_swapping(self: Path) -> bytes:
        if self == master:
            reads["master"] += 1
            # read #1 is verify_massive_capture_manifest's hash read; every
            # later read is the attacker's swap landing in the window.
            return original if reads["master"] == 1 else swapped
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes_swapping)

    calls: list[str] = []
    code, summary = _execute(scenario, runner=_fake_runner(scenario, calls))
    assert code == 6, "the swapped master must refuse the work-manifest gate"
    assert summary is None and calls == []
    view = read_bars_ledger(scenario["authority_root"])
    assert [r.kind for r in view.records] == ["BARS_LAUNCH_APPROVAL"], (
        "authority was consumed against a capture dir that no longer matches its manifest"
    )
    store = RunStore.open(scenario["store_root"], str(scenario["run_id"]))
    assert store.state is RunState.BARS_READY


def test_cli_execute_refused_exit_10_touches_nothing(
    scenario: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _approve(scenario)
    before = _tree_state(scenario["authority_root"], scenario["store_root"])
    code = launch.main(["--execute", *_argv(scenario)])
    assert code == 10
    assert "no bars-era runner" in capsys.readouterr().err
    assert _tree_state(scenario["authority_root"], scenario["store_root"]) == before
    store = RunStore.open(scenario["store_root"], str(scenario["run_id"]))
    assert store.state is RunState.BARS_READY

"""The window-A extension surface: per-window bindings, the ext-1 date
scope, and the guards that keep the spent window-A packet sealed.

Owner direction 2026-09-04 (plan approved): the five sealed dates window A
never evaluated (2026-07-17..2026-08-14 — not label-complete on the
window-A world) become evaluable once the world grows, as a NEW packet
under a NEW authority. These tests own the separation:

- the spent window-A binding is the module default and is UNCHANGED (the
  tracked registration/approval/consumption/evidence keep refusing a
  second look through it);
- ``window-a-ext-1`` rebinds EVERY driver surface (paths, window id,
  program id, run indices) — no extension artifact may land on a
  window-A path and vice versa;
- the extension's date scope is DERIVED from the spent packet: the sealed
  dates MINUS window A's registered permitted set, and only after the
  tracked window-A evidence exists (the base window must be consumed
  first — an extension before the base look is structurally refuseable);
- the runner-side authority shape accepts the ratified extension window
  id and still refuses every other id.

The launcher re-open (REQUIRED_BARS_PROTOCOL_VERSION 0.2.2 for the
continuation capture) and the work-manifest build wrapper live in their
own tests at the bottom.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]

from tree_options.protocol.holdout import (  # noqa: E402
    FINAL_HOLDOUT_DATES,
    FINAL_HOLDOUT_WINDOW_ID,
    RATIFIED_HOLDOUT_WINDOW_IDS,
)

_spec = importlib.util.spec_from_file_location(
    "run_p4_holdout", REPO / "scripts" / "run_p4_holdout.py"
)
p4 = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("run_p4_holdout", p4)
assert _spec.loader is not None
_spec.loader.exec_module(p4)

# the window-A registration's registered permitted set (tracked, committed
# at 03de5330; the approval bound it and the single execute verified it) —
# the extension scope is FINAL_HOLDOUT_DATES minus exactly these
_WINDOW_A_PERMITTED = (
    "2026-05-08",
    "2026-05-15",
    "2026-05-22",
    "2026-05-29",
    "2026-06-05",
    "2026-06-12",
    "2026-06-26",
    "2026-07-10",
)
_EXT_SCOPE = tuple(d for d in FINAL_HOLDOUT_DATES if d not in _WINDOW_A_PERMITTED)
assert _EXT_SCOPE == (
    "2026-07-17",
    "2026-07-24",
    "2026-07-31",
    "2026-08-07",
    "2026-08-14",
)


@pytest.fixture()
def restore_binding():
    """Snapshot every binding global and restore window A after the test —
    a leaked ext binding would silently re-target later tests' paths."""
    snapshot = {name: getattr(p4, name) for name in p4._BINDING_GLOBALS}
    try:
        yield
    finally:
        for name, value in snapshot.items():
            setattr(p4, name, value)


def _ext_bound(monkeypatch, tmp_path):
    """Bind the extension window with every path under a scratch tree and
    the window-A base records pointing at synthetic tracked files."""
    p4._bind_window(p4.WINDOW_A_EXT_1)
    monkeypatch.setattr(p4, "P4_ROOT", tmp_path / "artifacts" / "theory" / "p4-ext-1")
    monkeypatch.setattr(
        p4,
        "REGISTRATION_PATH",
        tmp_path / "docs" / "theory" / "p4-window-a-ext-1-registration.json",
    )
    monkeypatch.setattr(
        p4, "STATE_PATH", tmp_path / "artifacts" / "theory" / "p4-ext-1" / "state.json"
    )
    monkeypatch.setattr(
        p4, "REGISTRY_PATH", tmp_path / "artifacts" / "theory" / "p4-ext-1" / "registry.db"
    )
    monkeypatch.setattr(p4, "TRIALS_DIR", tmp_path / "artifacts" / "theory" / "p4-ext-1" / "trials")
    monkeypatch.setattr(
        p4, "SCRATCH_ROOT", tmp_path / "artifacts" / "theory" / "p4-ext-1" / "scratch"
    )
    monkeypatch.setattr(p4, "AUTHORITY_ROOT", tmp_path / "artifacts" / "p4-authority-ext-1")
    monkeypatch.setattr(
        p4, "APPROVAL_PATH", tmp_path / "artifacts" / "p4-authority-ext-1" / "approval.json"
    )
    monkeypatch.setattr(
        p4,
        "CONSUMPTION_PATH",
        tmp_path / "artifacts" / "p4-authority-ext-1" / "consumption.jsonl",
    )
    monkeypatch.setattr(
        p4, "VERDICT_PATH", tmp_path / "artifacts" / "theory" / "p4-ext-1" / "verdict.json"
    )
    monkeypatch.setattr(
        p4,
        "EVIDENCE_PATH",
        tmp_path / "docs" / "evidence-logs" / "m4" / "m4-p4-window-a-ext-1.json",
    )
    _synthetic_window_a_base(monkeypatch, tmp_path)


def _synthetic_window_a_base(monkeypatch, tmp_path) -> None:
    """The never-rebound window-A base records in their CANONICAL shape:
    a committed-clean registration carrying the window-A identity and the
    spent permitted set, plus the tracked evidence corroborating both."""
    base_registration = tmp_path / "window-a-registration.json"
    base_registration.write_text(
        json.dumps(
            {
                "program": "p4-window-a",
                "evaluation_window": {
                    "window_id": "final-holdout-window-a",
                    "expected_permitted": list(_WINDOW_A_PERMITTED),
                },
            }
        ),
        encoding="utf-8",
    )
    base_evidence = tmp_path / "m4-p4-window-a.json"
    base_evidence.write_text(
        json.dumps(
            {
                "program": "p4-window-a",
                "permitted_test_sessions": list(_WINDOW_A_PERMITTED),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(p4, "WINDOW_A_REGISTRATION_PATH", base_registration)
    monkeypatch.setattr(p4, "WINDOW_A_EVIDENCE_PATH", base_evidence)
    # the base registration reads as COMMITTED-CLEAN at HEAD (the git
    # round-trip is stubbed; the bytes comparison stays live)
    monkeypatch.setattr(p4, "_committed_file_bytes", lambda path: path.read_bytes())


# ---- the window configurations ----------------------------------------------------


def test_window_a_is_the_module_default_and_unchanged() -> None:
    """The spent packet's binding is what the module loads as: every global
    the extension could move still points at the window-A surface, so the
    tracked registration/approval/consumption/evidence refuse a second
    look through the DEFAULT invocation exactly as before."""
    assert p4.P4_WINDOW_ID == FINAL_HOLDOUT_WINDOW_ID
    assert p4.P4_PROGRAM_ID == "p4-window-a"
    assert p4.REGISTRATION_PATH == REPO / "docs" / "theory" / "p4-window-a-registration.json"
    assert p4.EVIDENCE_PATH == REPO / "docs" / "evidence-logs" / "m4" / "m4-p4-window-a.json"
    assert p4.APPROVAL_PATH == REPO / "artifacts" / "p4-authority" / "approval.json"
    assert p4.P4_ROOT == REPO / "artifacts" / "theory" / "p4"
    assert p4._ACTIVE_WINDOW is p4.WINDOW_A
    menu = p4.p4_menu()
    assert sorted(c.run_index for c in menu if c.arm == "A") == [15, 16, 17, 18, 19]
    assert [c.run_index for c in menu if c.arm == "B"] == [3]


def test_the_extension_rebinds_every_surface(restore_binding, monkeypatch, tmp_path) -> None:
    """window-a-ext-1 moves EVERY binding global: paths, window id, program
    id, and the per-arm run-index namespaces — an extension artifact can
    never land on a window-A path, and the extension's trials cannot reuse
    the spent packet's run indices."""
    window_a_paths = {
        name: getattr(p4, name)
        for name in (
            "REGISTRATION_PATH",
            "STATE_PATH",
            "REGISTRY_PATH",
            "TRIALS_DIR",
            "SCRATCH_ROOT",
            "AUTHORITY_ROOT",
            "APPROVAL_PATH",
            "CONSUMPTION_PATH",
            "VERDICT_PATH",
            "EVIDENCE_PATH",
        )
    }
    # bind WITHOUT re-pointing paths at a scratch tree: this test owns the
    # real binding values themselves
    _synthetic_window_a_base(monkeypatch, tmp_path)
    p4._bind_window(p4.WINDOW_A_EXT_1)
    assert p4.P4_WINDOW_ID == "final-holdout-window-a-ext-1"
    assert p4.P4_PROGRAM_ID == "p4-window-a-ext-1"
    ext = p4.WINDOW_A_EXT_1
    for name, window_a_value in window_a_paths.items():
        bound = getattr(p4, name)
        assert bound != window_a_value, name
    # each rebound path is the EXTENSION window's own surface, verbatim
    assert p4.REGISTRATION_PATH == ext.registration_path
    assert p4.STATE_PATH == ext.state_path
    assert p4.REGISTRY_PATH == ext.registry_path
    assert p4.TRIALS_DIR == ext.trials_dir
    assert p4.SCRATCH_ROOT == ext.scratch_root
    assert p4.AUTHORITY_ROOT == ext.authority_root
    assert p4.APPROVAL_PATH == ext.approval_path
    assert p4.CONSUMPTION_PATH == ext.consumption_path
    assert p4.VERDICT_PATH == ext.verdict_path
    assert p4.EVIDENCE_PATH == ext.evidence_path
    assert p4.P4_ROOT == ext.p4_root
    menu = p4.p4_menu()
    assert sorted(c.run_index for c in menu if c.arm == "A") == [20, 21, 22, 23, 24]
    assert [c.run_index for c in menu if c.arm == "B"] == [4]
    assert p4._ACTIVE_WINDOW is p4.WINDOW_A_EXT_1


def test_binding_back_to_window_a_restores_the_spent_surface(restore_binding) -> None:
    """Rebinding is reversible and complete — after an ext-1 bind, binding
    window A again reproduces the module-load state field for field."""
    p4._bind_window(p4.WINDOW_A_EXT_1)
    p4._bind_window(p4.WINDOW_A)
    assert p4.P4_WINDOW_ID == FINAL_HOLDOUT_WINDOW_ID
    assert p4.REGISTRATION_PATH == REPO / "docs" / "theory" / "p4-window-a-registration.json"
    assert p4._ACTIVE_WINDOW is p4.WINDOW_A
    assert [c.run_index for c in p4.p4_menu() if c.arm == "A"] == [15, 16, 17, 18, 19]


# ---- the derived extension date scope ----------------------------------------------


def test_window_a_scope_is_every_sealed_date(monkeypatch, tmp_path) -> None:
    """Window A evaluates any label-complete sealed date (its own excluded
    dates were merely not label-complete in world) — the default scope is
    the full enumeration, unchanged."""
    _synthetic_window_a_base(monkeypatch, tmp_path)
    assert p4._active_date_scope() == frozenset(FINAL_HOLDOUT_DATES)


def test_the_extension_requires_the_consumed_base(monkeypatch, tmp_path, restore_binding) -> None:
    """No tracked window-A evidence — no extension: the five remaining
    dates may only be evaluated AFTER the base window was spent (the
    extension is a second look at the same seal, never a first look at a
    wider one)."""
    _ext_bound(monkeypatch, tmp_path)
    p4.WINDOW_A_EVIDENCE_PATH.unlink()
    with pytest.raises(SystemExit, match="window-A evidence"):
        p4._active_date_scope()


def test_the_extension_scope_is_the_unconsumed_remainder(
    monkeypatch, tmp_path, restore_binding
) -> None:
    """The scope derives from the BASE window's tracked registration — the
    sealed enumeration minus exactly what window A consumed; the five
    excluded dates and nothing else."""
    _ext_bound(monkeypatch, tmp_path)
    assert p4._active_date_scope() == frozenset(_EXT_SCOPE)


def test_a_corrupt_base_registration_refuses_the_scope(
    monkeypatch, tmp_path, restore_binding
) -> None:
    """A base registration naming a date outside the sealed enumeration is
    corrupt (it cannot be the tracked window-A registration) — the scope
    derivation refuses rather than deriving a widened or narrowed set."""
    _ext_bound(monkeypatch, tmp_path)
    foreign = ["2027-01-01", *list(_WINDOW_A_PERMITTED)]
    p4.WINDOW_A_REGISTRATION_PATH.write_text(
        json.dumps(
            {
                "program": "p4-window-a",
                "evaluation_window": {
                    "window_id": "final-holdout-window-a",
                    "expected_permitted": foreign,
                },
            }
        ),
        encoding="utf-8",
    )
    # the evidence AGREES with the forged list — the sealed-enumeration
    # check must be the refusal even under a self-consistent forgery
    p4.WINDOW_A_EVIDENCE_PATH.write_text(
        json.dumps({"program": "p4-window-a", "permitted_test_sessions": foreign}),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="not sealed"):
        p4._active_date_scope()
    p4.WINDOW_A_REGISTRATION_PATH.unlink()
    with pytest.raises(SystemExit, match="window-A registration"):
        p4._active_date_scope()


# ---- the Codex round-1 fix round ---------------------------------------------------


def test_extension_refuses_a_partial_tranche(monkeypatch, tmp_path, restore_binding) -> None:
    """(F1) The extension is all-or-nothing: on a world grown through
    2026-09-04 exactly three of the five scoped dates are label-complete —
    registering then would consume ext-1 for those three and STRAND
    08-07/08-14 forever (the window is one-shot). The machinery refuses,
    naming the immature dates."""
    _ext_bound(monkeypatch, tmp_path)
    partial = _World(date(2026, 9, 4))  # matures 07-17/07-24/07-31 only
    with pytest.raises(SystemExit, match="all-or-nothing") as exc_info:
        p4._world_permitted(partial)
    assert "2026-08-07" in str(exc_info.value) and "2026-08-14" in str(exc_info.value)


def test_the_base_registration_must_be_committed_clean(
    monkeypatch, tmp_path, restore_binding
) -> None:
    """(F2) The scope derives from the CANONICAL spent packet: a base
    registration whose on-disk bytes differ from HEAD's committed bytes
    (an offline rewrite) refuses — never a working-tree forgery."""
    _ext_bound(monkeypatch, tmp_path)
    monkeypatch.setattr(p4, "_committed_file_bytes", lambda path: b"stale\n")
    with pytest.raises(SystemExit, match="committed-clean"):
        p4._active_date_scope()
    monkeypatch.setattr(p4, "_committed_file_bytes", lambda path: None)
    with pytest.raises(SystemExit, match="committed-clean"):
        p4._active_date_scope()


def test_the_base_registration_must_carry_the_window_a_identity(
    monkeypatch, tmp_path, restore_binding
) -> None:
    """(F2) Bytes alone are not identity: a committed-clean registration
    that does not carry the window-A program/window identity is some other
    file — the scope refuses to derive from it."""
    _ext_bound(monkeypatch, tmp_path)
    for body in (
        {
            "program": "p4-window-a-ext-1",
            "evaluation_window": {
                "window_id": "final-holdout-window-a",
                "expected_permitted": list(_WINDOW_A_PERMITTED),
            },
        },
        {
            "program": "p4-window-a",
            "evaluation_window": {
                "window_id": "some-other-window",
                "expected_permitted": list(_WINDOW_A_PERMITTED),
            },
        },
    ):
        p4.WINDOW_A_REGISTRATION_PATH.write_text(json.dumps(body), encoding="utf-8")
        with pytest.raises(SystemExit, match="window-A program/window identity"):
            p4._active_date_scope()


def test_the_base_evidence_must_corroborate_the_registration(
    monkeypatch, tmp_path, restore_binding
) -> None:
    """(F2) The base-consumed record must AGREE with the base registration
    — an evidence file naming a different permitted set (or a different
    program) means the two base records disagree; the scope refuses
    rather than trusting either list alone."""
    _ext_bound(monkeypatch, tmp_path)
    p4.WINDOW_A_EVIDENCE_PATH.write_text(
        json.dumps({"program": "p4-window-a", "permitted_test_sessions": ["2026-05-08"]}),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="corroborate"):
        p4._active_date_scope()
    p4.WINDOW_A_EVIDENCE_PATH.write_text(
        json.dumps(
            {
                "program": "p4-window-a-ext-1",
                "permitted_test_sessions": list(_WINDOW_A_PERMITTED),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="corroborate"):
        p4._active_date_scope()


def test_a_preplanted_root_alias_refuses_the_binding(tmp_path, restore_binding) -> None:
    """(F3) A preplanted symlink on the extension's root path routes
    extension writes into the SPENT packet's tree — the bind-time walk
    refuses any existing symlinked component."""
    import dataclasses as _dc
    import shutil as _shutil
    import uuid as _uuid

    scratch = REPO / "artifacts" / "ext-alias-tests" / _uuid.uuid4().hex
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        aliased = scratch / "p4-ext-1"
        aliased.symlink_to(REPO / "artifacts" / "theory" / "p4")
        window = _dc.replace(p4.WINDOW_A_EXT_1, p4_root=aliased)
        with pytest.raises(SystemExit, match="symlink"):
            p4._bind_window(window)
    finally:
        _shutil.rmtree(scratch, ignore_errors=True)


def test_a_volatile_root_refuses_the_binding(restore_binding) -> None:
    """(F3) A window whose artifact root resolves under /tmp refuses at
    bind — durable authority may not live where a reboot wipes it."""
    import dataclasses as _dc

    window = _dc.replace(p4.WINDOW_A_EXT_1, p4_root=Path("/tmp") / "ext-volatile")
    with pytest.raises(SystemExit, match="volatile root"):
        p4._bind_window(window)


def test_malformed_permitted_elements_refuse_cleanly(
    monkeypatch, tmp_path, restore_binding
) -> None:
    """(F4) A non-string permitted element is a controlled REFUSED — never
    the TypeError the frozenset-membership regression raised (the default
    window-A validation must keep refusing exactly as it did before the
    extension existed)."""
    for bind_ext in (False, True):
        if bind_ext:
            _ext_bound(monkeypatch, tmp_path)
        else:
            _synthetic_window_a_base(monkeypatch, tmp_path)
        record = _approval_record(
            window_id="final-holdout-window-a-ext-1" if bind_ext else "final-holdout-window-a",
            permitted_test_sessions=[{"not": "a date"}],
        )
        with pytest.raises(SystemExit, match="REFUSED"):
            p4._validate_approval_record(record)


def test_the_verdict_rule_follows_the_window() -> None:
    """(F6) The verdict's self-describing rule is the ACTIVE window's:
    window A keeps the exact ruling text of the spent packet (pinned to
    p4_verdict's default), and the extension carries its own direction —
    extension evidence never labels itself with the base window's
    ruling."""
    from tree_options.trials.p4_verdict import _VERDICT_RULE

    assert p4.WINDOW_A.verdict_rule == _VERDICT_RULE
    assert p4.WINDOW_A_EXT_1.verdict_rule != _VERDICT_RULE
    assert "extension" in p4.WINDOW_A_EXT_1.verdict_rule
    assert "2026-09-04" in p4.WINDOW_A_EXT_1.verdict_rule


def test_the_approval_next_names_the_active_window(
    monkeypatch, tmp_path, restore_binding, capsys
) -> None:
    """(F8) The printed follow-on command names the ACTIVE window —
    following a window-A-spelled instruction after approving the
    extension would invoke the spent default binding and refuse."""
    _ext_bound(monkeypatch, tmp_path)
    registration = {
        "program": "p4-window-a-ext-1",
        "world_id": "massive-derived/FIXTURE/grown",
        "protocol_hash": "p" * 64,
        "dataset_manifest_hash": "m" * 64,
        "evaluation_window": {
            "window_id": "final-holdout-window-a-ext-1",
            "expected_permitted": list(_EXT_SCOPE),
        },
        "slots": [
            {
                "slot_id": config.slot_id,
                "params_key": [str(part) for part in config.params_key()],
                "hypothesis": config.hypothesis,
                "run_index": config.run_index,
            }
            for config in p4.p4_menu()
        ],
    }
    raw = p4._registration_bytes(registration)
    p4.REGISTRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    p4.REGISTRATION_PATH.write_bytes(raw)
    p4.save_state({"registration_sha256": p4._sha256_bytes(raw), "executions": [], "verdict": None})
    monkeypatch.setattr(p4, "_git_head", lambda repo: "d" * 40)
    monkeypatch.setattr(p4, "_require_committed_registration", lambda head: None)
    assert p4.approve("d" * 40, "the owner's extension declaration") == 0
    err = capsys.readouterr().out
    assert "--window window-a-ext-1 --execute" in err


class _World:
    """A world whose grid is the sealed enumeration preceded by research
    Fridays and (for grown worlds) followed by the capture-extension
    Fridays through the last bar session; the last bar session pins the
    label-completeness horizon."""

    def __init__(self, world_last: date) -> None:
        self._last = world_last
        self.world_id = "massive-derived/AAPL+ADBE+AMD+AMZN+AVGO/fixture-grown-world"
        self.dataset = type("D", (), {"bars": (type("B", (), {"session": world_last})(),)})()
        self.grid = self

    def sessions(self):
        from datetime import timedelta

        research = tuple(
            date.fromisoformat(d) for d in ("2026-02-06", "2026-02-13", "2026-02-20", "2026-02-27")
        )
        sealed = tuple(date.fromisoformat(d) for d in FINAL_HOLDOUT_DATES)
        grown = []
        step = date(2026, 8, 21)  # the first Friday past the sealed enumeration
        while step <= self._last:
            grown.append(step)
            step += timedelta(weeks=1)
        return research + sealed + tuple(grown)


def test_extension_permitted_filters_out_window_a_dates(
    monkeypatch, tmp_path, restore_binding
) -> None:
    """A fully-grown world (every sealed date label-complete) still yields
    ONLY the five unconsumed dates under the extension binding — window
    A's dates are spent and can never ride a second evaluation."""
    _ext_bound(monkeypatch, tmp_path)
    grown = _World(date(2026, 9, 25))  # 5+ Fridays past 2026-08-14
    assert tuple(d.isoformat() for d in p4._world_permitted(grown)) == _EXT_SCOPE


def test_extension_refuses_when_no_scoped_date_is_label_complete(
    monkeypatch, tmp_path, restore_binding
) -> None:
    """Today's world shape (last session 2026-08-14): the eight window-A
    dates are label-complete but OUT of scope and the five in-scope dates
    are not — the extension registration refuses with the grow-the-world
    message instead of evaluating a padded or borrowed set."""
    _ext_bound(monkeypatch, tmp_path)
    current = _World(date(2026, 8, 14))
    with pytest.raises(SystemExit, match="grow the world"):
        p4._world_permitted(current)


def test_window_a_permitted_is_unchanged_by_the_scope_machinery(monkeypatch, tmp_path) -> None:
    """The default binding's permitted set is exactly the pre-extension
    computation (the eight label-complete dates on the current world) —
    the scope filter is a no-op for window A."""
    _synthetic_window_a_base(monkeypatch, tmp_path)
    current = _World(date(2026, 8, 14))
    assert tuple(d.isoformat() for d in p4._world_permitted(current)) == _WINDOW_A_PERMITTED


# ---- registration + approval under the extension binding ---------------------------


def _fake_world_builder(world_last: date, manifest_hash: str = "m" * 64):
    def _build():
        return _World(world_last), type("P", (), {})(), manifest_hash

    return _build


def test_extension_registration_records_its_own_identity(
    monkeypatch, tmp_path, restore_binding
) -> None:
    """--register-only under the extension writes the EXT registration:
    its own program/window ids, the ext run indices, the derived scope as
    expected_permitted, and the still-immature dates as excluded."""
    _ext_bound(monkeypatch, tmp_path)
    monkeypatch.setattr(p4, "_build_world", _fake_world_builder(date(2026, 9, 25)))
    monkeypatch.setattr(p4, "_git_head", lambda repo: "d" * 40)
    monkeypatch.setattr(p4, "protocol_hash", lambda protocol: "b" * 64)
    assert p4.register_only() == 0
    registration = json.loads(p4.REGISTRATION_PATH.read_text(encoding="utf-8"))
    assert registration["program"] == "p4-window-a-ext-1"
    window = registration["evaluation_window"]
    assert window["window_id"] == "final-holdout-window-a-ext-1"
    assert window["expected_permitted"] == list(_EXT_SCOPE)
    assert window["expected_excluded"] == []
    assert sorted(row["run_index"] for row in registration["slots"] if row["arm"] == "A") == [
        20,
        21,
        22,
        23,
        24,
    ]
    assert [row["run_index"] for row in registration["slots"] if row["arm"] == "B"] == [4]
    # the extension's state is its OWN state file, not window A's
    state = json.loads(p4.STATE_PATH.read_text(encoding="utf-8"))
    assert state["registration_sha256"]


def test_extension_registration_refuses_on_the_current_world(
    monkeypatch, tmp_path, restore_binding
) -> None:
    """Registering the extension before the world grows refuses — no
    registration artifact, no state."""
    _ext_bound(monkeypatch, tmp_path)
    monkeypatch.setattr(p4, "_build_world", _fake_world_builder(date(2026, 8, 14)))
    monkeypatch.setattr(p4, "_git_head", lambda repo: "d" * 40)
    with pytest.raises(SystemExit, match="grow the world"):
        p4.register_only()
    assert not p4.REGISTRATION_PATH.exists()
    assert not p4.STATE_PATH.is_file()


def _approval_record(**overrides) -> dict[str, Any]:
    base = {
        "kind": "P4_HOLDOUT_APPROVAL",
        "window_id": "final-holdout-window-a-ext-1",
        "world_id": "massive-derived/AAPL+ADBE+AMD+AMZN+AVGO/grown-world-identity",
        "protocol_hash": "b" * 64,
        "dataset_manifest_hash": "c" * 64,
        "registration_sha256": "d" * 64,
        "declared_head": "d" * 40,
        "permitted_test_sessions": list(_EXT_SCOPE),
        "reason": "the owner's window-A extension declaration",
        "at_epoch": 1_700_000_000,
    }
    base.update(overrides)
    return base


def test_extension_approval_validates_against_the_ext_scope(
    monkeypatch, tmp_path, restore_binding
) -> None:
    """An extension approval permitting the full ext scope validates; one
    that smuggles a WINDOW-A-CONSUMED date into the permitted set refuses
    by name (those dates are spent — the one-shot the seal exists for)."""
    _ext_bound(monkeypatch, tmp_path)
    p4._validate_approval_record(_approval_record())
    with pytest.raises(SystemExit, match="consumed by window A"):
        p4._validate_approval_record(
            _approval_record(permitted_test_sessions=["2026-05-08", *_EXT_SCOPE])
        )
    with pytest.raises(SystemExit, match="names window"):
        p4._validate_approval_record(_approval_record(window_id="final-holdout-window-a"))


def test_window_a_approval_still_validates_unchanged(monkeypatch, tmp_path) -> None:
    """The default binding accepts the window-A approval shape exactly as
    before the extension existed (the spent packet's --verdict re-read
    keeps working)."""
    _synthetic_window_a_base(monkeypatch, tmp_path)
    record = _approval_record(
        window_id="final-holdout-window-a",
        permitted_test_sessions=list(_WINDOW_A_PERMITTED),
        reason="the owner's window-A declaration",
    )
    assert p4._validate_approval_record(record) is record


# ---- the committed-registration path follows the binding ---------------------------


def test_committed_registration_check_uses_the_active_window_path(
    monkeypatch, tmp_path, restore_binding
) -> None:
    """The git committed-bytes check names the ACTIVE window's registration
    path — an ext approval can never be satisfied by the tracked window-A
    registration being committed (and vice versa)."""
    _ext_bound(monkeypatch, tmp_path)
    monkeypatch.setattr(p4, "REPO_ROOT", tmp_path)
    registration = {"program": "p4-window-a-ext-1"}
    p4.REGISTRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    raw = p4._registration_bytes(registration)
    p4.REGISTRATION_PATH.write_bytes(raw)
    argv_seen: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = raw

    def _fake_run(argv, **kwargs):
        argv_seen.append(list(argv))
        return _Result()

    monkeypatch.setattr(p4.subprocess, "run", _fake_run)
    p4._require_committed_registration("d" * 40)
    assert argv_seen, "the check must consult git"
    rel = "docs/theory/p4-window-a-ext-1-registration.json"
    assert any(rel in part for argv in argv_seen for part in argv), argv_seen


def test_the_trials_prefix_follows_the_binding(restore_binding, monkeypatch, tmp_path) -> None:
    """The verdict's artifact-path prefix is derived from the ACTIVE
    trials directory — extension evidence lives under p4-ext-1/trials and
    a window-A-shaped path is not extension evidence."""
    _ext_bound(monkeypatch, tmp_path)
    monkeypatch.setattr(p4, "REPO_ROOT", tmp_path)
    prefix = p4._trials_rel_prefix()
    assert prefix == "artifacts/theory/p4-ext-1/trials/"
    assert prefix != "artifacts/theory/p4/trials/"


# ---- the runner-side authority accepts the ratified extension -----------------------


def test_the_authority_shape_accepts_the_ratified_extension_id() -> None:
    """The extension window id is a ratified holdout window: the authority
    shape check accepts it (the binding checks against the trial's own
    world/protocol still apply at the call site)."""
    from tree_options.trials.options_run import (
        HoldoutEvaluationAuthority,
        _validate_holdout_authority_shape,
    )

    assert set(RATIFIED_HOLDOUT_WINDOW_IDS) == {
        "final-holdout-window-a",
        "final-holdout-window-a-ext-1",
    }
    authority = HoldoutEvaluationAuthority(
        window_id="final-holdout-window-a-ext-1",
        world_id="massive-derived/AAPL+ADBE+AMD+AMZN+AVGO/grown-world-identity",
        protocol_hash_value="b" * 64,
        registration_sha256="d" * 64,
        authority_record_sha256="e" * 64,
        declared_head="d" * 40,
        permitted_test_sessions=tuple(date.fromisoformat(d) for d in _EXT_SCOPE),
    )
    _validate_holdout_authority_shape(authority)  # does not raise
    stranger = HoldoutEvaluationAuthority(
        **{**authority.__dict__, "window_id": "final-holdout-window-b"}
    )
    with pytest.raises(ValueError, match="not a ratified"):
        _validate_holdout_authority_shape(stranger)


# ---- the launcher re-open + the work-manifest build wrapper ------------------------


def test_the_launcher_requires_the_current_protocol_version() -> None:
    """(window-A extension continuation) the bars launcher's protocol gate
    is re-opened at the LIVE protocol version 0.2.2 (the 0.2.1 freeze was
    the closed-era state; the continuation approval binds the 0.2.2 hash,
    which the owner ratified in the 0.2.2 flip)."""
    import launch_bars_era as launch
    from tree_options.protocol.loader import load_protocol

    live = load_protocol(REPO / "research_protocol.yaml")
    assert live.meta.protocol_version == launch.REQUIRED_BARS_PROTOCOL_VERSION == "0.2.2"


def test_the_work_manifest_wrapper_builds_a_verifiable_manifest(tmp_path, monkeypatch) -> None:
    """scripts/build_bars_work_manifest.py turns the library-only builder
    into the owner-runnable continuation step: from a capture dir + the
    committed profile + the census-bound capture manifest it writes the
    byte-exact manifest the launcher's preflight verifies."""
    from tests.fixtures.bars_sample import write_bars_capture, write_capture_manifest

    capture_dir = write_bars_capture(tmp_path / "capture")
    manifest_path = write_capture_manifest(capture_dir, capture_dir / "capture_manifest.json")
    out_path = tmp_path / "work-manifest.json"
    argv = [
        "--capture-dir",
        str(capture_dir),
        "--capture-manifest",
        str(manifest_path),
        "--budget",
        "45",
        "--out",
        str(out_path),
    ]
    exit_code = p4_work_manifest_main(argv)
    assert exit_code == 0, out_path.read_text(encoding="utf-8") if out_path.exists() else ""
    from tree_options.data.bars_manifest import (
        build_bars_work_manifest,
        load_selection_profile,
        parse_bars_work_manifest,
        verify_bars_work_manifest,
    )

    library = build_bars_work_manifest(
        capture_dir,
        profile=load_selection_profile(REPO / "data" / "bars" / "selection-profile.json"),
        capture_manifest=manifest_path,
        budget_limit=45,
    )
    raw = out_path.read_bytes()
    parsed = parse_bars_work_manifest(raw, source=str(out_path))
    verify_bars_work_manifest(
        parsed,
        profile=load_selection_profile(REPO / "data" / "bars" / "selection-profile.json"),
        capture_manifest_sha256=library.capture_manifest_sha256,
        capture_dir=capture_dir,
    )
    assert parsed.content_sha256 == library.content_sha256
    assert json.loads(raw)["cost"]["budget_covers_worst_case"] is True


def test_the_work_manifest_wrapper_is_write_once(tmp_path) -> None:
    """The wrapper never overwrites --out: a rebuilt manifest is a NEW
    manifest (different content hash) and an approval may already bind the
    existing file's bytes — a silent replace would strand that approval."""
    from tests.fixtures.bars_sample import write_bars_capture, write_capture_manifest

    capture_dir = write_bars_capture(tmp_path / "capture")
    manifest_path = write_capture_manifest(capture_dir, capture_dir / "capture_manifest.json")
    out_path = tmp_path / "work-manifest.json"
    argv = [
        "--capture-dir",
        str(capture_dir),
        "--capture-manifest",
        str(manifest_path),
        "--budget",
        "45",
        "--out",
        str(out_path),
    ]
    assert p4_work_manifest_main(argv) == 0
    first = out_path.read_bytes()
    assert p4_work_manifest_main(argv) == 2
    assert out_path.read_bytes() == first


def test_the_work_manifest_wrapper_refuses_an_uncoverable_budget(tmp_path) -> None:
    """A budget that cannot pre-charge the worst-case wire requests
    refuses at BUILD time — before any approval could bind an uncoverable
    grid — and writes nothing."""
    from tests.fixtures.bars_sample import write_bars_capture, write_capture_manifest

    capture_dir = write_bars_capture(tmp_path / "capture")
    manifest_path = write_capture_manifest(capture_dir, capture_dir / "capture_manifest.json")
    out_path = tmp_path / "work-manifest.json"
    argv = [
        "--capture-dir",
        str(capture_dir),
        "--capture-manifest",
        str(manifest_path),
        "--budget",
        "1",
        "--out",
        str(out_path),
    ]
    assert p4_work_manifest_main(argv) == 4
    assert not out_path.exists()


def p4_work_manifest_main(argv: list[str]) -> int:
    """Load scripts/build_bars_work_manifest.py by path (the wave/p4
    script-loading pattern) and run its main."""
    spec = importlib.util.spec_from_file_location(
        "build_bars_work_manifest", REPO / "scripts" / "build_bars_work_manifest.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("build_bars_work_manifest", module)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module.main(argv)

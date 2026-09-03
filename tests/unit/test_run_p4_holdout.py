"""P4 driver tests: the owner-ratified menu, the one-shot authority
ledger, and the binding refusals.

The P4 driver is scripts/run_p4_holdout.py (a path script, like the wave
driver) — loaded by path, the same pattern the wave tests use. The
sealed-input/world machinery is NOT exercised here (the seam tests in
test_trials_options_run.py own the authorized trial itself); these own
the driver's guards: what may be registered, approved, consumed, and
verdicted."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]

_SEALED_DATES = frozenset(
    __import__(
        "tree_options.protocol.holdout", fromlist=["FINAL_HOLDOUT_DATES"]
    ).FINAL_HOLDOUT_DATES
)

_spec = importlib.util.spec_from_file_location(
    "run_p4_holdout", REPO / "scripts" / "run_p4_holdout.py"
)
p4 = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("run_p4_holdout", p4)
assert _spec.loader is not None
_spec.loader.exec_module(p4)


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Point every driver path at a scratch tree; fake git."""
    monkeypatch.setattr(
        p4, "REGISTRATION_PATH", tmp_path / "docs" / "p4-window-a-registration.json"
    )
    monkeypatch.setattr(p4, "P4_ROOT", tmp_path / "artifacts" / "theory" / "p4")
    monkeypatch.setattr(p4, "STATE_PATH", tmp_path / "artifacts" / "theory" / "p4" / "state.json")
    monkeypatch.setattr(
        p4, "REGISTRY_PATH", tmp_path / "artifacts" / "theory" / "p4" / "registry.db"
    )
    monkeypatch.setattr(p4, "TRIALS_DIR", tmp_path / "artifacts" / "theory" / "p4" / "trials")
    monkeypatch.setattr(p4, "SCRATCH_ROOT", tmp_path / "artifacts" / "theory" / "p4" / "scratch")
    monkeypatch.setattr(p4, "AUTHORITY_ROOT", tmp_path / "artifacts" / "p4-authority")
    monkeypatch.setattr(
        p4, "APPROVAL_PATH", tmp_path / "artifacts" / "p4-authority" / "approval.json"
    )
    monkeypatch.setattr(
        p4, "CONSUMPTION_PATH", tmp_path / "artifacts" / "p4-authority" / "consumption.jsonl"
    )
    monkeypatch.setattr(
        p4, "VERDICT_PATH", tmp_path / "artifacts" / "theory" / "p4" / "verdict.json"
    )
    monkeypatch.setattr(
        p4, "EVIDENCE_PATH", tmp_path / "docs" / "evidence-logs" / "m4" / "m4-p4-window-a.json"
    )
    monkeypatch.setattr(
        p4, "WAVE0_STATE_PATH", tmp_path / "artifacts" / "theory" / "wave0" / "state.json"
    )
    return tmp_path


def test_the_menu_is_the_owner_ratified_budget() -> None:
    """Ruling 1 + 4: six unique configs — null x3 on reused seeds 1/2/3
    (arm A), mom20 on BOTH arms, exit-2 (arm A); run indices continue the
    wave's per-arm namespace (a-r15..r19, b-r3) so no registry key can
    collide with a consumed wave trial."""
    menu = p4.p4_menu()
    assert [c.slot_id for c in menu] == list(p4.P4_SLOT_ORDER)
    keys = [c.params_key() for c in menu]
    assert len(set(keys)) == 6
    nulls = [c for c in menu if c.family == "P4-NULL"]
    assert {c.score_seed for c in nulls} == {"theory-null-1", "theory-null-2", "theory-null-3"}
    assert {c.arm for c in nulls} == {"A"}
    moms = [c for c in menu if c.family == "P4-MOM"]
    assert {c.arm for c in moms} == {"A", "B"}
    assert {c.model_family for c in moms} == {"mom20-quintile/v1"}
    hold = [c for c in menu if c.family == "P4-HOLD"]
    assert len(hold) == 1 and hold[0].exit_sessions_after_entry == 2
    arm_a_indices = sorted(c.run_index for c in menu if c.arm == "A")
    arm_b_indices = sorted(c.run_index for c in menu if c.arm == "B")
    assert arm_a_indices == [15, 16, 17, 18, 19]
    assert arm_b_indices == [3]


def _registration(tmp_path: Path, permitted: list[str] | None = None) -> dict[str, Any]:
    body = {
        "program": "p4-window-a",
        "world_id": "m3-massive-derived/test",
        "protocol_hash": "p" * 64,
        "dataset_manifest_hash": "m" * 64,
        "evaluation_window": {
            "window_id": "final-holdout-window-a",
            "expected_permitted": permitted
            or [
                "2026-05-08",
                "2026-05-15",
                "2026-05-22",
                "2026-05-29",
                "2026-06-05",
                "2026-06-12",
                "2026-06-26",
                "2026-07-10",
            ],
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
    raw = p4._registration_bytes(body)
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    p4.REGISTRATION_PATH.write_bytes(raw)
    p4.save_state({"registration_sha256": p4._sha256_bytes(raw), "executions": [], "verdict": None})
    return body


def test_register_only_never_overwrites(isolated, monkeypatch) -> None:
    """The registration is written ONCE; a rewrite is a deliberate owner
    act, never a driver convenience."""
    _registration(isolated)
    with pytest.raises(SystemExit, match="written once"):
        p4.register_only()


def test_approve_binds_and_refuses(isolated, monkeypatch) -> None:
    """The approval (the owner act) requires: the exact declared head, the
    registration COMMITTED at that head with matching on-disk bytes, a
    recorded reason — and is itself one-shot."""
    registration = _registration(isolated)
    monkeypatch.setattr(p4, "_git_head", lambda repo: "d" * 40)
    monkeypatch.setattr(p4, "_require_committed_registration", lambda head: None)
    with pytest.raises(SystemExit, match="needs a reason"):
        p4.approve("d" * 40, "  ")
    p4.approve("d" * 40, "the owner's window-A declaration")
    record = json.loads(p4.APPROVAL_PATH.read_text(encoding="utf-8"))
    assert record["registration_sha256"] == p4._sha256_bytes(p4.REGISTRATION_PATH.read_bytes())
    assert (
        record["permitted_test_sessions"] == registration["evaluation_window"]["expected_permitted"]
    )
    assert record["declared_head"] == "d" * 40
    with pytest.raises(SystemExit, match="ONE act"):
        p4.approve("d" * 40, "a second approval")
    # a different head than the checkout's refuses
    with pytest.raises(SystemExit, match="not the"):
        p4.approve("e" * 40, "wrong head")


def test_approve_requires_a_committed_registration(isolated, monkeypatch) -> None:
    """An uncommitted or edited registration cannot be approved — the
    approval binds COMMITTED content, not a working tree."""
    _registration(isolated)

    def _fail(head: str) -> None:
        raise SystemExit("REFUSED: not committed (test)")

    monkeypatch.setattr(p4, "_git_head", lambda repo: "d" * 40)
    monkeypatch.setattr(p4, "_require_committed_registration", _fail)
    with pytest.raises(SystemExit, match="not committed"):
        p4.approve("d" * 40, "reason")


def test_content_identity_is_head_independent(isolated) -> None:
    """The consumed CONTENT (world + protocol + registration + permitted
    set) identifies the one-shot — a re-execution at a DIFFERENT head is
    still the same window's second look and must refuse."""
    approval = {
        "kind": "P4_HOLDOUT_APPROVAL",
        "window_id": "final-holdout-window-a",
        "world_id": "w",
        "protocol_hash": "p",
        "dataset_manifest_hash": "m",
        "registration_sha256": "r",
        "permitted_test_sessions": ["2026-05-08"],
    }
    assert p4._content_identity(approval) == p4._content_identity(
        {**approval, "declared_head": "x"}
    )
    assert p4._content_identity(approval) != p4._content_identity(
        {**approval, "permitted_test_sessions": ["2026-05-08", "2026-05-15"]}
    )


def test_the_consumption_ledger_chains_and_refuses_damage(isolated) -> None:
    first = p4._append_consumption(
        {"kind": "P4_CONSUMPTION", "content_identity": "c1", "head": "a" * 40}
    )
    second = p4._append_consumption(
        {"kind": "P4_CONSUMPTION", "content_identity": "c2", "head": "b" * 40}
    )
    records = p4._read_consumptions()
    assert [r["content_identity"] for r in records] == ["c1", "c2"]
    assert records[1]["prev_record_sha256"] == first
    assert records[1]["record_sha256"] == second
    # tamper with the first line: the chain refuses
    lines = p4.CONSUMPTION_PATH.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace("c1", "c9")
    p4.CONSUMPTION_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="damaged"):
        p4._read_consumptions()


def test_config_drift_refuses_by_name(isolated) -> None:
    """Params, hypothesis text, or run_index drift from the committed
    registration refuses BEFORE any execution."""
    registration = _registration(isolated)
    config = p4.p4_menu()[0]
    p4._verify_config_against_registration(registration, config)
    rows = {row["slot_id"]: dict(row) for row in registration["slots"]}
    rows[config.slot_id]["params_key"] = ["X", *rows[config.slot_id]["params_key"][1:]]
    with pytest.raises(SystemExit, match="drifted"):
        p4._verify_config_against_registration(
            {**registration, "slots": list(rows.values())}, config
        )
    rows[config.slot_id]["params_key"] = [str(part) for part in config.params_key()]
    rows[config.slot_id]["hypothesis"] = "rewritten"
    with pytest.raises(SystemExit, match="hypothesis text drifted"):
        p4._verify_config_against_registration(
            {**registration, "slots": list(rows.values())}, config
        )
    rows[config.slot_id]["hypothesis"] = config.hypothesis
    rows[config.slot_id]["run_index"] = 99
    with pytest.raises(SystemExit, match="run_index drifted"):
        p4._verify_config_against_registration(
            {**registration, "slots": list(rows.values())}, config
        )
    with pytest.raises(SystemExit, match="not in the committed"):
        import dataclasses as _dc

        stranger = _dc.replace(p4.p4_menu()[0], slot_id="p4-band-035")
        p4._verify_config_against_registration(registration, stranger)


def _approval_record(**overrides) -> dict[str, Any]:
    base = {
        "kind": "P4_HOLDOUT_APPROVAL",
        "window_id": "final-holdout-window-a",
        "world_id": "massive-derived/AAPL+ADBE+AMD+AMZN+AVGO/d467d7878609-a",
        "protocol_hash": "b" * 64,
        "dataset_manifest_hash": "c" * 64,
        "registration_sha256": "d" * 64,
        "declared_head": "d" * 40,
        "permitted_test_sessions": ["2026-05-08", "2026-05-15"],
        "reason": "the owner's declaration",
        "at_epoch": 1_700_000_000,
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("wrong kind", {"kind": "SOMETHING_ELSE"}),
        ("wrong window", {"window_id": "final-holdout-window-b"}),
        ("short hash", {"registration_sha256": "a" * 63}),
        ("bad head", {"declared_head": "zzz"}),
        ("unsealed permitted", {"permitted_test_sessions": ["2026-05-07"]}),
        ("empty permitted", {"permitted_test_sessions": []}),
        ("no reason", {"reason": "  "}),
        ("bad epoch", {"at_epoch": 0}),
        ("empty world", {"world_id": "  "}),
        ("not an object", None),
    ],
)
def test_approval_lookalikes_refuse_by_name(isolated, label, mutate) -> None:
    """(Codex round 1 P1-1) the approval RECORD is the owner act: a
    hand-written lookalike — wrong kind, wrong window, partial hashes, an
    unsealed or empty permitted set, no reason, a bogus timestamp — is
    not an approval."""
    record = mutate if mutate is None else _approval_record(**mutate)
    if mutate is None:
        with pytest.raises(SystemExit, match="not an object"):
            p4._validate_approval_record(record)
    else:
        with pytest.raises(SystemExit, match="REFUSED"):
            p4._validate_approval_record(record)


def test_the_verdict_requires_a_consumption(isolated, monkeypatch) -> None:
    """(Codex round 1 P1-3) no approval or no consumption record means
    nothing was spent — a verdict cannot certify a bare state file."""
    state = {"executions": []}
    with pytest.raises(SystemExit, match="no approval record"):
        p4._verdict_from_state(state)
    p4.APPROVAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    p4.APPROVAL_PATH.write_text(json.dumps(_approval_record()), encoding="utf-8")
    with pytest.raises(SystemExit, match="no consumption record"):
        p4._verdict_from_state(state)


def test_the_verdict_refuses_fabricated_evidence(isolated, tmp_path, monkeypatch) -> None:
    """(Codex round 1 P1-3) fabricated artifacts refuse at EVERY new
    binding: a path outside the driver's trial directory, a trial the
    registry does not hold COMPLETE, and a stamp from another head."""
    monkeypatch.setattr(p4, "REPO_ROOT", tmp_path)
    p4.APPROVAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    approval = _approval_record()
    p4.APPROVAL_PATH.write_text(json.dumps(approval), encoding="utf-8")
    p4._append_consumption(
        {
            "kind": "P4_CONSUMPTION",
            "content_identity": p4._content_identity(approval),
            "head": "d" * 40,
        }
    )
    body = {
        "stamp": {"trial_id": "t-1", "git_sha": "d" * 40},
        "payload": {"backtest": {"total_return": -1.0}},
    }
    art_dir = tmp_path / "artifacts" / "theory" / "p4" / "trials" / "p4-null-1"
    art_dir.mkdir(parents=True)
    (art_dir / "t.json").write_text(json.dumps(body), encoding="utf-8")
    executions = [
        {
            "slot_id": slot,
            "trial_id": "t-1",
            "artifact_path": "artifacts/theory/p4/trials/p4-null-1/t.json",
        }
        for slot in p4.P4_SLOT_ORDER
    ]
    state = {"executions": executions}

    # no registry row at all
    with pytest.raises(SystemExit, match="not registered-and-COMPLETED"):
        p4._verdict_from_state(state)
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from tree_options.registry.scope import TrialScope as _Scope
    from tree_options.registry.sqlite import TrialRegistry as _Reg
    from tree_options.schemas.trial import TrialRecord as _Rec

    registry = _Reg(str(p4.REGISTRY_PATH))
    scope = _Scope(
        protocol_id="tree_options",
        protocol_hash="b" * 64,
        outer_fold_id="p4",
        target_horizon="h5",
        feature_set_id="p4|options|ov1",
        model_family="options_A:v1",
    )
    record = _Rec(
        trial_id="t-1",
        created_at=_dt(2026, 9, 3, tzinfo=_UTC),
        hypothesis="the registered window-A trial",
        git_sha="d" * 40,
        config_hash="c" * 64,
        dataset_manifest_hash="m" * 64,
        hyperparameters={},
        scope_key=scope.scope_key(),
    )
    registry.register(record, scope)
    registry.mark_running(
        "t-1",
        git_sha="d" * 40,
        config_hash="c" * 64,
        dataset_manifest_hash="m" * 64,
        at=_dt(2026, 9, 3, tzinfo=_UTC),
    )
    registry.complete("t-1", "file://p4", outcome_at=_dt(2026, 9, 3, tzinfo=_UTC))
    registry.close()
    # path that ESCAPES the slot's own directory (normpath collapses
    # the .. — a bare startswith would have let it through)
    state["executions"][1]["artifact_path"] = (
        "artifacts/theory/p4/trials/p4-null-2/../../p4-null-1/t.json"
    )
    with pytest.raises(SystemExit, match="not under"):
        p4._verdict_from_state(state)
    state["executions"][1]["artifact_path"] = "artifacts/theory/p4/trials/p4-null-1/t.json"
    # the stamp names another head
    wrong_head = json.dumps(
        {
            "stamp": {"trial_id": "t-1", "git_sha": "e" * 40},
            "payload": {"backtest": {"total_return": -1.0}},
        }
    )
    (art_dir / "t.json").write_text(wrong_head, encoding="utf-8")
    with pytest.raises(SystemExit, match="another head"):
        p4._verdict_from_state(state)
    # the stamp names a DIFFERENT TRIAL than the state recorded
    mismatched_trial = json.dumps(
        {
            "stamp": {"trial_id": "t-OTHER", "git_sha": "d" * 40},
            "payload": {"backtest": {"total_return": -1.0}},
        }
    )
    (art_dir / "t.json").write_text(mismatched_trial, encoding="utf-8")
    with pytest.raises(SystemExit, match="only the EXECUTED artifact"):
        p4._verdict_from_state(state)


def test_the_tracked_evidence_file_refuses_a_second_consumption(isolated) -> None:
    """(Codex round 1 P1-2) the TRACKED evidence record travels with the
    repo — its existence alone refuses the window (the cross-checkout
    one-shot: a second worktree's local ledger never saw the spend)."""
    approval = _approval_record()
    p4.EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    p4.EVIDENCE_PATH.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="tracked window-A evidence"):
        p4._refuse_second_consumption(approval)


def test_the_locked_consume_is_one_act(isolated) -> None:
    """(Codex round 1 P1-2) _consume_authority appends the chained record
    under the flock and refuses the same content a second time — the
    check and the append cannot interleave."""
    approval = _approval_record()
    p4.APPROVAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    p4.APPROVAL_PATH.write_text(json.dumps(approval), encoding="utf-8")
    first = p4._consume_authority(approval, "d" * 40)
    records = p4._read_consumptions()
    assert len(records) == 1 and records[0]["record_sha256"] == first
    with pytest.raises(SystemExit, match="already consumed"):
        p4._consume_authority(approval, "d" * 40)
    other = p4._consume_authority({**approval, "registration_sha256": "q" * 64}, "d" * 40)
    assert len(p4._read_consumptions()) == 2
    assert p4._read_consumptions()[1]["prev_record_sha256"] == first
    assert p4._read_consumptions()[1]["record_sha256"] == other


def test_decision_sessions_exclude_unpermitted_sealed_dates() -> None:
    """The driver's grid: research sessions + EXACTLY the permitted set —
    an unpermitted sealed date never rides the decision grid."""

    class _Grid:
        def sessions(self):
            return tuple(
                date.fromisoformat(d)
                for d in (
                    "2026-04-17",
                    "2026-04-24",
                    "2026-05-01",
                    "2026-05-08",
                    "2026-05-15",
                    "2026-07-17",
                    "2026-08-14",
                )
            )

    permitted = (date(2026, 5, 8), date(2026, 5, 15))
    world = type("W", (), {"grid": _Grid()})()
    sessions = p4._decision_sessions(world, permitted)
    assert sessions == tuple(
        date.fromisoformat(d)
        for d in ("2026-04-17", "2026-04-24", "2026-05-01", "2026-05-08", "2026-05-15")
    )


def test_momentum_rows_score_the_permitted_dates_under_authority(monkeypatch) -> None:
    """The P4 driver is the ONLY caller that lifts the momentum scorer's
    seal skip (include_holdout=True), and it passes EXACTLY the permitted
    dates as sessions."""
    captured: dict[str, Any] = {}

    def _spy(bars, grid, sessions, *, include_holdout=False):
        captured["sessions"] = tuple(sessions)
        captured["include_holdout"] = include_holdout
        return ()

    monkeypatch.setattr(p4.wave, "momentum_scored_rows", _spy)
    permitted = (date(2026, 5, 8), date(2026, 5, 15))

    class _World:
        dataset = type("D", (), {"bars": ()})()
        grid = None

    config = next(c for c in p4.p4_menu() if c.slot_id == "p4-mom-a")
    p4._scored_for(config, _World(), permitted)
    assert captured["sessions"] == permitted
    assert captured["include_holdout"] is True


def test_a_second_consumption_of_the_same_content_refuses(isolated) -> None:
    """The one-shot: a consumption record whose content identity matches
    the approval's refuses; DIFFERENT content (a future, differently
    scoped window evaluation) proceeds to the world build."""
    approval = {
        "kind": "P4_HOLDOUT_APPROVAL",
        "window_id": "final-holdout-window-a",
        "world_id": "w",
        "protocol_hash": "p",
        "dataset_manifest_hash": "m",
        "registration_sha256": "r",
        "permitted_test_sessions": ["2026-05-08"],
    }
    p4._refuse_second_consumption(approval)  # nothing consumed yet: passes
    p4._append_consumption(
        {
            "kind": "P4_CONSUMPTION",
            "content_identity": p4._content_identity(approval),
            "head": "a" * 40,
        }
    )
    with pytest.raises(SystemExit, match="already consumed"):
        p4._refuse_second_consumption(approval)
    # different content is a different one-shot
    other = {**approval, "registration_sha256": "r2"}
    p4._refuse_second_consumption(other)


def test_momentum_rows_score_sealed_sessions_only_under_the_flag() -> None:
    """include_holdout=False (the research default) SKIPS sealed sessions;
    True scores them — the P4 driver is the only caller that passes True,
    and the score is strictly backward-looking so a sealed decision's
    score consumes no forward seal information."""
    from datetime import datetime, time, timedelta
    from decimal import Decimal as _Decimal

    from tree_options.evaluation.stats import ScoredLabel  # noqa: F401

    start = date(2025, 11, 28)  # a Friday; 26 weekly bars end 2026-05-22
    sessions = tuple(start + timedelta(weeks=i) for i in range(26))
    sealed = [s for s in sessions if s.isoformat() in set(_SEALED_DATES)]
    assert tuple(sealed) == (date(2026, 5, 8), date(2026, 5, 15), date(2026, 5, 22))
    # the warm-up needs 21 VISIBLE bars, so the first scored session is
    # index 21 (2026-04-24) — the sealed dates sit past it
    assert sessions[21] == date(2026, 4, 24)

    class _Bar:
        def __init__(self, session: date, i: int) -> None:
            self.security_id = "TEST"
            self.session = session
            self.close = _Decimal(100 + i)
            self.available_at = datetime.combine(
                session + timedelta(days=1), time(9, 0), tzinfo=None
            ).replace(tzinfo=__import__("datetime").UTC)

    bars = tuple(_Bar(s, i) for i, s in enumerate(sessions))

    class _Grid:
        def session_close(self, session: date):
            return datetime.combine(session, time(16, 0), tzinfo=__import__("datetime").UTC)

    rows_default = p4.wave.momentum_scored_rows(bars, _Grid(), sessions)
    rows_p4 = p4.wave.momentum_scored_rows(bars, _Grid(), sessions, include_holdout=True)
    scored = set(sessions[21:])  # the 21-bar warm-up owns the head
    assert {r.session for r in rows_default} == scored - set(sealed)
    assert {r.session for r in rows_p4} == scored

"""desk digest provenance: the digest pins its inputs (the outcome-table
bytes every number depends on, the pre-registered plan, the code that wrote
it) and a redigest preserves every prior digest APPEND-ONLY instead of
burning them in a single backup slot.

Every oracle here is hand-derived. The sha256 constants were computed
off-line (sha256sum / an independent canonicalization run in a shell), never
by the code under test; the history tests pin exact file names, not globs.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_desk_longrun import SESSIONS6, FakeAsk, boards_for
from tests.unit.test_desk_skill import _table_file
from tree_options.desk import longrun, skill

# 197 bytes, exactly; sha256 computed off-line with sha256sum
TABLE_BYTES = (b'{"snapshot": "p1", "candidate_id": "u", "exit_mode": "intraday", '
               b'"status": "closed", "gross": 3.0, "net": 1.0}\n'
               b'{"snapshot": "p2", "candidate_id": "d", "exit_mode": "intraday", '
               b'"status": "no_fill"}\n')
TABLE_SHA = "ff3dcdd34f8a5c18083ea281deb2f300a6b5d4ef24f822c8592f3592597684ca"
# sha256 of the canonical JSON {"draws":1000,"incumbent":"m","metric":"net_total",
# "random_seeds":200} (sort_keys, separators (",", ":")), computed off-line
PROTOCOL_SHA = "926a16711c3a2f9803389d6f6d84c1d1d0e66afa3b34742ebb6785855b45cda1"
FINGERPRINT = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
VERSION = "b757009-dirty"


# ------------------------------------------------------------- the block


def _hand_run_dir(tmp_path: Path, *, with_table: bool = True) -> Path:
    """A minimal run dir whose every file is a hand-written literal, so the
    expected provenance block is fully known in advance."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "plan.json").write_text(json.dumps({
        "schema": "desk-longrun-plan/1", "run_id": "hand",
        "boards_fingerprint": FINGERPRINT,
        "protocol": {"draws": 1000, "random_seeds": 200, "incumbent": "m",
                     "metric": "net_total"}}))
    outcome: dict[str, Any] = {"plugin": "v2"}
    if with_table:
        (tmp_path / "table.jsonl").write_bytes(TABLE_BYTES)
        outcome["table"] = str(tmp_path / "table.jsonl")
    (run_dir / "config.json").write_text(json.dumps({"outcome": outcome}))
    return run_dir


def test_provenance_block_pins_every_input_against_a_hand_fixture(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(longrun, "_git_describe", lambda start: VERSION)
    run_dir = _hand_run_dir(tmp_path)
    assert longrun.provenance_block(run_dir, {}) == {
        "schema": "desk-longrun-provenance/1",
        "outcome_table": {"path": str(tmp_path / "table.jsonl"), "bytes": 197,
                          "sha256": TABLE_SHA},
        "boards_fingerprint": FINGERPRINT,
        "protocol_sha256": PROTOCOL_SHA,
        "code_version": VERSION}


def test_provenance_omits_code_version_outside_a_repo(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(longrun, "_git_describe", lambda start: None)
    block = longrun.provenance_block(_hand_run_dir(tmp_path, with_table=False), {})
    assert "code_version" not in block  # omitted cleanly, never null-string filler
    assert block["outcome_table"] == {
        "path": None,
        "note": "no outcome table in config.json (outcome plug-in 'v2')"}


def test_git_describe_names_this_repo_and_none_elsewhere(tmp_path: Path) -> None:
    assert longrun._git_describe(tmp_path) is None  # not a repo: no version claim
    described = longrun._git_describe(Path(longrun.__file__).resolve().parent)
    assert isinstance(described, str) and re.fullmatch(r"\S+", described)


def test_provenance_survives_a_dir_without_plan_or_config(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    block = longrun.provenance_block(empty, {})
    assert block["schema"] == "desk-longrun-provenance/1"
    assert block["outcome_table"]["path"] is None
    assert "boards_fingerprint" not in block and "protocol_sha256" not in block


# ------------------------------------------------- wiring into write_digest


def _cli_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
             capsys: pytest.CaptureFixture[str]) -> tuple[Path, Path]:
    """A finished CLI run (config.json + plan.json on disk in the run dir)."""
    from tree_options.desk.__main__ import run_cli

    boards = boards_for(SESSIONS6[:3])
    monkeypatch.setitem(longrun.PLUGINS["boards"], "pytest", lambda p, c: boards)
    monkeypatch.setitem(longrun.PLUGINS["ask"], "pytest", lambda p, c: FakeAsk())
    table = tmp_path / "table.jsonl"
    _table_file(table, boards)
    config = tmp_path / "longrun.json"
    config.write_text(json.dumps({
        "out_root": "out", "incumbent": "m", "concurrency": 2,
        "boards": {"plugin": "pytest"},
        "outcome": {"plugin": "v2", "table": str(table)},
        "ask": {"plugin": "pytest"}, "quota": {"plugin": "always"},
        "protocol": {"draws": 1000, "random_seeds": 200},
        "policies": [{"name": "m", "kind": "model", "repeats": 2}]}))
    assert run_cli(["longrun", "run", "--config", str(config)]) == 0
    capsys.readouterr()
    return run_dir_of(tmp_path), table


def run_dir_of(tmp_path: Path) -> Path:
    roots = sorted((tmp_path / "out").iterdir())
    assert len(roots) == 1 and roots[0].is_dir()
    return roots[0]


def test_the_written_digest_carries_provenance_of_its_actual_inputs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    run_dir, table = _cli_run(tmp_path, monkeypatch, capsys)
    digest = json.loads((run_dir / "digest.json").read_text())
    raw = table.read_bytes()  # oracle: hash the INPUT bytes independently
    assert digest["provenance"]["outcome_table"] == {
        "path": str(table), "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest()}
    plan = json.loads((run_dir / "plan.json").read_text())
    assert digest["provenance"]["boards_fingerprint"] == plan["boards_fingerprint"]
    canon = json.dumps(plan["protocol"], sort_keys=True, separators=(",", ":")).encode()
    assert digest["provenance"]["protocol_sha256"] == hashlib.sha256(canon).hexdigest()
    assert re.fullmatch(r"\S+", digest["provenance"]["code_version"])
    md = (run_dir / "digest.md").read_text()
    assert "## Provenance" in md
    assert digest["provenance"]["outcome_table"]["sha256"] in md


def test_an_out_redigest_pins_the_table_it_was_told_to_use(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    run_dir, table = _cli_run(tmp_path, monkeypatch, capsys)
    out = tmp_path / "out-redigest"
    skill.redigest(run_dir, table=table, out=out)
    prov = json.loads((out / "digest.json").read_text())["provenance"]
    raw = table.read_bytes()
    assert prov["outcome_table"] == {"path": str(table), "bytes": len(raw),
                                     "sha256": hashlib.sha256(raw).hexdigest()}
    # the block describes the RUN's pre-registration, not the scratch out dir
    assert prov["boards_fingerprint"] == json.loads(
        (run_dir / "plan.json").read_text())["boards_fingerprint"]


# ------------------------------------------------------ append-only history


def test_three_redigests_leave_three_recoverable_priors(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    run_dir, _ = _cli_run(tmp_path, monkeypatch, capsys)
    original = json.loads((run_dir / "digest.json").read_text())
    moments = [datetime(2026, 9, 30, 12, 0, i, tzinfo=UTC) for i in (1, 2, 3)]
    for moment in moments:
        assert skill.redigest(run_dir, clock=lambda m=moment: m)["redigest"][
            "prior_in"] is not None
    history = run_dir / "digest.history"
    names = sorted(p.name for p in history.glob("*.json"))
    assert names == [f"000{i}-20260930T12000{i}Z.json" for i in (1, 2, 3)]
    priors = [json.loads((history / name).read_text()) for name in names]
    assert [p["at"] for p in priors] == [original["at"], moments[0].isoformat(),
                                         moments[1].isoformat()]
    assert priors[0] == original  # the run's own digest survives content-intact
    latest = json.loads((run_dir / "digest.json").read_text())
    assert latest["at"] == moments[2].isoformat()
    assert latest["redigest"]["prior_in"] == "digest.history/0003-20260930T120003Z.json"
    md = (run_dir / "digest.md").read_text()
    assert "digest.history/0003-20260930T120003Z.json" in md


def test_history_never_collides_nor_overwrites_within_the_same_second(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    run_dir, _ = _cli_run(tmp_path, monkeypatch, capsys)
    same = datetime(2026, 9, 30, 12, 0, 9, tzinfo=UTC)
    skill.redigest(run_dir, clock=lambda: same)
    skill.redigest(run_dir, clock=lambda: same)  # same second: seq keeps them apart
    history = run_dir / "digest.history"
    names = sorted(p.name for p in history.glob("*.json"))
    assert names == ["0001-20260930T120009Z.json", "0002-20260930T120009Z.json"]
    first_bytes = (history / names[0]).read_bytes()
    skill.redigest(run_dir, clock=lambda: same)
    assert (history / names[0]).read_bytes() == first_bytes  # append-only, never edited
    assert len(list(history.glob("*.json"))) == 3


def test_redigest_without_a_prior_digest_writes_no_history(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    run_dir, _ = _cli_run(tmp_path, monkeypatch, capsys)
    (run_dir / "digest.json").unlink()
    doc = skill.redigest(run_dir)
    assert doc["redigest"]["prior_in"] is None
    assert not (run_dir / "digest.history").exists()


# ------------------------------------------------------ consumers tolerate


def test_cockpit_view_surfaces_provenance_and_tolerates_an_old_digest(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    run_dir, _ = _cli_run(tmp_path, monkeypatch, capsys)
    view = longrun.cockpit_view(run_dir)
    prov = view["digest"]["provenance"]
    assert prov["boards_fingerprint"] == json.loads(
        (run_dir / "plan.json").read_text())["boards_fingerprint"]
    assert re.fullmatch(r"[0-9a-f]{64}", prov["protocol_sha256"])
    # additive only: a digest written BEFORE provenance existed still projects
    digest = json.loads((run_dir / "digest.json").read_text())
    digest.pop("provenance")
    (run_dir / "digest.json").write_text(json.dumps(digest))
    old = longrun.cockpit_view(run_dir)["digest"]
    assert "provenance" not in old and old["headline"]

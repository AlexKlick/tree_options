"""Review r1 P1-5: the verdict-correction driver must refuse inputs that are
not the ruled run (fail-closed), instead of re-verdicting a different gate
run whose failures may have nothing to do with criterion 4's bound."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "run_m3_sealed_verdict_correction",
    Path(__file__).resolve().parents[2] / "scripts" / "run_m3_sealed_verdict_correction.py",
)
assert _spec is not None and _spec.loader is not None
svc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(svc)

HEADS = (
    "3bbb461508cf7a7d86f73d64a529651b4719c155",
    "b1c9b45b9b3a6e981cd16f7d58b705bda13b62d5",
)
PROTO = "p" * 64
CONFIG = "c" * 64


def _stamp(world: str, arm: str, **overrides) -> dict:
    base = {
        "trial_id": f"m3-{world}-{arm.lower()}-r1",
        "git_sha": HEADS[1],
        "protocol_hash": PROTO,
        "config_hash": CONFIG,
        "dataset_manifest_hash": f"ds-{world}",
    }
    base.update(overrides)
    return base


def _stamps() -> dict:
    return {
        (w, a): _stamp(w, a)
        for w in (
            "synth-v1-val-null-701",
            "synth-v1-val-null-702",
            "synth-v1-val-alpha-710",
            "synth-v1-val-alpha-711",
        )
        for a in ("A", "B")
    }


def _original(failures: list[str], trials: int = 8) -> dict:
    return {"verdict": "FAIL", "trials": trials, "failures": failures}


RULED = [
    "synth-v1-val-null-701|B: position SYN-0023 open past expiration 2019-11-01",
    "synth-v1-val-alpha-711|B: position SYN-0057 open past expiration 2019-11-01",
]


def _run(
    original: dict, stamps: dict | None = None, summary_stamp: dict | None = None
) -> list[str]:
    return svc._validate_inputs(
        original,
        stamps if stamps is not None else _stamps(),
        expected_protocol_hash=PROTO,
        summary_stamp=summary_stamp
        if summary_stamp is not None
        else {"protocol_hash": PROTO, "git_sha": HEADS[1]},
        expected_heads=HEADS,
    )


def test_ruled_failures_validate_cleanly() -> None:
    assert _run(_original(RULED)) == []


def test_unrelated_failure_refuses() -> None:
    """The review's scenario: a trial-status failure alongside the ruled ones
    must refuse — the correction re-verdicts nothing outside its ruling."""
    violations = _run(_original([*RULED, "synth-v1-val-null-701|A: trial status FAILED"]))
    assert any("outside the ruled criterion-4 class" in v for v in violations)


def test_no_failures_refuses() -> None:
    assert any("no failures" in v for v in _run(_original([])))


def test_trial_count_mismatch_refuses() -> None:
    assert any("!= 8 stamped payloads" in v for v in _run(_original(RULED, trials=7)))


def test_wrong_trial_id_refuses() -> None:
    stamps = _stamps()
    stamps[("synth-v1-val-null-701", "B")] = _stamp(
        "synth-v1-val-null-701", "B", trial_id="m3-synth-v1-val-null-701-b-r2"
    )
    assert any("trial_id" in v for v in _run(_original(RULED), stamps))


def test_unknown_source_sha_refuses() -> None:
    stamps = _stamps()
    stamps[("synth-v1-val-null-702", "A")] = _stamp("synth-v1-val-null-702", "A", git_sha="f" * 40)
    assert any("head set" in v for v in _run(_original(RULED), stamps))


def test_protocol_mismatch_refuses() -> None:
    stamps = _stamps()
    stamps[("synth-v1-val-alpha-710", "A")] = _stamp(
        "synth-v1-val-alpha-710", "A", protocol_hash="x" * 64
    )
    assert any("protocol_hash" in v for v in _run(_original(RULED), stamps))


def test_summary_stamp_outside_the_ruled_run_refuses() -> None:
    violations = _run(_original(RULED), summary_stamp={"protocol_hash": "z" * 64})
    assert any("summary stamp protocol_hash" in v for v in violations)
    violations = _run(_original(RULED), summary_stamp={"protocol_hash": PROTO, "git_sha": "f" * 40})
    assert any("summary stamp git_sha" in v for v in violations)


def test_per_trial_config_hashes_may_differ() -> None:
    """r1.1 correction: the first hardening required every trial's
    config_hash to equal the gate summary's and refused the GENUINE ruled
    run (all 8 stamps INPUT_REJECTED). Each trial's config embeds its
    world and arm, so the hashes legitimately differ — differing config
    hashes alone must validate cleanly."""
    stamps = {
        (w, a): _stamp(w, a, config_hash=f"cfg-{w}-{a}")
        for w in (
            "synth-v1-val-null-701",
            "synth-v1-val-null-702",
            "synth-v1-val-alpha-710",
            "synth-v1-val-alpha-711",
        )
        for a in ("A", "B")
    }
    assert _run(_original(RULED), stamps) == []


def test_dataset_hash_disagreement_across_arms_refuses() -> None:
    stamps = _stamps()
    stamps[("synth-v1-val-alpha-711", "B")] = _stamp(
        "synth-v1-val-alpha-711", "B", dataset_manifest_hash="ds-other"
    )
    assert any("dataset_manifest_hash disagrees" in v for v in _run(_original(RULED), stamps))


def test_failure_world_outside_the_stamped_set_refuses() -> None:
    """Review r3 P1-3: a ruled-SHAPED failure naming a world that is not in
    the stamped artifact set must refuse — the regex alone never bound the
    failure's world to the stamps, so an unrelated summary carrying such a
    line would validate cleanly and get re-verdicted."""
    stranger = ["not-a-sealed-world|B: position SYN-9999 open past expiration 2019-11-01"]
    violations = _run(_original(stranger))
    assert any("outside the stamped world set" in v for v in violations)


def test_expected_heads_flag_parses_and_defaults_to_the_ruled_mix() -> None:
    """Phase-6 re-registration: --expected-heads carries the code-final run's
    SHAs explicitly; without it the driver validates against the ORIGINAL
    ruled mix. The flag exists so the re-registered run's stamps at a new
    head validate — the fail-closed contract is unchanged (wrong heads
    still refuse, whatever set is in force)."""
    parser = svc._build_parser()
    assert parser.parse_args([]).expected_heads is None
    heads = "c9e843058a9def39013bdb3d5be8c8bd70f415e4"
    parsed = parser.parse_args(["--expected-heads", f"{heads}, {heads.upper()}"])
    assert parsed.expected_heads == f"{heads}, {heads.upper()}"
    assert svc.RULED_HEAD_MIX == HEADS  # the default mix is the ruled one


def test_reregistered_head_set_validates_when_passed() -> None:
    """The re-registered run stamps every trial + summary at the code-final
    head; _validate_inputs accepts exactly that head when it is the expected
    set (and still refuses a third, unrelated head)."""
    rehead = "c9e843058a9def39013bdb3d5be8c8bd70f415e4"
    stamps = {
        (w, a): _stamp(w, a, git_sha=rehead)
        for w in (
            "synth-v1-val-null-701",
            "synth-v1-val-null-702",
            "synth-v1-val-alpha-710",
            "synth-v1-val-alpha-711",
        )
        for a in ("A", "B")
    }
    summary = {"protocol_hash": PROTO, "git_sha": rehead}
    assert (
        svc._validate_inputs(
            _original(RULED),
            stamps,
            expected_protocol_hash=PROTO,
            summary_stamp=summary,
            expected_heads=(rehead,),
        )
        == []
    )
    violations = svc._validate_inputs(
        _original(RULED),
        stamps,
        expected_protocol_hash=PROTO,
        summary_stamp=summary,
        expected_heads=("f" * 40,),
    )
    assert any("head set" in v for v in violations)

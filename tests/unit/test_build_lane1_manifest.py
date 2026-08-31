"""Lane-1 Cboe manifest materializer: custody-first, deterministic, hermetic.

The script under test (``scripts/build_lane1_manifest.py``) exists to unblock
the G4 sealed-event preflight (``scripts/g4_seal.py preflight
--lane1-manifest … --lane1-source …``). Red-first lane: the script did not
exist when these tests were written — the module import below was the
captured red (ModuleNotFoundError).

Contract under test (lane brief 2026-08-28):

- custody FIRST: the source's sha256 must equal the pin before anything is
  parsed or written; a missing source is a named refusal, never a default;
- the manifest is built through the REAL adapter path — ``parse_cboe_eod_csv``
  → ``build_real_overlay`` → ``build_real_options_manifest`` — never
  re-implemented;
- two runs over the same bytes are byte-identical, and the payload carries
  no absolute paths (the delivery filename is the recorded ``source_path``);
- the written JSON re-validates as ``RealOptionsManifest`` and survives the
  same closed loop the G4 verifier runs (``seal/verified_inputs._verify_lane1``:
  re-parse with the manifest's own variant/underlying, then verify).

The synthetic fixture is the shared hermetic SPY CSV (``SPY_MAIN_ROWS``),
pinned by its own computed sha256 through the script's ``--expect-sha256``
seam — the default pin stays the retained no_cgi sample's hash.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT
from tests.fixtures import cboe_eod_rows as cboe_fx
from tree_options.data.cboe_eod import (
    RealOptionsManifest,
    parse_cboe_eod_csv,
    verify_real_options_manifest,
    verify_real_options_manifest_tokens,
)
from tree_options.data.digest import sha256_hex
from tree_options.data.real_overlay import build_real_overlay

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_lane1_manifest as lane1  # type: ignore[import-not-found]  # scripts/ via path


def _write_spy_fixture(tmp_path: Path) -> Path:
    """A small valid Cboe-shaped CSV (13 rows, 4 sessions) in tmp_path."""
    return cboe_fx.write_csv(tmp_path / "spy.csv", cboe_fx.SPY_MAIN_ROWS)


def _pin_of(source: Path) -> str:
    return sha256_hex(source.read_bytes())


def test_manifest_builds_writes_verifies_and_is_byte_identical_across_two_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_spy_fixture(tmp_path)
    pin = _pin_of(source)
    out_a = tmp_path / "run-a" / "cboe-manifest.json"
    out_b = tmp_path / "run-b" / "cboe-manifest.json"

    for out in (out_a, out_b):
        assert (
            lane1.main(["--source", str(source), "--out", str(out), "--expect-sha256", pin]) == 0
        ), f"materialization into {out} refused"

    raw_a = out_a.read_bytes()
    raw_b = out_b.read_bytes()
    assert raw_a == raw_b, "two runs over the same source bytes must be byte-identical"
    assert raw_a.endswith(b"\n"), "the artifact is newline-terminated JSON"

    manifest = RealOptionsManifest.model_validate_json(raw_a)
    verify_real_options_manifest_tokens(manifest)
    assert manifest.source_sha256 == pin
    assert manifest.underlying_security_id == "SPY"
    assert manifest.variant == "no_cgi", "the pinned file's declared product variant"
    assert manifest.source_path == source.name, "delivery filename, never an absolute path"
    assert str(tmp_path) not in raw_a.decode("utf-8"), "no absolute paths in the payload"

    # The G4 verifier's own closed loop (seal/verified_inputs._verify_lane1):
    # re-parse from the recorded variant/underlying and verify the manifest.
    result = parse_cboe_eod_csv(
        source, variant=manifest.variant, underlying=manifest.underlying_security_id
    )
    overlay = build_real_overlay(result)
    verify_real_options_manifest(manifest, result, overlay=overlay)

    stdout = capsys.readouterr().out
    assert pin in stdout, "the custody pin is printed"
    assert sha256_hex(raw_a) in stdout, "the written manifest's raw sha256 is printed"
    assert "verify_real_options_manifest_tokens: OK" in stdout


def test_wrong_hash_source_is_a_named_custody_refusal_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_spy_fixture(tmp_path)
    pin = _pin_of(source)
    flipped = bytearray(source.read_bytes())
    flipped[len(flipped) // 2] ^= 0x01  # byte-flip: same shape, different bytes
    wrong = tmp_path / "wrong-hash.csv"
    wrong.write_bytes(bytes(flipped))
    out = tmp_path / "refused" / "cboe-manifest.json"

    assert lane1.main(["--source", str(wrong), "--out", str(out), "--expect-sha256", pin]) == 2, (
        "a wrong-hash source must refuse, not build"
    )

    captured = capsys.readouterr()
    assert "custody refusal" in captured.err, "the refusal is named"
    assert pin in captured.err, "the refusal names the pin it failed against"
    assert not out.exists(), "no output is written on a custody refusal"
    assert "manifest raw_sha256" not in captured.out


def test_missing_source_is_a_named_custody_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out" / "cboe-manifest.json"

    assert (
        lane1.main(
            [
                "--source",
                str(tmp_path / "absent.csv"),
                "--out",
                str(out),
                "--expect-sha256",
                "0" * 64,
            ]
        )
        == 2
    ), "a missing source must refuse, never default to another file"

    err = capsys.readouterr().err
    assert "custody refusal" in err
    assert "absent.csv" in err
    assert not out.exists()


def test_live_era_output_paths_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The bars-era capture is live: artifacts/bars*, artifacts/m4b-coverage-era
    and runstate are never writable targets for the lane-1 artifact."""
    source = _write_spy_fixture(tmp_path)
    pin = _pin_of(source)
    out = tmp_path / "artifacts" / "bars" / "cboe-manifest.json"

    assert lane1.main(["--source", str(source), "--out", str(out), "--expect-sha256", pin]) == 2

    err = capsys.readouterr().err
    assert "protected" in err
    assert "bars" in err
    assert not out.exists()

"""G5: the deterministic null-score generator (the T-NULL score model).

No randomness anywhere (the trial runner forbids it): the score of one
(session, security_id) is the leading 64 bits of a domain-joined sha256,
mapped onto [0, 1). The seed is a REQUIRED parameter and rides the trial
config hash (`run_options_trial(score_seed=...)`), so a null trial can
never masquerade as another configuration.
"""

from __future__ import annotations

import hashlib
import io
from datetime import date

import pytest

from tree_options.trials.null_score import (
    NULL_SCORE_MODEL_FAMILY,
    main,
    null_score,
    null_scored_labels,
)

D1 = date(2025, 5, 5)
D2 = date(2025, 5, 6)


def test_same_seed_is_bit_identical() -> None:
    assert null_score(seed="t-null", session=D1, security_id="SPY") == null_score(
        seed="t-null", session=D1, security_id="SPY"
    )


def test_the_preimage_contract_is_pinned() -> None:
    """sha256(seed ‖ session ISO ‖ security_id) — unit-separator joined so
    the preimage is injective; the leading 64 bits mapped onto [0, 1) by the
    exact-arithmetic construction (P3-7: top 53 bits over 2**53, which
    divides exactly and can never round up to 1.0)."""
    digest = hashlib.sha256(b"t-null\x1f2025-05-05\x1fSPY").digest()
    expected = (int.from_bytes(digest[:8], "big") >> 11) / 2**53
    assert null_score(seed="t-null", session=D1, security_id="SPY") == expected


def test_every_field_and_the_seed_move_the_score() -> None:
    base = null_score(seed="t-null", session=D1, security_id="SPY")
    assert null_score(seed="t-nulm", session=D1, security_id="SPY") != base  # seed
    assert null_score(seed="t-null", session=D2, security_id="SPY") != base  # session
    assert null_score(seed="t-null", session=D1, security_id="QQQ") != base  # id


def test_scores_live_in_the_unit_interval() -> None:
    for session in (D1, D2):
        for sid in ("SPY", "QQQ", "I:SPX", "BRK.B"):
            score = null_score(seed="t-null", session=session, security_id=sid)
            assert 0.0 <= score < 1.0


class _MaximalDigest:
    """A stand-in sha256 whose digest is 0xff in every byte — the maximal
    64-bit prefix, the exact input class the old float division rounded to
    1.0. The seam is honest: it replaces the module's own `hashlib.sha256`
    reference and nothing else in the code path."""

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def digest(self) -> bytes:
        return b"\xff" * 32


def test_maximal_digest_stays_strictly_below_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """(P3-7, Codex round 1) The declared range is [0, 1) — STRICTLY below
    1.0. `int.from_bytes(digest[:8]) / float(2**64)` rounded to exactly 1.0
    for prefixes >= 0xfffffffffffffc00 (2^-54 probability per row), so the
    maximal digest is the honest seam: the mapping must keep it < 1.0
    structurally, not with luck (RED before P3-7: this returned 1.0)."""
    import tree_options.trials.null_score as module

    monkeypatch.setattr(module.hashlib, "sha256", _MaximalDigest)
    score = null_score(seed="t-null", session=D1, security_id="SPY")
    assert 0.0 <= score < 1.0


def test_the_mapping_is_structurally_below_one_for_every_input() -> None:
    """(P3-7) The pure mapping itself, at the boundary the old construction
    broke at: 0xfffffffffffffc00 was the FIRST prefix whose /2**64 float
    division rounded up to 1.0, and 0xffff…ff the last — the 53-bit
    construction maps every one of the 2**64 inputs into [0, 1) exactly."""
    from tree_options.trials.null_score import _unit_score

    top = 0xFFFFFFFFFFFFFFFF
    assert _unit_score(0) == 0.0
    for leading_bits in (0xFFFFFFFFFFFFFC00, top - 1, top):
        mapped = _unit_score(leading_bits)
        assert 0.0 <= mapped < 1.0
    # exactness: the top input maps to (2**53 - 1) / 2**53, the largest
    # float below 1.0 — never a rounded-up 1.0
    assert _unit_score(top) == (2**53 - 1) / 2**53


def test_seed_is_required() -> None:
    with pytest.raises(ValueError, match="seed is required"):
        null_score(seed="", session=D1, security_id="SPY")


def test_null_scored_labels_supplies_the_runner() -> None:
    rows = ((D1, "SPY", 0.25), (D1, "QQQ", -0.10), (D2, "SPY", 0.05))
    scored = null_scored_labels("t-null", rows)
    assert [r.security_id for r in scored] == ["SPY", "QQQ", "SPY"]
    assert [r.session for r in scored] == [D1, D1, D2]
    assert [r.label for r in scored] == [0.25, -0.10, 0.05]
    for row in scored:
        assert row.score == null_score(
            seed="t-null", session=row.session, security_id=row.security_id
        )


def test_model_family_token_is_declared() -> None:
    assert NULL_SCORE_MODEL_FAMILY == "null-sha256/1"


def test_cli_single_row(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--seed", "t-null", "--session", "2025-05-05", "--security-id", "SPY"]) == 0
    out = capsys.readouterr().out
    assert out.strip() == str(null_score(seed="t-null", session=D1, security_id="SPY"))


def test_cli_reads_rows_from_stdin(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("# comment\n\n2025-05-05,SPY\n2025-05-06,QQQ\n"))
    assert main(["--seed", "t-null"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out == [
        f"2025-05-05,SPY,{null_score(seed='t-null', session=D1, security_id='SPY')}",
        f"2025-05-06,QQQ,{null_score(seed='t-null', session=D2, security_id='QQQ')}",
    ]


def test_cli_requires_seed() -> None:
    with pytest.raises(SystemExit):
        main(["--session", "2025-05-05", "--security-id", "SPY"])

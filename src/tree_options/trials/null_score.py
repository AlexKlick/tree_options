"""G5: the deterministic null-score generator — the T-NULL score model.

The options trial runner is model-agnostic: the CALLER supplies the scored
cross-section (`trials/options_run.py`, exactly as
`scripts/run_m3_dev_trials.py` supplies the M2 H5 ridge scores). T-NULL's
model is NO model: the score of one (session, security_id) is the leading
64 bits of sha256 over the domain-joined triple

    seed ‖ "\\x1f" ‖ session ISO ‖ "\\x1f" ‖ security_id

mapped onto [0, 1) by exact arithmetic (top 53 bits over 2**53 — strictly
below 1.0 for EVERY input; see `_unit_score`). Determinism is total — same
seed, same scores, on
every host, forever; there is no RNG, no state, and no clock anywhere in
this module (the trial runner forbids randomness outright). The ASCII
unit separator makes the preimage injective, so field values can never
bleed across a boundary ("A", "B.C" and "A.B", "C" hash differently).

The seed is a REQUIRED parameter and a first-class trial input: pass the
same string to `run_options_trial(score_seed=...)` so the declared score
model's input rides the config hash and the stamped payload — a null
trial can never masquerade as another configuration.

CLI (the T-NULL launch entry):

    python -m tree_options.trials.null_score --seed <seed> \
        [--session YYYY-MM-DD --security-id ID]

Without --session/--security-id it reads `session,security_id` lines from
stdin (blank lines and # comments ignored) and writes
`session,security_id,score` lines in the same order.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Iterable, Sequence
from datetime import date

from tree_options.evaluation.stats import ScoredLabel

NULL_SCORE_MODEL_FAMILY = "null-sha256/1"

_UNIT = "\x1f"  # the ASCII unit separator: the join is injective
# (P3-7, Codex round 1) exact-arithmetic mapping onto [0, 1): the top
# _SHIFT bits are dropped and the remaining 53-bit integer is divided by
# 2**53. A 53-bit integer over 2**53 is EXACTLY representable as a float
# and strictly smaller than 1.0 for every one of the 2**64 inputs — the
# previous `int / float(2**64)` rounded to exactly 1.0 for prefixes
# >= 0xfffffffffffffc00, violating the declared [0, 1) (2^-54 per row).
_SHIFT = 11
_DENOMINATOR = 2**53


def _unit_score(leading_bits: int) -> float:
    """Map the leading 64 hash bits onto [0, 1) exactly.

    `(n >> 11) / 2**53`: the quotient of a 53-bit integer by a power of two
    needs at most 53 significand bits, so the division is exact — no
    rounding, hence no input can ever map to 1.0."""
    return (leading_bits >> _SHIFT) / _DENOMINATOR


def null_score(*, seed: str, session: date, security_id: str) -> float:
    """The deterministic hash score of one (session, security_id) in [0, 1).

    Pure sha256 arithmetic over exact strings — the same inputs always
    produce the same float on every platform."""
    if not seed:
        raise ValueError(
            "seed is required: a null score without a declared seed is unregistered randomness"
        )
    preimage = _UNIT.join((seed, session.isoformat(), security_id)).encode("utf-8")
    digest = hashlib.sha256(preimage).digest()
    return _unit_score(int.from_bytes(digest[:8], "big"))


def null_scored_labels(
    seed: str, rows: Iterable[tuple[date, str, float]]
) -> tuple[ScoredLabel, ...]:
    """Rows of (session, security_id, label) -> the runner's scored input,
    every score from `null_score` under the one seed."""
    return tuple(
        ScoredLabel(
            security_id=security_id,
            session=session,
            score=null_score(seed=seed, session=session, security_id=security_id),
            label=label,
        )
        for session, security_id, label in rows
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tree_options.trials.null_score",
        description="Deterministic null scores for the T-NULL launch (G5).",
    )
    parser.add_argument("--seed", required=True, help="the required score seed")
    parser.add_argument("--session", help="one-shot: the session ISO date")
    parser.add_argument("--security-id", help="one-shot: the security id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.session is not None or args.security_id is not None:
        if args.session is None or args.security_id is None:
            _parser().error("--session and --security-id come together")
        print(
            null_score(
                seed=args.seed,
                session=date.fromisoformat(args.session),
                security_id=args.security_id,
            )
        )
        return 0
    for line in sys.stdin:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        session_text, security_id = text.split(",", 1)
        session = date.fromisoformat(session_text.strip())
        print(
            f"{session.isoformat()},{security_id.strip()},"
            f"{null_score(seed=args.seed, session=session, security_id=security_id.strip())}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - the CLI entry
    raise SystemExit(main())

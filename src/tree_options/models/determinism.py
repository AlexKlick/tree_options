"""Determinism pins for the numpy era (M2-proper §3.D).

numpy's BLAS backends reduce in thread-count-dependent orders, which
would break byte-identical re-runs. Drivers (the trial runner, the test
suite, the gate) FORCE these pins BEFORE the first numpy import; the
gate script additionally exports them. Determinism scope: same locked
environment (uv.lock pins numpy and its BLAS wheel) ⇒ byte-identical
coefficients across runs and processes.
"""

from __future__ import annotations

import os

_BLAS_THREAD_VARS = (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
)


def force_single_threaded_blas() -> None:
    """Force every BLAS threading knob to 1 (idempotent, overriding)."""
    for var in _BLAS_THREAD_VARS:
        os.environ[var] = "1"


def blas_pinned() -> bool:
    """True when every threading knob is currently forced to 1."""
    return all(os.environ.get(var) == "1" for var in _BLAS_THREAD_VARS)

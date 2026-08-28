"""(R6-P2, Codex round 6) THE shared spot-proxy token contract.

One owner for the value discipline every spot-proxy consumer applies:
the runtime file loader (`massive_overlay._load_spot`), the lane-2
adapter's constructor copy loop (`vwap_pit_surface.VwapPitSurface`), and
the CENSUS path (`scripts/inspect_structural_coverage.load_spot_proxy`,
which `scripts/build_coverage_census.py` reads through). Before R6-P2 the
census scripts parsed the same declared input with their OWN `_dec` +
`<= 0` gate — a SECOND contract — so a token the runtime refuses
(``"Infinity"``: `_dec` accepts it and it is POSITIVE-looking) loaded on
the census side and a malformed capture classified COMPLETE at exit 0.
The validator lives here, importable by both sides without dragging the
WS-D1 client into the WS-D2 inspector (that is why it is not imported
from `massive_overlay`, which imports `massive_options`), and each
consumer rebrands the refusal to its own error shape through `refuse`.

The sentinel is here for the same reason: the FLAT form
``{"SPY": "5750.00"}`` declares one spot for every session and is stored
under ``date.min`` — the one key that can never collide with a real
as_of. The census's presence check reads it as covering EVERY session
(the documented semantics); the runtime surfaces answer it the same way
(``vwap_pit_surface._spot_for``).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Final

SPOT_SENTINEL_SESSION: Final = date.min  # the flat form's every-session key


class SpotTokenError(ValueError):
    """The shared refusal every spot-proxy token validation raises (each
    consumer rebrands it through `refuse` into its own error shape)."""


def validated_spot_token(
    where: str,
    session: date,
    value: object,
    *,
    refuse: Callable[[str], Exception] = SpotTokenError,
) -> Decimal:
    """(R5-P2 birthed, R6-P2 shared) ONE row discipline for the ORDINARY
    spot proxy — the same shape `_validated_spot_v2_row` gives the v2
    dollar-volume source — so an injected mapping can never carry what a
    file cannot, and the census scripts can never accept what the runtime
    refuses.

    The value must be an EXACT decimal — the file's string token, parsed,
    or an already-exact Decimal (an int stays the file path's own accepted
    token) — and must be FINITE and positive. Finiteness comes FIRST:
    `_dec` accepts "Infinity" and it is POSITIVE-looking, so the old
    `<= 0`-only gate let it LOAD — an infinite spot then flows into
    intrinsic and the election policy, where any finite bid is below
    Infinity * 0.98 and malformed input forces an early-exercise election
    — while "NaN" raises InvalidOperation on comparison and escaped as a
    raw arithmetic crash. Refusals name the underlying (`where`), the
    session, and the token; `refuse` builds each one in the CALLING
    module's own error shape (fail-closed everywhere, never a second
    validation)."""
    if isinstance(value, bool):
        raise refuse(
            f"{where}[{session.isoformat()}]: spot must be an exact decimal —"
            " a string token or a Decimal — got bool"
        )
    if type(value) is Decimal:
        spot = value
    elif isinstance(value, int):
        spot = Decimal(value)
    elif isinstance(value, str):
        try:
            spot = Decimal(value.strip())
        except ArithmeticError:
            raise refuse(
                f"{where}[{session.isoformat()}]: spot {value!r} is not a decimal"
            ) from None
    else:
        raise refuse(
            f"{where}[{session.isoformat()}]: spot must be an exact decimal —"
            f" a string token or a Decimal — got {type(value).__name__}"
        )
    if not spot.is_finite():
        raise refuse(f"{where}[{session.isoformat()}]: spot {spot} is not a finite decimal")
    if spot <= 0:
        raise refuse(f"{where}[{session.isoformat()}]: spot {spot} is not positive")
    return spot


__all__ = ["SPOT_SENTINEL_SESSION", "SpotTokenError", "validated_spot_token"]

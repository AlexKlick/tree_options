"""The desk's daily grant: base budget plus extra when sub quota is spare.

OPERATOR RULING 2026-09-28: the active desk gets a DAILY BASE GRANT that
can use EXTRA when extra is available. "Available" is subscription-quota
headroom: a window currently UNDER-USING (more left than planned at this
point in its reset window) is spare capacity the desk may convert into
work — one extra order per under-using window, capped by the rails.

The policy is PURE over a window snapshot; the snapshot is a JSON file
(``desk-paper/quota-windows.json``) an agent or the operator refreshes from
the quota dashboard (the broker at :8019 exposes raw token usage; the
planned-vs-actual comparison lives in the flow controller's dashboard).
Wiring the controller directly is a later lane; the file shape is frozen:

``{"windows": [{"name": "zai", "actual_left_pct": "94.0",
               "planned_left_pct": "51.9", "resets_at": "15:29", "dry": false}]}``

Unknown planned usage earns NO extra (fail closed); a dry window neither
adds extra nor blocks the base.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from tree_options.trex.desk_runtime import DeskPaths
from tree_options.trex.supervised import (
    SupervisedPaths,
    SupervisedRefused,
    grant_mandate,
)

WINDOWS_SCHEMA = "desk-quota-windows/1"
DEFAULT_BASE = 1
DEFAULT_CAP = 3
#: a daily grant must survive the session and the evening, nothing more
DEFAULT_TTL_S = 12 * 60 * 60


@dataclass(frozen=True)
class QuotaWindow:
    """One subscription window's state at snapshot time."""

    name: str
    actual_left_pct: Decimal
    planned_left_pct: Decimal | None
    resets_at: str | None = None
    dry: bool = False

    @property
    def under_using(self) -> bool:
        """Spare capacity: more left than planned, and not dry."""
        if self.dry or self.planned_left_pct is None:
            return False
        return self.actual_left_pct > self.planned_left_pct


@dataclass(frozen=True)
class GrantPlan:
    """The day's budget decision, with its reasons (audit-friendly)."""

    base_orders: int
    extra_orders: int
    total_orders: int
    reasons: tuple[str, ...]

    @property
    def capped(self) -> bool:
        return self.total_orders < self.base_orders + self.extra_orders


def daily_grant(
    windows: tuple[QuotaWindow, ...] = (), *, base: int = DEFAULT_BASE, cap: int = DEFAULT_CAP
) -> GrantPlan:
    """Base + one order per under-using window, never above ``cap``."""
    if base < 1:
        raise ValueError("base must be >= 1")
    if cap < base:
        raise ValueError("cap cannot be below base")
    reasons: list[str] = []
    extra = 0
    for window in windows:
        if window.under_using:
            extra += 1
            reasons.append(
                f"extra: {window.name} under-using "
                f"({window.actual_left_pct}% left vs {window.planned_left_pct}%"
                f" planned, resets {window.resets_at or '?'})"
            )
        elif window.dry:
            reasons.append(f"no extra: {window.name} dry")
        elif window.planned_left_pct is None:
            reasons.append(f"no extra: {window.name} plan unknown (fail closed)")
        else:
            reasons.append(f"no extra: {window.name} on plan or over-using")
    total = min(base + extra, cap)
    plan = GrantPlan(
        base_orders=base, extra_orders=extra, total_orders=total, reasons=tuple(reasons)
    )
    return plan


def load_windows(path: Path) -> tuple[QuotaWindow, ...]:
    """Read and validate a snapshot file; a bad file refuses (never defaults)."""
    document = json.loads(path.read_bytes())
    if document.get("schema") != WINDOWS_SCHEMA:
        raise ValueError(f"{path.name}: schema must be {WINDOWS_SCHEMA}")
    out: list[QuotaWindow] = []
    for raw in document.get("windows", []):
        out.append(
            QuotaWindow(
                name=str(raw["name"]),
                actual_left_pct=Decimal(str(raw["actual_left_pct"])),
                planned_left_pct=(
                    Decimal(str(raw["planned_left_pct"]))
                    if raw.get("planned_left_pct") is not None
                    else None
                ),
                resets_at=(str(raw["resets_at"]) if raw.get("resets_at") is not None else None),
                dry=bool(raw.get("dry", False)),
            )
        )
    return tuple(out)


def windows_path(paths: DeskPaths | None = None) -> Path:
    return (paths or DeskPaths.default()).root / "quota-windows.json"


def grant_command(
    plan: GrantPlan,
    *,
    account: str,
    owner_epoch: str,
    strategy: str,
    profile_digest: str,
    ttl_seconds: int,
    granted_by: str,
) -> str:
    """The exact command an operator (or authorized agent) runs."""
    return (
        f"python -m tree_options.trex.supervised grant --account {account} "
        f"--owner-epoch {owner_epoch} --strategy {strategy} "
        f"--profile-digest {profile_digest} --max-orders {plan.total_orders} "
        f"--ttl-seconds {ttl_seconds} --granted-by {granted_by}"
    )


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tree_options.trex.grant_policy",
        description="Compute the desk's daily grant from sub-quota headroom.",
    )
    parser.add_argument(
        "--windows",
        type=Path,
        default=None,
        help="quota snapshot (default: the desk run dir's quota-windows.json)",
    )
    parser.add_argument("--base", type=int, default=DEFAULT_BASE)
    parser.add_argument("--cap", type=int, default=DEFAULT_CAP)
    parser.add_argument("--ttl-seconds", type=int, default=DEFAULT_TTL_S)
    parser.add_argument("--account", default="DUT143714")
    parser.add_argument("--strategy", default="operational-canary/1")
    parser.add_argument("--granted-by", default="operator-terminal")
    parser.add_argument(
        "--apply", action="store_true", help="run the grant (authorized agent sessions only)"
    )
    parser.add_argument(
        "--owner-epoch", default=None, help="required with --apply (desk-paper/owner.json)"
    )
    parser.add_argument(
        "--profile-digest",
        default=None,
        help="required with --apply (supervised_desk profile-digest)",
    )
    args = parser.parse_args(argv)

    snapshot = args.windows or windows_path()
    try:
        windows = load_windows(snapshot) if snapshot.exists() else ()
    except (OSError, ValueError, KeyError) as error:
        print(f"refused: bad_snapshot {error!r}", file=sys.stderr)
        return 2
    plan = daily_grant(windows, base=args.base, cap=args.cap)
    digest = (
        args.profile_digest
        or "<digest from: python -m tree_options.trex.supervised_desk profile-digest>"
    )
    epoch = args.owner_epoch or "<epoch from: desk-paper/owner.json>"
    command = grant_command(
        plan,
        account=args.account,
        owner_epoch=epoch,
        strategy=args.strategy,
        profile_digest=digest,
        ttl_seconds=args.ttl_seconds,
        granted_by=args.granted_by,
    )
    print(
        json.dumps(
            {
                "schema": "desk-grant-plan/1",
                "base": plan.base_orders,
                "extra": plan.extra_orders,
                "total": plan.total_orders,
                "capped": plan.capped,
                "reasons": list(plan.reasons),
                "command": command,
            },
            indent=2,
        )
    )
    if not args.apply:
        return 0
    if args.owner_epoch is None or args.profile_digest is None or "<" in args.profile_digest:
        print("refused: --apply needs --owner-epoch and --profile-digest", file=sys.stderr)
        return 2
    from datetime import UTC, datetime

    try:
        mandate = grant_mandate(
            SupervisedPaths.default(),
            now=datetime.now(UTC),
            account_id=args.account,
            owner_epoch=args.owner_epoch,
            strategy_version=args.strategy,
            profile_digest=args.profile_digest,
            max_orders=plan.total_orders,
            ttl_seconds=args.ttl_seconds,
            granted_by=args.granted_by,
        )
    except (SupervisedRefused, ValueError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "granted": mandate.mandate_id,
                "max_orders": mandate.max_orders,
                "expires_at": mandate.expires_at.isoformat(),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())

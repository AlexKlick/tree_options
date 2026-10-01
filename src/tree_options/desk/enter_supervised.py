"""E6 -> supervised entry requests, SHADOW: previews only, never the inbox.

The miner's admissible deals become complete ``trex-desk-entry-request/1``
files a supervised desk WOULD accept — written to
``desk-paper/requests-preview/``, a directory the live desk never reads.
Moving the desk live on mined deals is an operator ruling (the sealed
selection rule is PROPOSED); until then these previews are the shadow
record of what would have been requested.

Fail-closed by construction: every preview carries
``strategy_version = "desk-row/<R>"``, which no operator mandate covers
(the canary profile allows ``operational-canary/1`` only), so even a
preview hand-copied into the live inbox refuses at ``active_mandate``
(scope mismatch) and never sends.

Debit-kind deals produce a pure, desk-parseable request (limit = the
deal's modeled fill, within the spec's cap — the same bound the send
boundary enforces). Credit kinds are blocked in v1 (the desk cannot open
them): a ``<deal_id>.BLOCKED.json`` diagnostic is written instead, never
a request with a hidden refusal inside it.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from tree_options.desk import paths, selection
from tree_options.desk.contracts import (
    MAX_JSON_BYTES,
    digest,
    parse_deal,
    parse_queue,
    read_json,
    timestamp,
)
from tree_options.desk.evidence import EvidenceStore
from tree_options.desk.sessions import Calendar, latest_completed_session
from tree_options.trex.clock import ET

PREVIEW_SCHEMA = "supervised-previews/1"
REQUESTED_BY = "desk-enter-shadow"
#: the account the previews are composed for; the desk's screening binds
#: the mandate's account regardless, so this is annotation, not authority
DEFAULT_ACCOUNT = "DUT143714"
_BLOCKED_KINDS = ("credit_vertical", "iron_condor", "calendar", "diagonal")


def preview_directory(run_dir: Path | None = None) -> Path:
    """Previews live under the desk run dir; the live inbox is a SIBLING."""
    from tree_options.desk.enter import execution_directory

    return (run_dir or execution_directory()) / "requests-preview"


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_bytes(json.dumps(payload, sort_keys=True, indent=2).encode())
    os.replace(tmp, path)


def write_previews(
    *,
    now: datetime,
    cal: Calendar,
    session: date | None = None,
    queue_dir: Path | None = None,
    run_dir: Path | None = None,
    database: Path | None = None,
    account_id: str = DEFAULT_ACCOUNT,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Write one preview per admissible deal. Never writes the live inbox."""
    timestamp(now.isoformat())
    latest = latest_completed_session(now, cal)
    if session is not None and session != latest:
        raise ValueError("session_not_current")
    local = now.astimezone(ET)
    result: dict[str, Any] = {
        "schema": PREVIEW_SCHEMA,
        "mode": "shadow",
        "execution_enabled": False,
        "at": now.isoformat(),
        "session": latest.isoformat(),
        "written": [],
        "blocked": [],
        "decisions": [],
    }
    if not cal.is_session(local.date()) or not time(9, 50) <= local.time() < time(11, 30):
        return {**result, "status": "outside_entry_window"}
    path = (queue_dir or paths.queue_dir()) / f"{latest.isoformat()}.json"
    if not path.exists():
        return {**result, "status": "queue_not_ready"}
    if path.is_symlink():
        raise ValueError("queue_symlink")
    with path.open("rb") as stream:
        queue = parse_queue(
            read_json(stream.read(MAX_JSON_BYTES + 1)), cal, expected_session=latest
        )
    if queue.entry_session != local.date() or not queue.cutoff <= now < queue.valid_until:
        raise ValueError("queue_not_current")
    deals = [parse_deal(d, queue, cal) for d in queue.admissible]
    from tree_options.desk.enter import execution_directory

    desk_dir = run_dir or execution_directory()
    halted = [flag for flag in ("HALT", "AUTO_OFF") if os.path.lexists(desk_dir / flag)]
    if halted:
        return {**result, "status": "halted", "halted_by": halted}
    if selection.load_config().status == "PROPOSED":
        result["selection_rule"] = "proposed_previews_only"
    from tree_options.desk.lab import default_root as lab_root
    from tree_options.desk.lab_scoreboard import aggregate, best_advisory

    advisory = best_advisory(aggregate(lab_root()))
    result["advisory"] = advisory
    previews = preview_directory(desk_dir)
    with EvidenceStore(database or _default_database(), transient=dry_run) as store:
        with store.atomic():
            store.verify()
            for deal in deals:
                preview = _preview_doc(deal, queue.entry_session, queue.valid_until, account_id)
                blocked = deal.spec.kind in _BLOCKED_KINDS
                outcome = {
                    "deal_id": deal.deal_id,
                    "row": deal.raw.get("row"),
                    "kind": deal.spec.kind,
                    "blocked": blocked,
                    "queue_sha256": queue.sha256,
                    "execution_enabled": False,
                    "advice": advisory,
                }
                if not dry_run:
                    out = previews / (
                        f"{deal.deal_id}.BLOCKED.json" if blocked else f"{deal.deal_id}.json"
                    )
                    _atomic_write(out, preview)
                result["blocked" if blocked else "written"].append(deal.deal_id)
                result["decisions"].append(outcome)
                store.put(
                    "supervised_preview", digest(preview), {**preview, "advice": advisory}, now
                )
        result["audit"] = store.verify()
    result["status"] = "ok" if result["written"] or result["blocked"] else "empty_queue"
    return result


def _default_database() -> Path:
    from tree_options.desk.shadows import database_path

    return database_path()


def _preview_doc(
    deal: Any, entry_session: date, valid_until: datetime, account_id: str
) -> dict[str, Any]:
    """The complete request doc the desk would consume (or a BLOCKED reason)."""
    from tree_options.trex.supervised_ibkr import supervised_order_ref

    spec = deal.spec
    if spec.kind in _BLOCKED_KINDS:
        return {
            "schema": "supervised-preview-blocked/1",
            "deal_id": deal.deal_id,
            "row": deal.raw.get("row"),
            "kind": spec.kind,
            "reason": "credit_open_not_supported_v1",
            "execution_enabled": False,
        }
    limit = deal.fill
    return {
        "schema": "trex-desk-entry-request/1",
        "strategy_version": f"desk-row/{deal.raw.get('row', 'unknown')}",
        "send_deadline": valid_until.isoformat(),
        "requested_by": REQUESTED_BY,
        "effect": {
            "intent_id": deal.deal_id,
            "account_id": account_id,
            "structure": spec.model_dump(mode="json"),
            "side": spec.open_side,
            "quantity": spec.quantity,
            "limit": str(limit),
            "order_ref": supervised_order_ref(deal.deal_id),
        },
    }

"""E6 -> entry PROPOSAL drafts, SHADOW: never a request, never a proposal.

The miner's admissible debit deals become ``trex-entry-proposal/1`` DRAFTS
in ``desk-paper/proposal-drafts/``, a directory the live desk never reads
(the live inbox ``requests/`` and the owner's ``proposals/`` are siblings,
never written here). Every entry is approved by the owner one at a time;
the owner flow composes the real proposal from a draft with live facts.

A draft is the proposal body with every LIVE-ONLY binding left null
(``LIVE_BINDINGS``: the proposal times, the reviewed quote, the screening
digest, the desk's owner epoch and profile digest). It is therefore never
approvable as written: ``EntryProposalBody`` refuses it by construction.
``complete_draft`` is the one way to turn it into an ``EntryProposal``,
with the live facts, inside the candidate's horizon (the queue's
``valid_until``).

The draft's effect is a complete option ``supervised-effect/1`` (limit =
the deal's modeled fill, inside the spec's cap: the same bound the send
boundary enforces). Its intent id is ``candidate_id(deal_id)``: path-safe,
because mandate/2 and spec/2 ids are file names and miner deal ids carry
``:``; the original deal id stays on the structure (``deal_id``) and the
draft. ``strategy_version`` stays ``desk-row/<R>``, which no v1 operator
mandate covers.

Credit kinds (and calendars/diagonals) are blocked in v1 (the desk cannot
open them): a ``<candidate>.BLOCKED.json`` diagnostic is written instead,
never a draft with a hidden refusal inside it. HALT / AUTO_OFF and the
entries-only pause (``PAUSE_ENTRIES``) stop drafting altogether.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime, time
from decimal import ROUND_CEILING, Decimal
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
from tree_options.trex.contracts.canonical import model_document
from tree_options.trex.contracts.effects import effect_v1_sha256
from tree_options.trex.contracts.proposals import (
    PROPOSAL_SCHEMA,
    EntryProposal,
    EntryProposalBody,
    Quote,
)
from tree_options.trex.pause import pause_blockers

PREVIEW_SCHEMA = "supervised-previews/2"
DRAFT_SCHEMA = "trex-entry-proposal-draft/1"
REQUESTED_BY = "desk-enter-shadow"
#: the program a miner draft belongs to (``trex-entry-proposal/1`` program_id)
PROGRAM_ID = "desk-miner"
#: the account the drafts are composed for; the owner flow binds the live
#: desk's account regardless, so this is annotation, not authority
DEFAULT_ACCOUNT = "DUT143714"
#: proposal fields only the live desk can attest (null in every draft)
LIVE_BINDINGS = (
    "proposed_at",
    "send_deadline",
    "review.quote",
    "review.screening_sha256",
    "review.owner_epoch",
    "review.profile_digest",
)
_BLOCKED_KINDS = ("credit_vertical", "iron_condor", "calendar", "diagonal")
_PATH_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
#: an option package is 100 shares: price x 100 per package
_OPTION_MULTIPLIER = 100


def draft_directory(run_dir: Path | None = None) -> Path:
    """Drafts live under the desk run dir; inbox and proposals are SIBLINGS."""
    from tree_options.desk.enter import execution_directory

    return (run_dir or execution_directory()) / "proposal-drafts"


def candidate_id(deal_id: str) -> str:
    """A path-safe intent id for a miner deal (deterministic, distinct).

    A path-safe deal id is used as is; any other gets its unsafe runs
    replaced by ``.`` plus a short digest of the ORIGINAL id, so two deal
    ids that sanitize alike still differ."""
    if _PATH_SAFE.fullmatch(deal_id):
        return deal_id
    stem = _UNSAFE.sub(".", deal_id).strip("._-")[:80] or "deal"
    tag = hashlib.sha256(deal_id.encode()).hexdigest()[:12]
    return f"{stem}-{tag}"


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
    """Write one draft (or BLOCKED diagnostic) per admissible deal."""
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
    paused = list(pause_blockers(desk_dir))
    if paused:
        return {**result, "status": "paused", "blockers": paused}
    if selection.load_config().status == "PROPOSED":
        result["selection_rule"] = "proposed_previews_only"
    from tree_options.desk.lab import default_root as lab_root
    from tree_options.desk.lab_scoreboard import aggregate, best_advisory

    advisory = best_advisory(aggregate(lab_root()))
    result["advisory"] = advisory
    drafts = draft_directory(desk_dir)
    with EvidenceStore(database or _default_database(), transient=dry_run) as store:
        with store.atomic():
            store.verify()
            for deal in deals:
                preview = _preview_doc(deal, queue.entry_session, queue.valid_until, account_id)
                blocked = preview["schema"] != DRAFT_SCHEMA
                cid = preview["candidate_id"]
                outcome = {
                    "deal_id": deal.deal_id,
                    "candidate_id": cid,
                    "row": deal.raw.get("row"),
                    "kind": deal.spec.kind,
                    "blocked": blocked,
                    "queue_sha256": queue.sha256,
                    "execution_enabled": False,
                    "advice": advisory,
                }
                if blocked:
                    outcome["reason"] = preview["reason"]
                else:
                    preview["queue_sha256"] = queue.sha256
                if not dry_run:
                    out = drafts / (f"{cid}.BLOCKED.json" if blocked else f"{cid}.json")
                    _atomic_write(out, preview)
                result["blocked" if blocked else "written"].append(cid)
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


def _cents(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _blocked(deal: Any, cid: str, reason: str) -> dict[str, Any]:
    return {
        "schema": "supervised-preview-blocked/1",
        "deal_id": deal.deal_id,
        "candidate_id": cid,
        "row": deal.raw.get("row"),
        "kind": deal.spec.kind,
        "reason": reason,
        "execution_enabled": False,
    }


def _preview_doc(
    deal: Any, entry_session: date, valid_until: datetime, account_id: str
) -> dict[str, Any]:
    """The proposal draft for a debit deal, or a BLOCKED diagnostic."""
    from tree_options.trex.plan import LegStructure, validate_package_order
    from tree_options.trex.supervised_ibkr import SupervisedEffect, supervised_order_ref

    spec = deal.spec
    cid = candidate_id(deal.deal_id)
    if spec.kind in _BLOCKED_KINDS:
        return _blocked(deal, cid, "credit_open_not_supported_v1")
    structure = LegStructure.model_validate({**spec.model_dump(), "id": cid})
    limit = Decimal(deal.fill)
    try:
        validate_package_order(structure, structure.open_side, structure.quantity, limit)
        effect = SupervisedEffect(
            intent_id=cid,
            account_id=account_id,
            structure=structure,
            side=structure.open_side,
            quantity=structure.quantity,
            limit=limit,
            order_ref=supervised_order_ref(cid),
        )
    except ValueError:
        return _blocked(deal, cid, "package_order_invalid")
    # a debit package costs its limit per package: the most the order pays
    # is also the most it can lose (the reviewed notional and max loss)
    cost = _cents(limit * _OPTION_MULTIPLIER * effect.quantity * 100)
    return {
        "schema": DRAFT_SCHEMA,
        "draft_of": PROPOSAL_SCHEMA,
        "deal_id": deal.deal_id,
        "candidate_id": cid,
        "row": deal.raw.get("row"),
        "kind": spec.kind,
        "entry_session": entry_session.isoformat(),
        "valid_until": valid_until.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "requested_by": REQUESTED_BY,
        "execution_enabled": False,
        "missing": list(LIVE_BINDINGS),
        "body": {
            "schema": PROPOSAL_SCHEMA,
            "effect": model_document(effect),
            "effect_sha256": effect_v1_sha256(effect),
            "strategy_version": f"desk-row/{deal.raw.get('row', 'unknown')}",
            "program_id": PROGRAM_ID,
            "sleeve_ref": None,
            "proposed_at": None,
            "send_deadline": None,
            "review": {
                "quote": None,
                "max_loss_cents": cost,
                "notional_cents": cost,
                "screening_sha256": None,
                "owner_epoch": None,
                "profile_digest": None,
            },
        },
    }


def complete_draft(
    draft: Mapping[str, Any],
    *,
    proposed_at: datetime,
    send_deadline: datetime,
    quote: Quote,
    screening_sha256: str,
    owner_epoch: str,
    profile_digest: str,
) -> EntryProposal:
    """Mint the ``trex-entry-proposal/1`` for a draft from LIVE facts.

    Every non-live field (the effect and its hash, strategy, program,
    sleeve, reviewed cost) is taken from the draft unchanged; the contract
    validators then check the whole proposal. A send deadline past the
    candidate's horizon (the queue's ``valid_until``) refuses."""
    if draft.get("schema") != DRAFT_SCHEMA or not isinstance(draft.get("body"), Mapping):
        raise ValueError("not a trex-entry-proposal draft")
    horizon = datetime.fromisoformat(str(draft["valid_until"]))
    if send_deadline.tzinfo is None or (send_deadline - horizon).total_seconds() > 0:
        raise ValueError("send_deadline is past the candidate horizon (valid_until)")
    body = dict(draft["body"])
    body["proposed_at"] = proposed_at
    body["send_deadline"] = send_deadline
    body["review"] = {
        **dict(body["review"]),
        "quote": model_document(quote),
        "screening_sha256": screening_sha256,
        "owner_epoch": owner_epoch,
        "profile_digest": profile_digest,
    }
    return EntryProposal.create(EntryProposalBody.model_validate(body))

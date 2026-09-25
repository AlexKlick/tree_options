"""Strict consumer boundary for the existing ``trex.deal/1`` miner output.

Parsing proves internal consistency, not permission to trade. Keep the miner's
sealed files and its real wire format; never infer an entry date from wall time.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

from tree_options.desk import playbook, selection
from tree_options.desk.sessions import Calendar, first_session_after, previous_session
from tree_options.trex.clock import ET
from tree_options.trex.plan import LegStructure

MAX_JSON_BYTES = 32 * 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,15}\Z")
_MONEY = re.compile(r"-?\d{1,18}(?:\.\d{1,18})?(?:[eE][+-]?\d{1,2})?\Z")


class ContractError(ValueError):
    """Stable, non-secret diagnostic code at an untrusted input boundary."""


def canonical(doc: Any) -> bytes:
    try:
        return json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError) as exc:
        raise ContractError("json_value") from exc


def digest(doc: Any) -> str:
    return hashlib.sha256(canonical(doc)).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for k, v in pairs:
        if k in obj:
            raise ContractError("duplicate_json_key")
        obj[k] = v
    return obj


def _constant(_: str) -> None:
    raise ContractError("non_finite_json")


def read_json(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_JSON_BYTES:
        raise ContractError("json_size")
    try:
        doc = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ContractError("invalid_json") from exc
    if not isinstance(doc, dict):
        raise ContractError("json_object_required")
    return doc


def money(v: Any) -> Decimal:
    if not isinstance(v, str) or not _MONEY.fullmatch(v):
        raise ContractError("money_string")
    try:
        d = Decimal(v)
    except InvalidOperation as exc:
        raise ContractError("money_string") from exc
    if not d.is_finite() or abs(d) > Decimal("1e18"):
        raise ContractError("money_finite_range")
    return d


def timestamp(v: Any) -> datetime:
    try:
        d = datetime.fromisoformat(v) if isinstance(v, str) else None
    except ValueError as exc:
        raise ContractError("aware_timestamp") from exc
    if d is None or d.tzinfo is None or d.utcoffset() is None:
        raise ContractError("aware_timestamp")
    return d


def iso_date(v: Any, code: str) -> date:
    try:
        d = date.fromisoformat(v) if isinstance(v, str) else None
    except ValueError as exc:
        raise ContractError(code) from exc
    if d is None or d.isoformat() != v:
        raise ContractError(code)
    return d


def identifier(v: Any) -> str:
    if not isinstance(v, str) or not _ID.fullmatch(v) or ".." in v:
        raise ContractError("unsafe_identifier")
    return v


@dataclass(frozen=True)
class Queue:
    raw: dict[str, Any]
    session: date
    entry_session: date
    cutoff: datetime
    valid_until: datetime
    sha256: str
    admissible: tuple[dict[str, Any], ...]
    surfaced: tuple[dict[str, Any], ...]

    @property
    def rows(self) -> tuple[dict[str, Any], ...]:
        return self.admissible + self.surfaced


@dataclass(frozen=True)
class Deal:
    deal_id: str
    spec: LegStructure
    fill: Decimal
    raw: dict[str, Any]


def parse_queue(doc: Mapping[str, Any], cal: Calendar, *, expected_session: date | None = None) -> Queue:
    if not isinstance(doc, Mapping) or doc.get("schema") != "trex.deal/1":
        raise ContractError("queue_schema")
    raw = copy.deepcopy(dict(doc))
    session = iso_date(raw.get("session"), "queue_session")
    entry = iso_date(raw.get("entry_session"), "entry_session")
    if not cal.is_session(session) or (expected_session is not None and session != expected_session):
        raise ContractError("queue_session")
    if entry != first_session_after(session, cal):
        raise ContractError("entry_session")
    cutoff, until = timestamp(raw.get("decision_cutoff")), timestamp(raw.get("valid_until"))
    if cutoff != datetime.combine(entry, time(9, 30), ET):
        raise ContractError("decision_cutoff")
    if until != datetime.combine(entry, time(11, 30), ET):
        raise ContractError("valid_until")
    cfg, pb = selection.load_config(), playbook.load_playbook()
    for key, version, sha in (("miner", cfg.version, cfg.sha256), ("playbook", pb.version, pb.sha256)):
        obj = raw.get(key)
        if not isinstance(obj, dict) or obj.get("version") != version or obj.get("sha256") != sha:
            raise ContractError(f"{key}_seal")
    if raw["miner"].get("status") != cfg.status:
        raise ContractError("miner_status")
    ids: set[str] = set()
    for bucket in ("admissible", "surfaced"):
        rows = raw.get(bucket)
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ContractError("queue_rows")
        for row in rows:
            if not isinstance(row, dict):
                raise ContractError("queue_rows")
            did = identifier(row.get("deal_id"))
            if did in ids:
                raise ContractError("duplicate_deal")
            ids.add(did)
            if (row.get("status") == "admissible") != (bucket == "admissible"):
                raise ContractError("bucket_status")
    ranks = [r.get("rank") for r in raw["admissible"]]
    if any(type(r) is not int for r in ranks) or ranks != list(range(1, len(ranks) + 1)):
        raise ContractError("queue_rank")
    return Queue(raw, session, entry, cutoff, until, digest(raw), tuple(raw["admissible"]), tuple(raw["surfaced"]))


def _leg_identity(raw: Any) -> tuple[Any, ...]:
    if not isinstance(raw, dict):
        raise ContractError("leg_shape")
    strike = money(raw.get("strike"))
    if raw.get("right") not in ("C", "P") or raw.get("action") not in ("BUY", "SELL"):
        raise ContractError("leg_shape")
    iso_date(raw.get("expiry"), "leg_expiry")
    for k in ("bid", "ask"):
        if raw.get(k) is not None:
            money(raw[k])
    ratio = raw.get("ratio", 1)
    if type(ratio) is not int or ratio != 1:
        raise ContractError("leg_ratio")
    return raw.get("right"), raw.get("action"), strike, raw.get("expiry"), ratio


def parse_deal(doc: Mapping[str, Any], queue: Queue, cal: Calendar) -> Deal:
    if not isinstance(doc, Mapping):
        raise ContractError("deal_object")
    raw = copy.deepcopy(dict(doc))
    did = identifier(raw.get("deal_id"))
    identifier(raw.get("row"))
    if not isinstance(raw.get("underlying"), str) or not _SYMBOL.fullmatch(raw["underlying"]):
        raise ContractError("underlying")
    if type(raw.get("quantity")) is not int or raw["quantity"] < 1:
        raise ContractError("quantity")
    structure = raw.get("structure")
    if not isinstance(structure, dict) or type(structure.get("quantity")) is not int:
        raise ContractError("structure_invalid")
    for key in ("limit", "ref_mid"):
        money(structure.get(key))
        money(raw.get(key))
    exits = structure.get("exits")
    if not isinstance(exits, dict):
        raise ContractError("exits_shape")
    for ex in ("take_profit", "stop_loss"):
        rule = exits.get(ex)
        if rule is not None:
            if not isinstance(rule, dict):
                raise ContractError("exits_shape")
            money(rule.get("value"))
    if not isinstance(structure.get("legs"), list) or not isinstance(raw.get("legs"), list):
        raise ContractError("legs_shape")
    inner = [_leg_identity(g) for g in structure.get("legs", [])]
    outer = [_leg_identity(g) for g in raw.get("legs", [])]
    if not inner or sorted(inner) != sorted(outer):
        raise ContractError("legs_mismatch")
    try:
        spec = LegStructure.model_validate(structure)
    except (ValueError, TypeError) as exc:
        raise ContractError("structure_invalid") from exc
    if spec.id != did or spec.deal_id != did:
        raise ContractError("deal_id_mismatch")
    for k in ("underlying", "kind", "quantity"):
        if raw.get(k) != getattr(spec, k):
            raise ContractError(f"{k}_mismatch")
    if iso_date(raw.get("entry_session"), "entry_session") != queue.entry_session or spec.entry_date != queue.entry_session:
        raise ContractError("entry_session")
    deadline = iso_date(raw.get("exit_deadline"), "exit_deadline")
    if deadline != spec.exit_deadline or not cal.is_session(deadline):
        raise ContractError("exit_deadline")
    last_hold = previous_session(spec.first_expiry, cal)
    if last_hold is None or deadline >= last_hold:
        raise ContractError("deadline_safety_buffer")
    if money(raw.get("max_loss")) != spec.max_loss():
        raise ContractError("max_loss_mismatch")
    if money(raw["limit"]) != spec.limit or money(raw["ref_mid"]) != spec.ref_mid:
        raise ContractError("price_mismatch")
    if (money(raw["width"]) if raw.get("width") is not None else None) != spec.width:
        raise ContractError("width_mismatch")
    fill = money(raw.get("fill"))
    if fill <= 0 or (spec.width is not None and fill >= spec.width):
        raise ContractError("fill_range")
    if (spec.is_credit and fill < spec.limit) or (not spec.is_credit and fill > spec.limit):
        raise ContractError("fill_limit")
    dec = raw.get("decision")
    if dec is not None:
        if not isinstance(dec, dict):
            raise ContractError("decision_shape")
        for key in ("ev", "ev_stress_fill", "ev_per_max_loss"):
            money(dec.get(key))
    return Deal(did, spec, fill, raw)

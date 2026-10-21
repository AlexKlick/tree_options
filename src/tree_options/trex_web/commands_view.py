"""Web command backend (Wave 2 lane G): authenticated, idempotent, broker-free.

Coordinator decision 2026-10-21: the desk OWNER process applies every
command; the web authenticates, validates and QUEUES ``trex-command/1``
documents into ``<run dir>/commands`` and never writes a kill file. Every
effecting route lives here:

- ``POST /api/commands``: one ``trex-command/1``. The client authors
  ``idempotency_key``, ``kind``, ``args``, ``issued_at``, ``expires_at``;
  the raw request is verified by a ``CommandAuthority`` FIRST (no actor, no
  effect: 403 and nothing written), then the backend stamps ``issued_by``
  and records it through ``CommandStore`` for the owner to consume.
  Answers the ``trex-command-result/1`` document: 202 ``accepted``, 200
  ``duplicate`` (``result`` names the stored outcome), 409 ``refused``
  (``command_body_mismatch``, or the one validity rule of
  ``command_store.command_window_blocker``: ``command_not_yet_valid``,
  ``command_expired``).
- ``POST /api/desk/supervised/{halt,flatten,resume}`` (legacy cockpit
  verbs): authenticated the same way, then translated into the matching
  kind (``halt``, ``flatten``, ``resume_all``) issued by the verified actor
  and queued; 202 with the command key. ``resume`` meant HALT and FLATTEN
  only, so while entries are paused (the file, or a queued
  ``pause_entries``) it is refused instead of widened to ``resume_all``.
- ``POST /api/desk/automation/{key}/{action}`` (timer control): there is
  no ``trex-command/1`` kind for it, so after authentication and
  validation it is refused (501) and nothing runs.
- ``GET /api/commands/{key}``: the stored (latest) result; the owner moves
  it on to ``applied``/``refused``/``failed``.
- ``GET /api/proposals``: the owner's ``trex-entry-proposal/1`` files with
  the hashes an approval repeats and their state.

Nothing here contacts a broker or imports a broker library.

The authority is pluggable (``create_app(command_authority=...)``). The
default ``LoopbackCommandAuthority`` applies ``workspace_guard``'s rules to
the raw request: explicit enablement (``TREX_WORKSPACE_CONTROLS=1``), a
loopback transport peer, a loopback ``Host``, the exact same-origin
``Origin``, and no forwarding or family actor header. The family portal
forwards the browser's public ``Host`` and ``Origin`` from loopback, so it
is refused until the family-signed authority (Wave 3) is plugged in.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from tree_options.time.sessions import shift_instant
from tree_options.trex import pause
from tree_options.trex.contracts import (
    Actor,
    AnyCommand,
    CommandAuthority,
    CommandAuthorityRefused,
    EntryProposal,
    RawCommandRequest,
    SupervisedEquityEffect,
    SupervisedMandateV2,
    Wave2RunDir,
    Wave2SupervisedDir,
    actor_binds_request,
    model_document,
    parse_command,
    proposal_bytes,
    sha256_hex,
)
from tree_options.trex.contracts.commands import CommandKind
from tree_options.trex.contracts.defaults import COMMAND_MAX_TTL_S
from tree_options.trex.contracts.layout import HALT
from tree_options.trex.desk_cli import HALT_FLATTEN_WARNING
from tree_options.trex_web.automation import AUTOMATION_UNITS, KILL_FILES
from tree_options.trex_web.command_store import (
    CommandStore,
    CommandStoreUnavailable,
    Submission,
)
from tree_options.trex_web.workspace_guard import controls_enabled

__all__ = [
    "AUTOMATION_PATH",
    "COMMANDS_PATH",
    "LEGACY_KINDS",
    "LEGACY_SUPERVISED_PATH",
    "LOOPBACK_SUB",
    "LoopbackCommandAuthority",
    "attach",
    "command_actor_guard",
    "proposals_payload",
    "raw_command_request",
    "read_bounded_body",
]

COMMANDS_PATH: Final = "/api/commands"
#: The cockpit's legacy kill-file verbs, now translated and queued.
LEGACY_SUPERVISED_PATH: Final = "/api/desk/supervised/{action}"
#: The cockpit's timer controls: no command kind, refused after validation.
AUTOMATION_PATH: Final = "/api/desk/automation/{key}/{action}"
#: legacy verb -> the ``trex-command/1`` kind it queues (``resume_all`` is
#: the contract's superset of the desk_cli resume, default 11)
LEGACY_KINDS: Final[dict[str, CommandKind]] = {
    "halt": "halt",
    "flatten": "flatten",
    "resume": "resume_all",
}
AUTOMATION_ACTIONS: Final = ("enable", "disable", "run")
#: A command document is small; anything bigger is refused before parsing.
MAX_COMMAND_BYTES: Final = 64 * 1024
#: The principal a loopback operator request is recorded as.
LOOPBACK_SUB: Final = "loopback-operator"
#: Headers a proxy adds: a request carrying one is never plain loopback.
FORWARDED_HEADERS: Final = (
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-real-ip",
)
#: The family portal's signed actor (verified by the Wave 3 family authority).
FAMILY_ACTOR_HEADER: Final = "x-family-actor"
#: HTTP status per ``trex-command-result/1`` status an intake can answer.
STATUS_CODES: Final = {"accepted": 202, "duplicate": 200, "refused": 409}
MAX_PROPOSALS: Final = 100
MAX_PROPOSAL_BYTES: Final = 256 * 1024
NO_STORE: Final = {"Cache-Control": "no-store"}
_KEY_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

Clock = Callable[[], datetime]
PathSource = Callable[[], Path]


# -- authority -----------------------------------------------------------------


def _is_loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def _refuse(detail: str) -> CommandAuthorityRefused:
    return CommandAuthorityRefused(detail=detail)


class LoopbackCommandAuthority:
    """``CommandAuthority`` for a local operator (``workspace_guard``'s rules).

    ``enabled`` is read on every request (default: ``TREX_WORKSPACE_CONTROLS``
    is ``1``); ``scheme`` is the scheme this server is reached by on
    loopback. Refusal details: ``controls_disabled``, ``wrong_peer``,
    ``family_ingress_unavailable``, ``forwarded``, ``host_invalid``,
    ``host_userinfo``, ``host_not_loopback``, ``origin_mismatch``,
    ``cross_site``.
    """

    def __init__(
        self,
        *,
        enabled: Callable[[], bool] = controls_enabled,
        scheme: str = "http",
        sub: str = LOOPBACK_SUB,
    ) -> None:
        self._enabled = enabled
        self._scheme = scheme
        self._sub = sub

    def verify(self, raw_request: RawCommandRequest) -> Actor:
        headers = raw_request.headers
        if not self._enabled():
            raise _refuse("controls_disabled")
        if not _is_loopback(raw_request.peer_host):
            raise _refuse("wrong_peer")
        if FAMILY_ACTOR_HEADER in headers:
            raise _refuse("family_ingress_unavailable")
        if any(name in headers for name in FORWARDED_HEADERS):
            raise _refuse("forwarded")
        host = headers.get("host", "")
        try:
            netloc = urlsplit(f"//{host}")
            hostname = netloc.hostname
            _ = netloc.port  # a malformed port raises ValueError
        except ValueError as error:
            raise _refuse("host_invalid") from error
        if netloc.username is not None or netloc.password is not None:
            raise _refuse("host_userinfo")
        if not host or not _is_loopback(hostname):
            raise _refuse("host_not_loopback")
        if headers.get("origin") != f"{self._scheme}://{host}":
            raise _refuse("origin_mismatch")
        if headers.get("sec-fetch-site", "same-origin") != "same-origin":
            raise _refuse("cross_site")
        return Actor(
            sub=self._sub,
            ingress="loopback",
            request_id=raw_request.request_id,
            request_sha256=sha256_hex(raw_request.body),
        )


# -- request plumbing ------------------------------------------------------------


class BodyTooLarge(Exception):
    """The request body exceeds the command limit."""


async def read_bounded_body(request: Request, limit: int = MAX_COMMAND_BYTES) -> bytes:
    """The raw body, refusing (``BodyTooLarge``) past ``limit`` bytes."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise BodyTooLarge
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise BodyTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def raw_command_request(request: Request, body: bytes) -> RawCommandRequest:
    """What the authority verifies: lower-case headers (a repeated header is
    joined, so a second ``Origin`` can never hide behind the first), the
    TRANSPORT peer (never a forwarded header) and a fresh request id."""
    headers: dict[str, str] = {}
    for name, value in request.headers.items():
        headers[name] = f"{headers[name]}, {value}" if name in headers else value
    return RawCommandRequest(
        method=request.method,
        path=request.url.path,
        headers=headers,
        body=body,
        peer_host=request.client.host if request.client is not None else "",
        request_id=f"web-{uuid.uuid4().hex}",
    )


def _authenticate(authority: CommandAuthority, raw: RawCommandRequest) -> Actor | dict[str, str]:
    """The verified actor, or the refusal document (never raises)."""
    try:
        actor = authority.verify(raw)
    except CommandAuthorityRefused as refused:
        return {"error": "command_unauthorized", "reason": refused.reason, "detail": refused.detail}
    if not actor_binds_request(actor, raw):
        return {
            "error": "command_unauthorized",
            "reason": "command_unauthorized",
            "detail": "actor_not_bound_to_request",
        }
    return actor


def command_actor_guard(authority: CommandAuthority) -> Callable[[Request], Awaitable[Actor]]:
    """A FastAPI dependency: the route runs only for a verified actor."""

    async def guard(request: Request) -> Actor:
        try:
            body = await read_bounded_body(request)
        except BodyTooLarge as error:
            raise HTTPException(status_code=413, detail={"error": "body_too_large"}) from error
        outcome = _authenticate(authority, raw_command_request(request, body))
        if not isinstance(outcome, Actor):
            raise HTTPException(status_code=403, detail=outcome)
        return outcome

    return guard


def _error(status: int, error: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": error, **extra}, status_code=status, headers=NO_STORE)


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-finite number {name}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate key {key!r}")
        document[key] = value
    return document


def _strict_json(body: bytes) -> Any:
    """JSON with no duplicate keys and no NaN/Infinity (ValueError otherwise)."""
    return json.loads(
        body.decode("utf-8"), parse_constant=_reject_constant, object_pairs_hook=_unique_object
    )


def _validation_errors(error: ValidationError) -> list[dict[str, Any]]:
    return [
        {"loc": [str(part) for part in item["loc"]], "type": item["type"], "msg": item["msg"]}
        for item in error.errors(include_url=False, include_input=False, include_context=False)
    ][:10]


# -- queueing --------------------------------------------------------------------------


async def _queue(
    store: CommandStore, command: AnyCommand, now: datetime
) -> Submission | JSONResponse:
    """``store.submit`` off the event loop; a store fault is a 503 response."""
    try:
        return await run_in_threadpool(store.submit, command, now=now)
    except CommandStoreUnavailable as error:
        return _error(503, "command_store_unavailable", reason=error.reason)
    except OSError:
        return _error(503, "command_store_unavailable", reason="commands_dir_unwritable")


def legacy_command(kind: CommandKind, action: str, actor: Actor, now: datetime) -> AnyCommand:
    """The ``trex-command/1`` a legacy cockpit verb stands for: a fresh key,
    issued now by the verified ``actor``, living the contract's full TTL."""
    return parse_command(
        {
            "schema": "trex-command/1",
            "idempotency_key": str(uuid.uuid4()),
            "kind": kind,
            "args": {"reason": f"legacy cockpit verb {action}"},
            "issued_at": now,
            "expires_at": shift_instant(now, COMMAND_MAX_TTL_S),
            "issued_by": model_document(actor),
        }
    )


def _standing_kill_files(run_dir: Path) -> list[str]:
    """HALT/FLATTEN as they stand (read only; the owner writes them)."""
    return sorted(name for name in KILL_FILES if os.path.lexists(run_dir / name))


def _entries_pause_pending(run_dir: Path) -> bool:
    """True while ``PAUSE_ENTRIES`` stands or a ``pause_entries`` command
    waits for the owner (``CommandStoreUnavailable`` when unknowable)."""
    if pause.is_paused(run_dir):
        return True
    return "pause_entries" in CommandStore(run_dir).pending_kinds()


# -- proposals ---------------------------------------------------------------------


def _iso(instant: datetime) -> str:
    """An instant as the contracts serialize it (UTC, ``Z``)."""
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _mandate_state(
    supervised: Wave2SupervisedDir, intent_id: str, proposal_id: str
) -> tuple[str | None, str | None]:
    """(state, problem): ``consumed``/``approved`` when a mandate/2 for this
    exact proposal exists; a problem reason when one cannot be read."""
    for path, state in (
        (supervised.mandate_consumed(intent_id), "consumed"),
        (supervised.mandate(intent_id), "approved"),
    ):
        try:
            if not path.exists():
                continue
            if path.is_symlink() or path.stat().st_size > MAX_PROPOSAL_BYTES:
                return None, "mandate_unreadable"
            mandate = SupervisedMandateV2.model_validate_json(path.read_bytes())
        except (OSError, ValidationError):
            return None, "mandate_unreadable"
        if mandate.proposal_id == proposal_id:
            return state, None
    return None, None


def _proposal_row(
    path: Path, layout: Wave2RunDir, supervised: Wave2SupervisedDir, now: datetime
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    def problem(reason: str) -> tuple[None, dict[str, str]]:
        return None, {"file": path.name, "reason": reason}

    try:
        if path.is_symlink() or path.stat().st_size > MAX_PROPOSAL_BYTES:
            return problem("proposal_unreadable")
        raw = path.read_bytes()
        document = _strict_json(raw)
    except (OSError, ValueError):
        return problem("proposal_unreadable")
    try:
        proposal = EntryProposal.model_validate(document)
    except ValidationError:
        return problem("proposal_invalid")
    if proposal_bytes(proposal) != raw:
        return problem("proposal_not_canonical")
    if path.name != f"{proposal.proposal_id}.json":
        return problem("proposal_id_mismatch")
    effect = proposal.effect
    status: str
    mandate_problem: str | None = None
    if layout.proposal_rejected(proposal.proposal_id).exists():
        status = "rejected"
    else:
        state, mandate_problem = _mandate_state(supervised, effect.intent_id, proposal.proposal_id)
        if mandate_problem is not None:
            status = "unknown"
        elif state is not None:
            status = state
        elif now >= proposal.send_deadline:
            status = "expired"
        else:
            status = "pending"
    row = {
        "proposal_id": proposal.proposal_id,
        "proposal_sha256": sha256_hex(raw),
        "status": status,
        "asset_kind": "equity" if isinstance(effect, SupervisedEquityEffect) else "option",
        "intent_id": effect.intent_id,
        "proposed_at": _iso(proposal.proposed_at),
        "send_deadline": _iso(proposal.send_deadline),
        "proposal": model_document(proposal),
    }
    if mandate_problem is not None:
        return row, {"file": path.name, "reason": mandate_problem}
    return row, None


def proposals_payload(run_dir: Path, supervised_dir: Path, now: datetime) -> dict[str, Any]:
    """``GET /api/proposals``: newest first; a bad file is a problem row, never dropped."""
    layout = Wave2RunDir(run_dir)
    supervised = Wave2SupervisedDir(supervised_dir)
    folder = layout.proposals()
    rows: list[dict[str, Any]] = []
    problems: list[dict[str, str]] = []
    candidates: list[tuple[float, Path]] = []
    if folder.is_dir():
        for path in folder.glob("*.json"):
            if path.name.endswith(".rejected.json"):
                continue
            try:
                candidates.append((path.lstat().st_mtime, path))
            except OSError:
                problems.append({"file": path.name, "reason": "proposal_unreadable"})
    candidates.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    for _, path in candidates[:MAX_PROPOSALS]:
        row, problem = _proposal_row(path, layout, supervised, now)
        if row is not None:
            rows.append(row)
        if problem is not None:
            problems.append(problem)
    rows.sort(key=lambda row: (row["proposed_at"], row["proposal_id"]), reverse=True)
    return {
        "schema": "trex-proposal-list/1",
        "now": _iso(now),
        "proposals": rows,
        "problems": sorted(problems, key=lambda p: p["file"]),
        "truncated": len(candidates) > MAX_PROPOSALS,
        "execution_enabled": False,
    }


# -- routes ----------------------------------------------------------------------------


def attach(
    app: FastAPI,
    *,
    authority: CommandAuthority,
    run_dir: PathSource,
    supervised_dir: PathSource,
    clock: Clock,
) -> None:
    """Mount the command routes; ``run_dir``/``supervised_dir`` resolve per request."""
    operator = Depends(command_actor_guard(authority))

    @app.post(COMMANDS_PATH)
    async def post_command(request: Request) -> JSONResponse:
        try:
            body = await read_bounded_body(request)
        except BodyTooLarge:
            return _error(413, "body_too_large")
        raw = raw_command_request(request, body)
        actor = _authenticate(authority, raw)
        if not isinstance(actor, Actor):
            return JSONResponse(actor, status_code=403, headers=NO_STORE)
        try:
            document = _strict_json(body)
        except ValueError:
            return _error(422, "invalid_json")
        if not isinstance(document, dict):
            return _error(422, "invalid_json")
        if "issued_by" in document:
            return _error(422, "issued_by_is_stamped_by_the_backend")
        try:
            command = parse_command({**document, "issued_by": model_document(actor)})
        except ValidationError as error:
            return _error(422, "invalid_command", errors=_validation_errors(error))
        # the validity window is judged by the store (one rule, every kind)
        submission = await _queue(CommandStore(run_dir()), command, clock())
        if isinstance(submission, JSONResponse):
            return submission
        result = submission.result
        return JSONResponse(
            model_document(result),
            status_code=STATUS_CODES.get(result.status, 500),
            headers=NO_STORE,
        )

    @app.post(LEGACY_SUPERVISED_PATH)
    async def legacy_supervised(action: str, actor: Actor = operator) -> JSONResponse:
        """HALT / FLATTEN / resume from the cockpit, as queued commands.

        Only for a verified actor (403 before anything else). The running
        desk owner applies the command on its next tick; this writes only
        ``commands/`` and answers 202 with the command key."""
        kind = LEGACY_KINDS.get(action)
        if kind is None:
            return _error(422, "unknown_action")
        root = run_dir()
        if kind == "resume_all":
            try:
                paused = await run_in_threadpool(_entries_pause_pending, root)
            except CommandStoreUnavailable as error:
                return _error(503, "command_store_unavailable", reason=error.reason)
            if paused:
                return _error(
                    409,
                    "legacy_resume_would_lift_entries_pause",
                    detail=(
                        "the cockpit resume lifts HALT and FLATTEN only; entries are paused, "
                        "so queue resume_all (lifts everything) or resume_entries explicitly "
                        "through POST /api/commands"
                    ),
                )
        now = clock()
        command = legacy_command(kind, action, actor, now)
        submission = await _queue(CommandStore(root), command, now)
        if isinstance(submission, JSONResponse):
            return submission
        result = submission.result
        key = command.idempotency_key
        doc: dict[str, Any] = {
            "schema": "trex-legacy-command/1",
            "command_key": key,
            "kind": kind,
            "result_url": f"{COMMANDS_PATH}/{key}",
            "queued": model_document(result),
            "kill_files": _standing_kill_files(root),
            "execution_enabled": False,
        }
        if kind == "flatten" and os.path.lexists(root / HALT):
            doc["warning"] = HALT_FLATTEN_WARNING
        return JSONResponse(doc, status_code=STATUS_CODES.get(result.status, 500), headers=NO_STORE)

    @app.post(AUTOMATION_PATH, dependencies=[operator])
    def legacy_automation(key: str, action: str) -> JSONResponse:
        """Timer control: validated for a verified actor, then refused.

        ``trex-command/1`` has no kind for it, so it cannot be queued for
        the owner, and the web applies nothing itself: no systemctl call,
        no audit line."""
        if action not in AUTOMATION_ACTIONS:
            return _error(422, "unknown_action")
        if key not in AUTOMATION_UNITS:
            return _error(404, "unknown_unit")
        return _error(
            501,
            "automation_not_queueable",
            detail=(
                "timer control has no trex-command/1 kind; the web applies no command "
                "itself. Use systemctl --user on the host."
            ),
        )

    @app.get(COMMANDS_PATH + "/{key}")
    def get_command(key: str) -> JSONResponse:
        if not _KEY_RE.fullmatch(key):
            return _error(404, "unknown_command")
        try:
            result = CommandStore(run_dir()).read_result(key)
        except CommandStoreUnavailable as error:
            return _error(503, "command_store_unavailable", reason=error.reason)
        if result is None:
            return _error(404, "unknown_command")
        return JSONResponse(model_document(result), headers=NO_STORE)

    @app.get("/api/proposals")
    def get_proposals() -> JSONResponse:
        return JSONResponse(
            proposals_payload(run_dir(), supervised_dir(), clock()), headers=NO_STORE
        )

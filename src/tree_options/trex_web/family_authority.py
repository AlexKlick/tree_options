"""Wave 3 lane B: the family command authority (C3.1, docs/product/WAVE3-CONTRACTS.md).

The browser reaches ``POST /api/commands`` only through the Family Apps
portal, which mints a short-lived ``trex-family-command/1`` assertion bound
to the exact request and forwards it as ``x-family-actor``. This module
turns the contract's pure verifier (``wave3.verify_family_command``) into a
Wave 2 ``CommandAuthority``. The web still only QUEUES commands; nothing
here contacts a broker or applies a command.

- Key loading, by NAME only: ``TREX_FAMILY_COMMAND_KEY_FILE`` (default
  ``~/.config/trex/family-command.key``) in family-apps' pairing-key format
  (``parse_command_key``): an absolute path to a regular owner-only file
  (no group or other permission bits) of at most 1 KiB, never a symlink. The
  file is read per request, so installing, rotating or removing the key
  needs no restart. Its bytes never reach a log line, a repr or an error.
- Configuration: ``TREX_OWNER_EMAILS``, ``TREX_FAMILY_ORIGINS`` and
  ``TREX_FAMILY_AUDIENCE`` (``trex-command`` for ``/trex/``,
  ``trex-staging-command`` for ``/trex-staging/``). A missing or invalid
  piece fails closed: the refusal is ``family_ingress_disabled`` (the
  contract's detail) and ``FamilyIngressDisabled.problems`` /
  ``FamilyCommandAuthority.status()`` name the cause (``key_file_missing``
  and the other ``IngressProblem`` codes); a change of cause is logged once
  with the key file's path.
- The durable replay ledger ``<run dir>/authority/assertions/<jti>.json``
  (``trex-family-assertion-seen/1``): the entry is written to a private
  temp file, fsynced, then published with ``link(2)``, which fails when the
  name exists exactly as ``O_CREAT|O_EXCL`` does. So one use of a ``jti``
  ever succeeds, an entry is complete or absent, a second use is
  ``assertion_replayed`` and any other failure is
  ``assertion_ledger_unavailable`` (fail closed). The actor is returned only
  after the entry is durable. ``prune_assertions`` removes entries once
  ``exp`` + 5 s has passed (the assertion is refused as expired by then).
  The web must be able to write that directory: trex-web runs with
  ``ProtectHome=read-only``, so ``deploy/trex/trex-web.service`` grants
  ``<run dir>/authority/assertions`` (created 0700 before the unit starts).
  ``family_ingress_status(run_dir=...)`` and ``FamilyCommandAuthority.status()``
  probe it without writing and report ``assertion_ledger_unwritable``, so
  Today and onboarding never read a family ingress as enabled that the
  ledger would refuse.
- Dispatch (``wave3.authority_for``): a request carrying ``x-family-actor``
  is judged by the family verifier ONLY, never falling back; any other
  request by the Wave 2 ``LoopbackCommandAuthority``, which refuses
  forwarded headers and a public Host/Origin. A forwarded identity without
  a valid assertion is therefore always refused.

Wiring (integration coordinator, ``trex_web/app.py`` ``create_app``): the
default authority line becomes

    authority = command_authority if command_authority is not None else dispatching_authority(run_dir=resolve_run_dir, clock=command_clock)

with ``from tree_options.trex_web.family_authority import dispatching_authority``
beside the other local imports. ``command_clock=None`` means the wall clock.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import secrets
import stat
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum, unique
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from tree_options.trex.contracts import (
    Actor,
    CommandAuthority,
    CommandAuthorityRefused,
    RawCommandRequest,
    model_bytes,
)
from tree_options.trex.contracts.vocabulary import Blocker
from tree_options.trex.contracts.wave3 import (
    FAMILY_COMMAND_MAX_SKEW_S,
    FAMILY_KEY_FILE_DEFAULT,
    TREX_FAMILY_AUDIENCE_ENV,
    TREX_FAMILY_ORIGINS_ENV,
    TREX_KEY_FILE_ENV,
    TREX_OWNER_EMAILS_ENV,
    AssertionSeen,
    FamilyAuthorityConfig,
    FamilyCommandClaims,
    FamilyRefusal,
    Wave3RunDir,
    assertion_seen_record,
    authority_for,
    family_config_from_env,
    parse_command_key,
    parse_origins,
    parse_owner_emails,
    verify_family_command,
)
from tree_options.trex_web.commands_view import LoopbackCommandAuthority

__all__ = [
    "KEY_FILE_MAX_BYTES",
    "DispatchingCommandAuthority",
    "FamilyCommandAuthority",
    "FamilyIngressDisabled",
    "FamilyIngressStatus",
    "FamilyKeyUnavailable",
    "IngressProblem",
    "dispatching_authority",
    "family_ingress_status",
    "key_file_path",
    "ledger_problem",
    "load_command_key",
    "prune_assertions",
    "record_assertion",
]

logger = logging.getLogger(__name__)

#: family-apps' pairing-key files are at most this big.
KEY_FILE_MAX_BYTES: Final = 1024
#: A ledger entry is small; a bigger file is never parsed (prune skips it).
LEDGER_ENTRY_MAX_BYTES: Final = 4096
#: ``prune_assertions`` looks at no more names than this per call.
PRUNE_SCAN_LIMIT: Final = 10_000
_ENTRY_NAME = re.compile(r"^[0-9a-f]{32}\.json$")

Clock = Callable[[], datetime]
RunDirSource = Callable[[], Path]


@unique
class IngressProblem(StrEnum):
    """Why family ingress is disabled (``FamilyIngressDisabled.problems``).

    Never a key value: only what is wrong with the file or the setting."""

    KEY_FILE_NOT_ABSOLUTE = "key_file_not_absolute"
    KEY_FILE_MISSING = "key_file_missing"
    KEY_FILE_SYMLINK = "key_file_symlink"
    KEY_FILE_NOT_REGULAR = "key_file_not_regular"
    KEY_FILE_PERMISSIONS = "key_file_permissions"
    KEY_FILE_TOO_LARGE = "key_file_too_large"
    KEY_FILE_UNREADABLE = "key_file_unreadable"
    KEY_INVALID = "key_invalid"
    OWNERS_MISSING = "owners_missing"
    OWNERS_INVALID = "owners_invalid"
    ORIGINS_MISSING = "origins_missing"
    ORIGINS_INVALID = "origins_invalid"
    AUDIENCE_INVALID = "audience_invalid"
    #: ``<run dir>/authority/assertions`` cannot be written by this process
    #: (e.g. trex-web's ``ProtectHome=read-only`` without that grant), so
    #: every family command would be refused ``assertion_ledger_unavailable``.
    #: Reported by the status only (``family_ingress_status(run_dir=...)``).
    ASSERTION_LEDGER_UNWRITABLE = "assertion_ledger_unwritable"


def _refused(detail: FamilyRefusal) -> CommandAuthorityRefused:
    return CommandAuthorityRefused(reason=Blocker.COMMAND_UNAUTHORIZED, detail=detail.value)


class FamilyIngressDisabled(CommandAuthorityRefused):
    """``family_ingress_disabled`` (the contract's detail) with its causes."""

    def __init__(self, problems: tuple[str, ...]) -> None:
        super().__init__(
            reason=Blocker.COMMAND_UNAUTHORIZED,
            detail=FamilyRefusal.FAMILY_INGRESS_DISABLED.value,
        )
        self.problems = problems

    def __str__(self) -> str:
        return f"{self.reason}: {self.detail} ({', '.join(self.problems)})"


class FamilyKeyUnavailable(Exception):
    """The key file cannot be used; ``problem`` says why (never its bytes)."""

    def __init__(self, problem: IngressProblem) -> None:
        super().__init__(problem.value)
        self.problem = problem


# -- key loading -------------------------------------------------------------------------


def key_file_path(env: Mapping[str, str]) -> Path:
    """``TREX_FAMILY_COMMAND_KEY_FILE`` or the default, ``~`` expanded."""
    return Path(env.get(TREX_KEY_FILE_ENV, "").strip() or FAMILY_KEY_FILE_DEFAULT).expanduser()


def _read_at_most(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while size < limit:
        chunk = os.read(fd, limit - size)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def load_command_key(path: Path) -> bytes:
    """The 32-byte HMAC key from ``path`` (family-apps' ``readPairingKey``
    rules), or ``FamilyKeyUnavailable``. Errors never chain the file's text."""
    if not path.is_absolute():
        raise FamilyKeyUnavailable(IngressProblem.KEY_FILE_NOT_ABSOLUTE)
    try:
        # O_NONBLOCK: a FIFO planted at the path must not hang the request
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise FamilyKeyUnavailable(IngressProblem.KEY_FILE_MISSING) from None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise FamilyKeyUnavailable(IngressProblem.KEY_FILE_SYMLINK) from None
        raise FamilyKeyUnavailable(IngressProblem.KEY_FILE_UNREADABLE) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise FamilyKeyUnavailable(IngressProblem.KEY_FILE_NOT_REGULAR)
        if info.st_mode & 0o077:
            raise FamilyKeyUnavailable(IngressProblem.KEY_FILE_PERMISSIONS)
        if info.st_size > KEY_FILE_MAX_BYTES:
            raise FamilyKeyUnavailable(IngressProblem.KEY_FILE_TOO_LARGE)
        data = _read_at_most(fd, KEY_FILE_MAX_BYTES + 1)
    except OSError:
        raise FamilyKeyUnavailable(IngressProblem.KEY_FILE_UNREADABLE) from None
    finally:
        os.close(fd)
    if len(data) > KEY_FILE_MAX_BYTES:
        raise FamilyKeyUnavailable(IngressProblem.KEY_FILE_TOO_LARGE)
    try:
        return parse_command_key(data.decode("ascii"))
    except ValueError:  # UnicodeDecodeError included; its text would quote a byte
        raise FamilyKeyUnavailable(IngressProblem.KEY_INVALID) from None


# -- configuration -------------------------------------------------------------------------


@dataclass(frozen=True)
class FamilyIngressStatus:
    """Whether family commands can be authorized now, and if not, why.

    ``key_file`` is the path (a name, never contents); ``audience`` is None
    only when ``TREX_FAMILY_AUDIENCE`` is invalid."""

    enabled: bool
    problems: tuple[str, ...]
    key_file: str
    audience: str | None


def _resolve(env: Mapping[str, str]) -> tuple[FamilyAuthorityConfig | None, FamilyIngressStatus]:
    """The verifier config (None when disabled) and the status, from ``env``
    and the key file. Every setting is judged by the contract's own parser."""
    problems: list[IngressProblem] = []
    path = key_file_path(env)
    key = b""
    try:
        key = load_command_key(path)
    except FamilyKeyUnavailable as unavailable:
        problems.append(unavailable.problem)
    for name, parse, missing, invalid in (
        (
            TREX_OWNER_EMAILS_ENV,
            parse_owner_emails,
            IngressProblem.OWNERS_MISSING,
            IngressProblem.OWNERS_INVALID,
        ),
        (
            TREX_FAMILY_ORIGINS_ENV,
            parse_origins,
            IngressProblem.ORIGINS_MISSING,
            IngressProblem.ORIGINS_INVALID,
        ),
    ):
        try:
            if not parse(env.get(name, "")):
                problems.append(missing)
        except ValueError:
            problems.append(invalid)
    audience: str | None
    try:
        audience = family_config_from_env(
            {TREX_FAMILY_AUDIENCE_ENV: env.get(TREX_FAMILY_AUDIENCE_ENV, "")}, b""
        ).audience
    except ValueError:
        audience = None
        problems.append(IngressProblem.AUDIENCE_INVALID)
    config = None if problems else family_config_from_env(env, key)
    status = FamilyIngressStatus(
        enabled=config is not None and config.enabled,
        problems=tuple(problem.value for problem in problems),
        key_file=str(path),
        audience=audience,
    )
    return config, status


def ledger_problem(run_dir: Path) -> IngressProblem | None:
    """``ASSERTION_LEDGER_UNWRITABLE`` when ``record_assertion`` could not
    write ``<run dir>/authority/assertions``; judged WITHOUT writing.

    ``authority`` and ``assertions`` must be real directories when present
    (``_ledger_dir`` refuses anything else). The deepest existing directory
    on the ledger's path must then be one this process may write and search:
    ``access(2)`` answers no on a read-only mount too (``EROFS``), which is
    how ``ProtectHome=read-only`` without a ``ReadWritePaths`` grant looks."""
    folder = Wave3RunDir(run_dir).assertions()
    try:
        for path in (folder.parent, folder):
            if os.path.lexists(path) and not _real_dir(path):
                return IngressProblem.ASSERTION_LEDGER_UNWRITABLE
        deepest = folder
        # the nearest existing path: a missing run dir is created with its
        # parents, so that one must be a directory (symlinks followed)
        while not os.path.lexists(deepest) and deepest != deepest.parent:
            deepest = deepest.parent
        writable = deepest.is_dir() and os.access(deepest, os.W_OK | os.X_OK)
    except OSError:
        return IngressProblem.ASSERTION_LEDGER_UNWRITABLE
    return None if writable else IngressProblem.ASSERTION_LEDGER_UNWRITABLE


def _status(env: Mapping[str, str], run_dir: Path | None) -> FamilyIngressStatus:
    status = _resolve(env)[1]
    problem = None if run_dir is None else ledger_problem(run_dir)
    if problem is None:
        return status
    return replace(status, enabled=False, problems=(*status.problems, problem.value))


def family_ingress_status(
    env: Mapping[str, str] | None = None, *, run_dir: Path | None = None
) -> FamilyIngressStatus:
    """The family ingress status for ``env`` (default: the process
    environment); read-only, for views (``family_commands_disabled``).

    With ``run_dir`` it also names a replay ledger this process cannot
    write (``assertion_ledger_unwritable``, after the configuration
    problems): the verifier refuses every command then, whatever the key."""
    return _status(os.environ if env is None else env, run_dir)


# -- the replay ledger -------------------------------------------------------------------------


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _real_dir(path: Path) -> bool:
    return stat.S_ISDIR(os.lstat(path).st_mode)


def _ledger_dir(run_dir: Path) -> Path:
    """``<run dir>/authority/assertions``, created 0700; a symlinked or
    non-directory ``authority`` or ``assertions`` raises (fail closed)."""
    folder = Wave3RunDir(run_dir).assertions()
    run_dir.mkdir(parents=True, exist_ok=True)
    for path in (folder.parent, folder):
        path.mkdir(mode=0o700, exist_ok=True)
        if not _real_dir(path):
            raise NotADirectoryError(errno.ENOTDIR, "not a real directory", str(path))
    return folder


def _write_new(path: Path, payload: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)


def record_assertion(
    run_dir: Path, claims: FamilyCommandClaims, *, backend_request_id: str, seen_at: datetime
) -> Path:
    """Consume ``claims.jti`` once, durably: the entry's path, or
    ``assertion_replayed`` (the jti was used) / ``assertion_ledger_unavailable``."""
    try:
        payload = model_bytes(
            assertion_seen_record(claims, backend_request_id=backend_request_id, seen_at=seen_at)
        )
    except ValidationError as error:
        raise _refused(FamilyRefusal.ASSERTION_LEDGER_UNAVAILABLE) from error
    entry = Wave3RunDir(run_dir).assertion(claims.jti)
    try:
        folder = _ledger_dir(run_dir)
        temp = folder / f".{claims.jti}.{secrets.token_hex(8)}.tmp"
        try:
            _write_new(temp, payload)
            try:
                os.link(temp, entry)  # exclusive: fails when the jti was seen
                published = True
            except FileExistsError:
                published = False
        finally:
            temp.unlink(missing_ok=True)
        if published:
            _fsync_dir(folder)
    except OSError as error:
        raise _refused(FamilyRefusal.ASSERTION_LEDGER_UNAVAILABLE) from error
    if not published:
        raise _refused(FamilyRefusal.ASSERTION_REPLAYED)
    return entry


def prune_assertions(run_dir: Path, now: datetime, *, limit: int = PRUNE_SCAN_LIMIT) -> int:
    """Delete ledger entries whose ``exp`` + 5 s has passed at ``now``; the
    number deleted. Entries that cannot be read or parsed are kept."""
    folder = Wave3RunDir(run_dir).assertions()
    try:
        if not (_real_dir(folder.parent) and _real_dir(folder)):
            return 0
        names = sorted(name for name in os.listdir(folder) if _ENTRY_NAME.fullmatch(name))
    except FileNotFoundError:
        return 0
    now_s = int(now.timestamp())
    removed = 0
    for name in names[:limit]:
        path = folder / name
        try:
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or info.st_size > LEDGER_ENTRY_MAX_BYTES:
                continue
            seen = AssertionSeen.model_validate_json(path.read_bytes())
        except (OSError, ValidationError):
            continue
        if f"{seen.jti}.json" != name or now_s <= seen.exp + FAMILY_COMMAND_MAX_SKEW_S:
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        removed += 1
    return removed


# -- the authorities -------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(UTC)


class FamilyCommandAuthority:
    """``CommandAuthority`` for portal-forwarded family commands (C3.1).

    ``run_dir`` resolves the desk run dir per request (the ledger lives
    under it); ``clock`` defaults to the wall clock; ``env`` defaults to the
    process environment, read per request like the key file."""

    def __init__(
        self,
        *,
        run_dir: RunDirSource,
        clock: Clock | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._run_dir = run_dir
        self._clock = clock if clock is not None else _utc_now
        self._env = env
        self._reported: tuple[str, ...] | None = None
        self._report_lock = threading.Lock()

    def _environment(self) -> Mapping[str, str]:
        return os.environ if self._env is None else self._env

    def status(self) -> FamilyIngressStatus:
        """The configuration and this authority's replay ledger (read-only)."""
        return _status(self._environment(), self._run_dir())

    def _report(self, status: FamilyIngressStatus) -> None:
        """Log each change of the enabled/disabled cause once."""
        with self._report_lock:
            if status.problems == self._reported:
                return
            self._reported = status.problems
        if status.problems:
            logger.warning(
                "TREX family command ingress disabled: %s (key file %s)",
                ", ".join(status.problems),
                status.key_file,
            )
        else:
            logger.info("TREX family command ingress enabled (audience %s)", status.audience)

    def verify(self, raw_request: RawCommandRequest) -> Actor:
        now = self._clock()
        config, status = _resolve(self._environment())
        self._report(status)
        if config is None:
            raise FamilyIngressDisabled(status.problems)
        actor, claims = verify_family_command(raw_request, config=config, now=now)
        try:
            record_assertion(
                self._run_dir(), claims, backend_request_id=raw_request.request_id, seen_at=now
            )
        except CommandAuthorityRefused as refused:
            logger.warning(
                "TREX family command refused: %s (request %s)",
                refused.detail,
                raw_request.request_id,
            )
            raise
        return actor

    def prune(self) -> int:
        """``prune_assertions`` on this authority's run dir at its clock."""
        return prune_assertions(self._run_dir(), self._clock())


class DispatchingCommandAuthority:
    """C3.1 dispatch: ``x-family-actor`` -> ``family`` ONLY (no fallback);
    every other request -> ``loopback``."""

    def __init__(self, *, family: CommandAuthority, loopback: CommandAuthority) -> None:
        self.family = family
        self.loopback = loopback

    def verify(self, raw_request: RawCommandRequest) -> Actor:
        if authority_for(raw_request) == "family":
            return self.family.verify(raw_request)
        return self.loopback.verify(raw_request)


def dispatching_authority(
    *,
    run_dir: RunDirSource,
    clock: Clock | None = None,
    env: Mapping[str, str] | None = None,
    loopback: CommandAuthority | None = None,
) -> DispatchingCommandAuthority:
    """The authority ``create_app`` should default to: the family verifier
    beside the Wave 2 ``LoopbackCommandAuthority`` (``loopback`` overrides it)."""
    return DispatchingCommandAuthority(
        family=FamilyCommandAuthority(run_dir=run_dir, clock=clock, env=env),
        loopback=loopback if loopback is not None else LoopbackCommandAuthority(),
    )

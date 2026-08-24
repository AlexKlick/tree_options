"""Unified capture manifest for the Massive (Polygon) structural lane (M4-B).

A capture directory (`scripts/capture_massive_structural.py` output) is the
ONLY thing that crosses from the live wire to the analysis side, so it is
pinned end to end by ONE manifest:

- every `masters/*.json`, `bars/*.json` and `spot_proxy.json` file is listed
  with the sha256 of its RAW bytes and its byte length — never a
  re-serialisation, which would route the vendor's exact number tokens
  (`587.5`) through a float and launder them;
- the run's own accounting (capture/provider/schema tokens, budget charged,
  client stats, per-master completeness, notes) rides on the same model, so
  the manifest describes HOW the bytes were obtained, not just that they
  exist;
- `content_sha256` binds all of the above: sha256 over
  `MASSIVE_MANIFEST_DOMAIN || canonical_bytes(manifest-with-empty-hash)`,
  the same domain separation `cboe_eod` uses — the manifest cannot be edited
  (a flipped row count, a swapped file hash) without breaking the binding.

Verification is reconciliation, not just re-hashing. `verify_...` refuses,
naming the file and the reason, when: a token mismatches the constants or
the expected capture version; a listed file is missing, re-hashes
differently, or changed length; ANY `*.json` sits under `masters/`, `bars/`
or the capture-dir root that the manifest does not list (an unlisted file
is unprovenance — it could be anything); or the content binding fails to
recompute. The one exemption is the manifest's own file
(`capture_manifest.json`): a manifest cannot contain its own hash, so it is
pinned by `content_sha256` instead and is exempt from the disk scan BY NAME.

No price number ever lands on this model. Spot-proxy closes are strings and
client stats are plain counters, so `json` (not `loads_exact`) decodes the
manifest file without any exactness to lose.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Literal, NoReturn

from pydantic import NonNegativeInt, ValidationError

from tree_options.data.digest import canonical_bytes, sha256_hex
from tree_options.data.massive_client import MassiveError
from tree_options.data.massive_options import MASSIVE_PROVIDER
from tree_options.schemas.common import StrictModel

MASSIVE_MANIFEST_SCHEMA_VERSION = "m4b-manifest/1"
MASSIVE_MANIFEST_DOMAIN = b"tree-options-m4b-massive-capture-v1"

# The manifest's own name inside a capture directory (what the bridge
# writes). Exempted from disk reconciliation BY NAME — see module docstring.
CAPTURE_MANIFEST_FILENAME = "capture_manifest.json"

MASTERS_DIR = "masters"
BARS_DIR = "bars"
SPOT_PROXY_FILENAME = "spot_proxy.json"

CaptureKind = Literal["master", "bar", "spot_proxy"]


class MassiveManifestError(MassiveError):
    """A capture manifest does not describe the capture directory on disk —
    refused with the file and the reason, never repaired."""


# ---- models -------------------------------------------------------------------


class CaptureFile(StrictModel):
    """One pinned capture file: path relative to the capture directory, the
    sha256 of its RAW bytes, its byte length, and which leg of the capture
    it belongs to."""

    path: str
    sha256: str
    bytes: NonNegativeInt
    kind: CaptureKind


class MasterEntry(StrictModel):
    """One contract-master capture: what it covers and whether it is whole.

    Mirrors the bridge's per-master accounting; `file` is the bare filename
    inside `masters/` (or `None` when nothing was written for this master)."""

    underlying: str
    as_of: str
    pages: NonNegativeInt
    rows: NonNegativeInt
    complete: bool
    truncated: bool
    error: str | None
    file: str | None


class MassiveCaptureManifest(StrictModel):
    """Immutable lineage for one Massive structural capture: the pinned
    files, the run's request accounting, per-master completeness, and a
    domain-separated `content_sha256` binding all of it."""

    provider: str = MASSIVE_PROVIDER
    schema_version: str = MASSIVE_MANIFEST_SCHEMA_VERSION
    capture_version: str
    budget_limit: NonNegativeInt
    requests_charged: NonNegativeInt
    client_stats: dict[str, int | float]
    masters: tuple[MasterEntry, ...]
    bars: tuple[str, ...]
    spot_proxy: dict[str, dict[str, str]]
    files: tuple[CaptureFile, ...]
    notes: tuple[str, ...]
    content_sha256: str


# ---- capture-directory scanning ------------------------------------------------


def _kind_of(relative_path: str) -> CaptureKind | None:
    """Which leg of the capture a relative path belongs to, or `None` when
    this lane does not own it."""
    if relative_path.startswith(f"{MASTERS_DIR}/"):
        return "master"
    if relative_path.startswith(f"{BARS_DIR}/"):
        return "bar"
    if relative_path == SPOT_PROXY_FILENAME:
        return "spot_proxy"
    return None


def _json_files_on_disk(capture_dir: Path) -> set[str]:
    """Every `*.json` this lane owns on disk, as capture-relative posix
    paths: the direct children of `masters/` and `bars/`, plus the
    capture-dir root (the manifest's own file excepted — see docstring)."""
    found: set[str] = set()
    for root in (MASTERS_DIR, BARS_DIR):
        directory = capture_dir / root
        if directory.is_dir():
            found.update(
                f"{root}/{entry.name}"
                for entry in directory.iterdir()
                if entry.is_file() and entry.suffix == ".json"
            )
    if capture_dir.is_dir():
        found.update(
            entry.name
            for entry in capture_dir.iterdir()
            if entry.is_file()
            and entry.suffix == ".json"
            and entry.name != CAPTURE_MANIFEST_FILENAME
        )
    return found


def _capture_file_of(capture_dir: Path, relative_path: str) -> CaptureFile:
    """Hash one on-disk capture file over its RAW bytes."""
    kind = _kind_of(relative_path)
    if kind is None:  # pragma: no cover - the scan only yields owned roots
        raise MassiveManifestError(
            f"{capture_dir / relative_path}: not a masters/, bars/ or"
            f" {SPOT_PROXY_FILENAME} capture file — refusing to describe it"
        )
    try:
        raw = (capture_dir / relative_path).read_bytes()
    except OSError as exc:
        raise MassiveManifestError(
            f"{capture_dir / relative_path}: unreadable ({exc.strerror})"
        ) from None
    return CaptureFile(path=relative_path, sha256=sha256_hex(raw), bytes=len(raw), kind=kind)


def _resolve_listed(capture_dir: Path, relative_path: str) -> Path:
    """The absolute path of a listed file, refusing escapes.

    The manifest is data read off disk: an absolute path or a `..` hop would
    turn "re-hash the listed files" into "read arbitrary host files"."""
    posix = PurePosixPath(relative_path)
    if posix.is_absolute() or ".." in posix.parts or not posix.parts:
        raise MassiveManifestError(
            f"listed path {relative_path!r} is not a file inside the capture directory"
        )
    return capture_dir.joinpath(*posix.parts)


def _content_sha256_of(manifest: MassiveCaptureManifest) -> str:
    """The domain-separated binding: the domain prefix keeps a manifest
    body from ever being replayed as some other hash input, exactly as
    `cboe_eod.REAL_MANIFEST_DOMAIN` does for the Cboe lane."""
    core = manifest.model_copy(update={"content_sha256": ""})
    return sha256_hex(MASSIVE_MANIFEST_DOMAIN + canonical_bytes(core))


# ---- build ---------------------------------------------------------------------


def _coerce_master(entry: object, *, index: int) -> MasterEntry:
    """A loose mapping (the bridge's plain dict) -> `MasterEntry`.

    `MasterEntry` is a `StrictModel`, so an unknown key or a wrong shape is
    refused by validation — this wrapper only adds the position to the
    message, so a bad entry is named rather than buried in a page of
    pydantic output."""
    if isinstance(entry, MasterEntry):
        return entry
    if not isinstance(entry, Mapping):
        raise MassiveManifestError(
            f"master entry {index}: {type(entry).__name__} is not a mapping of master fields"
        )
    try:
        return MasterEntry.model_validate(dict(entry))
    except ValidationError as exc:
        raise MassiveManifestError(f"master entry {index}: {exc}") from None


def build_massive_capture_manifest(
    capture_dir: Path,
    *,
    capture_version: str,
    budget_limit: int,
    requests_charged: int,
    client_stats: Mapping[str, int | float],
    masters: Sequence[Mapping[str, object]] | Sequence[MasterEntry],
    bars: Sequence[str],
    spot_proxy: Mapping[str, Mapping[str, str]],
    notes: Sequence[str],
) -> MassiveCaptureManifest:
    """Scan `capture_dir`, hash every capture file, and bind the run's
    accounting into one manifest.

    The file listing comes from the DIRECTORY, not from the caller: anything
    the caller forgot to mention is still pinned, and any `*.json` this lane
    does not own (a stray file at the capture root) is refused here rather
    than verified-then-rejected later."""
    files = tuple(
        _capture_file_of(capture_dir, relative)
        for relative in sorted(_json_files_on_disk(capture_dir))
    )
    manifest = MassiveCaptureManifest(
        capture_version=capture_version,
        budget_limit=budget_limit,
        requests_charged=requests_charged,
        client_stats=dict(client_stats),
        masters=tuple(_coerce_master(entry, index=i) for i, entry in enumerate(masters)),
        bars=tuple(bars),
        spot_proxy={name: dict(sessions) for name, sessions in spot_proxy.items()},
        files=files,
        notes=tuple(notes),
        content_sha256="",
    )
    return manifest.model_copy(update={"content_sha256": _content_sha256_of(manifest)})


# ---- load ----------------------------------------------------------------------


def load_massive_capture_manifest(
    path: Path, *, raw: bytes | None = None
) -> MassiveCaptureManifest:
    """A manifest JSON file -> `MassiveCaptureManifest`, or a refusal naming
    the path.

    The manifest carries no price numbers (spot closes are strings, stats
    are counters), so plain `json` loses nothing here. A file that is
    missing, not JSON, or not the pinned shape — including extra keys — is
    a `MassiveManifestError`; the verify path must never answer "verified"
    for a manifest it could not load.

    Round-7 review fix (2026-08-24, finding 3): ``raw`` lets a caller that
    must read the file exactly ONCE (verify, hash, and consume the same
    bytes — the same discipline ``massive_overlay._load_spot`` grew in the
    R7 wave) parse its one read instead of the loader re-reading the path.
    The default (``None``) keeps the current read-from-path behavior."""
    if raw is None:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise MassiveManifestError(f"{path}: manifest unreadable ({exc.strerror})") from None
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise MassiveManifestError(f"{path}: manifest is not JSON ({exc})") from None
    try:
        return MassiveCaptureManifest.model_validate(payload)
    except ValidationError as exc:
        raise MassiveManifestError(
            f"{path}: manifest does not match the pinned shape ({exc})"
        ) from None


# ---- verify --------------------------------------------------------------------


def verify_massive_capture_manifest(
    manifest: MassiveCaptureManifest,
    capture_dir: Path,
    *,
    capture_version: str,
) -> None:
    """Fail-closed reconciliation of (manifest, capture directory).

    Tokens are checked first — a foreign manifest is refused before it is
    used to read anything — then every listed file is re-hashed from raw
    bytes, then the disk is reconciled against the listing (an unlisted
    `*.json` is unprovenance), then the content binding is recomputed."""

    def fail(detail: str) -> NoReturn:
        raise MassiveManifestError(f"massive capture manifest for {capture_dir}: {detail}")

    if manifest.schema_version != MASSIVE_MANIFEST_SCHEMA_VERSION:
        fail(f"schema_version {manifest.schema_version!r} != {MASSIVE_MANIFEST_SCHEMA_VERSION!r}")
    if manifest.provider != MASSIVE_PROVIDER:
        fail(f"provider {manifest.provider!r} != {MASSIVE_PROVIDER!r}")
    if manifest.capture_version != capture_version:
        fail(f"capture_version {manifest.capture_version!r} != {capture_version!r}")

    listed = {entry.path: entry for entry in manifest.files}
    if len(listed) != len(manifest.files):
        fail("files[] lists the same path more than once")

    for entry in manifest.files:
        if _kind_of(entry.path) != entry.kind:
            fail(
                f"listed file {entry.path} claims kind {entry.kind!r},"
                " which does not match where it sits in the capture directory"
            )
        target = _resolve_listed(capture_dir, entry.path)
        if not target.is_file():
            fail(f"listed file {entry.path} is missing on disk")
        try:
            raw = target.read_bytes()
        except OSError as exc:
            fail(f"listed file {entry.path} unreadable ({exc.strerror})")
        if sha256_hex(raw) != entry.sha256:
            fail(f"listed file {entry.path} re-hash mismatch: tampered capture file")
        if len(raw) != entry.bytes:
            fail(f"listed file {entry.path} is {len(raw)} bytes, manifest pins {entry.bytes}")

    unlisted = sorted(_json_files_on_disk(capture_dir) - set(listed))
    if unlisted:
        fail(f"*.json on disk but not in files[]: {', '.join(unlisted)}")

    if _content_sha256_of(manifest) != manifest.content_sha256:
        fail("content_sha256 does not bind the manifest body")


__all__ = [
    "BARS_DIR",
    "CAPTURE_MANIFEST_FILENAME",
    "MASSIVE_MANIFEST_DOMAIN",
    "MASSIVE_MANIFEST_SCHEMA_VERSION",
    "MASTERS_DIR",
    "SPOT_PROXY_FILENAME",
    "CaptureFile",
    "CaptureKind",
    "MassiveCaptureManifest",
    "MassiveManifestError",
    "MasterEntry",
    "build_massive_capture_manifest",
    "load_massive_capture_manifest",
    "verify_massive_capture_manifest",
]

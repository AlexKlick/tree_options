"""Bounded LIVE capture of the spot_proxy_v2 dollar-volume sidecar (M4-B).

Owner-ruled 2026-08-29 ("Capture it"): the 0.2.2-ruled
`underlying_liquidity_term: evaluated` needs the 20-session median
underlying dollar volume (close x volume), and nothing on the host can
produce it -- the era's `spot_proxy.json` is close-only v1 and
`capture_massive_structural.py` has no aggregates mode. This script is the
~29-call equity-aggregates recapture the sealed machinery already accepts at
`artifacts/spot-proxy-v2.json` (`load_spot_proxy_v2`,
`VwapPitSurface(spot_v2=...)`).

THE DECLARED INPUTS, never a clock read:

* the universe is `data/coverage/coverage_universe.json` (the 29
  underlyings -- UNDERLYING tickers, never option O: tickers);
* the window is the ERA'S OWN captured session set: the union of the
  per-name session keys of `artifacts/m4b-coverage-era/spot_proxy.json`.
  The union's first and last session bound the request range, so nothing is
  captured beyond the era (the leakage guard), and every session the era
  itself carries for a name MUST be answerable by the vendor response or
  the run FAILS naming the gap -- fail-closed, no partial file.

Rows are written for EVERY vendor session inside that [start, end] window,
not only the era's decision-grid Fridays: the consumer's median
(`VwapPitSurface._dollar_volume_as_of`, src/tree_options/data/
vwap_pit_surface.py:726-731) slices the 20 CONSECUTIVE EXCHANGE-calendar
sessions ending at the visible session -- a Friday-only file would leave
the ruled term at the Decimal("0") sentinel forever, defeating the capture's
stated purpose. The era's sessions remain the must-answer FLOOR (a gap in
them is fatal); the window bounds the CEILING (a vendor row outside it is
dropped, recorded in the receipt).

Row discipline is the LOADER'S OWN (`_validated_spot_v2_row`, shared at the
mapping layer): `close` is the vendor's exact decimal token (Decimal from
`loads_exact`, or an int literal), formatted back exponent-free -- a float
is refused, never coerced; `volume` is a strict int >= 0 (bools, floats,
Decimals and strings all refuse; zero volume is a real observation). The
session is the ET calendar date of the row's `t` (the repo's
`session_of_epoch_ms` DST-correct discipline). A session the era's own
proxy never carried for a name may legitimately be absent from that name's
response: recorded as preexisting-absent in custody, never a gap.

`adjusted=false` is deliberate and load-bearing, exactly as in the era's
own v1 spot capture (`capture_massive_structural.capture_spot_proxy`): the
adjusted series restates split history to the PRESENT, while this file
feeds a point-in-time dollar volume. The unadjusted close x the as-traded
volume is the notional that actually traded in the era's own share terms.

THE TRANSPORT IS THE EXISTING CLIENT (`tree_options.data.massive_client`):
key custody, the 5 requests/minute governor, the bounded 429/5xx backoff,
the entitlement gate, and the key-redacted response cache are reused, never
duplicated -- one `get_json` call per name (a full daily series fits one
response), which is how `massive_options.fetch_daily_bars` drives the same
endpoint. The API key is never logged, never written into an artifact, and
never part of a receipt (endpoints go through `display_url`, key-free by
construction).

Custody lands beside the output: the output sha256, per-name request
receipts (endpoint, HTTP status, served-from, row counts, first/last
session), the universe + window set hashes, the client/provider schema
tokens, the script's own git sha at run time, and `network: true` -- this
artifact is produced by network capture, unlike every repo-generated
artifact.

Exit codes (contract):
  0  captured (both files written)
  2  not entitled on this API tier
  3  the API key was rejected
  4  capture refusal: a named vendor gap, a malformed/refused row, an
     unusable declared input, or an unanswerable name -- NOTHING written
  5  output-root refusal (--out/--custody outside artifacts/, inside a
     protected era subtree, or the same file twice)
  1  unexpected error

Usage (the orchestrator runs this ONCE, after merge):

    uv run --frozen python scripts/capture_spot_proxy_v2.py \
        --out artifacts/spot-proxy-v2.json \
        --custody artifacts/spot-proxy-v2-custody.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT / "src"))

from tree_options.data.massive_client import (  # noqa: E402
    MASSIVE_PROVIDER,
    HttpResponse,
    MassiveAuthRejectedError,
    MassiveClient,
    MassiveError,
    MassiveNotEntitledError,
    Transport,
    client_from_environment,
    loads_exact,
    urllib_transport,
)
from tree_options.data.massive_options import (  # noqa: E402
    AGGS_PATH_TEMPLATE,
    MASSIVE_SCHEMA_VERSION,
    session_of_epoch_ms,
)
from tree_options.data.massive_overlay import MassiveOverlayError  # noqa: E402
from tree_options.data.vwap_pit_surface import _validated_spot_v2_row  # noqa: E402

CAPTURE_VERSION = "m4b-spot-proxy-v2/1"
UNIVERSE_SCHEMA_VERSION = "m4-coverage-universe/2"

DEFAULT_OUT = REPO_ROOT / "artifacts" / "spot-proxy-v2.json"
DEFAULT_CUSTODY = REPO_ROOT / "artifacts" / "spot-proxy-v2-custody.json"
DEFAULT_UNIVERSE = REPO_ROOT / "data" / "coverage" / "coverage_universe.json"
DEFAULT_ERA_PROXY = REPO_ROOT / "artifacts" / "m4b-coverage-era" / "spot_proxy.json"
ARTIFACTS_ROOT = REPO_ROOT / "artifacts"

# The era subtrees this lane must never write into (they are capture
# authority for the closed bars era; the runstate stores are equally
# untouchable and live outside artifacts/ altogether).
PROTECTED_ARTIFACT_SUBDIRS = ("bars", "m4b-coverage-era", "bars-authority")

SCRIPT_PATH = "scripts/capture_spot_proxy_v2.py"


# ---- errors (fail closed) ----------------------------------------------------


class SpotProxyV2Error(RuntimeError):
    """Base class for spot_proxy_v2 capture failures."""


class OutputRefusedError(SpotProxyV2Error):
    """An output path is outside artifacts/, inside a protected era subtree,
    or aliased. Nothing is written."""


class CaptureRefusedError(SpotProxyV2Error):
    """The declared inputs or the vendor response cannot answer the window
    fail-closed: a named gap, a refused row, an unusable input. No partial
    file is ever written."""


# ---- declared inputs ---------------------------------------------------------


def _read_declared_json(path: Path) -> Any:
    """Read one declared input exactly, refusing (exit-4 shape) on any I/O
    or decode failure: an unreadable declaration is a capture refusal, not
    an unexpected crash."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CaptureRefusedError(f"{path}: declared input unreadable ({exc.strerror})") from None
    try:
        return loads_exact(raw)
    except ValueError as exc:
        raise CaptureRefusedError(f"{path}: declared input is not JSON: {exc}") from None


def load_universe(path: Path) -> tuple[list[str], str]:
    """The 29 underlyings, in file order, plus the schema token.

    The token is pinned: a universe of a different shape is a different
    declaration, and reading it as this one would misdescribe coverage."""
    payload = _read_declared_json(path)
    if not isinstance(payload, Mapping):
        raise CaptureRefusedError(f"{path}: universe JSON is not an object")
    schema = payload.get("schema_version")
    if schema != UNIVERSE_SCHEMA_VERSION:
        raise CaptureRefusedError(
            f"{path}: schema_version {schema!r} != the pinned {UNIVERSE_SCHEMA_VERSION!r}"
        )
    names = payload.get("underlyings")
    if not isinstance(names, list) or not names:
        raise CaptureRefusedError(f"{path}: `underlyings` is not a non-empty list")
    seen: set[str] = set()
    for name in names:
        if not isinstance(name, str) or not name:
            raise CaptureRefusedError(f"{path}: underlying {name!r} is not a symbol")
        if name in seen:
            raise CaptureRefusedError(f"{path}: duplicate underlying {name!r}")
        seen.add(name)
    return list(names), str(schema)


def load_era_sessions(path: Path) -> dict[str, frozenset[date]]:
    """The era's own per-name session keys (v1 spot proxy form: name ->
    ISO-date -> close token). The VALUES are not consumed here -- only the
    session keys declare what the era could answer; they are validated as
    v1 close tokens so a misshapen file refuses rather than half-defines a
    window."""
    payload = _read_declared_json(path)
    if not isinstance(payload, Mapping):
        raise CaptureRefusedError(f"{path}: era proxy JSON is not an object")
    era: dict[str, frozenset[date]] = {}
    for name, sessions in payload.items():
        if not isinstance(name, str) or not name:
            raise CaptureRefusedError(f"{path}: key {name!r} is not a symbol")
        if not isinstance(sessions, Mapping) or not sessions:
            raise CaptureRefusedError(f"{path}[{name!r}]: session map is not a non-empty object")
        parsed: set[date] = set()
        for raw_session, close in sessions.items():
            try:
                session = date.fromisoformat(str(raw_session).strip())
            except ValueError:
                raise CaptureRefusedError(
                    f"{path}[{name!r}]: key {raw_session!r} is not an ISO date"
                ) from None
            if not isinstance(close, str) or not close.strip():
                raise CaptureRefusedError(
                    f"{path}[{name!r}][{raw_session!r}]: v1 close must be a string token"
                )
            parsed.add(session)
        era[name] = frozenset(parsed)
    return era


def declared_window(era: Mapping[str, frozenset[date]]) -> tuple[date, date, frozenset[date]]:
    """(start, end, union) of the era's actual captured session set -- the
    union across names, never a clock read."""
    union = frozenset().union(*era.values())
    sessions = sorted(union)
    return sessions[0], sessions[-1], union


# ---- row mapping (the loader's own discipline) -------------------------------


def _plain(value: Decimal) -> str:
    """Exponent-free decimal text, the v1 spot capture's own convention."""
    return format(value.normalize(), "f")


def map_vendor_row(where: str, row: Mapping[str, Any]) -> tuple[date, str, int, bool]:
    """One /v2/aggs results[] row -> (session, close token, volume, truncated).

    `t` must be a strict int (the vendor's millisecond epoch); the session
    is its ET calendar date. `c` must arrive EXACT -- a Decimal from the
    client's `loads_exact`, or an int literal -- and is formatted back
    exponent-free; a float (exactness already lost upstream), a string, or
    anything else refuses. `v` may arrive as a strict int or as the vendor's
    float-shaped token (a Decimal after `loads_exact`): an INTEGRAL Decimal
    converts exactly (no float ever exists); a NON-NEGATIVE FRACTIONAL
    Decimal truncates toward zero — the file's contract is a strict int and
    truncation never inflates a liquidity measure (the $50M median's
    conservative direction) — with `truncated=True` so the capture COUNTS
    it in custody, never silent. Everything else — bools, floats, strings,
    negatives — refuses, and the loader's own `_validated_spot_v2_row`
    re-validates both fields, so a row this function returns can always
    live in the file the loader parses."""
    raw_t = row.get("t")
    if type(raw_t) is not int:
        raise CaptureRefusedError(
            f"{where}: t must be a strict int millisecond epoch, got {type(raw_t).__name__}"
        )
    try:
        session = session_of_epoch_ms(raw_t)
    except (OverflowError, OSError, ValueError) as exc:
        raise CaptureRefusedError(f"{where}: t {raw_t} is not a usable epoch ({exc})") from None
    row_where = f"{where}[{session.isoformat()}]"
    raw_c = row.get("c")
    if isinstance(raw_c, Decimal):
        exact_close = raw_c
    elif type(raw_c) is int:
        exact_close = Decimal(raw_c)
    else:
        raise CaptureRefusedError(
            f"{row_where}: close must arrive as an exact JSON number (Decimal or int"
            f" after loads_exact), got {type(raw_c).__name__} — never float, never string"
        )
    # (2026-08-31, the live AAPL refusals) the vendor ships volumes as
    # float-shaped JSON tokens (50190574.0), which `loads_exact` hands over
    # as Decimal — an INTEGRAL Decimal is the same number as its int and
    # converts exactly; the consolidated feed ALSO emits sub-share volumes
    # (37308155.220558), which truncate toward zero, never inflating a
    # liquidity measure, and are counted as truncated for custody.
    raw_v = row.get("v")
    truncated = False
    if isinstance(raw_v, Decimal):
        truncated = raw_v != raw_v.to_integral_value()
        if not truncated:
            raw_v = int(raw_v)
        else:
            if raw_v < 0:
                raise CaptureRefusedError(
                    f"{row_where}: a negative fractional volume is not a share count"
                    f" ({raw_v}) — refusing, never truncating a negative toward zero"
                )
            raw_v = int(raw_v)
    try:
        close, volume = _validated_spot_v2_row(row_where, session, exact_close, raw_v)
    except MassiveOverlayError as exc:
        raise CaptureRefusedError(f"{row_where}: {exc}") from None
    return session, _plain(close), volume, truncated


# ---- the capture -------------------------------------------------------------


@dataclass(frozen=True)
class CaptureOutcome:
    """Everything the run produced, in memory: no file lands until every
    name has answered (fail-closed, no partial file)."""

    payload_text: str
    custody: dict[str, Any]


def _set_sha256(tokens: Iterable[str]) -> str:
    return hashlib.sha256(json.dumps(sorted(tokens), separators=(",", ":")).encode()).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_stamp() -> dict[str, Any]:
    """The script's own git sha at run time (fail-closed: repo machinery
    refuses to stamp authority it cannot attribute)."""
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True
    )
    if head.returncode != 0:
        raise CaptureRefusedError(
            f"cannot stamp the script's git sha: git rev-parse HEAD failed ({head.stderr.strip()})"
        )
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", SCRIPT_PATH],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return {
        "path": SCRIPT_PATH,
        "git_sha": head.stdout.strip(),
        "dirty": bool(dirty.returncode == 0 and dirty.stdout.strip()),
    }


def run_capture(
    client: MassiveClient,
    *,
    universe_path: Path,
    era_proxy_path: Path,
    statuses: Mapping[str, int] | None = None,
) -> CaptureOutcome:
    """One equity-aggregates request per underlying; the window, the gap
    check and the custody receipt all derive from the declared inputs.

    Raises `CaptureRefusedError` on any unanswerable session (nothing is
    written); `MassiveNotEntitledError` / `MassiveAuthRejectedError`
    propagate for the CLI's own exit codes."""
    names, universe_schema = load_universe(universe_path)
    era = load_era_sessions(era_proxy_path)
    if set(era) != set(names):
        unexplained = sorted(set(era) ^ set(names))
        raise CaptureRefusedError(
            f"the era proxy and the universe disagree on {unexplained} — every declared"
            " name needs an era session set, and the era carries no undeclared names"
        )
    start, end, union = declared_window(era)

    payload: dict[str, dict[str, dict[str, Any]]] = {}
    receipts: dict[str, dict[str, Any]] = {}
    preexisting_absent: dict[str, list[str]] = {}
    floor_expected: dict[str, frozenset[date]] = {}
    written_sessions: dict[str, set[date]] = {}
    first_served: dict[str, date] = {}
    total_rows = 0

    for name in names:
        path = AGGS_PATH_TEMPLATE.format(ticker=name, start=start.isoformat(), end=end.isoformat())
        params: dict[str, str] = {"adjusted": "false"}
        endpoint = client.display_url(path, params)
        wire_before = client.stats.requests
        hits_before = client.stats.cache_hits
        try:
            body = client.get_json(path, params)
        except (MassiveNotEntitledError, MassiveAuthRejectedError):
            raise
        except MassiveError as exc:
            raise CaptureRefusedError(
                f"{name}: the aggregates request failed, so the window cannot be"
                f" answered ({type(exc).__name__}: {exc})"
            ) from None
        wire_requests = client.stats.requests - wire_before
        cache_hits = client.stats.cache_hits - hits_before

        results = body.get("results")
        if not isinstance(results, list):
            raise CaptureRefusedError(f"{name}[{endpoint}]: `results` is not a list")
        results_count = body.get("resultsCount")
        if type(results_count) is int and results_count != len(results):
            raise CaptureRefusedError(
                f"{name}[{endpoint}]: resultsCount {results_count} != {len(results)}"
                " results — a truncated response is refused, never salvaged"
            )
        request_id = body.get("request_id")

        rows: dict[date, tuple[str, int]] = {}
        outside_window = 0
        truncated_volume_rows = 0
        for index, raw_row in enumerate(results):
            if not isinstance(raw_row, Mapping):
                raise CaptureRefusedError(f"{name}[row {index}]: a result row is not an object")
            session, close_token, volume, truncated = map_vendor_row(
                f"{name}[row {index}]", raw_row
            )
            if truncated:
                truncated_volume_rows += 1
            if not start <= session <= end:
                outside_window += 1
                continue
            if session in rows:
                raise CaptureRefusedError(
                    f"{name}[{session.isoformat()}]: the vendor returned the session twice"
                    " — refusing to overwrite silently"
                )
            rows[session] = (close_token, volume)

        expected = era[name]
        # (2026-08-31, the live AAPL run) the free tier serves exactly two
        # years back, so era sessions BEFORE the vendor's served boundary
        # are entitlement-absent, not gaps — the boundary is GLOBAL (the
        # earliest session ANY name was served) and the floor check runs
        # after every name is fetched.
        floor_expected[name] = expected
        written_sessions[name] = set(rows)

        sessions_sorted = sorted(rows)
        payload[name] = {
            session.isoformat(): {"close": rows[session][0], "volume": rows[session][1]}
            for session in sessions_sorted
        }
        total_rows += len(sessions_sorted)
        http_status: int | None = None
        if statuses is not None and path in statuses:
            http_status = statuses[path]
        elif wire_requests > 0:
            http_status = 200  # get_json only returns bodies the client accepted
        receipts[name] = {
            "endpoint": endpoint,
            "http_status": http_status,
            "served_from": "cache" if (cache_hits > 0 and wire_requests == 0) else "wire",
            "vendor_status": body.get("status"),
            "request_id": request_id if isinstance(request_id, str) else None,
            "results_count": results_count if type(results_count) is int else None,
            "rows_in_response": len(results),
            "truncated_volume_rows": truncated_volume_rows,
            "rows_outside_window": outside_window,
            "rows_written": len(sessions_sorted),
            "expected_era_sessions": len(expected),
            "first_session": sessions_sorted[0].isoformat(),
            "last_session": sessions_sorted[-1].isoformat(),
            "gaps": [],
        }
        first_served[name] = sessions_sorted[0]

    # ---- the declared-window floor, judged against the served boundary ----
    boundary = min(first_served.values())
    entitlement_absent: dict[str, list[str]] = {}
    for name in names:
        expected = floor_expected[name]
        entitlement_absent[name] = [
            session.isoformat() for session in sorted(expected) if session < boundary
        ]
        missing = sorted(
            session for session in expected - written_sessions[name] if session >= boundary
        )
        if missing:
            named = ", ".join(session.isoformat() for session in missing)
            raise CaptureRefusedError(
                f"{name}: {len(missing)} vendor gap(s) — the era itself carries this name"
                f" on {named}, at or after the served boundary {boundary.isoformat()};"
                " refusing to write a partial file"
            )
        preexisting_absent[name] = [session.isoformat() for session in sorted(union - expected)]

    payload_text = json.dumps(payload, indent=2, sort_keys=True, separators=(",", ": ")) + "\n"
    custody: dict[str, Any] = {
        "capture_version": CAPTURE_VERSION,
        "network": True,
        "captured_at": datetime.now(UTC).isoformat(),
        "provider": MASSIVE_PROVIDER,
        "client_schema": MASSIVE_SCHEMA_VERSION,
        "adjusted": False,
        "script": _git_stamp(),
        "universe": {
            "path": str(universe_path),
            "sha256": _file_sha256(universe_path),
            "schema_version": universe_schema,
            "names": len(names),
            "set_sha256": _set_sha256(names),
        },
        "window": {
            "era_proxy": str(era_proxy_path),
            "era_proxy_sha256": _file_sha256(era_proxy_path),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "sessions": len(union),
            "set_sha256": _set_sha256(session.isoformat() for session in union),
        },
        "output": {
            "sha256": hashlib.sha256(payload_text.encode()).hexdigest(),
            "bytes": len(payload_text.encode()),
            "rows": total_rows,
            "names": len(payload),
        },
        "preexisting_absent": preexisting_absent,
        "vendor_entitlement_boundary": boundary.isoformat(),
        "vendor_entitlement_absent": entitlement_absent,
        "names": receipts,
    }
    return CaptureOutcome(payload_text=payload_text, custody=custody)


# ---- output confinement ------------------------------------------------------


def confine_output(path: Path, *, label: str, other: Path | None = None) -> Path:
    """Resolve one output path against the REPO ROOT (never the cwd) and
    refuse it unless it lands inside artifacts/ — and outside every
    protected era subtree. Symlinks are refused outright: the write must
    land under the name the operator sees, never through a planted link."""
    resolved = (REPO_ROOT / path).resolve() if not path.is_absolute() else path.resolve()
    if not resolved.is_relative_to(ARTIFACTS_ROOT):
        raise OutputRefusedError(
            f"{label} {path} resolves to {resolved}, outside artifacts/"
            f" ({ARTIFACTS_ROOT}) — refusing to write it"
        )
    for protected in PROTECTED_ARTIFACT_SUBDIRS:
        ceiling = ARTIFACTS_ROOT / protected
        if resolved.is_relative_to(ceiling):
            raise OutputRefusedError(
                f"{label} {path} resolves under the protected era subtree"
                f" artifacts/{protected}/ — this lane never writes there"
            )
    if path.is_symlink():
        raise OutputRefusedError(f"{label} {path} is itself a symlink — refusing to write it")
    if other is not None and resolved == other.resolve():
        raise OutputRefusedError(
            f"{label} and --custody resolve to the same file {resolved} — the output and"
            " its custody receipt must never alias"
        )
    if resolved.exists() and not resolved.is_file():
        raise OutputRefusedError(f"{label} {resolved} exists and is not a file")
    return resolved


def _atomic_write(path: Path, text: str) -> None:
    """Write via a staging file and `os.replace`, so a crash never leaves a
    half-written artifact under its own name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.parent / f".{path.name}.{os.getpid()}.tmp"
    staging.write_text(text, encoding="utf-8")
    os.replace(staging, path)


# ---- the client seam ---------------------------------------------------------


def _recording_transport(statuses: dict[str, int]) -> Transport:
    """The stdlib transport, wrapped to record each wire request's HTTP
    status keyed by its key-free PATH — the receipt's `http_status`. A cache
    hit never reaches the transport, which is how the receipt says
    served_from "cache" with http_status null."""

    def transport(url: str, *, timeout: float) -> HttpResponse:
        response = urllib_transport(url, timeout=timeout)
        statuses[urlsplit(url).path] = response.status
        return response

    return transport


ClientBundle = tuple[MassiveClient, dict[str, int]]


def build_client(**kwargs: Any) -> ClientBundle:
    """The CLI's client: the environment-keyed client over the recording
    transport. The statuses dict feeds the per-name receipts."""
    statuses: dict[str, int] = {}
    client = client_from_environment(transport=_recording_transport(statuses), **kwargs)
    return client, statuses


# ---- CLI ---------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="the spot_proxy_v2 file")
    parser.add_argument(
        "--custody", type=Path, default=DEFAULT_CUSTODY, help="the custody manifest"
    )
    parser.add_argument(
        "--universe",
        type=Path,
        default=DEFAULT_UNIVERSE,
        help="coverage universe JSON (default: data/coverage/coverage_universe.json)",
    )
    parser.add_argument(
        "--era-proxy",
        type=Path,
        default=DEFAULT_ERA_PROXY,
        help="the era's v1 spot proxy, whose session keys declare the window",
    )
    parser.add_argument(
        "--cache-dir", type=Path, help="response cache location (default: artifacts/massive-cache)"
    )
    parser.add_argument("--timeout", type=float, help="per-request HTTP timeout in seconds")
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    client_factory: Any = build_client,
) -> int:
    args = _parse_args(argv)
    try:
        # Guard BEFORE any client exists: a refused path must never spend a
        # request, let alone the key.
        out_path = confine_output(args.out, label="--out")
        custody_path = confine_output(args.custody, label="--custody", other=args.out)
        client_kwargs: dict[str, Any] = {}
        if args.cache_dir is not None:
            client_kwargs["cache_dir"] = args.cache_dir
        if args.timeout is not None:
            client_kwargs["timeout"] = args.timeout
        client, statuses = client_factory(**client_kwargs)
        outcome = run_capture(
            client,
            universe_path=args.universe,
            era_proxy_path=args.era_proxy,
            statuses=statuses,
        )
        _atomic_write(out_path, outcome.payload_text)
        custody = dict(outcome.custody)
        custody["output"] = {**custody["output"], "path": str(out_path)}
        custody["custody_path"] = str(custody_path)
        _atomic_write(
            custody_path,
            json.dumps(custody, indent=2, sort_keys=True, separators=(",", ": ")) + "\n",
        )
    except OutputRefusedError as exc:
        print(f"SPOT PROXY V2 REFUSED (OutputRefusedError): {exc}", file=sys.stderr)
        return 5
    except MassiveNotEntitledError as exc:
        print(f"NOT ENTITLED: {exc}", file=sys.stderr)
        return 2
    except MassiveAuthRejectedError as exc:
        print(f"AUTH REJECTED: {exc}", file=sys.stderr)
        return 3
    except CaptureRefusedError as exc:
        print(f"SPOT PROXY V2 REFUSED (CaptureRefusedError): {exc}", file=sys.stderr)
        return 4
    print(json.dumps(custody, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

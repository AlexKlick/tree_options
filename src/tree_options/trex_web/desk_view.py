"""Read-only shadow-evidence projection on the existing cockpit application."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from tree_options.desk import book as desk_book
from tree_options.desk import production, scorecards
from tree_options.desk.book import BookSlice
from tree_options.desk.contracts import ContractError
from tree_options.desk.evidence import EvidenceError
from tree_options.desk.lab_scoreboard import aggregate, best_advisory
from tree_options.desk.trade_floor import project_replay
from tree_options.trex.clock import now_et, session_calendar
from tree_options.trex.desk_cli import collect_status
from tree_options.trex.desk_runtime import DeskPaths
from tree_options.trex.supervised import SupervisedPaths
from tree_options.trex_web.automation import (
    automation_action,
    automation_status,
    kill_file_action,
)

_ERRORS = (ContractError, EvidenceError, sqlite3.Error, OSError)

#: how long the cockpit serves the standings file the nightly
#: ``challenge standings`` rebuild owns; past this it recomputes (and only
#: RETURNS — the web lane never writes the store)
STANDINGS_FRESH_S = 26 * 3600

#: who owns a book, by the ``BookPosition.source`` the adapter gives it.
#: The desk book is the supervised desk's; every other book under the same
#: state root belongs to the legacy trex monitor (a different service, on
#: the same paper account).
OWNERS = {"desk": "supervised-desk"}

#: the service that owns every non-desk book on this account
LEGACY_OWNER = "trex-monitor"


def _owner(source: str) -> str:
    return OWNERS.get(source, LEGACY_OWNER)


def _money(value: Decimal | None) -> str | None:
    """Money as a 2-dp string, or None when it is not countable (never a
    number the cockpit could add up)."""
    return None if value is None else f"{value:.2f}"


def _slice_block(slice_: BookSlice) -> dict[str, Any]:
    deadline = slice_.earliest_exit_deadline
    return {
        "structures": slice_.structures,
        "legs": slice_.legs,
        "max_loss_usd": _money(slice_.max_loss_usd),
        "earliest_exit_deadline": None if deadline is None else deadline.isoformat(),
        "exit_deadlines_unknown": slice_.unknown_deadlines,
        "owners": sorted({_owner(p.source) for p in slice_.positions}),
        "books": list(slice_.books),
        "positions": [
            {
                "id": p.id,
                "book": p.source,
                "owner": _owner(p.source),
                "underlying": p.underlying,
                "status": p.status,
                "quantity": p.quantity,
                "max_loss_usd": _money(p.max_loss_usd),
                "exit_deadline": None if p.exit_deadline is None else p.exit_deadline.isoformat(),
            }
            for p in slice_.positions
        ],
    }


def account_block(
    paths: DeskPaths, *, plans_root: Path | None, state_root: Path | None, as_of: date
) -> dict[str, Any]:
    """The ACCOUNT's exposure as the files stand, split by owner: the desk
    book against every other book under the same state root.

    Read-only, exactly like the rest of this route: the same ``load_book``
    adapter the desk's own admission screen uses (it already counts the
    legacy books), never a broker call. ``countable`` false means a book
    could not be read and no total may be claimed from this block."""
    exposure = desk_book.account_exposure(
        as_of=as_of,
        plans_root=plans_root,
        state_root=state_root,
        desk_specs=paths.specs(),
        desk_book=paths.book(),
    )
    return {
        "schema": "desk-account-exposure/1",
        "as_of": as_of.isoformat(),
        "state_root": None if state_root is None else str(state_root),
        "desk_run_dir": str(paths.root),
        "countable": exposure.countable,
        "max_loss_usd": (
            None
            if not exposure.countable
            else _money(
                (exposure.outside.max_loss_usd or Decimal(0))
                + (exposure.desk.max_loss_usd or Decimal(0))
            )
        ),
        "outside_desk_book": _slice_block(exposure.outside),
        "desk_book": _slice_block(exposure.desk),
        "problems": list(exposure.problems),
    }


def attach(
    app: FastAPI,
    *,
    database: Path,
    replay_dir: Path | None = None,
    portfolio_dir: Path | None = None,
    intraday_dir: Path | None = None,
    trade_floor_dir: Path | None = None,
    longrun_dir: Path | None = None,
    challenge_dir: Path | None = None,
    plans_root: Path | None = None,
    state_root: Path | None = None,
) -> None:
    @app.get("/api/desk/health")
    def health() -> JSONResponse:
        try:
            doc = production.health(database=database, now=now_et(), cal=session_calendar())
            return JSONResponse(
                doc,
                status_code=200 if doc["evidence_status"] == "ready" else 503,
                headers={"Cache-Control": "no-store"},
            )
        except _ERRORS:
            return _unavailable()

    @app.get("/api/desk/scorecards")
    def summary() -> JSONResponse:
        try:
            return JSONResponse(
                scorecards.build_scorecards(database), headers={"Cache-Control": "no-store"}
            )
        except _ERRORS:
            return _unavailable()

    @app.get("/api/desk/historical-replays")
    def historical_replays() -> JSONResponse:
        """Read the latest bounded exploratory reports; never launch work."""
        root = replay_dir or database.parent.parent / "evaluations" / "historical-replay"
        reports: list[dict[str, Any]] = []
        try:
            for path in sorted(root.glob("replay-*.json"), reverse=True)[:12]:
                if path.stat().st_size > 20_000_000:
                    continue
                doc = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(doc, dict) or doc.get("schema") != "desk-historical-replay/1":
                    continue
                if any(
                    not isinstance(doc.get(key), dict)
                    for key in ("spec", "counts", "by_structure", "by_variant", "provenance")
                ):
                    continue
                reports.append(
                    {
                        "id": path.stem,
                        "label": doc.get("label"),
                        "spec": doc.get("spec"),
                        "counts": doc.get("counts"),
                        "by_structure": doc.get("by_structure"),
                        "by_variant": doc["by_variant"],
                        "eligibility_by_variant": doc.get("eligibility_by_variant"),
                        "provenance": doc.get("provenance"),
                        "limitations": doc.get("limitations"),
                    }
                )
        except (OSError, ValueError, json.JSONDecodeError):
            return _unavailable()
        return JSONResponse(
            {
                "schema": "desk-historical-replay-list/1",
                "reports": reports,
                "execution_enabled": False,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/desk/portfolio-scenarios")
    def portfolio_scenarios() -> JSONResponse:
        """Summaries of frozen modeled risk budgets, without trade rows or effects."""
        root = portfolio_dir or database.parent.parent / "evaluations" / "portfolio-scenario"
        reports: list[dict[str, Any]] = []
        try:
            for path in sorted(root.glob("portfolio-*.json"), reverse=True)[:12]:
                if path.is_symlink() or path.stat().st_size > 20_000_000:
                    continue
                doc = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(doc, dict) or doc.get("schema") != "desk-portfolio-scenario/1":
                    continue
                if any(
                    not isinstance(doc.get(key), dict) for key in ("spec", "variants", "provenance")
                ):
                    continue
                spec, provenance = doc["spec"], doc["provenance"]
                if any(
                    not isinstance(spec.get(key), str)
                    for key in ("intended_capital", "max_trade_loss", "max_open_loss")
                ):
                    continue
                if (
                    not isinstance(provenance.get("replay_sha256"), str)
                    or len(provenance["replay_sha256"]) != 64
                    or not isinstance(provenance.get("code_dirty"), bool)
                    or not isinstance(doc.get("limitations"), list)
                ):
                    continue
                variants = {}
                for name, row in doc["variants"].items():
                    if not isinstance(name, str) or not isinstance(row, dict):
                        continue
                    if any(
                        key not in row
                        for key in (
                            "considered",
                            "admitted",
                            "skipped",
                            "peak_open_loss_reserved",
                            "closed_pnl",
                            "ending_closed_capital",
                            "minimum_closed_capital",
                        )
                    ):
                        continue
                    variants[name] = {
                        key: row[key]
                        for key in (
                            "considered",
                            "admitted",
                            "skipped",
                            "peak_open_loss_reserved",
                            "closed_pnl",
                            "ending_closed_capital",
                            "minimum_closed_capital",
                        )
                        if key in row
                    }
                reports.append(
                    {
                        "id": path.stem,
                        "label": doc.get("label"),
                        "spec": doc["spec"],
                        "variants": variants,
                        "provenance": {
                            key: doc["provenance"].get(key)
                            for key in ("replay_sha256", "code_head", "code_dirty")
                        },
                        "limitations": doc.get("limitations"),
                    }
                )
        except (OSError, ValueError, json.JSONDecodeError):
            return _unavailable()
        return JSONResponse(
            {
                "schema": "desk-portfolio-scenario-list/1",
                "reports": reports,
                "execution_enabled": False,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/desk/intraday-graphs")
    def intraday_graphs() -> JSONResponse:
        """Project bounded graph summaries; never serve full bars or trigger replay."""
        root = intraday_dir or database.parent.parent / "evaluations" / "intraday-graph"
        reports: list[dict[str, Any]] = []
        try:
            for path in sorted(root.glob("*/*.summary.json"), reverse=True)[:24]:
                if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > 1_000_000:
                    continue
                doc = json.loads(path.read_text(encoding="utf-8"))
                if (
                    not isinstance(doc, dict)
                    or doc.get("schema") != "desk-intraday-graph-summary/1"
                    or doc.get("execution_authorized") is not False
                    or not isinstance(doc.get("windows"), list)
                    or any(
                        not isinstance(row, dict)
                        or any(
                            key not in row
                            for key in (
                                "start",
                                "end",
                                "sessions",
                                "scheduled_snapshots",
                                "potential_trades",
                                "entered",
                                "modeled_wins",
                                "modeled_losses",
                                "open_at_end",
                                "closed_capital_proxy",
                                "minimum_closed_capital_proxy",
                                "peak_open_loss_reserved",
                            )
                        )
                        for row in doc["windows"]
                    )
                    or not isinstance(doc.get("limitations"), list)
                    or not isinstance(doc.get("source_sha256"), str)
                    or len(doc["source_sha256"]) != 64
                ):
                    continue
                reports.append(
                    {
                        "id": f"{path.parent.name}/{path.stem.removesuffix('.summary')}",
                        "policy": doc.get("policy"),
                        "source_sha256": doc["source_sha256"],
                        "requested_contracts": doc.get("requested_contracts"),
                        "captured_contracts": doc.get("captured_contracts"),
                        "traded_minute_bars": doc.get("traded_minute_bars"),
                        "windows": [
                            {
                                key: row.get(key)
                                for key in (
                                    "start",
                                    "end",
                                    "sessions",
                                    "scheduled_snapshots",
                                    "potential_trades",
                                    "entered",
                                    "modeled_wins",
                                    "modeled_losses",
                                    "open_at_end",
                                    "closed_capital_proxy",
                                    "minimum_closed_capital_proxy",
                                    "peak_open_loss_reserved",
                                )
                            }
                            for row in doc["windows"]
                        ],
                        "limitations": doc["limitations"],
                    }
                )
        except (OSError, ValueError, json.JSONDecodeError):
            return _unavailable()
        return JSONResponse(
            {
                "schema": "desk-intraday-graph-list/1",
                "reports": reports,
                "execution_enabled": False,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/desk/trade-floor")
    def trade_floor() -> JSONResponse:
        """Serve compact historical spectator rounds; never launch model or broker work."""
        root = trade_floor_dir or database.parent.parent / "evaluations" / "trade-floor"
        replays: list[dict[str, Any]] = []
        try:
            for path in sorted(root.glob("*.json"), reverse=True)[:4]:
                if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > 2_000_000:
                    return _unavailable()
                replays.append(project_replay(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return _unavailable()
        return JSONResponse(
            {"schema": "desk-trade-floor-list/1", "replays": replays, "execution_enabled": False},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/desk/supervised")
    def supervised_status() -> JSONResponse:
        """One read-only dump of the supervised desk's on-disk paper state.

        The desk process owns the broker session; this never contacts it —
        it serves the files as they stand (kill files, book, inbox,
        mandate, outbox, and the ACCOUNT's exposure: the desk book beside
        every other book on the same account, since a flat desk book is
        not a flat account)."""
        try:
            now = now_et()
            desk_paths = DeskPaths.default()
            doc = collect_status(desk_paths, SupervisedPaths.default(), now=now, events=20)
            doc["account_exposure"] = account_block(
                desk_paths, plans_root=plans_root, state_root=state_root, as_of=now.date()
            )
        except (*_ERRORS, OSError, ValueError, KeyError, TypeError):
            return _unavailable()
        return JSONResponse(doc, headers={"Cache-Control": "no-store"})

    @app.get("/api/desk/automation")
    def automation() -> JSONResponse:
        """Timer settings + kill-file states for the desk's own units."""
        try:
            doc = automation_status(DeskPaths.default().root)
        except (OSError, RuntimeError):
            return _unavailable()
        return JSONResponse(doc, headers={"Cache-Control": "no-store"})

    @app.post("/api/desk/automation/{key}/{action}")
    def automation_control(key: str, action: str) -> JSONResponse:
        """Enable/disable a whitelisted desk timer, or run its service now.

        The unit whitelist is the whole surface: an unknown key or action
        is a 404/422, never a shell. Every action is audited to
        automation.jsonl (actor: cockpit)."""
        if action not in ("enable", "disable", "run"):
            return JSONResponse({"error": "unknown_action"}, status_code=422)
        try:
            doc = automation_action(DeskPaths.default().root, key, action)
        except KeyError:
            return JSONResponse({"error": "unknown_unit"}, status_code=404)
        except (OSError, RuntimeError):
            return _unavailable()
        return JSONResponse(doc, headers={"Cache-Control": "no-store"})

    @app.post("/api/desk/supervised/{action}")
    def supervised_control(action: str) -> JSONResponse:
        """HALT / FLATTEN / resume for the supervised desk (the desk_cli
        verbs, surfaced; the running desk observes the files on its next
        tick - nothing here contacts the broker)."""
        if action not in ("halt", "flatten", "resume"):
            return JSONResponse({"error": "unknown_action"}, status_code=422)
        try:
            doc = kill_file_action(DeskPaths.default().root, action)
        except OSError:
            return _unavailable()
        return JSONResponse(doc, headers={"Cache-Control": "no-store"})

    @app.get("/api/desk/lab")
    def lab_scoreboard() -> JSONResponse:
        """Fold the lab's run summaries into a per-policy scoreboard.

        The advisory is an annotation, never a promotion: no rule is
        registered, so ``promoted`` is False by construction."""
        from tree_options.desk.lab import default_root

        try:
            scoreboard = aggregate(default_root())
            advisory = best_advisory(scoreboard)
        except (*_ERRORS, OSError, ValueError, KeyError, TypeError):
            return _unavailable()
        return JSONResponse(
            {**scoreboard, "advisory": advisory, "execution_enabled": False},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/desk/longrun")
    def longrun_view() -> JSONResponse:
        """The latest long run: live progress, plus the digest's standings once
        finished. Read-only; ``TREX_DESK_LONGRUN_DIR`` (a run dir or a root of
        run dirs) overrides the store default. Never promotes, never launches."""
        from tree_options.desk import longrun

        override = os.environ.get(longrun.DIR_ENV, "").strip()
        root = (
            Path(override).expanduser()
            if override
            else longrun_dir or database.parent.parent / "evaluations" / "longrun"
        )
        try:
            doc = longrun.cockpit_view(root)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return _unavailable()
        return JSONResponse(
            {**doc, "execution_enabled": False}, headers={"Cache-Control": "no-store"}
        )

    @app.get("/api/desk/standings")
    def standings_view() -> JSONResponse:
        """The challenge standings every clause of the sealed promotion rule
        reads: ``evaluations/challenge/standings.json`` when present and
        fresh (< 26 h), else RECOMPUTED from the post-seal digests and
        returned without being written (the web lane never mutates the
        store). Strictly read-only: no rule_check runs here — the clauses
        are printable client-side, and the web layer computes no stats."""
        from tree_options.desk.challenge import accumulate_standings

        root = challenge_dir or database.parent.parent / "evaluations" / "challenge"
        doc: dict[str, Any] | None = None
        path = root / "standings.json"
        try:
            if path.is_file() and (time.time() - path.stat().st_mtime) < STANDINGS_FRESH_S:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                doc = loaded if isinstance(loaded, dict) else None
        except (OSError, ValueError):
            doc = None  # a torn file falls through to the recompute
        if doc is None:
            try:
                doc = accumulate_standings(root.parent.parent)
            except (OSError, ValueError, KeyError, TypeError):
                return _unavailable()
        return JSONResponse(doc, headers={"Cache-Control": "no-store"})

    @app.get("/desk/evidence")
    def page() -> HTMLResponse:
        html = Path(__file__).with_name("desk_evidence.html").read_text(encoding="utf-8")
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        sha = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        return HTMLResponse(
            html,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": f"default-src 'none'; script-src 'sha256-{sha}'; "
                "style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'none'; object-src 'none'",
            },
        )


def _unavailable() -> JSONResponse:
    doc: dict[str, Any] = {
        "schema": "desk-error/1",
        "error": "evidence_unavailable",
        "execution_enabled": False,
        "evidence_status": "unavailable",
    }
    return JSONResponse(doc, status_code=503, headers={"Cache-Control": "no-store"})

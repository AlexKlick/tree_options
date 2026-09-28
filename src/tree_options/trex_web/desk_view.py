"""Read-only shadow-evidence projection on the existing cockpit application."""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from tree_options.desk import production, scorecards
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


def attach(app: FastAPI, *, database: Path, replay_dir: Path | None = None,
           portfolio_dir: Path | None = None, intraday_dir: Path | None = None,
           trade_floor_dir: Path | None = None) -> None:
    @app.get('/api/desk/health')
    def health() -> JSONResponse:
        try:
            doc = production.health(database=database, now=now_et(), cal=session_calendar())
            return JSONResponse(doc, status_code=200 if doc['evidence_status'] == 'ready' else 503,
                                headers={'Cache-Control': 'no-store'})
        except _ERRORS:
            return _unavailable()

    @app.get('/api/desk/scorecards')
    def summary() -> JSONResponse:
        try:
            return JSONResponse(scorecards.build_scorecards(database), headers={'Cache-Control': 'no-store'})
        except _ERRORS:
            return _unavailable()

    @app.get('/api/desk/historical-replays')
    def historical_replays() -> JSONResponse:
        """Read the latest bounded exploratory reports; never launch work."""
        root = replay_dir or database.parent.parent / 'evaluations' / 'historical-replay'
        reports: list[dict[str, Any]] = []
        try:
            for path in sorted(root.glob('replay-*.json'), reverse=True)[:12]:
                if path.stat().st_size > 20_000_000:
                    continue
                doc = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(doc, dict) or doc.get('schema') != 'desk-historical-replay/1':
                    continue
                if any(not isinstance(doc.get(key), dict) for key in
                       ('spec', 'counts', 'by_structure', 'by_variant', 'provenance')):
                    continue
                reports.append({
                    'id': path.stem, 'label': doc.get('label'), 'spec': doc.get('spec'),
                    'counts': doc.get('counts'), 'by_structure': doc.get('by_structure'),
                    'by_variant': doc['by_variant'],
                    'eligibility_by_variant': doc.get('eligibility_by_variant'),
                    'provenance': doc.get('provenance'), 'limitations': doc.get('limitations'),
                })
        except (OSError, ValueError, json.JSONDecodeError):
            return _unavailable()
        return JSONResponse({'schema': 'desk-historical-replay-list/1', 'reports': reports,
                             'execution_enabled': False}, headers={'Cache-Control': 'no-store'})

    @app.get('/api/desk/portfolio-scenarios')
    def portfolio_scenarios() -> JSONResponse:
        """Summaries of frozen modeled risk budgets, without trade rows or effects."""
        root = portfolio_dir or database.parent.parent / 'evaluations' / 'portfolio-scenario'
        reports: list[dict[str, Any]] = []
        try:
            for path in sorted(root.glob('portfolio-*.json'), reverse=True)[:12]:
                if path.is_symlink() or path.stat().st_size > 20_000_000:
                    continue
                doc = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(doc, dict) or doc.get('schema') != 'desk-portfolio-scenario/1':
                    continue
                if any(not isinstance(doc.get(key), dict) for key in ('spec', 'variants', 'provenance')):
                    continue
                spec, provenance = doc['spec'], doc['provenance']
                if any(not isinstance(spec.get(key), str) for key in (
                    'intended_capital', 'max_trade_loss', 'max_open_loss')):
                    continue
                if (not isinstance(provenance.get('replay_sha256'), str)
                    or len(provenance['replay_sha256']) != 64
                    or not isinstance(provenance.get('code_dirty'), bool)
                    or not isinstance(doc.get('limitations'), list)):
                    continue
                variants = {}
                for name, row in doc['variants'].items():
                    if not isinstance(name, str) or not isinstance(row, dict):
                        continue
                    if any(key not in row for key in (
                        'considered', 'admitted', 'skipped', 'peak_open_loss_reserved',
                        'closed_pnl', 'ending_closed_capital', 'minimum_closed_capital')):
                        continue
                    variants[name] = {key: row[key] for key in (
                        'considered', 'admitted', 'skipped', 'peak_open_loss_reserved',
                        'closed_pnl', 'ending_closed_capital', 'minimum_closed_capital') if key in row}
                reports.append({'id': path.stem, 'label': doc.get('label'),
                                'spec': doc['spec'], 'variants': variants,
                                'provenance': {key: doc['provenance'].get(key) for key in (
                                    'replay_sha256', 'code_head', 'code_dirty')},
                                'limitations': doc.get('limitations')})
        except (OSError, ValueError, json.JSONDecodeError):
            return _unavailable()
        return JSONResponse({'schema': 'desk-portfolio-scenario-list/1', 'reports': reports,
                             'execution_enabled': False}, headers={'Cache-Control': 'no-store'})

    @app.get('/api/desk/intraday-graphs')
    def intraday_graphs() -> JSONResponse:
        """Project bounded graph summaries; never serve full bars or trigger replay."""
        root = intraday_dir or database.parent.parent / 'evaluations' / 'intraday-graph'
        reports: list[dict[str, Any]] = []
        try:
            for path in sorted(root.glob('*/*.summary.json'), reverse=True)[:24]:
                if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > 1_000_000:
                    continue
                doc = json.loads(path.read_text(encoding='utf-8'))
                if (not isinstance(doc, dict) or doc.get('schema') != 'desk-intraday-graph-summary/1'
                        or doc.get('execution_authorized') is not False
                        or not isinstance(doc.get('windows'), list)
                        or any(not isinstance(row, dict) or any(key not in row for key in (
                            'start', 'end', 'sessions', 'scheduled_snapshots', 'potential_trades',
                            'entered', 'modeled_wins', 'modeled_losses', 'open_at_end',
                            'closed_capital_proxy', 'minimum_closed_capital_proxy',
                            'peak_open_loss_reserved'))
                            for row in doc['windows'])
                        or not isinstance(doc.get('limitations'), list)
                        or not isinstance(doc.get('source_sha256'), str)
                        or len(doc['source_sha256']) != 64):
                    continue
                reports.append({'id': f"{path.parent.name}/{path.stem.removesuffix('.summary')}",
                                'policy': doc.get('policy'), 'source_sha256': doc['source_sha256'],
                                'requested_contracts': doc.get('requested_contracts'),
                                'captured_contracts': doc.get('captured_contracts'),
                                'traded_minute_bars': doc.get('traded_minute_bars'),
                                'windows': [{key: row.get(key) for key in (
                                    'start', 'end', 'sessions', 'scheduled_snapshots',
                                    'potential_trades', 'entered', 'modeled_wins', 'modeled_losses',
                                    'open_at_end', 'closed_capital_proxy',
                                    'minimum_closed_capital_proxy', 'peak_open_loss_reserved')}
                                    for row in doc['windows']],
                                'limitations': doc['limitations']})
        except (OSError, ValueError, json.JSONDecodeError):
            return _unavailable()
        return JSONResponse({'schema': 'desk-intraday-graph-list/1', 'reports': reports,
                             'execution_enabled': False}, headers={'Cache-Control': 'no-store'})

    @app.get('/api/desk/trade-floor')
    def trade_floor() -> JSONResponse:
        """Serve compact historical spectator rounds; never launch model or broker work."""
        root = trade_floor_dir or database.parent.parent / 'evaluations' / 'trade-floor'
        replays: list[dict[str, Any]] = []
        try:
            for path in sorted(root.glob('*.json'), reverse=True)[:4]:
                if (path.is_symlink() or path.parent.is_symlink()
                        or path.stat().st_size > 2_000_000):
                    return _unavailable()
                replays.append(project_replay(json.loads(path.read_text(encoding='utf-8'))))
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return _unavailable()
        return JSONResponse({'schema': 'desk-trade-floor-list/1', 'replays': replays,
                             'execution_enabled': False}, headers={'Cache-Control': 'no-store'})

    @app.get('/api/desk/supervised')
    def supervised_status() -> JSONResponse:
        """One read-only dump of the supervised desk's on-disk paper state.

        The desk process owns the broker session; this never contacts it —
        it serves the files as they stand (kill files, book, inbox,
        mandate, outbox)."""
        try:
            doc = collect_status(DeskPaths.default(), SupervisedPaths.default(),
                                 now=now_et(), events=20)
        except (*_ERRORS, OSError, ValueError, KeyError, TypeError):
            return _unavailable()
        return JSONResponse(doc, headers={'Cache-Control': 'no-store'})

    @app.get('/api/desk/automation')
    def automation() -> JSONResponse:
        """Timer settings + kill-file states for the desk's own units."""
        try:
            doc = automation_status(DeskPaths.default().root)
        except OSError:
            return _unavailable()
        return JSONResponse(doc, headers={'Cache-Control': 'no-store'})

    @app.post('/api/desk/automation/{key}/{action}')
    def automation_control(key: str, action: str) -> JSONResponse:
        """Enable/disable a whitelisted desk timer, or run its service now.

        The unit whitelist is the whole surface: an unknown key or action
        is a 404/422, never a shell. Every action is audited to
        automation.jsonl (actor: cockpit)."""
        if action not in ('enable', 'disable', 'run'):
            return JSONResponse({'error': 'unknown_action'}, status_code=422)
        try:
            doc = automation_action(DeskPaths.default().root, key, action)
        except KeyError:
            return JSONResponse({'error': 'unknown_unit'}, status_code=404)
        except OSError:
            return _unavailable()
        return JSONResponse(doc, headers={'Cache-Control': 'no-store'})

    @app.post('/api/desk/supervised/{action}')
    def supervised_control(action: str) -> JSONResponse:
        """HALT / FLATTEN / resume for the supervised desk (the desk_cli
        verbs, surfaced; the running desk observes the files on its next
        tick - nothing here contacts the broker)."""
        if action not in ('halt', 'flatten', 'resume'):
            return JSONResponse({'error': 'unknown_action'}, status_code=422)
        try:
            doc = kill_file_action(DeskPaths.default().root, action)
        except OSError:
            return _unavailable()
        return JSONResponse(doc, headers={'Cache-Control': 'no-store'})

    @app.get('/api/desk/lab')
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
        return JSONResponse({**scoreboard, 'advisory': advisory,
                             'execution_enabled': False},
                            headers={'Cache-Control': 'no-store'})

    @app.get('/desk/evidence')
    def page() -> HTMLResponse:
        html = Path(__file__).with_name('desk_evidence.html').read_text(encoding='utf-8')
        script = html.split('<script>', 1)[1].split('</script>', 1)[0]
        sha = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        return HTMLResponse(html, headers={'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': f"default-src 'none'; script-src 'sha256-{sha}'; "
                "style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'none'; object-src 'none'"})


def _unavailable() -> JSONResponse:
    doc: dict[str, Any] = {'schema': 'desk-error/1', 'error': 'evidence_unavailable',
        'execution_enabled': False, 'evidence_status': 'unavailable'}
    return JSONResponse(doc, status_code=503, headers={'Cache-Control': 'no-store'})

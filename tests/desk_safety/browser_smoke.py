"""Optional visual fixture render; not a network, CSP or broker integration test.

Run from the repository root with PYTHONPATH=src:. and an installed Playwright /
Chromium. Uses synthetic test data only. Does not start a server or make requests.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, time
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from tests.desk_safety.conftest import world as fixture

from tree_options.desk import production, scorecards, shadows
from tree_options.desk.sessions import cutoff_instant
from tree_options.trex.clock import ET
from tree_options.trex_web.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--chromium', default='/usr/bin/chromium')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='trex-browser-') as tmp:
        world = fixture.__wrapped__(Path(tmp))
        world.queue_file()
        world.chain(world.entry)
        world.chain(world.deadline)
        shadows.update_shadows(
            session=world.deadline, now=cutoff_instant(world.deadline),
            cal=world.cal, database=world.db, store_root=world.store, queue_dir=world.queues,
        )
        cards = scorecards.build_scorecards(world.db)
        health = production.health(
            database=world.db,
            now=datetime.combine(world.cal.nth_after(world.deadline, 1), time(10), ET),
            cal=world.cal,
        )
        with TestClient(create_app(desk_state_dir=str(world.root))) as client:
            html = client.get('/desk/evidence').text
        html = html.replace('TREX / Research custody',
                            'TREX / Synthetic fixture · not your trading book')
        evidence = {
            'renderer': 'Chromium', 'fixture': 'synthetic_not_live',
            'method': 'inline_content_with_fixture_fetch',
            'limitations': ['not HTTP navigation', 'not CSP enforcement', 'not live broker data'],
            'checks': [],
        }
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=args.chromium, headless=True, args=['--no-sandbox'],
            )
            for name, width, height in [('desktop', 1440, 1120), ('mobile', 390, 844)]:
                page = browser.new_page(viewport={'width': width, 'height': height})
                errors: list[str] = []
                page.on('pageerror', lambda error, sink=errors: sink.append(str(error)))
                page.evaluate('''fixtures => { window.fetch = async path => ({
                    json: async () => fixtures[path], ok: true
                }); }''', {'/api/desk/health': health, '/api/desk/scorecards': cards})
                page.set_content(html)
                page.wait_for_function("document.getElementById('resolved').textContent === '1'")
                # Hand-computed fixture outcome, not an oracle read from production code.
                assert '-$12.60' in page.locator('#families').inner_text()
                assert not errors, errors
                overflow = page.evaluate('document.documentElement.scrollWidth > innerWidth')
                assert not overflow, f'{name}: horizontal page overflow'
                evidence['checks'].append({
                    'viewport': name, 'page_errors': errors,
                    'horizontal_page_overflow': overflow, 'modeled_net_rendered': '-$12.60',
                })
                page.screenshot(path=str(args.out / f'TREX-evidence-{name}.png'), full_page=True)
                page.close()
            browser.close()
        (args.out / 'browser-verification.json').write_text(json.dumps(evidence, indent=2) + '\n')
        print(json.dumps(evidence, indent=2))


if __name__ == '__main__':
    main()

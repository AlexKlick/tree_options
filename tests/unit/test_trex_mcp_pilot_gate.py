"""Gate cannot overclaim authorization; static import policy is red/green tested."""
from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/trex_mcp_pilot_gate.py'
spec = importlib.util.spec_from_file_location('trex_mcp_pilot_gate', SCRIPT)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_forbidden_execution_imports_are_reported() -> None:
    assert mod.unsafe_imports('from tree_options.trex.enter import run\n')
    assert mod.unsafe_imports('import ib_async\n')
    assert mod.unsafe_imports('from tree_options.desk.store import ChainStore\n')
    assert mod.unsafe_imports('def lazy():\n    import snaptrade\n')
    assert mod.unsafe_imports('import tree_options.execution.foo\n')
    assert not mod.unsafe_imports('from tree_options.trex_mcp.analysis import evaluate_bearish_scenario\n')
    assert not mod.unsafe_imports('import httpx\n')


def test_static_gate_is_clean_for_the_mcp_module() -> None:
    failures = mod.static_gate(mod.HERE)
    assert failures == []


def test_gate_never_claims_live_or_customer_acceptance() -> None:
    report = mod.gate(mod.HERE, static_only=True)
    assert report['verdict'] == 'LOCAL_CHECKS_PASS_NOT_CUSTOMER_READY'
    assert report['customer_activation_authorized'] is False
    assert report['oauth_interoperability_checked'] is False
    assert report['host_deployment_checked'] is False


def test_reject_import_in_fake_package(tmp_path: Path) -> None:
    target = tmp_path / 'src/tree_options/trex_mcp'
    target.mkdir(parents=True)
    (target / 'server.py').write_text('import tree_options.trex.monitor\n')
    assert any('forbidden import' in msg for msg in mod.static_gate(tmp_path))

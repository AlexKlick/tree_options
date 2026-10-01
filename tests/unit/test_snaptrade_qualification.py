import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from tree_options.execution.snaptrade_provider import (
    AccountSnapshot,
    ProviderObservation,
    ProviderUnavailable,
    SnapTradeProvider,
)
from tree_options.trex.snaptrade_qualification import initialize_binding, qualify_read_only
from tree_options.trex.snaptrade_runtime import SnapTradePaperRuntime
from tree_options.trex.supervised import SupervisedPaths

from .test_snaptrade_provider import FakeSDK, binding

NOW = datetime(2026, 9, 30, 2, tzinfo=UTC)


def runtime_for(tmp_path, monkeypatch):
    from . import test_snaptrade_provider

    monkeypatch.setattr(test_snaptrade_provider, "NOW", NOW)
    sdk = FakeSDK()
    provider = SnapTradeProvider(
        sdk, binding(), user_id="user", user_secret="SECRET", clock=lambda: NOW
    )
    runtime = SnapTradePaperRuntime(
        provider,
        SupervisedPaths(tmp_path / "state"),
        ownership_root=tmp_path / "owners",
        clock=lambda: NOW,
    )
    return runtime, sdk


def test_qualification_is_durable_readonly_and_releases_ownership(tmp_path, monkeypatch):
    runtime, sdk = runtime_for(tmp_path, monkeypatch)
    receipt = qualify_read_only(runtime)
    assert receipt["verdict"] == "QUALIFIED"
    assert receipt["owner_held_at_assessment"] is True
    assert receipt["orders_authorized"] is False
    assert receipt["exact_economics"] is False
    assert receipt["live_money"] is False
    assert receipt["findings"] == []
    assert runtime.owner.held is False and runtime.ready is False
    assert json.loads((runtime.paths.root / "read-only-qualification.json").read_text()) == receipt
    assert "SECRET" not in json.dumps(receipt)
    assert all(name.startswith(("get_", "detail_")) for name, _ in sdk.calls)
    assert len(receipt["observations"]) == 5
    assert all(row["request_id"] for row in receipt["observations"])
    assert not runtime.paths.mandate().exists()


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"disabled": True}, "connection_disabled_or_unknown"),
        ({"disabled": None}, "connection_disabled_or_unknown"),
        (
            {"data_freshness_mode": {"institution": "realtime", "snaptrade": "delayed"}},
            "connection_freshness_unverified",
        ),
        (
            {"data_freshness_mode": {"institution": "delayed", "snaptrade": "realtime"}},
            "connection_freshness_unverified",
        ),
        ({"data_freshness_mode": None}, "connection_freshness_unverified"),
        ({"id": "other-connection"}, "connection_identity_mismatch"),
    ],
)
def test_cached_disabled_or_wrong_connection_cannot_qualify(
    tmp_path, monkeypatch, change, expected
):
    runtime, sdk = runtime_for(tmp_path, monkeypatch)
    original = sdk.detail_brokerage_authorization

    def response(**kwargs):
        result = original(**kwargs)
        result.body.update(change)
        return result

    sdk.detail_brokerage_authorization = response
    receipt = qualify_read_only(runtime)
    assert receipt["verdict"] == "BLOCKED" and expected in receipt["findings"]
    assert runtime.owner.held is False


def test_stale_snapshot_successful_transport_cannot_qualify(tmp_path, monkeypatch):
    runtime, sdk = runtime_for(tmp_path, monkeypatch)
    original = sdk.get_user_account_details

    def response(**kwargs):
        result = original(**kwargs)
        result.body["sync_status"]["holdings"]["last_successful_sync"] = "2026-09-29T02:00:00Z"
        return result

    sdk.get_user_account_details = response
    receipt = qualify_read_only(runtime)
    assert receipt["verdict"] == "BLOCKED" and "account_snapshot_stale" in receipt["findings"]


@pytest.mark.parametrize("unavailable", [True, None])
def test_unavailable_holdings_are_not_an_empty_account(tmp_path, monkeypatch, unavailable):
    runtime, sdk = runtime_for(tmp_path, monkeypatch)
    original = sdk.get_user_account_details

    def response(**kwargs):
        result = original(**kwargs)
        result.body["sync_status"]["holdings"]["holdings_unavailable"] = unavailable
        return result

    sdk.get_user_account_details = response
    receipt = qualify_read_only(runtime)
    assert (
        receipt["verdict"] == "BLOCKED" and "holdings_availability_unknown" in receipt["findings"]
    )


def test_failed_refresh_clears_previous_readiness(tmp_path, monkeypatch):
    runtime, _ = runtime_for(tmp_path, monkeypatch)
    runtime.start()
    assert runtime.ready is True

    def unavailable():
        raise ProviderUnavailable("unavailable")

    monkeypatch.setattr(runtime.provider, "account_snapshot", unavailable)
    with pytest.raises(ProviderUnavailable):
        runtime.refresh()
    assert runtime.ready is False and runtime.account is None
    runtime.close()


def test_missing_private_binding_writes_blocked_receipt_without_provider_contact(tmp_path):
    from tree_options.trex.snaptrade_qualification import main

    state = tmp_path / "state"
    assert main(["--config", str(tmp_path / "absent.json"), "--state", str(state), "qualify"]) == 2
    receipt = json.loads((state / "read-only-qualification.json").read_text())
    assert receipt["verdict"] == "BLOCKED" and receipt["findings"] == [
        "private_binding_unavailable"
    ]
    assert receipt["observations"] == [] and receipt["orders_authorized"] is False


def test_template_is_private_offline_and_never_overwrites(tmp_path, monkeypatch):
    def forbidden(**kwargs):
        raise AssertionError("offline setup called SDK")

    monkeypatch.setattr("tree_options.execution.snaptrade_provider.build_sdk", forbidden)
    path = tmp_path / "private" / "snaptrade-paper.json"
    initialize_binding(path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        initialize_binding(path)
    assert path.read_bytes() == before
    with pytest.raises(ProviderUnavailable):
        SnapTradeProvider.from_private_file(path)


@pytest.mark.parametrize("kind", ["missing", "public", "symlink"])
def test_private_binding_failures_are_safe(tmp_path, kind):
    path = tmp_path / "binding.json"
    if kind != "missing":
        initialize_binding(path)
    if kind == "public":
        path.chmod(0o644)
    if kind == "symlink":
        alias = tmp_path / "link.json"
        alias.symlink_to(path)
        path = alias
    with pytest.raises(ProviderUnavailable):
        SnapTradeProvider.from_private_file(path)


@pytest.mark.parametrize(
    "method,body,expected",
    [
        (
            "get_user_account_balance",
            [{"currency": {"code": "USD"}, "cash": "NaN"}],
            "account_balance_invalid",
        ),
        (
            "get_user_account_balance",
            [{"currency": {"code": "USD"}, "cash": None}],
            "account_balance_invalid",
        ),
        ("get_user_account_balance", [], "account_balances_unavailable"),
        ("get_all_account_positions", [{"units": None}], "account_position_invalid"),
        (
            "get_user_account_orders",
            [{"status": "UNKNOWN", "filled_quantity": 0, "time_updated": NOW.isoformat()}],
            "account_order_ambiguous",
        ),
        (
            "get_user_account_orders",
            [
                {
                    "status": "ACCEPTED",
                    "brokerage_order_id": "b1",
                    "total_quantity": 2,
                    "filled_quantity": 1,
                    "time_updated": NOW.isoformat(),
                }
            ],
            "account_order_invalid",
        ),
        (
            "get_user_account_orders",
            [
                {
                    "status": "REJECTED",
                    "brokerage_order_id": "b1",
                    "total_quantity": 2,
                    "filled_quantity": 1,
                    "time_updated": NOW.isoformat(),
                }
            ],
            "account_order_invalid",
        ),
        (
            "get_user_account_orders",
            [
                {
                    "status": "EXECUTED",
                    "brokerage_order_id": "b1",
                    "total_quantity": 2,
                    "filled_quantity": 1,
                    "time_updated": NOW.isoformat(),
                }
            ],
            "account_order_invalid",
        ),
        ("get_user_account_orders", [None], "account_order_invalid"),
    ],
)
def test_malformed_account_facts_block_qualification(tmp_path, monkeypatch, method, body, expected):
    runtime, sdk = runtime_for(tmp_path, monkeypatch)
    original = getattr(sdk, method)

    def response(**kwargs):
        result = original(**kwargs)
        result.body = body
        return result

    setattr(sdk, method, response)
    receipt = qualify_read_only(runtime)
    assert receipt["verdict"] == "BLOCKED" and expected in receipt["findings"]


@pytest.mark.parametrize(
    "at", [NOW - timedelta(seconds=31), NOW + timedelta(seconds=1), NOW.replace(tzinfo=None)]
)
def test_stale_future_and_naive_observations_fail_closed(tmp_path, monkeypatch, at):
    runtime, _ = runtime_for(tmp_path, monkeypatch)
    account = runtime.provider.account_snapshot()
    obs = ProviderObservation(account.orders.body, at, "req", "a" * 64)
    snapshot = AccountSnapshot(
        account.binding,
        account.details,
        account.balances,
        account.positions,
        obs,
        account.holdings_at,
        account.connection,
    )
    assert not snapshot.fresh_at(NOW)


def test_same_provider_account_with_another_alias_is_fenced_before_any_reads(tmp_path, monkeypatch):
    runtime, _ = runtime_for(tmp_path, monkeypatch)
    runtime.start()
    provider = SnapTradeProvider(
        FakeSDK(),
        binding().model_copy(update={"alias": "another-alias"}),
        user_id="user",
        user_secret="SECRET",
        clock=lambda: NOW,
    )
    other = SnapTradePaperRuntime(
        provider,
        SupervisedPaths(tmp_path / "other"),
        ownership_root=runtime.owner.root,
        clock=lambda: NOW,
    )
    receipt = qualify_read_only(other)
    assert receipt["verdict"] == "BLOCKED" and receipt["findings"] == ["account_already_owned"]
    assert provider._sdk.calls == [] and runtime.owner.held and runtime.provider_owner.held
    runtime.close()
    assert qualify_read_only(other)["verdict"] == "QUALIFIED"


def test_unknown_request_identity_blocks_and_raw_metadata_never_persists(tmp_path, monkeypatch):
    runtime, sdk = runtime_for(tmp_path, monkeypatch)
    original = sdk.get_user_account_details

    def response(**kwargs):
        result = original(**kwargs)
        result.body["metadata"] = {"user_secret": "SENTINEL-PRIVATE"}
        result.headers = {}
        return result

    sdk.get_user_account_details = response
    receipt = qualify_read_only(runtime)
    assert (
        receipt["verdict"] == "BLOCKED" and "provider_request_id_unavailable" in receipt["findings"]
    )
    for path in runtime.paths.root.rglob("*.json"):
        assert "SENTINEL-PRIVATE" not in path.read_text() and "SECRET" not in path.read_text()


def test_provider_exception_is_safe_and_replaces_latest_qualified_receipt(tmp_path, monkeypatch):
    runtime, _ = runtime_for(tmp_path, monkeypatch)
    assert qualify_read_only(runtime)["verdict"] == "QUALIFIED"

    def failure():
        raise RuntimeError("SECRET in signed URL")

    monkeypatch.setattr(runtime.provider, "account_snapshot", failure)
    receipt = qualify_read_only(runtime)
    assert receipt["verdict"] == "BLOCKED" and "SECRET" not in json.dumps(receipt)
    assert (
        json.loads((runtime.paths.root / "read-only-qualification.json").read_text())["verdict"]
        == "BLOCKED"
    )
    assert len(list((runtime.paths.root / "qualification").glob("*.json"))) == 2


def test_check_config_validates_offline_without_sdk_or_secrets(tmp_path, monkeypatch, capsys):
    from tree_options.trex.snaptrade_qualification import main

    path = tmp_path / "binding.json"
    initialize_binding(path)
    config = json.loads(path.read_text())
    config["credentials"] = {k: "PRIVATE-SENTINEL" for k in config["credentials"]}
    config["binding"].update(account_id="account-1", paper_confirmed_by="operator")
    path.write_text(json.dumps(config))
    monkeypatch.setattr(
        "tree_options.execution.snaptrade_provider.build_sdk",
        lambda **kwargs: pytest.fail("SDK called offline"),
    )
    assert main(["--config", str(path), "check-config"]) == 0
    assert "PRIVATE-SENTINEL" not in capsys.readouterr().out


def test_receipt_expires_from_oldest_observation_without_extending_its_freshness(
    tmp_path, monkeypatch
):
    runtime, sdk = runtime_for(tmp_path, monkeypatch)
    original = sdk.get_user_account_details

    def response(**kwargs):
        result = original(**kwargs)
        result.body["sync_status"]["holdings"]["last_successful_sync"] = (
            NOW - timedelta(seconds=15)
        ).isoformat()
        return result

    sdk.get_user_account_details = response
    receipt = qualify_read_only(runtime)
    assert receipt["verdict"] == "QUALIFIED"
    assert receipt["expires_at"] == (NOW + timedelta(seconds=15)).isoformat()


def test_read_connection_cannot_enter_the_canary_effect_boundary(tmp_path):
    from .test_snaptrade_runtime import FakeProvider, setup

    provider = FakeProvider()
    runtime, _, effect, order = setup(tmp_path, provider)
    original = provider.account_snapshot

    def readonly():
        snapshot = original()
        snapshot.connection.body["type"] = "read"
        return snapshot

    provider.account_snapshot = readonly
    with pytest.raises(Exception, match="connection_read_only"):
        runtime.canary(order, effect, operator_approved=True)
    assert provider.calls == 0
    runtime.close()


def test_refused_contender_preserves_active_owner_projection(tmp_path, monkeypatch):
    runtime, _ = runtime_for(tmp_path, monkeypatch)
    runtime.start()
    runtime.publish()
    projection = runtime.paths.root / "projection.json"
    before = projection.read_bytes()
    provider = SnapTradeProvider(
        FakeSDK(), binding(), user_id="user", user_secret="SECRET", clock=lambda: NOW
    )
    contender = SnapTradePaperRuntime(
        provider, runtime.paths, ownership_root=runtime.owner.root, clock=lambda: NOW
    )
    receipt = qualify_read_only(contender)
    assert receipt["verdict"] == "BLOCKED" and receipt["findings"] == ["account_already_owned"]
    assert projection.read_bytes() == before
    assert runtime.owner.held and runtime.provider_owner.held
    runtime.close()


def test_state_root_cannot_be_owned_or_rebound_by_another_account(tmp_path, monkeypatch):
    runtime, _ = runtime_for(tmp_path, monkeypatch)
    runtime.start()
    runtime.publish()
    projection = runtime.paths.root / "projection.json"
    before = projection.read_bytes()
    provider = SnapTradeProvider(
        FakeSDK(),
        binding().model_copy(update={"alias": "another", "account_id": "account-2"}),
        user_id="user",
        user_secret="SECRET",
        clock=lambda: NOW,
    )
    other = SnapTradePaperRuntime(
        provider, runtime.paths, ownership_root=runtime.owner.root, clock=lambda: NOW
    )
    receipt = qualify_read_only(other)
    assert receipt["verdict"] == "BLOCKED" and receipt["findings"] == ["account_already_owned"]
    assert projection.read_bytes() == before and provider._sdk.calls == []
    runtime.close()
    closed = projection.read_bytes()
    receipt = qualify_read_only(other)
    assert receipt["findings"] == ["state_account_binding_mismatch"]
    assert projection.read_bytes() == closed and provider._sdk.calls == []


def test_receipt_identity_is_canonical_idempotent_and_collision_refuses_rewrite(
    tmp_path, monkeypatch
):
    from tree_options.action_graph.proposal import canonical_bytes
    from tree_options.trex.snaptrade_qualification import _persist
    from tree_options.trex.supervised import SupervisedRefused

    runtime, _ = runtime_for(tmp_path, monkeypatch)
    receipt = qualify_read_only(runtime)
    payload = canonical_bytes(receipt)
    historical = (
        runtime.paths.root / "qualification" / (hashlib.sha256(payload).hexdigest() + ".json")
    )
    assert historical.read_bytes() == payload
    inode = historical.stat().st_ino
    _persist(runtime.paths, receipt)
    assert historical.stat().st_ino == inode
    latest = (runtime.paths.root / "read-only-qualification.json").read_bytes()
    historical.write_text("CORRUPT")
    with pytest.raises(SupervisedRefused, match="qualification_receipt_identity_collision"):
        _persist(runtime.paths, receipt)
    assert historical.read_text() == "CORRUPT"
    assert (runtime.paths.root / "read-only-qualification.json").read_bytes() == latest


def test_account_fence_excludes_a_separate_process(tmp_path, monkeypatch):
    runtime, _ = runtime_for(tmp_path, monkeypatch)
    runtime.start()
    command = "from pathlib import Path; from tree_options.trex.account_ownership import AccountOwnership; from tree_options.trex.supervised import SupervisedRefused; import sys\ntry:\n AccountOwnership(Path(sys.argv[1]),sys.argv[2]).acquire()\nexcept SupervisedRefused:\n print('REFUSED')\nelse:\n raise SystemExit('Fence failed')"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            command,
            str(runtime.owner.root),
            "snaptrade-account:" + runtime.provider.binding.account_id,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert result.stdout.strip() == "REFUSED"
    runtime.close()


@pytest.mark.parametrize("symbol", ["AAPL.", "A...", "AA..BB", ".AAPL"])
def test_canary_symbol_requires_valid_us_equity_share_class_shape(symbol):
    from tree_options.execution.snaptrade_provider import EquityPaperEffect

    with pytest.raises(ValueError):
        EquityPaperEffect(
            intent_id="intent",
            account_alias="alias",
            owner_epoch="epoch",
            strategy_version="operational-canary/1",
            symbol=symbol,
            quantity=1,
            limit="50",
        )


def test_existing_unbound_execution_state_cannot_be_claimed(tmp_path, monkeypatch):
    runtime, sdk = runtime_for(tmp_path, monkeypatch)
    runtime.paths.root.mkdir()
    foreign = runtime.paths.root / "projection.json"
    foreign.write_bytes(b'{"owner_epoch":"ibkr-owner","owner_held":true}')
    before = foreign.read_bytes()
    receipt = qualify_read_only(runtime)
    assert receipt["findings"] == ["state_account_binding_unknown"]
    assert foreign.read_bytes() == before and sdk.calls == []
    assert not (runtime.paths.root / "account-binding.json").exists()


def test_qualifying_an_active_runtime_object_does_not_release_or_publish_it(tmp_path, monkeypatch):
    runtime, sdk = runtime_for(tmp_path, monkeypatch)
    runtime.start()
    runtime.publish()
    before = (runtime.paths.root / "projection.json").read_bytes()
    calls = len(sdk.calls)
    receipt = qualify_read_only(runtime)
    assert receipt["findings"] == ["qualification_runtime_already_started"]
    assert receipt["owner_released_after_assessment"] is False
    assert runtime.owner.held and runtime.provider_owner.held and runtime.state_owner.held
    assert runtime.ready and len(sdk.calls) == calls
    assert (runtime.paths.root / "projection.json").read_bytes() == before
    runtime.close()


def test_personal_template_is_private_and_has_no_commercial_user_fields(tmp_path):
    from tree_options.trex.snaptrade_qualification import initialize_binding

    path = tmp_path / "personal.json"
    initialize_binding(path, auth_mode="personal")
    credentials = json.loads(path.read_text())["credentials"]
    assert credentials == {"auth_mode": "personal", "client_id": "", "consumer_key": ""}
    assert path.stat().st_mode & 0o777 == 0o600
    before = path.read_bytes()
    with pytest.raises(OSError):
        initialize_binding(path, auth_mode="personal")
    assert path.read_bytes() == before

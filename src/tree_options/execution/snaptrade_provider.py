"""Server-only SnapTrade SDK boundary. No SDK types escape into research.

Account bindings and secrets are loaded from a private operator-managed file.
Mutation methods are internal to the supervised broker, never browser tools.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import Field, StrictStr

from tree_options.execution.records import ExactPrice
from tree_options.schemas.common import IdStr, StrictModel

SDK_VERSION = "13.0.28"


class ProviderUnavailable(RuntimeError):
    pass


class ProviderDisconnected(ProviderUnavailable):
    pass


class PaperAccountBinding(StrictModel):
    alias: IdStr
    account_id: IdStr
    brokerage_slug: IdStr
    paper_confirmed_by: IdStr
    credential_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment: str = "broker_paper"
    live_money: bool = False

    def model_post_init(self, context: Any) -> None:
        if (
            self.environment != "broker_paper"
            or self.live_money
            or self.brokerage_slug != "ALPACA-PAPER"
        ):
            raise ValueError("only operator-confirmed Alpaca paper bindings are supported")


@dataclass(frozen=True)
class ProviderObservation:
    body: Any = field(repr=False)
    captured_at: datetime
    request_id: str | None
    digest: str


@dataclass(frozen=True)
class AccountSnapshot:
    binding: PaperAccountBinding
    details: ProviderObservation
    balances: ProviderObservation
    positions: ProviderObservation
    orders: ProviderObservation
    holdings_at: datetime
    connection: ProviderObservation | None = None

    def fresh_at(self, now: datetime, *, max_age_seconds: int = 30) -> bool:
        stamps: tuple[datetime, ...] = (
            self.holdings_at,
            self.details.captured_at,
            self.balances.captured_at,
            self.positions.captured_at,
            self.orders.captured_at,
        )
        if self.connection is not None:
            stamps += (self.connection.captured_at,)
        try:
            return now.utcoffset() is not None and all(
                t.utcoffset() is not None and 0 <= (now - t).total_seconds() <= max_age_seconds
                for t in stamps
            )
        except (TypeError, ValueError):
            return False


class PrivateCredentials(StrictModel):
    client_id: StrictStr = Field(min_length=1, repr=False)
    consumer_key: StrictStr = Field(min_length=1, repr=False)
    user_id: StrictStr = Field(min_length=1, repr=False)
    user_secret: StrictStr = Field(min_length=1, repr=False)


def load_private_binding(path: Path) -> tuple[PaperAccountBinding, PrivateCredentials]:
    """Offline validation using the opened file's identity, never printing values."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError
            config = json.load(stream)
        secrets = PrivateCredentials.model_validate(config["credentials"])
        if any(not value.strip() for value in secrets.model_dump().values()):
            raise ValueError
        fingerprint = hashlib.sha256(
            json.dumps(secrets.model_dump(), sort_keys=True).encode()
        ).hexdigest()
        binding = PaperAccountBinding.model_validate(
            {**config["binding"], "credential_sha256": fingerprint}
        )
        return binding, secrets
    except Exception:
        raise ProviderUnavailable("private SnapTrade binding unavailable or invalid") from None


def account_findings(account: AccountSnapshot, now: datetime) -> tuple[str, ...]:
    """Admit current read-only facts, without granting execution authority."""
    from tree_options.execution.records import BrokerReadbackStatus
    from tree_options.execution.snaptrade_adapter import (
        snapshot_from_mapping,
        validate_snapshot_status,
    )

    findings: list[str] = []
    if not account.fresh_at(now):
        findings.append("account_snapshot_stale")
    raw = account.details.body
    if not isinstance(raw, dict) or raw.get("id") != account.binding.account_id:
        findings.append("account_identity_mismatch")
        raw = {}
    if (
        raw.get("is_paper") is not True
        or not isinstance(raw.get("institution_name"), str)
        or raw["institution_name"].casefold() != "alpaca"
    ):
        findings.append("paper_environment_unverified")
    sync_status = raw.get("sync_status")
    holdings = sync_status.get("holdings", {}) if isinstance(sync_status, dict) else {}
    if not isinstance(holdings, dict) or holdings.get("holdings_unavailable") is not False:
        findings.append("holdings_availability_unknown")
    connection = account.connection.body if account.connection is not None else {}
    if not isinstance(connection, dict):
        connection = {}
    if not raw.get("brokerage_authorization") or connection.get("id") != raw.get(
        "brokerage_authorization"
    ):
        findings.append("connection_identity_mismatch")
    if connection.get("disabled") is not False:
        findings.append("connection_disabled_or_unknown")
    if connection.get("data_freshness_mode") != {
        "institution": "realtime",
        "snaptrade": "realtime",
    }:
        findings.append("connection_freshness_unverified")
    if (
        not isinstance(connection.get("brokerage"), dict)
        or connection["brokerage"].get("slug") != account.binding.brokerage_slug
    ):
        findings.append("connection_brokerage_mismatch")
    if not isinstance(connection.get("type"), str) or connection["type"] not in {"read", "trade"}:
        findings.append("connection_permission_unknown")
    for name in ("balances", "positions", "orders"):
        observation = getattr(account, name)
        if not isinstance(observation.body, list):
            findings.append(f"account_{name}_shape_invalid")
    if isinstance(account.balances.body, list):
        if not account.balances.body:
            findings.append("account_balances_unavailable")
        currencies: set[str] = set()
        for row in account.balances.body:
            try:
                code = row["currency"]["code"]
                if not isinstance(code, str) or len(code) != 3 or code in currencies:
                    raise ValueError
                currencies.add(code)
                if isinstance(row["cash"], bool) or not Decimal(str(row["cash"])).is_finite():
                    raise ValueError
            except (KeyError, TypeError, ValueError, InvalidOperation):
                findings.append("account_balance_invalid")
    if isinstance(account.positions.body, list):
        for row in account.positions.body:
            try:
                if (
                    not isinstance(row["symbol"]["symbol"]["symbol"], str)
                    or not row["symbol"]["symbol"]["symbol"]
                ):
                    raise ValueError
                if isinstance(row["units"], bool) or not Decimal(str(row["units"])).is_finite():
                    raise ValueError
            except (KeyError, TypeError, ValueError, InvalidOperation):
                findings.append("account_position_invalid")
    if isinstance(account.orders.body, list):
        seen: dict[str, str] = {}
        for row in account.orders.body:
            try:
                if not isinstance(row, dict):
                    raise ValueError
                snap = snapshot_from_mapping(row)
                if validate_snapshot_status(snap) is BrokerReadbackStatus.AMBIGUOUS:
                    findings.append("account_order_ambiguous")
                if snap.broker_snapshot_at > now:
                    findings.append("account_order_future")
                identity = snap.brokerage_order_id
                if not identity:
                    findings.append("account_order_identity_unknown")
                elif identity in seen and seen[identity] != snap.raw_digest:
                    findings.append("account_order_identity_collision")
                elif identity:
                    seen[identity] = snap.raw_digest
            except (ValueError, TypeError, KeyError):
                findings.append("account_order_invalid")
    return tuple(dict.fromkeys(findings))


def build_sdk(*, client_id: str, consumer_key: str) -> Any:
    sdk = importlib.import_module("snaptrade_client")
    auth = sdk.SnapTradeAuth.commercial_api_key(client_id=client_id, consumer_key=consumer_key)
    config = sdk.Configuration(auth=auth)
    config.retries = 0
    config.debug = False
    client = sdk.SnapTrade(configuration=config, auth=auth)
    # The generated methods omit timeout. SDK 13.0.28 also signs the
    # pre-serialization Decimal body, which its signing JSON encoder rejects.
    # Sign the native JSON body already serialized by the SDK, preserving the
    # exact wire numbers; this shim changes neither authentication nor retries.
    api = client.trading.api_client
    call_api = api.call_api

    def bounded_call(*args: Any, **kwargs: Any) -> Any:
        kwargs["timeout"] = 10
        if kwargs.get("serialized_body") is not None:
            kwargs["body"] = json.loads(kwargs["serialized_body"])
        return call_api(*args, **kwargs)

    api.call_api = bounded_call
    return client


class SnapTradeProvider:
    def __init__(
        self,
        sdk: Any,
        binding: PaperAccountBinding,
        *,
        user_id: str,
        user_secret: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sdk = sdk
        self.binding = binding
        self._user_id = user_id
        self._user_secret = user_secret
        self.clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def from_private_file(cls, path: Path) -> SnapTradeProvider:
        binding, secrets = load_private_binding(path)
        try:
            sdk = build_sdk(client_id=secrets.client_id, consumer_key=secrets.consumer_key)
            return cls(sdk, binding, user_id=secrets.user_id, user_secret=secrets.user_secret)
        except Exception:
            raise ProviderUnavailable("invalid private SnapTrade binding or credentials") from None

    def _call(self, namespace: str, method: str, **kwargs: Any) -> ProviderObservation:
        try:
            scope = {} if namespace == "connections" else {"account_id": self.binding.account_id}
            result = getattr(getattr(self._sdk, namespace), method)(
                **scope,
                user_id=self._user_id,
                user_secret=self._user_secret,
                **kwargs,
            )
            data = getattr(result.response, "data", None)
            body = json.loads(data, parse_float=Decimal) if data else result.body
            encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
            headers = result.headers
            return ProviderObservation(
                body,
                self.clock(),
                headers.get("X-Request-ID") or headers.get("x-request-id"),
                hashlib.sha256(encoded.encode()).hexdigest(),
            )
        except TimeoutError:
            raise TimeoutError("SnapTrade request timed out") from None
        except Exception as error:
            cause = getattr(error, "reason", error)
            if type(cause).__name__ in {"ReadTimeoutError", "ConnectTimeoutError"}:
                raise TimeoutError("SnapTrade request timed out") from None
            if isinstance(cause, ConnectionError) or type(cause).__name__ in {
                "ProtocolError",
                "NewConnectionError",
            }:
                raise ProviderDisconnected("SnapTrade connection unavailable") from None
            # Generated exceptions can include signed URLs/userSecret. Preserve
            # uncertainty with a safe code, never the provider exception text.
            raise ProviderUnavailable("SnapTrade transport or response unavailable") from None

    def account_snapshot(self) -> AccountSnapshot:
        details = self._call("account_information", "get_user_account_details")
        raw = details.body
        if not isinstance(raw, dict) or raw.get("id") != self.binding.account_id:
            raise ProviderUnavailable("account identity mismatch")
        # Account.is_paper is the generated provider contract's affirmative
        # environment fact. Operator labels alone cannot authorize effects.
        if (
            raw.get("is_paper") is not True
            or not isinstance(raw.get("institution_name"), str)
            or raw["institution_name"].casefold() != "alpaca"
        ):
            raise ProviderUnavailable("provider did not positively identify Alpaca paper")
        try:
            holdings = raw["sync_status"]["holdings"]
            if holdings["initial_sync_completed"] is not True:
                raise ValueError
            at = datetime.fromisoformat(holdings["last_successful_sync"].replace("Z", "+00:00"))
            if at.tzinfo is None:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ProviderUnavailable("account holdings freshness is unknown") from None
        authorization_id = raw.get("brokerage_authorization")
        if not isinstance(authorization_id, str) or not authorization_id:
            raise ProviderUnavailable("account connection identity is unknown")
        connection = self._call(
            "connections", "detail_brokerage_authorization", authorization_id=authorization_id
        )
        balances = self._call("account_information", "get_user_account_balance")
        positions = self._call("account_information", "get_all_account_positions")
        orders = self._call("account_information", "get_user_account_orders", state="all", days=7)
        if (
            not isinstance(balances.body, list)
            or not isinstance(positions.body, list)
            or not isinstance(orders.body, list)
        ):
            raise ProviderUnavailable(
                "account state must contain complete balances/positions/orders"
            )
        return AccountSnapshot(
            self.binding, details, balances, positions, orders, at.astimezone(UTC), connection
        )

    def order_readback(self, broker_order_id: str) -> ProviderObservation:
        return self._call(
            "account_information",
            "get_user_account_order_detail",
            brokerage_order_id=broker_order_id,
        )

    def quotes(self, symbol: str) -> ProviderObservation:
        return self._call("trading", "get_user_account_quotes", symbols=symbol, use_ticker=True)

    def activities(self) -> ProviderObservation:
        # Daily activities are retained observations only. They are not a
        # validated stable-fill/correction/fee source for execution.evidence.
        return self._call("account_information", "get_account_activities", limit=1000, offset=0)

    def _submit(
        self, *, symbol: str, side: str, quantity: int, limit: Decimal, client_order_id: str
    ) -> ProviderObservation:
        if type(quantity) is not int or quantity < 1:
            raise ValueError("integer shares required")
        if not limit.is_finite() or limit <= 0 or Decimal(str(float(limit))) != limit:
            raise ValueError("SDK numeric serialization cannot preserve limit")
        return self._call(
            "trading",
            "place_force_order",
            symbol=symbol,
            action=side,
            units=Decimal(quantity),
            order_type="Limit",
            time_in_force="Day",
            price=float(limit),
            client_order_id=client_order_id,
        )

    def _cancel(self, broker_order_id: str) -> ProviderObservation:
        return self._call("trading", "cancel_order", brokerage_order_id=broker_order_id)

    def _replace(
        self, broker_order_id: str, *, symbol: str, side: str, quantity: int, limit: Decimal
    ) -> ProviderObservation:
        if Decimal(str(float(limit))) != limit:
            raise ValueError("SDK numeric serialization cannot preserve replacement limit")
        return self._call(
            "trading",
            "replace_order",
            brokerage_order_id=broker_order_id,
            symbol=symbol,
            action=side,
            units=Decimal(quantity),
            price=float(limit),
            order_type="Limit",
            time_in_force="Day",
        )


class EquityPaperEffect(StrictModel):
    intent_id: IdStr
    account_alias: IdStr
    owner_epoch: IdStr
    strategy_version: IdStr
    symbol: str = Field(pattern=r"^[A-Z]+(?:\.[A-Z]+)?$", max_length=10)
    side: str = "BUY"
    quantity: int = Field(strict=True, ge=1)
    limit: ExactPrice

    def model_post_init(self, context: Any) -> None:
        if self.side != "BUY" or self.strategy_version != "operational-canary/1":
            raise ValueError("initial canary permits buy/open-long operational-canary/1 only")

    def payload(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()

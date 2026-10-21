"""The historical lab: quota-gated model runs over the frozen bundle.

The model transport is faked (no network, no keys burned in tests); the
oracle for a run's accounting is the replay module itself, called
independently with the same decisions.
"""

from __future__ import annotations

import json
import urllib.error
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_desk_intraday_action_graph import _bundle
from tree_options.desk import intraday_action_graph as iag
from tree_options.desk.lab import (
    BOARD_RETRY,
    BURN_NOTE,
    LabConfig,
    RetryPolicy,
    ask_board,
    board_prompt,
    board_rows,
    burn_allowed,
    latest_sessions,
    parse_choice,
    run_lab,
    transient_reason,
)
from tree_options.trex.discovery.llm import PROVIDERS, REQUEST_TIMEOUT, LlmError
from tree_options.trex.grant_policy import WINDOWS_SCHEMA, QuotaWindow

T0 = datetime(2026, 9, 28, 20, 0, tzinfo=UTC)
UNDER = (
    QuotaWindow(name="zai", actual_left_pct=Decimal("94.0"), planned_left_pct=Decimal("51.9")),
)
ON_PLAN = (
    QuotaWindow(name="zai", actual_left_pct=Decimal("40.0"), planned_left_pct=Decimal("50.0")),
)


def _windows_file(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema": WINDOWS_SCHEMA,
                "windows": [{"name": "zai", "actual_left_pct": "94.0", "planned_left_pct": "51.9"}],
            }
        )
    )
    return path


class FakeTransport:
    """Returns OpenAI-shaped completions; the reply picks board row ``mode``.

    mode: 'first' echoes the first row id, 'bogus' an unknown id,
    'garbage' a non-JSON reply, 'http500' an HTTP failure."""

    def __init__(self, mode: str = "first") -> None:
        self.mode = mode
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, url: str, body: bytes, headers: dict[str, str], timeout: float
    ) -> tuple[int, bytes]:
        self.calls.append({"url": url, "body": json.loads(body)})
        if self.mode == "http500":
            return 500, b""
        prompt = json.loads(body)["messages"][0]["content"]
        rows = json.loads(prompt)["board"]
        if self.mode == "garbage":
            content = "I refuse your format"
        elif self.mode == "bogus":
            content = json.dumps({"choice": "not-a-real-id", "note": "x"})
        else:
            content = json.dumps({"choice": rows[0]["id"], "note": "top rr"})
        envelope = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
        return 200, json.dumps(envelope).encode()


class FakeClock:
    """Monotonic seconds that move only on a sleep or on a scripted call's
    duration, so retry delays and the wall cap are exact and instant."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ScriptedTransport:
    """One scripted outcome per call: an int is an HTTP status, an exception
    class is raised (a TimeoutError burns the call's whole timeout on the
    fake clock, any other call one second), "ok" answers the board's first
    row, "garbage" answers non-JSON. ``cycle`` replays the script per call
    forever; otherwise its last step repeats."""

    def __init__(
        self, script: list[Any], clock: FakeClock | None = None, *, cycle: bool = False
    ) -> None:
        self.script = list(script)
        self.clock = clock
        self.cycle = cycle
        self.timeouts: list[float] = []

    def __call__(
        self, url: str, body: bytes, headers: dict[str, str], timeout: float
    ) -> tuple[int, bytes]:
        self.timeouts.append(timeout)
        n = len(self.timeouts) - 1
        step = self.script[n % len(self.script) if self.cycle else min(n, len(self.script) - 1)]
        if isinstance(step, type) and issubclass(step, BaseException):
            if self.clock is not None:
                self.clock.now += timeout if step is TimeoutError else 1.0
            if step is urllib.error.URLError:
                raise urllib.error.URLError("scripted")
            raise step("scripted")
        if self.clock is not None:
            self.clock.now += 1.0
        if isinstance(step, int):
            return step, b""
        rows = json.loads(json.loads(body)["messages"][0]["content"])["board"]
        content = (
            "I refuse your format"
            if step == "garbage"
            else json.dumps({"choice": rows[0]["id"], "note": "ok"})
        )
        envelope = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
        return 200, json.dumps(envelope).encode()


@pytest.fixture()
def bundle(tmp_path: Path) -> Path:
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(_bundle(date(2026, 9, 24))))
    return path


@pytest.fixture(autouse=True)
def _fake_zai_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_ZAI", "test-key-material")


# ------------------------------------------------------------------ gates


def test_burn_gate_is_under_using_only():
    assert burn_allowed(UNDER)
    assert not burn_allowed(ON_PLAN)
    assert not burn_allowed(())


def test_a_model_policy_without_quota_headroom_skips(bundle, tmp_path):
    config = LabConfig(bundle=bundle, policy="model:zai", lab_root=tmp_path / "lab")
    document = run_lab(config, windows=ON_PLAN, now=T0)
    assert (document["status"], document["reason"]) == ("skipped", BURN_NOTE)
    assert not (tmp_path / "lab").exists() or not list((tmp_path / "lab").iterdir())


def test_a_rules_policy_runs_without_quota(bundle, tmp_path):
    config = LabConfig(bundle=bundle, policy="no_trade", lab_root=tmp_path / "lab")
    document = run_lab(config, windows=(), now=T0, transport=FakeTransport())
    assert document["status"] == "ok"
    assert document["model_calls"] == 0
    assert document["summary"]["entered"] == 0


# ------------------------------------------------------------- model runs


def test_model_choices_flow_into_the_replay_accounting(bundle, tmp_path):
    transport = FakeTransport("first")
    config = LabConfig(bundle=bundle, policy="model:zai", sessions=1, lab_root=tmp_path / "lab")
    document = run_lab(config, windows=UNDER, transport=transport, now=T0)
    assert document["status"] == "ok"
    assert document["model_calls"] == len(transport.calls) >= 1
    assert document["model_failures"] == 0
    # oracle: the same decisions through replay() reproduce the summary
    decisions = {r["snapshot"]: r["choice"] for r in document["receipts"] if r.get("choice")}
    raw = json.loads(bundle.read_bytes())
    sessions = latest_sessions(raw, 1)
    expected = iag.replay(raw, sessions, decisions)
    assert document["summary"]["entered"] == expected["entered"]
    assert document["summary"]["closed_capital_proxy"] == expected["closed_capital_proxy"]
    # the run is evidence on disk
    run_dir = Path(document["run_dir"])
    assert (run_dir / "summary.json").exists() and (run_dir / "receipts.jsonl").exists()
    assert transport.calls[0]["url"].endswith("/chat/completions")
    assert "Bearer" not in json.dumps(document)  # keys never leak into evidence


def test_a_bogus_choice_is_rejected_not_adopted(bundle, tmp_path):
    config = LabConfig(bundle=bundle, policy="model:zai", sessions=1, lab_root=tmp_path / "lab")
    document = run_lab(config, windows=UNDER, transport=FakeTransport("bogus"), now=T0)
    assert document["model_failures"] == 0  # the call succeeded; the CHOICE was refused
    assert all(r["choice"] is None for r in document["receipts"])
    assert any("unknown id rejected" in (r.get("note") or "") for r in document["receipts"])
    assert document["summary"]["entered"] == 0


def test_provider_failures_are_recorded_and_the_run_continues(bundle, tmp_path):
    config = LabConfig(
        bundle=bundle, policy="model:zai", sessions=1, boards_cap=3, lab_root=tmp_path / "lab"
    )
    clock = FakeClock()
    document = run_lab(
        config,
        windows=UNDER,
        transport=FakeTransport("http500"),
        now=T0,
        sleep=clock.sleep,
        clock=clock,
    )
    assert document["model_calls"] >= 1
    assert document["model_failures"] == document["model_calls"]
    assert all("HTTP 500" in (r.get("error") or "") for r in document["receipts"])
    # HTTP 500 is transient: every board spent its full retry budget first
    assert all(r["attempts"] == 1 + len(BOARD_RETRY.delays_s) for r in document["receipts"])
    assert document["summary"]["entered"] == 0


def test_two_runs_never_share_a_directory(bundle, tmp_path):
    root = tmp_path / "lab"
    first = run_lab(
        LabConfig(bundle=bundle, policy="model:zai", sessions=1, lab_root=root),
        windows=UNDER,
        transport=FakeTransport(),
        now=T0,
    )
    second = run_lab(
        LabConfig(bundle=bundle, policy="model:zai", sessions=1, lab_root=root),
        windows=UNDER,
        transport=FakeTransport(),
        now=T0,
    )
    assert first["run_dir"] != second["run_dir"]


# ------------------------------------------------------------------ boards


def test_board_rows_are_capped_aliased_and_choice_validated():
    rows = [
        {
            "id": f"c{i}",
            "structure": "put_credit",
            "width": "2",
            "observed_premium": "1.0",
            "max_loss_proxy": "100",
            "max_gain_proxy": "100",
            "reward_to_risk_proxy": "1.00",
            "long_recent_trade_move": None,
            "short_recent_trade_move": None,
            "data_kind": "x",
        }
        for i in range(30)
    ]
    board = board_rows({"candidates": rows})
    assert len(board) == 12 and "ticker" not in json.dumps(board).lower()
    prompt = board_prompt(board)
    content = prompt[0]["content"]
    assert '\\"choice\\"' in content and '"board"' in content  # the task is JSON-embedded
    assert parse_choice({"choice": "c0"}, {"c0"}) == ("c0", "")
    assert parse_choice({"choice": None}, {"c0"}) == (None, "")
    picked, note = parse_choice({"choice": "zz", "note": "hi"}, {"c0"})
    assert (picked, note) == (None, "unknown id rejected: zz")


def test_ask_board_uses_the_injected_transport():
    transport = FakeTransport("first")
    reply = ask_board(
        "zai",
        board_rows(
            {
                "candidates": [
                    {
                        "id": "c1",
                        "structure": "put_credit",
                        "width": "2",
                        "observed_premium": "1.0",
                        "max_loss_proxy": "100",
                        "max_gain_proxy": "100",
                        "reward_to_risk_proxy": "1.00",
                        "long_recent_trade_move": None,
                        "short_recent_trade_move": None,
                        "data_kind": "x",
                    }
                ]
            }
        ),
        transport=transport,
    )
    assert reply["choice"].startswith("c") or reply["choice"] is None
    assert len(transport.calls) == 1


# ------------------------------------------- transient errors (2026-10-21)
#
# Owner ruling 2026-10-21 (default 4a): retry transient provider errors
# twice, count only FINAL failures as model_failures, record the attempts.
# Game 20261020T010000Z lost 81 of 594 board calls (51 TimeoutError, 26 HTTP
# 429, 4 HTTP 500) to one provider incident with no retry at all.

DAYS6 = (
    date(2026, 9, 17),
    date(2026, 9, 18),
    date(2026, 9, 21),
    date(2026, 9, 22),
    date(2026, 9, 23),
    date(2026, 9, 24),
)


def _one_row_board() -> list[dict[str, Any]]:
    return board_rows(
        {
            "candidates": [
                {
                    "id": "c1",
                    "structure": "put_credit",
                    "width": "2",
                    "observed_premium": "1.0",
                    "max_loss_proxy": "100",
                    "max_gain_proxy": "100",
                    "reward_to_risk_proxy": "1.00",
                    "long_recent_trade_move": None,
                    "short_recent_trade_move": None,
                    "data_kind": "x",
                }
            ]
        }
    )


def _ask(
    script: list[Any], *, provider: str = "zai", retry: RetryPolicy | None = BOARD_RETRY
) -> tuple[dict[str, Any] | LlmError, dict[str, Any], ScriptedTransport, FakeClock]:
    clock = FakeClock()
    transport = ScriptedTransport(script, clock)
    trace: dict[str, Any] = {}
    try:
        outcome: dict[str, Any] | LlmError = ask_board(
            provider,
            _one_row_board(),
            transport=transport,
            retry=retry,
            sleep=clock.sleep,
            clock=clock,
            trace=trace,
        )
    except LlmError as error:
        outcome = error
    return outcome, trace, transport, clock


def _configured_timeout(provider: str) -> float:
    return float(PROVIDERS[provider].get("timeout", REQUEST_TIMEOUT))


def test_the_retry_policy_is_the_owner_ruling():
    """The literal numbers of the 2026-10-21 ruling: two retries 5 s then
    20 s, a timeout doubles the next attempt's timeout, a 90 s wall per
    board, the breaker after six consecutive final failures."""
    assert BOARD_RETRY.delays_s == (5.0, 20.0)
    assert BOARD_RETRY.timeout_growth == 2.0
    assert BOARD_RETRY.wall_cap_s == 90.0
    assert BOARD_RETRY.breaker_after == 6


def test_a_429_then_success_retries_once_and_is_not_a_failure():
    outcome, trace, transport, clock = _ask([429, "ok"])
    assert isinstance(outcome, dict) and outcome["choice"] == "c1"
    assert trace["attempts"] == 2
    assert trace["retry_reasons"] == ["HTTP 429"]
    assert clock.sleeps == [BOARD_RETRY.delays_s[0]]
    configured = _configured_timeout("zai")
    assert transport.timeouts == [configured, configured]  # only a TIMEOUT grows it


def test_three_timeouts_are_one_final_failure_after_three_attempts():
    outcome, trace, transport, clock = _ask([TimeoutError])
    assert isinstance(outcome, LlmError) and str(outcome).endswith("TimeoutError")
    assert trace["attempts"] == 3
    assert trace["retry_reasons"] == ["TimeoutError", "TimeoutError"]
    assert clock.sleeps == list(BOARD_RETRY.delays_s)
    configured = _configured_timeout("zai")
    grown = configured * BOARD_RETRY.timeout_growth
    # oracle: the wall left when the third attempt starts, from the ruling's
    # numbers (first attempt, first delay, second attempt, second delay)
    left = (
        BOARD_RETRY.wall_cap_s
        - (configured + BOARD_RETRY.delays_s[0] + grown)
        - (BOARD_RETRY.delays_s[1])
    )
    assert 0 < left < grown
    assert transport.timeouts == [configured, grown, left]
    assert clock.now <= BOARD_RETRY.wall_cap_s


def test_a_timeout_grows_the_next_attempt_then_succeeds():
    outcome, trace, transport, _clock = _ask([TimeoutError, "ok"])
    assert isinstance(outcome, dict)
    configured = _configured_timeout("zai")
    assert transport.timeouts == [configured, configured * BOARD_RETRY.timeout_growth]
    assert trace["attempts"] == 2 and trace["retry_reasons"] == ["TimeoutError"]


def test_a_400_is_not_retried():
    outcome, trace, transport, clock = _ask([400, "ok"])
    assert isinstance(outcome, LlmError) and "HTTP 400" in str(outcome)
    assert trace["attempts"] == 1 and trace["retry_reasons"] == []
    assert len(transport.timeouts) == 1 and clock.sleeps == []


def test_a_parse_error_is_not_retried():
    outcome, trace, transport, clock = _ask(["garbage", "ok"])
    assert isinstance(outcome, LlmError) and "no JSON object" in str(outcome)
    assert trace["attempts"] == 1
    assert len(transport.timeouts) == 1 and clock.sleeps == []


@pytest.mark.parametrize(
    ("first", "reason"),
    [
        (500, "HTTP 500"),
        (503, "HTTP 503"),
        (504, "HTTP 504"),
        (urllib.error.URLError, "URLError"),
        (ConnectionResetError, "ConnectionResetError"),
        (ConnectionRefusedError, "ConnectionRefusedError"),
        (TimeoutError, "TimeoutError"),
    ],
)
def test_5xx_and_connection_errors_are_transient(first, reason):
    outcome, trace, _transport, _clock = _ask([first, "ok"])
    assert isinstance(outcome, dict)
    assert trace["attempts"] == 2 and trace["retry_reasons"] == [reason]


@pytest.mark.parametrize(
    "message",
    [
        "zai: HTTP 400",
        "zai: HTTP 401",
        "zai: HTTP 404",
        "zai: no JSON object in model output",
        "zai: malformed completion envelope",
        "zai: reply truncated at max_tokens",
        "minimax: served model 'other' != requested 'MiniMax-M3.1-Flash-Preview'",
        "zai: ANTHROPIC_AUTH_TOKEN_ZAI or ZAI_CODING_API_KEY not set",
        "zai: ValueError",
    ],
)
def test_non_transient_errors_are_classified_as_final(message):
    assert transient_reason(LlmError(message)) is None


def test_the_wall_cap_stops_a_retry_that_cannot_finish(monkeypatch):
    """minimax's configured 45 s: 45 + 5 + (90 capped to the 40 s left)
    reaches the 90 s wall, so no third attempt starts."""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_MINIMAX2", "test-key-material")
    outcome, trace, transport, clock = _ask([TimeoutError], provider="minimax")
    assert isinstance(outcome, LlmError)
    configured = _configured_timeout("minimax")
    left = BOARD_RETRY.wall_cap_s - configured - BOARD_RETRY.delays_s[0]
    assert transport.timeouts == [configured, min(configured * 2, left)]
    assert trace["attempts"] == 2
    assert trace["retry_stopped"] == "wall_cap"
    assert clock.sleeps == [BOARD_RETRY.delays_s[0]]
    assert clock.now <= BOARD_RETRY.wall_cap_s


def test_without_a_policy_ask_board_makes_one_attempt():
    """The default stays one attempt: longrun and lab_overnight call
    ask_board directly and keep their own failure semantics."""
    outcome, trace, transport, clock = _ask([429, "ok"], retry=None)
    assert isinstance(outcome, LlmError) and "HTTP 429" in str(outcome)
    assert len(transport.timeouts) == 1 and clock.sleeps == []
    assert trace["attempts"] == 1


@pytest.fixture()
def six_day_bundle(tmp_path: Path) -> Path:
    """The single-day fixture's contracts with every day's bars appended."""
    merged = _bundle(DAYS6[0])
    for day in DAYS6[1:]:
        for ticker, body in _bundle(day)["contracts"].items():
            merged["contracts"][ticker]["results"].extend(body["results"])
    path = tmp_path / "six-day.json"
    path.write_text(json.dumps(merged))
    return path


def _six_day_config(path: Path, root: Path) -> LabConfig:
    return LabConfig(bundle=path, policy="model:zai", sessions=6, boards_cap=24, lab_root=root)


def test_a_run_counts_only_final_failures_and_records_attempts(six_day_bundle, tmp_path):
    healthy = run_lab(
        _six_day_config(six_day_bundle, tmp_path / "a"),
        windows=UNDER,
        transport=FakeTransport("first"),
        now=T0,
    )
    clock = FakeClock()
    flaky = run_lab(
        _six_day_config(six_day_bundle, tmp_path / "b"),
        windows=UNDER,
        transport=ScriptedTransport([429, "ok"], clock, cycle=True),
        now=T0,
        sleep=clock.sleep,
        clock=clock,
    )
    calls = healthy["model_calls"]
    assert calls >= 1
    assert flaky["model_calls"] == calls and flaky["boards_shown"] == healthy["boards_shown"]
    assert flaky["model_failures"] == 0
    assert flaky["first_attempt_failures"] == calls
    assert flaky["model_attempts"] == 2 * calls
    assert flaky["retry_reasons"] == {"HTTP 429": calls}
    assert flaky["skipped_breaker"] == 0 and flaky["breaker"] is None
    assert all(
        (r["ok"], r["attempts"], r["retry_reasons"]) == (True, 2, ["HTTP 429"])
        for r in flaky["receipts"]
    )
    # oracle: a retried run decides exactly what a healthy run decides
    assert flaky["summary"] == healthy["summary"]
    # sparse: a clean run carries no resilience fields (pre-correction bytes)
    resilience = {"model_attempts", "first_attempt_failures", "retry_reasons", "breaker"}
    assert not resilience & set(healthy) and "skipped_breaker" not in healthy
    assert not any({"attempts", "retry_reasons"} & set(r) for r in healthy["receipts"])
    on_disk = json.loads((Path(flaky["run_dir"]) / "summary.json").read_text())
    assert on_disk["first_attempt_failures"] == calls
    lines = (Path(flaky["run_dir"]) / "receipts.jsonl").read_text().splitlines()
    assert all(json.loads(line)["attempts"] == 2 for line in lines)


def test_three_timeouts_per_board_are_one_failure_each(bundle, tmp_path):
    clock = FakeClock()
    transport = ScriptedTransport([TimeoutError], clock)
    config = LabConfig(
        bundle=bundle, policy="model:zai", sessions=1, boards_cap=3, lab_root=tmp_path / "lab"
    )
    document = run_lab(
        config, windows=UNDER, transport=transport, now=T0, sleep=clock.sleep, clock=clock
    )
    calls = document["model_calls"]
    assert calls >= 1
    assert document["model_failures"] == calls
    assert document["first_attempt_failures"] == calls
    assert document["model_attempts"] == 3 * calls == len(transport.timeouts)
    assert all(r["attempts"] == 3 and r["ok"] is False for r in document["receipts"])


def test_the_breaker_trips_after_six_consecutive_final_failures(six_day_bundle, tmp_path):
    healthy = run_lab(
        _six_day_config(six_day_bundle, tmp_path / "a"),
        windows=UNDER,
        transport=FakeTransport("first"),
        now=T0,
    )
    planned = healthy["boards_shown"]
    after = BOARD_RETRY.breaker_after
    assert planned > after  # the fixture must leave boards for the breaker to skip
    clock = FakeClock()
    transport = ScriptedTransport([500], clock)
    document = run_lab(
        _six_day_config(six_day_bundle, tmp_path / "b"),
        windows=UNDER,
        transport=transport,
        now=T0,
        sleep=clock.sleep,
        clock=clock,
    )
    attempts = 1 + len(BOARD_RETRY.delays_s)
    assert document["model_calls"] == after == document["boards_shown"]
    assert document["model_failures"] == after  # skipped boards are NOT failures
    assert document["skipped_breaker"] == planned - after
    assert len(transport.timeouts) == after * attempts  # nothing burned after the trip
    planned_snapshots = [r["snapshot"] for r in healthy["receipts"]]
    breaker = document["breaker"]
    assert breaker["after_consecutive_failures"] == after
    assert breaker["tripped_at"] == planned_snapshots[after - 1]
    assert breaker["skipped_snapshots"] == planned_snapshots[after:]
    # receipts are the boards a model was asked; the trip is on the last one
    assert [r["snapshot"] for r in document["receipts"]] == planned_snapshots[:after]
    assert document["receipts"][-1]["breaker_tripped"] is True
    assert not any(r.get("breaker_tripped") for r in document["receipts"][:-1])
    on_disk = json.loads((Path(document["run_dir"]) / "summary.json").read_text())
    assert on_disk["skipped_breaker"] == planned - after
    assert on_disk["breaker"]["tripped_at"] == breaker["tripped_at"]


def test_a_success_resets_the_breaker(six_day_bundle, tmp_path):
    """breaker_after=2: fail, ok, fail, fail trips on the fourth board; a
    breaker that did not reset on the success would trip on the third."""
    healthy = run_lab(
        _six_day_config(six_day_bundle, tmp_path / "a"),
        windows=UNDER,
        transport=FakeTransport("first"),
        now=T0,
    )
    planned = healthy["boards_shown"]
    assert planned > 4
    policy = RetryPolicy(breaker_after=2)
    attempts = 1 + len(policy.delays_s)
    script: list[Any] = [*([500] * attempts), "ok", *([500] * attempts * 2)]
    clock = FakeClock()
    document = run_lab(
        _six_day_config(six_day_bundle, tmp_path / "b"),
        windows=UNDER,
        transport=ScriptedTransport(script, clock),
        now=T0,
        retry=policy,
        sleep=clock.sleep,
        clock=clock,
    )
    assert [r["ok"] for r in document["receipts"]] == [False, True, False, False]
    assert document["model_failures"] == 3
    assert document["skipped_breaker"] == planned - 4
    assert document["breaker"]["tripped_at"] == healthy["receipts"][3]["snapshot"]


def test_a_run_without_a_policy_never_retries_nor_trips(six_day_bundle, tmp_path):
    clock = FakeClock()
    transport = ScriptedTransport([500], clock)
    document = run_lab(
        _six_day_config(six_day_bundle, tmp_path / "lab"),
        windows=UNDER,
        transport=transport,
        now=T0,
        retry=None,
        sleep=clock.sleep,
        clock=clock,
    )
    assert clock.sleeps == []
    assert document["model_failures"] == document["model_calls"] == len(transport.timeouts)
    assert document["skipped_breaker"] == 0 and document["breaker"] is None

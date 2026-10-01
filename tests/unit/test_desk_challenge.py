"""The end-to-end challenge game on fixtures: bundle discovery, the session
partition, the policy field, hard-cap enforcement, the quota gate, and one
whole round with a fake transport (no network, no keys burned).

The oracle for every scorecard number is the replay module itself, called
independently with the run's own recorded choices — so a score that came from
anywhere but mechanical replay accounting fails here.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_desk_hindsight import multi_day_bundle
from tree_options.desk import challenge, gepa
from tree_options.desk import intraday_action_graph as iag
from tree_options.desk.__main__ import run_cli
from tree_options.desk.challenge import (
    HARD_CHALLENGE_BOARDS,
    HARD_ROUND_BOARDS,
    HARD_ROUND_REFLECTIONS,
    PARTS,
    UNTRUSTED_NOTE,
    BudgetCounter,
    BudgetRefused,
    ChallengeBudget,
    discover_bundles,
    partition_sessions,
    policy_field,
    run_challenge,
)
from tree_options.trex.grant_policy import QuotaWindow

T0 = datetime(2026, 9, 29, 1, 0, tzinfo=UTC)  # 19:00 MDT
DAYS3 = [date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24)]
DAYS6 = [
    date(2026, 9, 17),
    date(2026, 9, 18),
    date(2026, 9, 21),
    date(2026, 9, 22),
    date(2026, 9, 23),
    date(2026, 9, 24),
]
VINTAGE = "20260927-v1"
BUNDLE_NAME = "minute-bars-4mo.json"

UNDER = (
    QuotaWindow(name="zai", actual_left_pct=Decimal("94.0"), planned_left_pct=Decimal("51.9")),
)
ON_PLAN = (
    QuotaWindow(name="zai", actual_left_pct=Decimal("40.0"), planned_left_pct=Decimal("50.0")),
)


class BoardTransport:
    """OpenAI-shaped completions: picks each board's top reward/risk row and
    CLAIMS a capital figure the digest must never adopt as a number."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, url: str, body: bytes, headers: dict[str, str], timeout: float
    ) -> tuple[int, bytes]:
        self.calls.append({"url": url, "body": json.loads(body)})
        prompt = json.loads(json.loads(body)["messages"][0]["content"])
        rows = prompt["board"]
        reply = {
            "choice": rows[0]["id"],
            "note": "top rr",
            "claimed_capital_proxy": "99999.0",
        }  # untrusted model claim
        envelope = {
            "choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}]
        }
        return 200, json.dumps(envelope).encode()


@pytest.fixture(autouse=True)
def _fake_zai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_ZAI", "test-key-material")


def _store(tmp_path: Path, vintages: dict[str, dict[str, Any]]) -> Path:
    """A fixture desk store with one frozen minute-bar bundle per vintage."""
    store = tmp_path / "store"
    for name, doc in vintages.items():
        directory = store / "evaluations" / "intraday-graph" / name
        directory.mkdir(parents=True)
        (directory / BUNDLE_NAME).write_text(json.dumps(doc))
    return store


def _bundle_path(store: Path, vintage: str = VINTAGE) -> Path:
    return store / "evaluations" / "intraday-graph" / vintage / BUNDLE_NAME


# ---------------------------------------------------------------- discovery


def test_discovery_finds_every_frozen_vintage(tmp_path: Path) -> None:
    store = _store(
        tmp_path,
        {"20260920-v1": multi_day_bundle(*DAYS6[:3]), "20260927-v2": multi_day_bundle(*DAYS6[3:])},
    )
    bundles = discover_bundles(store)
    assert [path.parent.name for path in bundles] == ["20260920-v1", "20260927-v2"]
    assert all(path.name.startswith("minute-bars") for path in bundles)
    # a store without the intraday-graph dir (or no store at all) is empty
    assert discover_bundles(tmp_path / "nowhere") == []
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "evaluations").mkdir()
    assert discover_bundles(empty) == []


def test_discovery_picks_the_largest_bundle_per_vintage_not_probe_shards(tmp_path):
    """The first real run (20260928T224926Z) challenged a 14 KB probe shard
    beside the 69 MB vintage: alphabetical-last selection is wrong. The
    LARGEST file per vintage dir is the bundle."""

    store = _store(tmp_path, {VINTAGE: multi_day_bundle(*DAYS3)})
    directory = _bundle_path(store).parent
    real_bytes = _bundle_path(store).read_bytes()
    for shard, blob in [
        ("minute-bars-probe.json", b'{"schema": "desk-option-minute-bars/1", "contracts": {}}'),
        ("minute-bars-cache-only.json", b"{}"),
    ]:
        (directory / shard).write_bytes(blob)
    bundles = discover_bundles(store)
    assert [p.name for p in bundles] == [BUNDLE_NAME], "the real vintage, not shards"
    assert bundles[0].read_bytes() == real_bytes


# --------------------------------------------------------------- partition


def test_partition_is_disjoint_deterministic_and_complete() -> None:
    raw = multi_day_bundle(*DAYS6)
    sessions = sorted(DAYS6)
    first = partition_sessions(raw)
    assert first == partition_sessions(raw, seed=7)  # reproducible across calls
    assert len(first) == PARTS
    flattened = [day for part in first for day in part]
    assert sorted(flattened) == sessions  # every session exactly once
    assert all(part == sorted(part) for part in first)  # each slice ascending
    # the documented rule: the session at index j -> slice (j + seed) % PARTS
    assert first[0] == [sessions[j] for j in range(len(sessions)) if (j + 7) % PARTS == 0]
    assert partition_sessions(raw, seed=8) == [first[2], first[0], first[1]]
    # a single-session bundle: one slice carries it, the others are empty
    assert partition_sessions(multi_day_bundle(DAYS3[0])) == [[], [DAYS3[0]], []]


# ------------------------------------------------------------ policy field


def test_an_empty_archive_fields_the_control_and_the_incumbent(tmp_path: Path) -> None:
    field = policy_field(tmp_path / "lab")
    assert [(entry.policy, entry.kind, entry.prompt) for entry in field] == [
        ("no_trade", "rules", None),
        ("first_row", "rules", None),
        ("model:zai", "model", None),
    ]


def _measured(prompt: str, pnl: int, worst: int) -> dict[str, Any]:
    record = gepa.new_policy(prompt, generation=1, parents=[], created_by="test")
    gepa.fold_run(
        record,
        {
            "at": "x",
            "boards_shown": 2,
            "model_calls": 2,
            "summary": {
                "entered": 1,
                "closed_capital_proxy": str(5000 + pnl),
                "minimum_closed_capital_proxy": str(worst),
            },
        },
    )
    return record


def test_the_field_is_the_archive_front_capped_at_three(tmp_path: Path) -> None:
    lab_root = tmp_path / "lab"
    # pnl rises while the worst minimum falls: none dominates another
    for index in range(4):
        gepa.save_policy(
            lab_root,
            _measured(
                f"Policy number {index} of the fixture field.", 10 * (index + 1), 4990 - index
            ),
        )
    field = policy_field(lab_root)
    archive = gepa.load_archive(lab_root)
    assert len(archive) == 4
    assert len(field) == 2 + 3  # both controls ALWAYS + at most FRONT_MAX=3
    assert field[0] == challenge.PolicyEntry(policy="no_trade", kind="rules")
    assert field[1] == challenge.PolicyEntry(policy="first_row", kind="rules")
    for entry in field[2:]:
        assert entry.policy.startswith("gepa:") and entry.prompt


# ------------------------------------------------------------- hard caps


def test_the_hard_caps_are_not_configurable_around() -> None:
    assert (HARD_ROUND_BOARDS, HARD_ROUND_REFLECTIONS, HARD_CHALLENGE_BOARDS) == (200, 8, 1000)
    for bad in (
        {"boards": HARD_ROUND_BOARDS + 1},
        {"reflections": HARD_ROUND_REFLECTIONS + 1},
        {"total_boards": HARD_CHALLENGE_BOARDS + 1},
        {"boards": 0},
        {"total_boards": 0},
    ):
        with pytest.raises(ValueError):
            ChallengeBudget(**bad)


def test_the_counter_refuses_before_exceeding() -> None:
    counter = BudgetCounter(ChallengeBudget(boards=2, total_boards=3))
    assert (counter.boards_left, counter.total_boards_left) == (2, 3)
    counter.take_boards(1)
    counter.take_boards(1)
    with pytest.raises(BudgetRefused):
        counter.take_boards(1)  # the round cap is gone
    counter.start_round()  # the round resets, the challenge total does not
    assert counter.boards_left == 2 and counter.total_boards_left == 1
    counter.take_boards(1)
    with pytest.raises(BudgetRefused):
        counter.take_boards(1)  # the challenge total is gone
    counter.refund_boards(1)
    counter.take_boards(1)  # the unused reservation came back
    assert counter.boards_used == 3  # only what was really taken, refunds netted
    with pytest.raises(BudgetRefused):
        counter.take_reflections(9)
    counter.take_reflections(8)
    with pytest.raises(BudgetRefused):
        counter.take_reflections(1)


def test_a_plan_that_cannot_fit_refuses_before_any_burn(tmp_path: Path) -> None:
    store = _store(tmp_path, {VINTAGE: multi_day_bundle(*DAYS3)})
    transport = BoardTransport()
    with pytest.raises(BudgetRefused):
        run_challenge(
            store_root=store,
            now=T0,
            lab_root=tmp_path / "lab",
            budget=ChallengeBudget(boards=2, total_boards=2),
            transport=transport,
        )
    assert transport.calls == []
    assert not (store / "evaluations" / "challenge").exists()


# --------------------------------------------------------------------- e2e


def test_one_round_end_to_end(tmp_path: Path) -> None:
    store = _store(tmp_path, {VINTAGE: multi_day_bundle(*DAYS3)})
    transport = BoardTransport()
    document = run_challenge(
        store_root=store, now=T0, lab_root=tmp_path / "lab", transport=transport
    )
    assert document["status"] == "ok"
    assert document["mode"] == "standing_budget"  # no fresh windows snapshot
    # the digest is evidence on disk with the untrusted-prose note UP FRONT
    digest_dir = Path(document["digest_dir"])
    assert digest_dir == (store / "evaluations" / "challenge" / T0.strftime("%Y%m%dT%H%M%SZ"))
    on_disk = json.loads((digest_dir / "digest.json").read_bytes())
    assert on_disk["untrusted_note"] == UNTRUSTED_NOTE
    assert "untrusted prose" in UNTRUSTED_NOTE.lower()
    markdown = (digest_dir / "digest.md").read_text()
    assert markdown.index(UNTRUSTED_NOTE) < markdown.index("## ")
    assert "nothing is promoted" in on_disk["promotion"]["rule"]
    assert on_disk["promotion"]["promoted"] is False
    assert "99999" not in json.dumps(on_disk)  # a model claim is never a number
    assert '"prompt"' not in json.dumps(on_disk)  # no evolved prompt text leaks

    # scorecards: both policies, the honest control entered nothing
    round_entry = document["rounds"][0]
    assert round_entry["vintage"] == VINTAGE
    cards = {card["policy"]: card for card in round_entry["scorecards"]}
    assert set(cards) == {"no_trade", "first_row", "model:zai"}
    control = cards["no_trade"]
    assert (control["entered"], control["closed_pnl_sum"], control["model_calls"]) == (0, "0", 0)
    ranked = [Decimal(card["closed_pnl_sum"]) for card in round_entry["scorecards"]]
    assert ranked == sorted(ranked, reverse=True)

    # ORACLE: the scorecard is exactly what replay accounting says for the
    # policy's own recorded choices, recomputed here from the receipts
    runs_root = digest_dir / "runs"
    model_runs = [
        json.loads((runs_root / name / "summary.json").read_bytes())
        for name in cards["model:zai"]["run_dirs"]
    ]
    raw = json.loads(_bundle_path(store).read_bytes())
    nonempty = [[date.fromisoformat(d) for d in part] for part in round_entry["slices"] if part]
    assert len(model_runs) == len(nonempty) == PARTS
    oracle = Decimal(0)
    for run_document, days in zip(model_runs, nonempty, strict=True):
        assert run_document["sessions"] == [str(d) for d in days]
        decisions = {
            r["snapshot"]: r["choice"] for r in run_document["receipts"] if r.get("choice")
        }
        expected = iag.replay(challenge._slice_bundle(raw, days), days, decisions)
        assert run_document["summary"]["entered"] == expected["entered"]
        assert run_document["summary"]["closed_capital_proxy"] == expected["closed_capital_proxy"]
        oracle += Decimal(expected["closed_capital_proxy"]) - Decimal(5000)
    assert Decimal(cards["model:zai"]["closed_pnl_sum"]) == oracle
    assert oracle != 0  # the fixture must score, or this oracle is vacuous

    # gap samples from the policy's own receipts (or an explicit none)
    assert round_entry["gap_samples"]
    for sample in round_entry["gap_samples"]:
        assert len(sample["snapshots"]) <= 2  # first + middle of the slice
        assert sample["boards"] or sample.get("reason")


def test_a_challenge_with_zero_boards_everywhere_is_not_ok(tmp_path):
    """A probe shard (sessions present, nothing evaluable) must not claim a
    plain ok — the digest says empty_bundles so a rerun investigates instead
    of trusting a nothing-run (the first real run shipped exactly that)."""
    from tests.unit.test_desk_intraday_action_graph import _bundle

    store = _store(tmp_path, {VINTAGE: multi_day_bundle(*DAYS3)})
    transport = BoardTransport()
    # drop one leg of every vertical pair: sessions survive (the remaining
    # ticker still has bars) but no candidate ever has both sides quoted
    # -> zero candidates -> zero boards everywhere
    doc = _bundle(date(2026, 9, 22))
    tickers = sorted(doc["contracts"])
    assert len(tickers) >= 2
    del doc["contracts"][tickers[-1]]
    _bundle_path(store).write_text(json.dumps(doc))
    document = run_challenge(
        store_root=store, now=T0, lab_root=tmp_path / "lab", transport=transport
    )
    assert document["status"] == "empty_bundles"
    assert sum(sc.get("boards", 0) for rnd in document["rounds"] for sc in rnd["scorecards"]) == 0
    assert [p["policy"] for p in document["policies"]] == ["no_trade", "first_row", "model:zai"]
    # zero candidates -> zero boards -> the model is never called: no burn
    assert len(transport.calls) == 0
    assert document["budget"]["boards_used"] <= HARD_ROUND_BOARDS


def test_two_rounds_play_every_bundle_and_rounds_n_the_newest(tmp_path: Path) -> None:
    vintages = {"20260920-v1": multi_day_bundle(*DAYS6[:3]), VINTAGE: multi_day_bundle(*DAYS3)}
    store = _store(tmp_path, vintages)
    both = run_challenge(
        store_root=store, now=T0, lab_root=tmp_path / "lab", transport=BoardTransport()
    )
    assert both["status"] == "ok"
    assert [entry["vintage"] for entry in both["rounds"]] == ["20260920-v1", VINTAGE]
    assert both["budget"]["boards_used"] <= HARD_CHALLENGE_BOARDS
    newest = run_challenge(
        store_root=store, now=T0, lab_root=tmp_path / "lab", rounds=1, transport=BoardTransport()
    )
    assert [entry["vintage"] for entry in newest["rounds"]] == [VINTAGE]
    assert newest["boards_per_run"] <= both["boards_per_run"]
    assert Path(newest["digest_dir"]) != Path(both["digest_dir"])  # never shared


def test_the_archive_front_plays_and_the_digest_snapshots_stats_only(tmp_path: Path) -> None:
    store = _store(tmp_path, {VINTAGE: multi_day_bundle(*DAYS3)})
    lab_root = tmp_path / "lab"
    champion = _measured("Enter only fresh, wide-premium boards.", 30, 4990)
    gepa.save_policy(lab_root, champion)
    document = run_challenge(
        store_root=store, now=T0, lab_root=lab_root, transport=BoardTransport()
    )
    assert document["status"] == "ok"
    assert [p["policy"] for p in document["policies"]] == ["no_trade", "first_row", f"gepa:{champion['id']}"]
    cards = {card["policy"]: card for card in document["rounds"][0]["scorecards"]}
    assert cards[f"gepa:{champion['id']}"]["model_calls"] >= 1
    # the archive snapshot AFTER the challenge: ids + mechanical stats only
    assert document["archive_after"] == [
        {"id": champion["id"], "generation": champion["generation"], "stats": champion["stats"]}
    ]
    assert "prompt" not in json.dumps(document["archive_after"])
    text = (Path(document["digest_dir"]) / "digest.json").read_text()
    assert champion["prompt"] not in text  # no prompt text in the digest


# --------------------------------------------------------------- the gate


def test_fresh_on_plan_windows_skip_model_policies_but_rules_score(tmp_path: Path) -> None:
    store = _store(tmp_path, {VINTAGE: multi_day_bundle(*DAYS3)})
    transport = BoardTransport()
    document = run_challenge(
        store_root=store,
        now=T0,
        lab_root=tmp_path / "lab",
        windows=ON_PLAN,
        windows_age_s=60.0,
        transport=transport,
    )
    assert (document["status"], document["mode"]) == ("quota_dry", "under_using_gate")
    assert document["windows"]["fresh"] is True
    assert transport.calls == []  # nothing burned: the gate fired first
    assert document["budget"]["boards_used"] == 0
    round_entry = document["rounds"][0]
    assert round_entry["skipped"] == [{"policy": "model:zai", "reason": "quota_dry"}]
    cards = {card["policy"] for card in round_entry["scorecards"]}
    assert cards == {"no_trade", "first_row"}  # the rules controls still scored
    assert round_entry["gap_samples"] == []  # no model receipts to measure
    markdown = (Path(document["digest_dir"]) / "digest.md").read_text()
    assert "skipped model:zai: quota_dry" in markdown


def test_fresh_under_using_windows_burn_through_the_gate(tmp_path: Path) -> None:
    store = _store(tmp_path, {VINTAGE: multi_day_bundle(*DAYS3)})
    transport = BoardTransport()
    document = run_challenge(
        store_root=store,
        now=T0,
        lab_root=tmp_path / "lab",
        windows=UNDER,
        windows_age_s=60.0,
        transport=transport,
    )
    assert (document["status"], document["mode"]) == ("ok", "under_using_gate")
    assert document["windows"]["under_using"] == ["zai"]
    assert document["budget"]["boards_used"] == len(transport.calls) >= 1


def test_stale_windows_fall_back_to_the_standing_budget(tmp_path: Path) -> None:
    store = _store(tmp_path, {VINTAGE: multi_day_bundle(*DAYS3)})
    transport = BoardTransport()
    document = run_challenge(
        store_root=store,
        now=T0,
        lab_root=tmp_path / "lab",
        windows=ON_PLAN,
        windows_age_s=7 * 3600.0,
        transport=transport,
    )
    assert (document["status"], document["mode"]) == ("ok", "standing_budget")
    assert document["budget"]["boards_used"] >= 1  # the caps bound this burn


# ---------------------------------------------------------------- dry run


def test_dry_run_computes_the_plan_and_writes_nothing(tmp_path: Path) -> None:
    store = _store(
        tmp_path, {"20260920-v1": multi_day_bundle(*DAYS6[:3]), VINTAGE: multi_day_bundle(*DAYS3)}
    )
    transport = BoardTransport()
    document = run_challenge(
        store_root=store, now=T0, lab_root=tmp_path / "lab", dry_run=True, transport=transport
    )
    assert document["status"] == "dry_run"
    assert document["mode"] == "standing_budget"
    assert document["boards_per_run"] == min(HARD_ROUND_BOARDS, HARD_CHALLENGE_BOARDS // 2) // PARTS
    assert [entry["vintage"] for entry in document["rounds"]] == ["20260920-v1", VINTAGE]
    assert all(entry["slices"] for entry in document["rounds"])
    assert [p["policy"] for p in document["policies"]] == ["no_trade", "first_row", "model:zai"]
    assert transport.calls == []
    assert "digest_dir" not in document
    assert not (store / "evaluations" / "challenge").exists()


def test_an_empty_store_is_reported_not_run(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    document = run_challenge(store_root=store, now=T0, lab_root=tmp_path / "lab")
    assert (document["status"], document["bundles"]) == ("no_bundles", [])
    assert "digest_dir" not in document


# -------------------------------------------------------------------- CLI


def test_the_cli_registers_beside_lab_overnight_and_plans_a_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TREX_DESK_STATE", str(tmp_path / "state"))
    store = _store(tmp_path, {VINTAGE: multi_day_bundle(*DAYS3)})
    rc = run_cli(
        [
            "challenge",
            "run",
            "--bundles-from",
            str(store),
            "--lab-root",
            str(tmp_path / "lab"),
            "--rounds",
            "1",
            "--dry-run",
        ]
    )
    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "dry_run"
    assert printed["bundles"] == [BUNDLE_NAME]
    assert not (store / "evaluations" / "challenge").exists()


def test_the_cli_refuses_a_bad_plan_with_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TREX_DESK_STATE", str(tmp_path / "state"))
    store = _store(tmp_path, {VINTAGE: multi_day_bundle(*DAYS3)})
    assert (
        run_cli(
            [
                "challenge",
                "run",
                "--bundles-from",
                str(store),
                "--lab-root",
                str(tmp_path / "lab"),
                "--rounds",
                "0",
            ]
        )
        == 2
    )
    assert "refused" in capsys.readouterr().err
    assert not (store / "evaluations" / "challenge").exists()

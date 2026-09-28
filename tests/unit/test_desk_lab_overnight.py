"""The overnight engine end-to-end on fixtures: fake transports for BOTH
providers, hard-cap enforcement, the gating modes, disjoint held-out slices,
archive updates from mechanical summaries only, and the nightly digest."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_desk_hindsight import multi_day_bundle
from tree_options.desk import gepa
from tree_options.desk.lab_overnight import (
    HARD_BOARD_CALLS,
    HARD_REFLECTION_CALLS,
    UNTRUSTED_NOTE,
    BudgetCounter,
    BudgetRefused,
    OvernightBudget,
    run_overnight,
)
from tree_options.trex.clock import ET
from tree_options.trex.grant_policy import QuotaWindow

T0 = datetime(2026, 9, 29, 4, 17, tzinfo=UTC)  # 22:17 MDT = 04:17 UTC next day
DAYS3 = [date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24)]
DAYS5 = [date(2026, 9, 18), date(2026, 9, 21), date(2026, 9, 22),
         date(2026, 9, 23), date(2026, 9, 24)]

UNDER = (QuotaWindow(name="zai", actual_left_pct=Decimal("94.0"),
                     planned_left_pct=Decimal("51.9")),)
ON_PLAN = (QuotaWindow(name="zai", actual_left_pct=Decimal("40.0"),
                       planned_left_pct=Decimal("50.0")),)

ZAI_URL = "https://api.z.ai/api/coding/paas/v4/chat/completions"
MM_URL = "https://api.minimax.io/v1/chat/completions"


class NightTransport:
    """Serves board prompts (a fixed choice) and reflection prompts."""

    def __init__(self, reflection: dict[str, Any] | None = None) -> None:
        self.reflection = reflection if reflection is not None else {
            "diagnosis": "The policy skips boards where the premium moved.",
            "revised_prompt": "Enter only when the premium is fresh and wide.",
            "variants": ["Skip after two consecutive losses."]}
        self.fail_reflections = False
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, body: bytes, headers: dict[str, str],
                 timeout: float) -> tuple[int, bytes]:
        self.calls.append({"url": url, "body": json.loads(body)})
        payload = json.loads(json.loads(body)["messages"][0]["content"])
        if "gap_boards" in payload:
            if self.fail_reflections:
                return 500, b""
            reply: dict[str, Any] = dict(self.reflection)
        else:
            reply = {"choice": payload["board"][0]["id"], "note": "top rr"}
        envelope = {"choices": [{"message": {"content": json.dumps(reply)},
                                 "finish_reason": "stop"}]}
        return 200, json.dumps(envelope).encode()

    @property
    def board_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls
                if "gap_boards" not in json.loads(
                    c["body"]["messages"][0]["content"])]

    @property
    def reflection_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls
                if "gap_boards" in json.loads(
                    c["body"]["messages"][0]["content"])]


@pytest.fixture(autouse=True)
def _fake_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_ZAI", "test-key-material")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_MINIMAX2", "test-key-material-2")


@pytest.fixture()
def bundle3(tmp_path: Path) -> Path:
    path = tmp_path / "bundle3.json"
    path.write_text(json.dumps(multi_day_bundle(*DAYS3)))
    return path


@pytest.fixture()
def bundle5(tmp_path: Path) -> Path:
    path = tmp_path / "bundle5.json"
    path.write_text(json.dumps(multi_day_bundle(*DAYS5)))
    return path


def _night(bundle: Path, tmp_path: Path, **kwargs: Any) -> dict[str, Any]:
    zai = kwargs.pop("zai", NightTransport())
    minimax = kwargs.pop("minimax", NightTransport())
    document = run_overnight(bundle=bundle, now=kwargs.pop("now", T0),
                             budget=kwargs.pop("budget", OvernightBudget()),
                             lab_root=kwargs.pop("lab_root", tmp_path / "lab"),
                             transports={"zai": zai, "minimax-flash": minimax},
                             **kwargs)
    document["_zai"], document["_minimax"] = zai, minimax
    return document


def _run_document(entry: dict[str, Any]) -> dict[str, Any]:
    return json.loads(
        Path(str(entry["run"]["run_dir"]), "summary.json").read_bytes())


# ------------------------------------------------------------------- e2e


def test_one_night_end_to_end(bundle3: Path, tmp_path: Path) -> None:
    document = _night(bundle3, tmp_path)
    zai, minimax = document["_zai"], document["_minimax"]
    assert document["status"] == "ok"
    assert document["mode"] == "standing_budget"  # no fresh windows snapshot
    assert document["seeded"] is True  # the empty archive was seeded
    assert document["budget"]["reflections_used"] == 2
    assert document["budget"]["boards_used"] == len(zai.board_calls)
    assert document["budget"]["boards_used"] <= HARD_BOARD_CALLS

    # one reflection per provider: both flash tiers burned
    assert len(zai.reflection_calls) == 1
    assert len(minimax.reflection_calls) == 1
    assert minimax.calls[0]["url"] == MM_URL
    assert minimax.calls[0]["body"]["model"] == "MiniMax-M3.1-Flash-Preview"
    # every board call went to the flash volume lane
    assert zai.board_calls
    assert all(call["url"] == ZAI_URL for call in zai.board_calls)

    # the seed champion ran, its stats came from the MECHANICAL summary
    champion = document["champions"][0]
    assert champion["generation"] == 0
    assert champion["stats_before"]["runs"] == 0
    assert champion["stats_after"]["runs"] == 1
    archive = gepa.load_archive(tmp_path / "lab")
    seed = next(p for p in archive if p["id"] == champion["id"])
    assert seed["stats"]["closed_pnl_sum"] == str(
        Decimal(champion["run"]["closed_capital_proxy"]) - Decimal(5000))
    assert seed["stats"]["entered"] == champion["run"]["entered"]

    # gaps were computed mechanically for the champion
    assert champion["gap_totals"]["boards"] >= 1
    assert champion["top_gaps"]

    # proposals became candidates and were evaluated on DISJOINT boards
    assert document["candidates"]
    champion_snaps = {r["snapshot"] for r in _run_document(champion)["receipts"]}
    seen: set[str] = set()
    for entry in document["candidates"]:
        if not entry["run"]:
            assert entry["held_out_boards"] == 0
            continue
        snaps = {r["snapshot"] for r in _run_document(entry)["receipts"]}
        assert snaps.isdisjoint(champion_snaps)
        assert snaps.isdisjoint(seen)
        seen |= snaps
    assert seen, "at least one candidate was scored on held-out boards"

    # archive: candidates saved with lineage, pareto front rebuilt
    for entry in document["candidates"]:
        record = next(p for p in archive if p["id"] == entry["id"])
        assert record["parents"] == [champion["id"]]
        assert record["generation"] == 1
        assert record["created_by"].startswith("reflect:")
    assert document["pareto_front_after"]

    # the night's sessions are marked used
    assert set(gepa.load_state(tmp_path / "lab")["used_sessions"]) == set(
        document["sessions"])

    # the digest exists and says exactly what it is
    night = T0.astimezone(ET).date().isoformat()
    digest_dir = tmp_path / "lab" / "overnight" / night
    assert document["digest_dir"] == str(digest_dir)
    on_disk = json.loads((digest_dir / "digest.json").read_bytes())
    assert on_disk["untrusted_note"] == UNTRUSTED_NOTE
    assert "untrusted prose" in UNTRUSTED_NOTE.lower()
    markdown = (digest_dir / "digest.md").read_text()
    assert UNTRUSTED_NOTE in markdown
    assert "Nothing in this digest is promoted" in markdown
    # the model's diagnosis is quoted verbatim as model output
    assert "> The policy skips boards where the premium moved." in markdown


def test_a_failed_reflection_is_recorded_and_the_night_continues(
        bundle3: Path, tmp_path: Path) -> None:
    minimax = NightTransport()
    minimax.fail_reflections = True
    document = _night(bundle3, tmp_path, minimax=minimax)
    assert document["status"] == "ok"
    failed = next(r for r in document["reflections"]
                  if r["provider"] == "minimax-flash")
    assert failed["status"] == "failed"
    assert "HTTP 500" in failed["error"]
    healthy = next(r for r in document["reflections"] if r["provider"] == "zai")
    assert healthy["status"] == "ok"
    # candidates still came from the healthy reflection
    assert any(c["created_by"] == "reflect:zai" for c in document["candidates"])
    assert all(c["created_by"] != "reflect:minimax-flash"
               for c in document["candidates"])


def test_second_invocation_the_same_night_is_a_noop(bundle3: Path,
                                                    tmp_path: Path) -> None:
    first = _night(bundle3, tmp_path)
    zai, minimax = first["_zai"], first["_minimax"]
    burned = len(zai.calls) + len(minimax.calls)
    again = _night(bundle3, tmp_path, zai=zai, minimax=minimax)
    assert again["status"] == "already_done"
    assert len(zai.calls) + len(minimax.calls) == burned


def test_nights_rotate_to_not_yet_used_sessions(bundle5: Path,
                                                tmp_path: Path) -> None:
    first = _night(bundle5, tmp_path)
    second = _night(bundle5, tmp_path, now=T0 + timedelta(hours=24))
    assert first["status"] == "ok" and second["status"] == "ok"
    assert set(first["sessions"]).isdisjoint(second["sessions"])
    used = set(gepa.load_state(tmp_path / "lab")["used_sessions"])
    assert used == set(first["sessions"]) | set(second["sessions"])


# ------------------------------------------------------------- hard caps


def test_a_night_that_cannot_fit_the_reflections_refuses_before_any_burn(
        bundle3: Path, tmp_path: Path) -> None:
    zai, minimax = NightTransport(), NightTransport()
    with pytest.raises(BudgetRefused):
        run_overnight(bundle=bundle3, now=T0,
                      budget=OvernightBudget(boards=6, reflections=1),
                      lab_root=tmp_path / "lab",
                      transports={"zai": zai, "minimax-flash": minimax})
    assert zai.calls == [] and minimax.calls == []


def test_the_hard_caps_are_not_configurable_around() -> None:
    with pytest.raises(ValueError):
        OvernightBudget(boards=HARD_BOARD_CALLS + 1)
    with pytest.raises(ValueError):
        OvernightBudget(reflections=HARD_REFLECTION_CALLS + 1)
    with pytest.raises(ValueError):
        OvernightBudget(boards=0)
    assert HARD_BOARD_CALLS == 200 and HARD_REFLECTION_CALLS == 8


def test_the_counter_refuses_before_exceeding() -> None:
    counter = BudgetCounter(OvernightBudget(boards=2, reflections=8))
    counter.take_boards(1)
    counter.take_boards(1)
    with pytest.raises(BudgetRefused):
        counter.take_boards(1)
    assert counter.boards_left == 0
    counter.take_reflections(8)
    with pytest.raises(BudgetRefused):
        counter.take_reflections(1)


def test_a_tiny_board_budget_never_exceeds_one_call_per_board(
        bundle3: Path, tmp_path: Path) -> None:
    document = _night(bundle3, tmp_path, budget=OvernightBudget(boards=1))
    zai = document["_zai"]
    assert document["status"] == "ok"
    assert document["budget"]["boards_used"] == 1
    assert len(zai.board_calls) == 1
    # nothing else fit: candidates exist as proposals but were not scored
    assert all(entry["run"] is None for entry in document["candidates"])


# ------------------------------------------------------------- the gate


def test_fresh_on_plan_windows_skip_the_burn(bundle3: Path,
                                             tmp_path: Path) -> None:
    zai, minimax = NightTransport(), NightTransport()
    document = run_overnight(bundle=bundle3, now=T0, windows=ON_PLAN,
                             budget=OvernightBudget(),
                             lab_root=tmp_path / "lab",
                             transports={"zai": zai, "minimax-flash": minimax},
                             windows_age_s=60.0)
    assert (document["status"], document["mode"]) == ("skipped",
                                                      "under_using_gate")
    assert zai.calls == [] and minimax.calls == []
    assert not (tmp_path / "lab" / "overnight").exists()


def test_fresh_under_using_windows_burn_through_the_gate(
        bundle3: Path, tmp_path: Path) -> None:
    document = _night(bundle3, tmp_path, windows=UNDER, windows_age_s=60.0)
    assert document["status"] == "ok"
    assert document["mode"] == "under_using_gate"
    assert document["budget"]["boards_used"] >= 1


def test_stale_windows_fall_back_to_the_standing_budget(
        bundle3: Path, tmp_path: Path) -> None:
    document = _night(bundle3, tmp_path, windows=ON_PLAN,
                      windows_age_s=7 * 3600.0)
    assert document["status"] == "ok"
    assert document["mode"] == "standing_budget"

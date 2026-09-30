"""E6 shadow previews: desk-parseable request files, never the live inbox."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from tree_options.desk import enter_supervised
from tree_options.trex.plan import LegStructure
from tree_options.trex.supervised_desk import EntryRequest


def run(world, **kwargs):
    return enter_supervised.write_previews(
        now=kwargs.pop("now", world.now),
        cal=world.cal,
        database=world.db,
        queue_dir=kwargs.pop("queue_dir", world.queues),
        run_dir=kwargs.pop("run_dir", world.root / "execution"),
        **kwargs,
    )


def test_an_admissible_deal_becomes_a_desk_parseable_preview(world):
    world.queue_file()
    doc = run(world)
    assert doc["status"] == "ok" and len(doc["written"]) == 1
    deal_id = doc["written"][0]
    previews = world.root / "execution" / "requests-preview"
    request = EntryRequest.model_validate(  # the desk's own parser
        json.loads((previews / f"{deal_id}.json").read_text())
    )
    effect = request.effect
    assert effect.intent_id == deal_id == effect.structure.id
    assert request.strategy_version == "desk-row/R1"
    assert request.requested_by == "desk-enter-shadow"
    assert effect.side == "BUY"
    assert str(effect.limit) == str(world.queue["admissible"][0]["fill"])
    assert effect.order_ref == f"trex:sup:{deal_id}"
    assert request.send_deadline is not None


def test_the_live_inbox_is_never_touched(world):
    world.queue_file()
    run(world)
    inbox = world.root / "execution" / "requests"
    assert not inbox.exists() or list(inbox.iterdir()) == []


def test_previews_cannot_pass_any_canary_mandate(world):
    """strategy_version desk-row/* is out of every mandate's scope."""
    world.queue_file()
    doc = run(world)
    assert doc["decisions"][0]["row"] == "R1"
    request = json.loads(
        (world.root / "execution" / "requests-preview" / f"{doc['written'][0]}.json").read_text()
    )
    assert request["strategy_version"] == "desk-row/R1" != "operational-canary/1"


def test_operator_flags_stop_previews(world):
    world.queue_file()
    (world.root / "execution").mkdir(parents=True, exist_ok=True)
    (world.root / "execution" / "HALT").touch()
    doc = run(world)
    assert doc["status"] == "halted" and doc["written"] == []
    assert not (world.root / "execution" / "requests-preview").exists()


def test_off_window_and_missing_queue_are_statuses_not_errors(world):
    assert run(world, now=world.now.replace(hour=12))["status"] == "outside_entry_window"
    assert run(world)["status"] == "queue_not_ready"


def test_dry_run_writes_no_files_but_records_evidence(world):
    world.queue_file()
    doc = run(world, dry_run=True)
    assert doc["written"] == [world.queue["admissible"][0]["deal_id"]]
    assert not (world.root / "execution" / "requests-preview").exists()


def test_a_credit_kind_writes_a_blocked_diagnostic_never_a_request():
    spec = LegStructure(
        id="d-credit",
        underlying="AAPL",
        kind="credit_vertical",
        legs=[
            {"right": "P", "action": "SELL", "strike": "100", "expiry": date(2025, 4, 28)},
            {"right": "P", "action": "BUY", "strike": "95", "expiry": date(2025, 4, 28)},
        ],
        quantity=1,
        entry_date=date(2025, 3, 4),
        exit_deadline=date(2025, 3, 12),
        limit="1.00",
        exits={"touch": False, "breach": True},
    )
    deal = SimpleNamespace(deal_id="d-credit", spec=spec, fill=spec.limit, raw={"row": "R2"})
    doc = enter_supervised._preview_doc(deal, date(2025, 3, 4), world_now(), "DUT143714")
    assert doc["schema"] == "supervised-preview-blocked/1"
    assert doc["reason"] == "credit_open_not_supported_v1"
    with pytest.raises(ValueError):
        EntryRequest.model_validate(doc)  # a diagnostic is never a request


def world_now():
    from datetime import datetime

    from tree_options.trex.clock import ET

    return datetime(2025, 3, 4, 10, 0, tzinfo=ET)


def test_advice_from_the_lab_annotates_the_result(world, tmp_path, monkeypatch):
    """Three lab runs qualify a policy; the annotation is never promoted."""
    import tree_options.desk.lab as lab_module

    lab = tmp_path / "lab"
    for i in range(3):
        run_dir = lab / f"20260928T00000{i}Z-model-zai"
        run_dir.mkdir(parents=True)
        (run_dir / "summary.json").write_text(
            json.dumps(
                {
                    "policy": "model:zai",
                    "boards_shown": 12,
                    "model_calls": 12,
                    "model_failures": 0,
                    "summary": {
                        "entered": 10,
                        "modeled_wins": 6,
                        "modeled_losses": 4,
                        "closed_capital_proxy": "5100",
                        "minimum_closed_capital_proxy": "5050",
                    },
                }
            )
        )
    monkeypatch.setattr(lab_module, "default_root", lambda: lab)
    world.queue_file()
    doc = run(world)
    advice = doc["advisory"]
    assert advice is not None and advice["policy"] == "model:zai"
    assert advice["promoted"] is False
    assert advice["stats"]["runs"] == 3
    assert doc["decisions"][0]["advice"] == advice
    from tree_options.desk.evidence import EvidenceStore

    with EvidenceStore(world.db, readonly=True) as store:
        records = store.all("supervised_preview")
    assert records and records[0]["advice"] == advice

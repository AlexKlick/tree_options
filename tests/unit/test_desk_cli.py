"""The agent CLI: request composition, status dump, kill files, event tail.

Oracles are the desk's own schemas (the request must parse as the exact
EntryRequest the desk consumes) and the on-disk files — never CLI internals.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tree_options.trex.desk_cli import _cli, collect_status, compose_request
from tree_options.trex.desk_runtime import DeskPaths
from tree_options.trex.supervised import SupervisedPaths
from tree_options.trex.supervised_desk import EntryRequest

T0 = datetime(2026, 9, 28, 15, 0, tzinfo=UTC)
ACCOUNT = "DUT143714"


def _compose(**overrides):
    kwargs = dict(
        intent_id="canary-x",
        account_id=ACCOUNT,
        underlying="SPY",
        buy_strike=Decimal("744"),
        sell_strike=Decimal("742"),
        expiry=date(2026, 11, 20),
        debit=Decimal("0.90"),
        cap=Decimal("1.20"),
        entry_date=date(2026, 9, 29),
        exit_deadline=date(2026, 10, 23),
        tp_frac=Decimal("0.5"),
        send_deadline=T0,
        requested_by="test",
    )
    kwargs.update(overrides)
    return compose_request(**kwargs)


def test_composed_request_is_exactly_what_the_desk_consumes():
    request = _compose()
    assert isinstance(request, EntryRequest)
    effect = request.effect
    assert (effect.side, effect.quantity, effect.limit) == ("BUY", 1, Decimal("0.90"))
    assert effect.order_ref == "trex:sup:canary-x"
    legs = effect.structure.legs
    assert (legs[0].action, legs[0].strike) == ("BUY", Decimal("744"))
    assert (legs[1].action, legs[1].strike) == ("SELL", Decimal("742"))
    # the operator ruling of 2026-09-29: the DEFAULT take-profit basis is
    # gain_frac — 0.5 means +50% of the debit paid, not half the width
    tp = effect.structure.exits.take_profit
    assert tp is not None and (tp.basis, tp.value) == ("gain_frac", Decimal("0.5"))


def test_take_profit_basis_is_explicit_and_honored():
    tp = _compose(tp_basis="width_frac").effect.structure.exits.take_profit
    assert tp is not None and (tp.basis, tp.value) == ("width_frac", Decimal("0.5"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"debit": Decimal("1.50")},  # above the cap: refused
        {"tp_frac": Decimal("1.5"), "tp_basis": "width_frac"},  # width bound 0..1
        {"tp_frac": Decimal("0")},  # gain_frac needs value > 0
        {"tp_basis": "nope"},  # unknown basis refuses (not silently widened)
        {"sell_strike": Decimal("745")},  # not a debit vertical by strikes
    ],
)
def test_invalid_parameters_refuse_before_any_file(overrides, tmp_path):
    with pytest.raises(ValueError):
        _compose(**overrides)


def test_cli_writes_a_request_the_desk_will_accept(tmp_path, monkeypatch):
    monkeypatch.setenv("TREX_SUPERVISED_DIR", str(tmp_path / "supervised"))
    rc = _cli(
        [
            "--dir",
            str(tmp_path / "desk"),
            "request",
            "--buy-strike",
            "744",
            "--sell-strike",
            "742",
            "--expiry",
            "2026-11-20",
            "--debit",
            "0.90",
            "--cap",
            "1.20",
            "--exit-deadline",
            "2026-10-23",
            "--intent-id",
            "canary-t1",
        ]
    )
    assert rc == 0
    raw = json.loads((tmp_path / "desk" / "requests" / "canary-t1.json").read_text())
    request = EntryRequest.model_validate(raw)  # the desk's own parser
    assert request.effect.structure.id == "canary-t1"
    assert raw["effect"]["order_ref"] == "trex:sup:canary-t1"


def test_cli_refuses_a_bad_request_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TREX_SUPERVISED_DIR", str(tmp_path / "supervised"))
    rc = _cli(
        [
            "--dir",
            str(tmp_path / "desk"),
            "request",
            "--buy-strike",
            "744",
            "--sell-strike",
            "742",
            "--expiry",
            "2026-11-20",
            "--debit",
            "9.00",
            "--exit-deadline",
            "2026-10-23",
        ]
    )
    assert rc == 2
    assert "refused" in capsys.readouterr().err
    assert not (tmp_path / "desk" / "requests").exists() or not list(
        (tmp_path / "desk" / "requests").glob("*.json")
    )


def test_kill_file_round_trip(tmp_path):
    rc = _cli(["--dir", str(tmp_path), "halt"])
    assert rc == 0 and (tmp_path / "HALT").exists()
    rc = _cli(["--dir", str(tmp_path), "flatten"])
    assert rc == 0 and (tmp_path / "FLATTEN").exists()
    rc = _cli(["--dir", str(tmp_path), "resume"])
    assert rc == 0 and not (tmp_path / "HALT").exists()
    assert not (tmp_path / "FLATTEN").exists()


def test_cli_defaults_write_a_gain_frac_take_profit(tmp_path, monkeypatch):
    monkeypatch.setenv("TREX_SUPERVISED_DIR", str(tmp_path / "supervised"))
    rc = _cli(
        [
            "--dir",
            str(tmp_path / "desk"),
            "request",
            "--buy-strike",
            "744",
            "--sell-strike",
            "742",
            "--expiry",
            "2026-11-20",
            "--debit",
            "0.90",
            "--exit-deadline",
            "2026-10-23",
            "--intent-id",
            "canary-tp",
        ]
    )
    assert rc == 0
    raw = json.loads((tmp_path / "desk" / "requests" / "canary-tp.json").read_text())
    request = EntryRequest.model_validate(raw)
    tp = request.effect.structure.exits.take_profit
    assert tp is not None and tp.basis == "gain_frac"


def test_cli_refuses_an_unknown_take_profit_basis(tmp_path, monkeypatch):
    monkeypatch.setenv("TREX_SUPERVISED_DIR", str(tmp_path / "supervised"))
    with pytest.raises(SystemExit) as caught:
        _cli(
            [
                "--dir",
                str(tmp_path / "desk"),
                "request",
                "--buy-strike",
                "744",
                "--sell-strike",
                "742",
                "--expiry",
                "2026-11-20",
                "--debit",
                "0.90",
                "--exit-deadline",
                "2026-10-23",
                "--tp-basis",
                "half_width",
            ]
        )
    assert caught.value.code == 2
    assert not (tmp_path / "desk" / "requests").exists()


def test_flatten_under_halt_warns_that_exits_are_gated(tmp_path, capsys):
    """The footgun, out loud: HALT gates the close lane too, so a FLATTEN
    placed beside it closes nothing until the operator clears the HALT."""
    (tmp_path / "HALT").touch()
    rc = _cli(["--dir", str(tmp_path), "flatten"])
    assert rc == 0 and (tmp_path / "FLATTEN").exists()
    err = capsys.readouterr().err
    assert "WARNING" in err and "HALT" in err and "resume" in err


def test_flatten_without_halt_stays_quiet(tmp_path, capsys):
    rc = _cli(["--dir", str(tmp_path), "flatten"])
    assert rc == 0 and (tmp_path / "FLATTEN").exists()
    assert "WARNING" not in capsys.readouterr().err


def test_status_reads_the_desk_state(tmp_path):
    paths = DeskPaths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    (paths.root / "owner.json").write_text(
        json.dumps({"owner_epoch": "desk83-x", "client_id": 83, "pid": 1, "started_at": "t"})
    )
    (paths.root / "requests").mkdir()
    (paths.root / "requests" / "canary-t1.result.json").write_text(
        json.dumps({"status": "sent", "intent_id": "canary-t1"})
    )
    paths.book().write_text(
        json.dumps(
            {
                "heartbeat": "t",
                "structures": {
                    "canary-t1": {"status": "open", "filled_qty": 1, "exit_filled_qty": 0}
                },
            }
        )
    )
    paths.events().write_text(json.dumps({"event": "entry_filled"}) + "\n")
    paths.halt().touch()
    supervised = SupervisedPaths(tmp_path / "supervised")
    supervised.prepare()

    report = collect_status(paths, supervised, now=T0)
    assert report["owner"]["owner_epoch"] == "desk83-x"
    assert report["book"]["canary-t1"]["open_qty"] == 1
    assert report["last_results"][0]["status"] == "sent"
    assert report["supervised"]["mandate"]["state"] == "absent"
    assert report["events"][-1]["event"] == "entry_filled"
    assert report["kill_files"] == ["HALT"], "an agent must see stop states"

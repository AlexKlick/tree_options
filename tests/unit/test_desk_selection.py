"""The deal miner's sealed selection rules (``data/desk/miner/v1.toml``).

The playbook has no selection thresholds, so the rule that turns valued
deals into the entry queue is its own sealed, versioned file (the
playbook's seal mechanism: file bytes + sidecar + one SEALS.md row + the
digest pinned in code). v1 is PROPOSED pending an operator ruling. Oracles
are literal values and hand-computed ratios.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tree_options.desk import pricing, selection

REPO = Path(__file__).resolve().parents[2]
MINER_DIR = REPO / "data" / "desk" / "miner"
D = Decimal


# ------------------------------------------------------------ the seal


def test_the_active_version_is_v1_proposed_with_the_lane_defaults() -> None:
    cfg = selection.load_config()
    assert cfg.version == "v1" and cfg.status == "PROPOSED"
    raw = (MINER_DIR / "v1.toml").read_bytes()
    assert cfg.sha256 == hashlib.sha256(raw).hexdigest() == selection.APPROVED["v1.toml"]
    assert cfg.n_paths == 20_000
    assert cfg.fill_k == D("0.5") and cfg.stress_fill_k == D("1.0")
    assert cfg.max_valued_per_name_row == 400
    assert cfg.selection.min_stress_fill_ev_usd == D("0")
    assert cfg.selection.min_ev_per_max_loss == D("0.05")
    assert "operator ruling" in cfg.ruling.lower()


def test_default_path_follows_the_env_override(tmp_path: Path, monkeypatch) -> None:
    shutil.copytree(MINER_DIR, tmp_path / "m")
    monkeypatch.setenv("DESK_MINER_DIR", str(tmp_path / "m"))
    assert selection.load_config().sha256 == selection.APPROVED["v1.toml"]
    (tmp_path / "m" / "v1.toml").write_bytes(b"# gone\n")
    with pytest.raises(selection.MinerSealError):
        selection.load_config()


def _copy(tmp_path: Path) -> Path:
    d = tmp_path / "miner"
    shutil.copytree(MINER_DIR, d)
    return d


def test_an_edited_file_is_refused(tmp_path: Path) -> None:
    d = _copy(tmp_path)
    p = d / "v1.toml"
    p.write_bytes(
        p.read_bytes().replace(b'min_ev_per_max_loss = "0.05"', b'min_ev_per_max_loss = "0.01"')
    )
    with pytest.raises(selection.MinerSealError, match="sealed"):
        selection.load_config(p)


def test_a_coordinated_edit_of_file_sidecar_and_log_still_fails(tmp_path: Path) -> None:
    d = _copy(tmp_path)
    p = d / "v1.toml"
    data = p.read_bytes().replace(b'min_ev_per_max_loss = "0.05"', b'min_ev_per_max_loss = "0.01"')
    p.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    (d / "v1.sha256").write_text(f"{sha}  v1.toml\n")
    seals = (d / "SEALS.md").read_text()
    (d / "SEALS.md").write_text(seals.replace(selection.APPROVED["v1.toml"], sha))
    with pytest.raises(selection.MinerSealError, match="approved"):
        selection.load_config(p)


def test_a_second_seal_row_is_refused(tmp_path: Path) -> None:
    d = _copy(tmp_path)
    sha = selection.APPROVED["v1.toml"]
    with open(d / "SEALS.md", "a") as fh:
        fh.write(f"| 2026-09-25T00:00:00Z | v1.toml | {sha} | x | again |\n")
    with pytest.raises(selection.MinerSealError, match="rows"):
        selection.load_config(d / "v1.toml")


def test_no_sidecar_is_refused(tmp_path: Path) -> None:
    d = _copy(tmp_path)
    (d / "v1.sha256").unlink()
    with pytest.raises(selection.MinerSealError, match="sidecar"):
        selection.load_config(d / "v1.toml")


# ------------------------------------------------------------ the parse


def _doc(**over: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "schema": "desk-miner/1",
        "version": "v9",
        "written": "2026-09-24",
        "status": "PROPOSED",
        "ruling": "pending an operator ruling",
        "plan": "plan D6",
        "valuation": {
            "n_paths": 20000,
            "fill_k": "0.5",
            "stress_fill_k": "1.0",
            "max_valued_per_name_row": 400,
        },
        "limit": {"policy": "first_cent_beyond_base_fill", "on_limit_not_ok": "refuse"},
        "selection": {
            "rails": "all_pass",
            "decision_ev": "signal_ev_on_signal_rows_else_no_view_ev",
            "min_stress_fill_ev_usd": "0",
            "min_ev_per_max_loss": "0.05",
            "rank": "ev_per_max_loss_desc",
            "tie_breaks": ["ev_desc", "max_loss_asc", "deal_id_asc"],
            "joint": "greedy_rails_recheck",
        },
    }
    doc.update(over)
    return doc


def test_a_complete_document_parses() -> None:
    cfg = selection.parse_config(_doc(), sha256="x")
    assert cfg.version == "v9" and cfg.selection.min_ev_per_max_loss == D("0.05")


@pytest.mark.parametrize(
    "bad",
    [
        {"schema": "desk-miner/2"},
        {"status": "MAYBE"},
        {"extra": 1},
        {"selection": {"min_ev_per_max_loss": 0.05}},  # a TOML float: refused
        {"valuation": {"n_paths": 20000, "fill_k": "0.4", "stress_fill_k": "1.0",
                       "max_valued_per_name_row": 400}},  # not the pricer's fill
        {"valuation": {"n_paths": 20000, "fill_k": "0.5", "stress_fill_k": "1.0"}},
        {"limit": {"policy": "at_mid", "on_limit_not_ok": "refuse"}},
        {"limit": {"policy": "first_cent_beyond_base_fill", "on_limit_not_ok": "relimit"}},
    ],
)  # fmt: skip
def test_a_malformed_document_is_refused(bad: dict[str, object]) -> None:
    doc = _doc()
    for k, v in bad.items():
        if isinstance(v, dict) and isinstance(doc.get(k), dict) and k == "selection":
            doc[k] = {**doc[k], **v}  # type: ignore[dict-item]
        else:
            doc[k] = v
    with pytest.raises(selection.MinerConfigError):
        selection.parse_config(doc, sha256="x")


def test_the_pinned_fills_are_the_pricers() -> None:
    assert selection.load_config().fill_k == Decimal(repr(pricing.FILL_K))
    assert selection.load_config().stress_fill_k == Decimal(repr(pricing.STRESS_FILL_K))


def test_seal_config_seals_a_new_version_once(tmp_path: Path) -> None:
    d = _copy(tmp_path)
    src = (d / "v1.toml").read_text().replace('version = "v1"', 'version = "v2"')
    (d / "v2.toml").write_text(src)
    at = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    sha = selection.seal_config(d / "v2.toml", basis="test", sealed_at=at)
    assert (d / "v2.sha256").read_text() == f"{sha}  v2.toml\n"
    assert f"| 2026-09-24T12:00:00Z | v2.toml | {sha} |" in (d / "SEALS.md").read_text()
    assert selection.seal_config(d / "v2.toml", basis="again") == sha  # idempotent
    (d / "v2.toml").write_text(src + "# changed\n")
    with pytest.raises(selection.MinerSealError, match="new version"):
        selection.seal_config(d / "v2.toml", basis="x")
    # sealed but not approved in code: not loadable
    (d / "v2.toml").write_text(src)
    with pytest.raises(selection.MinerSealError, match="approved"):
        selection.load_config(d / "v2.toml")


# ------------------------------------------------------ the decision rule


@dataclass(frozen=True)
class _Val:
    ev: Decimal
    ev_fill_stress: Decimal
    ev_signal: Decimal | None
    ev_signal_fill_stress: Decimal | None
    max_loss: Decimal


def test_signal_rows_decide_on_the_signal_ev_and_the_rest_on_no_view() -> None:
    v = _Val(D("-12.00"), D("-30.00"), D("25.00"), D("8.00"), D("400.00"))
    sig = selection.decision_of(v, signal_view=True)
    assert sig is not None
    assert (sig.basis, sig.ev, sig.ev_stress_fill) == ("signal", D("25.00"), D("8.00"))
    assert sig.ev_per_max_loss == D("0.0625")  # 25 / 400
    nov = selection.decision_of(v, signal_view=False)
    assert nov is not None
    assert (nov.basis, nov.ev, nov.ev_stress_fill) == ("no_view", D("-12.00"), D("-30.00"))
    assert nov.ev_per_max_loss == D("-0.0300")
    # a signal row without its view valued is not decidable
    assert selection.decision_of(_Val(D(1), D(1), None, None, D(100)), signal_view=True) is None


def test_the_thresholds_stress_ev_strictly_above_and_ratio_at_least() -> None:
    cfg = selection.load_config()

    def fails(ev: str, stress: str, loss: str = "400.00") -> list[str]:
        dec = selection.decision_of(_Val(D(ev), D(stress), None, None, D(loss)), signal_view=False)
        assert dec is not None
        return selection.selection_failures(dec, cfg)

    assert fails("20.00", "0.01") == []  # 20/400 = 0.05 exactly: admitted
    assert fails("20.00", "0.00") == ["stress_fill_ev_not_positive"]  # 0 is not > 0
    assert fails("19.96", "5.00") == ["ev_per_max_loss_below_min"]  # 0.0499
    assert fails("-1.00", "-2.00") == ["stress_fill_ev_not_positive", "ev_per_max_loss_below_min"]


def test_rank_is_ratio_then_ev_then_smaller_loss_then_deal_id() -> None:
    def dec(ev: str, loss: str) -> selection.Decision:
        d = selection.decision_of(_Val(D(ev), D("1"), None, None, D(loss)), signal_view=False)
        assert d is not None
        return d

    items = [
        ("d-c", dec("30.00", "300.00"), D("300.00")),  # 0.1000
        ("d-a", dec("40.00", "400.00"), D("400.00")),  # 0.1000, larger EV
        ("d-b", dec("50.00", "250.00"), D("250.00")),  # 0.2000
        ("d-e", dec("30.00", "300.00"), D("300.00")),  # ties d-c: deal id
    ]
    got = sorted(items, key=lambda t: selection.rank_key(t[1], t[2], t[0]))
    assert [t[0] for t in got] == ["d-b", "d-a", "d-c", "d-e"]

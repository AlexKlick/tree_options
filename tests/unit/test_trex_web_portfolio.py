"""Wave 2 lane G: GET /api/portfolio, the owner's books as the files stand.

Every figure comes from the books lane's files under ``<run dir>/ledger``
(``Wave2RunDir`` paths). Oracles are the contract fixtures, the books
lane's own ``trex-holdings/1`` model (``ledger.HoldingsDocument``) and the
contract blocker functions, never this lane's code. An absent, unreadable or
invalid file is ``unavailable`` with a reason and null figures: never a zero.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.unit.trex_fakes import FakeCommandAuthority, SessionClock, contract_fixture
from tree_options.trex.contracts import (
    Holding,
    Reconciliation,
    RiskSnapshot,
    Wave2RunDir,
    model_bytes,
    model_document,
    reconciliation_blockers,
    risk_snapshot_blockers,
)
from tree_options.trex.ledger import HoldingsDocument
from tree_options.trex_web.app import create_app

#: the fixtures' ``as_of`` (risk snapshot and reconciliation)
AS_OF = datetime(2026, 10, 22, 14, 4, tzinfo=UTC)
#: the ledger projection's own ``as_of`` (10 s before the clock below)
HOLDINGS_AS_OF = datetime(2026, 10, 22, 14, 4, 20, tzinfo=UTC)
LEDGER_CASH_MICROS = 3_657_040_000


def holdings_document(*rows: dict, account_id: str = "DU0000001") -> HoldingsDocument:
    """A ``trex-holdings/1`` document built by the books lane's own model."""
    return HoldingsDocument(
        account_id=account_id,
        as_of=HOLDINGS_AS_OF,
        cash_micros=LEDGER_CASH_MICROS,
        holdings=tuple(Holding.model_validate(row) for row in rows),
    )


def write_holdings(books, document: HoldingsDocument) -> None:
    path = books.layout.holdings()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(model_bytes(document))


#: an unavailable holdings block: every figure null, never zero
UNAVAILABLE_HOLDINGS = {
    "status": "unavailable",
    "account_id": None,
    "as_of": None,
    "age_s": None,
    "ledger_cash_micros": None,
    "rows": None,
}


@pytest.fixture
def books(tmp_path):
    clock = SessionClock()
    clock.to(datetime(2026, 10, 22, 14, 4, 30, tzinfo=UTC))
    run_dir = tmp_path / "desk-paper"
    app = create_app(
        state_dir=str(tmp_path / "legacy"),
        plans_dir=str(tmp_path / "plans"),
        discovery_dir=str(tmp_path / "discovery"),
        desk_state_dir=str(tmp_path / "desk-state"),
        desk_run_dir=str(run_dir),
        supervised_dir=str(tmp_path / "supervised"),
        command_authority=FakeCommandAuthority(clock=clock),
        command_clock=clock,
    )
    client = TestClient(app, base_url="http://127.0.0.1:8090", client=("127.0.0.1", 50000))
    return SimpleNamespace(clock=clock, run_dir=run_dir, layout=Wave2RunDir(run_dir), client=client)


def write(path: Path, doc: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))


def full_books(books, *, reconciliation: dict | None = None) -> None:
    write(books.layout.risk_snapshot(), contract_fixture("risk_snapshot.json"))
    write(
        books.layout.reconciliation(),
        reconciliation or contract_fixture("reconciliation_required.json"),
    )
    write_holdings(books, holdings_document(contract_fixture("holding.json")))


def clean_reconciliation() -> dict:
    doc = contract_fixture("reconciliation_required.json")
    doc.update(status="clean", mismatches=[])
    return doc


def portfolio(books) -> dict:
    response = books.client.get("/api/portfolio")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    doc: dict = response.json()
    assert doc["schema"] == "trex-portfolio/1" and doc["execution_enabled"] is False
    return doc


def test_no_books_is_unavailable_with_reasons_never_zeros(books):
    doc = portfolio(books)
    assert doc["holdings"] == {**UNAVAILABLE_HOLDINGS, "reason": "holdings_missing"}
    cash = doc["cash"]
    assert cash["status"] == "unavailable" and cash["reason"] == "risk_snapshot_missing"
    assert cash["settled_cash_cents"] is None
    assert all(doc["risk"][k] is None for k in ("open_loss_cents", "equity_gross_cents"))
    assert doc["outside_positions"]["rows"] is None
    assert doc["outside_positions"]["reason"] == "risk_snapshot_missing"
    rec = doc["reconciliation"]
    assert rec["status"] == "unavailable" and rec["reason"] == "reconciliation_missing"
    assert rec["mismatches"] is None
    # absent books block entries (the contract's own blocker functions)
    assert doc["entry_blockers"] == sorted(
        [*risk_snapshot_blockers(None, AS_OF), *reconciliation_blockers(None, AS_OF)]
    )
    assert not books.run_dir.exists()  # a read never creates the run dir


def test_complete_books_are_served_exactly_as_the_owner_wrote_them(books):
    full_books(books, reconciliation=clean_reconciliation())
    doc = portfolio(books)
    snapshot = RiskSnapshot.model_validate(contract_fixture("risk_snapshot.json"))
    holding = Holding.model_validate(contract_fixture("holding.json"))

    assert doc["holdings"] == {
        "status": "ok",
        "reason": None,
        "account_id": "DU0000001",
        "as_of": "2026-10-22T14:04:20Z",
        "age_s": 10,
        "ledger_cash_micros": LEDGER_CASH_MICROS,
        "rows": [model_document(holding)],
    }
    assert doc["cash"] == {
        "status": "fresh",
        "reason": None,
        "settled_cash_cents": snapshot.cash_cents,
        "as_of": "2026-10-22T14:04:00Z",
        "age_s": 30,
    }
    assert doc["risk"]["open_loss_cents"] == snapshot.open_loss_cents
    assert doc["risk"]["day_realized_loss_cents"] == snapshot.day_realized_loss_cents
    assert doc["risk"]["equity_gross_cents"] == snapshot.equity_gross_cents
    assert doc["outside_positions"] == {
        "status": "fresh",
        "reason": None,
        "rows": [model_document(p) for p in snapshot.outside_positions],
    }
    assert doc["reconciliation"]["status"] == "clean"
    assert doc["reconciliation"]["fresh"] is True and doc["reconciliation"]["mismatches"] == []
    assert doc["account_ids"] == ["DU0000001"] and doc["problems"] == []
    assert doc["entry_blockers"] == []
    # money stays integer: no float anywhere in the books blocks
    assert isinstance(doc["cash"]["settled_cash_cents"], int)
    assert isinstance(doc["holdings"]["rows"][0]["cost_basis_micros"], int)


def test_reconciliation_required_lists_mismatches_and_blocks_entries_only(books):
    full_books(books)
    doc = portfolio(books)
    rec = Reconciliation.model_validate(contract_fixture("reconciliation_required.json"))
    assert doc["reconciliation"]["status"] == "reconciliation_required"
    assert doc["reconciliation"]["mismatches"] == [model_document(m) for m in rec.mismatches]
    assert doc["entry_blockers"] == list(reconciliation_blockers(rec, books.clock.utc()))
    assert doc["exits_blocked"] is False


def test_stale_books_keep_their_figures_but_say_stale(books):
    full_books(books, reconciliation=clean_reconciliation())
    books.clock.to(datetime(2026, 10, 22, 14, 6, 1, tzinfo=UTC))  # 121 s after as_of
    doc = portfolio(books)
    assert doc["cash"]["status"] == "stale" and doc["cash"]["reason"] == "risk_snapshot_stale"
    assert doc["cash"]["settled_cash_cents"] == contract_fixture("risk_snapshot.json")["cash_cents"]
    assert doc["cash"]["age_s"] == 121
    assert doc["reconciliation"]["fresh"] is False
    assert doc["entry_blockers"] == ["reconciliation_required", "risk_snapshot_stale"]


def test_an_unavailable_snapshot_has_null_figures_and_a_reason(books):
    write(
        books.layout.risk_snapshot(),
        {
            "schema": "trex-risk-snapshot/1",
            "account_id": "DU0000001",
            "as_of": "2026-10-22T14:04:00Z",
            "status": "unavailable",
            "open_loss_cents": None,
            "day_realized_loss_cents": None,
            "cash_cents": None,
            "equity_gross_cents": None,
        },
    )
    doc = portfolio(books)
    assert doc["cash"]["status"] == "unavailable"
    assert doc["cash"]["reason"] == "risk_snapshot_unavailable"
    assert doc["cash"]["settled_cash_cents"] is None
    assert doc["outside_positions"]["rows"] is None


@pytest.mark.parametrize(
    ("content", "reason"),
    [("not json", "unreadable"), (json.dumps({"schema": "trex-risk-snapshot/9"}), "invalid")],
)
def test_bad_files_are_unavailable_never_partial(books, content, reason):
    for path in (books.layout.risk_snapshot(), books.layout.reconciliation()):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    doc = portfolio(books)
    assert doc["cash"]["reason"] == f"risk_snapshot_{reason}"
    assert doc["reconciliation"]["reason"] == f"reconciliation_{reason}"
    assert doc["reconciliation"]["status"] == "unavailable"


def test_one_invalid_holding_makes_the_whole_list_unavailable(books):
    good = contract_fixture("holding.json")
    bad = {**good, "quantity": -2}  # a short stock row: long-only refuses
    document = model_document(holdings_document(good))
    document["holdings"] = [good, bad]
    write(books.layout.holdings(), document)
    doc = portfolio(books)
    assert doc["holdings"] == {**UNAVAILABLE_HOLDINGS, "reason": "holdings_invalid"}


def _adhoc_shapes() -> dict[str, object]:
    """Holdings-like documents that are NOT the books lane's trex-holdings/1."""
    good = contract_fixture("holding.json")
    canonical = model_document(holdings_document(good))
    return {
        "bare-list": [good],
        "no-schema": {"account_id": "DU0000001", "holdings": [good]},
        "wrong-schema": {**canonical, "schema": "trex-holdings/2"},
        "schema-missing": {k: v for k, v in canonical.items() if k != "schema"},
        "no-as-of": {k: v for k, v in canonical.items() if k != "as_of"},
        "float-cash": {**canonical, "cash_micros": 1.0},
        "extra-field": {**canonical, "total_cents": 0},
    }


@pytest.mark.parametrize("shape", sorted(_adhoc_shapes()))
def test_only_the_books_lane_document_is_read(books, shape):
    """Coordinator decision 2026-10-21: every reader uses D's trex-holdings/1;
    an ad-hoc shape is unavailable, never re-interpreted."""
    write(books.layout.holdings(), _adhoc_shapes()[shape])
    doc = portfolio(books)
    assert doc["holdings"] == {**UNAVAILABLE_HOLDINGS, "reason": "holdings_invalid"}


def test_a_projection_naming_two_accounts_is_unavailable(books):
    other = {**contract_fixture("holding.json"), "account_id": "DU0000002"}
    for lot in other["lots"]:
        lot["attribution"]["intent_id"] = "eq-spy-002"
    write_holdings(books, holdings_document(contract_fixture("holding.json"), other))
    doc = portfolio(books)
    assert doc["holdings"] == {**UNAVAILABLE_HOLDINGS, "reason": "holdings_account_mismatch"}
    assert "account_mismatch" in [p["reason"] for p in doc["problems"]]


def test_an_empty_ledger_projection_is_a_real_empty_list(books):
    write_holdings(books, holdings_document())
    doc = portfolio(books)
    assert doc["holdings"]["status"] == "ok" and doc["holdings"]["reason"] is None
    assert doc["holdings"]["rows"] == []
    # the ledger's cash is the document's figure, not a placeholder zero
    assert doc["holdings"]["ledger_cash_micros"] == LEDGER_CASH_MICROS


def test_an_unreadable_projection_is_unavailable(books):
    full_books(books, reconciliation=clean_reconciliation())
    path = books.layout.holdings()
    path.write_text('{"schema": "trex-holdings/1", torn')
    doc = portfolio(books)
    assert doc["holdings"] == {**UNAVAILABLE_HOLDINGS, "reason": "holdings_unreadable"}
    # a symlinked projection is not the books lane's file
    path.unlink()
    path.symlink_to(books.layout.risk_snapshot())
    doc = portfolio(books)
    assert doc["holdings"] == {**UNAVAILABLE_HOLDINGS, "reason": "holdings_unreadable"}
    # the other blocks are unaffected: one bad file never zeroes the rest
    assert doc["cash"]["status"] == "fresh"


def test_pause_blocks_entries_never_exits_and_halt_blocks_both(books):
    full_books(books, reconciliation=clean_reconciliation())
    (books.run_dir / "PAUSE_ENTRIES").write_text("")
    doc = portfolio(books)
    assert doc["kill_files"] == {"PAUSE_ENTRIES": True, "HALT": False, "FLATTEN": False}
    assert doc["entry_blockers"] == ["entries_paused"]
    assert doc["exits_blocked"] is False
    (books.run_dir / "HALT").write_text("")
    doc = portfolio(books)
    assert doc["exits_blocked"] is True


def test_mixed_or_non_paper_accounts_are_reported(books):
    full_books(books, reconciliation=clean_reconciliation())
    snapshot = contract_fixture("risk_snapshot.json")
    snapshot["account_id"] = "U7654321"
    write(books.layout.risk_snapshot(), snapshot)
    doc = portfolio(books)
    assert doc["account_ids"] == ["DU0000001", "U7654321"]
    assert sorted(p["reason"] for p in doc["problems"]) == ["account_mismatch", "non_paper_account"]


def test_portfolio_is_read_only(books):
    full_books(books)
    before = {p: p.read_bytes() for p in books.run_dir.rglob("*") if p.is_file()}
    portfolio(books)
    after = {p: p.read_bytes() for p in books.run_dir.rglob("*") if p.is_file()}
    assert after == before
    assert books.client.post("/api/portfolio").status_code == 405

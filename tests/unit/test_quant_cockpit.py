import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tree_options.trex_web.quant_view import attach


def theory_result():
    metrics = {
        "evidence_kind": "BACKTEST",
        "capital_policy": "independent_equal_capital_roundtrips",
        "disposition": "SCORED",
        "execution_authorized": False,
        "exact_external_economics": False,
        "period_count": 1,
        "scored_period_count": 1,
        "compound_nav": None,
        "max_drawdown_scope": "endpoint_loss_only",
        "mean_net_return": "0.012",
        "max_drawdown": "0.03",
        "turnover": "1.98",
        "fees": "10",
    }
    node = {
        "schema": "quant-research-node/1",
        "node_id": "b" * 64,
        "campaign_id": "a" * 64,
        "stage": "freeze_inputs",
        "payload_sha256": "c" * 64,
        "parents": [],
        "execution_authorized": False,
    }
    return {
        "schema": "quant-theory-result/1",
        "campaign_id": "a" * 64,
        "hypothesis": "Compare strategies on frozen inputs",
        "data_class": "synthetic_fixture",
        "evidence_kind": "synthetic_backtest",
        "registration": "exploratory_retrospective",
        "candidate_count": 2,
        "reflection_calls": 0,
        "winner": {
            "strategy_id": "equal_weight_us_equities",
            "parameters": {"top_n": 1},
            "version_id": "d" * 64,
        },
        "holdout": {"candidate": dict(metrics), "control": dict(metrics)},
        "graph": [node],
        "disposition": "REVIEW_REQUIRED",
        "objective": "mean_next_session_net_return-minus-endpoint_loss-and-turnover",
        "limitations": ["Independent roundtrips; endpoint loss only"],
        "execution_authorized": False,
        "exact_external_economics": False,
        "live_money": False,
    }


def persist_theory(workspace, result, *, persist_nodes=True):
    from tree_options.research.quant import register_version
    from tree_options.research.runstate.store import open_runstate_store

    with open_runstate_store(workspace) as store:
        version = register_version(
            store,
            result["winner"]["strategy_id"],
            code_sha="b" * 40,
            lock_sha="c" * 64,
            parameters=result["winner"]["parameters"],
        )
        result["winner"]["version_id"] = version["version_id"]
        for node in result["graph"] if persist_nodes else []:
            store.put("quant_provenance", node, key=node["node_id"])
        store.put("result", result, key=result["campaign_id"])


def test_theory_campaign_projects_persisted_metrics_and_graph_without_recompute(tmp_path):
    workspace = tmp_path / "research"
    result = theory_result()
    result["holdout"]["candidate"]["periods"] = [{"private_metadata": "not-for-the-cockpit"}]
    persist_theory(workspace, result)
    app = FastAPI()
    attach(app, workspace=workspace, execution_state=tmp_path / "broker")
    client = TestClient(app)
    response = client.get("/api/research/quant")
    assert response.status_code == 200
    row = response.json()["theory_campaigns"][0]
    assert row["candidate_count"] == 2 and row["data_class"] == "synthetic_fixture"
    assert row["holdout"]["candidate"]["mean_net_return"] == "0.012"
    assert row["graph"] == result["graph"]
    assert "not-for-the-cockpit" not in response.text
    assert client.post("/api/research/quant").status_code == 405


@pytest.mark.parametrize(
    "corruption",
    [
        "text_execution",
        "live_true",
        "text_exact",
        "nan_metric",
        "number_metric",
        "wrong_evidence",
        "wrong_loss_scope",
        "bad_graph",
        "bad_count",
    ],
)
def test_theory_campaign_invalid_proof_fails_closed(tmp_path, corruption):
    result = theory_result()
    if corruption == "text_execution":
        result["execution_authorized"] = "false"
    elif corruption == "live_true":
        result["live_money"] = True
    elif corruption == "text_exact":
        result["holdout"]["candidate"]["exact_external_economics"] = "false"
    elif corruption == "nan_metric":
        result["holdout"]["candidate"]["mean_net_return"] = "NaN"
    elif corruption == "number_metric":
        result["holdout"]["candidate"]["mean_net_return"] = 0.012
    elif corruption == "wrong_evidence":
        result["evidence_kind"] = "broker_paper"
    elif corruption == "wrong_loss_scope":
        result["holdout"]["control"]["max_drawdown_scope"] = "intraday_drawdown"
    elif corruption == "bad_count":
        result["candidate_count"] = True
    else:
        result["graph"][0]["parents"] = ["unknown-node"]
    workspace = tmp_path / "research"
    persist_theory(workspace, result)
    app = FastAPI()
    attach(app, workspace=workspace, execution_state=tmp_path / "broker")
    assert TestClient(app).get("/api/research/quant").status_code == 503


def test_cockpit_preserves_evidence_classes_and_never_owns_broker(tmp_path):
    app = FastAPI()
    attach(app, workspace=tmp_path / "research", execution_state=tmp_path / "broker")
    client = TestClient(app)
    response = client.get("/api/research/quant")
    assert response.status_code == 200
    data = response.json()
    assert len(data["strategies"]) == 7
    assert data["evidence_classes"] == [
        "BACKTEST",
        "DETERMINISTIC REPLAY",
        "SIMULATED EXECUTION",
        "BROKER PAPER",
        "LIVE",
    ]
    assert data["execution"]["state"] == "NOT_OBSERVED"
    assert data["live_money"] is False
    assert client.post("/api/research/quant/submit", json={}).status_code == 404


def test_invalid_or_stale_projection_never_reports_ready(tmp_path):
    import json
    from datetime import UTC, datetime

    from tree_options.time.sessions import shift_instant

    app = FastAPI()
    state = tmp_path / "broker"
    state.mkdir()
    attach(app, workspace=tmp_path / "research", execution_state=state)
    (state / "projection.json").write_text(
        json.dumps(
            {
                "environment": "BROKER PAPER",
                "live_money": False,
                "observed_at": shift_instant(datetime.now(UTC), -100).isoformat(),
                "ready": True,
                "owner_held": True,
            }
        )
    )
    result = TestClient(app).get("/api/research/quant").json()
    assert result["execution"]["state"] == "STALE"
    assert result["execution"]["ready"] is False
    (state / "projection.json").write_text("{invalid")
    assert TestClient(app).get("/api/research/quant").status_code == 503


@pytest.mark.parametrize(
    "corruption", ["naive_time", "missing_findings", "text_ready", "text_exact"]
)
def test_malformed_execution_projection_refuses_incomplete_proof(tmp_path, corruption):
    import json
    from datetime import UTC, datetime

    source = {
        "environment": "BROKER PAPER",
        "live_money": False,
        "observed_at": datetime.now(UTC).isoformat(),
        "ready": False,
        "owner_held": True,
        "executions": [
            {
                "intent_id": "one",
                "state": "SENT",
                "broker_state": "AMBIGUOUS",
                "reconciliation_clean": False,
                "findings": ["UNKNOWN"],
                "evidence_verdict": "REFUSED",
                "exact_economics": False,
                "records": [],
            }
        ],
    }
    if corruption == "naive_time":
        source["observed_at"] = "2026-09-29T20:00:00"
    elif corruption == "missing_findings":
        del source["executions"][0]["findings"]
    elif corruption == "text_ready":
        source["ready"] = "false"
    else:
        source["executions"][0]["exact_economics"] = "false"
    state = tmp_path / "broker"
    state.mkdir()
    (state / "projection.json").write_text(json.dumps(source))
    app = FastAPI()
    attach(app, workspace=tmp_path / "research", execution_state=state)
    assert TestClient(app).get("/api/research/quant").status_code == 503


@pytest.mark.parametrize("data_class", ["synthetic_fixture", "user_supplied_unqualified"])
def test_theory_incomplete_holdout_stays_unavailable(tmp_path, data_class):
    result = theory_result()
    result["data_class"] = data_class
    result["evidence_kind"] = (
        "synthetic_backtest" if data_class == "synthetic_fixture" else "simulated_execution"
    )
    result["disposition"] = "HOLDOUT_INCOMPLETE"
    result["holdout"]["candidate"].update(
        disposition="INCOMPLETE",
        scored_period_count=0,
        mean_net_return=None,
        max_drawdown=None,
        turnover=None,
        fees=None,
    )
    workspace = tmp_path / "research"
    persist_theory(workspace, result)
    app = FastAPI()
    attach(app, workspace=workspace, execution_state=tmp_path / "broker")
    response = TestClient(app).get("/api/research/quant")
    assert response.status_code == 200
    row = response.json()["theory_campaigns"][0]
    assert row["data_class"] == data_class
    assert row["holdout"]["candidate"]["mean_net_return"] is None
    assert row["disposition"] == "HOLDOUT_INCOMPLETE"


@pytest.mark.parametrize("corruption", ["overflow", "unpersisted_node", "incomplete_metrics"])
def test_theory_finite_and_durable_proof_required(tmp_path, corruption):
    result = theory_result()
    if corruption == "overflow":
        result["holdout"]["candidate"]["mean_net_return"] = "1e10000"
    elif corruption == "incomplete_metrics":
        result["disposition"] = "HOLDOUT_INCOMPLETE"
        result["holdout"]["candidate"].update(disposition="INCOMPLETE", scored_period_count=0)
    workspace = tmp_path / "research"
    persist_theory(workspace, result, persist_nodes=corruption != "unpersisted_node")
    app = FastAPI()
    attach(app, workspace=workspace, execution_state=tmp_path / "broker")
    assert TestClient(app).get("/api/research/quant").status_code == 503


def test_theory_dag_retains_verified_payload_reference(tmp_path):
    from tree_options.research.quant import digest
    from tree_options.research.runstate.store import open_runstate_store

    result = theory_result()
    payload = {"stage_note": "Frozen fixture inputs"}
    key = digest(payload)
    node = result["graph"][0]
    node["payload_sha256"] = key
    node["payload_ref"] = f"runstate:quant_provenance/{key}"
    workspace = tmp_path / "research"
    with open_runstate_store(workspace) as store:
        store.put(
            "quant_provenance", {"schema": "quant-research-payload/1", "payload": payload}, key=key
        )
    persist_theory(workspace, result)
    app = FastAPI()
    attach(app, workspace=workspace, execution_state=tmp_path / "broker")
    response = TestClient(app).get("/api/research/quant")
    assert response.status_code == 200
    projected = response.json()["theory_campaigns"][0]["graph"][0]
    assert projected["payload_ref"] == node["payload_ref"]
    assert "stage_note" not in projected


def test_real_campaign_projects_registered_winner_and_durable_dag(tmp_path, monkeypatch):
    import hashlib
    import json
    from datetime import date
    from decimal import Decimal

    from tree_options.research.quant import FrozenUniverse, QuantSnapshot
    from tree_options.research.quant_backtest import ReplayPeriod
    from tree_options.research.quant_campaign import CampaignSpec, run_campaign
    from tree_options.strategy_lab.contracts import Observation
    from tree_options.time.calendar import StaticSessionCalendar

    sessions = [date(2026, 9, n) for n in (21, 22, 23, 24, 25, 28)]
    path = tmp_path / "calendar.json"
    path.write_text(
        json.dumps(
            {
                "calendar": "fixture",
                "timezone": "America/New_York",
                "open": "09:30",
                "close": "16:00",
                "sessions": [day.isoformat() for day in sessions],
            }
        )
    )
    checksum = tmp_path / "calendar.sha256"
    checksum.write_text(hashlib.sha256(path.read_bytes()).hexdigest())
    calendar = StaticSessionCalendar(path, checksum)
    periods = []
    for decision in sessions[::2]:
        at = calendar.session_close(decision)
        snapshot = QuantSnapshot(
            FrozenUniverse(decision, ("A",), "fixture", "a" * 64),
            at,
            (Observation("A", at, at, {"close": Decimal("10")}, "fixture", f"A-{decision}"),),
        )
        execution = calendar.nth_after(decision, 1)
        periods.append(
            ReplayPeriod(
                snapshot,
                calendar.session_open(execution),
                calendar.session_close(execution),
                {"A": Decimal("10")},
                {"A": Decimal("11")},
            )
        )
    spec = CampaignSpec(
        "Compare frozen one-session returns",
        (periods[0],),
        (periods[1],),
        (periods[2],),
        "b" * 40,
        "c" * 64,
        (),
        max_candidates=2,
        generations=0,
        data_class="synthetic_fixture",
    )
    workspace = tmp_path / "research"
    result = run_campaign(workspace, spec, calendar)
    assert result["candidate_count"] == 1
    assert result["winner"]["version_id"].startswith("equal_weight_us_equities/v1/")

    def refuse_recompute(*args, **kwargs):
        raise AssertionError("GET must not rerun the campaign")

    monkeypatch.setattr("tree_options.research.quant_campaign.run_campaign", refuse_recompute)
    monkeypatch.setattr("tree_options.research.quant_backtest.evaluate_periods", refuse_recompute)
    app = FastAPI()
    attach(app, workspace=workspace, execution_state=tmp_path / "broker")
    response = TestClient(app).get("/api/research/quant")
    assert response.status_code == 200
    projected = response.json()["theory_campaigns"][0]
    assert projected["winner"] == result["winner"]
    assert projected["candidate_count"] == 1
    assert projected["graph"] == result["graph"]
    assert (
        projected["holdout"]["candidate"]["mean_net_return"]
        == result["holdout"]["candidate"]["mean_net_return"]
    )


def test_theory_winner_parameters_must_match_registered_version(tmp_path):
    from tree_options.research.runstate.store import open_runstate_store

    result = theory_result()
    workspace = tmp_path / "research"
    persist_theory(workspace, result)
    result["winner"]["parameters"]["top_n"] = 2
    with open_runstate_store(workspace) as store:
        store.replace("result", result, key=result["campaign_id"])
    app = FastAPI()
    attach(app, workspace=workspace, execution_state=tmp_path / "broker")
    assert TestClient(app).get("/api/research/quant").status_code == 503

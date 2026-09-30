"""Bounded quant assignments through the existing Research Lab queue.

Dataset paths are server configuration, never browser input. All datasets remain
unqualified for trading. HTTP spools immutable specs; the shared worker computes.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tree_options.research.paths import assert_no_overlap_with_desk
from tree_options.research.quant import digest
from tree_options.research.quant_campaign import Proposal, engine_identity, run_campaign
from tree_options.research.quant_campaign_io import (
    Glm53Proposer,
    make_fixture,
    spec_from_dict,
    strict_json,
)
from tree_options.research.runstate.store import open_runstate_store
from tree_options.time.calendar import StaticSessionCalendar, calendar_content_sha256

SYNTHETIC = "synthetic-machinery-v1"
STRATEGIES = {"equal_weight_us_equities", "momentum_12_1", "hqm_1_3_6_12"}
_FIELDS = {
    "hypothesis",
    "dataset_id",
    "capital",
    "max_candidates",
    "generations",
    "top_n",
    "strategy_id",
    "reflect_glm53",
    "sleeve_id",
}
_SAFE = _FIELDS | {
    "run_id",
    "status",
    "kind",
    "data_class",
    "created_at",
    "started_at",
    "completed_at",
}
_ROOT = Path(__file__).resolve().parents[3]


def datasets_root() -> Path:
    return Path(
        os.environ.get(
            "TREX_QUANT_DATASETS_DIR", str(Path.home() / ".local/state/trex-quant-datasets")
        )
    )


def _identity() -> dict[str, str]:
    return {
        "code_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=_ROOT, text=True
        ).strip(),
        "lock_sha": hashlib.sha256((_ROOT / "uv.lock").read_bytes()).hexdigest(),
        "engine_sha256": engine_identity(),
        "assignment_engine_sha256": digest(
            {
                name: hashlib.sha256((_ROOT / "src/tree_options" / name).read_bytes()).hexdigest()
                for name in ("research/quant_jobs.py", "research/runstate/worker.py")
            }
        ),
    }


def _source_clean() -> bool:
    return not subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=_ROOT, text=True
    ).strip()


def _path(root: Path, name: Any) -> Path:
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise ValueError("dataset paths must be relative server configuration")
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("dataset path escapes registry")
    return path


def _entries(root: Path) -> list[dict[str, Any]]:
    manifest = root / "manifest.json"
    if not manifest.exists():
        return []
    raw = strict_json(manifest)
    if (
        not isinstance(raw, dict)
        or set(raw) != {"schema", "datasets"}
        or raw["schema"] != "trex-quant-datasets/1"
        or not isinstance(raw["datasets"], list)
    ):
        raise ValueError("invalid frozen dataset manifest")
    if len(raw["datasets"]) > 16:
        raise ValueError("bounded dataset catalog required")
    seen = {SYNTHETIC}
    for row in raw["datasets"]:
        if not isinstance(row, dict) or set(row) != {
            "dataset_id",
            "label",
            "input_file",
            "input_sha256",
            "calendar_file",
            "calendar_checksum_file",
            "data_class",
        }:
            raise ValueError("invalid dataset entry")
        key = row["dataset_id"]
        if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", key) or key in seen:
            raise ValueError("invalid or duplicate dataset identity")
        seen.add(key)
        if (
            not isinstance(row["label"], str)
            or not 1 <= len(row["label"]) <= 160
            or row["data_class"] not in {"synthetic_fixture", "user_supplied_unqualified"}
        ):
            raise ValueError("dataset cannot claim qualification")
        if not isinstance(row["input_sha256"], str) or not re.fullmatch(
            "[a-f0-9]{64}", row["input_sha256"]
        ):
            raise ValueError("invalid dataset checksum")
        for field in ("input_file", "calendar_file", "calendar_checksum_file"):
            _path(root, row[field])
    return raw["datasets"]


def dataset_catalog(root: Path) -> list[dict[str, Any]]:
    rows = [
        {
            "dataset_id": SYNTHETIC,
            "label": "Synthetic machinery demonstration",
            "data_class": "synthetic_fixture",
        },
        *_entries(root),
    ]
    identity = _identity()
    result = []
    for row in rows:
        spec, _calendar, _binding = _dataset(root, row["dataset_id"], identity)
        summary = {}
        for split in ("train", "validation", "holdout"):
            periods = getattr(spec, split)
            summary[split] = {
                "period_count": len(periods),
                "decision_start": periods[0].snapshot.cutoff.date().isoformat(),
                "decision_end": periods[-1].snapshot.cutoff.date().isoformat(),
            }
        universe = {
            symbol
            for split in (spec.train, spec.validation, spec.holdout)
            for period in split
            for symbol in period.snapshot.universe.members
        }
        result.append(
            {k: row[k] for k in ("dataset_id", "label", "data_class")}
            | {"promotion_allowed": False, "splits": summary, "universe_count": len(universe)}
        )
    return result


def _dataset(
    root: Path, dataset_id: str, identity: dict[str, str]
) -> tuple[Any, StaticSessionCalendar, dict[str, Any]]:
    if dataset_id == SYNTHETIC:
        base = _ROOT / "data/calendar"
        calendar_path = base / "nyse_sessions_2018_01_02_2026_12_31.json"
        checksum_path = calendar_path.with_suffix(".sha256")
        calendar = StaticSessionCalendar(calendar_path, checksum_path)
        spec = make_fixture(calendar, identity["code_sha"], identity["lock_sha"])
        binding = {"dataset_id": dataset_id, "input_sha256": digest(spec.to_dict())}
    else:
        row = next((item for item in _entries(root) if item["dataset_id"] == dataset_id), None)
        if row is None:
            raise ValueError("unknown frozen dataset")
        source = _path(root, row["input_file"])
        if source.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("frozen dataset exceeds bounded size")
        body = source.read_bytes()
        if hashlib.sha256(body).hexdigest() != row["input_sha256"]:
            raise ValueError("frozen dataset checksum changed")
        from tree_options.research.quant_campaign_io import decode_json

        spec = spec_from_dict(decode_json(body))
        if sum(len(split) for split in (spec.train, spec.validation, spec.holdout)) > 512:
            raise ValueError("bounded research periods required")
        if spec.data_class != row["data_class"]:
            raise ValueError("dataset evidence class mismatch")
        calendar_path = _path(root, row["calendar_file"])
        checksum_path = _path(root, row["calendar_checksum_file"])
        calendar = StaticSessionCalendar(calendar_path, checksum_path)
        binding = {
            "dataset_id": dataset_id,
            "input_sha256": row["input_sha256"],
            "source_code_sha": spec.code_sha,
            "source_lock_sha": spec.lock_sha,
        }
    binding["calendar_sha256"] = calendar_content_sha256(calendar)
    binding["calendar_path"] = str(calendar_path)
    binding["calendar_checksum_path"] = str(checksum_path)
    return spec, calendar, binding


def _validate(raw: Any) -> dict[str, Any]:
    if (
        not isinstance(raw, dict)
        or set(raw) - _FIELDS
        or not {"hypothesis", "dataset_id", "capital"} <= set(raw)
    ):
        raise ValueError("invalid assignment fields")
    row = {
        "max_candidates": 8,
        "generations": 0,
        "top_n": None,
        "strategy_id": "equal_weight_us_equities",
        "reflect_glm53": False,
        **raw,
    }
    if not isinstance(row["hypothesis"], str) or not 8 <= len(row["hypothesis"].strip()) <= 2000:
        raise ValueError("bounded hypothesis required")
    if not isinstance(row["dataset_id"], str) or not re.fullmatch(
        r"[a-zA-Z0-9_-]{1,80}", row["dataset_id"]
    ):
        raise ValueError("dataset ID required")
    if not isinstance(row["capital"], str) or len(row["capital"]) > 40:
        raise ValueError("capital requires Decimal string")
    try:
        capital = Decimal(row["capital"])
    except InvalidOperation as exc:
        raise ValueError("invalid capital") from exc
    if not capital.is_finite() or not 0 < capital <= 1000000000:
        raise ValueError("invalid bounded capital")
    for key, low, high in (("max_candidates", 2, 32), ("generations", 0, 4)):
        if type(row[key]) is not int or not low <= row[key] <= high:
            raise ValueError(f"invalid {key}")
    if row["top_n"] is not None and (
        type(row["top_n"]) is not int or not 1 <= row["top_n"] <= 1000
    ):
        raise ValueError("invalid top_n")
    if row["strategy_id"] not in STRATEGIES or type(row["reflect_glm53"]) is not bool:
        raise ValueError("unsupported strategy or reflector")
    if "sleeve_id" in row and (
        not isinstance(row["sleeve_id"], str)
        or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", row["sleeve_id"])
    ):
        raise ValueError("invalid sleeve identity")
    return row


def safe_job(run: dict[str, Any]) -> dict[str, Any]:
    row = {k: v for k, v in run.items() if k in _SAFE}
    if "error" in run:
        row["error"] = run["error_code"] if "error_code" in run else "research_failed"
    return {**row, "promotion_allowed": False, "execution_authorized": False, "live_money": False}


def enqueue(workspace: Path, raw: Any, *, datasets_dir: Path) -> tuple[dict[str, Any], bool]:
    row = _validate(raw)
    if not _source_clean():
        raise ValueError("research custody requires clean source")
    assert_no_overlap_with_desk(workspace=workspace)
    identity = _identity()
    base, _calendar, binding = _dataset(datasets_dir, row["dataset_id"], identity)
    spec = replace(
        base,
        hypothesis=row["hypothesis"],
        capital=Decimal(row["capital"]),
        max_candidates=row["max_candidates"],
        generations=row["generations"],
        code_sha=identity["code_sha"],
        lock_sha=identity["lock_sha"],
        seeds=(Proposal(row["strategy_id"], row["top_n"], "User assigned bounded theory"),),
    )
    frozen = {
        "schema": "quant-assignment/1",
        "request": row,
        "campaign": spec.to_dict(),
        "identity": identity,
        "dataset": binding,
        "datasets_dir": str(datasets_dir.resolve()),
        "reflector": Glm53Proposer.identity if row["reflect_glm53"] else "none",
    }
    run_id = digest(frozen)
    workspace.mkdir(parents=True, exist_ok=True)
    with (workspace / "queue.control.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with open_runstate_store(workspace) as store:
            store.verify()
            retained = store.get("run", run_id)
            if retained is not None:
                return safe_job(retained), False
            from tree_options.research.runstate.worker import RUN_FORMAT_VERSION

            run = {
                **row,
                "run_id": run_id,
                "kind": "quant_campaign",
                "format_version": RUN_FORMAT_VERSION,
                "status": "queued",
                "created_at": datetime.now(UTC).isoformat(),
                "data_class": spec.data_class,
            }
            store.put("spec", frozen, key=run_id)
            store.put("run", run, key=run_id)
    return safe_job(run), True


def jobs(workspace: Path) -> list[dict[str, Any]]:
    with open_runstate_store(workspace) as store:
        store.verify()
        return [
            safe_job(r) | _result_summary(store.get("result", r["run_id"]))
            for r in store.all("run")
            if r.get("kind") == "quant_campaign"
        ]


def _result_summary(result: dict[str, Any] | None) -> dict[str, Any]:
    if result is None:
        return {}
    campaign = result["campaign"]
    candidate = campaign["holdout"]["candidate"]
    control = campaign["holdout"]["control"]
    return {
        "result_summary": {
            "disposition": campaign["disposition"],
            "mean_net_return": candidate["mean_net_return"],
            "control_mean_net_return": control["mean_net_return"],
            "period_count": candidate["period_count"],
            "fees": candidate["fees"],
            "evidence_kind": campaign["evidence_kind"],
            "data_class": campaign["data_class"],
            "exact_external_economics": False,
        }
    }


def _run(store: Any, run_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{64}", run_id):
        raise KeyError("unknown job")
    run = store.get("run", run_id)
    if run is None or run.get("kind") != "quant_campaign":
        raise KeyError("unknown job")
    return run


def job_detail(workspace: Path, run_id: str) -> dict[str, Any]:
    with open_runstate_store(workspace) as store:
        store.verify()
        run = _run(store, run_id)
        result = store.get("result", run_id)
    campaign_workspace = workspace / "quant-jobs" / run_id
    provenance = []
    if (campaign_workspace / "runstate.sqlite3").is_file():
        with open_runstate_store(campaign_workspace) as campaign_store:
            campaign_store.verify()
            provenance = [
                {k: node[k] for k in ("node_id", "stage", "parents", "payload_sha256") if k in node}
                for node in campaign_store.all("quant_provenance")
                if "stage" in node
            ]
    return {
        "job": safe_job(run),
        "result": result.get("campaign") if result else None,
        "provenance": provenance,
        "execution_authorized": False,
        "live_money": False,
    }


def set_stopped(workspace: Path, run_id: str, stopped: bool) -> dict[str, Any]:
    assert_no_overlap_with_desk(workspace=workspace)
    with (workspace / "queue.control.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with open_runstate_store(workspace) as store:
            store.verify()
            run = _run(store, run_id)
            if run["status"] == "completed":
                raise ValueError("completed research cannot resume or stop")
            stop = workspace / "quant-jobs" / run_id / "STOP"
            stop.parent.mkdir(parents=True, exist_ok=True)
            if stopped:
                stop.touch()
                status = "stopping" if run["status"] in {"running", "stopping"} else "stopped"
            else:
                if run["status"] in {"running", "stopping"}:
                    raise ValueError("wait for active research to stop")
                stop.unlink(missing_ok=True)
                status = "queued"
            updated = {**run, "status": status}
            store.replace("run", updated, key=run_id)
            return safe_job(updated)


def compute_job(workspace: Path, run_id: str, frozen: dict[str, Any]) -> dict[str, Any]:
    if digest(frozen) != run_id or frozen.get("schema") != "quant-assignment/1":
        raise ValueError("assignment identity mismatch")
    if not _source_clean():
        raise ValueError("research custody changed")
    identity = _identity()
    if identity != frozen["identity"]:
        raise ValueError("research custody changed")
    _base, calendar, binding = _dataset(
        Path(frozen["datasets_dir"]), frozen["request"]["dataset_id"], identity
    )
    if binding != frozen["dataset"]:
        raise ValueError("research dataset custody changed")
    spec = spec_from_dict(frozen["campaign"])
    reflector = Glm53Proposer() if frozen["reflector"] != "none" else None
    result = run_campaign(
        workspace / "quant-jobs" / run_id,
        spec,
        calendar,
        proposer=reflector,
        proposer_identity=reflector.identity if reflector else "none",
    )
    return {
        "run_id": run_id,
        "campaign": result,
        "wire": result,
        "result_sha256": digest(result),
        "engine_sha256": identity["engine_sha256"],
    }


def failure_code(exc: Exception) -> str:
    """Shared legacy run endpoints must also receive sanitized quant errors."""
    if str(exc) == "research custody changed":
        return "research_source_changed"
    if str(exc) in {"research dataset custody changed", "frozen dataset checksum changed"}:
        return "research_dataset_changed"
    if str(exc) == "research campaign halted by STOP file":
        return "research_halted"
    return "research_failed"

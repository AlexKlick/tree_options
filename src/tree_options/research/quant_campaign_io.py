"""Strict campaign input and explicit GLM-5.3 proposal boundary.

No arbitrary generated code executes. Credentials are read only by the existing
server-side TREX LLM transport and are never included in feedback or artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tree_options.research.quant import FrozenUniverse, QuantSnapshot
from tree_options.research.quant_backtest import ReplayPeriod
from tree_options.research.quant_campaign import CampaignSpec, Proposal, run_campaign
from tree_options.research.runstate.store import open_runstate_store
from tree_options.strategy_lab.contracts import Observation
from tree_options.time.calendar import StaticSessionCalendar


def make_fixture(calendar: StaticSessionCalendar, code_sha: str, lock_sha: str) -> CampaignSpec:
    """Generate a clearly synthetic acceptance fixture, never market evidence."""
    periods = []
    members = ("FIXTURE_A", "FIXTURE_B")
    for decision in (date(2026, 9, 21), date(2026, 9, 23), date(2026, 9, 25), date(2026, 9, 29)):
        at = calendar.session_close(decision)
        observations = tuple(
            Observation(
                symbol,
                at,
                at,
                {"close": Decimal("100")},
                "synthetic_fixture",
                f"{symbol}-{decision}",
            )
            for symbol in members
        )
        snapshot = QuantSnapshot(
            FrozenUniverse(
                decision,
                members,
                "synthetic_fixture",
                hashlib.sha256(b"fictional fixed universe").hexdigest(),
            ),
            at,
            observations,
        )
        next_session = calendar.nth_after(decision, 1)
        periods.append(
            ReplayPeriod(
                snapshot,
                calendar.session_open(next_session),
                calendar.session_close(next_session),
                dict.fromkeys(members, Decimal("100")),
                {"FIXTURE_A": Decimal("105"), "FIXTURE_B": Decimal("99")},
            )
        )
    return CampaignSpec(
        "Synthetic machinery test: compare concentration and declared costs",
        tuple(periods[:2]),
        (periods[2],),
        (periods[3],),
        code_sha,
        lock_sha,
        (Proposal("equal_weight_us_equities", 1, "Synthetic concentration challenger"),),
        max_candidates=4,
        data_class="synthetic_fixture",
    )


def _object(value: Any, fields: set[str], required: set[str] | None = None) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) - fields
        or not (fields if required is None else required) <= value.keys()
    ):
        raise ValueError("unknown or missing input fields")
    return value


def decode_json(text: str | bytes) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON identity")
            result[key] = value
        return result

    return json.loads(
        text,
        object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")),
    )


def strict_json(path: Path) -> Any:
    return decode_json(path.read_text())


def _snapshot(raw: Any) -> QuantSnapshot:
    row = _object(raw, {"universe", "knowledge_cutoff", "observations"})
    universe = _object(row["universe"], {"as_of", "members", "source", "source_sha256"})
    if not isinstance(universe["members"], list) or any(
        not isinstance(x, str) for x in universe["members"]
    ):
        raise ValueError("universe requires explicit member list")
    frozen = FrozenUniverse(
        date.fromisoformat(universe["as_of"]),
        tuple(universe["members"]),
        universe["source"],
        universe["source_sha256"],
    )
    if not isinstance(row["observations"], list):
        raise ValueError("explicit observations required")
    observations = []
    for raw_obs in row["observations"]:
        obs = _object(
            raw_obs,
            {"entity_id", "event_at", "available_at", "values", "source", "source_id", "metadata"},
            {"entity_id", "event_at", "available_at", "values", "source", "source_id"},
        )
        if not isinstance(obs["values"], dict) or any(
            not isinstance(x, str) for x in obs["values"].values()
        ):
            raise ValueError("observation values require Decimal strings")
        observations.append(
            Observation(
                obs["entity_id"],
                datetime.fromisoformat(obs["event_at"]),
                datetime.fromisoformat(obs["available_at"]),
                {k: Decimal(v) for k, v in obs["values"].items()},
                obs["source"],
                obs["source_id"],
                obs.get("metadata", {}),
            )
        )
    return QuantSnapshot(
        frozen, datetime.fromisoformat(row["knowledge_cutoff"]), tuple(observations)
    )


def period_from_dict(raw: Any) -> ReplayPeriod:
    row = _object(raw, {"snapshot", "execution_at", "mark_at", "opens", "closes"})
    prices = []
    for name in ("opens", "closes"):
        if not isinstance(row[name], dict) or any(
            not isinstance(v, str) for v in row[name].values()
        ):
            raise ValueError("outcome prices require Decimal strings")
        prices.append({k: Decimal(v) for k, v in row[name].items()})
    return ReplayPeriod(
        _snapshot(row["snapshot"]),
        datetime.fromisoformat(row["execution_at"]),
        datetime.fromisoformat(row["mark_at"]),
        *prices,
    )


def proposal_from_dict(raw: Any) -> Proposal:
    row = _object(raw, {"strategy_id", "parameters", "rationale"})
    params = _object(row["parameters"], {"top_n"}, set()) if row["parameters"] else {}
    if not isinstance(row["parameters"], dict):
        raise ValueError("parameters must be an object")
    return Proposal(row["strategy_id"], params.get("top_n"), row["rationale"])


def spec_from_dict(raw: Any) -> CampaignSpec:
    required = {
        "schema",
        "hypothesis",
        "train",
        "validation",
        "holdout",
        "code_sha",
        "lock_sha",
        "seeds",
    }
    optional = {
        "max_candidates",
        "generations",
        "capital",
        "slippage_bps",
        "risk_penalty",
        "turnover_penalty",
        "data_class",
    }
    derived = {"objective", "fees", "registration", "execution_authorized"}
    row = _object(raw, required | optional | derived, required)
    if row["schema"] != "quant-theory-campaign/1":
        raise ValueError("unknown campaign schema")
    kwargs = {k: row[k] for k in optional if k in row}
    for key in ("capital", "slippage_bps", "risk_penalty", "turnover_penalty"):
        if key in kwargs:
            if not isinstance(kwargs[key], str):
                raise ValueError("campaign monetary/objective values require Decimal strings")
            kwargs[key] = Decimal(kwargs[key])
    for name in ("train", "validation", "holdout", "seeds"):
        if not isinstance(row[name], list):
            raise ValueError("campaign splits and seeds require lists")
    spec = CampaignSpec(
        row["hypothesis"],
        tuple(period_from_dict(p) for p in row["train"]),
        tuple(period_from_dict(p) for p in row["validation"]),
        tuple(period_from_dict(p) for p in row["holdout"]),
        row["code_sha"],
        row["lock_sha"],
        tuple(proposal_from_dict(p) for p in row["seeds"]),
        **kwargs,
    )
    expected = spec.to_dict()
    if any(key in row and row[key] != expected[key] for key in derived):
        raise ValueError("derived campaign semantics cannot be overridden")
    return spec


class Glm53Proposer:
    identity = "trex-zai/glm-5.3/bounded-config-reflection/1"
    last_receipt: dict[str, Any] | None = None

    def __call__(self, feedback: dict[str, Any]) -> list[Proposal]:
        self.last_receipt = None
        from tree_options.trex.discovery.llm import LlmError, chat_json, urllib_post

        def verified_transport(
            url: str, body: bytes, headers: dict[str, str], timeout: float
        ) -> tuple[int, bytes]:
            status, raw = urllib_post(url, body, headers, timeout)
            if status == 200:
                try:
                    envelope = decode_json(raw)
                    if envelope.get("model") != "glm-5.3":
                        raise LlmError("requested GLM-5.3 model not proven")
                    identifier = envelope.get("id")
                    if not isinstance(identifier, str) or not identifier:
                        raise LlmError("model response identity unavailable")
                    content = envelope["choices"][0]["message"]["content"]
                    if not isinstance(content, str) or not isinstance(decode_json(content), dict):
                        raise LlmError("strict proposal object required")
                    self.last_receipt = {
                        "requested_model": "glm-5.3",
                        "returned_model": "glm-5.3",
                        "response_id": identifier,
                        "response_sha256": hashlib.sha256(raw).hexdigest(),
                    }
                except (ValueError, TypeError, AttributeError, KeyError, IndexError):
                    raise LlmError("model receipt unavailable") from None
            return status, raw

        response, model = chat_json(
            "zai",
            [
                {
                    "role": "system",
                    "content": 'Reflect on TRAINING mechanical results only. Propose restricted registered strategy configurations, never code, risk overrides, execution commands or claimed metrics. Return JSON {"proposals": [{"strategy_id": "one allowed strategy", "parameters": {"top_n": 1}, "rationale": "bounded hypothesis"}]}. Empty parameters retain all eligible members. Use at most remaining_candidates. All metrics are supplied by mechanical replay; optimize the registered mean next-session return with endpoint-loss and turnover penalties. This is exploratory; costs and ledger rules are fixed.',
                },
                {"role": "user", "content": json.dumps(feedback, sort_keys=True, allow_nan=False)},
            ],
            model="glm-5.3",
            transport=verified_transport,
            timeout=240,
            extra={"max_tokens": 4000, "thinking": {"type": "enabled"}, "reasoning_effort": "low"},
        )
        if (
            model != "glm-5.3"
            or not isinstance(response.get("proposals"), list)
            or set(response) != {"proposals"}
        ):
            raise ValueError("malformed reflection proposals")
        return [proposal_from_dict(p) for p in response["proposals"]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="TREX bounded theory campaign; broker-free research"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    fixture = sub.add_parser(
        "make-fixture", help="Write an offline synthetic example; never overwrite"
    )
    fixture.add_argument("--output", type=Path, required=True)
    fixture.add_argument("--calendar", type=Path, required=True)
    fixture.add_argument("--calendar-checksum", type=Path, required=True)
    run = sub.add_parser("run")
    run.add_argument("--input", type=Path, required=True)
    run.add_argument("--workspace", type=Path, required=True)
    run.add_argument("--calendar", type=Path, required=True)
    run.add_argument("--calendar-checksum", type=Path, required=True)
    run.add_argument(
        "--reflect-glm53",
        action="store_true",
        help="Explicitly permit bounded server-side GLM-5.3 research calls",
    )
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            if not (args.workspace / "runstate.sqlite3").is_file():
                raise ValueError("campaign workspace missing")
            with open_runstate_store(args.workspace) as store:
                store.verify()
                result = {
                    "binding": store.get("spec", "quant-campaign-binding"),
                    "results": [
                        r for r in store.all("result") if r.get("schema") == "quant-theory-result/1"
                    ],
                }
        else:
            root = Path(__file__).resolve().parents[3]
            actual_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip()
            actual_lock = hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest()
            calendar = StaticSessionCalendar(args.calendar, args.calendar_checksum)
            if args.command == "make-fixture":
                fixture_spec = make_fixture(calendar, actual_sha, actual_lock)
                with args.output.open("x") as output:
                    output.write(
                        json.dumps(fixture_spec.to_dict(), indent=2, sort_keys=True) + "\n"
                    )
                print(
                    json.dumps(
                        {
                            "data_class": "synthetic_fixture",
                            "output": str(args.output),
                            "execution_authorized": False,
                            "live_money": False,
                        }
                    )
                )
                return 0
            spec = spec_from_dict(strict_json(args.input))
            if spec.code_sha != actual_sha or spec.lock_sha != actual_lock:
                raise ValueError("input code/lock identity differs from current checkout")
            if subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=root, text=True
            ).strip():
                raise ValueError("campaign CLI requires clean source custody")
            reflector = Glm53Proposer() if args.reflect_glm53 else None
            result = run_campaign(
                args.workspace,
                spec,
                calendar,
                proposer=reflector,
                proposer_identity=reflector.identity if reflector else "none",
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RuntimeError,
        ArithmeticError,
        subprocess.SubprocessError,
    ) as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "reason": type(exc).__name__,
                    "detail": str(exc),
                    "execution_authorized": False,
                    "live_money": False,
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

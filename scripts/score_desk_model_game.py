#!/usr/bin/env python3
"""Score three frozen model selections against sealed historical outcomes."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tree_options.desk.model_game import prepare, score  # noqa: E402

MODELS = {"zai": "glm-5.3", "flash": "glm-5.3-flash", "minimax": "MiniMax-M3"}


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _checked_bytes(path: Path, expected: str) -> bytes:
    raw = path.read_bytes()
    if _sha(raw) != expected:
        raise ValueError(f"hash mismatch: {path.name}")
    return raw


def _model_result(game_dir: Path, name: str, model: str, prompt_sha: str,
                  hidden: dict[str, dict[str, Any]]) -> dict[str, Any]:
    receipt = json.loads((game_dir / f"{name}.receipt.json").read_text())
    if (receipt.get("requested_model") != model or receipt.get("exit_code") != 0
            or receipt.get("is_error") is not False or model not in receipt.get("modelUsage_keys", [])
            or receipt.get("prompt", {}).get("sha256") != prompt_sha):
        raise ValueError(f"{name}: provider receipt incomplete or inconsistent")
    raw = _checked_bytes(game_dir / f"{name}.raw.json", receipt["raw"]["sha256"])
    response = json.loads(raw)
    if not isinstance(response.get("result"), str) or response.get("is_error") is not False:
        raise ValueError(f"{name}: raw response invalid")
    proposal_raw = _checked_bytes(game_dir / f"{name}.proposal.json",
                                  receipt["proposal"]["sha256"])
    proposal = json.loads(proposal_raw)
    result_text = response["result"].strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", result_text, re.DOTALL)
    parsed_result = json.loads(fence.group(1) if fence else result_text)
    if proposal != parsed_result:
        raise ValueError(f"{name}: proposal differs from raw provider result")
    if not isinstance(proposal, dict) or not isinstance(proposal.get("selected_ids"), list):
        raise ValueError(f"{name}: malformed selection")
    if any(not isinstance(item, str) for item in proposal["selected_ids"]):
        raise ValueError(f"{name}: selection IDs must be strings")
    return {"model": model, "raw_sha256": _sha(raw),
            "proposal_sha256": _sha(proposal_raw),
            "selected_ids": proposal["selected_ids"],
            "policy": proposal.get("policy"), "caveats": proposal.get("caveats"),
            "score": score(hidden, proposal["selected_ids"])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-dir", required=True, type=Path)
    args = parser.parse_args()
    game_dir = args.game_dir.resolve(strict=True)
    manifest = json.loads((game_dir / "manifest.json").read_text())
    source = _checked_bytes(Path(manifest["source_path"]), manifest["source_sha256"])
    prompt = _checked_bytes(game_dir / "prompt.txt", manifest["prompt_sha256"])
    packet, hidden = prepare(json.loads(source))
    if json.dumps(packet, sort_keys=True, indent=2).encode() not in prompt:
        raise ValueError("prompt does not contain recomputed blind packet")
    sealed = json.loads((game_dir / "sealed.json").read_text())
    if sealed.get("hidden") != hidden:
        raise ValueError("sealed outcomes differ from source")
    if manifest.get("baseline") != score(hidden, list(hidden)):
        raise ValueError("baseline drift")
    results = {name: _model_result(game_dir, name, model, manifest["prompt_sha256"], hidden)
               for name, model in MODELS.items()}
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                          text=True, capture_output=True).stdout.strip()
    report = {
        "schema": "desk-model-game-scoreboard/1", "source_sha256": manifest["source_sha256"],
        "prompt_sha256": manifest["prompt_sha256"], "code_head": head,
        "training_rows": len(packet["training"]), "blind_rows": len(hidden),
        "baseline_accept_all": manifest["baseline"], "models": results,
        "limitations": [
            "only 13 blind evaluable rows from a selected and incomplete daily-VWAP cache",
            "training and blind periods were split once; model selection on this holdout would invalidate it",
            "candidate features lack full point-in-time volatility/quote state",
            "pseudonyms reduce but cannot prove absence of historical-event recall",
            "daily loss stop, actual fills, margin, assignment and future performance are untested",
        ],
        "execution_authorized": False,
    }
    path = game_dir / "scoreboard.json"
    with path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({"scoreboard": str(path), "baseline": report["baseline_accept_all"],
                      "models": {name: result["score"] for name, result in results.items()}},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

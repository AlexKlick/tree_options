#!/usr/bin/env python3
"""Ask three provider models for one sealed, as-of historical choice each."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tree_options.desk.intraday_action_graph import decision_packet  # noqa: E402

MODELS = {
    "zai": ("/home/alexk/.local/bin/claude-zai", "glm-5.3"),
    "flash": ("/home/alexk/.local/bin/claude-zai", "glm-5.3-flash"),
    "minimax": ("/home/alexk/.local/bin/claude-minimax2", "MiniMax-M3"),
}


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _proposal(result: str) -> dict[str, Any]:
    clean = result.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", clean, re.DOTALL)
    parsed = json.loads(fence.group(1) if fence else clean)
    if not isinstance(parsed, dict) or set(parsed) - {"selected_id", "reason"}:
        raise ValueError("proposal must contain only selected_id and reason")
    if not isinstance(parsed.get("reason"), str) or not isinstance(parsed.get("selected_id"), (str, type(None))):
        raise ValueError("proposal fields malformed")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--day", type=date.fromisoformat, required=True)
    parser.add_argument("--clock", default="10:00")
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.out_dir.exists():
        parser.error("output directory exists; preserve immutable pilot receipts")
    source = args.bundle.read_bytes()
    packet = decision_packet(json.loads(source), args.day, args.clock)
    aliases = {name: f"asset-{i:02d}" for i, name in
               enumerate(sorted({c["underlying"] for c in packet["candidates"]}), 1)}
    candidates = [{"id": c["id"], "asset_alias": aliases[c["underlying"]],
                   "structure": c["structure"],
                   "dte": (date.fromisoformat(c["expiry"])-args.day).days,
                   "width": c["width"], "observed_premium": c["observed_premium"],
                   "max_loss_proxy": c["max_loss_proxy"], "max_gain_proxy": c["max_gain_proxy"],
                   "reward_to_risk_proxy": c["reward_to_risk_proxy"],
                   "long_recent_trade_move": c["long_recent_trade_move"],
                   "short_recent_trade_move": c["short_recent_trade_move"]}
                  for c in packet["candidates"]]
    blind = {"schema": "desk-intraday-model-pilot/1", "point_index": 1,
             "candidates": candidates, "capital_profile": packet["capital_profile"],
             "data_kind": packet["data_kind"], "execution_authorized": False}
    prompt = ("Choose at most one historical option spread candidate, or skip. "
              "Favor controlled risk and small repeatable gains; do not infer a win probability "
              "from trade prices or claim executable fills. You see only this as-of packet. "
              "Return exactly JSON {\"selected_id\": string_or_null, \"reason\": string}.\n"
              + json.dumps(blind, indent=2, sort_keys=True))
    args.out_dir.mkdir(parents=True)
    (args.out_dir / "prompt.txt").write_text(prompt)
    prompt_sha = sha(prompt.encode())
    manifest = {"schema": "desk-intraday-model-pilot-receipts/1", "snapshot_id": packet["snapshot_id"],
                "bundle_sha256": sha(source), "prompt_sha256": prompt_sha,
                "candidate_ids": [c["id"] for c in candidates], "models": {},
                "execution_authorized": False}
    for alias, (executable, model) in MODELS.items():
        cmd = [executable, "--model", model, "-p", prompt, "--tools", "",
               "--output-format", "json", "--no-session-persistence",
               "--max-turns", "1", "--max-budget-usd", "0.30"]
        try:
            response = subprocess.run(cmd, cwd=ROOT, capture_output=True, timeout=240)
            raw, stderr, rc = response.stdout, response.stderr, response.returncode
        except subprocess.TimeoutExpired as exc:
            raw, stderr, rc = exc.stdout or b"", exc.stderr or b"", 124
        (args.out_dir / f"{alias}.raw.json").write_bytes(raw)
        (args.out_dir / f"{alias}.stderr.log").write_bytes(stderr)
        receipt: dict[str, Any] = {"model": model, "exit_code": rc,
                                   "raw_sha256": sha(raw), "stderr_sha256": sha(stderr),
                                   "prompt_sha256": prompt_sha}
        try:
            envelope = json.loads(raw)
            receipt["reported_cost_usd"] = envelope.get("total_cost_usd")
            if rc != 0 or envelope.get("is_error") is not False or model not in envelope.get("modelUsage", {}):
                raise ValueError("provider envelope did not prove requested successful model")
            proposal = _proposal(envelope["result"])
            if proposal["selected_id"] not in (*manifest["candidate_ids"], None):
                raise ValueError("unknown selected candidate")
            (args.out_dir / f"{alias}.proposal.json").write_text(json.dumps(proposal, indent=2) + "\n")
            receipt["selected_id"] = proposal["selected_id"]
            receipt["status"] = "valid"
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            receipt["status"] = "invalid"
            receipt["error"] = str(exc)
        manifest["models"][alias] = receipt
        (args.out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        print(json.dumps({"model": model, "status": receipt["status"],
                          "selected_id": receipt.get("selected_id"),
                          "reported_cost_usd": receipt.get("reported_cost_usd")}, sort_keys=True), flush=True)
    return 0 if all(m["status"] == "valid" for m in manifest["models"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

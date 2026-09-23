"""LLM watchlist proposals (M6): the model PROPOSES, the operator decides.

OpenAI-compatible chat over stdlib urllib against a provider CHAIN tried
in order (config ``llm_provider = "local,minimax,zai"``): the loopback
Qwen text-main lane first (no quota, no key), hosted providers as
fallback, the contended Z.AI coding plan last.

Key discipline: keys are read from environment variables named HERE and
nowhere else; they never appear in config, exceptions, logs, or
artifacts. Only provider + model names are echoed.

Output discipline: the model's reply is untrusted text. It is parsed by
a fence/think-tag stripping, string-aware brace extractor, normalized
(symbol regex, action enum, rationale <= 140 chars, confidence clamped),
filtered against the watchlist and the blocked set, and capped. Malformed
output yields a failure note and changes nothing.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from tree_options.trex.discovery.watchlist import SYMBOL_RE

REQUEST_TIMEOUT = 20.0
MAX_TOKENS = 900
RATIONALE_MAX = 140

PROVIDERS: dict[str, dict[str, Any]] = {
    "local": {
        "base_url": "http://127.0.0.1:18000/v1",
        "model": "Qwen/Qwen3.8-27B",
        "key_env": None,
        # llama.cpp: skip the reasoning pass for a short structured reply
        "extra": {"chat_template_kwargs": {"enable_thinking": False}},
    },
    # Hosted keys: the claude-zai / claude-minimax2 launcher names in
    # ~/.claude/.env first (the unit loads that file), older names after.
    "zai": {
        "base_url": os.environ.get(
            "ZAI_OPENAI_BASE_URL", "https://api.z.ai/api/coding/paas/v4"
        ),
        "model": "glm-5.3-flash",
        "key_env": ("ANTHROPIC_AUTH_TOKEN_ZAI", "ZAI_CODING_API_KEY"),
        "extra": {},
    },
    "minimax": {
        "base_url": "https://api.minimax.io/v1",
        "model": "MiniMax-M3",
        "key_env": ("ANTHROPIC_AUTH_TOKEN_MINIMAX2", "MINIMAX_API_KEY"),
        "extra": {},
    },
}

# (url, body, headers, timeout) -> (status, body)
PostTransport = Callable[[str, bytes, dict[str, str], float], tuple[int, bytes]]


class LlmError(RuntimeError):
    """Provider/parse failure. Messages never carry keys or raw bodies."""


def urllib_post(
    url: str, body: bytes, headers: dict[str, str], timeout: float
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(2_000_000)
    except urllib.error.HTTPError as exc:
        return exc.code, b""


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _extract_json_object(text: str) -> dict[str, Any]:
    """First parseable top-level JSON object in model output.

    Strips <think> blocks and code fences, then scans brace-balanced and
    string-aware (braces inside strings never close a span); a span that
    fails to parse moves the scan to the next '{'.
    """
    cleaned = _THINK_RE.sub("", text or "")
    cleaned = re.sub(r"```(?:json)?", "", cleaned)
    start = cleaned.find("{")
    while start != -1:
        depth, in_str, escaped = 0, False, False
        resume = start + 1  # unbalanced span: retry from the next brace
        for i in range(start, len(cleaned)):
            ch = cleaned[i]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(cleaned[start : i + 1])
                    except (json.JSONDecodeError, RecursionError):
                        obj = None
                    if isinstance(obj, dict):
                        return obj
                    resume = i + 1  # skip the whole rejected span
                    break
        start = cleaned.find("{", resume)
    raise LlmError("no JSON object in model output")


def chat_json(
    provider: str,
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    transport: PostTransport = urllib_post,
    timeout: float = REQUEST_TIMEOUT,
) -> tuple[dict[str, Any], str]:
    """One chat completion parsed to a JSON object -> (object, model used)."""
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise LlmError(f"unknown provider {provider!r}")
    headers = {"Content-Type": "application/json"}
    key_envs: tuple[str, ...] = spec["key_env"] or ()
    if key_envs:
        found = next(
            ((name, os.environ[name].strip()) for name in key_envs
             if os.environ.get(name, "").strip()),
            None,
        )
        if found is None:
            raise LlmError(f"{provider}: {' or '.join(key_envs)} not set")
        key_env, key = found
        if any(c.isspace() or ord(c) < 32 or ord(c) > 126 for c in key):
            raise LlmError(f"{provider}: {key_env} is malformed (whitespace/control chars)")
        headers["Authorization"] = f"Bearer {key}"
    used_model = model or spec["model"]
    body = json.dumps(
        {
            "model": used_model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": MAX_TOKENS,
            **spec["extra"],
        }
    ).encode()
    try:
        status, raw = transport(
            f"{spec['base_url']}/chat/completions", body, headers, timeout
        )
    except Exception as exc:  # never re-raise: messages can embed headers
        raise LlmError(f"{provider}: {type(exc).__name__}") from None
    if status != 200:
        raise LlmError(f"{provider}: HTTP {status}")
    try:
        content = json.loads(raw)["choices"][0]["message"].get("content") or ""
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise LlmError(f"{provider}: malformed completion envelope") from None
    return _extract_json_object(str(content)), used_model


SYSTEM_PROMPT = (
    "You assist a paper-trading options cockpit. Its scanner looks for "
    "put-debit-spread opportunities (bearish or hedging structures) on a "
    "watchlist of US stocks and index ETFs. Suggest changes to that "
    "watchlist: ADD liquid, optionable US-listed tickers worth scanning, or "
    "REMOVE watched tickers that no longer fit. Use only the context given; "
    "never invent prices. Reply with ONLY a JSON object, no prose: "
    '{"proposals":[{"symbol":"XYZ","action":"add"|"remove",'
    '"rationale":"<=140 chars","confidence":0.0-1.0}]}. '
    "Return at most {max_n} proposals; an empty list is a fine answer."
)


def normalize(
    raw: dict[str, Any],
    *,
    watched: set[str],
    blocked: set[str],
    max_n: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Untrusted model JSON -> vetted proposals + drop notes."""
    items = raw.get("proposals")
    if not isinstance(items, list):
        return [], ["model reply had no proposals list"]
    out: list[dict[str, Any]] = []
    notes: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        sym = str(item.get("symbol", "")).strip().upper()
        action = str(item.get("action", "")).strip().lower()
        if not SYMBOL_RE.match(sym):
            notes.append(f"dropped malformed symbol {sym[:12]!r}")
            continue
        if action not in ("add", "remove"):
            notes.append(f"{sym}: dropped unknown action {action[:12]!r}")
            continue
        if sym in seen:
            continue
        if sym in blocked:
            notes.append(f"{sym}: skipped (pending or recently dismissed)")
            continue
        if action == "add" and sym in watched:
            continue
        if action == "remove" and sym not in watched:
            continue
        try:
            conf = float(item.get("confidence", 0.5))
        except (TypeError, ValueError, OverflowError):
            conf = 0.5
        conf = min(1.0, max(0.0, conf)) if conf == conf else 0.5  # NaN -> 0.5
        rationale = " ".join(str(item.get("rationale", "")).split())[:RATIONALE_MAX]
        out.append(
            {"symbol": sym, "action": action, "rationale": rationale, "confidence": conf}
        )
        seen.add(sym)
        if len(out) >= max_n:
            break
    return out, notes


def propose(
    chain: list[str],
    context: dict[str, Any],
    *,
    watched: set[str],
    blocked: set[str],
    max_n: int,
    model_override: str = "",
    transport: PostTransport = urllib_post,
) -> dict[str, Any]:
    """Try each provider in order; the first parseable reply wins.

    Returns {proposals, notes, provider, model, elapsed_s, status} where
    status is "ok" | "failed"; a failed run proposes nothing.
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.replace("{max_n}", str(max_n))},
        {"role": "user", "content": json.dumps(context, separators=(",", ":"))},
    ]
    notes: list[str] = []
    for i, provider in enumerate(chain):
        if provider == "none":
            break
        t0 = time.monotonic()
        try:
            raw, used_model = chat_json(
                provider,
                messages,
                model=(model_override or None) if i == 0 else None,
                transport=transport,
            )
        except LlmError as exc:
            notes.append(str(exc))
            continue
        try:
            proposals, drop_notes = normalize(
                raw, watched=watched, blocked=blocked, max_n=max_n
            )
        except Exception as exc:  # untrusted shape: fall through to the next
            notes.append(f"{provider}: unusable proposals ({type(exc).__name__})")
            continue
        return {
            "status": "ok",
            "provider": provider,
            "model": used_model,
            "elapsed_s": round(time.monotonic() - t0, 2),
            "proposals": proposals,
            "notes": notes + drop_notes,
        }
    return {
        "status": "failed",
        "provider": None,
        "model": None,
        "elapsed_s": None,
        "proposals": [],
        "notes": notes or ["llm proposals disabled"],
    }

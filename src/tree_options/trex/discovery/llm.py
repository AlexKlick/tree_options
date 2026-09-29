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
# The single self-healing retry a desk caller makes on a truncated reply:
# double the effective budget, capped (an M3.1-Flash finish_reason=length at
# max effort usually means the always-on thinking ate the window).
TRUNCATED_NOTE = "reply truncated at max_tokens"
ESCALATE_TOKENS_CAP = 48_000
ESCALATE_TIMEOUT_CAP = 900.0

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
        # same reason as local: with thinking the reply ran 16-20s+
        # (past REQUEST_TIMEOUT); without it 5.2s (live 2026-09-23)
        "extra": {"thinking": {"type": "disabled"}},
    },
    # MiniMax-M3.1-Flash-Preview replaced MiniMax-M3 here on 2026-09-28
    # (operator: "switch literally everything from m3 to m3.1"). M3.1 always
    # thinks: never send thinking disabled or effort "none" (HTTP 400). The
    # effort is explicit: "high" for this judgement lane (discovery
    # proposals, model:minimax board choices). Its reasoning arrives in
    # reasoning_content, not inline; the content is the JSON (the <think>
    # stripper stays for older replies). Live 2026-09-28 on a discovery-
    # shaped prompt: effort high 8 s / 336 completion tokens, max 14 s /
    # 706, so 4000 tokens / 45 s keeps the old chain bound (local 20 +
    # minimax 45 + zai 20 = 85 s, at most every few hours).
    "minimax": {
        "base_url": "https://api.minimax.io/v1",
        "model": "MiniMax-M3.1-Flash-Preview",
        "key_env": ("ANTHROPIC_AUTH_TOKEN_MINIMAX2", "MINIMAX_API_KEY"),
        "max_tokens": 4000,
        "timeout": 45.0,
        "extra": {"reasoning_effort": "high"},
        "verify_model": True,
    },
    # L7's overnight reflection + long-run lane: the same model at effort
    # "max" (M3.1's server default, now sent explicitly so the lane's
    # behaviour does not move with this change). A finish_reason=length
    # reply is still a failure in chat_json (never a partial proposal).
    # The v2 board's richer context lengthens the always-on thinking: at
    # 4000 tokens / 60 s the first live v2 run (20260929T012836Z) lost 13
    # of 126 calls to finish_reason=length and 6 to timeouts (p90 latency
    # 49 s), so the budget is 12000 tokens / 120 s.
    "minimax-flash": {
        "base_url": "https://api.minimax.io/v1",
        "model": "MiniMax-M3.1-Flash-Preview",
        "key_env": ("ANTHROPIC_AUTH_TOKEN_MINIMAX2", "MINIMAX_API_KEY"),
        "max_tokens": 12000,
        "timeout": 120.0,
        "extra": {"reasoning_effort": "max"},
        "verify_model": True,
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
    timeout: float | None = None,
    extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """One chat completion parsed to a JSON object -> (object, model used).
    ``extra`` adds per-call body fields (e.g. M3.1-Flash ``reasoning_effort``,
    which defaults to max) over the provider's own."""
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise LlmError(f"unknown provider {provider!r}")
    if timeout is None:
        timeout = float(spec.get("timeout", REQUEST_TIMEOUT))
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
            "max_tokens": spec.get("max_tokens", MAX_TOKENS),
            **spec["extra"],
            **(extra or {}),
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
        envelope = json.loads(raw)
        choice = envelope["choices"][0]
        content = choice["message"].get("content") or ""
        finish = choice.get("finish_reason")
        served = envelope.get("model")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise LlmError(f"{provider}: malformed completion envelope") from None
    # MiniMax answers an unknown model id with HTTP 200 served by another
    # model, so a 200 proves nothing: the envelope's model must echo the
    # requested id (M3.1-Flash is not listed in GET /v1/models).
    if spec.get("verify_model") and served is not None and served != used_model:
        raise LlmError(
            f"{provider}: served model {str(served)[:60]!r} != requested {used_model!r}"
        )
    if finish == "length":  # a cut-off list can still hold a parseable inner object
        raise LlmError(f"{provider}: {TRUNCATED_NOTE}")
    try:
        return _extract_json_object(str(content)), used_model
    except LlmError as exc:
        raise LlmError(f"{provider}: {exc}") from None


def escalation_budget(
    provider: str, *, max_tokens: int | None, timeout: float | None,
    extra: dict[str, Any] | None,
) -> tuple[dict[str, Any], float, int]:
    """The budget of the ONE retry a caller makes after TRUNCATED_NOTE:
    double the call's effective starting budget - its own override, else the
    provider spec's - capped at 48000 tokens / 900 s. Returns the retry's
    (extra body fields, timeout, max_tokens); the caller's other extra
    fields (e.g. reasoning_effort) are preserved and max_tokens is set."""
    spec = PROVIDERS.get(provider, {})
    start_tokens = int(max_tokens if max_tokens is not None
                       else spec.get("max_tokens", MAX_TOKENS))
    start_timeout = float(timeout if timeout is not None
                          else spec.get("timeout", REQUEST_TIMEOUT))
    tokens = min(2 * start_tokens, ESCALATE_TOKENS_CAP)
    seconds = min(2 * start_timeout, ESCALATE_TIMEOUT_CAP)
    return {**(extra or {}), "max_tokens": tokens}, seconds, tokens


#: per-call ``extra`` keys that are safe on ANY provider (pure budgets);
#: everything else (e.g. minimax reasoning_effort) is primary-provider-only
GENERIC_EXTRA_KEYS = frozenset({"max_tokens"})


def ask_json(
    provider: str, messages: list[dict[str, str]], *, fallback: str | None = None,
    transport: PostTransport | None = None, extra: dict[str, Any] | None = None,
    timeout: float | None = None, max_tokens: int | None = None,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """chat_json for one decision with the two self-heals the desk lanes share:

    1. ONE escalating retry when the reply is truncated (escalation_budget);
    2. when ``fallback`` names a different provider, ONE attempt on it after
       the primary's final failure (any LlmError: timeout, HTTP, truncation
       that survived its escalation). The fallback call carries only generic
       per-call fields (max_tokens/timeout) - never the primary's
       provider-specific extras such as reasoning_effort.

    Returns (reply, model, meta); ``meta`` records who answered and how -
    ``{"provider": name}`` always, plus ``escalated``/``max_tokens``/``timeout``
    when the escalation fired and ``fallback: True`` when the backup answered -
    so receipts never hide which model made a call. A failure on both providers
    raises the fallback's LlmError (the primary's when no fallback applies)."""
    base: dict[str, Any] = {}
    if transport is not None:
        base["transport"] = transport

    def call(name: str, call_extra: dict[str, Any] | None
             ) -> tuple[dict[str, Any], str, dict[str, Any]]:
        kwargs = dict(base)
        if call_extra:
            kwargs["extra"] = dict(call_extra)
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            reply, model = chat_json(name, messages, **kwargs)
            return reply, model, {}
        except LlmError as error:  # one escalating retry, then the failure stands
            if TRUNCATED_NOTE not in str(error):
                raise
            retry_extra, seconds, tokens = escalation_budget(
                name, max_tokens=max_tokens, timeout=timeout, extra=call_extra)
            reply, model = chat_json(
                name, messages, **{**kwargs, "extra": retry_extra, "timeout": seconds})
            return reply, model, {"escalated": True, "max_tokens": tokens,
                                  "timeout": seconds}

    try:
        reply, model, meta = call(provider, extra)
        return reply, model, {**meta, "provider": provider}
    except LlmError:
        if not fallback or fallback == provider:
            raise
        backup_extra = {key: value for key, value in (extra or {}).items()
                        if key in GENERIC_EXTRA_KEYS}
        reply, model, meta = call(fallback, backup_extra or None)
        return reply, model, {**meta, "provider": fallback, "fallback": True}


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

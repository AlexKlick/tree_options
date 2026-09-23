"""M6: LLM watchlist proposals - untrusted-output parsing, normalization,
key discipline, and the provider chain (fake transports only)."""

from __future__ import annotations

import json

import pytest

from tree_options.trex.discovery.llm import (
    LlmError,
    _extract_json_object,
    chat_json,
    normalize,
    propose,
)

SECRET = "sk-test-SECRET-value-123"
KEY_ENVS = (
    "ANTHROPIC_AUTH_TOKEN_ZAI",
    "ZAI_CODING_API_KEY",
    "ANTHROPIC_AUTH_TOKEN_MINIMAX2",
    "MINIMAX_API_KEY",
)


@pytest.fixture(autouse=True)
def _no_host_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator's shell exports the real keys; none may reach a test."""
    for name in KEY_ENVS:
        monkeypatch.delenv(name, raising=False)


def _completion(content: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


class FakeTransport:
    def __init__(self, replies: list[tuple[int, bytes] | Exception]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, dict, dict]] = []
        self.timeouts: list[float] = []

    def __call__(self, url: str, body: bytes, headers: dict, timeout: float):
        self.calls.append((url, json.loads(body), dict(headers)))
        self.timeouts.append(timeout)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class TestExtractor:
    def test_plain_object(self) -> None:
        assert _extract_json_object('{"a": 1}') == {"a": 1}

    def test_fenced_with_prose(self) -> None:
        text = 'Sure!\n```json\n{"proposals": []}\n```\nHope that helps.'
        assert _extract_json_object(text) == {"proposals": []}

    def test_think_block_with_braces_is_ignored(self) -> None:
        text = '<think>maybe {"proposals": ["x"]} no</think>{"proposals": [1]}'
        assert _extract_json_object(text) == {"proposals": [1]}

    def test_braces_inside_strings_do_not_close(self) -> None:
        text = 'x {"rationale": "a } tricky { string", "n": 2} y'
        assert _extract_json_object(text) == {"rationale": "a } tricky { string", "n": 2}

    def test_invalid_first_span_then_valid(self) -> None:
        assert _extract_json_object('{nope} then {"ok": true}') == {"ok": True}

    def test_junk_raises(self) -> None:
        with pytest.raises(LlmError):
            _extract_json_object("no json here at all")


class TestNormalize:
    def test_vets_every_field(self) -> None:
        raw = {
            "proposals": [
                {"symbol": " tsm ", "action": "ADD", "rationale": "x" * 300, "confidence": 5},
                {"symbol": "TSM", "action": "add", "confidence": 0.1},  # dup
                {"symbol": "not a ticker", "action": "add"},
                {"symbol": "AMD", "action": "short"},  # bad action
                {"symbol": "SPY", "action": "add"},  # already watched
                {"symbol": "IWM", "action": "remove"},  # not watched
                {"symbol": "QQQ", "action": "remove", "confidence": -2},
                {"symbol": "XLE", "action": "add"},  # blocked
                {"symbol": "SMH", "action": "add", "confidence": "high"},
            ]
        }
        out, notes = normalize(raw, watched={"SPY", "QQQ"}, blocked={"XLE"}, max_n=5)
        assert [(p["symbol"], p["action"]) for p in out] == [
            ("TSM", "add"), ("QQQ", "remove"), ("SMH", "add")
        ]
        assert len(out[0]["rationale"]) == 140
        assert out[0]["confidence"] == 1.0
        assert out[1]["confidence"] == 0.0
        assert out[2]["confidence"] == 0.5
        assert any("XLE" in n for n in notes)
        assert any("malformed" in n for n in notes)

    def test_cap_and_missing_list(self) -> None:
        raw = {"proposals": [{"symbol": s, "action": "add"} for s in ["A", "B", "C", "D"]]}
        out, _ = normalize(raw, watched=set(), blocked=set(), max_n=2)
        assert len(out) == 2
        assert normalize({"ideas": []}, watched=set(), blocked=set(), max_n=2)[0] == []


class TestChatJson:
    def test_local_needs_no_key_and_disables_thinking(self) -> None:
        t = FakeTransport([(200, _completion('{"proposals": []}'))])
        obj, model = chat_json("local", [{"role": "user", "content": "hi"}], transport=t)
        assert obj == {"proposals": []}
        assert model == "Qwen/Qwen3.8-27B"
        url, body, headers = t.calls[0]
        assert url == "http://127.0.0.1:18000/v1/chat/completions"
        assert "Authorization" not in headers
        assert body["chat_template_kwargs"] == {"enable_thinking": False}

    def test_hosted_key_from_env_never_leaks_into_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ZAI_CODING_API_KEY", SECRET)
        t = FakeTransport([(500, b'{"error": "boom ' + SECRET.encode() + b'"}')])
        with pytest.raises(LlmError) as exc:
            chat_json("zai", [{"role": "user", "content": "hi"}], transport=t)
        assert SECRET not in str(exc.value)
        assert t.calls[0][2]["Authorization"] == f"Bearer {SECRET}"

    def test_missing_key_names_the_env_var_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        with pytest.raises(LlmError, match="MINIMAX_API_KEY not set"):
            chat_json("minimax", [], transport=FakeTransport([]))

    def test_timeout_is_an_llm_error(self) -> None:
        t = FakeTransport([TimeoutError("slow")])
        with pytest.raises(LlmError, match="TimeoutError"):
            chat_json("local", [], transport=t)

    def test_model_override(self) -> None:
        t = FakeTransport([(200, _completion("{}"))])
        _, model = chat_json("local", [], model="other/model", transport=t)
        assert model == "other/model" and t.calls[0][1]["model"] == "other/model"


class TestLauncherKeys:
    """Hosted lanes reuse the claude-zai / claude-minimax2 launcher keys
    (the ~/.claude/.env names); the older names stay as fallbacks."""

    def test_zai_prefers_the_claude_zai_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_ZAI", SECRET)
        monkeypatch.setenv("ZAI_CODING_API_KEY", "sk-older-key")
        t = FakeTransport([(200, _completion("{}"))])
        chat_json("zai", [], transport=t)
        assert t.calls[0][2]["Authorization"] == f"Bearer {SECRET}"

    def test_zai_disables_thinking(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # live 2026-09-23: 16-20s+ with thinking (past REQUEST_TIMEOUT), 5.2s without
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_ZAI", SECRET)
        t = FakeTransport([(200, _completion("{}"))])
        chat_json("zai", [], transport=t)
        assert t.calls[0][1]["thinking"] == {"type": "disabled"}

    def test_zai_falls_back_to_the_coding_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_ZAI", "  ")
        monkeypatch.setenv("ZAI_CODING_API_KEY", SECRET)
        t = FakeTransport([(200, _completion("{}"))])
        chat_json("zai", [], transport=t)
        assert t.calls[0][2]["Authorization"] == f"Bearer {SECRET}"

    def test_minimax_uses_the_claude_minimax2_key_and_m3(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_MINIMAX2", SECRET)
        monkeypatch.setenv("MINIMAX_API_KEY", "sk-alias-key")
        t = FakeTransport([(200, _completion('<think>plan</think>{"proposals": []}'))])
        obj, model = chat_json("minimax", [], transport=t)
        url, body, headers = t.calls[0]
        assert obj == {"proposals": []}
        assert headers["Authorization"] == f"Bearer {SECRET}"
        assert model == "MiniMax-M3" and body["model"] == "MiniMax-M3"
        assert url == "https://api.minimax.io/v1/chat/completions"

    def test_minimax_has_room_to_think(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # live 2026-09-23: M3's inline <think> plus the JSON hit 900 tokens
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_MINIMAX2", SECRET)
        t = FakeTransport([(200, _completion("{}")), (200, _completion("{}"))])
        chat_json("minimax", [], transport=t)
        chat_json("local", [], transport=t)
        assert t.calls[0][1]["max_tokens"] >= 3000
        assert t.calls[1][1]["max_tokens"] == 900
        # reasoning takes 6-20s+ live; the others keep the 20s bound
        assert t.timeouts == [45.0, 20.0]

    def test_explicit_timeout_still_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_MINIMAX2", SECRET)
        t = FakeTransport([(200, _completion("{}"))])
        chat_json("minimax", [], transport=t, timeout=5.0)
        assert t.timeouts == [5.0]

    def test_truncated_reply_falls_through_to_the_next_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # a cut-off list still holds a parseable INNER object; it must
        # never pass as an "ok" run with zero proposals
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_MINIMAX2", SECRET)
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_ZAI", SECRET)
        cut = '<think>x</think>{"proposals":[{"symbol":"TSM","action":"add"},{"symbol":"AA'
        truncated = json.dumps(
            {"choices": [{"message": {"content": cut}, "finish_reason": "length"}]}
        ).encode()
        good = '{"proposals": [{"symbol": "XLF", "action": "add", "rationale": "banks"}]}'
        t = FakeTransport([(200, truncated), (200, _completion(good))])
        run = propose(
            ["minimax", "zai"], {}, watched=set(), blocked=set(), max_n=3, transport=t
        )
        assert run["status"] == "ok" and run["provider"] == "zai"
        assert [p["symbol"] for p in run["proposals"]] == ["XLF"]
        assert any(n.startswith("minimax:") and "truncated" in n for n in run["notes"])

    def test_no_json_note_names_the_provider(self) -> None:
        t = FakeTransport([(200, _completion("I cannot help with that."))])
        run = propose(["local"], {}, watched=set(), blocked=set(), max_n=3, transport=t)
        assert run["notes"] == ["local: no JSON object in model output"]

    def test_missing_key_lists_every_accepted_name(self) -> None:
        with pytest.raises(LlmError) as exc:
            chat_json("zai", [], transport=FakeTransport([]))
        assert "ANTHROPIC_AUTH_TOKEN_ZAI" in str(exc.value)
        assert "ZAI_CODING_API_KEY" in str(exc.value)


class TestProposeChain:
    def test_falls_through_to_next_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ZAI_CODING_API_KEY", SECRET)
        good = '{"proposals": [{"symbol": "TSM", "action": "add", "rationale": "semis"}]}'
        t = FakeTransport([(500, b""), (200, _completion(good))])
        run = propose(["local", "zai"], {}, watched=set(), blocked=set(), max_n=3, transport=t)
        assert run["status"] == "ok"
        assert run["provider"] == "zai" and run["model"] == "glm-5.3-flash"
        assert run["proposals"][0]["symbol"] == "TSM"
        assert any("local: HTTP 500" in n for n in run["notes"])
        assert all(SECRET not in n for n in run["notes"])

    def test_all_fail_proposes_nothing(self) -> None:
        t = FakeTransport([(200, _completion("I cannot help with that."))])
        run = propose(["local"], {}, watched=set(), blocked=set(), max_n=3, transport=t)
        assert run["status"] == "failed" and run["proposals"] == []
        assert run["notes"]

    def test_none_never_calls_transport(self) -> None:
        t = FakeTransport([])
        run = propose(["none"], {}, watched=set(), blocked=set(), max_n=3, transport=t)
        assert run["status"] == "failed" and t.calls == []


class TestCodexM456Regressions:
    def test_malformed_key_rejected_without_echo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ZAI_CODING_API_KEY", "sk-example\nsecond-line")
        with pytest.raises(LlmError) as exc:
            chat_json("zai", [], transport=FakeTransport([]))
        assert "sk-example" not in str(exc.value) and "malformed" in str(exc.value)

    def test_transport_exception_text_never_surfaces(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ZAI_CODING_API_KEY", SECRET)
        t = FakeTransport([ValueError(f"Invalid header value b'Bearer {SECRET}'")])
        with pytest.raises(LlmError) as exc:
            chat_json("zai", [], transport=t)
        assert SECRET not in str(exc.value)
        assert exc.value.__cause__ is None and exc.value.__suppress_context__

    def test_rejected_draft_span_is_skipped_whole(self) -> None:
        text = (
            "Draft: {'discarded': {\"proposals\":[{\"symbol\":\"TSM\",\"action\":\"add\"}]}} "
            'Final: {"proposals":[]}'
        )
        assert _extract_json_object(text) == {"proposals": []}

    def test_null_message_falls_back_to_next_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ZAI_CODING_API_KEY", SECRET)
        null_msg = json.dumps({"choices": [{"message": None}]}).encode()
        t = FakeTransport([(200, null_msg), (200, _completion('{"proposals": []}'))])
        run = propose(["local", "zai"], {}, watched=set(), blocked=set(), max_n=3, transport=t)
        assert run["status"] == "ok" and run["provider"] == "zai"

    def test_huge_confidence_defaults_instead_of_crashing(self) -> None:
        raw = {"proposals": [{"symbol": "TSM", "action": "add", "confidence": 10**400}]}
        out, _ = normalize(raw, watched=set(), blocked=set(), max_n=3)
        assert out[0]["confidence"] == 0.5

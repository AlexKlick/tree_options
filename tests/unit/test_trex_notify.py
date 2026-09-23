"""Operator push (ntfy): off until configured, never raises, and the topic
URL (the capability) never lands in logs or exceptions."""

from __future__ import annotations

from pathlib import Path

from tree_options.trex.notify import load_config, send

TOPIC_URL = "https://ntfy.sh/trex-SECRET-topic-123"


class Recorder:
    def __init__(self, status: int = 200, exc: Exception | None = None) -> None:
        self.status, self.exc = status, exc
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []

    def __call__(self, url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
        self.calls.append((url, body, headers))
        if self.exc:
            raise self.exc
        return self.status


def test_unconfigured_is_a_quiet_no_op(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "missing.env")
    rec = Recorder()
    assert cfg is None
    assert send(cfg, "t", "m", transport=rec) is False
    assert rec.calls == []


def test_config_file_parsing(tmp_path: Path) -> None:
    path = tmp_path / "notify.env"
    path.write_text(f"# comment\nNTFY_URL={TOPIC_URL}\nNTFY_TOKEN='tk_abc'\n")
    cfg = load_config(path)
    assert cfg == {"url": TOPIC_URL, "token": "tk_abc"}


def test_non_https_url_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "notify.env"
    path.write_text("NTFY_URL=http://ntfy.sh/plain\n")
    assert load_config(path) is None


def test_send_posts_title_priority_and_utf8_body() -> None:
    rec = Recorder()
    ok = send({"url": TOPIC_URL, "token": "tk_abc"}, "trex: IB Gateway needs login",
               "Logged out since Tue 19:45 ET — 14h 33m.", "high", transport=rec)
    assert ok
    url, body, headers = rec.calls[0]
    assert url == TOPIC_URL
    assert body.decode() == "Logged out since Tue 19:45 ET — 14h 33m."
    assert headers["Title"] == "trex: IB Gateway needs login"
    assert headers["Priority"] == "high"
    assert headers["Authorization"] == "Bearer tk_abc"


def test_failures_return_false_and_never_echo_the_topic(capsys) -> None:  # type: ignore[no-untyped-def]
    assert send({"url": TOPIC_URL, "token": ""}, "t", "m",
                transport=Recorder(exc=OSError(f"connect to {TOPIC_URL} failed"))) is False
    assert send({"url": TOPIC_URL, "token": ""}, "t", "m", transport=Recorder(status=500)) is False
    captured = capsys.readouterr()
    assert "SECRET" not in captured.out + captured.err


def test_non_ascii_title_is_encoded_for_the_header() -> None:
    rec = Recorder()
    send({"url": TOPIC_URL, "token": ""}, "trex — gateway", "m", transport=rec)
    assert rec.calls[0][2]["Title"].isascii()

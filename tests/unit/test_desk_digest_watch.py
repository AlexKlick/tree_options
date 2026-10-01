"""scripts/desk_digest_watch.py: the nightly post-game watch, on fixtures.

The watch answers four questions about the desk store the 19:00 Denver
challenge game just wrote, and its honesty is the product: a missing game,
a quota_dry game, and a standings rebuild that never ran must each fail with
a reason the ops loop can read straight out of journald, while model
failures inside an executed game only warn. Every fixture here is a desk
store built on disk (digest directories named by UTC stamp, a digest.json,
a standings.json with a controlled mtime) and every expectation — cutoffs,
pass/fail — is written down by hand from the wall clock, not recomputed by
the code under test:

* the watch fires at 19:30 America/Denver = 21:30 ET, so for
  ``now = 2026-10-02T01:30:00Z`` (21:30 ET on 10-01) the game cutoff is
  2026-10-02T01:00:00Z — "today's game" is the digest stamped NEXT UTC day
  01:00Z, and the on-time game is stamped exactly that, not after it;
* yesterday's 20261001T010000Z-style stamp is stale;
* winter: 2027-01-05T02:30:00Z is 21:30 ET on 01-04 (EST), cutoff
  2027-01-05T02:00:00Z.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(SCRIPTS))


def _load_watch():
    spec = importlib.util.spec_from_file_location(
        "desk_digest_watch", SCRIPTS / "desk_digest_watch.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("desk_digest_watch", module)
    spec.loader.exec_module(module)
    return module


watch = _load_watch()

#: the watch's own firing moment: 19:30 America/Denver, written by hand as
#: UTC (21:30 ET on the 2026-10-01 ET date; Denver is UTC-6 in October)
NOW = datetime(2026, 10, 2, 1, 30, tzinfo=UTC)
#: 19:00 America/Denver on the ET date 2026-10-01, by hand
CUTOFF = datetime(2026, 10, 2, 1, 0, tzinfo=UTC)
#: tonight's on-time game stamp: exactly the cutoff instant
TONIGHT = "20261002T010000Z"
YESTERDAY = "20261001T010000Z"


def _epoch(moment: datetime) -> float:
    return moment.timestamp()


def _digest_document(status: str = "ok", model_failures: list[int] | None = None) -> dict:
    cards = [
        {"policy": f"policy{i}", "model_failures": failures}
        for i, failures in enumerate(model_failures or [0, 0])
    ]
    return {
        "schema": "desk-challenge/1",
        "status": status,
        "at": "2026-10-02T01:00:00+00:00",
        "rounds": [{"bundle": "b1", "scorecards": cards}],
    }


def _build_store(
    tmp_path: Path,
    stamp: str | None = TONIGHT,
    document: dict | None = None,
    digest_mtime: datetime | None = None,
    standings_mtime: datetime | None = None,
    standings: bool = True,
) -> Path:
    """One desk store with one digest; mtimes are pinned, not incidental."""
    base = tmp_path / "desk-store" / "evaluations" / "challenge"
    base.mkdir(parents=True)
    if stamp is not None:
        digest_dir = base / stamp
        digest_dir.mkdir()
        body = _digest_document() if document is None else document
        path = digest_dir / "digest.json"
        path.write_text(json.dumps(body, indent=2))
        moment = digest_mtime or datetime(2026, 10, 2, 1, 2, tzinfo=UTC)
        os.utime(path, (_epoch(moment), _epoch(moment)))
    if standings:
        standings_path = base / "standings.json"
        standings_path.write_text(json.dumps({"schema": "desk-challenge-standings/1"}))
        moment = standings_mtime or datetime(2026, 10, 2, 1, 3, tzinfo=UTC)
        os.utime(standings_path, (_epoch(moment), _epoch(moment)))
    return tmp_path / "desk-store"


# ------------------------------------------------------------ the cutoff


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # the watch's firing moment: 21:30 ET on 10-01 -> tonight's 21:00 ET cutoff
        (datetime(2026, 10, 2, 1, 30, tzinfo=UTC), CUTOFF),
        # 21:45 ET the same evening: same date, same cutoff
        (datetime(2026, 10, 2, 1, 45, tzinfo=UTC), CUTOFF),
        # winter: 21:30 ET on 2027-01-04 (EST, UTC-5) -> 02:00Z next day
        (datetime(2027, 1, 5, 2, 30, tzinfo=UTC), datetime(2027, 1, 5, 2, 0, tzinfo=UTC)),
    ],
)
def test_cutoff_is_1900_denver_on_the_current_et_date(now, expected):
    assert watch.game_cutoff_utc(now) == expected


# ------------------------------------------------------------ check 1: freshness


def test_ok_digest_with_fresh_standings_passes(tmp_path):
    store = _build_store(tmp_path)
    result = watch.check_challenge_digest(store, NOW)
    assert result.exit_code == 0
    assert result.failures == ()
    assert result.warnings == ()
    assert result.digest_id == TONIGHT
    assert result.digest_status == "ok"
    assert result.model_failures == 0
    assert result.standings_fresh is True
    assert any(line.startswith("summary: OK") for line in result.lines)


def test_on_time_game_stamped_exactly_at_cutoff_passes(tmp_path):
    # the 19:00:00 Denver slot stamps exactly 01:00:00Z: "at", not "after"
    store = _build_store(tmp_path, stamp="20261002T010000Z")
    result = watch.check_challenge_digest(store, NOW)
    assert result.exit_code == 0
    assert result.failures == ()


def test_collision_suffix_stamp_counts_as_its_base_time(tmp_path):
    store = _build_store(tmp_path, stamp="20261002T010000Z-1")
    result = watch.check_challenge_digest(store, NOW)
    assert result.exit_code == 0
    assert result.digest_id == "20261002T010000Z-1"


def test_missing_digest_fails_with_reason(tmp_path):
    store = _build_store(tmp_path, stamp=None, standings=False)
    result = watch.check_challenge_digest(store, NOW)
    assert result.exit_code == 1
    assert len(result.failures) == 1
    assert result.failures[0].startswith("digest-missing:")
    assert "evaluations/challenge" in result.failures[0]


def test_yesterdays_digest_is_stale_with_reason(tmp_path):
    store = _build_store(
        tmp_path,
        stamp=YESTERDAY,
        digest_mtime=datetime(2026, 10, 1, 1, 2, tzinfo=UTC),
        standings_mtime=datetime(2026, 10, 1, 1, 3, tzinfo=UTC),
    )
    result = watch.check_challenge_digest(store, NOW)
    assert result.exit_code == 1
    assert result.failures[0].startswith("digest-stale:")
    assert YESTERDAY in result.failures[0]
    assert "2026-10-02T01:00:00+00:00" in result.failures[0]  # the cutoff, by hand
    # an honest stale report still names the digest it did find
    assert result.digest_id == YESTERDAY


# ------------------------------------------------------------ check 2: status


def test_quota_dry_digest_fails_with_reason(tmp_path):
    store = _build_store(tmp_path, document=_digest_document(status="quota_dry"))
    result = watch.check_challenge_digest(store, NOW)
    assert result.exit_code == 1
    assert result.failures[0].startswith("digest-status:")
    assert "quota_dry" in result.failures[0]


def test_empty_bundles_digest_fails_with_reason(tmp_path):
    store = _build_store(tmp_path, document=_digest_document(status="empty_bundles"))
    result = watch.check_challenge_digest(store, NOW)
    assert result.exit_code == 1
    assert result.failures[0].startswith("digest-status:")
    assert "empty_bundles" in result.failures[0]


def test_unparseable_digest_fails_with_reason(tmp_path):
    base = tmp_path / "desk-store" / "evaluations" / "challenge" / TONIGHT
    base.mkdir(parents=True)
    (base / "digest.json").write_text("{not json")
    (tmp_path / "desk-store" / "evaluations" / "challenge" / "standings.json").write_text("{}")
    result = watch.check_challenge_digest(tmp_path / "desk-store", NOW)
    assert result.exit_code == 1
    assert result.failures[0].startswith("digest-unreadable:")


# ------------------------------------------------------------ check 3: standings


def test_stale_standings_fails_with_reason(tmp_path):
    store = _build_store(
        tmp_path,
        digest_mtime=datetime(2026, 10, 2, 1, 2, tzinfo=UTC),
        standings_mtime=datetime(2026, 10, 2, 1, 1, tzinfo=UTC),
    )
    result = watch.check_challenge_digest(store, NOW)
    assert result.exit_code == 1
    assert result.failures[0].startswith("standings-stale:")
    assert result.standings_fresh is False


def test_standings_mtime_tied_to_digest_is_stale(tmp_path):
    # not strictly newer means the rebuild did not demonstrably run after
    tied = datetime(2026, 10, 2, 1, 2, tzinfo=UTC)
    store = _build_store(tmp_path, digest_mtime=tied, standings_mtime=tied)
    result = watch.check_challenge_digest(store, NOW)
    assert result.exit_code == 1
    assert result.failures[0].startswith("standings-stale:")


def test_missing_standings_fails_with_reason(tmp_path):
    store = _build_store(tmp_path, standings=False)
    result = watch.check_challenge_digest(store, NOW)
    assert result.exit_code == 1
    assert result.failures[0].startswith("standings-missing:")


# ------------------------------------------------------------ check 4: warnings


def test_model_failures_warn_but_do_not_fail(tmp_path):
    store = _build_store(tmp_path, document=_digest_document(model_failures=[1, 2]))
    result = watch.check_challenge_digest(store, NOW)
    assert result.exit_code == 0
    assert result.failures == ()
    assert len(result.warnings) == 1
    assert result.warnings[0].startswith("model-failures:")
    assert result.model_failures == 3


def test_zero_model_failures_produces_no_warning(tmp_path):
    store = _build_store(tmp_path, document=_digest_document(model_failures=[0, 0]))
    result = watch.check_challenge_digest(store, NOW)
    assert result.exit_code == 0
    assert result.warnings == ()
    assert result.model_failures == 0


# ------------------------------------------------------------ failures compound


def test_every_failure_gets_its_own_line_plus_one_summary(tmp_path):
    # stale digest AND quota_dry AND stale standings: three lines, one summary
    store = _build_store(
        tmp_path,
        stamp=YESTERDAY,
        document=_digest_document(status="quota_dry"),
        digest_mtime=datetime(2026, 10, 1, 1, 2, tzinfo=UTC),
        standings_mtime=datetime(2026, 10, 1, 1, 1, tzinfo=UTC),
    )
    result = watch.check_challenge_digest(store, NOW)
    assert result.exit_code == 1
    kinds = [line.split(":", 1)[0] for line in result.failures]
    assert kinds == ["digest-stale", "digest-status", "standings-stale"]
    assert sum(1 for line in result.lines if line.startswith("summary:")) == 1
    assert "failed=3" in result.lines[-1]


# ------------------------------------------------------------ the CLI


def test_cli_prints_journald_lines_and_exits_zero(tmp_path, capsys):
    store = _build_store(tmp_path)
    code = watch.main(["--store", str(store), "--now", "2026-10-02T01:30:00+00:00"])
    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert out == [
        f"desk-digest-watch: summary: OK digest={TONIGHT} status=ok "
        "model_failures=0 standings_fresh=true"
    ]


def test_cli_exits_one_on_missing_digest(tmp_path, capsys):
    store = _build_store(tmp_path, stamp=None, standings=False)
    code = watch.main(["--store", str(store), "--now", "2026-10-02T01:30:00+00:00"])
    out = capsys.readouterr().out.splitlines()
    assert code == 1
    assert len(out) == 2
    assert out[0].startswith("desk-digest-watch: FAIL digest-missing:")
    assert out[1].startswith("desk-digest-watch: summary: FAIL")


def test_cli_refuses_naive_now(tmp_path):
    with pytest.raises(SystemExit):
        watch.main(["--store", str(tmp_path), "--now", "2026-10-02T01:30:00"])

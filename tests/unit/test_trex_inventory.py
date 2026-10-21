"""trex_inventory: read-only map of every trex-*/desk-* unit to the code it runs.

The fixture mirrors the 2026-10-21 drift shape: web + premarket on a release
worktree, QSL on another, the evidence/admission pair on an untracked copy,
everything else on the serving checkout. systemctl and git are faked through
the injectable runner; nothing here touches the real user manager.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import trex_inventory as inv  # noqa: E402

SHA_MAIN = "c0010040ac01bf1def9cded0cf049a91ef492604"
SHA_WEB = "0ec5acb99856589559fdb0b0f7164ac3dc9bb092"
SHA_QSL = "cc91b783e843ddd945d431d6fd7248dd49053629"
SHA_OLD = "f8d76000144c140abb2014e68a9efed80d5a053d"
SHA_LANE = "7e7d82caa80560524aa525568808f0ad36f65bd6"
SHA_GONE = "bdccf43af750ca0f67f09f9c5e91edff45a0a13d"
INLINE_SECRET = "supersecret-inline-value"
FILE_SECRET = "leaky-env-file-value"


def _epoch_us(*parts: int) -> int:
    return int(datetime(*parts, tzinfo=UTC).timestamp()) * 1_000_000


def _show(unit, *, load="loaded", active="inactive", sub="dead", file_state="static", **kw):
    lines = [
        f"Id={unit}",
        f"LoadState={load}",
        f"ActiveState={active}",
        f"SubState={sub}",
        f"UnitFileState={file_state}",
        f"FragmentPath={kw.get('fragment', '')}",
        "DropInPaths=" + " ".join(kw.get("dropins", ())),
        f"WorkingDirectory={kw.get('wd', '')}",
        "Environment=" + " ".join(kw.get("env", ())),
        "ReadWritePaths=" + " ".join(kw.get("rw", ())),
        "TriggeredBy=" + " ".join(kw.get("triggered_by", ())),
    ]
    lines += [f"EnvironmentFiles={name} (ignore_errors=yes)" for name in kw.get("env_files", ())]
    for phase, argv in kw.get("execs", ()):
        lines.append(
            f"{phase}={{ path={argv[0]} ; argv[]={' '.join(argv)} ; ignore_errors=no ; "
            "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
        )
    return "\n".join(lines)


def _show_timer(timer, unit, *, calendar=(), monotonic=(), active="active", file_state="enabled"):
    lines = [f"Unit={unit}"]
    lines += [
        f"TimersCalendar={{ OnCalendar={c} ; next_elapse=Wed 2026-10-21 17:30:00 MDT }}"
        for c in calendar
    ]
    lines += [f"TimersMonotonic={{ {m} ; next_elapse=3min }}" for m in monotonic]
    lines += [
        "NextElapseUSecRealtime=",
        f"Id={timer}",
        f"ActiveState={active}",
        f"UnitFileState={file_state}",
    ]
    return "\n".join(lines)


class Host:
    """A fake home with release dirs on disk and canned systemctl/git output."""

    def __init__(self, root: Path):
        self.home = root / "home"
        self.serving = self.home / "documents/tree_options"
        self.premarket = self.home / ".local/state/trex-premarket/releases"
        self.share = self.home / ".local/share/trex/releases"
        self.staging = self.home / ".local/state/trex-staging/release"
        self.web = self.premarket / "0ec5acb"
        self.qsl = self.premarket / "cc91b783"
        self.old = self.premarket / "f8d7600"
        self.rc2 = self.share / "desk-wave3-safety-rc2"
        self.rc1 = self.share / "desk-wave3-safety-rc1"
        self.lane = self.home / "documents/tree_options-worktrees/lane"
        self.gone = self.home / "documents/tree_options-worktrees/gone"
        self.desk_state = self.home / ".local/state/trex-desk"
        self.env_file = self.home / ".config/trex/notify.env"
        (self.serving / ".git").mkdir(parents=True)
        (self.serving / "artifacts/desk-store").mkdir(parents=True)
        for release in (self.web, self.qsl, self.old):
            (release / "source/src").mkdir(parents=True)
            (release / "source/.git").write_text("gitdir: elsewhere\n")
            (release / "premarket.py").write_text("# release gate\n")
        for copy in (self.rc2, self.rc1):
            (copy / "src").mkdir(parents=True)
        (self.staging / "src").mkdir(parents=True)
        (self.staging / ".git").write_text("gitdir: elsewhere\n")
        (self.lane / ".git").parent.mkdir(parents=True)
        (self.lane / ".git").write_text("gitdir: elsewhere\n")
        (self.desk_state / "evidence").mkdir(parents=True)
        self.env_file.parent.mkdir(parents=True)
        self.env_file.write_text(f"NOTIFY_TOKEN={FILE_SECRET}\n")
        self.calls: list[list[str]] = []
        self.timers_rc = 0
        self.services = self._services()
        self.timers = self._timers()

    def _py(self, root: Path) -> str:
        return f"{root}/.venv/bin/python"

    def _services(self) -> dict[str, str]:
        state = [
            f"HOME={self.home}",
            "UV_NO_SYNC=1",
            f"DESK_STORE={self.serving}/artifacts/desk-store",
            f"TREX_DESK_STATE={self.desk_state}",
        ]
        web_src = self.web / "source"
        qsl_src = self.qsl / "source"
        return {
            "desk-evidence.service": _show(
                "desk-evidence.service",
                wd=self.rc2,
                env=[*state, f"PYTHONPATH={self.rc2}/src"],
                rw=[f"{self.desk_state}/evidence"],
                execs=[("ExecStart", [self._py(self.rc2), "-m", "tree_options.desk", "shadows"])],
                triggered_by=["desk-evidence.timer"],
            ),
            "desk-admission-preview.service": _show(
                "desk-admission-preview.service",
                wd=self.rc2,
                env=[*state, f"PYTHONPATH={self.rc2}/src"],
                rw=[f"-{self.desk_state}/evidence"],
                execs=[
                    ("ExecStart", [self._py(self.rc2), "-m", "tree_options.desk", "desk-enter"])
                ],
            ),
            "desk-qsl.service": _show(
                "desk-qsl.service",
                wd=qsl_src,
                env=[*state, f"PYTHONPATH={qsl_src}/src"],
                rw=[str(self.desk_state)],
                execs=[
                    ("ExecStartPre", [self._py(qsl_src), f"{self.qsl}/premarket.py", "--check"]),
                    ("ExecStart", [self._py(qsl_src), "-m", "tree_options.desk", "qsl"]),
                    ("ExecStartPost", [self._py(qsl_src), "-m", "tree_options.desk", "shadows"]),
                ],
                triggered_by=["desk-qsl.timer"],
            ),
            "trex-web.service": _show(
                "trex-web.service",
                active="active",
                sub="running",
                file_state="enabled",
                wd=web_src,
                env=[
                    f"PYTHONPATH={web_src}/src",
                    f"TREX_API_TOKEN={INLINE_SECRET}",
                    f"TREX_PAPER_WORKSPACE={self.home}/.local/state/trex/paper-workspace",
                ],
                dropins=[
                    f"{self.home}/.config/systemd/user/trex-web.service.d/90-premarket-workspace.conf"
                ],
                execs=[
                    ("ExecStartPre", [self._py(web_src), f"{self.web}/premarket.py", "--check"]),
                    (
                        "ExecStart",
                        [self._py(web_src), "-m", "tree_options.trex_web", "--port", "8090"],
                    ),
                ],
            ),
            "trex-web-staging.service": _show(
                "trex-web-staging.service",
                active="active",
                sub="running",
                file_state="enabled",
                wd=self.staging,
                env=[
                    f"PYTHONPATH={self.staging}/src",
                    f"TREX_DESK_STATE={self.home}/.local/state/trex-staging/desk-state",
                ],
                execs=[("ExecStart", [self._py(self.staging), "-m", "tree_options.trex_web"])],
            ),
            "trex-premarket.service": _show(
                "trex-premarket.service",
                wd=web_src,
                env=[f"PYTHONPATH={web_src}/src"],
                execs=[
                    (
                        "ExecStart",
                        [
                            f"{self.home}/.local/bin/host-test",
                            self._py(web_src),
                            f"{self.web}/premarket.py",
                            "--run",
                        ],
                    )
                ],
                triggered_by=["trex-premarket.timer"],
            ),
            "desk-chain.service": _show(
                "desk-chain.service",
                wd=self.serving,
                env=[*state, f"PYTHONPATH={self.serving}/src"],
                env_files=[str(self.env_file)],
                execs=[
                    (
                        "ExecStart",
                        [self._py(self.serving), "-m", "tree_options.desk", "record-chains"],
                    )
                ],
                triggered_by=["desk-chain.timer"],
            ),
            "trex-desk.service": _show(
                "trex-desk.service",
                active="active",
                sub="running",
                file_state="enabled",
                wd=self.serving,
                env=[f"PYTHONPATH={self.serving}/src"],
                execs=[
                    (
                        "ExecStart",
                        [
                            f"{self.home}/.local/bin/uv",
                            "run",
                            "--group",
                            "trex",
                            "python",
                            "-m",
                            "tree_options.trex.supervised_desk",
                        ],
                    )
                ],
            ),
            # Imports the serving checkout but runs from a release directory.
            "desk-mixed.service": _show(
                "desk-mixed.service",
                wd=qsl_src,
                env=[f"PYTHONPATH={self.serving}/src"],
                execs=[("ExecStart", [self._py(self.serving), "-m", "tree_options.desk", "mine"])],
            ),
            # Loaded name with no unit file: still inventoried, source unknown.
            "desk-ghost.service": _show("desk-ghost.service", load="not-found"),
        }

    def _timers(self) -> dict[str, str]:
        return {
            "desk-evidence.timer": _show_timer(
                "desk-evidence.timer",
                "desk-evidence.service",
                calendar=[
                    "Mon..Sat *-*-* 19:30:00 America/New_York",
                    "Mon..Sat *-*-* 09:55:00 America/New_York",
                ],
            ),
            "desk-qsl.timer": _show_timer(
                "desk-qsl.timer",
                "desk-qsl.service",
                calendar=["Mon..Sat *-*-* 19:15:00 America/New_York"],
                file_state="disabled",
            ),
            "desk-chain.timer": _show_timer(
                "desk-chain.timer",
                "desk-chain.service",
                monotonic=["OnUnitActiveUSec=15min"],
            ),
            "trex-premarket.timer": _show_timer(
                "trex-premarket.timer",
                "trex-premarket.service",
                calendar=["Mon..Fri *-*-* 08:00:00 America/New_York"],
            ),
        }

    def list_timers_json(self) -> str:
        return json.dumps(
            [
                {
                    "next": _epoch_us(2026, 10, 21, 23, 30),
                    "left": 1,
                    "last": _epoch_us(2026, 10, 21, 17, 5),
                    "passed": 1,
                    "unit": "desk-evidence.timer",
                    "activates": "desk-evidence.service",
                },
                {
                    "next": _epoch_us(2026, 10, 21, 23, 15),
                    "left": 1,
                    "last": None,
                    "passed": None,
                    "unit": "desk-qsl.timer",
                    "activates": "desk-qsl.service",
                },
                {
                    "next": _epoch_us(2026, 10, 21, 20, 45),
                    "left": 1,
                    "last": 0,
                    "passed": 0,
                    "unit": "desk-chain.timer",
                    "activates": "desk-chain.service",
                },
                {
                    "next": None,
                    "left": None,
                    "last": None,
                    "passed": None,
                    "unit": "trex-premarket.timer",
                    "activates": "trex-premarket.service",
                },
            ]
        )

    def worktree_porcelain(self) -> str:
        blocks = [
            (self.serving, SHA_MAIN, "branch refs/heads/main"),
            (self.web / "source", SHA_WEB, "detached"),
            (self.qsl / "source", SHA_QSL, "detached"),
            (self.staging, SHA_WEB, "detached"),
            (self.lane, SHA_LANE, "branch refs/heads/fix/legacy-late-fill-price"),
            (
                self.gone,
                SHA_GONE,
                "branch refs/heads/docs/gone\nprunable gitdir file points to non-existent location",
            ),
        ]
        return "\n\n".join(f"worktree {p}\nHEAD {h}\n{tail}" for p, h, tail in blocks) + "\n"

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        head = argv[:3]
        if head == ["systemctl", "--user", "list-unit-files"]:
            names = [n for n in (*self.services, *self.timers) if n != "desk-ghost.service"]
            return 0, "\n".join(f"{n}  static  -" for n in names) + "\n"
        if head == ["systemctl", "--user", "list-units"]:
            names = [*self.services, *self.timers]
            return 0, "\n".join(f"{n} loaded inactive dead A unit" for n in names) + "\n"
        if head == ["systemctl", "--user", "list-timers"]:
            return self.timers_rc, self.list_timers_json() if self.timers_rc == 0 else ""
        if head == ["systemctl", "--user", "show"]:
            known = {**self.services, **self.timers}
            blocks = [known[a] for a in argv[3:] if a in known]
            return 0, "\n\n".join(blocks) + "\n"
        if argv[0] == "git":
            where = Path(argv[argv.index("-C") + 1])
            rest = argv[argv.index("-C") + 2 :]
            if where == self.serving and rest[:2] == ["worktree", "list"]:
                return 0, self.worktree_porcelain()
            if where == self.serving and rest[0] == "branch":
                return 0, (
                    f"fix/legacy-late-fill-price\t\t{SHA_LANE}\n"
                    f"docs/gone\torigin/docs/gone\t{SHA_GONE}\n"
                    f"w1/l7-release\torigin/w1/l7-release\t{SHA_MAIN}\n"
                )
            if where == self.old / "source" and rest == ["rev-parse", "HEAD"]:
                return 0, SHA_OLD + "\n"
        raise AssertionError(f"unexpected command: {argv}")

    def args(self) -> list[str]:
        return [
            "--serving-checkout",
            str(self.serving),
            "--release-parent",
            str(self.premarket),
            "--release-parent",
            str(self.share),
            "--release-dir",
            str(self.staging),
        ]

    def paths(self) -> inv.Paths:
        return inv.Paths(
            serving_checkout=self.serving,
            release_parents=(self.premarket, self.share),
            release_dirs=(self.staging,),
            templates=REPO_ROOT / "deploy/product",
        )


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


@pytest.fixture
def doc(host):
    return inv.build_inventory(host.paths(), host)


def test_every_unit_maps_to_its_source(host, doc):
    units = {u["unit"]: u for u in doc["units"]}
    assert set(units) == set(host.services)
    assert {t["timer"] for t in doc["timers"]} == set(host.timers)
    expected = {
        "desk-evidence.service": ("untracked_copy", host.rc2, None),
        "desk-admission-preview.service": ("untracked_copy", host.rc2, None),
        "desk-qsl.service": ("release", host.qsl / "source", SHA_QSL),
        "trex-web.service": ("release", host.web / "source", SHA_WEB),
        "trex-web-staging.service": ("release", host.staging, SHA_WEB),
        "trex-premarket.service": ("release", host.web / "source", SHA_WEB),
        "desk-chain.service": ("serving_checkout", host.serving, SHA_MAIN),
        "trex-desk.service": ("serving_checkout", host.serving, SHA_MAIN),
        "desk-mixed.service": ("serving_checkout", host.serving, SHA_MAIN),
        "desk-ghost.service": ("unknown", None, None),
    }
    for name, (kind, root, head) in expected.items():
        source = units[name]["source"]
        assert (source["kind"], source["root"], source["head"]) == (
            kind,
            None if root is None else str(root),
            head,
        ), name
    assert units["desk-mixed.service"]["source"]["consistent"] is False
    assert units["trex-premarket.service"]["source"]["consistent"] is True
    assert units["desk-ghost.service"]["load_state"] == "not-found"
    assert doc["summary"]["by_source_kind"] == {
        "release": 4,
        "serving_checkout": 3,
        "unknown": 1,
        "untracked_copy": 2,
    }


def test_untracked_copy_is_never_handed_to_git(host, doc):
    # git in a directory without its own .git ascends to an enclosing repo
    # (the home directory is one), which would misreport the copy's identity.
    for argv in host.calls:
        if argv[0] == "git":
            where = Path(argv[argv.index("-C") + 1])
            assert not where.is_relative_to(host.share), argv


def test_timers_report_next_fire_and_target_source(doc):
    timers = {t["timer"]: t for t in doc["timers"]}
    evidence = timers["desk-evidence.timer"]
    assert evidence["activates"] == "desk-evidence.service"
    assert evidence["next_fire_utc"] == "2026-10-21T23:30:00+00:00"
    assert evidence["last_fire_utc"] == "2026-10-21T17:05:00+00:00"
    assert evidence["calendar"] == [
        "Mon..Sat *-*-* 19:30:00 America/New_York",
        "Mon..Sat *-*-* 09:55:00 America/New_York",
    ]
    assert evidence["target_source"] == "untracked_copy"
    qsl = timers["desk-qsl.timer"]
    assert (qsl["active_state"], qsl["unit_file_state"]) == ("active", "disabled")
    assert qsl["last_fire_utc"] is None
    assert timers["desk-chain.timer"]["monotonic"] == ["OnUnitActiveUSec=15min"]
    assert timers["desk-chain.timer"]["last_fire_utc"] is None
    assert timers["trex-premarket.timer"]["next_fire_utc"] is None
    units = {u["unit"]: u for u in doc["units"]}
    assert units["desk-evidence.service"]["timers"] == ["desk-evidence.timer"]


def test_release_dirs_report_identity_and_references(host, doc):
    dirs = {d["path"]: d for d in doc["release_dirs"]}
    web = dirs[str(host.web)]
    assert (web["tracked"], web["head"]) == (True, SHA_WEB)
    assert web["referenced_by"] == ["trex-premarket.service", "trex-web.service"]
    assert dirs[str(host.qsl)]["referenced_by"] == ["desk-mixed.service", "desk-qsl.service"]
    old = dirs[str(host.old)]
    assert (old["referenced"], old["head"]) == (False, SHA_OLD)
    rc2 = dirs[str(host.rc2)]
    assert (rc2["tracked"], rc2["head"]) == (False, None)
    assert rc2["referenced_by"] == ["desk-admission-preview.service", "desk-evidence.service"]
    assert dirs[str(host.staging)]["referenced_by"] == ["trex-web-staging.service"]
    assert sorted(doc["summary"]["unreferenced_release_dirs"]) == sorted(
        [str(host.old), str(host.rc1)]
    )
    assert doc["summary"]["untracked_copies_in_use"] == [str(host.rc2)]


def test_worktrees_and_unpushed_branches(host, doc):
    trees = {w["path"]: w for w in doc["worktrees"]}
    assert trees[str(host.serving)]["role"] == "serving_checkout"
    assert trees[str(host.serving)]["branch"] == "main"
    assert trees[str(host.web / "source")]["role"] == "release"
    assert trees[str(host.web / "source")]["detached"] is True
    assert trees[str(host.staging)]["role"] == "release"
    assert trees[str(host.lane)]["role"] == "worktree"
    assert trees[str(host.gone)]["prunable"] is True
    assert trees[str(host.gone)]["exists"] is False
    assert doc["unpushed_branches"] == [{"branch": "fix/legacy-late-fill-price", "head": SHA_LANE}]


def test_state_roots_without_secret_values(host, doc):
    roots = {r["path"]: r for r in doc["state_roots"]}
    desk = roots[str(host.desk_state)]
    assert {"desk-evidence.service", "desk-qsl.service"} <= set(desk["units"])
    assert "TREX_DESK_STATE" in desk["keys"]
    assert desk["exists"] is True
    store = roots[f"{host.serving}/artifacts/desk-store"]
    assert store["inside_serving_checkout"] is True
    evidence = roots[f"{host.desk_state}/evidence"]
    assert evidence["keys"] == ["ReadWritePaths"]
    assert evidence["units"] == ["desk-admission-preview.service", "desk-evidence.service"]
    assert str(host.home) not in roots  # HOME is not a state root
    units = {u["unit"]: u for u in doc["units"]}
    assert units["desk-chain.service"]["environment_files"] == [str(host.env_file)]
    text = json.dumps(doc)
    assert INLINE_SECRET not in text
    assert "TREX_API_TOKEN" not in text
    assert FILE_SECRET not in text


def test_release_pinned_units_are_covered_by_release_templates(doc):
    units = {u["unit"]: u for u in doc["units"]}
    assert units["desk-evidence.service"]["release_template"] == "dropin"
    assert units["trex-premarket.service"]["release_template"] == "unit"
    assert units["desk-chain.service"]["release_template"] is None
    for unit in doc["units"]:
        if unit["source"]["kind"] in ("release", "untracked_copy"):
            assert unit["release_template"] is not None, unit["unit"]


def test_every_probe_is_read_only(host, doc):
    assert host.calls
    for argv in host.calls:
        inv.check_read_only(argv)
    for argv in (
        ["systemctl", "--user", "restart", "trex-web.service"],
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "desk-qsl.timer"],
        ["systemctl", "--user", "--now", "stop", "trex-web.service"],
        ["systemctl", "show", "trex-web.service"],
        ["git", "--no-optional-locks", "-C", "/x", "branch", "-D", "w1/l7-release"],
        ["git", "--no-optional-locks", "-C", "/x", "branch", "--no-merged", "x", "-D", "y"],
        ["git", "--no-optional-locks", "-C", "/x", "worktree", "prune"],
        ["git", "-C", "/x", "rev-parse", "HEAD"],
        ["rm", "-rf", "/x"],
    ):
        with pytest.raises(ValueError, match="inventory_command_not_read_only"):
            inv.check_read_only(argv)


def test_default_runner_refuses_mutation_before_spawning(monkeypatch):
    def spawned(*args, **kwargs):
        raise AssertionError("spawned a process")

    monkeypatch.setattr(inv.subprocess, "run", spawned)
    with pytest.raises(ValueError, match="inventory_command_not_read_only"):
        inv.run_read_only(["systemctl", "--user", "stop", "trex-desk.service"])


def _tree(root: Path) -> dict[str, tuple[int, int]]:
    seen = {}
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            stat = os.lstat(os.path.join(base, name))
            seen[os.path.join(base, name)] = (stat.st_size, stat.st_mtime_ns)
    return seen


def test_main_writes_only_its_out_path(host, tmp_path, capsys):
    before = _tree(tmp_path)
    out = tmp_path / "inventory.json"
    assert inv.main([*host.args(), "--json", "--out", str(out)], runner=host) == 0
    after = _tree(tmp_path)
    assert set(after) - set(before) == {str(out)}
    assert {p: after[p] for p in before} == before
    written = json.loads(out.read_text())
    assert written["schema"] == inv.SCHEMA == "trex-inventory/1"
    assert written["read_only"] is True
    assert json.loads(capsys.readouterr().out)["schema"] == inv.SCHEMA


def test_main_prints_a_text_table_by_default(host, capsys):
    assert inv.main(host.args(), runner=host) == 0
    out = capsys.readouterr().out
    line = next(x for x in out.splitlines() if x.startswith("desk-evidence.service"))
    assert "untracked_copy" in line and "desk-wave3-safety-rc2" in line
    assert "2026-10-21T23:30:00+00:00" in line
    web = next(x for x in out.splitlines() if x.startswith("trex-web.service"))
    assert SHA_WEB[:12] in web
    assert "unpushed branches=1" in out


def test_main_refuses_out_inside_serving_checkout(host, capsys):
    out = host.serving / "inventory.json"
    assert inv.main([*host.args(), "--out", str(out)], runner=host) == 2
    assert not out.exists()
    assert host.calls == []


def test_failed_probe_degrades_without_hiding_units(host):
    host.timers_rc = 1
    doc = inv.build_inventory(host.paths(), host)
    assert [e["command"] for e in doc["errors"]] == ["systemctl --user list-timers"]
    assert {u["unit"] for u in doc["units"]} == set(host.services)
    assert inv.main(host.args(), runner=host) == 3

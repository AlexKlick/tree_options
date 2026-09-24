"""Desk universe: the config-driven roster (``desk-universe.toml`` + loader).

Three layers, all spec-pinned (the oracle is the plan's roster, never the
loader implementation — the working tree's ``universe.py`` is the moving
side of this change):

1. **Pinned roster** — the module constants equal spec-pinned derivations
   (order-sensitive tuples, exact frozensets, partition invariants).
   Adding a name is a config edit PLUS an update to ``_PINNED_PANEL`` here
   in the same reviewed change.
2. **Committed-file consistency** — ``load_universe(<repo>/desk-universe.toml)``
   matches every module constant. This is what turns a stray ``DESK_UNIVERSE``
   export that poisoned import-time resolution loud. NEVER
   monkeypatch-and-reload: ``from X import Y`` consumers keep old bindings.
3. **Structural refusals** — a baseline-good config written under tmp_path
   with one surgical defect each; every refusal names the offending
   symbol/key. The real config file is never rewritten.

Invariant counts (spec): panel 39, ETFs 11, no-options 2, chain 37, XSMOM
tradables 36 (the SEALED ranking roster), late-close 9, regular-close 28,
partition 9 + 28 = 37.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from tree_options.desk import universe

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _no_stray_universe_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Hygiene only: the constants were bound at import (collection) time, so
    # deleting DESK_UNIVERSE here cannot mask anything layer 2 guards against.
    monkeypatch.delenv("DESK_UNIVERSE", raising=False)


# The spec roster, order load-bearing (panel fixtures and iteration follow it).
_PINNED_PANEL: tuple[str, ...] = (
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "LLY",
    "JPM", "V", "UNH", "XOM", "PG", "MA", "COST", "HD", "ADBE", "NFLX",
    "CRM", "AMD", "PEP", "KO", "DIS", "INTC", "QCOM", "PLTR", "SPCX",
    "SPY", "QQQ", "IWM", "SMH", "SOXX", "XLE", "XLV", "XLF", "GLD",
    "TQQQ", "SQQQ",
)  # fmt: skip
_PINNED_ETFS: tuple[str, ...] = (
    "SPY", "QQQ", "IWM", "SMH", "SOXX", "XLE", "XLV", "XLF", "GLD", "TQQQ", "SQQQ",
)  # fmt: skip
_PINNED_NO_OPTIONS: tuple[str, ...] = ("TQQQ", "SQQQ")
_PINNED_LATE: tuple[str, ...] = ("SPY", "QQQ", "IWM", "SMH", "SOXX", "XLE", "XLF", "XLV", "GLD")
_PINNED_REGULAR: tuple[str, ...] = (
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "LLY",
    "JPM", "V", "UNH", "XOM", "PG", "MA", "COST", "HD", "ADBE", "NFLX",
    "CRM", "AMD", "PEP", "KO", "DIS", "INTC", "QCOM", "PLTR", "SPCX",
)  # fmt: skip

_PINNED_CHAIN = tuple(n for n in _PINNED_PANEL if n not in set(_PINNED_NO_OPTIONS))
# The SEALED ranking universe (operator ruling 2026-09-23, seal effective
# 2026-10-01): the pre-2026-09-23 panel minus SPY. Deliberately NOT derived
# from the pin above -- PLTR/SPCX are captured but not ranked until re-seal.
_PINNED_XSMOM: tuple[str, ...] = (
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "LLY",
    "JPM", "V", "UNH", "XOM", "PG", "MA", "COST", "HD", "ADBE", "NFLX",
    "CRM", "AMD", "PEP", "KO", "DIS", "INTC", "QCOM",
    "QQQ", "IWM",
    "SMH", "SOXX", "XLE", "XLV", "XLF", "GLD", "TQQQ", "SQQQ",
)  # fmt: skip


# ----------------------------------------------------------- structural helper


def _array(values: list[str]) -> str:
    """Render a TOML inline string array (roster symbols need no escaping)."""
    return "[" + ", ".join(f'"{v}"' for v in values) + "]"


def _write_config(tmp_path: Path, *, raw: str | None = None, **overrides) -> Path:
    """Write a baseline-good spec-roster config under tmp_path, surgically bent.

    List/scalar kwargs (``version``, ``names``, ``etfs``, ``no_options_expression``,
    ``late``, ``regular``, ``tradables``) replace that field; ``drop_xsmom``
    omits the whole [xsmom] section; ``extra_top`` / ``extra_panel`` /
    ``extra_options_close`` / ``extra_xsmom`` append one raw TOML line in that
    section (unknown-key refusals); ``raw`` replaces the whole document
    (malformed-TOML case). The real repo config is never touched.
    """
    doc: dict[str, object] = {
        "version": 1,
        "names": list(_PINNED_PANEL),
        "etfs": list(_PINNED_ETFS),
        "no_options_expression": list(_PINNED_NO_OPTIONS),
        "late": list(_PINNED_LATE),
        "regular": list(_PINNED_REGULAR),
        "tradables": list(_PINNED_XSMOM),
    }
    doc.update(overrides)
    drop_xsmom = bool(doc.pop("drop_xsmom", False))
    extra_top = str(doc.pop("extra_top", ""))
    extra_panel = str(doc.pop("extra_panel", ""))
    extra_close = str(doc.pop("extra_options_close", ""))
    extra_xsmom = str(doc.pop("extra_xsmom", ""))
    lines = [f"version = {doc['version']}"]
    if extra_top:
        lines.append(extra_top)
    lines += [
        "[panel]",
        f"names = {_array(doc['names'])}",  # type: ignore[arg-type]
        f"etfs = {_array(doc['etfs'])}",  # type: ignore[arg-type]
        f"no_options_expression = {_array(doc['no_options_expression'])}",  # type: ignore[arg-type]
    ]
    if extra_panel:
        lines.append(extra_panel)
    lines += [
        "[options_close]",
        f"late = {_array(doc['late'])}",  # type: ignore[arg-type]
        f"regular = {_array(doc['regular'])}",  # type: ignore[arg-type]
    ]
    if extra_close:
        lines.append(extra_close)
    if not drop_xsmom:
        lines += ["[xsmom]", f"tradables = {_array(doc['tradables'])}"]  # type: ignore[arg-type]
        if extra_xsmom:
            lines.append(extra_xsmom)
    path = tmp_path / "desk-universe.toml"
    path.write_text(raw if raw is not None else "\n".join(lines) + "\n", encoding="utf-8")
    return path


# ------------------------------------------------------------- pinned roster


class TestPinnedRoster:
    def test_panel_names_pinned_in_order(self) -> None:
        assert isinstance(universe.PANEL_NAMES, tuple)
        assert universe.PANEL_NAMES == _PINNED_PANEL
        assert len(set(_PINNED_PANEL)) == 39  # the pin itself has no dups

    def test_chain_universe_is_the_panel_minus_the_leveraged_pair(self) -> None:
        assert isinstance(universe.CHAIN_UNIVERSE, tuple)
        assert universe.CHAIN_UNIVERSE == _PINNED_CHAIN  # order-preserving
        assert universe.CHAIN_UNIVERSE == tuple(
            n for n in universe.PANEL_NAMES if n not in {"TQQQ", "SQQQ"}
        )

    def test_xsmom_tradables_is_the_sealed_roster(self) -> None:
        # The sealed set is explicit, not panel-derived: PLTR/SPCX stay out
        # of the ranking until the owner re-seals [xsmom].tradables.
        assert isinstance(universe.XSMOM_TRADABLES, tuple)
        assert universe.XSMOM_TRADABLES == _PINNED_XSMOM
        assert set(universe.XSMOM_TRADABLES) <= set(universe.PANEL_NAMES)
        assert "SPY" not in universe.XSMOM_TRADABLES
        assert "PLTR" not in universe.XSMOM_TRADABLES and "SPCX" not in universe.XSMOM_TRADABLES

    def test_set_constants_are_the_pinned_frozensets(self) -> None:
        for const in (
            universe.PANEL_ETFS,
            universe.NO_OPTIONS_EXPRESSION,
            universe.LATE_CLOSE_OPTIONS,
            universe.REGULAR_CLOSE_OPTIONS,
        ):
            assert isinstance(const, frozenset)
        assert universe.PANEL_ETFS == frozenset(_PINNED_ETFS)
        assert universe.NO_OPTIONS_EXPRESSION == frozenset(_PINNED_NO_OPTIONS)
        assert universe.LATE_CLOSE_OPTIONS == frozenset(_PINNED_LATE)
        assert universe.REGULAR_CLOSE_OPTIONS == frozenset(_PINNED_REGULAR)

    def test_spec_counts(self) -> None:
        # panel 39, chain 37, xsmom (sealed) 36; etfs 11, no-options 2, late 9, regular 28
        assert len(_PINNED_PANEL) == 39 and len(universe.PANEL_NAMES) == 39
        assert len(_PINNED_CHAIN) == 37 and len(universe.CHAIN_UNIVERSE) == 37
        assert len(_PINNED_XSMOM) == 36 and len(universe.XSMOM_TRADABLES) == 36
        assert len(_PINNED_ETFS) == 11 and len(universe.PANEL_ETFS) == 11
        assert len(_PINNED_NO_OPTIONS) == 2 and len(universe.NO_OPTIONS_EXPRESSION) == 2
        assert len(_PINNED_LATE) == 9 and len(universe.LATE_CLOSE_OPTIONS) == 9
        assert len(_PINNED_REGULAR) == 28 and len(universe.REGULAR_CLOSE_OPTIONS) == 28

    def test_spec_roster_invariants_hold_in_the_pins(self) -> None:
        names = set(_PINNED_PANEL)
        chain = names - set(_PINNED_NO_OPTIONS)
        assert set(_PINNED_ETFS) <= names
        assert set(_PINNED_NO_OPTIONS) <= names
        assert set(_PINNED_LATE) <= chain
        assert set(_PINNED_REGULAR) <= chain
        assert not set(_PINNED_LATE) & set(_PINNED_REGULAR)
        assert set(_PINNED_LATE) | set(_PINNED_REGULAR) == chain
        assert set(_PINNED_XSMOM) <= names and "SPY" not in set(_PINNED_XSMOM)

    def test_close_classification_partitions_the_chain_universe(self) -> None:
        chain = set(universe.CHAIN_UNIVERSE)
        assert universe.LATE_CLOSE_OPTIONS | universe.REGULAR_CLOSE_OPTIONS == chain
        assert not universe.LATE_CLOSE_OPTIONS & universe.REGULAR_CLOSE_OPTIONS


# ------------------------------------------------ committed-file consistency


class TestCommittedFileConsistency:
    def test_committed_config_matches_every_module_constant(self) -> None:
        # The constants were bound at import from wherever resolve_universe_path()
        # pointed (explicit/env/default). If a stray DESK_UNIVERSE export poisoned
        # that resolution, this comparison against the committed file fails loudly.
        cfg = universe.load_universe(REPO / "desk-universe.toml")
        assert cfg.version == 1
        assert cfg.panel.names == universe.PANEL_NAMES
        assert frozenset(cfg.panel.etfs) == universe.PANEL_ETFS
        assert frozenset(cfg.panel.no_options_expression) == universe.NO_OPTIONS_EXPRESSION
        assert frozenset(cfg.options_close.late) == universe.LATE_CLOSE_OPTIONS
        assert frozenset(cfg.options_close.regular) == universe.REGULAR_CLOSE_OPTIONS
        assert cfg.chain_universe == universe.CHAIN_UNIVERSE
        assert cfg.xsmom_tradables == universe.XSMOM_TRADABLES


# ----------------------------------------------------------- path resolution


class TestResolveUniversePath:
    def test_default_is_the_repo_root_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DESK_UNIVERSE", raising=False)
        assert universe.resolve_universe_path() == REPO / "desk-universe.toml"

    def test_explicit_argument_wins_over_the_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DESK_UNIVERSE", str(tmp_path / "env-should-not-win.toml"))
        explicit = tmp_path / "explicit.toml"
        assert universe.resolve_universe_path(explicit) == explicit

    def test_env_var_beats_the_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_path = tmp_path / "from-env.toml"
        monkeypatch.setenv("DESK_UNIVERSE", str(env_path))
        assert universe.resolve_universe_path() == env_path


# -------------------------------------------------------- structural refusals


class TestStructuralRefusals:
    def test_universe_error_is_a_value_error(self) -> None:
        # UniverseError wraps TOML/validation failures and is catchable as ValueError
        assert issubclass(universe.UniverseError, ValueError)

    def test_baseline_loads_and_derives(self, tmp_path: Path) -> None:
        cfg = universe.load_universe(_write_config(tmp_path))
        assert cfg.version == 1
        assert cfg.panel.names == _PINNED_PANEL
        assert cfg.chain_universe == _PINNED_CHAIN  # order preserved, TQQQ/SQQQ out
        assert cfg.xsmom_tradables == _PINNED_XSMOM  # the sealed set, SPY excluded
        assert "SPY" not in cfg.xsmom_tradables

    def test_config_is_frozen(self, tmp_path: Path) -> None:
        cfg = universe.load_universe(_write_config(tmp_path))
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.version = 2  # type: ignore[misc]

    def test_duplicate_in_names_refused(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, names=[*_PINNED_PANEL, "AAPL"])
        with pytest.raises(ValueError, match="AAPL"):
            universe.load_universe(path)

    def test_duplicate_in_late_refused(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, late=[*_PINNED_LATE, "SPY"])
        with pytest.raises(ValueError, match="SPY"):
            universe.load_universe(path)

    def test_unknown_top_level_key_refused(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, extra_top="mystery_section_member = true")
        with pytest.raises(ValueError, match="mystery_section_member"):
            universe.load_universe(path)

    def test_unknown_panel_key_refused(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, extra_panel='side_book = ["SPX"]')
        with pytest.raises(ValueError, match="side_book"):
            universe.load_universe(path)

    def test_unknown_options_close_key_refused(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, extra_options_close='curfew = "16:15"')
        with pytest.raises(ValueError, match="curfew"):
            universe.load_universe(path)

    def test_partition_gap_names_the_missing_symbol(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, regular=[r for r in _PINNED_REGULAR if r != "PLTR"])
        with pytest.raises(ValueError, match="PLTR"):
            universe.load_universe(path)

    def test_partition_overlap_names_the_symbol(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, regular=[*_PINNED_REGULAR, "SPY"])
        with pytest.raises(ValueError, match="SPY"):
            universe.load_universe(path)

    def test_etfs_outside_names_refused(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, etfs=[*_PINNED_ETFS, "USO"])
        with pytest.raises(ValueError, match="USO"):
            universe.load_universe(path)

    def test_no_options_outside_names_refused(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, no_options_expression=["TQQQ", "SQQQ", "USO"])
        with pytest.raises(ValueError, match="USO"):
            universe.load_universe(path)

    def test_xsmom_outside_names_refused(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, tradables=[*_PINNED_XSMOM, "USO"])
        with pytest.raises(ValueError, match="USO"):
            universe.load_universe(path)

    def test_xsmom_containing_spy_refused(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, tradables=[*_PINNED_XSMOM, "SPY"])
        with pytest.raises(ValueError, match="SPY"):
            universe.load_universe(path)

    def test_unknown_xsmom_key_refused(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, extra_xsmom="horizon = 21")
        with pytest.raises(ValueError, match="horizon"):
            universe.load_universe(path)

    def test_missing_xsmom_section_refused(self, tmp_path: Path) -> None:
        # [xsmom] is required: the sealed ranking universe is explicit, never derived
        path = _write_config(tmp_path, drop_xsmom=True)
        with pytest.raises(ValueError, match="xsmom"):
            universe.load_universe(path)

    def test_late_outside_chain_refused(self, tmp_path: Path) -> None:
        # TQQQ is in the panel but excluded from the chain universe
        path = _write_config(tmp_path, late=[*_PINNED_LATE, "TQQQ"])
        with pytest.raises(ValueError, match="TQQQ"):
            universe.load_universe(path)

    @pytest.mark.parametrize("bad", ["pltr", "PL7R", "PLTRSPX"])  # lower / digit / 7 chars
    def test_bad_symbols_refused(self, tmp_path: Path, bad: str) -> None:
        # rename PLTR everywhere so ONLY the symbol-regex invariant is bent
        path = _write_config(
            tmp_path,
            names=[bad if n == "PLTR" else n for n in _PINNED_PANEL],
            regular=[bad if r == "PLTR" else r for r in _PINNED_REGULAR],
        )
        with pytest.raises(ValueError, match=bad):
            universe.load_universe(path)

    def test_empty_names_refused(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, names=[])
        with pytest.raises(ValueError):
            universe.load_universe(path)

    def test_unsupported_version_refused(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, version=2)
        with pytest.raises(ValueError, match="version"):
            universe.load_universe(path)

    def test_missing_file_names_the_path(self, tmp_path: Path) -> None:
        path = tmp_path / "no-such-dir" / "desk-universe.toml"
        with pytest.raises(FileNotFoundError, match=re.escape(str(path))):
            universe.load_universe(path)

    def test_malformed_toml_wrapped_naming_the_path(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, raw="version = [unclosed\n")
        with pytest.raises(ValueError, match=re.escape(str(path))):
            universe.load_universe(path)

"""The desk universe: the research panel's 39 names, from ``desk-universe.toml``.

The repo-root config (sibling of research_protocol.yaml) is the single
source: this module validates it and exposes the constants the desk code
imports; the research lane's ``artifacts/paper-trades/fetch_ohlc.py``
parses the same file directly with tomllib (``desk/`` and research never
import each other -- the shared *data file* is the whole coupling).
Provenance (panel history, close-class evidence, the XSMOM seal) lives in
the TOML's comments.

Stdlib only, on purpose: desk/ keeps a minimal import surface for the
timers (test_desk_signals pins the allowlist; pydantic is not on it). The
discovery config (``trex/discovery/config.py``) is the precedent: a frozen
dataclass plus explicit checks, not a schema framework.

DEVIATION from desk/paths.py, on purpose: paths resolves env-overridable
locations at call time and caches nothing, but this module binds its
constants at IMPORT time. signals.py binds XSMOM_TRADABLES as default
argument values at def time and events.py derives EARNINGS_NAMES at
import, so the values must exist before any consumer is defined. A config
edit therefore takes effect only in a fresh interpreter; every desk entry
point is a oneshot CLI or a fresh ``python -c`` (e.g.
scripts/desk_longdated_capture.sh), so that holds today. A missing or
broken config fails the import itself -- fail-closed like
research_protocol.yaml: even ``--help`` breaks, by design.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

_UNIVERSE_FILE = "desk-universe.toml"
_UNIVERSE_ENV_VAR = "DESK_UNIVERSE"

# Upper-case letters and dots, 1-6 chars (BRK.B-style classes).
_SYMBOL = re.compile(r"^[A-Z.]{1,6}$")

_SECTION_KEYS = {
    "panel": {"names", "etfs", "no_options_expression"},
    "options_close": {"late", "regular"},
    "xsmom": {"tradables"},
}
_TOP_KEYS = {"version", *_SECTION_KEYS}


class UniverseError(ValueError):
    """The desk-universe config is missing, malformed, or invalid."""


def _invalid_symbols(names: tuple[str, ...]) -> list[str]:
    return sorted(n for n in names if not _SYMBOL.match(n))


def _duplicate_symbols(names: tuple[str, ...]) -> list[str]:
    return sorted({n for n in names if names.count(n) > 1})


@dataclass(frozen=True)
class PanelSection:
    """``[panel]``: the research panel. ``names`` order is load-bearing
    (panel fixtures and iteration follow it)."""

    names: tuple[str, ...]
    etfs: tuple[str, ...]
    no_options_expression: tuple[str, ...]


@dataclass(frozen=True)
class OptionsCloseSection:
    """``[options_close]``: which chain symbols stop at the equity close
    (``regular``) and which run to 16:15 ET (``late``). Both lists stay
    explicit: only listed evidence earns the 16:00 cutoff (see the TOML)."""

    late: tuple[str, ...]
    regular: tuple[str, ...]


@dataclass(frozen=True)
class XsmomSection:
    """``[xsmom]``: the SEALED ranking universe -- the tradables XSMOM-TOP3
    ranks. Deliberately not derived from the panel: the rule's goldens and
    its protocol rows are sealed against this exact roster (operator ruling
    2026-09-23, seal effective 2026-10-01), so a panel addition captures
    data for a new name WITHOUT entering the ranking until the owner
    re-seals -- editing this list IS that re-seal decision."""

    tradables: tuple[str, ...]


@dataclass(frozen=True)
class UniverseConfig:
    version: int
    panel: PanelSection
    options_close: OptionsCloseSection
    xsmom: XsmomSection

    @property
    def chain_universe(self) -> tuple[str, ...]:
        """The chain recorder's universe: the panel minus the leveraged pair."""
        no_options = set(self.panel.no_options_expression)
        return tuple(n for n in self.panel.names if n not in no_options)

    @property
    def xsmom_tradables(self) -> tuple[str, ...]:
        """XSMOM-TOP3 ranks these: the sealed ranking universe (see XsmomSection)."""
        return self.xsmom.tradables


def _symbol_list(raw: object, where: str) -> tuple[str, ...]:
    """One config value as a tuple of validated symbols (fail-closed)."""
    if not isinstance(raw, list) or not all(isinstance(n, str) for n in raw):
        raise ValueError(f"{where} must be a list of strings")
    values = tuple(raw)
    if not values:
        raise ValueError(f"{where} must not be empty")
    invalid = _invalid_symbols(values)
    if invalid:
        raise ValueError(f"{where}: invalid symbol(s) {invalid}")
    dups = _duplicate_symbols(values)
    if dups:
        raise ValueError(f"{where}: duplicate symbol(s) {dups}")
    return values


def _section(raw: dict, name: str) -> dict:
    """One ``[section]`` table of the parsed document, refusing unknown keys."""
    table = raw.get(name)
    if not isinstance(table, dict):
        raise ValueError(f"[{name}] must be a table")
    unknown = sorted(set(table) - _SECTION_KEYS[name])
    if unknown:
        raise ValueError(f"unknown key(s) {unknown} in [{name}]")
    missing = sorted(_SECTION_KEYS[name] - set(table))
    if missing:
        raise ValueError(f"[{name}] is missing key(s) {missing}")
    return table


def _build(raw: dict) -> UniverseConfig:
    """Validate the parsed TOML document and construct the config."""
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise ValueError(f"unknown key(s) {unknown} at the top level")
    missing = sorted(_TOP_KEYS - set(raw))
    if missing:
        raise ValueError(f"missing key(s) {missing} at the top level")
    version = raw["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("version must be an integer")
    if version != 1:
        raise UniverseError(f"unsupported desk-universe version: {version} (expected 1)")

    panel = _section(raw, "panel")
    names = _symbol_list(panel["names"], "panel.names")
    etfs = _symbol_list(panel["etfs"], "panel.etfs")
    no_options = _symbol_list(panel["no_options_expression"], "panel.no_options_expression")
    not_panel = sorted(set(etfs) - set(names))
    if not_panel:
        raise ValueError(f"panel.etfs not in panel.names: {not_panel}")
    not_panel = sorted(set(no_options) - set(names))
    if not_panel:
        raise ValueError(f"panel.no_options_expression not in panel.names: {not_panel}")

    close = _section(raw, "options_close")
    late = _symbol_list(close["late"], "options_close.late")
    regular = _symbol_list(close["regular"], "options_close.regular")
    no_options_set = set(no_options)
    chain = tuple(n for n in names if n not in no_options_set)
    in_chain = set(chain)
    not_chain = sorted(set(late) - in_chain)
    if not_chain:
        raise ValueError(f"options_close.late not in the chain universe: {not_chain}")
    not_chain = sorted(set(regular) - in_chain)
    if not_chain:
        raise ValueError(f"options_close.regular not in the chain universe: {not_chain}")
    # The two lists must partition the chain universe; report gaps and
    # overlaps together so a roster edit's full mistake is in one message.
    overlap = sorted(set(late) & set(regular))
    neither = [s for s in chain if s not in late and s not in regular]
    if overlap or neither:
        detail = "; ".join(
            part
            for part in (
                f"in both lists: {overlap}" if overlap else "",
                f"in neither list: {neither}" if neither else "",
            )
            if part
        )
        raise ValueError(f"options_close late/regular must partition the chain universe ({detail})")

    xsmom = _section(raw, "xsmom")
    tradables = _symbol_list(xsmom["tradables"], "xsmom.tradables")
    not_panel = sorted(set(tradables) - set(names))
    if not_panel:
        raise ValueError(f"xsmom.tradables not in panel.names: {not_panel}")
    if "SPY" in tradables:
        raise ValueError("xsmom.tradables must not contain SPY (the panel's cash proxy)")

    return UniverseConfig(
        version=version,
        panel=PanelSection(names=names, etfs=etfs, no_options_expression=no_options),
        options_close=OptionsCloseSection(late=late, regular=regular),
        xsmom=XsmomSection(tradables=tradables),
    )


def resolve_universe_path(path: Path | str | None = None) -> Path:
    """Resolve which TOML file is the universe (pure precedence).

    Explicit path, then the DESK_UNIVERSE env var, then the repo-root
    default. Existence is deliberately NOT checked here: an explicit or
    env-named path is an operator assertion, and load_universe fails
    closed -- naming the path -- when the resolved file is missing.
    """
    if path is not None:
        return Path(path)
    if env := os.environ.get(_UNIVERSE_ENV_VAR, "").strip():
        return Path(env)
    # src/tree_options/desk/universe.py -> repo root is parents[3].
    return Path(__file__).resolve().parents[3] / _UNIVERSE_FILE


def load_universe(path: Path | str) -> UniverseConfig:
    """Load and validate the universe from ``path``.

    Pure: no caching, no env lookup -- the module tail below is the only
    import-time load. Fails closed on anything wrong with the file, with
    the path named in every error.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"desk universe config not found: {p}") from exc
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise UniverseError(f"{p}: malformed TOML: {exc}") from exc
    try:
        return _build(raw)
    except ValueError as exc:
        raise UniverseError(f"{p}: {exc}") from exc


# The import-time bind (see module docstring). Fails closed on a missing or
# invalid config: nothing below executes, so every consumer breaks loudly.
_CFG = load_universe(resolve_universe_path())

# The same seven names and types the desk has always imported.
PANEL_NAMES: tuple[str, ...] = _CFG.panel.names
PANEL_ETFS: frozenset[str] = frozenset(_CFG.panel.etfs)
NO_OPTIONS_EXPRESSION: frozenset[str] = frozenset(_CFG.panel.no_options_expression)
CHAIN_UNIVERSE: tuple[str, ...] = _CFG.chain_universe
XSMOM_TRADABLES: tuple[str, ...] = _CFG.xsmom.tradables
LATE_CLOSE_OPTIONS: frozenset[str] = frozenset(_CFG.options_close.late)
REGULAR_CLOSE_OPTIONS: frozenset[str] = frozenset(_CFG.options_close.regular)

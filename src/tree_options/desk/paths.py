"""Where the desk reads and writes (resolved at call time, env-overridable).

* ``DESK_STORE``: the chain store, default ``<repo>/artifacts/desk-store``
  (``artifacts/`` is gitignored);
* ``TREX_DESK_STATE``: job state (stage markers, signals, card drafts,
  owed pushes, logs), default ``~/.local/state/trex-desk``;
* ``DESK_PAPER_DIR``: the research lane's scripts and panel, default
  ``<repo>/artifacts/paper-trades`` (read, and written only through its
  own ``fetch_ohlc.py``);
* ``TREX_NOTIFY_ENV``: the ntfy config, default
  ``~/.config/trex/notify.env`` (see :mod:`tree_options.trex.notify`);
* ``DESK_REPO_ROOT``: the checkout the jobs run from (cwd of the
  research subprocesses), default: the tree this package was imported
  from;
* ``DESK_EVENTS_DIR``: the sealed macro calendar (tracked data), default
  ``<repo>/data/desk/events``;
* ``DESK_PLAYBOOK_DIR``: the sealed playbook (tracked data), default
  ``<repo>/data/desk/playbook``;
* ``DESK_MINER_DIR``: the sealed deal-miner selection rules (tracked
  data), default ``<repo>/data/desk/miner``;
* ``TREX_DESK_QUEUE``: the deal miner's entry queue, default
  ``<TREX_DESK_STATE>/queue``.

Tests must pin all of these to tmp: nothing here caches a value.
"""

from __future__ import annotations

import os
from pathlib import Path


def _env_path(var: str) -> Path | None:
    raw = os.environ.get(var, "").strip()
    return Path(raw) if raw else None


def repo_root() -> Path:
    return _env_path("DESK_REPO_ROOT") or Path(__file__).resolve().parents[3]


def store_root() -> Path:
    return _env_path("DESK_STORE") or repo_root() / "artifacts" / "desk-store"


def state_root() -> Path:
    return _env_path("TREX_DESK_STATE") or Path.home() / ".local" / "state" / "trex-desk"


def paper_dir() -> Path:
    return _env_path("DESK_PAPER_DIR") or repo_root() / "artifacts" / "paper-trades"


def events_dir() -> Path:
    return _env_path("DESK_EVENTS_DIR") or repo_root() / "data" / "desk" / "events"


def playbook_dir() -> Path:
    return _env_path("DESK_PLAYBOOK_DIR") or repo_root() / "data" / "desk" / "playbook"


def miner_dir() -> Path:
    """The sealed deal-miner selection rules (tracked data)."""
    return _env_path("DESK_MINER_DIR") or repo_root() / "data" / "desk" / "miner"


def queue_dir() -> Path:
    """The deal miner's entry queue (``<D>.json``, schema trex.deal/1)."""
    return _env_path("TREX_DESK_QUEUE") or state_root() / "queue"


def desk_paper_dir() -> Path:
    """The desk runtime's run dir (Wave 3: spec files, claims, admissions)."""
    return _env_path("DESK_PAPER_DIR") or Path.home() / ".local" / "state" / "trex" / "desk-paper"


def notify_env_path() -> Path:
    return _env_path("TREX_NOTIFY_ENV") or Path.home() / ".config" / "trex" / "notify.env"

"""Scoped trial budget (INV-13 / inner_loop.max_registered_configs = 32).

The cap is a pre-registration commitment device: once a scope holds `cap`
registered configs, no further registration is possible in that scope —
you cannot peek at outcomes and then register "one more" configuration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tree_options.registry.errors import ScopeBudgetExceededError

if TYPE_CHECKING:  # import only for typing; avoids an import cycle
    from tree_options.registry.sqlite import TrialRegistry


# Ceiling on any scope cap — mirrors the frozen protocol's
# inner_loop.max_registered_configs (32). A budget may TIGHTEN the
# pre-registration commitment below it; it can never loosen it (review F13).
MAX_SCOPE_CAP = 32


class TrialBudget:
    def __init__(self, cap: int = 32) -> None:
        if cap < 1:
            raise ValueError(f"cap must be >= 1, got {cap}")
        if cap > MAX_SCOPE_CAP:
            raise ValueError(
                f"cap {cap} exceeds the protocol maximum {MAX_SCOPE_CAP} "
                "(inner_loop.max_registered_configs): a scope budget may "
                "tighten the commitment, never loosen it"
            )
        self.cap = cap

    @classmethod
    def from_protocol(cls, protocol) -> TrialBudget:
        return cls(cap=protocol.inner_loop.max_registered_configs)

    def check(self, registry: TrialRegistry, scope_key: str) -> None:
        if registry.count_scope(scope_key) >= self.cap:
            raise ScopeBudgetExceededError(scope_key, self.cap)

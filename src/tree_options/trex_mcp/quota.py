"""Bounded, in-process per-tenant and per-subject MCP request admission.

This is defense in depth only: identity-provider and HTTPS ingress quotas must
span workers and hosts. No subject or bearer token is kept in the registry.
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from collections import deque
from collections.abc import Callable

from .server import Principal


class QuotaLimiter:
    """Window-accurate resource admission with bounded identity memory.

    Keeping a deque for each subject is O(1) at the capped workload.
    The lock serializes the entire admission decision so parallel requests
    cannot overdraw a tenant's shared allocation. Expired identity slots are
    reclaimed only on pressure, rather than scanning on every request.
    """

    def __init__(
        self, *, subject_limit: int = 60, tenant_limit: int = 300,
        window_seconds: float = 60.0, max_subject_keys: int = 2_048,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (subject_limit < 1 or tenant_limit < subject_limit
            or not 0 < window_seconds <= 86_400 or max_subject_keys < 1):
            raise ValueError("invalid MCP quota configuration")
        self.subject_limit = subject_limit
        self.tenant_limit = tenant_limit
        self.window_seconds = window_seconds
        self.max_subject_keys = max_subject_keys
        self.clock = clock
        self._salt = secrets.token_bytes(32)
        self._lock = asyncio.Lock()
        self.subject_windows: dict[str, deque[float]] = {}
        self.tenant_windows: dict[str, deque[float]] = {}

    def _key(self, tenant: str, subject: str) -> str:
        # No stored raw sub, JWT or bearer. Include tenant in the digest so
        # the same principal in two tenants is independently budgeted.
        digest = hashlib.blake2b(
            f"{tenant}\x00{subject}".encode("utf-8"), key=self._salt, digest_size=16
        )
        return digest.hexdigest()

    def _prune(self, samples: deque[float], now: float) -> None:
        deadline = now - self.window_seconds
        while samples and samples[0] <= deadline:
            samples.popleft()

    async def admit(self, principal: Principal) -> bool:
        if not principal.tenant_id:
            return False
        # A legacy test double without `subject` shares one quota slot.
        # Real OAuth subjects are always required by the introspection gate.
        identity = self._key(principal.tenant_id, principal.subject or "_unspecified")
        async with self._lock:
            now = self.clock()
            tenant = self.tenant_windows.setdefault(principal.tenant_id, deque())
            self._prune(tenant, now)
            if len(tenant) >= self.tenant_limit:
                return False
            subject = self.subject_windows.get(identity)
            if subject is None:
                if len(self.subject_windows) >= self.max_subject_keys:
                    # Pressure-only GC. Reject new identities rather than
                    # evicting an active quota window and bypassing limits.
                    for key, window in list(self.subject_windows.items()):
                        self._prune(window, now)
                        if not window:
                            del self.subject_windows[key]
                if len(self.subject_windows) >= self.max_subject_keys:
                    return False
                subject = deque()
                self.subject_windows[identity] = subject
            self._prune(subject, now)
            if len(subject) >= self.subject_limit:
                return False
            subject.append(now)
            tenant.append(now)
            return True

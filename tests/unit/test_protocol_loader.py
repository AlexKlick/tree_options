"""Protocol loader tests: typed load, canonical hash, fail-closed validation."""

from __future__ import annotations

from decimal import Decimal

import pydantic
import pytest


class TestProtocolLoad:
    def test_loads_and_exposes_invariants(self, protocol):
        ids = [inv.id for inv in protocol.invariants]
        assert ids == [f"INV-{i:02d}" for i in range(1, 15)]

    def test_timestamp_semantics(self, protocol):
        ts = protocol.timestamp_semantics
        assert ts.session_timezone == "America/New_York"
        assert ts.decision_instant == "session_close"
        assert ts.availability_rule == "available_at <= decision_at (inclusive)"

    def test_fold_shape(self, protocol):
        f = protocol.folds
        assert f.shape == "anchored_expanding"
        assert f.label_horizon_sessions == 5
        assert f.embargo_sessions == 5
        assert f.roll_forward_sessions <= f.test_window_sessions.min

    def test_fill_policy(self, protocol):
        fills = protocol.fills
        assert fills.primary.long_entry == "ask"
        assert fills.primary.long_exit == "bid"
        assert fills.primary.short_entry == "bid"
        assert fills.primary.short_exit == "ask"
        assert fills.fraction_to_midpoint_sensitivity == ("0.5", "1.0")
        assert fills.reject_locked_quotes is True
        assert fills.fill_size_fraction == Decimal("1.0")
        assert fills.same_session_execution == "reject"

    def test_inner_loop_cap(self, protocol):
        assert protocol.inner_loop.max_registered_configs == 32

    def test_unknown_key_rejected(self, protocol_path, tmp_path):
        from tree_options.protocol.loader import load_protocol

        bad = protocol_path.read_text() + "\nunexpected_key: true\n"
        (tmp_path / "p.yaml").write_text(bad)
        with pytest.raises(Exception, match="unexpected_key"):
            load_protocol(tmp_path / "p.yaml")

    def test_missing_invariant_rejected(self, protocol_path, tmp_path):
        from tree_options.protocol.loader import load_protocol

        text = protocol_path.read_text()
        # Remove INV-07 entirely.
        bad = "\n".join(
            line for line in text.splitlines() if "INV-07" not in line
        )
        (tmp_path / "p.yaml").write_text(bad + "\n")
        with pytest.raises(pydantic.ValidationError):
            load_protocol(tmp_path / "p.yaml")

    def test_invalid_protocol_rejected(self, tmp_path):
        from tree_options.protocol.loader import load_protocol

        (tmp_path / "p.yaml").write_text("meta: {protocol_version: '9.9.9'}\n")
        with pytest.raises(pydantic.ValidationError):
            load_protocol(tmp_path / "p.yaml")


class TestProtocolHash:
    def test_hash_is_sha256_hex(self, protocol):
        from tree_options.protocol.loader import protocol_hash

        h = protocol_hash(protocol)
        assert len(h) == 64
        int(h, 16)  # hex

    def test_hash_stable_across_comment_only_edit(self, protocol_path, tmp_path):
        from tree_options.protocol.loader import load_protocol, protocol_hash

        h1 = protocol_hash(load_protocol(protocol_path))
        commented = protocol_path.read_text() + "\n# a comment changes nothing\n"
        (tmp_path / "p.yaml").write_text(commented)
        h2 = protocol_hash(load_protocol(tmp_path / "p.yaml"))
        assert h1 == h2

    def test_hash_changes_on_semantic_edit(self, protocol_path, tmp_path):
        from tree_options.protocol.loader import load_protocol, protocol_hash

        h1 = protocol_hash(load_protocol(protocol_path))
        edited = protocol_path.read_text().replace("embargo_sessions: 5", "embargo_sessions: 6")
        assert edited != protocol_path.read_text()
        (tmp_path / "p.yaml").write_text(edited)
        h2 = protocol_hash(load_protocol(tmp_path / "p.yaml"))
        assert h1 != h2

    def test_raw_file_hash_recorded(self, protocol_path):
        from tree_options.protocol.loader import raw_file_hash

        h = raw_file_hash(protocol_path)
        assert len(h) == 64

    def test_invariant_02_statement_names_the_rule(self, protocol):
        inv2 = protocol.invariants[1]
        assert "available_at" in inv2.statement
        assert "decision_at" in inv2.statement

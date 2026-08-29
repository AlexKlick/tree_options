"""Protocol loader tests: typed load, canonical hash, fail-closed validation."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

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


class TestProtocolNumericStrictness:
    def test_max_registered_configs_is_strict(self):
        """Round-5 NEW-7: lax coercion let YAML `true` normalize to 1 (the
        silent bool alias), `"32"` to 32, and 32.0 to 32 into the
        pre-registration cap. The commitment field is strict: bool, str,
        and float inputs are refused instead of coerced."""
        from tree_options.protocol.schema import InnerLoopConfig

        with pytest.raises(pydantic.ValidationError):
            InnerLoopConfig(max_registered_configs=True)
        with pytest.raises(pydantic.ValidationError):
            InnerLoopConfig(max_registered_configs="32")
        with pytest.raises(pydantic.ValidationError):
            InnerLoopConfig(max_registered_configs=32.0)
        assert InnerLoopConfig(max_registered_configs=32).max_registered_configs == 32

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
        bad = "\n".join(line for line in text.splitlines() if "INV-07" not in line)
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


class TestProtocolIdentityPin:
    """(P1-4, Codex round 1) The absolute canonical-hash pin.

    w7a added a DEFAULTED schema member `underlying_liquidity_term` and the
    untouched 0.2.1 yaml silently re-hashed — 1,751 green tests never noticed
    because nothing pinned the ABSOLUTE value. The canonical hash represents
    what the yaml DECLARES: a defaulted-but-undeclared member is 0.2.2
    pre-draft machinery and must not ride the 0.2.1 identity."""

    # the ledger-bound identity (bars-authority binding + every cross-branch
    # trial identity); verified by execution on this branch before the pin
    LEDGER_BOUND_PROTOCOL_SHA256 = (
        "cfafc884d9c45d805f6d6028d6991daf9e2e1751d91823306d780506bbaffeb7"
    )

    def test_repo_yaml_hashes_to_the_ledger_bound_identity(self):
        """The absolute pin: the gate can never lose identity stability
        silently again (RED before P1-4: the defaulted member rode the dump
        and the same expression yielded 3b0b8a85…)."""
        from tree_options.protocol.loader import load_protocol, protocol_hash

        assert (
            protocol_hash(load_protocol(Path("research_protocol.yaml")))
            == self.LEDGER_BOUND_PROTOCOL_SHA256
        )

    def test_a_declared_dropped_term_rides_the_hash(self, protocol_path):
        """The exclusion must not swallow a real declaration: a yaml that
        DECLARES `dropped_no_equity_aggregates` (!= the default) hashes
        DIFFERENTLY from the pin — the disposition is a semantic protocol
        fact, not pre-draft machinery."""
        from tree_options.protocol.loader import load_protocol, protocol_hash

        base = load_protocol(protocol_path)
        lf = base.option_candidate_defaults.liquidity_volume_flow
        assert lf is not None
        declared = lf.model_copy(
            update={"underlying_liquidity_term": "dropped_no_equity_aggregates"}
        )
        mutated = base.model_copy(
            update={
                "option_candidate_defaults": base.option_candidate_defaults.model_copy(
                    update={"liquidity_volume_flow": declared}
                )
            }
        )
        assert protocol_hash(mutated) != self.LEDGER_BOUND_PROTOCOL_SHA256

    def test_undeclared_and_defaulted_models_hash_identically(self, protocol_path):
        """Cross-branch identity: a model constructed WITHOUT the field (the
        0.2.1 yaml) and one WITH the default stamped explicitly produce the
        SAME canonical bytes — a default is not a declaration."""
        from tree_options.protocol.loader import canonical_json, load_protocol

        undeclared = load_protocol(protocol_path)
        lf = undeclared.option_candidate_defaults.liquidity_volume_flow
        assert lf is not None
        defaulted = undeclared.model_copy(
            update={
                "option_candidate_defaults": undeclared.option_candidate_defaults.model_copy(
                    update={
                        "liquidity_volume_flow": lf.model_copy(
                            update={"underlying_liquidity_term": "evaluated"}
                        )
                    }
                )
            }
        )
        assert canonical_json(undeclared) == canonical_json(defaulted)

    def test_the_022_pre_draft_defaults_do_not_move_the_021_pin(self, protocol_path):
        """(0.2.2 lane, owner ruling m4-022-ruling-20260828) The two further
        pre-draft fields (`earnings_evaluation`, `fill_door_decision_close`)
        ride the same era-gated discipline as `underlying_liquidity_term`:
        stamped-at-default on a 0.2.1 protocol they hash to the PIN — the
        schema additions may never silently re-hash the untouched yaml."""
        from tree_options.protocol.loader import canonical_json, load_protocol

        undeclared = load_protocol(protocol_path)
        defaulted = undeclared.model_copy(
            update={
                "option_candidate_defaults": undeclared.option_candidate_defaults.model_copy(
                    update={
                        "earnings_evaluation": "evaluated",
                    }
                ),
                "fills": undeclared.fills.model_copy(
                    update={"fill_door_decision_close": "execution_calendar"}
                ),
            }
        )
        assert canonical_json(undeclared) == canonical_json(defaulted)
        from tree_options.protocol.loader import protocol_hash

        assert protocol_hash(defaulted) == self.LEDGER_BOUND_PROTOCOL_SHA256

    def test_a_022_protocol_rides_the_declared_fields_on_the_hash(self, protocol_path):
        """(0.2.2 declaration 1) At 0.2.2 NOTHING is stripped: the DECLARED
        values — including a DECLARED `underlying_liquidity_term: evaluated`
        (equal to its default!) — ride the hash by design, so the projected
        post-flip identity differs from the 0.2.1 pin on exactly the packet's
        content (version, amendment record, the three declarations)."""
        from tree_options.protocol.loader import canonical_json, load_protocol, protocol_hash

        base = load_protocol(protocol_path)
        data = base.model_dump(mode="json")
        data["meta"]["protocol_version"] = "0.2.2"
        data["meta"]["amendments"].append(
            {
                "version": "0.2.2",
                "date": "PENDING-OWNER-RATIFICATION",
                "decision": "unit: the 0.2.2 lane-on declarations",
                "changes": "unit fixture",
            }
        )
        data["option_candidate_defaults"]["liquidity_volume_flow"]["underlying_liquidity_term"] = (
            "evaluated"
        )
        data["option_candidate_defaults"]["earnings_evaluation"] = "disclosed_absence"
        data["fills"]["fill_door_decision_close"] = "decision_grid"
        from tree_options.protocol.schema import ResearchProtocol

        proposed = ResearchProtocol.model_validate(data)
        assert proposed.meta.protocol_version == "0.2.2"
        # the DECLARED "evaluated" rides the canonical bytes at 0.2.2 (it is
        # stripped below 0.2.2) — the version gate is the whole difference
        assert '"underlying_liquidity_term":"evaluated"' in canonical_json(proposed)
        assert '"earnings_evaluation":"disclosed_absence"' in canonical_json(proposed)
        assert '"fill_door_decision_close":"decision_grid"' in canonical_json(proposed)
        assert protocol_hash(proposed) != self.LEDGER_BOUND_PROTOCOL_SHA256

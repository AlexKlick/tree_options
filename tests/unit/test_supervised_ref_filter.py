"""The supervised orderRef filter: what the legacy runners never adopt."""

from __future__ import annotations

from tree_options.trex.desk_runtime import desk_order_ref
from tree_options.trex.ibkr import SUPERVISED_REF_PREFIXES, is_supervised_ref
from tree_options.trex.supervised_ibkr import SUPERVISED_REF_PREFIX, supervised_order_ref


def test_truth_table():
    assert is_supervised_ref(supervised_order_ref("canary-a"))
    assert is_supervised_ref(desk_order_ref("canary-a"))
    assert is_supervised_ref("trex:sup:")
    assert not is_supervised_ref("trex:nvda-oct")  # a legacy trex tag is legacy
    assert not is_supervised_ref("")
    assert not is_supervised_ref(None)
    assert not is_supervised_ref("trex:")


def test_the_prefix_tuple_cannot_drift_from_its_owners():
    """The tuple is defined in ibkr (no import cycle); the owners own the
    wire truth, so any drift must fail here."""
    assert SUPERVISED_REF_PREFIXES == (SUPERVISED_REF_PREFIX, "trex:desk:")
    assert SUPERVISED_REF_PREFIXES[1] + "x" == desk_order_ref("x")

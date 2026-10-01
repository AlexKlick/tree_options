from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tree_options.strategy_lab.contracts import Observation
from tree_options.strategy_lab.point_in_time import (
    PointInTimeViolation,
    latest_available,
    require_available,
)


def obs(source_id, available):
    return Observation(
        entity_id="A",
        event_at=datetime(2026, 1, 1, tzinfo=UTC),
        available_at=available,
        values={"x": Decimal("1")},
        source="fixture",
        source_id=source_id,
    )


def test_future_observation_refused():
    decision = datetime(2026, 1, 2, tzinfo=UTC)
    with pytest.raises(PointInTimeViolation):
        require_available(obs("future", decision + timedelta(seconds=1)), decision_at=decision)


def test_latest_available_never_sees_future():
    decision = datetime(2026, 1, 2, tzinfo=UTC)
    early = obs("early", decision - timedelta(hours=2))
    late = obs("late", decision - timedelta(hours=1))
    future = obs("future", decision + timedelta(hours=1))
    assert latest_available((early, future, late), entity_id="A", decision_at=decision) == late


def test_observation_cannot_be_available_before_event():
    event = datetime(2026, 1, 2, tzinfo=UTC)
    with pytest.raises(ValueError, match="available_at must be >= event_at"):
        Observation(
            entity_id="A",
            event_at=event,
            available_at=event - timedelta(seconds=1),
            values={"x": Decimal("1")},
            source="fixture",
            source_id="bad-time-order",
        )

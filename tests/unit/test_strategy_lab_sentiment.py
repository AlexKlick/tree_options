from datetime import UTC, datetime, timedelta

import pytest

from tree_options.strategy_lab.sentiment import SentimentObservation, aggregate_engagement


def row(entity, *, available, likes=100, comments=30):
    return SentimentObservation(
        entity_id=entity,
        event_at=available - timedelta(minutes=1),
        available_at=available,
        likes=likes,
        comments=comments,
        source="fixture",
        source_id=f"{entity}-{available.timestamp()}",
    )


def test_future_social_event_does_not_enter_signal():
    cut = datetime(2026, 1, 2, tzinfo=UTC)
    result = aggregate_engagement(
        [
            row("A", available=cut - timedelta(hours=1)),
            row("B", available=cut + timedelta(seconds=1)),
        ],
        decision_at=cut,
    )
    assert set(result) == {"A"}


def test_social_observation_cannot_precede_event():
    event = datetime(2026, 1, 2, tzinfo=UTC)
    with pytest.raises(ValueError, match="available_at must be >= event_at"):
        SentimentObservation(
            entity_id="A",
            event_at=event,
            available_at=event - timedelta(seconds=1),
            likes=1,
            comments=1,
            source="fixture",
            source_id="bad-time-order",
        )

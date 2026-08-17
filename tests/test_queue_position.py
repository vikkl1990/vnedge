"""Deterministic size-ahead queue-position model."""

import pytest

from vnedge.scalping.queue_position import (
    CancelPolicy,
    JoinPolicy,
    QueueModelConfig,
    QueuePositionModel,
    QueueSide,
)


def model_with_bid(size: float = 10.0) -> QueuePositionModel:
    model = QueuePositionModel()
    model.on_book_level(QueueSide.BID, 100.0, size)
    return model


def test_queue_ahead_must_clear_before_partial_fill() -> None:
    model = model_with_bid(10.0)
    order = model.insert("o1", QueueSide.BID, 100.0, 8.0, 1)

    assert model.on_trade(
        price=100.0, size=10.0, aggressor_buy=False, ts_ms=2
    ) == ()
    assert order.queue_ahead == 0.0

    (fill,) = model.on_trade(
        price=100.0, size=5.0, aggressor_buy=False, ts_ms=3
    )
    assert fill.order_id == "o1"
    assert fill.quantity == 5.0
    assert fill.complete is False
    assert order.remaining_size == 3.0


def test_fifo_orders_do_not_double_consume_shared_queue() -> None:
    model = model_with_bid(2.0)
    first = model.insert("first", QueueSide.BID, 100.0, 1.0, 1)
    second = model.insert("second", QueueSide.BID, 100.0, 2.0, 2)
    assert first.queue_ahead == 2.0
    assert second.queue_ahead == 3.0

    fills = model.on_trade(
        price=100.0, size=4.0, aggressor_buy=False, ts_ms=3
    )
    assert [(fill.order_id, fill.quantity) for fill in fills] == [
        ("first", 1.0),
        ("second", 1.0),
    ]
    assert second.remaining_size == 1.0
    assert second.queue_ahead == 0.0


def test_wrong_aggressor_or_unobserved_price_does_not_fill() -> None:
    model = model_with_bid()
    model.insert("o1", QueueSide.BID, 100.0, 1.0, 1)
    assert model.on_trade(
        price=100.0, size=20.0, aggressor_buy=True, ts_ms=2
    ) == ()
    assert model.on_trade(
        price=99.9, size=20.0, aggressor_buy=False, ts_ms=3
    ) == ()


def test_cancel_removes_order_and_prevents_fill() -> None:
    model = model_with_bid(0.0)
    model.insert("o1", QueueSide.BID, 100.0, 1.0, 1)
    assert model.cancel("o1") is not None
    assert model.cancel("o1") is None
    assert model.on_trade(
        price=100.0, size=1.0, aggressor_buy=False, ts_ms=2
    ) == ()


def test_pessimistic_front_join_increases_queue_ahead() -> None:
    model = model_with_bid(10.0)
    order = model.insert("o1", QueueSide.BID, 100.0, 1.0, 1)
    model.on_book_level(QueueSide.BID, 100.0, 15.0)
    assert order.queue_ahead == 15.0


def test_pro_rata_cancel_reduces_queue_ahead() -> None:
    model = model_with_bid(10.0)
    order = model.insert("o1", QueueSide.BID, 100.0, 1.0, 1)
    model.on_book_level(QueueSide.BID, 100.0, 4.0)
    assert order.queue_ahead == 4.0


def test_behind_join_policy_does_not_worsen_priority() -> None:
    model = QueuePositionModel(
        QueueModelConfig(
            join_policy=JoinPolicy.BEHIND,
            cancel_policy=CancelPolicy.PRO_RATA,
        )
    )
    model.on_book_level(QueueSide.ASK, 101.0, 5.0)
    order = model.insert("o1", QueueSide.ASK, 101.0, 1.0, 1)
    model.on_book_level(QueueSide.ASK, 101.0, 20.0)
    assert order.queue_ahead == 5.0


def test_modify_loses_priority_and_rejoins_current_queue() -> None:
    model = model_with_bid(5.0)
    model.on_book_level(QueueSide.BID, 99.9, 12.0)
    model.insert("o1", QueueSide.BID, 100.0, 1.0, 1)
    modified = model.modify("o1", price=99.9, size=2.0, ts_ms=5)
    assert modified.queue_ahead == 12.0
    assert modified.ts_insert_ms == 5


def test_invalid_or_unobserved_inputs_fail_closed() -> None:
    model = QueuePositionModel()
    with pytest.raises(ValueError, match="observed book level"):
        model.insert("o1", QueueSide.BID, 100.0, 1.0, 1)
    with pytest.raises(ValueError, match="positive"):
        model.on_book_level(QueueSide.BID, 0.0, 1.0)
    model.on_book_level(QueueSide.BID, 100.0, 0.0)
    with pytest.raises(ValueError, match="positive"):
        model.on_trade(price=100.0, size=0.0, aggressor_buy=False, ts_ms=2)

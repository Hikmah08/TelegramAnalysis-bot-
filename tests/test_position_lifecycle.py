import pytest
import sqlite3
import pandas as pd
from pathlib import Path
from src.paper.lifecycle_calculator import PositionLifecycleCalculator, PositionCalculations
from src.paper.position_monitor import PositionMonitorService
from src.storage.database import StorageManager


def test_position_calculations_long_profit_and_loss():
    # Long trade: Entry 50000, TP 52000, SL 49000, Size 1.0 (Initial risk = 1000)
    pos = {
        "position_id": 101,
        "user_id": "USER_A",
        "symbol": "BTCUSDT",
        "direction": "bullish",
        "entry_price": 50000.0,
        "stop_loss": 49000.0,
        "take_profit": 52000.0,
        "position_size": 1.0,
        "initial_risk": 1000.0,
        "entry_time": "2026-11-04T10:00:00+00:00",
    }

    # Case 1: In profit (+1000, halfway to TP 52000)
    calc_profit = PositionLifecycleCalculator.calculate_metrics(pos, current_price=51000.0)
    assert calc_profit.unrealized_pnl == 1000.0
    assert calc_profit.r_multiple == 1.0
    assert calc_profit.target_progress_pct == 50.0
    assert calc_profit.distance_from_entry == 1000.0
    assert calc_profit.distance_to_tp == 1000.0
    assert calc_profit.distance_to_sl == 2000.0

    # Case 2: In loss (-500, halfway to SL 49000)
    calc_loss = PositionLifecycleCalculator.calculate_metrics(pos, current_price=49500.0)
    assert calc_loss.unrealized_pnl == -500.0
    assert calc_loss.r_multiple == -0.5
    assert calc_loss.target_progress_pct == -25.0
    assert calc_loss.distance_from_entry == -500.0
    assert calc_loss.distance_to_tp == 2500.0
    assert calc_loss.distance_to_sl == 500.0


def test_position_calculations_short_profit_and_loss():
    # Short trade: Entry 2000.0, TP 1800.0, SL 2100.0, Size 2.0 (Initial risk = 200.0)
    pos = {
        "position_id": 102,
        "user_id": "USER_B",
        "symbol": "ETHUSDT",
        "direction": "bearish",
        "entry_price": 2000.0,
        "stop_loss": 2100.0,
        "take_profit": 1800.0,
        "position_size": 2.0,
        "initial_risk": 200.0,
        "entry_time": "2026-11-04T10:00:00+00:00",
    }

    # Case 1: In profit (Current price 1900 -> +200 profit, +1.0R, 50% target progress)
    calc_profit = PositionLifecycleCalculator.calculate_metrics(pos, current_price=1900.0)
    assert calc_profit.unrealized_pnl == 200.0
    assert calc_profit.r_multiple == 1.0
    assert calc_profit.target_progress_pct == 50.0
    assert calc_profit.distance_from_entry == 100.0
    assert calc_profit.distance_to_tp == 100.0
    assert calc_profit.distance_to_sl == 200.0

    # Case 2: In loss (Current price 2050 -> -100 loss, -0.5R, -25% target progress)
    calc_loss = PositionLifecycleCalculator.calculate_metrics(pos, current_price=2050.0)
    assert calc_loss.unrealized_pnl == -100.0
    assert calc_loss.r_multiple == -0.5
    assert calc_loss.target_progress_pct == -25.0


def test_exit_conditions_conservative_intra_candle():
    # Long trade: SL 100, TP 120
    # Both SL and TP touched within high/low candle (Low 95 <= 100 and High 125 >= 120)
    hit, exit_p, reason = PositionLifecycleCalculator.evaluate_exit_conditions(
        direction="bullish",
        stop_loss=100.0,
        take_profit=120.0,
        candle_high=125.0,
        candle_low=95.0,
        current_price=115.0,
    )
    assert hit is True
    assert reason == "stop_loss"  # Conservative ambiguity resolution


def test_position_monitor_lifecycle_atomic(tmp_path):
    test_db = tmp_path / "test_lifecycle.db"
    storage = StorageManager(db_path=test_db, primary_owner_id="USER_TEST")

    # Open paper position
    pos_id = storage.open_paper_trade(
        symbol="BTCUSDT",
        direction="bullish",
        entry_price=60000.0,
        stop_loss=59000.0,
        take_profit=64000.0,
        position_size=0.1,
        user_id="USER_TEST",
        initial_risk=100.0,
    )
    assert pos_id > 0

    # Initial balance is 10000.0
    bal_initial = float(storage.get_setting("current_balance", user_id="USER_TEST"))
    assert bal_initial == 10000.0

    # Update monitoring state
    updated = storage.update_position_monitoring_state(
        position_id=pos_id,
        current_price=62000.0,
        unrealized_pnl=200.0,
        r_multiple=2.0,
        target_progress_pct=50.0,
        price_timestamp="2026-11-04T12:00:00+00:00",
    )
    assert updated is True

    # Verify state persisted in database
    p_row = storage.get_paper_position(pos_id, user_id="USER_TEST")
    assert p_row["current_price"] == 62000.0
    assert p_row["unrealized_pnl"] == 200.0
    assert p_row["r_multiple"] == 2.0
    assert p_row["target_progress_pct"] == 50.0

    # Close atomically via TP
    closed = storage.close_paper_trade_atomic(
        position_id=pos_id,
        user_id="USER_TEST",
        exit_price=64000.0,
        exit_reason="take_profit",
        pnl=400.0,
        fee_paid=3.84,
        notification_event={"event_type": "TP_HIT", "symbol": "BTCUSDT"},
    )
    assert closed is True

    # Balance updated once
    bal_after = float(storage.get_setting("current_balance", user_id="USER_TEST"))
    assert bal_after == 10400.0

    # Attempting to close again must fail (idempotent atomic protection)
    closed_again = storage.close_paper_trade_atomic(
        position_id=pos_id,
        user_id="USER_TEST",
        exit_price=64000.0,
        exit_reason="take_profit",
        pnl=400.0,
    )
    assert closed_again is False

    # Outbox notification was created
    with storage._get_connection() as conn:
        outbox = conn.cursor().execute("SELECT * FROM outbox_notifications WHERE user_id = 'USER_TEST'").fetchall()
        assert len(outbox) == 1
        assert outbox[0]["status"] == "PENDING"

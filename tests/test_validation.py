from datetime import datetime, timezone
import pytest
import pandas as pd
from src.data.models import Timeframe, Candle
from src.utils.validation import filter_and_validate_candles


def test_timeframe_properties():
    assert Timeframe.M1.minutes == 1
    assert Timeframe.M5.minutes == 5
    assert Timeframe.M15.minutes == 15
    assert Timeframe.H1.minutes == 60
    assert Timeframe.H4.minutes == 240


def test_candle_model():
    ts = pd.Timestamp("2026-11-02 12:00:00", tz="UTC")
    close_ts = pd.Timestamp("2026-11-02 12:59:59.999", tz="UTC")
    candle = Candle(
        timestamp=ts,
        open=100.0,
        high=110.0,
        low=95.0,
        close=105.0,
        volume=150.0,
        close_time=close_ts,
    )
    assert candle.open == 100.0
    assert candle.high == 110.0
    d = candle.to_dict()
    assert d["close"] == 105.0
    assert d["volume"] == 150.0


def test_filter_removes_incomplete_candle():
    # 3 candles: 2 fully completed, 1 currently active
    now_utc = datetime(2026, 11, 2, 12, 30, tzinfo=timezone.utc)

    data = {
        "timestamp": [
            "2026-11-02 10:00:00",
            "2026-11-02 11:00:00",
            "2026-11-02 12:00:00",
        ],
        "open": [100.0, 102.0, 104.0],
        "high": [103.0, 105.0, 106.0],
        "low": [99.0, 101.0, 103.0],
        "close": [102.0, 104.0, 105.0],
        "volume": [10.0, 15.0, 8.0],
        "close_time": [
            "2026-11-02 10:59:59.999",
            "2026-11-02 11:59:59.999",
            "2026-11-02 12:59:59.999",  # Active/incomplete at 12:30
        ],
    }
    df = pd.DataFrame(data)
    cleaned = filter_and_validate_candles(df, current_utc_time=now_utc)

    assert len(cleaned) == 2
    assert cleaned.iloc[-1]["timestamp"] == pd.Timestamp("2026-11-02 11:00:00", tz="UTC")


def test_filter_discards_corrupt_price_candles():
    now_utc = datetime(2026, 11, 2, 13, 0, tzinfo=timezone.utc)

    # high < low is invalid
    data = {
        "timestamp": ["2026-11-02 10:00:00", "2026-11-02 11:00:00"],
        "open": [100.0, 100.0],
        "high": [90.0, 110.0],  # Invalid: high 90 < low 95
        "low": [95.0, 95.0],
        "close": [98.0, 105.0],
        "volume": [10.0, 20.0],
        "close_time": ["2026-11-02 10:59:59.999", "2026-11-02 11:59:59.999"],
    }
    df = pd.DataFrame(data)
    cleaned = filter_and_validate_candles(df, current_utc_time=now_utc)

    assert len(cleaned) == 1
    assert cleaned.iloc[0]["high"] == 110.0

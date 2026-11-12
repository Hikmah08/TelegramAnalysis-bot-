from unittest.mock import Mock, patch
from datetime import datetime, timezone
import pytest
import pandas as pd
from src.data.binance_public import BinancePublicDataProvider
from src.data.models import Timeframe


@pytest.fixture
def mock_binance_response():
    # 2 mock candles from Binance klines endpoint
    return [
        [
            1793617200000,  # 2026-11-02 11:00:00 UTC
            "100000.0",
            "101000.0",
            "99500.0",
            "100500.0",
            "120.5",
            1793620799999,  # 2026-11-02 11:59:59.999 UTC
            "12080000.0",
            1500,
            "60.0",
            "6020000.0",
            "0",
        ],
        [
            1793620800000,  # 2026-11-02 12:00:00 UTC
            "100500.0",
            "102000.0",
            "100200.0",
            "101800.0",
            "95.2",
            1793624399999,  # 2026-11-02 12:59:59.999 UTC
            "9650000.0",
            1100,
            "40.0",
            "4060000.0",
            "0",
        ],
    ]


def test_binance_fetcher_normalization(mock_binance_response):
    provider = BinancePublicDataProvider()
    
    mock_resp = Mock()
    mock_resp.json.return_value = mock_binance_response
    mock_resp.raise_for_status = Mock()

    with patch.object(provider.session, "get", return_value=mock_resp):
        with patch.object(
            provider,
            "get_server_time",
            return_value=datetime.fromtimestamp(1793624400, tz=timezone.utc),  # 13:00 UTC (both closed)
        ):
            df = provider.fetch_candles("BTC/USDT", Timeframe.H1, limit=10)

    assert len(df) == 2
    assert "close" in df.columns
    assert df.iloc[0]["open"] == 100000.0
    assert df.iloc[1]["close"] == 101800.0
    assert df.iloc[0]["timestamp"] < df.iloc[1]["timestamp"]


def test_binance_fetcher_filters_unfinished_candle(mock_binance_response):
    provider = BinancePublicDataProvider()
    
    mock_resp = Mock()
    mock_resp.json.return_value = mock_binance_response
    mock_resp.raise_for_status = Mock()

    # Time is set to 12:30:00 UTC (second candle is still open/in-progress)
    server_time = datetime.fromtimestamp(1793622600, tz=timezone.utc)

    with patch.object(provider.session, "get", return_value=mock_resp):
        with patch.object(provider, "get_server_time", return_value=server_time):
            df = provider.fetch_candles("BTCUSDT", Timeframe.H1, limit=10)

    # Second candle should be dropped
    assert len(df) == 1
    assert df.iloc[0]["close_time"] <= pd.Timestamp(server_time)

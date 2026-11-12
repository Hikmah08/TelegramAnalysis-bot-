import pytest
import pandas as pd
from datetime import datetime, timezone
from src.strategy.models import Direction
from src.strategy.mtf_models import MultiTimeframeSetup, HigherTimeframeBias, BiasState, AreaOfInterest
from src.strategy.models import SetupCandidate, SetupStage, LiquiditySweep, MarketStructureBreak, StructureType, FairValueGap, SwingPoint, SwingType
from src.paper import PaperTradingEngine, PaperAccount, PaperOrderStatus, PaperExitReason
from src.alerts import TelegramAlertService, TelegramConfig
from src.dashboard import create_dashboard_app


def test_paper_trading_lifecycle():
    account = PaperAccount(initial_balance=10000.0, risk_per_trade_pct=1.0, target_risk_reward=2.0)
    engine = PaperTradingEngine(account=account)

    # Setup with Entry 100, Stop 90 (Risk 10, TP 120 for Bullish)
    bias = HigherTimeframeBias(direction=BiasState.BULLISH, timestamp=pd.Timestamp.now(tz="UTC"), reason="MSS")
    aoi = AreaOfInterest(direction=Direction.BULLISH, aoi_type="1H_FVG", top_price=105, bottom_price=95, timestamp=pd.Timestamp.now(tz="UTC"))
    dummy_setup = SetupCandidate(direction=Direction.BULLISH, timeframe="15m", stage=SetupStage.ENTRY_RETRACED)

    mtf_setup = MultiTimeframeSetup(
        direction=Direction.BULLISH,
        bias_4h=bias,
        aoi_1h=aoi,
        setup_15m=dummy_setup,
        entry_price=100.0,
        stop_loss=90.0,
        timestamp=pd.Timestamp("2026-11-02 12:00:00", tz="UTC"),
    )

    # 1. Process virtual order entry
    pos = engine.process_setup_candidate(mtf_setup)
    assert pos is not None
    assert pos.status == PaperOrderStatus.FILLED
    assert len(account.open_positions) == 1

    # 2. Candle that touches Take Profit (High reaches 125 > 120)
    c_tp = pd.Series({
        "timestamp": pd.Timestamp("2026-11-02 12:15:00", tz="UTC"),
        "open": 105.0,
        "high": 125.0,
        "low": 104.0,
        "close": 122.0,
    })
    closed = engine.update_with_candle(c_tp)
    assert len(closed) == 1
    assert closed[0].exit_reason == PaperExitReason.TAKE_PROFIT
    assert closed[0].pnl > 0.0
    assert len(account.open_positions) == 0
    assert len(account.closed_positions) == 1
    assert account.current_balance > 10000.0


def test_telegram_alert_service_offline_mode():
    service = TelegramAlertService(TelegramConfig(enabled=False))
    # In offline mode, sending returns False safely without HTTP calls or errors
    assert service.send_message("Test message") is False
    assert service.send_test_alert() is False


def test_telegram_alert_service_enabled_success():
    from unittest.mock import patch, Mock
    config = TelegramConfig(bot_token="12345:ABCDEF", chat_id="999888", enabled=True)
    service = TelegramAlertService(config)

    mock_resp = Mock()
    mock_resp.status_code = 200

    with patch("requests.post", return_value=mock_resp) as mock_post:
        success = service.send_test_alert()
        assert success is True
        mock_post.assert_called_once()
        call_url = mock_post.call_args[0][0]
        call_json = mock_post.call_args[1]["json"]
        assert "12345:ABCDEF" in call_url
        assert call_json["chat_id"] == "999888"
        assert "Trading bot Telegram connection test successful" in call_json["text"]


def test_telegram_alert_service_error_handling():
    from unittest.mock import patch, Mock
    config = TelegramConfig(bot_token="invalid_token", chat_id="999888", enabled=True)
    service = TelegramAlertService(config)

    # 1. Non-200 HTTP response (e.g. 401 Unauthorized)
    mock_err_resp = Mock()
    mock_err_resp.status_code = 401
    with patch("requests.post", return_value=mock_err_resp):
        assert service.send_message("Hello") is False

    # 2. Connection error / Timeout
    with patch("requests.post", side_effect=Exception("Connection timed out")):
        assert service.send_message("Hello") is False


def test_telegram_discover_chat_id_safe():
    from unittest.mock import patch, Mock
    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "ok": True,
        "result": [
            {
                "message": {
                    "chat": {
                        "id": 12345678,
                        "type": "private",
                        "username": "trader_joe",
                        "first_name": "Joe",
                    }
                }
            }
        ],
    }

    with patch("requests.get", return_value=mock_resp):
        chats = TelegramAlertService.discover_chat_id("dummy_token")
        assert len(chats) == 1
        assert chats[0]["chat_id"] == 12345678
        assert chats[0]["username"] == "trader_joe"


def test_dashboard_creation():
    account = PaperAccount()
    last_scan = {
        "timestamp": "2026-11-02 12:00:00 UTC",
        "symbol": "BTCUSDT",
        "bias_4h": "BULLISH",
        "setups": [],
    }
    app = create_dashboard_app(account, last_scan)
    assert app.title == "ICT/SMC Trading Analysis Bot Dashboard"

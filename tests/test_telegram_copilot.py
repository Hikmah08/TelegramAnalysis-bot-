import pytest
import tempfile
import pandas as pd
from pathlib import Path
from unittest.mock import Mock, patch
from src.alerts import TelegramConfig, TelegramAlertService, TelegramBotHandler
from src.storage import StorageManager
from src.strategy.models import Direction
from src.strategy.mtf_models import MultiTimeframeSetup, HigherTimeframeBias, BiasState, AreaOfInterest
from src.strategy.models import SetupCandidate, SetupStage


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    storage = StorageManager(db_path=db_path)
    yield storage
    try:
        db_path.unlink()
    except Exception:
        pass


@pytest.fixture
def bot_handler(temp_db):
    config = TelegramConfig(bot_token="test_token", chat_id="123456", enabled=True)
    service = TelegramAlertService(config)
    # Mock send_message and answer_callback_query so tests make zero external calls
    service.send_message = Mock(return_value=True)
    service.answer_callback_query = Mock(return_value=True)
    return TelegramBotHandler(telegram_service=service, storage=temp_db)


# 1. Authorization & Security
def test_unauthorized_user_rejection(bot_handler):
    unauth_update = {
        "message": {
            "chat": {"id": 999999},  # Unauthorized chat ID
            "text": "/menu",
        }
    }
    res = bot_handler.process_incoming_update(unauth_update)
    assert res["status"] == "unauthorized"
    bot_handler.telegram.send_message.assert_called_with("⛔ <b>Access Denied:</b> You are not authorized to control this trading bot.")


# 2. Main Menu Navigation
def test_menu_navigation(bot_handler):
    auth_update = {
        "message": {
            "chat": {"id": 123456},
            "text": "/menu",
        }
    }
    res = bot_handler.process_incoming_update(auth_update)
    assert res["status"] == "menu_sent"
    bot_handler.telegram.send_message.assert_called()


# 3. Watchlist Add & Remove
def test_watchlist_management(bot_handler):
    # Add ETHUSDT
    add_update = {"message": {"chat": {"id": 123456}, "text": "/watchlist add ETHUSDT"}}
    res_add = bot_handler.process_incoming_update(add_update)
    assert res_add["status"] == "symbol_added"
    assert any(item["symbol"] == "ETHUSDT" for item in bot_handler.storage.get_watchlist())

    # Remove ETHUSDT
    del_update = {"message": {"chat": {"id": 123456}, "text": "/watchlist del ETHUSDT"}}
    res_del = bot_handler.process_incoming_update(del_update)
    assert res_del["status"] == "symbol_removed"
    assert not any(item["symbol"] == "ETHUSDT" for item in bot_handler.storage.get_watchlist())


# 4. Settings Persistence
def test_settings_persistence(bot_handler):
    bot_handler.storage.set_setting("risk_per_trade_pct", "2.5")
    bot_handler.storage.set_setting("target_risk_reward", "4.0")

    settings = bot_handler.storage.get_all_settings()
    assert settings["risk_per_trade_pct"] == "2.5"
    assert settings["target_risk_reward"] == "4.0"


# 5. Account Balance Retrieval
def test_account_summary_retrieval(bot_handler):
    update = {"message": {"chat": {"id": 123456}, "text": "/account"}}
    res = bot_handler.process_incoming_update(update)
    assert res["status"] == "account_sent"
    bot_handler.telegram.send_message.assert_called()


# 6. Concise Trade Message Formatting (No alert flooding)
def test_concise_trade_message_formatting(bot_handler):
    bias = HigherTimeframeBias(direction=BiasState.BULLISH, timestamp=pd.Timestamp.now(tz="UTC"), reason="MSS")
    aoi = AreaOfInterest(direction=Direction.BULLISH, aoi_type="1H_FVG", top_price=105, bottom_price=95, timestamp=pd.Timestamp.now(tz="UTC"))
    dummy = SetupCandidate(direction=Direction.BULLISH, timeframe="15m", stage=SetupStage.ENTRY_RETRACED)

    mtf_setup = MultiTimeframeSetup(
        direction=Direction.BULLISH,
        bias_4h=bias,
        aoi_1h=aoi,
        setup_15m=dummy,
        entry_price=100.0,
        stop_loss=90.0,
        audit_trail=["4H: Bullish", "1H: AOI touched", "5M: Confirmed"],
    )

    msg = bot_handler.telegram.format_opportunity_message(mtf_setup, "BTCUSDT")
    assert "NEW ICT/SMC TRADE OPPORTUNITY" in msg
    assert "BTCUSDT" in msg
    assert "4:1 R:R" in msg
    assert "4H: Bullish" in msg


# 7. Paper Trade Approval via Callback
def test_paper_trade_approval_callback(bot_handler):
    # Save a signal
    sig_id = "BTC_TEST_SIG_1"
    bot_handler.storage.save_signal(
        signal_id=sig_id,
        symbol="BTCUSDT",
        direction="bullish",
        entry_price=80000.0,
        stop_loss=79000.0,
        take_profit=84000.0,
        audit_notes=["Tested"],
        validity_minutes=30,
    )

    cb_update = {
        "callback_query": {
            "id": "cb_1",
            "message": {"chat": {"id": 123456}},
            "data": f"approve_{sig_id}",
        }
    }
    res = bot_handler.process_incoming_update(cb_update)
    assert res["status"] == "approved"
    assert "position_id" in res

    # Verify opened in paper positions table
    open_pos = bot_handler.storage.get_open_positions()
    assert len(open_pos) == 1
    assert open_pos[0]["symbol"] == "BTCUSDT"
    assert open_pos[0]["entry_price"] == 80000.0


# 8. Duplicate Callback Prevention
def test_duplicate_callback_prevention(bot_handler):
    sig_id = "BTC_TEST_SIG_DUP"
    bot_handler.storage.save_signal(
        signal_id=sig_id,
        symbol="BTCUSDT",
        direction="bullish",
        entry_price=80000.0,
        stop_loss=79000.0,
        take_profit=84000.0,
        audit_notes=["Tested"],
        validity_minutes=30,
    )

    cb_update = {
        "callback_query": {
            "id": "cb_dup",
            "message": {"chat": {"id": 123456}},
            "data": f"approve_{sig_id}",
        }
    }
    # First press
    res1 = bot_handler.process_incoming_update(cb_update)
    assert res1["status"] == "approved"

    # Second duplicate press
    res2 = bot_handler.process_incoming_update(cb_update)
    assert res2["status"] == "already_approved"


# 9. Trade Rejection Callback
def test_trade_rejection_callback(bot_handler):
    sig_id = "BTC_TEST_SIG_REJ"
    bot_handler.storage.save_signal(
        signal_id=sig_id,
        symbol="BTCUSDT",
        direction="bearish",
        entry_price=80000.0,
        stop_loss=81000.0,
        take_profit=76000.0,
        audit_notes=["Tested"],
        validity_minutes=30,
    )

    cb_update = {
        "callback_query": {
            "id": "cb_rej",
            "message": {"chat": {"id": 123456}},
            "data": f"reject_{sig_id}",
        }
    }
    res = bot_handler.process_incoming_update(cb_update)
    assert res["status"] == "rejected"

    # Verify signal status in storage
    sig = bot_handler.storage.get_signal(sig_id)
    assert sig["status"] == "REJECTED"


# 10. Expired Signal Rejection
def test_expired_signal_rejection(bot_handler):
    sig_id = "BTC_TEST_SIG_EXP"
    # Create with negative validity so it is immediately expired
    bot_handler.storage.save_signal(
        signal_id=sig_id,
        symbol="BTCUSDT",
        direction="bullish",
        entry_price=80000.0,
        stop_loss=79000.0,
        take_profit=84000.0,
        audit_notes=["Tested"],
        validity_minutes=-5,  # Expired 5 mins ago
    )

    cb_update = {
        "callback_query": {
            "id": "cb_exp",
            "message": {"chat": {"id": 123456}},
            "data": f"approve_{sig_id}",
        }
    }
    res = bot_handler.process_incoming_update(cb_update)
    assert res["status"] == "expired"


# 11. On-Demand Analysis "No Opportunities" Handling
def test_on_demand_analysis_no_opportunities(bot_handler):
    with patch("src.strategy.mtf_orchestrator.MultiTimeframeOrchestrator.scan_market", return_value=[]):
        res = bot_handler.trigger_on_demand_analysis("BTCUSDT")
        assert res["status"] == "no_opportunities"
        assert res["count"] == 0


# 12. Additional Command Aliases (/balance, /performance, /help)
def test_command_aliases(bot_handler):
    update_bal = {"message": {"chat": {"id": 123456}, "text": "/balance"}}
    res_bal = bot_handler.process_incoming_update(update_bal)
    assert res_bal["status"] == "account_sent"

    update_perf = {"message": {"chat": {"id": 123456}, "text": "/performance"}}
    res_perf = bot_handler.process_incoming_update(update_perf)
    assert res_perf["status"] == "account_sent"

    update_help = {"message": {"chat": {"id": 123456}, "text": "/help"}}
    res_help = bot_handler.process_incoming_update(update_help)
    assert res_help["status"] == "help_sent"


# 13. set_bot_commands Registration
def test_telegram_set_bot_commands(bot_handler):
    with patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 200
        ok = bot_handler.telegram.set_bot_commands()
        assert ok is True
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert "/setMyCommands" in args[0]
        commands = kwargs["json"]["commands"]
        cmd_names = [c["command"] for c in commands]
        assert "start" in cmd_names
        assert "menu" in cmd_names
        assert "analyze" in cmd_names
        assert "watchlist" in cmd_names
        assert "balance" in cmd_names
        assert "positions" in cmd_names
        assert "history" in cmd_names
        assert "performance" in cmd_names
        assert "settings" in cmd_names
        assert "close" in cmd_names
        assert "help" in cmd_names



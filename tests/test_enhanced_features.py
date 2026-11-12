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
from src.strategy.ranker import SetupRanker
from src.paper.account_service import AccountService
from src.paper.engine import PaperTradingEngine, PaperAccount, PaperExitReason


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
    service.send_message = Mock(return_value=True)
    service.answer_callback_query = Mock(return_value=True)
    return TelegramBotHandler(telegram_service=service, storage=temp_db)


# 1. Custom Pair Analysis & Unsupported Pair Rejection
def test_custom_pair_input_and_unsupported_rejection(bot_handler):
    # Valid crypto pair
    with patch("src.strategy.mtf_orchestrator.MultiTimeframeOrchestrator.scan_market", return_value=[]):
        res_valid = bot_handler._handle_command("/analyze BTCUSDT")
        assert res_valid["status"] == "no_opportunities"

    # Unsupported random ticker
    res_invalid = bot_handler._handle_command("/analyze UNKNOWNXYZ")
    assert res_invalid["status"] == "unsupported_instrument"
    assert res_invalid["symbol"] == "UNKNOWNXYZ"


# 2. Ranking and Top 2 Opportunity Selection
def test_setup_ranker_ranking_and_top_two():
    bias_bull = HigherTimeframeBias(direction=BiasState.BULLISH, timestamp=pd.Timestamp.now(tz="UTC"), reason="MSS")
    aoi_bull = AreaOfInterest(direction=Direction.BULLISH, aoi_type="1H_FVG", top_price=105, bottom_price=95, timestamp=pd.Timestamp.now(tz="UTC"))
    dummy_valid = SetupCandidate(direction=Direction.BULLISH, timeframe="15m", stage=SetupStage.ENTRY_RETRACED, is_valid=True)
    dummy_bear_valid = SetupCandidate(direction=Direction.BEARISH, timeframe="15m", stage=SetupStage.ENTRY_RETRACED, is_valid=True)

    # Setup 1 (Confluence with 5M confirmation)
    s1 = MultiTimeframeSetup(
        direction=Direction.BULLISH,
        bias_4h=bias_bull,
        aoi_1h=aoi_bull,
        setup_15m=dummy_valid,
        confirmation_5m=dummy_valid,
        entry_price=100.0,
        stop_loss=95.0,
        timestamp=pd.Timestamp("2026-11-02 12:00:00", tz="UTC"),
    )
    # Setup 2 (Lacks 5M confirmation -> lower score)
    s2 = MultiTimeframeSetup(
        direction=Direction.BULLISH,
        bias_4h=bias_bull,
        aoi_1h=aoi_bull,
        setup_15m=dummy_valid,
        confirmation_5m=None,
        entry_price=200.0,
        stop_loss=190.0,
        timestamp=pd.Timestamp("2026-11-02 12:05:00", tz="UTC"),
    )
    # Setup 3 (Duplicate of Setup 1 with almost identical entry)
    s3 = MultiTimeframeSetup(
        direction=Direction.BULLISH,
        bias_4h=bias_bull,
        aoi_1h=aoi_bull,
        setup_15m=dummy_valid,
        confirmation_5m=dummy_valid,
        entry_price=100.05,  # within 0.1% of s1
        stop_loss=95.0,
        timestamp=pd.Timestamp("2026-11-02 12:01:00", tz="UTC"),
    )
    # Setup 4 (Bearish setup with refinement)
    bias_bear = HigherTimeframeBias(direction=BiasState.BEARISH, timestamp=pd.Timestamp.now(tz="UTC"), reason="MSS")
    aoi_bear = AreaOfInterest(direction=Direction.BEARISH, aoi_type="1H_FVG", top_price=305, bottom_price=295, timestamp=pd.Timestamp.now(tz="UTC"))
    s4 = MultiTimeframeSetup(
        direction=Direction.BEARISH,
        bias_4h=bias_bear,
        aoi_1h=aoi_bear,
        setup_15m=dummy_bear_valid,
        confirmation_5m=dummy_bear_valid,
        refinement_1m=dummy_bear_valid,
        entry_price=300.0,
        stop_loss=305.0,
        timestamp=pd.Timestamp("2026-11-02 12:10:00", tz="UTC"),
    )

    ranked = SetupRanker.rank_and_deduplicate([s1, s2, s3, s4], max_results=2)
    # Must filter out s3 (duplicate of s1) and return top 2
    assert len(ranked) == 2
    # s4 has 1M refinement (+10 pts) and s1 has 5M confirmation, both outrank s2
    assert s4 in ranked
    assert s1 in ranked


# 3. Restoring Open Positions Across Restarts
def test_restore_open_positions_from_storage(temp_db):
    # Seed open position in storage
    pos_id = temp_db.open_paper_trade(
        symbol="BTCUSDT",
        direction="bullish",
        entry_price=80000.0,
        stop_loss=79000.0,
        take_profit=84000.0,
        position_size=0.1,
    )

    account = PaperAccount()
    engine = PaperTradingEngine(account=account)
    assert len(account.open_positions) == 0

    # Restore from storage
    engine.restore_from_storage(temp_db)
    assert len(account.open_positions) == 1
    assert pos_id in account.open_positions
    restored = account.open_positions[pos_id]
    assert restored.symbol == "BTCUSDT"
    assert restored.entry_price == 80000.0


# 4. Conservative Resolution for Intrabar Ambiguity (Both SL and TP touched)
def test_intrabar_ambiguity_conservative_stop_loss():
    account = PaperAccount(initial_balance=10000.0)
    engine = PaperTradingEngine(account=account)

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
        timestamp=pd.Timestamp("2026-11-02 12:00:00", tz="UTC"),
    )
    engine.process_setup_candidate(mtf_setup)

    # Intrabar ambiguous candle: High reaches 150 (above TP 140) AND Low reaches 80 (below SL 90)
    ambiguous_candle = pd.Series({
        "timestamp": pd.Timestamp("2026-11-02 12:15:00", tz="UTC"),
        "open": 100.0,
        "high": 150.0,
        "low": 80.0,
        "close": 110.0,
    })

    closed = engine.update_with_candle(ambiguous_candle)
    assert len(closed) == 1
    # Conservative rule: Must assume Stop Loss was hit first
    assert closed[0].exit_reason == PaperExitReason.STOP_LOSS
    assert closed[0].pnl < 0.0


# 5. Manual Closure of Paper Trade
def test_manual_position_closure(bot_handler):
    pos_id = bot_handler.storage.open_paper_trade(
        symbol="BTCUSDT",
        direction="bullish",
        entry_price=80000.0,
        stop_loss=79000.0,
        take_profit=84000.0,
        position_size=0.1,
    )
    res = bot_handler.manual_close_position(pos_id)
    assert res["status"] == "position_closed_manually"
    pos = bot_handler.storage.get_paper_position(pos_id)
    assert pos["status"] == "CLOSED"
    assert pos["exit_reason"] == "manual"


# 6. Telegram Dashboard View with Account Metrics & Refresh
def test_telegram_dashboard_view_and_refresh(bot_handler):
    update = {
        "message": {
            "chat": {"id": 123456},
            "text": "/dashboard",
        }
    }
    res = bot_handler.process_incoming_update(update)
    assert res["status"] == "dashboard_view_sent"
    assert "metrics" in res
    assert res["metrics"].starting_balance == 10000.0

    # Refresh via callback
    cb_update = {
        "callback_query": {
            "id": "cb_refresh",
            "message": {"chat": {"id": 123456}},
            "data": "dashboard_refresh",
        }
    }
    res_cb = bot_handler.process_incoming_update(cb_update)
    assert res_cb["status"] == "dashboard_view_sent"


# 7. Bot Settings Updates & Validation
def test_bot_settings_updates(bot_handler):
    # Valid Risk Update
    res = bot_handler._handle_command("/settings risk 2.0")
    assert res["status"] == "setting_updated"
    assert bot_handler.storage.get_setting("risk_per_trade_pct") == "2.0"

    # Invalid Risk Update
    res_inv = bot_handler._handle_command("/settings risk 50.0")
    assert res_inv["status"] == "validation_failed"

    # Valid R:R Update
    res_rr = bot_handler._handle_command("/settings rr 4.5")
    assert res_rr["status"] == "setting_updated"
    assert bot_handler.storage.get_setting("target_risk_reward") == "4.5"

    # Invalid R:R (must be >= 4.0)
    res_rr_inv = bot_handler._handle_command("/settings rr 2.0")
    assert res_rr_inv["status"] == "validation_failed"


# 8. Consistent Account Values between Telegram and Dashboard
def test_consistent_account_values_between_telegram_and_dashboard(temp_db):
    from src.dashboard.app import create_dashboard_app

    acct_svc = AccountService(storage=temp_db)
    metrics = acct_svc.get_metrics()

    app = create_dashboard_app(storage=temp_db)
    # Test route endpoint directly without requiring httpx testclient
    route_fn = next((r.endpoint for r in app.routes if getattr(r, "path", None) == "/api/status"), None)
    assert route_fn is not None
    data = route_fn()

    # Verify identical values between AccountService and Dashboard API
    assert data["account"]["starting_balance"] == metrics.starting_balance
    assert data["account"]["current_balance"] == metrics.current_balance
    assert data["account"]["equity"] == metrics.equity
    assert data["account"]["realized_pnl"] == metrics.realized_pnl
    assert data["account"]["win_rate_pct"] == metrics.win_rate_pct


# 9. Slash Command Registration Test
def test_telegram_slash_command_registration():
    from unittest.mock import patch, Mock
    config = TelegramConfig(bot_token="test_token", chat_id="123456", enabled=True)
    service = TelegramAlertService(config)

    mock_resp = Mock()
    mock_resp.status_code = 200

    with patch("requests.post", return_value=mock_resp) as mock_post:
        success = service.set_bot_commands()
        assert success is True
        mock_post.assert_called_once()
        url = mock_post.call_args[0][0]
        payload = mock_post.call_args[1]["json"]
        assert "setMyCommands" in url
        cmd_names = [c["command"] for c in payload["commands"]]
        assert "menu" in cmd_names
        assert "analyze" in cmd_names
        assert "dashboard" in cmd_names
        assert "positions" in cmd_names
        assert "history" in cmd_names


# 10. /analyze Opens Pair Selection Menu (Without Auto-scanning All Watchlist)
def test_analyze_opens_pair_selection_menu_without_scanning_all(bot_handler):
    with patch.object(bot_handler, "trigger_on_demand_analysis") as mock_scan:
        res = bot_handler._handle_command("/analyze")
        assert res["status"] == "analyze_menu_sent"
        mock_scan.assert_not_called()
        bot_handler.telegram.send_message.assert_called()
        call_markup = bot_handler.telegram.send_message.call_args[1]["reply_markup"]
        btn_texts = [btn["text"] for row in call_markup["inline_keyboard"] for btn in row]
        assert any("BTCUSDT" in t for t in btn_texts)
        assert any("ENTER CUSTOM PAIR" in t for t in btn_texts)


# 11. Selecting One Pair Analyses ONLY That Pair
def test_selecting_one_pair_analyses_only_that_pair(bot_handler):
    # Add another pair to watchlist
    bot_handler.storage.add_watchlist_symbol("ETHUSDT", asset_class="CRYPTO")
    with patch("src.strategy.mtf_orchestrator.MultiTimeframeOrchestrator.scan_market", return_value=[]) as mock_scan:
        # User clicks callback for BTCUSDT only
        cb_update = {
            "callback_query": {
                "id": "cb_btc",
                "message": {"chat": {"id": 123456}},
                "data": "analyze_pair_BTCUSDT",
            }
        }
        res = bot_handler.process_incoming_update(cb_update)
        assert res["status"] == "no_opportunities"
        assert res["symbol"] == "BTCUSDT"
        # Only BTCUSDT was scanned once
        assert mock_scan.call_count == 1


# 12. Maximum Open Position Enforcement & Reopening After Closure
def test_max_open_position_enforcement_and_reopen_after_close(bot_handler):
    bot_handler.storage.set_setting("max_open_positions", "1")

    # Signal 1
    sig1_id = "SIG_TEST_MAX_1"
    bot_handler.storage.save_signal(
        signal_id=sig1_id,
        symbol="BTCUSDT",
        direction="bullish",
        entry_price=80000.0,
        stop_loss=79000.0,
        take_profit=84000.0,
        audit_notes=["Test"],
        validity_minutes=30,
    )
    # Signal 2
    sig2_id = "SIG_TEST_MAX_2"
    bot_handler.storage.save_signal(
        signal_id=sig2_id,
        symbol="BTCUSDT",
        direction="bullish",
        entry_price=80500.0,
        stop_loss=79500.0,
        take_profit=84500.0,
        audit_notes=["Test"],
        validity_minutes=30,
    )

    # Approve Signal 1 -> Succeeds
    res1 = bot_handler._handle_callback("cb_1", f"approve_{sig1_id}")
    assert res1["status"] == "approved"
    pos1_id = res1["position_id"]

    # Try to approve Signal 2 -> Blocked due to max_open_positions limit of 1
    res2 = bot_handler._handle_callback("cb_2", f"approve_{sig2_id}")
    assert res2["status"] == "max_positions_reached"

    # Now close position 1 manually
    close_res = bot_handler.manual_close_position(pos1_id)
    assert close_res["status"] == "position_closed_manually"

    # Now approve Signal 2 -> Succeeds because closed positions do NOT count!
    res3 = bot_handler._handle_callback("cb_3", f"approve_{sig2_id}")
    assert res3["status"] == "approved"



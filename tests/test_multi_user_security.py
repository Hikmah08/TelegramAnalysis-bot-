import os
import sys
import tempfile
import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from src.storage.database import StorageManager
from src.paper.engine import PaperTradingEngine, PaperAccount, PaperPosition, PaperOrderStatus
from src.alerts.telegram import TelegramAlertService, TelegramConfig
from src.alerts.bot_handler import TelegramBotHandler
from src.alerts.outbox_dispatcher import OutboxNotificationDispatcher
from src.dashboard.app import create_dashboard_app
from src.strategy.models import Direction


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    storage = StorageManager(db_path=path)
    yield storage, path
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass


def test_user_registration_and_account_isolation(temp_db):
    storage, _ = temp_db

    # Register User A and User B
    u_a = storage.register_user("1001", username="alice", chat_id="1001", role="USER")
    u_b = storage.register_user("1002", username="bob", chat_id="1002", role="USER")

    assert u_a["user_id"] == "1001"
    assert u_b["user_id"] == "1002"

    # Verify duplicate registration does not reset balance or settings
    storage.set_setting("current_balance", "15000.0", user_id="1001")
    u_a_repeat = storage.register_user("1001", username="alice_updated", chat_id="1001")
    assert u_a_repeat["user_id"] == "1001"
    assert float(storage.get_setting("current_balance", user_id="1001")) == 15000.0
    # User B remains at default 10,000
    assert float(storage.get_setting("current_balance", user_id="1002")) == 10000.0


def test_isolated_watchlists(temp_db):
    storage, _ = temp_db
    storage.register_user("1001", username="alice", chat_id="1001")
    storage.register_user("1002", username="bob", chat_id="1002")

    # Add distinct custom symbols
    storage.add_watchlist_symbol("SOLUSDT", asset_class="crypto", user_id="1001")
    storage.add_watchlist_symbol("EURUSD", asset_class="forex", user_id="1002")

    wl_a = [x["symbol"] for x in storage.get_watchlist(user_id="1001")]
    wl_b = [x["symbol"] for x in storage.get_watchlist(user_id="1002")]

    assert "SOLUSDT" in wl_a
    assert "EURUSD" not in wl_a

    assert "EURUSD" in wl_b
    assert "SOLUSDT" not in wl_b


def test_independent_20_position_limits(temp_db):
    storage, _ = temp_db
    storage.register_user("1001", username="alice", chat_id="1001")
    storage.register_user("1002", username="bob", chat_id="1002")

    # User A opens 20 positions
    for i in range(20):
        storage.open_paper_trade(
            symbol=f"PAIR_{i}",
            direction="bullish",
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=120.0,
            position_size=1.0,
            user_id="1001",
        )

    assert storage.get_open_paper_positions_count(user_id="1001") == 20
    # User B has 0 positions and can freely open their own
    assert storage.get_open_paper_positions_count(user_id="1002") == 0

    p_b = storage.open_paper_trade(
        symbol="BTCUSDT",
        direction="bullish",
        entry_price=50000.0,
        stop_loss=49000.0,
        take_profit=54000.0,
        position_size=0.1,
        user_id="1002",
    )
    assert p_b is not None
    assert storage.get_open_paper_positions_count(user_id="1002") == 1


def test_signal_approval_hijacking_prevented(temp_db):
    storage, _ = temp_db
    storage.register_user("1001", username="alice", chat_id="1001")
    storage.register_user("1002", username="bob", chat_id="1002")

    mock_telegram = MagicMock(spec=TelegramAlertService)
    mock_telegram.config = TelegramConfig(enabled=True, bot_token="MOCK_TOKEN", chat_id="1001")
    handler = TelegramBotHandler(telegram_service=mock_telegram, storage=storage)

    # Save a signal belonging to Alice (1001)
    sig_id = "BTC_BULL_1001"
    storage.save_signal(
        signal_id=sig_id,
        symbol="BTCUSDT",
        direction="bullish",
        entry_price=50000.0,
        stop_loss=49000.0,
        take_profit=54000.0,
        audit_notes=["Confluence verified"],
        user_id="1001",
    )

    # Bob (1002) attempts to approve Alice's signal
    update = {
        "callback_query": {
            "id": "cb_hijack_1",
            "from": {"id": 1002, "username": "bob"},
            "message": {"chat": {"id": 1002}},
            "data": f"approve_{sig_id}",
        }
    }
    res = handler.process_incoming_update(update)
    assert res["status"] == "unauthorized"
    # Ensure signal was not approved and no position created for Bob
    assert storage.get_open_paper_positions_count(user_id="1002") == 0


def test_position_closure_tampering_prevented(temp_db):
    storage, _ = temp_db
    storage.register_user("1001", username="alice", chat_id="1001")
    storage.register_user("1002", username="bob", chat_id="1002")

    pos_id = storage.open_paper_trade(
        symbol="ETHUSDT",
        direction="bullish",
        entry_price=3000.0,
        stop_loss=2900.0,
        take_profit=3400.0,
        position_size=1.0,
        user_id="1001",
    )

    mock_telegram = MagicMock(spec=TelegramAlertService)
    mock_telegram.config = TelegramConfig(enabled=True, bot_token="MOCK_TOKEN", chat_id="1001")
    handler = TelegramBotHandler(telegram_service=mock_telegram, storage=storage)

    # Bob (1002) attempts to manually close Alice's position
    update = {
        "message": {
            "from": {"id": 1002, "username": "bob"},
            "chat": {"id": 1002},
            "text": f"/close {pos_id}",
        }
    }
    res = handler.process_incoming_update(update)
    assert res["status"] == "unauthorized"

    # Alice's position is still open
    pos = storage.get_paper_position(pos_id)
    assert pos["status"] == "OPEN"


def test_atomic_paper_trade_closure_and_outbox(temp_db):
    storage, _ = temp_db
    storage.register_user("1001", username="alice", chat_id="1001")

    pos_id = storage.open_paper_trade(
        symbol="BTCUSDT",
        direction="bullish",
        entry_price=50000.0,
        stop_loss=49000.0,
        take_profit=54000.0,
        position_size=1.0,
        user_id="1001",
    )

    # Atomically close position
    ok = storage.close_paper_trade_atomic(
        position_id=pos_id,
        user_id="1001",
        exit_price=54000.0,
        exit_reason="take_profit",
        pnl=3967.6,
        fee_paid=32.4,
        notification_event={
            "event_type": "trade_closed_take_profit",
            "symbol": "BTCUSDT",
            "pnl": 3967.6,
            "message": "Take profit reached! PnL: +3967.60 USD",
        },
    )
    assert ok is True

    # Check position status and user balance
    pos = storage.get_paper_position(pos_id)
    assert pos["status"] == "CLOSED"
    assert pos["pnl"] == 3967.6
    bal = float(storage.get_setting("current_balance", user_id="1001"))
    assert bal == 10000.0 + 3967.6

    # Verify pending outbox notification was created
    pending = storage.get_pending_outbox_notifications()
    assert len(pending) == 1
    assert pending[0]["user_id"] == "1001"
    assert pending[0]["chat_id"] == "1001"

    # Re-closing the same position should fail (status is already CLOSED)
    second_close = storage.close_paper_trade_atomic(
        position_id=pos_id,
        user_id="1001",
        exit_price=54000.0,
        exit_reason="take_profit",
        pnl=3967.6,
        fee_paid=32.4,
    )
    assert second_close is False


def test_outbox_dispatcher_delivery_and_retries(temp_db):
    storage, _ = temp_db
    storage.register_user("1001", username="alice", chat_id="1001")

    # Add notification to outbox
    storage.create_outbox_notification("1001", "1001", "test_alert", "Hello Alice")

    mock_telegram = MagicMock(spec=TelegramAlertService)
    mock_telegram.send_message.return_value = True

    dispatcher = OutboxNotificationDispatcher(storage=storage, telegram_service=mock_telegram)
    delivered = dispatcher.dispatch_pending()
    assert delivered == 1

    mock_telegram.send_message.assert_called_once_with(text="Hello Alice", chat_id="1001")
    # Verify no more pending
    assert len(storage.get_pending_outbox_notifications()) == 0


def test_admin_allowlist_enforcement(temp_db, monkeypatch):
    storage, _ = temp_db
    # Set explicit admin allowlist
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "99999,88888")

    mock_telegram = MagicMock(spec=TelegramAlertService)
    mock_telegram.config = TelegramConfig(enabled=True, bot_token="MOCK_TOKEN", chat_id="1001")
    handler = TelegramBotHandler(telegram_service=mock_telegram, storage=storage)

    # Ordinary User (1001) tries to access /admin
    res = handler.process_incoming_update({
        "message": {
            "from": {"id": 1001, "username": "alice"},
            "chat": {"id": 1001},
            "text": "/admin",
        }
    })
    assert res["status"] == "unauthorized"

    # Ordinary User tries /users
    res2 = handler.process_incoming_update({
        "message": {
            "from": {"id": 1001, "username": "alice"},
            "chat": {"id": 1001},
            "text": "/users",
        }
    })
    assert res2["status"] == "unauthorized"

    # Admin User (99999) accesses /admin
    res_adm = handler.process_incoming_update({
        "message": {
            "from": {"id": 99999, "username": "superadmin"},
            "chat": {"id": 99999},
            "text": "/admin",
        }
    })
    assert res_adm["status"] == "admin_dashboard_sent"


def test_dashboard_token_and_session_authentication(temp_db):
    storage, _ = temp_db
    storage.register_user("1001", username="alice", chat_id="1001")
    storage.register_user("1002", username="bob", chat_id="1002")

    # Set different balances
    storage.set_setting("current_balance", "12500.0", user_id="1001")
    storage.set_setting("current_balance", "8500.0", user_id="1002")

    app = create_dashboard_app(storage=storage)
    client = TestClient(app)

    # 1. Unauthenticated request to /api/status -> 401
    resp_unauth = client.get("/api/status")
    assert resp_unauth.status_code == 401

    # 2. Tampering attempt with query param: /api/status?user_id=1001 -> still 401
    resp_tamper = client.get("/api/status?user_id=1001")
    assert resp_tamper.status_code == 401

    # 3. Generate token for User A
    raw_token_a = storage.create_dashboard_token(user_id="1001", validity_minutes=15)

    # Token can only be consumed once
    login_resp = client.get(f"/auth/login?token={raw_token_a}", follow_redirects=False)
    assert login_resp.status_code == 302
    assert "session_id" in login_resp.cookies

    # Replaying token fails
    replay_resp = client.get(f"/auth/login?token={raw_token_a}", follow_redirects=False)
    assert replay_resp.status_code == 400

    # Request /api/status with valid session
    status_resp = client.get("/api/status", cookies=login_resp.cookies)
    assert status_resp.status_code == 200
    data = status_resp.json()
    assert data["user_id"] == "1001"
    assert data["account"]["current_balance"] == 12500.0

    # 4. Test Watchlist API
    wl_add = client.post("/api/watchlist/add", json={"symbol": "EURUSD"}, cookies=login_resp.cookies)
    assert wl_add.status_code == 200
    wl_get = client.get("/api/watchlist", cookies=login_resp.cookies)
    assert wl_get.status_code == 200
    assert any(x["symbol"] == "EURUSD" for x in wl_get.json()["watchlist"])

    # 5. Test Settings Update API
    set_resp = client.post("/api/settings/update", json={"key": "risk_per_trade_pct", "value": "2.0"}, cookies=login_resp.cookies)
    assert set_resp.status_code == 200
    get_sets = client.get("/api/settings", cookies=login_resp.cookies)
    assert get_sets.json()["settings"]["risk_per_trade_pct"] == "2.0"

    # 6. Test Position manual close via Dashboard
    pos_id = storage.open_paper_trade("BTCUSDT", "bullish", 80000.0, 79000.0, 84000.0, 0.1, user_id="1001")
    close_resp = client.post(f"/api/positions/close/{pos_id}", cookies=login_resp.cookies)
    assert close_resp.status_code == 200
    hist_resp = client.get("/api/history", cookies=login_resp.cookies)
    assert hist_resp.status_code == 200
    assert any(h["position_id"] == pos_id for h in hist_resp.json()["history"])



def test_telegram_alert_opportunity_chat_routing(caplog):
    """
    Regression test verifying:
    1. Analysis results are delivered to the requesting user's chat.
    2. Two different users receive messages in their own chats.
    3. Missing chat IDs log a clear error and return False without falling back to admin.
    4. Existing alert methods still work.
    """
    import logging
    import pandas as pd
    from src.strategy.mtf_models import MultiTimeframeSetup
    from src.strategy.models import Direction
    from src.paper.engine import PaperPosition, PaperExitReason

    config = TelegramConfig(enabled=True, bot_token="MOCK_TOKEN", chat_id="ADMIN_9999")
    service = TelegramAlertService(config)

    # Mock send_message to track recipients
    sent_messages = []

    def mock_send_message(text, reply_markup=None, chat_id=None):
        sent_messages.append({"text": text, "reply_markup": reply_markup, "chat_id": chat_id})
        return True

    service.send_message = mock_send_message

    from src.strategy.mtf_models import MultiTimeframeSetup, HigherTimeframeBias, BiasState, AreaOfInterest
    from src.strategy.models import SetupCandidate, SetupStage

    setup = MultiTimeframeSetup(
        direction=Direction.BULLISH,
        bias_4h=HigherTimeframeBias(direction=BiasState.BULLISH, timestamp=pd.Timestamp.now(), reason="4H MSS"),
        aoi_1h=AreaOfInterest(direction=Direction.BULLISH, aoi_type="1H_FVG", top_price=85200.0, bottom_price=84800.0, timestamp=pd.Timestamp.now()),
        setup_15m=SetupCandidate(direction=Direction.BULLISH, timeframe="15m", stage=SetupStage.MSS_CONFIRMED),
        entry_price=85000.0,
        stop_loss=84500.0,
        audit_trail=["4H bullish bias", "1H AOI", "15M structure confirmation"],
    )

    # 1. Delivery to requesting user User A
    res_a = service.alert_opportunity_with_approval(
        signal_id="SIG_USER_A",
        setup=setup,
        symbol="BTCUSDT",
        chat_id="CHAT_USER_A",
    )
    assert res_a is True
    assert len(sent_messages) == 1
    assert sent_messages[-1]["chat_id"] == "CHAT_USER_A"
    assert "approve_SIG_USER_A" in str(sent_messages[-1]["reply_markup"])

    # 2. Delivery to requesting user User B (distinct chat)
    res_b = service.alert_opportunity_with_approval(
        signal_id="SIG_USER_B",
        setup=setup,
        symbol="BTCUSDT",
        chat_id="CHAT_USER_B",
    )
    assert res_b is True
    assert len(sent_messages) == 2
    assert sent_messages[-1]["chat_id"] == "CHAT_USER_B"
    assert "approve_SIG_USER_B" in str(sent_messages[-1]["reply_markup"])

    # 3. Missing / empty chat_id must produce a clear error and return False (NO fallback to ADMIN_9999)
    with caplog.at_level(logging.ERROR):
        res_missing = service.alert_opportunity_with_approval(
            signal_id="SIG_NO_CHAT",
            setup=setup,
            symbol="BTCUSDT",
            chat_id=None,
        )
    assert res_missing is False
    assert len(sent_messages) == 2  # No message dispatched
    assert any("missing chat_id" in record.message for record in caplog.records)

    with caplog.at_level(logging.ERROR):
        res_empty = service.alert_opportunity_with_approval(
            signal_id="SIG_EMPTY_CHAT",
            setup=setup,
            symbol="BTCUSDT",
            chat_id="   ",
        )
    assert res_empty is False
    assert len(sent_messages) == 2  # No message dispatched

    # 4. alert_mtf_setup with explicit chat_id and fallback test
    res_mtf = service.alert_mtf_setup(setup, symbol="ETHUSDT", chat_id="CHAT_USER_C")
    assert res_mtf is True
    assert sent_messages[-1]["chat_id"] == "CHAT_USER_C"

    # Default fallback for scanner broadcast uses config.chat_id
    res_mtf_default = service.alert_mtf_setup(setup, symbol="ETHUSDT")
    assert res_mtf_default is True
    assert sent_messages[-1]["chat_id"] == "ADMIN_9999"

    # 5. alert_paper_trade_exit with chat_id
    pos = PaperPosition(
        position_id=42,
        symbol="BTCUSDT",
        direction=Direction.BULLISH,
        entry_time=pd.Timestamp.now(),
        entry_price=85000.0,
        stop_loss=84500.0,
        take_profit=87000.0,
        position_size=0.1,
        allocated_margin=8500.0,
        exit_time=pd.Timestamp.now(),
        exit_price=87000.0,
        exit_reason=PaperExitReason.TAKE_PROFIT,
        pnl=200.0,
        pnl_pct=2.35,
        fee_paid=5.22,
    )
    res_exit = service.alert_paper_trade_exit(pos, chat_id="CHAT_USER_A")
    assert res_exit is True
    assert sent_messages[-1]["chat_id"] == "CHAT_USER_A"


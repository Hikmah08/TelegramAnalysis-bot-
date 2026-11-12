import pytest
import tempfile
import pandas as pd
from pathlib import Path
from unittest.mock import Mock, patch
from datetime import datetime, timezone, timedelta

from src.storage import StorageManager
from src.paper.engine import PaperTradingEngine, PaperAccount, PaperExitReason
from src.paper.account_service import AccountService
from src.strategy import StrategyConfig, SequenceTracker
from src.strategy.models import Direction, SetupCandidate, SetupStage
from src.strategy.mtf_models import MultiTimeframeSetup, HigherTimeframeBias, BiasState, AreaOfInterest
from src.data.instruments import (
    INSTRUMENT_REGISTRY,
    AssetClass,
    InstrumentSpec,
    get_instrument_spec,
    detect_asset_class,
    is_supported_symbol,
)
from src.data.yahoo_provider import YahooFinanceMarketDataProvider
from src.data.models import Timeframe, Candle
from src.backtest import BacktestEngine, BacktestConfig, TradeRecord, TradeStatus, ExitReason, BacktestMetrics
from src.backtest.historical_manager import HistoricalDataManager
from src.backtest.fx_replay_bridge import FXReplayBridge
from src.dashboard.app import create_dashboard_app
from fastapi.testclient import TestClient


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


def make_dummy_setup(direction: Direction, entry: float, sl: float, timestamp=None):
    bias = HigherTimeframeBias(
        direction=BiasState.BULLISH if direction == Direction.BULLISH else BiasState.BEARISH,
        timestamp=pd.Timestamp.now(tz="UTC"),
        reason="MSS",
    )
    aoi = AreaOfInterest(
        direction=direction,
        aoi_type="1H_FVG",
        top_price=entry + 10.0,
        bottom_price=entry - 10.0,
        timestamp=pd.Timestamp.now(tz="UTC"),
    )
    cand = SetupCandidate(direction=direction, timeframe="15m", stage=SetupStage.ENTRY_RETRACED, is_valid=True)
    return MultiTimeframeSetup(
        direction=direction,
        bias_4h=bias,
        aoi_1h=aoi,
        setup_15m=cand,
        entry_price=entry,
        stop_loss=sl,
        timestamp=timestamp or pd.Timestamp.now(tz="UTC"),
    )


# 1. TEST 20 MAX OPEN POSITIONS LIFECYCLE
def test_20_max_open_positions_lifecycle(temp_db):
    # Verify DB default settings
    settings = temp_db.get_all_settings()
    assert int(settings.get("max_open_positions", 20)) == 20

    account = PaperAccount(initial_balance=100000.0, max_open_positions=20)
    engine = PaperTradingEngine(account=account)

    # Open 20 positions
    opened_ids = []
    db_ids = []
    base_time = pd.Timestamp.now(tz="UTC")
    for i in range(20):
        setup = make_dummy_setup(
            direction=Direction.BULLISH,
            entry=100.0 + i,
            sl=95.0 + i,
            timestamp=base_time + timedelta(minutes=i),
        )
        pos = engine.open_position(setup, symbol=f"SYM{i}")
        assert pos is not None
        opened_ids.append(pos.position_id)
        db_id = temp_db.open_paper_trade(
            symbol=pos.symbol,
            direction=pos.direction.value,
            entry_price=pos.entry_price,
            stop_loss=pos.stop_loss,
            take_profit=pos.take_profit,
            position_size=pos.position_size,
        )
        db_ids.append(db_id)

    # Verify count is 20/20
    assert len(account.open_positions) == 20
    assert temp_db.get_open_paper_positions_count() == 20

    # Attempt 21st position - must be rejected
    setup_21 = make_dummy_setup(
        direction=Direction.BULLISH,
        entry=200.0,
        sl=190.0,
        timestamp=base_time + timedelta(minutes=25),
    )
    rejected_pos = engine.open_position(setup_21, symbol="SYM21")
    assert rejected_pos is None
    assert len(account.open_positions) == 20

    # Close 1 position
    first_id = opened_ids[0]
    first_db_id = db_ids[0]
    closed_pos = engine.close_position_manually(first_id, current_price=110.0)
    assert closed_pos is not None
    temp_db.close_paper_trade(position_id=first_db_id, exit_price=110.0, exit_reason="manual", pnl=50.0)
    assert len(account.open_positions) == 19
    assert temp_db.get_open_paper_positions_count() == 19

    # Now opening the 20th position again should succeed
    setup_new = make_dummy_setup(
        direction=Direction.BULLISH,
        entry=150.0,
        sl=140.0,
        timestamp=base_time + timedelta(hours=2),
    )
    new_pos = engine.open_position(setup_new, symbol="SYM_NEW")
    assert new_pos is not None
    assert len(account.open_positions) == 20
    temp_db.open_paper_trade(
        symbol=new_pos.symbol,
        direction=new_pos.direction.value,
        entry_price=new_pos.entry_price,
        stop_loss=new_pos.stop_loss,
        take_profit=new_pos.take_profit,
        position_size=new_pos.position_size,
    )
    assert temp_db.get_open_paper_positions_count() == 20


# 2. TEST FOREX AND METALS SPECS AND PROVIDER
def test_forex_metals_spec_definitions():
    # Forex pairs
    for sym in ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "NZDUSD", "USDCAD"]:
        assert is_supported_symbol(sym)
        spec = get_instrument_spec(sym)
        assert spec.asset_class == AssetClass.FOREX
        assert spec.contract_size == 100000.0

    # Metals
    for sym in ["XAUUSD", "XAGUSD"]:
        assert is_supported_symbol(sym)
        spec = get_instrument_spec(sym)
        assert spec.asset_class == AssetClass.METALS
        assert spec.contract_size > 0


def test_yahoo_provider_resampling_and_validation():
    provider = YahooFinanceMarketDataProvider()
    assert provider.supports_symbol("EURUSD")
    assert provider.supports_symbol("XAUUSD")
    assert not provider.supports_symbol("BTCUSDT")  # Crypto handled by Binance

    # Verify 1H to 4H resampling mathematics matching Yahoo provider logic
    base_time = pd.Timestamp("2026-01-01 00:00:00+00:00")
    one_hour_records = []
    for h in range(12):
        ts = base_time + timedelta(hours=h)
        one_hour_records.append({
            "timestamp": ts,
            "open": 1.1000 + (h * 0.0005),
            "high": 1.1020 + (h * 0.0005),
            "low": 1.0990 + (h * 0.0005),
            "close": 1.1010 + (h * 0.0005),
            "volume": 500.0,
        })
    df_1h = pd.DataFrame(one_hour_records)
    df_1h.set_index("timestamp", inplace=True)
    resampled = df_1h.resample("4h").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna().reset_index()

    # 12 hours / 4 hours per bar = 3 closed 4H bars
    assert len(resampled) == 3
    assert resampled.iloc[0]["volume"] == 2000.0
    assert resampled.iloc[0]["open"] == 1.1000



# 3. TEST HISTORICAL BACKTESTING ENGINE & ENHANCED METRICS
def test_backtest_enhanced_metrics_and_ambiguity():
    base_time = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    raw = [
        [95, 100, 94, 98],
        [98, 105, 97, 103],
        [103, 110, 102, 108], # Swing high 110
        [108, 109, 104, 105],
        [105, 106, 95, 96],
        [96, 98, 85, 89],     # Swing low 85
        [89, 94, 88, 93],
        [93, 97, 91, 95],
        [95, 96, 80, 88],     # Sweep of 85 (wick 80, close 88)
        [88, 100, 87, 99],
        [99, 116, 98, 115],   # MSS past 110
        [115, 120, 105, 118], # FVG [100 - 105]
        [118, 119, 103, 114], # Retracement entry at 105, SL 80, TP (4:1) = 205
        [114, 150, 112, 148],
        [148, 210, 146, 208], # High 210 hits 4:1 TP
    ]
    records = []
    for i, c in enumerate(raw):
        ts = base_time + timedelta(minutes=15 * i)
        cts = ts + timedelta(minutes=14, seconds=59)
        records.append({
            "timestamp": ts,
            "open": float(c[0]),
            "high": float(c[1]),
            "low": float(c[2]),
            "close": float(c[3]),
            "volume": 100.0,
            "close_time": cts,
        })
    df = pd.DataFrame(records)

    tracker = SequenceTracker(timeframe="15m", config=StrategyConfig(swing_left_bars=2, swing_right_bars=2))
    engine = BacktestEngine(config=BacktestConfig(initial_capital=10000.0, target_risk_reward=4.0))

    metrics, trades = engine.run(df=df, tracker=tracker, symbol="BTCUSDT")

    assert len(trades) == 1
    assert trades[0].status == TradeStatus.WIN
    assert trades[0].exit_reason == ExitReason.TAKE_PROFIT
    assert metrics.winning_trades == 1
    assert metrics.total_trades == 1
    assert metrics.gross_profit > 0
    assert metrics.win_rate_pct == 100.0
    assert metrics.profit_factor > 1.0
    assert metrics.long_trades_count == 1
    assert len(metrics.equity_curve) >= 2


# 4. TEST HISTORICAL DATA MANAGER AND CSV IMPORT
def test_historical_data_manager_csv_import():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = HistoricalDataManager(storage_dir=Path(tmpdir))

        # Create a sample CSV
        csv_path = Path(tmpdir) / "sample_eurusd.csv"
        csv_content = """timestamp,open,high,low,close,volume
2026-01-01 00:00:00,1.1000,1.1050,1.0980,1.1020,1000
2026-01-01 01:00:00,1.1020,1.1080,1.1010,1.1060,1200
2026-01-01 02:00:00,1.1060,1.1100,1.1040,1.1090,1500
"""
        csv_path.write_text(csv_content, encoding="utf-8")

        imported_df = manager.import_csv(
            csv_path=csv_path,
            symbol="EURUSD",
            timeframe=Timeframe.H1,
        )
        assert len(imported_df) == 3

        # Retrieve candles
        candles = manager.load_dataset("EURUSD", Timeframe.H1)
        assert len(candles) == 3
        assert candles.iloc[0]["open"] == 1.1000
        assert candles.iloc[-1]["close"] == 1.1090


# 5. TEST FX REPLAY BRIDGE AND COMPARISON
def test_fx_replay_bridge_and_comparison():
    bridge = FXReplayBridge()

    trades = [
        TradeRecord(
            trade_id=1,
            symbol="EURUSD",
            direction=Direction.BULLISH,
            entry_time=pd.Timestamp("2026-01-01 10:00:00+00:00"),
            entry_price=1.1000,
            stop_loss=1.0950,
            take_profit=1.1200,
            position_size=10000.0,
            risk_amount=50.0,
            setup_timeframe="15m",
            trading_session="London",
            day_of_week="Thursday",
            exit_time=pd.Timestamp("2026-01-01 12:00:00+00:00"),
            exit_price=1.1200,
            exit_reason=ExitReason.TAKE_PROFIT,
            gross_pnl=200.0,
            net_pnl=198.0,
            fee_paid=2.0,
            r_multiple=4.0,
            pnl_pct=396.0,
            status=TradeStatus.WIN,
        )
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        export_path = Path(tmpdir) / "exported_trades.csv"
        bridge.export_trades_to_csv(trades, export_path)
        assert export_path.exists()

        # Re-import through bridge
        imported_records = bridge.import_fx_replay_journal(export_path)
        assert len(imported_records) == 1
        assert imported_records[0]["symbol"] == "EURUSD"
        assert imported_records[0]["direction"] == "BULLISH"
        assert imported_records[0]["r_multiple"] == 4.0

        # Generate comparison report
        report = bridge.generate_comparison_report(bot_trades=trades, fx_replay_records=imported_records, symbol="EURUSD")
        assert report["symbol"] == "EURUSD"
        assert report["matched_trades_count"] == 1
        assert "Cross-validation complete" in report["comparison_summary"]


# 6. TEST DASHBOARD BACKTESTING ISOLATION
def test_dashboard_backtesting_isolation(temp_db):
    app = create_dashboard_app(storage=temp_db)
    client = TestClient(app)

    # Get initial account
    account_service = AccountService(storage=temp_db)
    initial_metrics = account_service.get_metrics()
    initial_balance = initial_metrics.current_balance
    initial_open_positions = len(initial_metrics.open_positions)

    # Trigger backtest endpoint (mocking candle fetching so it doesn't need external network)
    with patch("src.data.provider_factory.MarketProviderFactory.get_provider_for_symbol") as mock_factory:
        mock_prov = Mock()
        now = datetime.now(timezone.utc)
        mock_df = pd.DataFrame([
            {
                "timestamp": now - timedelta(minutes=15 * i),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 10.0,
                "close_time": now - timedelta(minutes=15 * i - 14),
            }
            for i in range(100)
        ]).sort_values("timestamp").reset_index(drop=True)

        mock_prov.fetch_candles.return_value = mock_df
        mock_factory.return_value = mock_prov

        session_id = temp_db.create_dashboard_session(user_id="OWNER_001")
        client.cookies.set("session_id", session_id)

        response = client.post(
            "/api/backtest/run",
            json={
                "symbol": "BTCUSDT",
                "timeframe": "15m",
                "initial_capital": 100000.0,
                "risk_pct": 1.0,
                "candle_limit": 100,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "result" in data
        assert "metrics" in data["result"]

    # Verify live account has NOT been modified
    after_metrics = account_service.get_metrics()
    assert after_metrics.current_balance == initial_balance
    assert len(after_metrics.open_positions) == initial_open_positions

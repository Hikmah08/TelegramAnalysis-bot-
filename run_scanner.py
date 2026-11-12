import sys
from pathlib import Path
from datetime import datetime, timezone

# Add project root to sys.path
root_dir = Path(__file__).resolve().parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from src.data import BinancePublicDataProvider, Timeframe


def main():
    symbol = "BTCUSDT"
    timeframes = [
        Timeframe.H4,
        Timeframe.H1,
        Timeframe.M15,
        Timeframe.M5,
        Timeframe.M1,
    ]

    print("=" * 65)
    print(f" ICT/SMC Trading Analysis Bot - Data Ingestion Test ")
    print(f" Target Symbol: {symbol} | Mode: Analysis Only")
    print("=" * 65)

    provider = BinancePublicDataProvider()

    try:
        server_time = provider.get_server_time()
        print(f"Exchange Time (UTC): {server_time.strftime('%Y-%m-%d %H:%M:%S')}")
    except Exception as e:
        print(f"Warning: Could not fetch server time ({e}), using local UTC.")
        server_time = datetime.now(timezone.utc)

    print("-" * 65)

    from src.strategy import MultiTimeframeOrchestrator, StrategyConfig

    config = StrategyConfig(swing_left_bars=2, swing_right_bars=2)
    orchestrator = MultiTimeframeOrchestrator(config=config)

    timeframe_dfs = {}
    for tf in timeframes:
        try:
            df = provider.fetch_candles(symbol=symbol, timeframe=tf, limit=120)
            if not df.empty:
                timeframe_dfs[tf.value] = df
                last_c = df.iloc[-1]
                print(f"[{tf.value.upper():>4}] Ingested {len(df)} candles | Last Close: {last_c['close']:.2f} at {last_c['close_time'].strftime('%m-%d %H:%M')} UTC")
        except Exception as e:
            print(f"[{tf.value.upper():>4}] Ingestion error: {e}")

    print("-" * 65)
    print(" Running Top-Down Multi-Timeframe Confluence Hierarchy...")
    print("-" * 65)

    mtf_setups = orchestrator.scan_market(timeframe_dfs)

    if not mtf_setups:
        print("No setups passed the full 5-timeframe filter (4H Bias -> 1H AOI -> 15M Setup).")
        print("Note: Lower timeframes did not align with 4H directional bias or 1H AOI.")
    else:
        from src.alerts import TelegramConfig, TelegramAlertService
        telegram_service = TelegramAlertService(TelegramConfig.from_env())

        print(f"Found {len(mtf_setups)} fully qualified Multi-Timeframe Setup(s)!")
        for idx, setup in enumerate(mtf_setups, 1):
            print(f"\n[SETUP #{idx}] {setup.direction.value.upper()} on {symbol}")
            print(f"   Entry: {setup.entry_price:.2f} | Stop Loss: {setup.stop_loss:.2f}")
            print("   Confluence Audit Trail:")
            for note in setup.audit_trail:
                print(f"     * {note}")

            # Send Telegram alert if enabled
            if telegram_service.config.enabled:
                sent = telegram_service.alert_mtf_setup(setup, symbol=symbol)
                print(f"   Telegram Alert: {'SENT' if sent else 'FAILED'}")

    print("=" * 65)
    print("Multi-timeframe analysis scan finished.")

    # Execute paper position monitoring cycle
    print("\n" + "=" * 65)
    print(" Running Active Paper Position Monitor Cycle...")
    print("=" * 65)
    from src.storage.database import StorageManager
    from src.paper.position_monitor import PositionMonitorService
    storage = StorageManager()
    monitor = PositionMonitorService(storage=storage)
    mon_res = monitor.monitor_all_open_positions()
    print(f"Monitoring result: {mon_res}")
    print("=" * 65)


if __name__ == "__main__":
    main()

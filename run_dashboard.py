import sys
import uvicorn
from pathlib import Path
from datetime import datetime, timezone

# Add project root to sys.path
root_dir = Path(__file__).resolve().parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from src.data import BinancePublicDataProvider, Timeframe
from src.strategy import MultiTimeframeOrchestrator, StrategyConfig
from src.paper import PaperTradingEngine, PaperAccount
from src.dashboard import create_dashboard_app


def main():
    symbol = "BTCUSDT"
    print("=" * 65)
    print(" ICT/SMC Trading Analysis Dashboard Server ")
    print(f" Target: {symbol} | Mode: Analysis & Virtual Paper Trading Only")
    print("=" * 65)

    provider = BinancePublicDataProvider()
    timeframes = [Timeframe.H4, Timeframe.H1, Timeframe.M15, Timeframe.M5, Timeframe.M1]

    timeframe_dfs = {}
    for tf in timeframes:
        try:
            df = provider.fetch_candles(symbol=symbol, timeframe=tf, limit=120)
            if not df.empty:
                timeframe_dfs[tf.value] = df
        except Exception as e:
            print(f"[{tf.value.upper()}] Ingestion error: {e}")

    # Run MTF scan with strict 5M confirmation enabled
    config = StrategyConfig(swing_left_bars=2, swing_right_bars=2, require_ltf_confirmation=True)
    orchestrator = MultiTimeframeOrchestrator(config=config)
    setups = orchestrator.scan_market(timeframe_dfs)

    # Initialize Paper Engine and Trade Approval Queue
    paper_account = PaperAccount(initial_balance=10000.0, target_risk_reward=4.0)
    pending_approvals = {idx: s for idx, s in enumerate(setups, 1)}

    last_scan_data = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "symbol": symbol,
        "bias_4h": setups[0].bias_4h.direction.value if setups else "NEUTRAL",
        "setups": [
            {
                "direction": s.direction.value,
                "entry_price": s.entry_price,
                "stop_loss": s.stop_loss,
                "audit_trail": s.audit_trail,
            }
            for s in setups
        ],
    }

    app = create_dashboard_app(account=paper_account, last_scan_data=last_scan_data, pending_approvals=pending_approvals)
    print("\nStarting local dashboard at: http://127.0.0.1:8000")
    print("Press Ctrl+C to terminate.")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()

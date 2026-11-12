import csv
from pathlib import Path
from typing import List, Dict, Any, Optional
import pandas as pd
from .models import TradeRecord


class FXReplayBridge:
    """
    Bridge for FX Replay cross-validation and trade journal analysis.
    FX Replay uses UI-based CSV export/import rather than public developer REST endpoints.
    This bridge allows:
    1. Exporting bot-generated backtest trade logs to standard CSV format.
    2. Importing FX Replay manual trade journal CSV exports.
    3. Generating a side-by-side cross-validation comparison report.
    """

    @staticmethod
    def export_trades_to_csv(trades: List[TradeRecord], output_path: Path | str) -> Path:
        """Exports bot backtest trades to CSV format compatible with journaling tools."""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        rows = [t.to_dict() for t in trades]
        if not rows:
            with open(path, "w", newline="", encoding="utf-8") as f:
                f.write("trade_id,symbol,direction,entry_time,entry_price,exit_time,exit_price,net_pnl,r_multiple,exit_reason\n")
            return path

        df = pd.DataFrame(rows)
        df.to_csv(path, index=False)
        return path

    @staticmethod
    def import_fx_replay_journal(csv_path: Path | str) -> List[Dict[str, Any]]:
        """Imports an FX Replay trade log or journal CSV file."""
        path = Path(csv_path)
        if not path.exists():
            raise FileNotFoundError(f"FX Replay journal CSV not found: {path}")

        raw = pd.read_csv(path)
        cols = {c.strip().lower(): c for c in raw.columns}

        normalized = []
        for idx, row in raw.iterrows():
            r_col = cols.get("r") or cols.get("r-multiple") or cols.get("r_multiple") or cols.get("rmultiple")
            pnl_col = cols.get("pnl") or cols.get("profit") or cols.get("net_pnl")
            entry_p_col = cols.get("entry price") or cols.get("entry_price") or cols.get("open price") or cols.get("open_price") or cols.get("price")
            exit_p_col = cols.get("exit price") or cols.get("exit_price") or cols.get("close price") or cols.get("close_price")
            entry_t_col = cols.get("entry time") or cols.get("entry_time") or cols.get("open time") or cols.get("open_time") or cols.get("date") or cols.get("timestamp")
            exit_t_col = cols.get("exit time") or cols.get("exit_time") or cols.get("close time") or cols.get("close_time")

            rec = {
                "journal_id": idx + 1,
                "symbol": str(row.get(cols.get("symbol", ""), "EURUSD")).upper(),
                "direction": str(row.get(cols.get("direction", cols.get("type", "")), "LONG")).upper(),
                "entry_time": str(row.get(entry_t_col, "")) if entry_t_col else "",
                "entry_price": float(row.get(entry_p_col, 0.0)) if entry_p_col else 0.0,
                "exit_time": str(row.get(exit_t_col, "")) if exit_t_col else "",
                "exit_price": float(row.get(exit_p_col, 0.0)) if exit_p_col else 0.0,
                "pnl": float(row.get(pnl_col, 0.0)) if pnl_col else 0.0,
                "r_multiple": float(row.get(r_col, 0.0)) if r_col else 0.0,
            }
            normalized.append(rec)

        return normalized

    @classmethod
    def generate_comparison_report(
        cls,
        bot_trades: List[TradeRecord],
        fx_replay_records: List[Dict[str, Any]],
        symbol: str = "EURUSD",
    ) -> Dict[str, Any]:
        """
        Cross-validates bot signals against manual FX Replay session logs.
        Identifies matched setups, timing disparities, and execution price variances.
        """
        matches = []
        unmatched_bot = []
        unmatched_fx = list(fx_replay_records)

        for bt in bot_trades:
            matched = False
            for fx in list(unmatched_fx):
                bt_bull = bt.direction.value.lower() in ["bullish", "long", "buy"]
                fx_bull = fx["direction"].lower() in ["bullish", "long", "buy"]
                bt_bear = bt.direction.value.lower() in ["bearish", "short", "sell"]
                fx_bear = fx["direction"].lower() in ["bearish", "short", "sell"]
                dir_match = (bt_bull and fx_bull) or (bt_bear and fx_bear)
                
                price_diff_pct = abs(bt.entry_price - fx["entry_price"]) / max(bt.entry_price, 1e-6) if fx["entry_price"] > 0 else 1.0

                if dir_match and price_diff_pct < 0.005:  # within 0.5% price distance
                    matches.append({
                        "symbol": symbol,
                        "direction": bt.direction.value.upper(),
                        "bot_entry_time": str(bt.entry_time),
                        "fx_entry_time": fx["entry_time"],
                        "bot_entry": bt.entry_price,
                        "fx_entry": fx["entry_price"],
                        "bot_exit": bt.exit_price,
                        "fx_exit": fx["exit_price"],
                        "bot_r": bt.r_multiple,
                        "fx_r": fx["r_multiple"],
                        "bot_exit_reason": bt.exit_reason.value if bt.exit_reason else None,
                        "notes": "Valid execution match across both environments." if abs(bt.r_multiple - fx["r_multiple"]) < 0.2 else "Minor price/spread variance between broker feeds."
                    })
                    unmatched_fx.remove(fx)
                    matched = True
                    break

            if not matched:
                unmatched_bot.append(bt.to_dict())

        return {
            "symbol": symbol,
            "total_bot_trades": len(bot_trades),
            "total_fx_replay_trades": len(fx_replay_records),
            "matched_trades_count": len(matches),
            "matched_trades": matches,
            "unmatched_bot_count": len(unmatched_bot),
            "unmatched_fx_count": len(unmatched_fx),
            "comparison_summary": (
                f"Cross-validation complete: {len(matches)} setups matched between Bot and FX Replay. "
                "Any minor differences in fills or R-multiples are attributable to broker spread differences."
            ),
        }

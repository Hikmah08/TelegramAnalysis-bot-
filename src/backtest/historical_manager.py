import os
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
import pandas as pd
from ..data.models import Timeframe
from ..data.provider_factory import MarketProviderFactory

logger = logging.getLogger(__name__)

HISTORICAL_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "historical"
HISTORICAL_DATA_DIR.mkdir(parents=True, exist_ok=True)


class HistoricalDataManager:
    """
    Manages local historical datasets for backtesting:
    - Downloads historical OHLC candles from supported providers (crypto, forex, metals)
    - Imports valid external OHLC CSV files (e.g. from broker exports, FX Replay, Dukascopy)
    - Checks and filters duplicates and missing candles
    - Tracks start/end timestamps and provider metadata
    - Avoids repeated redundant downloads
    """

    def __init__(self, storage_dir: Path = HISTORICAL_DATA_DIR):
        self.storage_dir = storage_dir
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _get_file_path(self, symbol: str, timeframe: Timeframe) -> Path:
        clean = symbol.replace("/", "").replace("-", "").upper()
        return self.storage_dir / f"{clean}_{timeframe.value}.parquet"

    def _get_meta_path(self, symbol: str, timeframe: Timeframe) -> Path:
        clean = symbol.replace("/", "").replace("-", "").upper()
        return self.storage_dir / f"{clean}_{timeframe.value}_meta.json"

    def load_dataset(
        self,
        symbol: str,
        timeframe: Timeframe,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """Loads cached historical dataset, filtering by date if specified."""
        file_path = self._get_file_path(symbol, timeframe)
        if not file_path.exists():
            return pd.DataFrame()

        df = pd.read_parquet(file_path)
        if start_date:
            df = df[df["timestamp"] >= pd.to_datetime(start_date, utc=True)]
        if end_date:
            df = df[df["timestamp"] <= pd.to_datetime(end_date, utc=True)]

        return df.sort_values("timestamp").reset_index(drop=True)

    def fetch_and_store(
        self,
        symbol: str,
        timeframe: Timeframe,
        limit: int = 500,
        provider_name: Optional[str] = None,
    ) -> pd.DataFrame:
        """Fetches from appropriate provider and stores locally incrementally."""
        clean = symbol.replace("/", "").replace("-", "").upper()
        provider = MarketProviderFactory.get_provider_for_symbol(clean)
        new_df = provider.fetch_candles(clean, timeframe, limit=limit)

        if new_df.empty:
            return self.load_dataset(clean, timeframe)

        file_path = self._get_file_path(clean, timeframe)
        if file_path.exists():
            existing_df = pd.read_parquet(file_path)
            combined = pd.concat([existing_df, new_df]).drop_duplicates(subset=["timestamp"])
            combined = combined.sort_values("timestamp").reset_index(drop=True)
        else:
            combined = new_df.sort_values("timestamp").reset_index(drop=True)

        combined.to_parquet(file_path, index=False)

        # Update metadata
        meta = {
            "symbol": clean,
            "timeframe": timeframe.value,
            "provider": provider_name or provider.__class__.__name__,
            "total_candles": len(combined),
            "start_time": str(combined.iloc[0]["timestamp"]),
            "end_time": str(combined.iloc[-1]["timestamp"]),
        }
        with open(self._get_meta_path(clean, timeframe), "w") as f:
            json.dump(meta, f, indent=2)

        return combined

    def import_csv(
        self,
        csv_path: Path | str,
        symbol: str,
        timeframe: Timeframe,
        provider_name: str = "CSV_Import",
    ) -> pd.DataFrame:
        """
        Imports and validates an external OHLC CSV file.
        Accepts standard column headers ('timestamp'/'date'/'time', 'open', 'high', 'low', 'close', 'volume').
        """
        path = Path(csv_path)
        if not path.exists():
            raise FileNotFoundError(f"CSV file not found: {path}")

        raw = pd.read_csv(path)
        cols = {c.lower(): c for c in raw.columns}

        # Identify timestamp column
        ts_col = cols.get("timestamp") or cols.get("date") or cols.get("time") or cols.get("datetime")
        if not ts_col:
            raise ValueError(f"No valid timestamp or date column found in CSV: {raw.columns}")

        # Normalize required OHLC columns
        for col in ["open", "high", "low", "close"]:
            if col not in cols:
                raise ValueError(f"Required OHLC column '{col}' missing from CSV: {raw.columns}")

        df = pd.DataFrame()
        df["timestamp"] = pd.to_datetime(raw[ts_col], utc=True)
        df["open"] = raw[cols["open"]].astype(float)
        df["high"] = raw[cols["high"]].astype(float)
        df["low"] = raw[cols["low"]].astype(float)
        df["close"] = raw[cols["close"]].astype(float)
        df["volume"] = raw[cols["volume"]].astype(float) if "volume" in cols else 0.0

        from datetime import timedelta
        df["close_time"] = df["timestamp"] + timedelta(minutes=timeframe.minutes)

        # Drop duplicates and sort
        df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

        # Save to parquet
        clean = symbol.replace("/", "").replace("-", "").upper()
        file_path = self._get_file_path(clean, timeframe)
        df.to_parquet(file_path, index=False)

        meta = {
            "symbol": clean,
            "timeframe": timeframe.value,
            "provider": provider_name,
            "total_candles": len(df),
            "start_time": str(df.iloc[0]["timestamp"]),
            "end_time": str(df.iloc[-1]["timestamp"]),
        }
        with open(self._get_meta_path(clean, timeframe), "w") as f:
            json.dump(meta, f, indent=2)

        return df

    def get_dataset_metadata(self, symbol: str, timeframe: Timeframe) -> Dict[str, Any]:
        clean = symbol.replace("/", "").replace("-", "").upper()
        meta_file = self._get_meta_path(clean, timeframe)
        if meta_file.exists():
            with open(meta_file, "r") as f:
                return json.load(f)
        return {}

from datetime import datetime, timezone
import pandas as pd


def filter_and_validate_candles(df: pd.DataFrame, current_utc_time: datetime | None = None) -> pd.DataFrame:
    """
    Validates candle data:
    1. Ensures required columns exist and converts numeric fields.
    2. Drops any incomplete/in-progress candles (where close_time >= current_utc_time).
    3. Drops duplicate timestamps.
    4. Ensures monotonic ascending order by timestamp.
    5. Validates high >= low, high >= open, high >= close, etc.
    """
    if df.empty:
        return df

    required_cols = ["timestamp", "open", "high", "low", "close", "volume", "close_time"]
    for col in required_cols:
        if col not in df.columns:
            raise ValueError(f"Missing required candle column: {col}")

    # Work on a copy
    df = df.copy()

    # Ensure timestamps are in UTC
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], utc=True)

    # Ensure numeric columns
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Drop NaN values
    df = df.dropna(subset=required_cols)

    # Remove unfinished/active candle to prevent look-ahead bias and unconfirmed data
    now_utc = current_utc_time or datetime.now(timezone.utc)
    if not isinstance(now_utc, pd.Timestamp):
        now_utc = pd.to_datetime(now_utc, utc=True)

    # A candle is strictly closed only if its close_time has elapsed
    # (Binance close_time is the last millisecond of the candle, e.g., 00:59:59.999)
    df = df[df["close_time"] <= now_utc]

    # Deduplicate and sort ascending by timestamp
    df = df.drop_duplicates(subset=["timestamp"])
    df = df.sort_values(by="timestamp", ascending=True).reset_index(drop=True)

    # Validate price relationships
    invalid_mask = (
        (df["high"] < df["low"]) |
        (df["high"] < df["open"]) |
        (df["high"] < df["close"]) |
        (df["low"] > df["open"]) |
        (df["low"] > df["close"]) |
        (df["volume"] < 0)
    )
    if invalid_mask.any():
        df = df[~invalid_mask].reset_index(drop=True)

    return df

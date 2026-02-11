"""
Alpaca data pool for fetching OHLCV bars.

Replaces yfinance for daily OHLCV data fetching. Uses Alpaca's StockHistoricalDataClient
which supports batch requests (100+ symbols per call) and provides SIP-quality daily bars
on the free tier (with 15-minute delay, irrelevant for daily data).

yfinance is still used for metadata: company info, earnings, recommendations, news.
"""

import logging
import os
import threading
from datetime import UTC, datetime, timedelta

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

logger = logging.getLogger("maverick_mcp.alpaca_pool")

# Period string to days mapping (for compatibility with yfinance-style period args)
_PERIOD_DAYS = {
    "1d": 1,
    "2d": 2,
    "5d": 5,
    "1mo": 30,
    "3mo": 90,
    "6mo": 180,
    "1y": 365,
    "2y": 730,
    "5y": 1825,
    "10y": 3650,
}


class AlpacaDataPool:
    """Thread-safe singleton for Alpaca historical data fetching."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        api_key = os.environ.get("ALPACA_API_KEY") or os.environ.get(
            "APCA_API_KEY_ID", ""
        )
        secret_key = os.environ.get("ALPACA_SECRET_KEY") or os.environ.get(
            "APCA_API_SECRET_KEY", ""
        )
        if not api_key or not secret_key:
            raise ValueError(
                "Alpaca credentials required. Set ALPACA_API_KEY and ALPACA_SECRET_KEY."
            )
        self._client = StockHistoricalDataClient(
            api_key=api_key, secret_key=secret_key
        )
        self._initialized = True
        logger.info("AlpacaDataPool initialized")

    def get_history(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
        period: str | None = None,
        interval: str = "1d",
    ) -> pd.DataFrame:
        """Fetch daily bars for a single symbol.

        Args:
            symbol: Stock ticker (e.g. "AAPL")
            start: Start date "YYYY-MM-DD" (optional if period is set)
            end: End date "YYYY-MM-DD" (optional, defaults to today)
            period: Period string like "5d", "1mo", "1y" (alternative to start/end)
            interval: Only "1d" is supported

        Returns:
            DataFrame with columns: Open, High, Low, Close, Volume
            Index: timezone-naive dates
        """
        if interval != "1d":
            raise ValueError(
                f"AlpacaDataPool only supports daily bars (interval='1d'), got '{interval}'"
            )

        # Resolve dates from period if needed
        if period and not start:
            days = _PERIOD_DAYS.get(period)
            if days is None:
                raise ValueError(f"Unsupported period: {period}")
            end_dt = datetime.now(UTC)
            start_dt = end_dt - timedelta(days=days)
            start = start_dt.strftime("%Y-%m-%d")
            end = end_dt.strftime("%Y-%m-%d")

        result = self.batch_get_history([symbol], start, end)
        return result.get(symbol, pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"]))

    def batch_get_history(
        self,
        symbols: list[str],
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, pd.DataFrame]:
        """Fetch daily bars for multiple symbols in a single API call.

        Args:
            symbols: List of ticker symbols
            start: Start date "YYYY-MM-DD"
            end: End date "YYYY-MM-DD"

        Returns:
            Dict mapping symbol -> DataFrame with columns: Open, High, Low, Close, Volume
        """
        if not symbols:
            return {}

        # Default date range
        if not end:
            end = datetime.now(UTC).strftime("%Y-%m-%d")
        if not start:
            start = (datetime.now(UTC) - timedelta(days=365)).strftime("%Y-%m-%d")

        start_dt = datetime.strptime(start, "%Y-%m-%d")
        end_dt = datetime.strptime(end, "%Y-%m-%d")

        request = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            start=start_dt,
            end=end_dt,
        )

        logger.info(
            f"Fetching Alpaca bars for {len(symbols)} symbols ({start} to {end})"
        )
        bars = self._client.get_stock_bars(request)

        df = bars.df
        if df.empty:
            logger.warning("Alpaca returned empty DataFrame")
            return {}

        result: dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            try:
                if symbol not in df.index.get_level_values(0):
                    continue
                sym_df = df.loc[symbol].copy()

                # Capitalize column names to match existing pipeline
                sym_df = sym_df.rename(
                    columns={
                        "open": "Open",
                        "high": "High",
                        "low": "Low",
                        "close": "Close",
                        "volume": "Volume",
                    }
                )

                # Convert timestamp index to timezone-naive dates
                sym_df.index = sym_df.index.tz_localize(None)
                sym_df.index.name = "Date"

                # Keep only OHLCV columns
                keep = [
                    c
                    for c in ["Open", "High", "Low", "Close", "Volume"]
                    if c in sym_df.columns
                ]
                sym_df = sym_df[keep]

                if not sym_df.empty:
                    result[symbol] = sym_df
            except Exception as e:
                logger.warning(f"Error processing Alpaca bars for {symbol}: {e}")

        logger.info(f"Got Alpaca bars for {len(result)}/{len(symbols)} symbols")
        return result


def get_alpaca_pool() -> AlpacaDataPool:
    """Get or create the singleton AlpacaDataPool instance."""
    return AlpacaDataPool()

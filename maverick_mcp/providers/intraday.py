"""Intraday data service for fresh price data via yfinance.

Fetches 15-minute intraday bars, consolidates them into a synthetic
"today" daily bar (open/high/low/close/volume), and provides a batch
refresh endpoint used by the autonomous trader pipeline.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd

from maverick_mcp.utils.yfinance_pool import YFinancePool

logger = logging.getLogger(__name__)


def fetch_intraday_bars(
    symbol: str,
    interval: str = "15m",
    period: str = "1d",
) -> pd.DataFrame:
    """Fetch intraday bars for a single symbol via yfinance.

    Args:
        symbol: Ticker symbol (e.g. "AAPL").
        interval: Bar interval — "1m", "5m", "15m", "30m", "1h".
        period: Lookback period — typically "1d" for today's bars.

    Returns:
        DataFrame with columns [Open, High, Low, Close, Volume] indexed by
        datetime, or an empty DataFrame on failure.
    """
    pool = YFinancePool()
    try:
        df = pool.get_history(symbol, period=period, interval=interval)
        if df is None or df.empty:
            logger.debug("No intraday data for %s (%s/%s)", symbol, interval, period)
            return pd.DataFrame()
        return df
    except Exception as e:
        logger.warning("fetch_intraday_bars(%s) failed: %s", symbol, e)
        return pd.DataFrame()


def consolidate_to_today_bar(
    intraday_df: pd.DataFrame,
) -> Optional[dict[str, Any]]:
    """Consolidate intraday bars into a single synthetic daily bar.

    Args:
        intraday_df: DataFrame from ``fetch_intraday_bars`` with OHLCV columns.

    Returns:
        Dict with keys {open, high, low, close, volume, bar_count, as_of} or
        None if the input is empty / invalid.
    """
    if intraday_df is None or intraday_df.empty:
        return None

    # Normalise column names to lowercase for consistency
    df = intraday_df.copy()
    df.columns = [c.lower() for c in df.columns]

    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(set(df.columns)):
        logger.warning(
            "consolidate_to_today_bar: missing columns %s",
            required - set(df.columns),
        )
        return None

    try:
        return {
            "open": float(df["open"].iloc[0]),
            "high": float(df["high"].max()),
            "low": float(df["low"].min()),
            "close": float(df["close"].iloc[-1]),
            "volume": int(df["volume"].sum()),
            "bar_count": len(df),
            "as_of": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.warning("consolidate_to_today_bar failed: %s", e)
        return None


def refresh_intraday_batch(
    tickers: list[str],
    interval: str = "15m",
) -> dict[str, Any]:
    """Fetch and consolidate intraday bars for a batch of tickers.

    Args:
        tickers: List of ticker symbols.
        interval: Bar interval (default "15m").

    Returns:
        Dict with keys:
        - status: "success" | "partial" | "error"
        - refreshed: number of tickers successfully refreshed
        - errors: list of {ticker, error} dicts
        - bars: {ticker: today_bar_dict} for successful tickers
        - source: "yfinance_intraday"
        - timestamp: ISO-8601 UTC timestamp
    """
    bars: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []

    for ticker in tickers:
        try:
            df = fetch_intraday_bars(ticker, interval=interval)
            today_bar = consolidate_to_today_bar(df)
            if today_bar:
                bars[ticker] = today_bar
            else:
                errors.append({"ticker": ticker, "error": "no data or empty"})
        except Exception as e:
            errors.append({"ticker": ticker, "error": str(e)})
            logger.warning("refresh_intraday_batch: %s failed: %s", ticker, e)

    # Invalidate technical analysis cache for refreshed tickers
    if bars:
        try:
            from maverick_mcp.data.cache import clear_cache

            for ticker in bars:
                clear_cache(f"v1:technical:full:{ticker}:*")
        except Exception as e:
            logger.debug("Cache invalidation failed: %s", e)

    refreshed = len(bars)
    total = len(tickers)

    if refreshed == total:
        status = "success"
    elif refreshed > 0:
        status = "partial"
    else:
        status = "error"

    return {
        "status": status,
        "refreshed": refreshed,
        "total": total,
        "errors": errors,
        "bars": bars,
        "source": "yfinance_intraday",
        "interval": interval,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

"""
Stock screening router for Maverick-MCP.

This module contains all stock screening related tools including
Maverick, supply/demand breakouts, and other screening strategies.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Create the screening router
screening_router: FastMCP = FastMCP("Stock_Screening")


def get_maverick_stocks(limit: int = 20, bypass_cache: bool = False) -> dict[str, Any]:
    """
    Get top Maverick stocks from the screening results.

    DISCLAIMER: Stock screening results are for educational and research purposes only.
    This is not investment advice. Past performance does not guarantee future results.
    Always conduct thorough research and consult financial professionals before investing.

    The Maverick screening strategy identifies stocks with:
    - High momentum strength
    - Technical patterns (Cup & Handle, consolidation, etc.)
    - Momentum characteristics
    - Strong combined scores

    Args:
        limit: Maximum number of stocks to return (default: 20)
        bypass_cache: If True, skip cache and fetch fresh data

    Returns:
        Dictionary containing Maverick stock screening results
    """
    try:
        from maverick_mcp.data.cache import get_from_cache, save_to_cache

        cache_key = f"v1:screening:maverick:{limit}"
        if not bypass_cache:
            cached = get_from_cache(cache_key)
            if cached is not None:
                return cached

        from maverick_mcp.data.models import MaverickStocks, SessionLocal

        with SessionLocal() as session:
            stocks = MaverickStocks.get_top_stocks(session, limit=limit)

            result = {
                "status": "success",
                "count": len(stocks),
                "stocks": [stock.to_dict() for stock in stocks],
                "screening_type": "maverick_bullish",
                "description": "High momentum stocks with bullish technical setups",
            }

        save_to_cache(cache_key, result, ttl=1800)
        return result
    except Exception as e:
        logger.error(f"Error fetching Maverick stocks: {str(e)}")
        return {"error": str(e), "status": "error"}


def get_maverick_bear_stocks(
    limit: int = 20, bypass_cache: bool = False
) -> dict[str, Any]:
    """
    Get top Maverick Bear stocks from the screening results.

    DISCLAIMER: Bearish screening results are for educational purposes only.
    This is not advice to sell short or make bearish trades. Short selling involves
    unlimited risk potential. Always consult financial professionals before trading.

    The Maverick Bear screening identifies stocks with:
    - Weak momentum strength
    - Bearish technical patterns
    - Distribution characteristics
    - High bear scores

    Args:
        limit: Maximum number of stocks to return (default: 20)
        bypass_cache: If True, skip cache and fetch fresh data

    Returns:
        Dictionary containing Maverick Bear stock screening results
    """
    try:
        from maverick_mcp.data.cache import get_from_cache, save_to_cache

        cache_key = f"v1:screening:bear:{limit}"
        if not bypass_cache:
            cached = get_from_cache(cache_key)
            if cached is not None:
                return cached

        from maverick_mcp.data.models import MaverickBearStocks, SessionLocal

        with SessionLocal() as session:
            stocks = MaverickBearStocks.get_top_stocks(session, limit=limit)

            result = {
                "status": "success",
                "count": len(stocks),
                "stocks": [stock.to_dict() for stock in stocks],
                "screening_type": "maverick_bearish",
                "description": "Weak stocks with bearish technical setups",
            }

        save_to_cache(cache_key, result, ttl=1800)
        return result
    except Exception as e:
        logger.error(f"Error fetching Maverick Bear stocks: {str(e)}")
        return {"error": str(e), "status": "error"}


def get_supply_demand_breakouts(
    limit: int = 20, filter_moving_averages: bool = False, bypass_cache: bool = False
) -> dict[str, Any]:
    """
    Get stocks showing supply/demand breakout patterns from accumulation.

    This screening identifies stocks in the demand expansion phase with:
    - Price above all major moving averages (demand zone)
    - Moving averages in proper alignment indicating accumulation (50 > 150 > 200)
    - Strong momentum strength showing institutional interest
    - Market structure indicating supply absorption and demand dominance

    Args:
        limit: Maximum number of stocks to return (default: 20)
        filter_moving_averages: If True, only return stocks above all moving averages
        bypass_cache: If True, skip cache and fetch fresh data

    Returns:
        Dictionary containing supply/demand breakout screening results
    """
    try:
        from maverick_mcp.data.cache import get_from_cache, save_to_cache

        cache_key = f"v1:screening:breakouts:{limit}:{filter_moving_averages}"
        if not bypass_cache:
            cached = get_from_cache(cache_key)
            if cached is not None:
                return cached

        from maverick_mcp.data.models import SessionLocal, SupplyDemandBreakoutStocks

        with SessionLocal() as session:
            if filter_moving_averages:
                stocks = SupplyDemandBreakoutStocks.get_stocks_above_moving_averages(
                    session
                )[:limit]
            else:
                stocks = SupplyDemandBreakoutStocks.get_top_stocks(session, limit=limit)

            result = {
                "status": "success",
                "count": len(stocks),
                "stocks": [stock.to_dict() for stock in stocks],
                "screening_type": "supply_demand_breakout",
                "description": "Stocks breaking out from accumulation with strong demand dynamics",
            }

        save_to_cache(cache_key, result, ttl=1800)
        return result
    except Exception as e:
        logger.error(f"Error fetching supply/demand breakout stocks: {str(e)}")
        return {"error": str(e), "status": "error"}


def get_all_screening_recommendations() -> dict[str, Any]:
    """
    Get comprehensive screening results from all strategies.

    This tool returns the top stocks from each screening strategy:
    - Maverick Bullish: High momentum growth stocks
    - Maverick Bearish: Weak stocks for short opportunities
    - Supply/Demand Breakouts: Stocks breaking out from accumulation phases

    Returns:
        Dictionary containing all screening results organized by strategy
    """
    try:
        from maverick_mcp.providers.stock_data import StockDataProvider

        provider = StockDataProvider()
        return provider.get_all_screening_recommendations()
    except Exception as e:
        logger.error(f"Error getting all screening recommendations: {e}")
        return {
            "error": str(e),
            "status": "error",
            "maverick_stocks": [],
            "maverick_bear_stocks": [],
            "supply_demand_breakouts": [],
        }


def get_screening_by_criteria(
    min_momentum_score: float | str | None = None,
    min_volume: int | str | None = None,
    max_price: float | str | None = None,
    sector: str | None = None,
    limit: int | str = 20,
) -> dict[str, Any]:
    """
    Get stocks filtered by specific screening criteria.

    This tool allows custom filtering across all screening results based on:
    - Momentum score rating
    - Volume requirements
    - Price constraints
    - Sector preferences

    Args:
        min_momentum_score: Minimum momentum score rating (0-100)
        min_volume: Minimum average daily volume
        max_price: Maximum stock price
        sector: Specific sector to filter (e.g., "Technology")
        limit: Maximum number of results

    Returns:
        Dictionary containing filtered screening results
    """
    try:
        from maverick_mcp.data.models import MaverickStocks, SessionLocal

        # Convert string inputs to appropriate numeric types
        if min_momentum_score is not None:
            min_momentum_score = float(min_momentum_score)
        if min_volume is not None:
            min_volume = int(min_volume)
        if max_price is not None:
            max_price = float(max_price)
        if isinstance(limit, str):
            limit = int(limit)

        with SessionLocal() as session:
            query = session.query(MaverickStocks)

            if min_momentum_score:
                query = query.filter(
                    MaverickStocks.momentum_score >= min_momentum_score
                )

            if min_volume:
                query = query.filter(MaverickStocks.avg_vol_30d >= min_volume)

            if max_price:
                query = query.filter(MaverickStocks.close_price <= max_price)

            # Note: Sector filtering would require joining with Stock table
            # This is a simplified version

            stocks = (
                query.order_by(MaverickStocks.combined_score.desc()).limit(limit).all()
            )

            return {
                "status": "success",
                "count": len(stocks),
                "stocks": [stock.to_dict() for stock in stocks],
                "criteria": {
                    "min_momentum_score": min_momentum_score,
                    "min_volume": min_volume,
                    "max_price": max_price,
                    "sector": sector,
                },
            }
    except Exception as e:
        logger.error(f"Error in custom screening: {str(e)}")
        return {"error": str(e), "status": "error"}


def _merge_candidate(
    candidates: dict[str, dict[str, Any]],
    ticker: str,
    composite: float,
    algorithm: str,
    data: dict[str, Any],
) -> None:
    """Merge a screening candidate into the deduplicated candidates dict."""
    if ticker not in candidates or composite > candidates[ticker]["composite_score"]:
        candidates[ticker] = {
            "ticker": ticker,
            "composite_score": round(composite, 1),
            "algorithms": [algorithm],
            **data,
        }
    else:
        if algorithm not in candidates[ticker]["algorithms"]:
            candidates[ticker]["algorithms"].append(algorithm)
        candidates[ticker]["composite_score"] = max(
            candidates[ticker]["composite_score"], round(composite, 1)
        )


def _process_maverick_stocks(
    session, days_back: int, candidates: dict
) -> tuple[int, Any]:
    """Process Maverick bullish screening results into candidates."""
    from maverick_mcp.data.models import MaverickStocks

    stocks = MaverickStocks.get_latest_analysis(session, days_back=days_back)
    screening_date = None

    for stock in stocks:
        ticker = stock.stock.ticker_symbol if stock.stock else None
        if not ticker:
            continue

        combined = float(stock.combined_score or 0)
        momentum = float(stock.momentum_score or 0)
        composite = (combined / 8.0 * 100 * 0.6) + (momentum * 0.4)

        _merge_candidate(
            candidates,
            ticker,
            composite,
            "maverick_bullish",
            {
                "momentum_score": round(momentum, 1),
                "combined_score": int(combined),
                "close_price": float(stock.close_price or 0),
            },
        )

        if stock.date_analyzed and (
            screening_date is None or stock.date_analyzed > screening_date
        ):
            screening_date = stock.date_analyzed

    return len(stocks), screening_date


def _process_supply_demand_stocks(
    session, days_back: int, candidates: dict
) -> tuple[int, Any]:
    """Process Supply/Demand breakout screening results into candidates."""
    from maverick_mcp.data.models import SupplyDemandBreakoutStocks

    stocks = SupplyDemandBreakoutStocks.get_top_stocks(session, limit=100)
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days_back)
    count = 0
    screening_date = None

    for stock in stocks:
        ticker = stock.stock.ticker_symbol if stock.stock else None
        if not ticker:
            continue
        if stock.date_analyzed and stock.date_analyzed < cutoff:
            continue

        count += 1
        momentum = float(stock.momentum_score or 0)
        accumulation = float(stock.accumulation_rating or 0)
        breakout = float(stock.breakout_strength or 0)
        composite = (momentum * 0.5) + (accumulation * 0.3) + (breakout * 20 * 0.2)

        _merge_candidate(
            candidates,
            ticker,
            composite,
            "supply_demand_breakout",
            {
                "momentum_score": round(momentum, 1),
                "breakout_strength": round(breakout, 1),
                "close_price": float(stock.close_price or 0),
            },
        )

        if stock.date_analyzed and (
            screening_date is None or stock.date_analyzed > screening_date
        ):
            screening_date = stock.date_analyzed

    return count, screening_date


def _process_bear_stocks(session, days_back: int, candidates: dict) -> tuple[int, Any]:
    """Process Maverick Bear screening results into candidates."""
    from maverick_mcp.data.models import MaverickBearStocks

    stocks = MaverickBearStocks.get_latest_analysis(session, days_back=days_back)
    screening_date = None

    for stock in stocks:
        ticker = stock.stock.ticker_symbol if stock.stock else None
        if not ticker:
            continue

        score = float(stock.score or 0)
        momentum = float(stock.momentum_score or 0)
        composite = (score * 0.6) + ((100 - momentum) * 0.4)

        _merge_candidate(
            candidates,
            ticker,
            composite,
            "maverick_bearish",
            {
                "momentum_score": round(momentum, 1),
                "bear_score": int(score),
                "close_price": float(stock.close_price or 0),
            },
        )

        if stock.date_analyzed and (
            screening_date is None or stock.date_analyzed > screening_date
        ):
            screening_date = stock.date_analyzed

    return len(stocks), screening_date


def get_ranked_watchlist(
    max_symbols: int | str = 10,
    include_bearish: bool | str = False,
    days_back: int | str = 3,
    bypass_cache: bool = False,
) -> dict[str, Any]:
    """
    Get a ranked, deduplicated watchlist from all screening algorithms.

    Queries Maverick bullish, Supply/Demand breakout, and optionally Bear
    screening results, normalizes scores to 0-100, deduplicates by ticker,
    and returns top N ranked candidates.

    Args:
        max_symbols: Maximum symbols to return (default: 10)
        include_bearish: Whether to include bearish setups (default: False)
        days_back: Days back to look for screening results (default: 3, handles weekends)
        bypass_cache: If True, skip cache and fetch fresh data

    Returns:
        Dictionary containing ranked watchlist with scores and algorithms
    """
    try:
        max_symbols = int(max_symbols)
        if isinstance(include_bearish, str):
            include_bearish = include_bearish.lower() in ("true", "1", "yes")
        days_back = int(days_back)

        from maverick_mcp.data.cache import get_from_cache, save_to_cache

        cache_key = f"v1:screening:ranked:{max_symbols}:{include_bearish}:{days_back}"
        if not bypass_cache:
            cached = get_from_cache(cache_key)
            if cached is not None:
                return cached

        from maverick_mcp.data.models import SessionLocal

        candidates: dict[str, dict[str, Any]] = {}
        algorithms_queried = ["maverick_bullish", "supply_demand_breakout"]

        with SessionLocal() as session:
            maverick_count, maverick_date = _process_maverick_stocks(
                session, days_back, candidates
            )
            sd_count, sd_date = _process_supply_demand_stocks(
                session, days_back, candidates
            )

            bear_count = 0
            bear_date = None
            if include_bearish:
                algorithms_queried.append("maverick_bearish")
                bear_count, bear_date = _process_bear_stocks(
                    session, days_back, candidates
                )

        total_candidates = maverick_count + sd_count + bear_count

        ranked = sorted(
            candidates.values(),
            key=lambda x: x["composite_score"],
            reverse=True,
        )[:max_symbols]

        for i, item in enumerate(ranked):
            item["rank"] = i + 1

        all_dates = [d for d in [maverick_date, sd_date, bear_date] if d is not None]
        result = {
            "status": "success",
            "watchlist": ranked,
            "total_candidates": total_candidates,
            "algorithms_queried": algorithms_queried,
            "screening_date": maverick_date.isoformat() if maverick_date else None,
            "data_freshness": {
                "oldest_date": min(all_dates).isoformat() if all_dates else None,
                "maverick_date": maverick_date.isoformat() if maverick_date else None,
                "supply_demand_date": sd_date.isoformat() if sd_date else None,
                "bear_date": bear_date.isoformat() if bear_date else None,
            },
        }

        save_to_cache(cache_key, result, ttl=1800)
        return result

    except Exception as e:
        logger.error(f"Error getting ranked watchlist: {e}")
        return {"error": str(e), "status": "error"}


# ---------------------------------------------------------------------------
# Market Regime Detection
# ---------------------------------------------------------------------------


def _fetch_spy_metrics() -> dict[str, Any]:
    """Fetch SPY price, 200 SMA, SMA slope, and 52-week high via yfinance."""
    import yfinance as yf

    spy = yf.Ticker("SPY")
    hist = spy.history(period="1y")

    if hist.empty:
        return {"error": "No SPY data"}

    current_price = float(hist["Close"].iloc[-1])
    sma_200 = (
        float(hist["Close"].rolling(200).mean().dropna().iloc[-1])
        if len(hist) >= 200
        else current_price
    )
    # SMA slope: compare current SMA to SMA 22 trading days ago
    sma_series = hist["Close"].rolling(200).mean().dropna()
    if len(sma_series) >= 22:
        sma_slope = float(sma_series.iloc[-1] - sma_series.iloc[-22])
    else:
        sma_slope = 0.0
    high_52w = float(hist["Close"].max())
    pct_from_high = ((current_price - high_52w) / high_52w) * 100

    return {
        "price": round(current_price, 2),
        "sma_200": round(sma_200, 2),
        "sma_rising": sma_slope > 0,
        "high_52w": round(high_52w, 2),
        "pct_from_high": round(pct_from_high, 1),
    }


def _fetch_vix() -> float:
    """Fetch current VIX value via yfinance."""
    import yfinance as yf

    try:
        vix = yf.Ticker("^VIX")
        hist = vix.history(period="5d")
        if not hist.empty:
            return round(float(hist["Close"].iloc[-1]), 2)
    except Exception:
        pass
    return 20.0  # long-term average fallback


def _calculate_breadth() -> float:
    """Calculate breadth proxy: bullish / (bullish + bearish) from screening tables."""
    from maverick_mcp.data.models import (
        MaverickBearStocks,
        MaverickStocks,
        SessionLocal,
    )

    with SessionLocal() as session:
        bullish = MaverickStocks.get_latest_analysis(session, days_back=3)
        bearish = MaverickBearStocks.get_latest_analysis(session, days_back=3)

    bull_count = len(bullish)
    bear_count = len(bearish)
    total = bull_count + bear_count
    if total == 0:
        return 50.0
    return round((bull_count / total) * 100, 1)


def _classify_regime(
    spy_above_sma: bool,
    sma_rising: bool,
    pct_from_high: float,
    vix: float,
    breadth: float,
) -> tuple[str, float, dict[str, Any]]:
    """Classify market regime from indicators. Returns (regime, confidence, guidance)."""
    # CORRECTION: SPY down >10% from 52w high AND VIX > 25
    if pct_from_high < -10 and vix > 25:
        return (
            "CORRECTION",
            0.9,
            {
                "position_size_multiplier": 0.25,
                "allow_new_longs": False,
                "allow_new_shorts": True,
            },
        )

    # STRONG_BEAR: below SMA, SMA falling, low breadth, high VIX
    if not spy_above_sma and not sma_rising and breadth < 30 and vix > 25:
        return (
            "STRONG_BEAR",
            0.85,
            {
                "position_size_multiplier": 0.25,
                "allow_new_longs": False,
                "allow_new_shorts": True,
            },
        )

    # BEAR: below SMA, low breadth
    if not spy_above_sma and breadth < 40:
        return (
            "BEAR",
            0.75,
            {
                "position_size_multiplier": 0.5,
                "allow_new_longs": False,
                "allow_new_shorts": True,
            },
        )

    # STRONG_BULL: above SMA, SMA rising, high breadth, low VIX
    if spy_above_sma and sma_rising and breadth > 60 and vix < 18:
        return (
            "STRONG_BULL",
            0.85,
            {
                "position_size_multiplier": 1.0,
                "allow_new_longs": True,
                "allow_new_shorts": False,
            },
        )

    # BULL: above SMA, decent breadth
    if spy_above_sma and breadth > 50:
        return (
            "BULL",
            0.7,
            {
                "position_size_multiplier": 1.0,
                "allow_new_longs": True,
                "allow_new_shorts": False,
            },
        )

    # NEUTRAL: everything else
    return (
        "NEUTRAL",
        0.5,
        {
            "position_size_multiplier": 0.75,
            "allow_new_longs": True,
            "allow_new_shorts": False,
        },
    )


def get_market_regime() -> dict[str, Any]:
    """Detect current market regime using SPY technicals, VIX, and screening breadth.

    Returns regime classification with strategy guidance for position sizing
    and directional bias. Uses deterministic thresholds (no ML).

    Regimes: STRONG_BULL, BULL, NEUTRAL, BEAR, STRONG_BEAR, CORRECTION

    Returns:
        Dictionary with regime, confidence, indicators, and strategy_guidance
    """
    try:
        from maverick_mcp.data.cache import get_from_cache, save_to_cache

        cache_key = "v1:market:regime"
        cached = get_from_cache(cache_key)
        if cached is not None:
            return cached

        spy = _fetch_spy_metrics()
        if "error" in spy:
            return _default_regime(spy["error"])

        vix = _fetch_vix()
        breadth = _calculate_breadth()

        spy_above_sma = spy["price"] > spy["sma_200"]
        regime, confidence, guidance = _classify_regime(
            spy_above_sma=spy_above_sma,
            sma_rising=spy["sma_rising"],
            pct_from_high=spy["pct_from_high"],
            vix=vix,
            breadth=breadth,
        )

        result = {
            "status": "success",
            "regime": regime,
            "confidence": confidence,
            "spy_price": spy["price"],
            "spy_sma_200": spy["sma_200"],
            "spy_above_sma": spy_above_sma,
            "sma_rising": spy["sma_rising"],
            "pct_from_52w_high": spy["pct_from_high"],
            "vix": vix,
            "breadth_pct": breadth,
            "strategy_guidance": guidance,
        }

        save_to_cache(cache_key, result, ttl=300)
        return result

    except Exception as e:
        logger.error(f"Error detecting market regime: {e}")
        return _default_regime(str(e))


# ---------------------------------------------------------------------------
# Earnings Calendar
# ---------------------------------------------------------------------------


_NO_EARNINGS: dict[str, None] = {"next_earnings": None, "days_until": None}
_EARNINGS_DATE_KEY = "Earnings Date"


def _extract_earnings_date(cal: Any) -> Any:
    """Extract the raw earnings date from a yfinance calendar (dict or DataFrame)."""
    if isinstance(cal, dict):
        val = cal.get(_EARNINGS_DATE_KEY)
        if isinstance(val, list) and val:
            return val[0]
        return val

    # DataFrame format
    if hasattr(cal, "loc") and _EARNINGS_DATE_KEY in cal.index:
        val = cal.loc[_EARNINGS_DATE_KEY]
        return val.iloc[0] if hasattr(val, "iloc") else val

    return None


def _normalize_earnings_date(raw_date: Any, today) -> dict[str, Any]:
    """Normalize a raw earnings date to {next_earnings, days_until}."""
    if raw_date is None:
        return dict(_NO_EARNINGS)

    if hasattr(raw_date, "date"):
        raw_date = raw_date.date()
    elif isinstance(raw_date, str):
        raw_date = datetime.fromisoformat(raw_date).date()

    days_until = (raw_date - today).days
    if days_until < 0:
        return dict(_NO_EARNINGS)

    return {"next_earnings": raw_date.isoformat(), "days_until": days_until}


def get_earnings_calendar(tickers: list[str] | str) -> dict[str, Any]:
    """Get next earnings dates for a list of tickers.

    Uses yfinance to look up the next earnings date for each ticker.
    Useful for avoiding entries right before earnings announcements.

    Args:
        tickers: List of ticker symbols, or comma-separated string

    Returns:
        Dictionary mapping ticker to earnings info (next_earnings date, days_until)
    """
    import yfinance as yf

    if isinstance(tickers, str):
        tickers = [t.strip() for t in tickers.split(",") if t.strip()]

    results: dict[str, dict[str, Any]] = {}
    today = datetime.now(timezone.utc).date()

    for ticker in tickers:
        try:
            stock = yf.Ticker(ticker)
            cal = stock.calendar
            if cal is None or (hasattr(cal, "empty") and cal.empty):
                results[ticker] = dict(_NO_EARNINGS)
                continue

            raw_date = _extract_earnings_date(cal)
            results[ticker] = _normalize_earnings_date(raw_date, today)

        except Exception as e:
            logger.warning(f"Failed to get earnings for {ticker}: {e}")
            results[ticker] = dict(_NO_EARNINGS)

    return {
        "status": "success",
        "earnings": results,
        "tickers_checked": len(tickers),
    }


def _default_regime(error: str = "") -> dict[str, Any]:
    """Return a safe NEUTRAL default when regime detection fails."""
    return {
        "status": "error" if error else "success",
        "regime": "NEUTRAL",
        "confidence": 0.0,
        "spy_price": 0.0,
        "spy_sma_200": 0.0,
        "spy_above_sma": True,
        "sma_rising": True,
        "pct_from_52w_high": 0.0,
        "vix": 20.0,
        "breadth_pct": 50.0,
        "strategy_guidance": {
            "position_size_multiplier": 0.75,
            "allow_new_longs": True,
            "allow_new_shorts": False,
        },
        "error": error,
    }

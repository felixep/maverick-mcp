"""
Direct REST API for programmatic consumers (autonomous-trader, n8n, etc.).

This router exposes lightweight HTTP endpoints that bypass the MCP protocol
handshake overhead. Each MCP tool call through the SSE proxy costs ~1 second
of init/teardown.  These REST endpoints eliminate that overhead entirely.

MCP (via /sse) remains available for Claude Desktop; this API is additive.
"""

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

trader_router = APIRouter(prefix="/v1", tags=["trader-api"])

# Limit concurrent ticker analyses in batch endpoint to avoid DB/API contention
_batch_semaphore: asyncio.Semaphore | None = None


def _get_batch_semaphore() -> asyncio.Semaphore:
    global _batch_semaphore
    if _batch_semaphore is None:
        _batch_semaphore = asyncio.Semaphore(10)
    return _batch_semaphore


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class BatchAnalysisRequest(BaseModel):
    tickers: list[str] = Field(..., min_length=1, max_length=50)
    include_news: bool = True
    days: int = 365
    intraday_bars: dict[str, dict[str, Any]] | None = None
    fingerprints: dict[str, str] | None = None  # Phase 6: {ticker: fingerprint}


class IntradayRefreshRequest(BaseModel):
    tickers: list[str] = Field(..., min_length=1, max_length=20)
    interval: str = Field("15m", pattern=r"^(1m|5m|15m|30m|1h)$")


class ScreeningRefreshRequest(BaseModel):
    symbols: list[str] | None = None


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@trader_router.get("/health")
async def health() -> dict[str, Any]:
    """Lightweight health probe — no DB/Redis/API checks."""
    return {"status": "ok", "timestamp": datetime.now(UTC).isoformat()}


# ---------------------------------------------------------------------------
# Technical analysis
# ---------------------------------------------------------------------------


@trader_router.post("/technical/full-analysis")
async def full_technical_analysis(
    ticker: str = Query(...),
    days: int = Query(365),
) -> dict[str, Any]:
    """Full technical analysis for a single ticker."""
    from maverick_mcp.api.routers.technical_enhanced import (
        get_full_technical_analysis_enhanced,
    )
    from maverick_mcp.validation.technical import TechnicalAnalysisRequest

    request = TechnicalAnalysisRequest(ticker=ticker, days=days)
    return await get_full_technical_analysis_enhanced(request)


@trader_router.post("/technical/support-resistance")
async def support_resistance(
    ticker: str = Query(...),
    days: int = Query(365),
) -> dict[str, Any]:
    """Support and resistance levels for a single ticker."""
    from maverick_mcp.api.routers.technical import get_support_resistance

    return await get_support_resistance(ticker, days)


# ---------------------------------------------------------------------------
# News / sentiment
# ---------------------------------------------------------------------------


@trader_router.post("/news/sentiment")
async def news_sentiment(
    ticker: str = Query(...),
    timeframe: str = Query("7d"),
    limit: int = Query(10),
) -> dict[str, Any]:
    """News sentiment analysis for a single ticker."""
    from maverick_mcp.api.routers.news_sentiment_enhanced import (
        get_news_sentiment_enhanced,
    )

    return await get_news_sentiment_enhanced(ticker, timeframe, limit)


# ---------------------------------------------------------------------------
# Screening
# ---------------------------------------------------------------------------


@trader_router.get("/screening/maverick")
def screening_maverick(
    limit: int = Query(20),
    bypass_cache: bool = Query(False),
) -> dict[str, Any]:
    """Top Maverick bullish stocks."""
    from maverick_mcp.api.routers.screening import get_maverick_stocks

    return get_maverick_stocks(limit, bypass_cache=bypass_cache)


@trader_router.get("/screening/bear")
def screening_bear(
    limit: int = Query(20),
    bypass_cache: bool = Query(False),
) -> dict[str, Any]:
    """Top Maverick bearish stocks."""
    from maverick_mcp.api.routers.screening import get_maverick_bear_stocks

    return get_maverick_bear_stocks(limit, bypass_cache=bypass_cache)


@trader_router.get("/screening/breakouts")
def screening_breakouts(
    limit: int = Query(20),
    bypass_cache: bool = Query(False),
) -> dict[str, Any]:
    """Top supply/demand breakout stocks."""
    from maverick_mcp.api.routers.screening import get_supply_demand_breakouts

    return get_supply_demand_breakouts(limit, bypass_cache=bypass_cache)


@trader_router.get("/screening/ranked-watchlist")
def screening_ranked_watchlist(
    max_symbols: int = Query(10),
    include_bearish: bool = Query(False),
    days_back: int = Query(3),
    bypass_cache: bool = Query(False),
    sort_by: str = Query("balanced", pattern=r"^(balanced|momentum|oversold|breakout_proximity|novelty)$"),
    exclude: str = Query(""),
) -> dict[str, Any]:
    """Ranked, deduplicated watchlist from all screening algorithms."""
    from maverick_mcp.api.routers.screening import get_ranked_watchlist

    exclude_list = [t.strip().upper() for t in exclude.split(",") if t.strip()] if exclude else []
    return get_ranked_watchlist(
        max_symbols, include_bearish, days_back,
        bypass_cache=bypass_cache, sort_by=sort_by, exclude=exclude_list,
    )


# ---------------------------------------------------------------------------
# Screening delta APIs (Phase 7)
# ---------------------------------------------------------------------------

# In-memory snapshot ring buffer (last 24 entries ≈ 6 hours at 15-min scanner)
_screening_snapshots: list[dict[str, Any]] = []
_MAX_SNAPSHOTS = 24


def _content_hash(stocks: list[dict[str, Any]]) -> str:
    """Deterministic SHA-256 of the ranked ticker+score list."""
    canon = json.dumps(
        [{"t": s.get("ticker", s.get("stock_symbol", "")),
          "s": round(s.get("composite_score", s.get("combined_score", 0)), 2)}
         for s in stocks],
        sort_keys=True,
    )
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def _staleness_label(ts: str) -> str:
    """Return 'fresh' / 'stale' / 'expired' based on age of timestamp."""
    try:
        dt = datetime.fromisoformat(ts)
        age = (datetime.now(UTC) - dt).total_seconds()
    except Exception:
        return "unknown"
    if age < 1800:
        return "fresh"
    if age < 7200:
        return "stale"
    return "expired"


def _take_snapshot(watchlist: list[dict[str, Any]], label: str = "auto") -> dict[str, Any]:
    """Append a snapshot to the ring buffer and return it."""
    now = datetime.now(UTC).isoformat()
    tickers = [s.get("ticker", s.get("stock_symbol", "")) for s in watchlist]
    snap: dict[str, Any] = {
        "timestamp": now,
        "content_hash": _content_hash(watchlist),
        "count": len(watchlist),
        "tickers": tickers,
        "label": label,
    }
    _screening_snapshots.append(snap)
    if len(_screening_snapshots) > _MAX_SNAPSHOTS:
        _screening_snapshots.pop(0)
    return snap


@trader_router.get("/screening/cache-info")
def screening_cache_info() -> dict[str, Any]:
    """Content hash + staleness label for the current screening data.

    The trader can call this before fetching the full watchlist:
    if the hash matches the previous cycle, skip the screening step entirely.
    """
    from maverick_mcp.api.routers.screening import get_ranked_watchlist

    result = get_ranked_watchlist(max_symbols=20, include_bearish=False)
    watchlist = result.get("watchlist", [])
    ch = _content_hash(watchlist)
    ts = result.get("timestamp", datetime.now(UTC).isoformat())

    # Auto-snapshot on every cache-info call
    _take_snapshot(watchlist, label="cache-info")

    return {
        "content_hash": ch,
        "count": len(watchlist),
        "staleness": _staleness_label(ts),
        "timestamp": ts,
    }


@trader_router.get("/screening/deltas")
def screening_deltas(
    since_hash: str = Query("", description="Content hash from a previous cache-info call"),
) -> dict[str, Any]:
    """Compute deltas between the current screening and a previous snapshot.

    If *since_hash* matches the current hash, returns ``changed: false``.
    Otherwise returns new entries, exits, and rank changes.
    """
    from maverick_mcp.api.routers.screening import get_ranked_watchlist

    result = get_ranked_watchlist(max_symbols=20, include_bearish=False)
    watchlist = result.get("watchlist", [])
    current_hash = _content_hash(watchlist)
    current_tickers = [s.get("ticker", "") for s in watchlist]

    if since_hash and since_hash == current_hash:
        return {
            "changed": False,
            "content_hash": current_hash,
            "unchanged_count": len(watchlist),
        }

    # Find the previous snapshot matching since_hash
    prev_tickers: list[str] = []
    for snap in reversed(_screening_snapshots):
        if snap["content_hash"] == since_hash:
            prev_tickers = snap.get("tickers", [])
            break

    current_set = set(current_tickers)
    prev_set = set(prev_tickers)

    new_snap = _take_snapshot(watchlist, label="delta")

    return {
        "changed": True,
        "content_hash": current_hash,
        "previous_hash": since_hash or None,
        "entries": sorted(current_set - prev_set),
        "exits": sorted(prev_set - current_set),
        "unchanged_count": len(current_set & prev_set),
        "current_count": len(current_tickers),
        "timestamp": new_snap["timestamp"],
    }


@trader_router.get("/screening/snapshots")
def screening_snapshots(
    limit: int = Query(10, ge=1, le=50),
) -> dict[str, Any]:
    """Return recent screening snapshots for observability.

    Each snapshot records the content hash, ticker list, and timestamp.
    Useful for the dashboard to visualise ranked-list evolution over the day.
    """
    recent = list(reversed(_screening_snapshots[-limit:]))
    return {
        "count": len(recent),
        "snapshots": recent,
    }


# ---------------------------------------------------------------------------
# Market regime
# ---------------------------------------------------------------------------


@trader_router.get("/market/regime")
def market_regime() -> dict[str, Any]:
    """Detect current market regime (BULL/BEAR/NEUTRAL/CORRECTION)."""
    from maverick_mcp.api.routers.screening import get_market_regime

    return get_market_regime()


# ---------------------------------------------------------------------------
# Screening refresh
# ---------------------------------------------------------------------------


@trader_router.post("/screening/refresh")
async def screening_refresh(
    body: ScreeningRefreshRequest | None = None,
) -> dict[str, Any]:
    """Trigger a screening refresh, optionally for specific symbols."""
    from maverick_mcp.api.server import screening_refresh_now

    symbols = body.symbols if body else None
    return await screening_refresh_now(symbols)


# ---------------------------------------------------------------------------
# Earnings
# ---------------------------------------------------------------------------


@trader_router.get("/earnings")
def earnings_calendar(
    tickers: str = Query(..., description="Comma-separated ticker list"),
) -> dict[str, Any]:
    """Get next earnings dates for a list of tickers."""
    from maverick_mcp.api.routers.screening import get_earnings_calendar

    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    return get_earnings_calendar(ticker_list)


# ---------------------------------------------------------------------------
# Batch analysis (the big win)
# ---------------------------------------------------------------------------


def _unwrap_gathered(value: Any) -> Any:
    """Return an error dict if value is an exception, otherwise the value itself."""
    return {"error": str(value)} if isinstance(value, BaseException) else value


async def _fetch_extras(
    ticker: str, days: int, include_news: bool,
) -> dict[str, Any]:
    """Fetch support/resistance (and optionally news) for a ticker."""
    from maverick_mcp.api.routers.news_sentiment_enhanced import (
        get_news_sentiment_enhanced,
    )
    from maverick_mcp.api.routers.technical import get_support_resistance

    tasks: list[asyncio.Task] = [asyncio.create_task(get_support_resistance(ticker, days))]
    if include_news:
        tasks.append(asyncio.create_task(get_news_sentiment_enhanced(ticker)))
    gathered = await asyncio.gather(*tasks, return_exceptions=True)
    extras: dict[str, Any] = {"support_resistance": _unwrap_gathered(gathered[0])}
    if include_news and len(gathered) > 1:
        extras["news"] = _unwrap_gathered(gathered[1])
    return extras


@trader_router.post("/analysis/batch")
async def batch_analysis(body: BatchAnalysisRequest) -> dict[str, Any]:
    """
    Analyse multiple tickers in one call.

    For each ticker runs technical analysis + support/resistance (+ optionally
    news sentiment) in parallel.  Returns all results keyed by ticker.

    This replaces the pattern of 3 separate MCP calls per ticker, eliminating
    ~1 second of MCP handshake overhead per call.
    """
    from maverick_mcp.api.routers.technical_enhanced import (
        get_full_technical_analysis_enhanced,
    )
    from maverick_mcp.validation.technical import TechnicalAnalysisRequest

    intraday = body.intraday_bars or {}
    known_fps = body.fingerprints or {}

    async def _analyse_one(ticker: str) -> tuple[str, dict[str, Any]]:
        async with _get_batch_semaphore():
            known_fp = known_fps.get(ticker)

            # Phase 6: when caller has a fingerprint, run technical first and
            # skip extras if the data is unchanged (saves ~5s per ticker).
            tech_task = get_full_technical_analysis_enhanced(
                TechnicalAnalysisRequest(ticker=ticker, days=body.days),
                today_bar=intraday.get(ticker),
                known_fingerprint=known_fp,
            )

            if known_fp is None:
                # No fingerprint — run technical + extras in parallel
                tech_result, extras = await asyncio.gather(
                    tech_task, _fetch_extras(ticker, body.days, body.include_news),
                )
                return ticker, {"technical": _unwrap_gathered(tech_result), **extras}

            # Has fingerprint — run technical first, skip extras if unchanged
            tech = _unwrap_gathered(await tech_task)
            if isinstance(tech, dict) and tech.get("status") == "unchanged":
                return ticker, {"technical": tech, "changed": False}

            extras = await _fetch_extras(ticker, body.days, body.include_news)
            return ticker, {"technical": tech, **extras}

    pairs = await asyncio.gather(
        *[_analyse_one(t.upper()) for t in body.tickers],
        return_exceptions=True,
    )

    results: dict[str, Any] = {}
    for item in pairs:
        if isinstance(item, BaseException):
            logger.error("Batch analysis error: %s", item)
            continue
        ticker, data = item
        results[ticker] = data

    return {
        "status": "success",
        "count": len(results),
        "results": results,
        "timestamp": datetime.now(UTC).isoformat(),
    }


# ---------------------------------------------------------------------------
# Intraday data refresh
# ---------------------------------------------------------------------------


@trader_router.post("/data/refresh-intraday")
async def refresh_intraday(body: IntradayRefreshRequest) -> dict[str, Any]:
    """Fetch and consolidate intraday bars for a list of tickers.

    Uses yfinance to get 15-minute (or other interval) bars for today,
    consolidates them into a synthetic "today" daily bar (OHLCV).

    The autonomous trader calls this before batch analysis so the
    analysis uses today's price instead of yesterday's close.
    """
    from maverick_mcp.providers.intraday import refresh_intraday_batch

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, refresh_intraday_batch, body.tickers, body.interval
    )
    return result

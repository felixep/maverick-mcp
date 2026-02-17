"""
Universe management router for MaverickMCP.

Tools for registering custom tickers, deactivating stocks,
and inspecting the screened universe.
"""

import asyncio
import logging
import re
from typing import Any

import yfinance as yf
from fastmcp import FastMCP

from maverick_mcp.data.models import Stock, get_db

logger = logging.getLogger(__name__)

management_router: FastMCP = FastMCP("Management")


def _normalize_ticker(ticker: str) -> str:
    """Normalize ticker symbol to uppercase and strip whitespace."""
    return ticker.strip().upper()


def _validate_ticker(ticker: str) -> tuple[bool, str | None]:
    """
    Validate ticker symbol format.

    Allows 1-10 characters: uppercase letters, digits, hyphens, and dots
    (e.g. BRK-B, BF.B are valid).
    """
    if not ticker or not ticker.strip():
        return False, "Ticker symbol cannot be empty"

    normalized = ticker.strip().upper()

    if not re.match(r"^[A-Z0-9.\-]{1,10}$", normalized):
        return (
            False,
            f"Invalid ticker symbol '{ticker}': use 1-10 characters "
            "(letters, digits, hyphens, and dots only)",
        )

    return True, None


async def register_ticker(
    ticker: str,
    company_name: str = "",
    sector: str = "",
    auto_enrich: bool = True,
    auto_refresh: bool = False,
) -> dict[str, Any]:
    """
    Register a custom ticker in the MaverickMCP screened universe.

    Adds the stock to the mcp_stocks table with is_active=True so it is
    included in daily bar refreshes and all future screening runs. Optionally
    enriches company metadata from yfinance and triggers an immediate
    screening refresh for the new ticker.

    Args:
        ticker: Stock ticker symbol (e.g. "BE", "NVDA", "BRK-B")
        company_name: Optional company name override (used only when auto_enrich=False)
        sector: Optional sector override (used only when auto_enrich=False)
        auto_enrich: If True (default), fetch company metadata from yfinance
        auto_refresh: If True, trigger a screening run for this ticker immediately
                      (runs in background, tool returns immediately). Default: False.

    Returns:
        Dict with status, ticker details, and whether a refresh was triggered.
    """
    is_valid, err = _validate_ticker(ticker)
    if not is_valid:
        return {"status": "error", "error": err}

    ticker = _normalize_ticker(ticker)
    db = next(get_db())
    try:
        existing = db.query(Stock).filter_by(ticker_symbol=ticker).first()

        # Already registered and active
        if existing and existing.is_active:
            return {
                "status": "already_registered",
                "ticker": ticker,
                "company_name": existing.company_name,
                "sector": existing.sector,
                "exchange": existing.exchange,
                "message": f"{ticker} is already in the active universe. "
                "No changes made.",
            }

        # Exists but was deactivated — reactivate it
        if existing and not existing.is_active:
            existing.is_active = True
            db.commit()
            return {
                "status": "reactivated",
                "ticker": ticker,
                "company_name": existing.company_name,
                "sector": existing.sector,
                "exchange": existing.exchange,
                "message": f"{ticker} has been reactivated. It will be included "
                "in the next daily bar refresh and screening run.",
            }

        # New ticker — optionally enrich from yfinance
        enriched: dict[str, Any] = {}
        enriched_ok = False
        if auto_enrich:
            try:
                info = yf.Ticker(ticker).info
                description = info.get("longBusinessSummary", "")
                if description and len(description) > 500:
                    description = description[:500] + "..."
                enriched = {
                    "company_name": info.get("longName") or company_name or ticker,
                    "sector": info.get("sector") or sector or "Unknown",
                    "industry": info.get("industry") or "Unknown",
                    "exchange": info.get("exchange", "NASDAQ"),
                    "country": info.get("country", "US"),
                    "currency": info.get("currency", "USD"),
                    "market_cap": info.get("marketCap"),
                    "shares_outstanding": info.get("sharesOutstanding"),
                    "description": description,
                }
                enriched_ok = True
            except Exception as e:
                logger.warning(f"yfinance enrichment failed for {ticker}: {e}")

        if not enriched_ok:
            enriched = {
                "company_name": company_name or ticker,
                "sector": sector or "Unknown",
                "industry": "Unknown",
                "exchange": "NASDAQ",
                "country": "US",
                "currency": "USD",
            }

        Stock.get_or_create(
            db,
            ticker_symbol=ticker,
            is_active=True,
            **enriched,
        )

        # Optionally trigger an immediate screening refresh in the background
        refresh_triggered = False
        if auto_refresh:
            try:
                from maverick_mcp.utils.screening_scheduler import ScreeningScheduler

                async def _do_refresh() -> None:
                    try:
                        scheduler = ScreeningScheduler()
                        await scheduler.run_screening(symbols=[ticker])
                        logger.info(f"Background screening refresh completed for {ticker}")
                    except Exception as exc:
                        logger.warning(
                            f"Background screening refresh failed for {ticker}: {exc}"
                        )

                asyncio.create_task(_do_refresh())
                refresh_triggered = True
            except Exception as e:
                logger.warning(f"Could not trigger screening refresh for {ticker}: {e}")

        return {
            "status": "success",
            "ticker": ticker,
            "company_name": enriched.get("company_name", ticker),
            "sector": enriched.get("sector", "Unknown"),
            "exchange": enriched.get("exchange", "NASDAQ"),
            "enriched": enriched_ok,
            "refresh_triggered": refresh_triggered,
            "message": (
                f"{ticker} has been added to the screened universe. "
                "It will appear in the next daily bar refresh (5:30 PM ET) and screening run. "
                + (
                    "A screening refresh has been triggered in the background."
                    if refresh_triggered
                    else "Set auto_refresh=True to trigger an immediate screening run."
                )
            ),
        }

    finally:
        db.close()


async def deactivate_ticker(
    ticker: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """
    Deactivate a ticker from the MaverickMCP screened universe.

    Sets is_active=False on the stock so it is excluded from daily bar
    refreshes and all future screening runs. The stock record is preserved
    and can be reactivated at any time via management_register_ticker.

    Args:
        ticker: Stock ticker symbol to deactivate (e.g. "BE")
        confirm: Must be True to execute the deactivation (safety guard).
                 Call with confirm=False first to see the confirmation prompt.

    Returns:
        Dict with status. If confirm=False, returns a confirmation prompt
        without making any changes.
    """
    is_valid, err = _validate_ticker(ticker)
    if not is_valid:
        return {"status": "error", "error": err}

    ticker = _normalize_ticker(ticker)

    if not confirm:
        return {
            "status": "requires_confirmation",
            "ticker": ticker,
            "message": (
                f"To deactivate {ticker}, call management_deactivate_ticker again "
                "with confirm=True. The stock record will be preserved but excluded "
                "from all future daily refreshes and screening runs. "
                "You can reactivate it at any time via management_register_ticker."
            ),
        }

    db = next(get_db())
    try:
        stock = db.query(Stock).filter_by(ticker_symbol=ticker).first()

        if not stock:
            return {
                "status": "error",
                "error": f"{ticker} was not found in the screened universe.",
            }

        if not stock.is_active:
            return {
                "status": "already_inactive",
                "ticker": ticker,
                "company_name": stock.company_name,
                "message": f"{ticker} is already inactive.",
            }

        stock.is_active = False
        db.commit()

        return {
            "status": "success",
            "ticker": ticker,
            "company_name": stock.company_name,
            "message": (
                f"{ticker} has been deactivated and will no longer appear in "
                "screening results or daily bar refreshes. "
                "Use management_register_ticker to reactivate it."
            ),
        }

    finally:
        db.close()


async def list_universe(
    active_only: bool = True,
    sector: str = "",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """
    List stocks in the MaverickMCP screened universe.

    Returns stocks from the mcp_stocks table, with optional filtering
    by active status and sector. Useful for verifying which tickers are
    included in daily bar refreshes and screening runs.

    Args:
        active_only: If True (default), only return active stocks
        sector: Optional sector filter — case-insensitive substring match
                (e.g. "Technology", "Health Care", "Industrials")
        limit: Maximum number of results to return (default: 100, max: 500)
        offset: Pagination offset for large universes (default: 0)

    Returns:
        Dict with total count and paginated list of stock records.
    """
    limit = min(limit, 500)

    db = next(get_db())
    try:
        query = db.query(Stock)

        if active_only:
            query = query.filter(Stock.is_active.is_(True))

        if sector:
            query = query.filter(Stock.sector.ilike(f"%{sector}%"))

        total = query.count()
        stocks = (
            query.order_by(Stock.ticker_symbol)
            .offset(offset)
            .limit(limit)
            .all()
        )

        return {
            "status": "success",
            "total": total,
            "limit": limit,
            "offset": offset,
            "active_only": active_only,
            "sector_filter": sector or None,
            "stocks": [
                {
                    "ticker": s.ticker_symbol,
                    "company_name": s.company_name,
                    "sector": s.sector,
                    "industry": s.industry,
                    "exchange": s.exchange,
                    "is_active": s.is_active,
                }
                for s in stocks
            ],
        }

    finally:
        db.close()

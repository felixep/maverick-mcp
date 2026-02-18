#!/usr/bin/env python3
"""
Extended universe seed script for MaverickMCP.

Expands the screened universe beyond S&P 500 by pulling four lists from
stockanalysis.com (no API key required):

  - NASDAQ 100  : https://stockanalysis.com/list/nasdaq-100-stocks/
  - Dow Jones   : https://stockanalysis.com/list/dow-jones-stocks/
  - Top Dividend : https://stockanalysis.com/list/top-rated-dividend-stocks/
  - Mid-Cap     : https://stockanalysis.com/list/mid-cap-stocks/

Usage:
    python scripts/seed_extended_universe.py
"""

import logging
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# noqa: E402 - imports must come after sys.path modification
import pandas as pd  # noqa: E402
import yfinance as yf  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from maverick_mcp.data.models import Stock  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("maverick_mcp.seed_extended")

# Built-in fallback for NASDAQ 100 (used only if stockanalysis.com is unreachable)
NASDAQ100_FALLBACK = [
    "MSFT", "AAPL", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "TSLA", "AVGO", "COST",
    "ASML", "NFLX", "AMD", "AZN", "ADBE", "QCOM", "INTC", "INTU", "TXN", "CMCSA",
    "AMAT", "MU", "ISRG", "BKNG", "SBUX", "GILD", "MDLZ", "ADI", "REGN", "LRCX",
]

# Stockanalysis.com list definitions
LISTS = [
    ("NASDAQ 100",       "https://stockanalysis.com/list/nasdaq-100-stocks/",         20),
    ("Dow Jones",        "https://stockanalysis.com/list/dow-jones-stocks/",           20),
    ("Top Dividend",     "https://stockanalysis.com/list/top-rated-dividend-stocks/",  20),
    ("Mid-Cap",          "https://stockanalysis.com/list/mid-cap-stocks/",            200),
]


def get_database_url() -> str:
    """Get the database URL from environment or use default."""
    return os.getenv("DATABASE_URL") or "sqlite:///maverick_mcp.db"


def fetch_stockanalysis_symbols(name: str, url: str, min_expected: int) -> list[str]:
    """
    Fetch ticker symbols from a stockanalysis.com list page.

    Uses the same embedded SvelteKit JSON extraction pattern as load_alpaca_data.py.
    Returns [] if the page is unavailable or returns too few symbols.
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as response:
            html = response.read().decode("utf-8")

        raw = re.findall(r'["\']?s["\']?\s*:\s*"([A-Z]{1,5}(?:\.[A-Z])?)"', html)
        seen: set[str] = set()
        symbols: list[str] = []
        for s in raw:
            if s not in seen:
                seen.add(s)
                symbols.append(s)

        if len(symbols) < min_expected:
            logger.warning(
                f"{name}: only {len(symbols)} symbols returned (expected ≥{min_expected})"
            )
            return []

        logger.info(f"{name}: fetched {len(symbols)} symbols from stockanalysis.com")
        return symbols

    except Exception as e:
        logger.warning(f"{name}: could not fetch from stockanalysis.com — {e}")
        return []


def enrich_stock_data(symbol: str) -> dict:
    """Enrich stock metadata from yfinance."""
    try:
        info = yf.Ticker(symbol).info
        description = info.get("longBusinessSummary", "")
        if description and len(description) > 500:
            description = description[:500] + "..."
        return {
            "market_cap": info.get("marketCap"),
            "shares_outstanding": info.get("sharesOutstanding"),
            "description": description,
            "country": info.get("country", "US"),
            "currency": info.get("currency", "USD"),
            "exchange": info.get("exchange", "NASDAQ"),
            "industry": info.get("industry", ""),
            "sector": info.get("sector", ""),
        }
    except Exception as e:
        logger.warning(f"yfinance enrichment failed for {symbol}: {e}")
        return {}


def create_stocks(session, symbols: list[str]) -> tuple[int, int]:
    """
    Add tickers to the DB using Stock.get_or_create (duplicate-safe).

    Returns:
        Tuple of (added_count, skipped_count)
    """
    added = 0
    skipped = 0
    batch_size = 10

    for symbol in symbols:
        symbol = symbol.strip().upper()
        if not symbol:
            continue

        # Skip if already in DB
        if session.query(Stock).filter_by(ticker_symbol=symbol).first():
            skipped += 1
            continue

        try:
            # Rate-limit yfinance calls
            if added > 0 and added % batch_size == 0:
                logger.info(f"Processed {added} new stocks so far, pausing 2s...")
                time.sleep(2)

            enriched = enrich_stock_data(symbol)

            Stock.get_or_create(
                session,
                ticker_symbol=symbol,
                company_name=enriched.get("sector", ""),  # filled by yfinance
                sector=enriched.get("sector") or "Unknown",
                industry=enriched.get("industry") or "Unknown",
                description=enriched.get("description") or f"{symbol} - Extended universe",
                exchange=enriched.get("exchange", "NASDAQ"),
                country=enriched.get("country", "US"),
                currency=enriched.get("currency", "USD"),
                market_cap=enriched.get("market_cap"),
                shares_outstanding=enriched.get("shares_outstanding"),
                is_active=True,
            )
            added += 1
            logger.info(f"✓ Added {symbol}")

        except Exception as e:
            logger.error(f"✗ Error adding {symbol}: {e}")
            continue

    session.commit()
    return added, skipped


def main() -> bool:
    """Main entry point."""
    logger.info("🚀 Starting extended universe seeding...")

    database_url = get_database_url()
    logger.info(f"Database: {database_url}")

    # Collect symbols from all lists
    all_symbols: list[str] = []
    for name, url, min_expected in LISTS:
        symbols = fetch_stockanalysis_symbols(name, url, min_expected)
        if not symbols and name == "NASDAQ 100":
            logger.info("Using built-in NASDAQ 100 fallback list")
            symbols = NASDAQ100_FALLBACK
        all_symbols.extend(symbols)
        logger.info(f"  {name}: {len(symbols)} symbols")

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_symbols: list[str] = []
    for s in all_symbols:
        if s not in seen:
            seen.add(s)
            unique_symbols.append(s)

    logger.info(
        f"Combined: {len(unique_symbols)} unique tickers from {len(LISTS)} lists "
        f"(before DB dedup)"
    )

    engine = create_engine(database_url, echo=False)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as session:
        try:
            added, skipped = create_stocks(session, unique_symbols)

            total_active = session.query(Stock).filter_by(is_active=True).count()

            logger.info("")
            logger.info("=== Extended Universe Seeding Complete ===")
            logger.info(f"✅ New tickers added:               {added}")
            logger.info(f"⏭  Already in DB (skipped):        {skipped}")
            logger.info(f"📊 Total active stocks in universe:  {total_active}")
            logger.info("")

            if total_active > 0:
                sector_counts = session.execute(
                    text("""
                        SELECT sector, COUNT(*) as count
                        FROM mcp_stocks
                        WHERE is_active = true AND sector IS NOT NULL
                        GROUP BY sector
                        ORDER BY count DESC
                        LIMIT 10
                    """)
                ).fetchall()
                if sector_counts:
                    logger.info("Top sectors in active universe:")
                    for sec, count in sector_counts:
                        logger.info(f"   {sec}: {count}")

            logger.info("")
            logger.info("Next steps:")
            logger.info(
                "  - Daily bar refresh at 5:30 PM ET will automatically include new stocks"
            )
            logger.info(
                "  - To refresh screening data immediately, call the 'screening_refresh_now' MCP tool"
            )
            return True

        except Exception as e:
            logger.error(f"Seeding failed: {e}")
            session.rollback()
            raise


if __name__ == "__main__":
    try:
        success = main()
        if not success:
            sys.exit(1)
    except KeyboardInterrupt:
        logger.info("\n⏹️  Seeding interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
        sys.exit(1)

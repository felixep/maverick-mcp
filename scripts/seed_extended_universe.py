#!/usr/bin/env python3
"""
Extended universe seed script for MaverickMCP.

Expands the screened universe beyond S&P 500 by adding NASDAQ 100
and S&P 400 Mid-Cap constituents using the Financial Modeling Prep API.

Usage:
    FMP_API_KEY=yourkey python scripts/seed_extended_universe.py

Get a free API key at https://financialmodelingprep.com
"""

import logging
import os
import sys
import time
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# noqa: E402 - imports must come after sys.path modification
import pandas as pd  # noqa: E402
import requests  # noqa: E402
import yfinance as yf  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from maverick_mcp.data.models import Stock  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("maverick_mcp.seed_extended")

FMP_BASE_URL = "https://financialmodelingprep.com/api/v3"

# Fallback list if FMP returns no data for NASDAQ 100
NASDAQ100_FALLBACK = [
    ("MSFT", "Microsoft Corporation", "Information Technology", "Software"),
    ("AAPL", "Apple Inc.", "Information Technology", "Technology Hardware, Storage & Peripherals"),
    ("NVDA", "NVIDIA Corporation", "Information Technology", "Semiconductors & Semiconductor Equipment"),
    ("AMZN", "Amazon.com Inc.", "Consumer Discretionary", "Internet & Direct Marketing Retail"),
    ("GOOGL", "Alphabet Inc.", "Communication Services", "Interactive Media & Services"),
    ("GOOG", "Alphabet Inc. Class C", "Communication Services", "Interactive Media & Services"),
    ("META", "Meta Platforms Inc.", "Communication Services", "Interactive Media & Services"),
    ("TSLA", "Tesla Inc.", "Consumer Discretionary", "Automobiles"),
    ("AVGO", "Broadcom Inc.", "Information Technology", "Semiconductors & Semiconductor Equipment"),
    ("COST", "Costco Wholesale Corp.", "Consumer Staples", "Food & Staples Retailing"),
    ("ASML", "ASML Holding NV", "Information Technology", "Semiconductors & Semiconductor Equipment"),
    ("NFLX", "Netflix Inc.", "Communication Services", "Entertainment"),
    ("AMD", "Advanced Micro Devices Inc.", "Information Technology", "Semiconductors & Semiconductor Equipment"),
    ("AZN", "AstraZeneca PLC", "Health Care", "Pharmaceuticals"),
    ("ADBE", "Adobe Inc.", "Information Technology", "Software"),
    ("QCOM", "QUALCOMM Inc.", "Information Technology", "Semiconductors & Semiconductor Equipment"),
    ("INTC", "Intel Corp.", "Information Technology", "Semiconductors & Semiconductor Equipment"),
    ("INTU", "Intuit Inc.", "Information Technology", "Software"),
    ("TXN", "Texas Instruments Inc.", "Information Technology", "Semiconductors & Semiconductor Equipment"),
    ("CMCSA", "Comcast Corp.", "Communication Services", "Cable & Satellite"),
    ("AMAT", "Applied Materials Inc.", "Information Technology", "Semiconductors & Semiconductor Equipment"),
    ("MU", "Micron Technology Inc.", "Information Technology", "Semiconductors & Semiconductor Equipment"),
    ("ISRG", "Intuitive Surgical Inc.", "Health Care", "Health Care Equipment & Supplies"),
    ("BKNG", "Booking Holdings Inc.", "Consumer Discretionary", "Internet & Direct Marketing Retail"),
    ("SBUX", "Starbucks Corp.", "Consumer Discretionary", "Restaurants"),
    ("GILD", "Gilead Sciences Inc.", "Health Care", "Biotechnology"),
    ("MDLZ", "Mondelez International Inc.", "Consumer Staples", "Food Products"),
    ("ADI", "Analog Devices Inc.", "Information Technology", "Semiconductors & Semiconductor Equipment"),
    ("REGN", "Regeneron Pharmaceuticals", "Health Care", "Biotechnology"),
    ("LRCX", "Lam Research Corp.", "Information Technology", "Semiconductors & Semiconductor Equipment"),
]

# Fallback list if FMP returns no data or S&P 400 endpoint requires paid tier
SP400_FALLBACK = [
    ("BE", "Bloom Energy Corp.", "Industrials", "Electrical Components & Equipment"),
    ("SFM", "Sprouts Farmers Market Inc.", "Consumer Staples", "Food & Staples Retailing"),
    ("TREX", "Trex Company Inc.", "Industrials", "Building Products"),
    ("CHDN", "Churchill Downs Inc.", "Consumer Discretionary", "Casinos & Gaming"),
    ("ITT", "ITT Inc.", "Industrials", "Industrial Machinery"),
    ("CIVI", "Civitas Resources Inc.", "Energy", "Oil, Gas & Consumable Fuels"),
    ("UFPI", "UFP Industries Inc.", "Industrials", "Forest Products"),
    ("IRDM", "Iridium Communications Inc.", "Communication Services", "Wireless Telecommunication Services"),
    ("LGIH", "LGI Homes Inc.", "Consumer Discretionary", "Homebuilding"),
    ("MMSI", "Merit Medical Systems Inc.", "Health Care", "Health Care Equipment & Supplies"),
    ("MRCY", "Mercury Systems Inc.", "Industrials", "Aerospace & Defense"),
    ("NVEE", "NV5 Global Inc.", "Industrials", "Engineering & Construction Services"),
    ("FORM", "FormFactor Inc.", "Information Technology", "Semiconductors & Semiconductor Equipment"),
    ("HAFC", "Hanmi Financial Corp.", "Financials", "Banks"),
    ("FHB", "First Hawaiian Inc.", "Financials", "Banks"),
    ("NBTB", "NBT Bancorp Inc.", "Financials", "Banks"),
    ("OFG", "OFG Bancorp", "Financials", "Banks"),
    ("NSIT", "Insight Direct Worldwide Inc.", "Information Technology", "IT Services"),
    ("PLUS", "ePlus Inc.", "Information Technology", "IT Services"),
    ("PTCT", "PTC Therapeutics Inc.", "Health Care", "Biotechnology"),
    ("LNTH", "Lantheus Holdings Inc.", "Health Care", "Health Care Supplies"),
    ("VIRT", "Virtu Financial Inc.", "Financials", "Capital Markets"),
    ("WMS", "Advanced Drainage Systems Inc.", "Industrials", "Building Products"),
    ("MGEE", "MGE Energy Inc.", "Utilities", "Electric Utilities"),
    ("MSEX", "Middlesex Water Company", "Utilities", "Water Utilities"),
    ("PIPR", "Piper Sandler Companies", "Financials", "Capital Markets"),
    ("RMBS", "Rambus Inc.", "Information Technology", "Semiconductors & Semiconductor Equipment"),
    ("GOLF", "Acushnet Holdings Corp.", "Consumer Discretionary", "Leisure Products"),
    ("CRVL", "CorVel Corp.", "Health Care", "Health Care Services"),
    ("UFPT", "UFP Technologies Inc.", "Materials", "Containers & Packaging"),
]


def get_database_url() -> str:
    """Get the database URL from environment or use default."""
    return os.getenv("DATABASE_URL") or "sqlite:///maverick_mcp.db"


def fetch_fmp_constituents(endpoint: str, api_key: str) -> list[dict]:
    """
    Fetch index constituents from FMP API.

    Returns list of constituent dicts on success, empty list on error.
    """
    url = f"{FMP_BASE_URL}/{endpoint}?apikey={api_key}"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and len(data) > 0:
            return data
        # FMP returns {"Error Message": "..."} for invalid keys or paid endpoints
        if isinstance(data, dict) and "Error Message" in data:
            logger.warning(f"FMP error for {endpoint}: {data['Error Message']}")
        return []
    except Exception as e:
        logger.warning(f"FMP request failed for {endpoint}: {e}")
        return []


def fetch_nasdaq100_list(api_key: str) -> pd.DataFrame:
    """Fetch NASDAQ 100 constituents from FMP, with fallback."""
    logger.info("Fetching NASDAQ 100 list from FMP...")
    data = fetch_fmp_constituents("nasdaq_constituent", api_key)

    if data:
        rows = []
        for item in data:
            symbol = str(item.get("symbol", "")).strip().replace(".", "-")
            if symbol:
                rows.append({
                    "symbol": symbol,
                    "company": item.get("name", ""),
                    "gics_sector": item.get("sector", ""),
                    "gics_sub_industry": item.get("subSector", ""),
                })
        df = pd.DataFrame(rows)
        logger.info(f"Fetched {len(df)} NASDAQ 100 constituents from FMP")
        return df

    logger.warning("FMP returned no NASDAQ 100 data — using built-in fallback list")
    return pd.DataFrame(
        NASDAQ100_FALLBACK,
        columns=["symbol", "company", "gics_sector", "gics_sub_industry"],
    )


def fetch_sp400_list(api_key: str) -> pd.DataFrame:
    """Fetch S&P 400 Mid-Cap constituents from FMP, with fallback."""
    logger.info("Fetching S&P 400 Mid-Cap list from FMP...")
    data = fetch_fmp_constituents("sp400_constituent", api_key)

    if data:
        rows = []
        for item in data:
            symbol = str(item.get("symbol", "")).strip().replace(".", "-")
            if symbol:
                rows.append({
                    "symbol": symbol,
                    "company": item.get("name", ""),
                    "gics_sector": item.get("sector", ""),
                    "gics_sub_industry": item.get("subSector", ""),
                })
        df = pd.DataFrame(rows)
        logger.info(f"Fetched {len(df)} S&P 400 Mid-Cap constituents from FMP")
        return df

    logger.warning(
        "FMP returned no S&P 400 data (may require paid tier) — using built-in fallback list"
    )
    return pd.DataFrame(
        SP400_FALLBACK,
        columns=["symbol", "company", "gics_sector", "gics_sub_industry"],
    )


def enrich_stock_data(symbol: str) -> dict:
    """Enrich stock data with additional information from yfinance."""
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info

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
        logger.warning(f"Failed to enrich data for {symbol}: {e}")
        return {}


def create_stocks(session, df: pd.DataFrame) -> tuple[int, int]:
    """
    Add stocks to the DB using Stock.get_or_create (duplicate-safe).

    Returns:
        Tuple of (added_count, skipped_count)
    """
    added = 0
    skipped = 0
    batch_size = 10

    for i, row in df.iterrows():
        symbol = str(row["symbol"]).strip()
        if not symbol:
            continue

        company = str(row.get("company", "")).strip()
        sector = str(row.get("gics_sector", "")).strip()
        industry = str(row.get("gics_sub_industry", "")).strip()

        # Check if already exists — skip without making an API call
        existing = session.query(Stock).filter_by(ticker_symbol=symbol.upper()).first()
        if existing:
            skipped += 1
            continue

        try:
            # Rate limiting — pause every batch to be friendly to yfinance
            if added > 0 and added % batch_size == 0:
                logger.info(f"Processed {added} new stocks so far, pausing 2s...")
                time.sleep(2)

            enriched = enrich_stock_data(symbol)

            Stock.get_or_create(
                session,
                ticker_symbol=symbol,
                company_name=company,
                sector=enriched.get("sector") or sector or "Unknown",
                industry=enriched.get("industry") or industry or "Unknown",
                description=enriched.get("description") or f"{company} - Extended universe",
                exchange=enriched.get("exchange", "NASDAQ"),
                country=enriched.get("country", "US"),
                currency=enriched.get("currency", "USD"),
                market_cap=enriched.get("market_cap"),
                shares_outstanding=enriched.get("shares_outstanding"),
                is_active=True,
            )
            added += 1
            logger.info(f"✓ Added {symbol}: {company}")

        except Exception as e:
            logger.error(f"✗ Error adding {symbol}: {e}")
            continue

    session.commit()
    return added, skipped


def main() -> bool:
    """Main entry point."""
    logger.info("🚀 Starting extended universe seeding (NASDAQ 100 + S&P 400 Mid-Cap)...")

    api_key = os.getenv("FMP_API_KEY", "").strip()
    if not api_key:
        logger.error("FMP_API_KEY environment variable is not set.")
        logger.error("Get a free key at https://financialmodelingprep.com")
        logger.error(
            "Usage: FMP_API_KEY=yourkey python scripts/seed_extended_universe.py"
        )
        return False

    database_url = get_database_url()
    logger.info(f"Database: {database_url}")

    engine = create_engine(database_url, echo=False)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as session:
        try:
            # Fetch both constituent lists
            nasdaq_df = fetch_nasdaq100_list(api_key)
            sp400_df = fetch_sp400_list(api_key)

            # Combine and deduplicate by symbol
            combined = pd.concat([nasdaq_df, sp400_df], ignore_index=True)
            combined = combined.drop_duplicates(subset=["symbol"])
            logger.info(
                f"Combined universe: {len(combined)} unique tickers to process "
                f"({len(nasdaq_df)} NASDAQ 100 + {len(sp400_df)} S&P 400, deduped)"
            )

            # Add to DB
            added, skipped = create_stocks(session, combined)

            # Summary
            total_active = session.query(Stock).filter_by(is_active=True).count()

            # Sector breakdown of new additions
            logger.info("")
            logger.info("=== Extended Universe Seeding Complete ===")
            logger.info(f"✅ New tickers added:              {added}")
            logger.info(f"⏭  Already in DB (skipped):       {skipped}")
            logger.info(f"📊 Total active stocks in universe: {total_active}")
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

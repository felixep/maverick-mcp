#!/usr/bin/env python3
"""
Alpaca Data Loader for MaverickMCP

Loads market data from Alpaca API into the self-contained MaverickMCP database.
Supports batch loading (multiple symbols per request), progress tracking,
technical indicator calculation, and screening.

This is the primary data loader; Tiingo (load_tiingo_data.py) serves as fallback.
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from tqdm import tqdm

# Add parent directory to path for imports
sys.path.append(str(Path(__file__).parent.parent))

from maverick_mcp.data.models import (
    MaverickBearStocks,
    MaverickStocks,
    PriceCache,
    Stock,
    SupplyDemandBreakoutStocks,
    bulk_insert_price_data,
    bulk_insert_screening_data,
    get_latest_price_dates,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Configuration
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"

DEFAULT_CHECKPOINT_FILE = os.getenv(
    "ALPACA_CHECKPOINT_FILE", "alpaca_load_progress.json"
)

# Batch size for multi-symbol requests (avoid excessive memory usage)
BATCH_SIZE = int(os.getenv("ALPACA_BATCH_SIZE", "100"))


class AlpacaDataLoader:
    """Handles loading data from Alpaca API into MaverickMCP database.

    Key advantage over Tiingo: fetches multiple symbols in a single paginated
    request (200 req/min free tier vs Tiingo's ~50/hour).
    """

    def __init__(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        db_url: str | None = None,
        paper: bool | None = None,
        checkpoint_file: str | None = None,
    ):
        self.api_key = api_key or ALPACA_API_KEY
        self.secret_key = secret_key or ALPACA_SECRET_KEY
        if not self.api_key or not self.secret_key:
            raise ValueError(
                "Alpaca API key and secret key required. "
                "Set ALPACA_API_KEY and ALPACA_SECRET_KEY env vars."
            )

        self.paper = paper if paper is not None else ALPACA_PAPER

        # Database
        self.db_url = db_url or os.getenv("DATABASE_URL")
        if not self.db_url:
            raise ValueError("DATABASE_URL env var required.")
        self.engine = create_engine(self.db_url)
        self.SessionLocal = sessionmaker(bind=self.engine)

        # Alpaca clients (lazy-imported to avoid import errors if not installed)
        self._data_client = None
        self._trading_client = None

        # Checkpoint
        self.checkpoint_file = checkpoint_file or DEFAULT_CHECKPOINT_FILE
        self.checkpoint_data = self._load_checkpoint()

        # Pre-loaded latest dates per symbol (populated by load_symbols)
        self._latest_dates: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Alpaca SDK clients
    # ------------------------------------------------------------------

    @property
    def data_client(self):
        if self._data_client is None:
            from alpaca.data.historical import StockHistoricalDataClient

            self._data_client = StockHistoricalDataClient(
                api_key=self.api_key, secret_key=self.secret_key
            )
        return self._data_client

    @property
    def trading_client(self):
        if self._trading_client is None:
            from alpaca.trading.client import TradingClient

            self._trading_client = TradingClient(
                api_key=self.api_key,
                secret_key=self.secret_key,
                paper=self.paper,
            )
        return self._trading_client

    # ------------------------------------------------------------------
    # Checkpoint management (same approach as TiingoDataLoader)
    # ------------------------------------------------------------------

    def _load_checkpoint(self) -> dict:
        if Path(self.checkpoint_file).exists():
            try:
                with open(self.checkpoint_file) as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not load checkpoint: {e}")
        return {"completed_symbols": [], "last_symbol": None}

    def save_checkpoint(self, symbol: str):
        self.checkpoint_data["completed_symbols"].append(symbol)
        self.checkpoint_data["last_symbol"] = symbol
        self.checkpoint_data["timestamp"] = datetime.now().isoformat()
        try:
            with open(self.checkpoint_file, "w") as f:
                json.dump(self.checkpoint_data, f, indent=2)
        except Exception as e:
            logger.error(f"Could not save checkpoint: {e}")

    # ------------------------------------------------------------------
    # Data fetching (batch)
    # ------------------------------------------------------------------

    def fetch_bars_batch(
        self,
        symbols: list[str],
        start_date: str,
        end_date: str,
    ) -> dict[str, pd.DataFrame]:
        """Fetch daily bars for multiple symbols in one paginated SDK call.

        Returns a dict mapping symbol -> DataFrame with columns:
        Open, High, Low, Close, Volume (capitalised to match pipeline).
        """
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")

        request = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            start=start_dt,
            end=end_dt,
        )

        logger.info(
            f"Fetching bars for {len(symbols)} symbols "
            f"({start_date} to {end_date}) ..."
        )
        bars = self.data_client.get_stock_bars(request)

        # Convert to DataFrame — multi-index (symbol, timestamp)
        df = bars.df
        if df.empty:
            return {}

        result: dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            try:
                if symbol not in df.index.get_level_values(0):
                    continue
                sym_df = df.loc[symbol].copy()

                # Capitalise column names to match existing pipeline
                sym_df = sym_df.rename(
                    columns={
                        "open": "Open",
                        "high": "High",
                        "low": "Low",
                        "close": "Close",
                        "volume": "Volume",
                    }
                )

                # Convert timestamp index to date objects (no timezone)
                sym_df.index = sym_df.index.date

                # Keep only OHLCV columns needed by bulk_insert_price_data
                keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in sym_df.columns]
                sym_df = sym_df[keep]

                if not sym_df.empty:
                    result[symbol] = sym_df
            except Exception as e:
                logger.warning(f"Error processing bars for {symbol}: {e}")

        logger.info(f"Got bars for {len(result)}/{len(symbols)} symbols")
        return result

    # ------------------------------------------------------------------
    # Technical indicators (same logic as TiingoDataLoader)
    # ------------------------------------------------------------------

    def calculate_technical_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate technical indicators for the data."""
        if df.empty or len(df) < 200:
            return df

        try:
            # Moving averages
            df["SMA_20"] = df["Close"].rolling(window=20).mean()
            df["SMA_50"] = df["Close"].rolling(window=50).mean()
            df["SMA_150"] = df["Close"].rolling(window=150).mean()
            df["SMA_200"] = df["Close"].rolling(window=200).mean()
            df["EMA_21"] = df["Close"].ewm(span=21, adjust=False).mean()

            # RSI
            delta = df["Close"].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            df["RSI"] = 100 - (100 / (1 + rs))

            # MACD
            exp1 = df["Close"].ewm(span=12, adjust=False).mean()
            exp2 = df["Close"].ewm(span=26, adjust=False).mean()
            df["MACD"] = exp1 - exp2
            df["MACD_Signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
            df["MACD_Histogram"] = df["MACD"] - df["MACD_Signal"]

            # ATR
            high_low = df["High"] - df["Low"]
            high_close = np.abs(df["High"] - df["Close"].shift())
            low_close = np.abs(df["Low"] - df["Close"].shift())
            ranges = pd.concat([high_low, high_close, low_close], axis=1)
            true_range = np.max(ranges, axis=1)
            df["ATR"] = true_range.rolling(14).mean()

            # ADR (Average Daily Range) as percentage
            df["ADR_PCT"] = (
                ((df["High"] - df["Low"]) / df["Close"] * 100).rolling(20).mean()
            )

            # Volume indicators
            df["Volume_SMA_30"] = df["Volume"].rolling(window=30).mean()
            df["Volume_Ratio"] = df["Volume"] / df["Volume_SMA_30"]

            # Momentum Score (simplified)
            returns = df["Close"].pct_change(periods=252)
            df["Momentum_Score"] = returns.rank(pct=True) * 100

        except Exception as e:
            logger.error(f"Error calculating indicators: {e}")

        return df

    # ------------------------------------------------------------------
    # Screening algorithms (same logic as TiingoDataLoader)
    # ------------------------------------------------------------------

    def run_maverick_screening(self, df: pd.DataFrame, symbol: str) -> dict | None:
        if df.empty or len(df) < 200:
            return None
        try:
            latest = df.iloc[-1]
            score = 0
            if latest["Close"] > latest.get("EMA_21", 0):
                score += 25
            if latest.get("EMA_21", 0) > latest.get("SMA_50", 0):
                score += 25
            if latest.get("SMA_50", 0) > latest.get("SMA_200", 0):
                score += 25
            if latest.get("Momentum_Score", 0) > 70:
                score += 25

            if score >= 75:
                return {
                    "stock": symbol,
                    "close": float(latest["Close"]),
                    "volume": int(latest["Volume"]),
                    "momentum_score": float(latest.get("Momentum_Score", 0)),
                    "combined_score": score,
                    "adr_pct": float(latest.get("ADR_PCT", 0)),
                    "atr": float(latest.get("ATR", 0)),
                    "ema_21": float(latest.get("EMA_21", 0)),
                    "sma_50": float(latest.get("SMA_50", 0)),
                    "sma_150": float(latest.get("SMA_150", 0)),
                    "sma_200": float(latest.get("SMA_200", 0)),
                }
        except Exception as e:
            logger.error(f"Error in Maverick screening for {symbol}: {e}")
        return None

    def run_bear_screening(self, df: pd.DataFrame, symbol: str) -> dict | None:
        if df.empty or len(df) < 200:
            return None
        try:
            latest = df.iloc[-1]
            score = 0
            if latest["Close"] < latest.get("EMA_21", float("inf")):
                score += 25
            if latest.get("EMA_21", float("inf")) < latest.get("SMA_50", float("inf")):
                score += 25
            if latest.get("Momentum_Score", 100) < 30:
                score += 25
            if latest.get("MACD", 0) < 0:
                score += 25

            if score >= 75:
                return {
                    "stock": symbol,
                    "close": float(latest["Close"]),
                    "volume": int(latest["Volume"]),
                    "momentum_score": float(latest.get("Momentum_Score", 0)),
                    "score": score,
                    "rsi_14": float(latest.get("RSI", 0)),
                    "macd": float(latest.get("MACD", 0)),
                    "macd_signal": float(latest.get("MACD_Signal", 0)),
                    "macd_histogram": float(latest.get("MACD_Histogram", 0)),
                    "adr_pct": float(latest.get("ADR_PCT", 0)),
                    "atr": float(latest.get("ATR", 0)),
                    "ema_21": float(latest.get("EMA_21", 0)),
                    "sma_50": float(latest.get("SMA_50", 0)),
                    "sma_200": float(latest.get("SMA_200", 0)),
                }
        except Exception as e:
            logger.error(f"Error in Bear screening for {symbol}: {e}")
        return None

    def run_supply_demand_screening(self, df: pd.DataFrame, symbol: str) -> dict | None:
        if df.empty or len(df) < 200:
            return None
        try:
            latest = df.iloc[-1]
            close = latest["Close"]
            sma_50 = latest.get("SMA_50", 0)
            sma_150 = latest.get("SMA_150", 0)
            sma_200 = latest.get("SMA_200", 0)

            price_above_all = close > sma_50 > sma_150 > sma_200
            strong_momentum = latest.get("Momentum_Score", 0) > 80
            volume_confirmation = latest.get("Volume_Ratio", 0) > 1.2

            if price_above_all and strong_momentum and volume_confirmation:
                return {
                    "stock": symbol,
                    "close": float(close),
                    "volume": int(latest["Volume"]),
                    "momentum_score": float(latest.get("Momentum_Score", 0)),
                    "adr_pct": float(latest.get("ADR_PCT", 0)),
                    "atr": float(latest.get("ATR", 0)),
                    "ema_21": float(latest.get("EMA_21", 0)),
                    "sma_50": float(sma_50),
                    "sma_150": float(sma_150),
                    "sma_200": float(sma_200),
                    "avg_volume_30d": float(latest.get("Volume_SMA_30", 0)),
                }
        except Exception as e:
            logger.error(f"Error in Supply/Demand screening for {symbol}: {e}")
        return None

    # ------------------------------------------------------------------
    # DB helpers (same logic as TiingoDataLoader)
    # ------------------------------------------------------------------

    def _load_price_df_from_db(self, symbol: str) -> pd.DataFrame:
        """Load full price history from DB with capitalised column names."""
        with self.SessionLocal() as db_session:
            df = PriceCache.get_price_data(db_session, symbol, start_date="2000-01-01")
        if df.empty:
            return df
        df = df.rename(columns={
            "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume",
        })
        df = df.drop(columns=["symbol"], errors="ignore")
        return df

    def _run_screening_on_df(self, df: pd.DataFrame, symbol: str) -> dict:
        """Calculate indicators and run all three screening algorithms."""
        if df.empty or len(df) < 200:
            return {}
        df = self.calculate_technical_indicators(df)
        results: dict = {}
        mav = self.run_maverick_screening(df, symbol)
        if mav:
            results["maverick"] = mav
        bear = self.run_bear_screening(df, symbol)
        if bear:
            results["bear"] = bear
        sd = self.run_supply_demand_screening(df, symbol)
        if sd:
            results["supply_demand"] = sd
        return results

    # ------------------------------------------------------------------
    # Symbol processing
    # ------------------------------------------------------------------

    def process_symbol(
        self,
        symbol: str,
        bars_df: pd.DataFrame | None,
        run_screening: bool = True,
    ) -> dict:
        """Process one symbol: store bars, run screening.

        Args:
            symbol: Ticker symbol
            bars_df: Pre-fetched bars DataFrame (None if up-to-date)
            run_screening: Whether to run screening algorithms

        Returns:
            Screening results dict (may be empty)
        """
        # Store new bars in DB
        if bars_df is not None and not bars_df.empty:
            with self.SessionLocal() as db_session:
                Stock.get_or_create(db_session, symbol)
                count = bulk_insert_price_data(db_session, symbol, bars_df)
                if count > 0:
                    logger.debug(f"Inserted {count} records for {symbol}")

        # Run screening on full DB history
        screening_results: dict = {}
        if run_screening:
            full_df = self._load_price_df_from_db(symbol)
            screening_results = self._run_screening_on_df(full_df, symbol)

        self.save_checkpoint(symbol)
        return screening_results

    # ------------------------------------------------------------------
    # Main loading orchestration
    # ------------------------------------------------------------------

    def load_symbols(
        self,
        symbols: list[str],
        start_date: str,
        end_date: str,
        run_screening: bool = True,
    ):
        """Load data for multiple symbols using batch Alpaca API calls.

        1. Pre-load latest dates from DB
        2. Split symbols: up-to-date / need-fetch
        3. Batch fetch from Alpaca in groups of BATCH_SIZE
        4. Process each symbol (store + screen)
        5. Bulk insert screening results
        """
        logger.info(
            f"Loading data for {len(symbols)} symbols from {start_date} to {end_date}"
        )

        # Pre-load existing data dates
        with self.SessionLocal() as db_session:
            self._latest_dates = get_latest_price_dates(db_session, symbols)

        end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()

        # Classify symbols
        up_to_date = []
        need_fetch = []
        for s in symbols:
            if s in self.checkpoint_data.get("completed_symbols", []):
                continue  # already processed this run
            latest = self._latest_dates.get(s)
            if latest and (end_dt - latest).days <= 3:
                up_to_date.append(s)
            else:
                need_fetch.append(s)

        already_done = len(symbols) - len(up_to_date) - len(need_fetch)
        logger.info(
            f"Data check: {len(up_to_date)} up-to-date, "
            f"{len(need_fetch)} need API calls, "
            f"{already_done} already processed (checkpoint)"
        )

        screening_results: dict[str, list] = {
            "maverick": [], "bear": [], "supply_demand": [],
        }

        # --- Process up-to-date symbols (screening only, no API call) ---
        if up_to_date and run_screening:
            logger.info(f"Running screening on {len(up_to_date)} up-to-date symbols ...")
            for symbol in tqdm(up_to_date, desc="Screening (cached)"):
                results = self.process_symbol(symbol, bars_df=None, run_screening=True)
                for k, v in results.items():
                    screening_results[k].append(v)

        # --- Batch fetch symbols that need data ---
        if need_fetch:
            # Process in batches to control memory
            for i in range(0, len(need_fetch), BATCH_SIZE):
                batch = need_fetch[i : i + BATCH_SIZE]
                batch_num = i // BATCH_SIZE + 1
                total_batches = (len(need_fetch) + BATCH_SIZE - 1) // BATCH_SIZE
                logger.info(
                    f"Batch {batch_num}/{total_batches}: "
                    f"fetching {len(batch)} symbols from Alpaca ..."
                )

                # Determine per-symbol start dates (incremental where possible)
                # For simplicity, use the earliest needed start date for the batch
                batch_start = start_date
                for s in batch:
                    latest = self._latest_dates.get(s)
                    if latest:
                        inc_start = (latest + timedelta(days=1)).strftime("%Y-%m-%d")
                        if inc_start > batch_start:
                            pass  # keep the earlier start for the whole batch
                        # NOTE: Alpaca returns data for all symbols over the
                        # same date range. We'll rely on bulk_insert_price_data's
                        # duplicate-skip to ignore dates we already have.

                try:
                    bars_by_symbol = self.fetch_bars_batch(batch, batch_start, end_date)
                except Exception as e:
                    logger.error(f"Batch fetch failed: {e}")
                    bars_by_symbol = {}

                # Process each symbol in the batch
                for symbol in tqdm(batch, desc=f"Processing batch {batch_num}"):
                    bars_df = bars_by_symbol.get(symbol)
                    if bars_df is None and symbol not in self._latest_dates:
                        logger.warning(f"No data for {symbol} — skipping")
                        self.save_checkpoint(symbol)
                        continue

                    results = self.process_symbol(
                        symbol, bars_df=bars_df, run_screening=run_screening
                    )
                    for k, v in results.items():
                        screening_results[k].append(v)

        # --- Store screening results ---
        if run_screening:
            self._store_screening_results(screening_results)

        logger.info(
            f"Completed: Maverick={len(screening_results['maverick'])}, "
            f"Bear={len(screening_results['bear'])}, "
            f"S/D={len(screening_results['supply_demand'])}"
        )

    def _store_screening_results(self, results: dict):
        with self.SessionLocal() as db_session:
            if results["maverick"]:
                c = bulk_insert_screening_data(db_session, MaverickStocks, results["maverick"])
                logger.info(f"Stored {c} Maverick screening results")
            if results["bear"]:
                c = bulk_insert_screening_data(db_session, MaverickBearStocks, results["bear"])
                logger.info(f"Stored {c} Bear screening results")
            if results["supply_demand"]:
                c = bulk_insert_screening_data(
                    db_session, SupplyDemandBreakoutStocks, results["supply_demand"]
                )
                logger.info(f"Stored {c} Supply/Demand screening results")


# ======================================================================
# S&P 500 symbol resolution
# ======================================================================


def _normalize_ticker(symbol: str) -> str:
    """Normalize ticker symbol (dots to dashes, uppercase)."""
    return symbol.strip().upper().replace(".", "-")


def _fetch_stockanalysis_sp500() -> list[str] | None:
    """Fetch S&P 500 symbols from stockanalysis.com."""
    import urllib.request

    try:
        url = "https://stockanalysis.com/list/sp-500-stocks/"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as response:
            html = response.read().decode("utf-8")

        raw = re.findall(r'["\']?s["\']?\s*:\s*"([A-Z]{1,5}(?:\.[A-Z])?)"', html)
        if not raw:
            return None

        seen: set[str] = set()
        symbols: list[str] = []
        for s in raw:
            if s not in seen:
                seen.add(s)
                symbols.append(_normalize_ticker(s))

        if len(symbols) < 400:
            logger.warning(f"stockanalysis.com: only {len(symbols)} symbols")
            return None

        logger.info(f"Fetched {len(symbols)} S&P 500 symbols from stockanalysis.com")
        return symbols
    except Exception as e:
        logger.warning(f"Could not fetch from stockanalysis.com: {e}")
        return None


def _fetch_slickcharts_sp500() -> list[str] | None:
    """Fetch S&P 500 symbols from slickcharts.com."""
    import urllib.request

    try:
        url = "https://www.slickcharts.com/sp500"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as response:
            html = response.read().decode("utf-8")

        raw = re.findall(r'href="/symbol/([A-Z]{1,5}(?:\.[A-Z])?)"', html)
        if not raw:
            return None

        seen: set[str] = set()
        symbols: list[str] = []
        for s in raw:
            if s not in seen:
                seen.add(s)
                symbols.append(_normalize_ticker(s))

        if len(symbols) < 400:
            return None

        logger.info(f"Fetched {len(symbols)} S&P 500 symbols from slickcharts.com")
        return symbols
    except Exception as e:
        logger.warning(f"Could not fetch from slickcharts.com: {e}")
        return None


def _fetch_alpaca_tradable_symbols() -> set[str]:
    """Fetch the set of tradable US equity symbols from Alpaca."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return set()

    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetAssetsRequest
        from alpaca.trading.enums import AssetClass, AssetStatus

        client = TradingClient(
            api_key=ALPACA_API_KEY,
            secret_key=ALPACA_SECRET_KEY,
            paper=ALPACA_PAPER,
        )
        request = GetAssetsRequest(
            asset_class=AssetClass.US_EQUITY,
            status=AssetStatus.ACTIVE,
        )
        assets = client.get_all_assets(request)
        tradable = {a.symbol for a in assets if a.tradable}
        logger.info(f"Fetched {len(tradable)} tradable symbols from Alpaca")
        return tradable
    except Exception as e:
        logger.warning(f"Could not fetch Alpaca assets: {e}")
        return set()


def _cache_symbols(symbols: list[str]) -> None:
    cache_file = os.getenv("SP500_CACHE_FILE", "sp500_symbols_cache.txt")
    try:
        with open(cache_file, "w") as f:
            f.write("\n".join(symbols) + "\n")
    except Exception:
        pass


def get_sp500_symbols() -> list[str]:
    """Get S&P 500 symbols, validated against Alpaca's tradable assets.

    Resolution order:
    1. File from SP500_SYMBOLS_FILE env var
    2. stockanalysis.com
    3. slickcharts.com
    4. Cached symbols file
    """
    # 1. User-provided file
    symbols_file = os.getenv("SP500_SYMBOLS_FILE")
    if symbols_file and Path(symbols_file).exists():
        try:
            with open(symbols_file) as f:
                symbols = [line.strip() for line in f if line.strip()]
            if symbols:
                logger.info(f"Loaded {len(symbols)} symbols from {symbols_file}")
                return symbols
        except Exception:
            pass

    # 2. Web scraping
    symbols = _fetch_stockanalysis_sp500() or _fetch_slickcharts_sp500()
    if symbols:
        # Validate against Alpaca tradable symbols
        alpaca_symbols = _fetch_alpaca_tradable_symbols()
        if alpaca_symbols:
            valid = [s for s in symbols if s in alpaca_symbols]
            dropped = len(symbols) - len(valid)
            if dropped:
                logger.warning(f"Dropped {dropped} symbols not tradable on Alpaca")
            symbols = valid
        logger.info(f"Using {len(symbols)} validated S&P 500 symbols")
        _cache_symbols(symbols)
        return symbols

    # 3. Cache fallback
    cache_file = os.getenv("SP500_CACHE_FILE", "sp500_symbols_cache.txt")
    if Path(cache_file).exists():
        try:
            with open(cache_file) as f:
                symbols = [line.strip() for line in f if line.strip()]
            if symbols:
                logger.info(f"Loaded {len(symbols)} symbols from cache")
                return symbols
        except Exception:
            pass

    logger.error("Unable to load S&P 500 symbols")
    return []


# ======================================================================
# CLI
# ======================================================================


def main():
    global BATCH_SIZE
    parser = argparse.ArgumentParser(
        description="Load market data from Alpaca API (primary provider)"
    )
    parser.add_argument("--symbols", nargs="+", help="Symbols to load")
    parser.add_argument("--file", help="Load symbols from file")
    parser.add_argument("--sp500", action="store_true", help="Load S&P 500 symbols")
    parser.add_argument("--years", type=int, default=2, help="Years of history")
    parser.add_argument("--start-date", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="End date (YYYY-MM-DD)")
    parser.add_argument(
        "--calculate-indicators", action="store_true", help="Calculate technical indicators"
    )
    parser.add_argument("--run-screening", action="store_true", help="Run screening algorithms")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    parser.add_argument("--db-url", help="Database URL override")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="Symbols per API call")

    args = parser.parse_args()

    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        logger.error("ALPACA_API_KEY and ALPACA_SECRET_KEY env vars required")
        sys.exit(1)

    db_url = args.db_url or os.getenv("MCP_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL not configured")
        sys.exit(1)

    # Determine symbols
    symbols: list[str] = []
    if args.symbols:
        symbols = args.symbols
    elif args.file:
        try:
            with open(args.file) as f:
                symbols = [line.strip() for line in f if line.strip()]
        except Exception as e:
            logger.error(f"Could not read symbols from file: {e}")
            sys.exit(1)
    elif args.sp500:
        symbols = get_sp500_symbols()
    else:
        logger.error("No symbols specified. Use --symbols, --file, or --sp500")
        sys.exit(1)

    logger.info(f"Loading {len(symbols)} symbols")

    # Date range
    end_date = args.end_date or datetime.now().strftime("%Y-%m-%d")
    start_date = args.start_date or (
        datetime.now() - timedelta(days=365 * args.years)
    ).strftime("%Y-%m-%d")

    # Override global batch size
    BATCH_SIZE = args.batch_size

    loader = AlpacaDataLoader(db_url=db_url)

    loader.load_symbols(
        symbols,
        start_date,
        end_date,
        run_screening=args.run_screening or args.calculate_indicators,
    )

    logger.info("Data loading complete!")

    # Clean up checkpoint
    if not args.resume and Path(loader.checkpoint_file).exists():
        os.remove(loader.checkpoint_file)
        logger.info("Removed checkpoint file")


if __name__ == "__main__":
    main()

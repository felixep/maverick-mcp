"""
Daily screening scheduler for MaverickMCP.

Runs screening algorithms automatically after market close to keep
screening data fresh. Uses asyncio background tasks - no external
dependencies needed.
"""

import asyncio
import logging
import os
from datetime import datetime, time, timedelta, timezone

logger = logging.getLogger(__name__)

# US Eastern timezone offset (UTC-5 standard, UTC-4 daylight)
ET_OFFSET_STANDARD = timezone(timedelta(hours=-5))
ET_OFFSET_DAYLIGHT = timezone(timedelta(hours=-4))


def _get_et_now() -> datetime:
    """Get current time in US Eastern."""
    utc_now = datetime.now(timezone.utc)
    # Simplified DST check: March second Sunday to November first Sunday
    year = utc_now.year
    # DST starts second Sunday of March
    march_1 = datetime(year, 3, 1, tzinfo=timezone.utc)
    dst_start = march_1 + timedelta(days=(6 - march_1.weekday()) % 7 + 7)
    # DST ends first Sunday of November
    nov_1 = datetime(year, 11, 1, tzinfo=timezone.utc)
    dst_end = nov_1 + timedelta(days=(6 - nov_1.weekday()) % 7)

    if dst_start <= utc_now.replace(tzinfo=timezone.utc) < dst_end:
        return utc_now.astimezone(ET_OFFSET_DAYLIGHT)
    return utc_now.astimezone(ET_OFFSET_STANDARD)


class ScreeningScheduler:
    """Background scheduler that refreshes screening data daily after market close."""

    def __init__(self, screening_time: time = time(17, 30)):
        """
        Args:
            screening_time: Time in ET to run screening (default 5:30 PM ET).
        """
        self.screening_time = screening_time
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_run_date: datetime | None = None

    async def start(self):
        """Start the background scheduler."""
        if self._running:
            logger.warning("Screening scheduler already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._scheduler_loop())
        logger.info(
            f"Screening scheduler started - will run daily at {self.screening_time.strftime('%I:%M %p')} ET"
        )

    async def stop(self):
        """Stop the background scheduler."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Screening scheduler stopped")

    async def _scheduler_loop(self):
        """Main scheduler loop - checks every 60 seconds if it's time to run."""
        while self._running:
            try:
                et_now = _get_et_now()
                current_time = et_now.time()
                current_date = et_now.date()

                # Check if it's time to run and we haven't run today
                is_weekday = et_now.weekday() < 5  # Mon-Fri
                past_screening_time = current_time >= self.screening_time
                not_run_today = (
                    self._last_run_date is None
                    or self._last_run_date != current_date
                )

                if is_weekday and past_screening_time and not_run_today:
                    logger.info(
                        f"Triggering daily screening refresh at {et_now.strftime('%Y-%m-%d %I:%M %p')} ET"
                    )
                    await self.run_screening()
                    self._last_run_date = current_date

                # Sleep 60 seconds before next check
                await asyncio.sleep(60)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scheduler loop error: {e}")
                await asyncio.sleep(300)  # Wait 5 min on error

    async def run_screening(self, symbols: list[str] | None = None) -> dict:
        """Run the screening pipeline.

        Args:
            symbols: Optional list of ticker symbols to screen.
                     If None, screens all active stocks (full refresh).
                     If provided, only screens those specific symbols.
        """
        scope = f"{len(symbols)} symbols" if symbols else "all stocks"
        results = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "status": "running",
            "symbols_requested": symbols,
            "maverick": 0,
            "bear": 0,
            "supply_demand": 0,
        }

        try:
            logger.info(f"Starting screening refresh ({scope})...")

            # Import here to avoid circular imports
            from maverick_mcp.config.database_self_contained import (
                SelfContainedDatabaseSession,
                init_self_contained_database,
            )
            from maverick_mcp.data.models import (
                MaverickBearStocks,
                MaverickStocks,
                SupplyDemandBreakoutStocks,
                bulk_insert_screening_data,
            )

            # Initialize self-contained DB session
            database_url = os.environ.get("DATABASE_URL")
            init_self_contained_database(database_url=database_url)

            # Import the screener
            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent.parent / "scripts"
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from run_stock_screening import StockScreener

            screener = StockScreener()
            today = datetime.now().date()

            with SelfContainedDatabaseSession() as session:
                # Maverick (bullish) screening
                try:
                    maverick_results = await screener.run_maverick_screening(
                        session, symbols=symbols
                    )
                    if maverick_results:
                        count = bulk_insert_screening_data(
                            session, MaverickStocks, maverick_results, today
                        )
                        results["maverick"] = count
                        logger.info(f"Maverick screening: {count} candidates")
                except Exception as e:
                    logger.error(f"Maverick screening failed: {e}")

                # Bear screening
                try:
                    bear_results = await screener.run_bear_screening(
                        session, symbols=symbols
                    )
                    if bear_results:
                        count = bulk_insert_screening_data(
                            session, MaverickBearStocks, bear_results, today
                        )
                        results["bear"] = count
                        logger.info(f"Bear screening: {count} candidates")
                except Exception as e:
                    logger.error(f"Bear screening failed: {e}")

                # Supply/Demand breakout screening
                try:
                    sd_results = await screener.run_supply_demand_screening(
                        session, symbols=symbols
                    )
                    if sd_results:
                        count = bulk_insert_screening_data(
                            session, SupplyDemandBreakoutStocks, sd_results, today
                        )
                        results["supply_demand"] = count
                        logger.info(f"Supply/Demand screening: {count} candidates")
                except Exception as e:
                    logger.error(f"Supply/Demand screening failed: {e}")

            results["status"] = "completed"
            results["completed_at"] = datetime.now(timezone.utc).isoformat()
            logger.info(
                f"Screening refresh complete ({scope}): "
                f"maverick={results['maverick']}, "
                f"bear={results['bear']}, "
                f"supply_demand={results['supply_demand']}"
            )

        except Exception as e:
            results["status"] = "failed"
            results["error"] = str(e)
            logger.error(f"Screening refresh failed: {e}")

        return results

    @property
    def status(self) -> dict:
        """Get scheduler status."""
        et_now = _get_et_now()
        return {
            "running": self._running,
            "screening_time": self.screening_time.strftime("%I:%M %p ET"),
            "last_run": self._last_run_date.isoformat() if self._last_run_date else None,
            "current_time_et": et_now.strftime("%Y-%m-%d %I:%M %p ET"),
            "next_run": self._next_run_time(et_now),
        }

    def _next_run_time(self, et_now: datetime) -> str:
        """Calculate next scheduled run time."""
        today_run = et_now.replace(
            hour=self.screening_time.hour,
            minute=self.screening_time.minute,
            second=0,
        )

        if et_now.time() < self.screening_time and et_now.weekday() < 5:
            return today_run.strftime("%Y-%m-%d %I:%M %p ET")

        # Find next weekday
        next_day = et_now + timedelta(days=1)
        while next_day.weekday() >= 5:  # Skip weekends
            next_day += timedelta(days=1)

        next_run = next_day.replace(
            hour=self.screening_time.hour,
            minute=self.screening_time.minute,
            second=0,
        )
        return next_run.strftime("%Y-%m-%d %I:%M %p ET")


# Singleton instance
_scheduler: ScreeningScheduler | None = None


def get_screening_scheduler() -> ScreeningScheduler:
    """Get or create the singleton screening scheduler."""
    global _scheduler
    if _scheduler is None:
        _scheduler = ScreeningScheduler()
    return _scheduler

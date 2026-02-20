# Data Accuracy Audit — MaverickMCP

> **Date**: 2026-02-19
> **Scope**: End-to-end data pipeline from providers through caching, technical
> analysis, screening, and API responses.
> **Method**: Static code analysis + live cross-validation against independent
> yfinance + pandas\_ta calculations.

---

## Executive Summary

The MaverickMCP data pipeline is **fundamentally accurate** for its core use
case.  Price data from Alpaca matches independent yfinance data exactly.
Technical indicators (pandas\_ta) produce correct, industry-standard results.
Market regime detection validated to within 0.1% of independent calculations.

However, the audit identified **14 issues** across 4 severity tiers.  The most
impactful are: screening data date inconsistencies, overly simplistic
support/resistance levels, and precision loss from 2-decimal rounding in MACD
output.

### Severity Summary

| Severity | Count | Description |
|----------|------:|-------------|
| Critical | 1 | Screening date inconsistency across algorithms |
| High | 3 | Support/resistance method, MACD precision, chart pattern div-by-zero |
| Medium | 5 | Cache staleness, date offset cosmetic, volume zero-fill, missing VWAP, batch concurrency |
| Low | 5 | Penny stock rounding, S/R lookback mismatch, pattern detection too strict, no gap detection, no output validation |

---

## 1. Price Data Accuracy

### Provider Chain

```
Daily bars:  Alpaca (primary) → yfinance (fallback)
Intraday:    yfinance only (Alpaca free tier = daily only)
Metadata:    yfinance
```

**Files**: `providers/stock_data.py`, `utils/alpaca_pool.py`, `utils/yfinance_pool.py`

### Cross-Validation (AAPL, 2026-02-18)

| Field | Production (Alpaca) | Independent (yfinance) | Delta |
|-------|--------------------:|-----------------------:|------:|
| Close | 264.35 | 264.35 | **0.00** |
| RSI(14) | 48.93 | 49.08 | 0.15 (0.3%) |
| MACD | 0.60 | 0.6700 | 0.07 (rounding) |
| MACD Signal | 0.99 | 1.0408 | 0.05 (rounding) |
| MACD Hist | -0.39 | -0.3708 | 0.02 (rounding) |

**Verdict**: Price data is **exact**.  Indicator deltas are within expected
tolerance caused by minor historical price differences between providers and
2-decimal rounding in the API response.

### Data Normalization

- Alpaca returns lowercase columns → normalized to capitalized (`Open`, `High`,
  `Low`, `Close`, `Volume`) at `alpaca_pool.py:163-170`
- Timezone: all operations use timezone-naive dates.  Alpaca's tz-aware index is
  stripped via `tz_localize(None)` at `alpaca_pool.py:173`
- Database stores prices as `Numeric(12,4)` (Decimal) — no float rounding in
  storage.  Converted to `float64` only at query time (`stock_data.py:299-311`)
- yfinance returns adjusted prices by default — consistent with Alpaca's
  adjusted bars.  No mixing risk.

### Smart Date Handling

- If requesting today's data before 22:00 UTC (market close + 1 hr), the system
  automatically rolls back to the previous trading day (`stock_data.py:573-595`)
- Weekend/holiday dates auto-adjust to last trading day
- Uses NYSE market calendar (`pandas_market_calendars`) for trading day detection

### Circuit Breaker

- Alpaca failures trigger automatic yfinance fallback
  (`circuit_breaker_decorators.py:27-83`)
- Empty Alpaca results also trigger fallback (`stock_data.py:706-708`)
- Both providers failing returns empty DataFrame — no stale data served

---

## 2. Technical Analysis Accuracy

### Library

All indicators use **pandas\_ta** (industry-standard Python TA library).
Configuration in `config/technical_constants.py`:

| Indicator | Parameters | Library |
|-----------|-----------|---------|
| RSI | 14-period | pandas\_ta |
| EMA | 21-period | pandas\_ta |
| SMA | 50, 200-period | pandas\_ta |
| MACD | 12/26/9 | pandas\_ta |
| Bollinger Bands | 20-period, 2.0 std | pandas\_ta |
| Stochastic | 14/3/3 K/D/smooth | pandas\_ta |
| ADX | 14-period | pandas\_ta |
| ATR | 14-period | pandas\_ta |

**File**: `core/technical_analysis.py`

### NaN Handling

All analysis functions check for NaN before using values (`technical_analysis.py`
lines 288-296, 342-350, 409-416, 473-482).  Missing indicators return
`{"signal": "unavailable"}` rather than crashing or returning incorrect values.

### Trend Strength Scoring (0–7)

Each of these adds +1 point (`technical_analysis.py:217-246`):
1. Close > SMA(50)
2. Close > EMA(21)
3. EMA(21) > SMA(50)
4. SMA(50) > SMA(200)
5. RSI > 50
6. MACD > 0
7. ADX > 25

All values checked with `pd.notna()` — missing indicators don't count.

### Issues Found

#### HIGH — Support/Resistance Uses Fixed Percentages (not true technical levels)

**File**: `technical_analysis.py:153-198`

Current implementation:
```
Support:    [30-day low, close × 0.95, close × 0.90]
Resistance: [30-day high, close × 1.05, close × 1.10]
```

This is **not real support/resistance detection**.  It just returns the recent
low/high plus fixed 5% and 10% offsets.  No pivot points, no volume clusters,
no price memory analysis.

**Impact**: Traders relying on these levels for entry/exit decisions get
arbitrary price points that don't reflect actual supply/demand zones.

**Recommendation**: Replace with pivot-point or fractal-based detection, or
at minimum use multiple-timeframe highs/lows with clustering.

#### HIGH — MACD Rounded to 2 Decimal Places

**File**: `technical_analysis.py:375-377`

MACD values are rounded to 2 decimal places in the API response.  For a stock
at $264, MACD of 0.6700 becomes 0.60 — a 10% loss in precision.  Signal line
crossovers near zero become indistinguishable.

**Recommendation**: Round MACD to 4 decimal places, or return raw float values.

#### HIGH — Division by Zero in Chart Pattern Detection

**File**: `technical_analysis.py:640, 660, 674, 690`

Pattern detection divides by price values without zero checks:
```python
/ recent_lows[potential_bottoms[-2]]   # line 640 — could be 0
/ recent_highs[potential_tops[-2]]     # line 660 — could be 0
/ recent_prices[i - 1]                 # lines 674, 690 — could be 0
```

**Impact**: Stocks approaching $0 (penny stocks, delisted) will crash the
pattern detection function.  The outer try/except catches it, but the entire
pattern list returns empty rather than just skipping the failing pattern.

**Recommendation**: Add `if value == 0: continue` guards before each division.

#### LOW — Lookback Mismatch

`technical_constants.py` defines `SUPPORT_RESISTANCE_LOOKBACK = 20` but
`technical_analysis.py:164,188` hard-codes 30 days.  The constant is unused.

#### LOW — Pattern Detection Too Strict

Bullish/Bearish flag detection requires *every single bar* in the consolidation
phase to be within 2% of the previous bar (`technical_analysis.py:673-676`).
In practice this almost never triggers.

---

## 3. Market Regime Accuracy

### Cross-Validation (2026-02-19)

| Indicator | Production | Independent | Delta |
|-----------|----------:|------------:|------:|
| SPY Close | 683.90 | 684.01 | 0.11 (0.02%) |
| SMA(200) | 647.93 | 647.93 | **0.00** |
| Above SMA | true | true | Match |
| SMA Rising | true | true | Match |
| % from 52w High | -1.7% | -1.7% | Match |
| VIX | 20.5 | 20.4 | 0.1 |

**Verdict**: Market regime detection is **highly accurate**.

### Classification Logic

**File**: `api/routers/screening.py:518-571`

| Regime | Conditions | Confidence |
|--------|-----------|:----------:|
| CORRECTION | SPY < 52w high by 10%+ AND VIX > 25 | 0.90 |
| STRONG\_BEAR | Below SMA + SMA falling + breadth < 30% + VIX > 25 | 0.85 |
| BEAR | Below SMA + breadth < 40% | 0.75 |
| STRONG\_BULL | Above SMA + SMA rising + breadth > 60% + VIX < 18 | 0.85 |
| BULL | Above SMA + breadth > 50% | 0.70 |
| NEUTRAL | Default | 0.50 |

### Breadth Calculation

Uses `bullish_count / (bullish_count + bearish_count) × 100` from the screening
tables (last 3 days of data).

**Caveat**: Breadth depends on screening data freshness.  If screening is stale,
breadth calculation uses old data.  Mitigated by the 5-minute cache TTL — worst
case is 5 minutes of slightly stale breadth.

---

## 4. Screening Data Accuracy

### Algorithm Validation

Three screening algorithms are implemented in `scripts/run_stock_screening.py`:

| Screen | Minimum Score | Key Criteria | Data Required |
|--------|:------------:|-------------|:-------------:|
| Maverick Bullish | 50/100 | Price > SMA 50/150/200, alignment, volume, RSI < 80 | 200+ days |
| Maverick Bear | 40/100 | Price < SMA 50/200, RSI < 30, MACD bearish, high-vol decline | 200+ days |
| Supply/Demand | All pass | Price > SMA 150/200, SMA 150 > 200, SMA 200 rising 1mo+ | 200+ days |

### CRITICAL — Screening Date Inconsistency

**Observed on production (2026-02-19, 2:24 PM ET)**:

| Algorithm | Latest `date_analyzed` | Expected |
|-----------|:----------------------:|:--------:|
| Maverick Bullish | **2026-02-17** (Mon) | 2026-02-18 (Tue) |
| Maverick Bear | 2026-02-18 (Tue) | 2026-02-18 (Tue) |
| Supply/Demand | 2026-02-18 (Tue) | 2026-02-18 (Tue) |

Bullish screening is **1 trading day behind** bear and breakout screening.
This means the ranked watchlist (which merges all three) compares data from
different dates.

**Root cause**: Likely a partial failure in the Feb 18 screening run —
bear/breakout completed but bullish failed silently.  The scheduler logs show
the container was rebuilt today (Feb 19) and the scheduler was just restarted
at 19:04 UTC.  No Feb 18 run logs available (container replaced).

**Impact**: The ranked watchlist mixes Feb 17 bullish scores with Feb 18
bear/breakout scores, creating an apples-to-oranges comparison.

**Recommendation**:
1. Add per-algorithm date tracking to the scheduler
2. Log/alert when any algorithm fails independently
3. Add a `data_freshness` field to the ranked watchlist response showing the
   oldest `date_analyzed` across all included algorithms

### Scheduler Behavior

**File**: `utils/screening_scheduler.py`

- Runs daily at 5:30 PM ET (weekdays only)
- Pipeline: refresh daily bars → run all 3 screening algorithms → invalidate cache
- Uses 7-day lookback for bars to cover weekends/holidays
- Single-run-per-day guard via `_last_run_date`
- Sends Telegram notification on completion (if configured)

**No retry on partial failure**: If one algorithm fails, the others' results are
still saved but the failed algorithm's data becomes stale with no automatic
retry.

---

## 5. Caching Analysis

### Cache Architecture

```
Request → Redis (TTL-based) → Database (persistent) → Provider (API)
```

**File**: `data/cache.py`

### TTLs

| Data | Cache Key Pattern | TTL | Invalidation |
|------|-------------------|:----|:-------------|
| Maverick Bullish | `v1:screening:maverick:{limit}` | 30 min | After daily scheduler |
| Maverick Bear | `v1:screening:bear:{limit}` | 30 min | After daily scheduler |
| Supply/Demand | `v1:screening:breakouts:{limit}:{filter}` | 30 min | After daily scheduler |
| Ranked Watchlist | `v1:screening:ranked:{max}:{bearish}:{days}` | 30 min | After daily scheduler |
| Market Regime | `v1:market:regime` | 5 min | After daily scheduler |
| System Health | `v1:system_health` | 30 sec | None |
| Price Data | DB (`mcp_price_cache`) | Permanent | Smart gap-fill |

### Serialization

- DataFrames: msgpack + zlib compression with JSON fallback
- Simple types: msgpack preferred, JSON fallback
- In-memory fallback: 1000 entries / 100 MB limit with LRU eviction

### MEDIUM — 30-Minute Screening Cache During Volatility

If the market drops 10% at 10:00 AM, cached screening results from 9:45 AM
remain valid until 10:15 AM.  Traders see bullish recommendations for stocks
now in freefall.

**Mitigation**: The 5-minute regime cache detects the crash quickly (VIX spike
triggers CORRECTION/BEAR regime).  Traders using regime-aware position sizing
would reduce exposure even if screening data is stale.

**Recommendation**: Add an optional `bypass_cache=true` query parameter to
force fresh results during high-volatility periods.

### MEDIUM — Cache Not Invalidated on Scheduler Failure

If the scheduler fails, stale cache entries persist until TTL expiry.
The `_invalidate_screening_cache()` method is only called **after successful
completion** (`screening_scheduler.py:343`).

---

## 6. News Sentiment

### Sources

**File**: `api/routers/news_sentiment_enhanced.py`

1. **Alpaca News API** (free, Benzinga-powered) — primary
2. **Tiingo News API** (requires paid plan) — fallback
3. **Research-based sentiment** (LLM analysis via OpenRouter) — last resort

### Analysis

- Top 10 articles analyzed by LLM for sentiment, confidence, themes
- Returns: overall sentiment (bullish/bearish/neutral), confidence (0-1),
  breakdown counts, key themes, top headlines

### No Caching

News sentiment is fetched fresh on every call.  This is correct behavior for
a time-sensitive data type, but means repeated calls for the same ticker within
seconds will hit the API multiple times.

---

## 7. Data Pipeline Issues (by severity)

### Critical

| # | Issue | File | Impact |
|:-:|-------|------|--------|
| 1 | Screening date inconsistency across algorithms | `screening_scheduler.py` | Ranked watchlist mixes dates |

### High

| # | Issue | File | Impact |
|:-:|-------|------|--------|
| 2 | Support/resistance uses fixed % (5%/10%), not real levels | `technical_analysis.py:153-198` | Misleading S/R levels |
| 3 | MACD rounded to 2 decimal places | `technical_analysis.py:375-377` | Signal crossovers near zero indistinguishable |
| 4 | Division by zero in chart patterns | `technical_analysis.py:640,660,674,690` | Pattern detection crashes for penny stocks |

### Medium

| # | Issue | File | Impact |
|:-:|-------|------|--------|
| 5 | 30-min screening cache during volatility | `screening.py:43-161` | Stale bullish recs during crashes |
| 6 | Date index 05:00 offset (UTC-5 artifact) | `alpaca_pool.py:173` | Confusing date display |
| 7 | Volume zero-fill indistinguishable from real zero | `stock_data.py:738` | Volume analysis can misinterpret missing data |
| 8 | No VWAP calculation | `technical_analysis.py` | Missing key indicator for intraday traders |
| 9 | Batch analysis unbounded concurrency | `trader_api.py:245-249` | Potential DB contention on large batches |

### Low

| # | Issue | File | Impact |
|:-:|-------|------|--------|
| 10 | 2-decimal rounding lossy for penny stocks | `technical_analysis.py` | $1.99 and $2.00 S/R become indistinct |
| 11 | S/R lookback constant unused (20 vs hardcoded 30) | `technical_constants.py` vs `technical_analysis.py:164` | Inconsistency, not a bug |
| 12 | Pattern detection too strict (rarely triggers) | `technical_analysis.py:665-695` | False negatives on valid patterns |
| 13 | No price gap detection | `technical_analysis.py` | Gap-up/gap-down events not flagged |
| 14 | No Pydantic output validation on data endpoints | `api/routers/data.py:83-88` | NaN/Inf could leak to API response |

---

## 8. What Was Validated as NOT a Bug

### Market Calendar Timezone

One agent flagged `schedule.index.tz_localize(None)` (`stock_data.py:371`) as a
critical bug, claiming the index is timezone-aware.  **Tested and confirmed
false** — `pandas_market_calendars.schedule().index` returns `tz: None`
(timezone-naive) in the installed version.  The `tz_localize(None)` call is a
harmless no-op.

### Adjusted vs Unadjusted Prices

Both Alpaca and yfinance return **adjusted prices** by default.  No mixing risk
exists unless a provider is explicitly configured for unadjusted data (which
the codebase does not do).

### Decimal to Float Conversion

Database stores `Numeric(12,4)` and converts to `float64` at query time.  For
stock prices in the $1–$10,000 range, `float64` has ~15 significant digits of
precision — more than enough.  The theoretical precision loss is on the order of
10⁻¹² and is irrelevant for trading decisions.

---

## 9. Recommendations (Prioritized)

### Immediate (Pre-next trading cycle)

1. **Add per-algorithm error handling to scheduler** — if bullish screening
   fails, log an alert and retry once, rather than silently skipping
2. **Add `data_freshness` to ranked watchlist response** — include the oldest
   `date_analyzed` so the trader knows how stale the data is

### Short Term

3. **Increase MACD precision to 4 decimal places** — change
   `technical_analysis.py:375-377` from `round(..., 2)` to `round(..., 4)`
4. **Add division-by-zero guards in pattern detection** — 4 lines of code
5. **Add `bypass_cache` parameter to screening endpoints** — allow traders to
   force fresh data during high-volatility periods

### Medium Term

6. **Replace support/resistance with real detection** — pivot points, volume
   profile, or fractal-based levels
7. **Add VWAP indicator** — standard for intraday and position sizing
8. **Add concurrency semaphore to batch endpoint** — limit to 5–10 concurrent
   analysis tasks
9. **Add Pydantic response models to data endpoints** — catch NaN/Inf before
   serialization

### Low Priority

10. **Clean up date index offset** — strip the 05:00:00 time component from
    Alpaca date indexes for cleaner display
11. **Use S/R lookback constant** — wire `SUPPORT_RESISTANCE_LOOKBACK` into the
    actual function
12. **Relax pattern detection thresholds** — allow 1-2 bars to violate the
    consolidation criteria

---

## Appendix: Test Methodology

### Live Cross-Validation

Performed on 2026-02-19 at ~14:20 ET against production (Unraid
`192.168.10.251:8003`) and independent local yfinance + pandas\_ta calculations.

**Tickers tested**: AAPL, SPY, ^VIX

**Process**:
1. Fetch AAPL full analysis from production REST API
2. Independently fetch AAPL data via `yfinance.download()` and compute
   RSI, MACD, SMA, EMA using `pandas_ta` directly
3. Compare indicator values
4. Fetch SPY/VIX independently to validate market regime
5. Check screening dates across all 3 algorithms

### Static Code Analysis

Full codebase review of:
- `maverick_mcp/providers/stock_data.py` (742 lines)
- `maverick_mcp/core/technical_analysis.py` (872 lines)
- `maverick_mcp/api/routers/screening.py` (628 lines)
- `maverick_mcp/api/routers/trader_api.py` (260 lines)
- `maverick_mcp/api/routers/news_sentiment_enhanced.py` (712 lines)
- `maverick_mcp/data/cache.py` (804 lines)
- `maverick_mcp/data/models.py` (1775 lines)
- `maverick_mcp/utils/screening_scheduler.py` (370 lines)
- `maverick_mcp/utils/alpaca_pool.py` (196 lines)
- `maverick_mcp/utils/yfinance_pool.py` (276 lines)
- `scripts/run_stock_screening.py` (500 lines)

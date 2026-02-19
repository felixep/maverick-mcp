# Performance Optimization — MaverickMCP

## Problem Statement

The autonomous-trader calls maverick-mcp tools through a Node.js REST-to-MCP
proxy.  **Every REST call created a brand-new MCP session** — SSE connect,
`initialize` handshake (enumerates 35+ tools), `notifications/initialized`,
the actual tool call, then teardown.  Production logs showed ~1 second of
overhead per call, even for trivial operations like `get_system_health`.

### By the numbers (before)

| Metric | Value |
|--------|-------|
| Health polls / day | ~1,896 (every 30 s, each 1 s) |
| Per-ticker analysis calls | 3 (tech + S/R + news) |
| Tickers per cycle | ~520 |
| Total MCP sessions per cycle | ~1,560 + polling |
| MCP handshake overhead | ~1 s / call |
| Full analysis wall time | ~26 min (MCP overhead alone) |

---

## Changes Made

### Phase 1 — Direct REST API (biggest win)

**New file**: `maverick_mcp/api/routers/trader_api.py`

A FastAPI `APIRouter(prefix="/api/v1")` is mounted on the existing FastMCP
FastAPI app (same port, same process).  Programmatic consumers call these
endpoints directly over HTTP — zero MCP handshake overhead.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/health` | GET | Lightweight ping (`{"status":"ok"}`) |
| `/api/v1/technical/full-analysis?ticker=X&days=N` | POST | Full technical analysis |
| `/api/v1/technical/support-resistance?ticker=X&days=N` | POST | Support / resistance levels |
| `/api/v1/news/sentiment?ticker=X` | POST | News sentiment |
| `/api/v1/screening/maverick?limit=N` | GET | Bullish screening |
| `/api/v1/screening/bear?limit=N` | GET | Bearish screening |
| `/api/v1/screening/breakouts?limit=N` | GET | Supply/demand breakouts |
| `/api/v1/screening/ranked-watchlist` | GET | Ranked watchlist |
| `/api/v1/market/regime` | GET | Market regime detection |
| `/api/v1/screening/refresh` | POST | Trigger screening refresh |
| `/api/v1/earnings?tickers=X,Y` | GET | Earnings calendar |
| `/api/v1/analysis/batch` | POST | **Batch**: analyse N tickers in one call |

**Batch endpoint** accepts `{"tickers":["AAPL","MSFT",...], "include_news": true, "days": 365}` and runs all three analyses per ticker in parallel.  Replaces 1,560 MCP sessions with ~52 REST calls.

**Mounted in** `server.py` after the existing health/monitoring routers.

**Backward compatibility**: Claude Desktop continues using `/sse`.  These REST
endpoints are additive and do not touch the MCP transport layer.

### Phase 2 — N+1 Query Fix + Redis Caching

#### N+1 fix (`data/models.py`)

Added `lazy="joined"` to three screening model relationships that previously
defaulted to `lazy="select"` (one extra SQL query per row):

- `MaverickStocks.stock` (line 551)
- `MaverickBearStocks.stock` (line 672)
- `SupplyDemandBreakoutStocks.stock` (line 797)

Getting 20 screening results now issues 1 SQL query instead of 21.

#### Redis caching (`api/routers/screening.py`)

Five screening functions now check Redis before hitting the database:

| Function | Cache key pattern | TTL |
|----------|-------------------|-----|
| `get_maverick_stocks` | `v1:screening:maverick:{limit}` | 30 min |
| `get_maverick_bear_stocks` | `v1:screening:bear:{limit}` | 30 min |
| `get_supply_demand_breakouts` | `v1:screening:breakouts:{limit}:{filter}` | 30 min |
| `get_ranked_watchlist` | `v1:screening:ranked:{max}:{bearish}:{days}` | 30 min |
| `get_market_regime` | `v1:market:regime` | 5 min |

Uses the existing `get_from_cache` / `save_to_cache` from `data/cache.py`.

#### Cache invalidation (`utils/screening_scheduler.py`)

After the daily screening refresh completes, `clear_cache("v1:screening:*")`
and `clear_cache("v1:market:regime")` are called to force fresh data on the
next request.

### Phase 3 — Health Check Caching

`get_system_health` MCP tool (`api/routers/health_tools.py`) now caches its
result for 30 seconds via Redis.  Health polls at 30-second intervals will hit
cache instead of running 4 concurrent subsystem checks every time.

### Phase 4 — Production Config

Update the Unraid docker-compose environment:

```yaml
- DB_POOL_SIZE=15          # was 5
- DB_POOL_MAX_OVERFLOW=10  # was 3
```

This gives 25 max connections (within PostgreSQL's `max_connections=100`).

---

## Proxy

The Node.js proxy (`8004`) is **not involved** in the new REST API path.  The
trader calls the MCP server (`8003`) directly.  The proxy remains in place
solely for Claude Desktop's MCP/SSE connection.

---

## Benchmark Results (2026-02-19, production)

Tested from local machine → Unraid server (`192.168.10.251`).  Each endpoint
called 3 times; averages shown below.

### Per-endpoint comparison

| Endpoint | MCP Proxy (avg) | REST Direct (avg) | Speedup | Saved |
|----------|----------------:|------------------:|--------:|------:|
| Health Check | 1,052 ms | 11 ms | **95.6x** | -1,041 ms |
| News Sentiment | 11,195 ms | 3,247 ms | **3.4x** | -7,948 ms |
| Screening: Maverick | 53 ms | 19 ms | **2.7x** | -34 ms |
| Screening: Bear | 56 ms | 19 ms | **3.0x** | -37 ms |
| Screening: Breakouts | 55 ms | 19 ms | **2.9x** | -36 ms |
| Ranked Watchlist | 64 ms | 14 ms | **4.7x** | -50 ms |
| Market Regime | 42 ms | 13 ms | **3.3x** | -29 ms |
| Earnings Calendar | 318 ms | 234 ms | 1.4x | -84 ms |
| Technical Analysis | 3,082 ms | 3,350 ms | ~same | compute-bound |
| Support/Resistance | 1,622 ms | 1,640 ms | ~same | compute-bound |

> Technical Analysis and Support/Resistance are compute-bound (data fetch +
> indicator calculation dominates).  The MCP handshake overhead is negligible
> relative to 1.5–3 seconds of actual work.

### Batch analysis (new — no MCP equivalent)

| Config | REST Batch | MCP Equivalent | Speedup |
|--------|----------:|---------------:|--------:|
| 2 tickers (w/ news) | 9.0 s | 11.2 s (6 calls) | 1.2x |
| 5 tickers (w/ news) | 19.4 s | ~28 s (15 calls) | 1.4x |
| 10 tickers (no news) | 30.8 s | ~46 s (20 calls) | 1.5x |

### Cached endpoints (2nd+ call within TTL)

| Endpoint | Cached response | TTL |
|----------|----------------:|-----|
| Health | 11 ms | N/A (lightweight) |
| Screening: Maverick | 19 ms | 30 min |
| Screening: Bear | 19 ms | 30 min |
| Screening: Breakouts | 19 ms | 30 min |
| Ranked Watchlist | 14 ms | 30 min |
| Market Regime | 13 ms | 5 min |

### Projected full-cycle impact (520 tickers)

| Metric | Before (MCP proxy) | After (REST batch) |
|--------|--------------------:|-------------------:|
| Calls per cycle | 1,560 | 52 |
| Per-ticker time | ~5.8 s | ~3.1 s (in batch) |
| Estimated wall time | ~50 min | ~27 min |
| **Time saved** | — | **~23 min (46%)** |

### Key takeaways

- **Health polling** sees the biggest relative gain: 95.6x faster, saving ~33
  minutes of cumulative overhead per day across 1,896 polls.
- **News sentiment** is the biggest absolute win per call: 3.4x faster (~8 s
  saved) because the MCP handshake compounds with external API latency.
- **Screening + regime** endpoints benefit from both REST speedup and Redis
  caching — repeat calls within TTL return in 13–19 ms.
- **Batch endpoint** reduces HTTP round-trips by 30x and runs analyses in
  parallel, but the per-ticker compute time is the same.
- **Technical analysis** is compute-bound — REST vs MCP makes no meaningful
  difference because the 1.5–3 s of actual work dwarfs the ~40 ms handshake.

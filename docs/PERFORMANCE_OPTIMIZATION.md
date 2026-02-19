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

## Expected Impact

| Change | Latency reduction | Notes |
|--------|-------------------|-------|
| REST API endpoints | ~80% per call | Eliminates MCP handshake |
| Batch endpoint | 30x fewer calls | 52 vs 1,560 |
| N+1 query fix | 20x fewer DB queries | 1 vs 21 per screening call |
| Redis screening cache | Instant repeat calls | 30 min TTL |
| Market regime cache | 3 s saved / call | 5 min TTL |
| Health cache | 1,896 heavy calls eliminated | 30 s TTL |
| Pool size increase | Prevents starvation | 25 max vs 8 |

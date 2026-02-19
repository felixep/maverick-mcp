# Trader Integration Guide — MaverickMCP REST API v1

> **Audience**: autonomous-trading team.  This document describes the new direct
> REST API added to MaverickMCP and how to migrate from the MCP-over-SSE proxy
> pattern to direct HTTP calls.

---

## Why This Change

Previously every tool call from the trader went through this cycle:

```
trader → proxy (REST) → proxy opens SSE → MCP initialize (enumerates 35 tools)
       → notifications/initialized → tools/call → response → SSE close
```

Each call paid ~1 second of MCP handshake overhead.  For 520 tickers × 3
parallel calls, that's ~26 minutes of pure protocol overhead.

The new REST API lets you call the same tool functions directly over HTTP — no
SSE, no MCP handshake, no per-request init.  Latency drops to the actual
computation time (typically 50–500 ms depending on the tool).

---

## Base URL

The REST API is served on the **same port** as the MCP server.  The trader
calls it directly — **no proxy needed**.

| Environment | Base URL |
|-------------|----------|
| Unraid (from host/other containers) | `http://192.168.10.251:8003/api/v1` |
| Docker internal (same compose network) | `http://maverick-mcp:8000/api/v1` |

The proxy (`8004`) is only used by Claude Desktop for MCP/SSE.  The trader
bypasses it entirely.

---

## Endpoints

### Health

```
GET /api/v1/health
```

Returns `{"status": "ok", "timestamp": "..."}`.  No DB/Redis/API checks.
Use this for liveness polling instead of the heavy `get_system_health` MCP tool.

---

### Technical Analysis

```
POST /api/v1/technical/full-analysis?ticker=AAPL&days=365
```

Full technical analysis (RSI, MACD, Bollinger, trend, volume, patterns).
Same output as MCP tool `technical_get_full_technical_analysis`.

```
POST /api/v1/technical/support-resistance?ticker=AAPL&days=365
```

Support/resistance levels.  Same as MCP tool `technical_get_support_resistance`.

---

### News Sentiment

```
POST /api/v1/news/sentiment?ticker=AAPL&timeframe=7d&limit=10
```

News sentiment analysis.  Same as MCP tool `data_get_news_sentiment`.

---

### Screening

```
GET /api/v1/screening/maverick?limit=20
GET /api/v1/screening/bear?limit=20
GET /api/v1/screening/breakouts?limit=20
GET /api/v1/screening/ranked-watchlist?max_symbols=10&include_bearish=false&days_back=3
```

All return the same JSON as their MCP counterparts.  Results are now **cached
for 30 minutes** in Redis and invalidated when the daily screening scheduler
runs (5:30 PM ET).

---

### Market Regime

```
GET /api/v1/market/regime
```

Returns regime classification (`STRONG_BULL`, `BULL`, `NEUTRAL`, `BEAR`,
`STRONG_BEAR`, `CORRECTION`) with confidence, indicators, and strategy guidance.

Cached for **5 minutes**.

---

### Screening Refresh

```
POST /api/v1/screening/refresh
Content-Type: application/json

{"symbols": ["AAPL", "MSFT"]}   # optional — null for full refresh
```

Triggers a screening refresh.  Same as MCP tool `screening_refresh_now`.

---

### Earnings Calendar

```
GET /api/v1/earnings?tickers=AAPL,MSFT,GOOG
```

Returns next earnings dates.  Same as MCP tool `data_get_earnings_calendar`.

---

### Batch Analysis (Recommended)

```
POST /api/v1/analysis/batch
Content-Type: application/json

{
  "tickers": ["AAPL", "MSFT", "GOOG", "AMZN", "NVDA"],
  "include_news": true,
  "days": 365
}
```

**Response:**

```json
{
  "status": "success",
  "count": 5,
  "results": {
    "AAPL": {
      "technical": { ... },
      "support_resistance": { ... },
      "news": { ... }
    },
    "MSFT": { ... },
    ...
  },
  "timestamp": "2026-02-19T..."
}
```

This endpoint runs all three analyses (technical + support/resistance + news)
**in parallel** for each ticker, all within a single HTTP request.

**Recommended usage**: batch 5–10 tickers per call.  For 520 tickers, that's
52–104 HTTP calls instead of 1,560 MCP sessions.

**Limits**: max 50 tickers per batch request.

---

## Migration Path

### Step 1: Switch health polling (immediate win)

Replace:
```python
# Old: MCP via proxy (~1s per call)
response = requests.post(f"{MAVERICK_URL}/tools/get_system_health", json={})
```

With:
```python
# New: Direct REST (~5ms)
response = requests.get(f"{MAVERICK_BASE}/api/v1/health")
```

### Step 2: Switch screening calls

Replace:
```python
# Old
response = requests.post(f"{MAVERICK_URL}/tools/screening_get_maverick_stocks", json={"limit": 20})
```

With:
```python
# New
response = requests.get(f"{MAVERICK_BASE}/api/v1/screening/maverick?limit=20")
```

### Step 3: Switch to batch analysis

Replace the per-ticker loop:
```python
# Old: 3 MCP calls per ticker, 520 tickers = 1,560 calls
for ticker in watchlist:
    tech = call_mcp("technical_get_full_technical_analysis", {"ticker": ticker})
    sr = call_mcp("technical_get_support_resistance", {"ticker": ticker})
    news = call_mcp("data_get_news_sentiment", {"ticker": ticker})
```

With batched calls:
```python
# New: 1 REST call per batch of 10 tickers = 52 calls total
for batch in chunks(watchlist, 10):
    response = requests.post(f"{MAVERICK_BASE}/api/v1/analysis/batch", json={
        "tickers": batch,
        "include_news": True,
        "days": 365
    })
    for ticker, data in response.json()["results"].items():
        tech = data["technical"]
        sr = data["support_resistance"]
        news = data.get("news")
```

### Step 4: Switch market regime

Replace:
```python
response = requests.post(f"{MAVERICK_URL}/tools/market_get_regime", json={})
```

With:
```python
response = requests.get(f"{MAVERICK_BASE}/api/v1/market/regime")
```

---

## Environment Variable Changes

Update the trader config to point directly at the MCP server's REST API:

```yaml
# Old — goes through proxy, pays MCP handshake overhead
MAVERICK_MCP_URL: http://192.168.10.251:8004/tools

# New — direct REST API, no proxy
MAVERICK_MCP_URL: http://192.168.10.251:8003/api/v1
```

The old proxy `/tools` endpoints still work — this migration can be done
gradually.

---

## Benchmark Results (2026-02-19)

Measured on production (local machine → Unraid `192.168.10.251`), 3 runs each.

### Per-endpoint latency

| Endpoint | MCP Proxy | REST Direct | Speedup |
|----------|----------:|------------:|--------:|
| Health Check | 1,052 ms | 11 ms | **95.6x** |
| News Sentiment | 11,195 ms | 3,247 ms | **3.4x** |
| Screening: Maverick | 53 ms | 19 ms | **2.7x** |
| Screening: Bear | 56 ms | 19 ms | **3.0x** |
| Screening: Breakouts | 55 ms | 19 ms | **2.9x** |
| Ranked Watchlist | 64 ms | 14 ms | **4.7x** |
| Market Regime | 42 ms | 13 ms | **3.3x** |
| Earnings Calendar | 318 ms | 234 ms | 1.4x |
| Technical Analysis | 3,082 ms | 3,350 ms | ~same |
| Support/Resistance | 1,622 ms | 1,640 ms | ~same |

> Technical Analysis and Support/Resistance are compute-bound — the REST path
> does not speed them up because the actual data fetch + calculation (1.5–3 s)
> dwarfs any protocol overhead.

### Batch analysis

| Batch size | REST (1 call) | Old MCP equivalent | Speedup |
|------------|----------:|-------------------:|--------:|
| 2 tickers (w/ news) | 9.0 s | 11.2 s (6 calls) | 1.2x |
| 5 tickers (w/ news) | 19.4 s | ~28 s (15 calls) | 1.4x |
| 10 tickers (no news) | 30.8 s | ~46 s (20 calls) | 1.5x |

### Cached screening (2nd+ call within TTL)

All screening and regime endpoints return in **13–19 ms** on cache hit (Redis).
Cache is invalidated automatically when the daily screening scheduler runs at
5:30 PM ET.

### Full-cycle projection (520 tickers)

| Metric | Before | After |
|--------|-------:|------:|
| Calls per cycle | 1,560 | 52 |
| Estimated wall time | ~50 min | ~27 min |
| **Time saved** | — | **~23 min (46%)** |

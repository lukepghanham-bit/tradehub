"""Live crypto prices from public exchange APIs (no API keys needed).

We pull 1-minute candles rather than the last price so that wicks count:
if price spikes through a target or stop between two checks, the candle's
high/low still catches it.

Order of sources: Binance spot -> Bybit perps -> Bybit spot.
"""
import asyncio
import logging
import time

import aiohttp

log = logging.getLogger("prices")

BINANCE = "https://data-api.binance.vision/api/v3/klines"
BYBIT = "https://api.bybit.com/v5/market/kline"

_source_cache = {}  # symbol -> "binance" | "bybit-linear" | "bybit-spot"


async def _binance(session, symbol, start_ms):
    params = {"symbol": symbol, "interval": "1m", "startTime": start_ms, "limit": 1000}
    async with session.get(BINANCE, params=params) as r:
        if r.status != 200:
            return None
        rows = await r.json()
    # [openTime, open, high, low, close, ...]
    return [(int(k[0]), float(k[2]), float(k[3]), float(k[4])) for k in rows]


async def _bybit(session, symbol, start_ms, category):
    params = {"category": category, "symbol": symbol, "interval": "1", "start": start_ms, "limit": 1000}
    async with session.get(BYBIT, params=params) as r:
        if r.status != 200:
            return None
        data = await r.json()
    if data.get("retCode") != 0 or not data.get("result", {}).get("list"):
        return None
    rows = data["result"]["list"]  # newest first: [start, open, high, low, close, ...]
    return sorted((int(k[0]), float(k[2]), float(k[3]), float(k[4])) for k in rows)


SOURCES = {
    "binance": lambda s, sym, st: _binance(s, sym, st),
    "bybit-linear": lambda s, sym, st: _bybit(s, sym, st, "linear"),
    "bybit-spot": lambda s, sym, st: _bybit(s, sym, st, "spot"),
}


async def candles(session, symbol, start_ms):
    """Return [(open_time_ms, high, low, close), ...] oldest first, or None if no exchange lists it."""
    start_ms = max(start_ms, int(time.time() * 1000) - 999 * 60_000)  # cap lookback at ~16h
    order = list(SOURCES)
    if symbol in _source_cache:
        order.remove(_source_cache[symbol])
        order.insert(0, _source_cache[symbol])
    for name in order:
        try:
            rows = await SOURCES[name](session, symbol, start_ms)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("%s %s: %s", name, symbol, e)
            rows = None
        if rows:
            _source_cache[symbol] = name
            return rows
    return None


async def last_price(session, symbol):
    rows = await candles(session, symbol, int(time.time() * 1000) - 5 * 60_000)
    return rows[-1][3] if rows else None


def new_session():
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

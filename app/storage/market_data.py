"""SQLite-backed candle cache for historical market data."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from app.clients.binance_rest import BinanceRestClient
from app.models.events import CandleEvent

MARKET_DATA_PATH = Path.home() / ".bnc-tui" / "market_data.sqlite3"
SUPPORTED_CANDLE_INTERVALS = ("1m", "15m", "1h", "4h", "1d")

_INTERVAL_MS = {
    "1m": 60_000,
    "15m": 15 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}


class MarketDataStore:
    """Persists Binance OHLC candles without losing Decimal precision."""

    def __init__(self, path: Path = MARKET_DATA_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS candles (
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    open_time_ms INTEGER NOT NULL,
                    close_time_ms INTEGER NOT NULL,
                    open TEXT NOT NULL,
                    high TEXT NOT NULL,
                    low TEXT NOT NULL,
                    close TEXT NOT NULL,
                    volume TEXT NOT NULL,
                    quote_volume TEXT NOT NULL,
                    trade_count INTEGER NOT NULL,
                    is_closed INTEGER NOT NULL,
                    PRIMARY KEY (symbol, interval, open_time_ms)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_candles_symbol_interval_range
                ON candles (symbol, interval, open_time_ms)
                """
            )

    def upsert_candles(self, candles: Iterable[CandleEvent]) -> int:
        """Insert or replace candles. Returns the number of rows written."""
        rows = [
            (
                candle.symbol.upper(),
                candle.interval,
                candle.open_time_ms,
                candle.close_time_ms,
                str(candle.open),
                str(candle.high),
                str(candle.low),
                str(candle.close),
                str(candle.volume),
                str(candle.quote_volume),
                candle.trade_count,
                1 if candle.is_closed else 0,
            )
            for candle in candles
        ]
        if not rows:
            return 0

        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO candles (
                    symbol, interval, open_time_ms, close_time_ms,
                    open, high, low, close, volume, quote_volume,
                    trade_count, is_closed
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, interval, open_time_ms) DO UPDATE SET
                    close_time_ms = excluded.close_time_ms,
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume,
                    quote_volume = excluded.quote_volume,
                    trade_count = excluded.trade_count,
                    is_closed = excluded.is_closed
                """,
                rows,
            )
        return len(rows)

    def get_candles(
        self,
        symbol: str,
        interval: str,
        start_time_ms: int,
        end_time_ms: int,
    ) -> list[CandleEvent]:
        """Return candles in [start_time_ms, end_time_ms) ordered by open_time_ms ascending."""
        _validate_interval(interval)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM candles
                WHERE symbol = ?
                  AND interval = ?
                  AND open_time_ms >= ?
                  AND open_time_ms < ?
                ORDER BY open_time_ms ASC
                """,
                (symbol.upper(), interval, start_time_ms, end_time_ms),
            ).fetchall()
        return [_row_to_candle(row) for row in rows]

    def get_cached_open_times(
        self,
        symbol: str,
        interval: str,
        start_time_ms: int,
        end_time_ms: int,
    ) -> set[int]:
        """Return the set of cached open_time_ms values within the given range."""
        _validate_interval(interval)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT open_time_ms
                FROM candles
                WHERE symbol = ?
                  AND interval = ?
                  AND open_time_ms >= ?
                  AND open_time_ms < ?
                """,
                (symbol.upper(), interval, start_time_ms, end_time_ms),
            ).fetchall()
        return {int(row["open_time_ms"]) for row in rows}

    def get_latest_candles(
        self,
        symbol: str,
        interval: str,
        limit: int,
    ) -> list[CandleEvent]:
        """Return the *limit* most recent candles ordered by open_time_ms ascending."""
        _validate_interval(interval)
        safe_limit = max(1, limit)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM candles
                WHERE symbol = ?
                  AND interval = ?
                ORDER BY open_time_ms DESC
                LIMIT ?
                """,
                (symbol.upper(), interval, safe_limit),
            ).fetchall()
        return [_row_to_candle(row) for row in reversed(rows)]


class CandleCache:
    """Loads historical candles from SQLite and fills gaps from Binance."""

    def __init__(
        self,
        store: MarketDataStore,
        rest_client: BinanceRestClient,
    ) -> None:
        self._store = store
        self._rest_client = rest_client

    async def get_or_fetch(
        self,
        symbol: str,
        interval: str,
        start_time_ms: int,
        end_time_ms: int,
    ) -> list[CandleEvent]:
        """Return candles for the range, fetching any missing gaps from Binance REST."""
        _validate_interval(interval)
        start_time_ms = _align_open_time(start_time_ms, interval)
        missing_ranges = self._missing_ranges(symbol, interval, start_time_ms, end_time_ms)

        for missing_start, missing_end in missing_ranges:
            candles = await self._rest_client.get_historical_klines(
                symbol,
                interval,
                start_time_ms=missing_start,
                end_time_ms=missing_end,
            )
            self._store.upsert_candles(candles)

        return self._store.get_candles(symbol, interval, start_time_ms, end_time_ms)

    def _missing_ranges(
        self,
        symbol: str,
        interval: str,
        start_time_ms: int,
        end_time_ms: int,
    ) -> list[tuple[int, int]]:
        """Identify contiguous gaps in the cached candle data and return them as (start, end) pairs."""
        step = _interval_ms(interval)
        cached = self._store.get_cached_open_times(
            symbol,
            interval,
            start_time_ms,
            end_time_ms,
        )
        ranges: list[tuple[int, int]] = []
        current_start: int | None = None
        current = start_time_ms
        while current < end_time_ms:
            if current not in cached and current_start is None:
                current_start = current
            elif current in cached and current_start is not None:
                ranges.append((current_start, current))
                current_start = None
            current += step

        if current_start is not None:
            ranges.append((current_start, end_time_ms))
        return ranges


def _interval_ms(interval: str) -> int:
    _validate_interval(interval)
    return _INTERVAL_MS[interval]


def candle_interval_ms(interval: str) -> int:
    return _interval_ms(interval)


def _align_open_time(timestamp_ms: int, interval: str) -> int:
    step = _interval_ms(interval)
    return timestamp_ms - (timestamp_ms % step)


def _validate_interval(interval: str) -> None:
    if interval not in _INTERVAL_MS:
        supported = ", ".join(SUPPORTED_CANDLE_INTERVALS)
        raise ValueError(f"Unsupported candle interval: {interval}. Supported: {supported}")


def _row_to_candle(row: sqlite3.Row) -> CandleEvent:
    from decimal import Decimal

    return CandleEvent(
        symbol=row["symbol"],
        interval=row["interval"],
        open_time_ms=int(row["open_time_ms"]),
        close_time_ms=int(row["close_time_ms"]),
        open=Decimal(row["open"]),
        high=Decimal(row["high"]),
        low=Decimal(row["low"]),
        close=Decimal(row["close"]),
        volume=Decimal(row["volume"]),
        quote_volume=Decimal(row["quote_volume"]),
        trade_count=int(row["trade_count"]),
        is_closed=bool(row["is_closed"]),
    )

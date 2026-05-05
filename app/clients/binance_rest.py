"""
BinanceRestClient — direct Binance REST integration without a middleman.

Trade history and balances are TTL-cached for 30 seconds to avoid hitting
rate limits on every TUI refresh. Network errors are retried up to 3 times
with exponential backoff (1 s → 2 s → 4 s). HTTP 4xx errors are not retried.
"""

import hashlib
import hmac
import os
import time
from decimal import Decimal
from urllib.parse import urlencode

import httpx
from cachetools import TTLCache
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.models.events import CandleEvent

BINANCE_BASE_URL = "https://api.binance.com"

_STABLECOINS = {"USDT", "USDC", "FDUSD", "BUSD", "DAI", "TUSD"}

_trade_cache: TTLCache = TTLCache(maxsize=10, ttl=30)
_balance_cache: TTLCache = TTLCache(maxsize=1, ttl=30)
_kline_cache: TTLCache = TTLCache(maxsize=20, ttl=30)


class BinanceClientError(RuntimeError):
    pass


class BinanceRestClient:
    """Direct Binance REST API client. Prices are streamed via WebSocket; this client handles trade history, balances, and historical klines."""

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        base_url: str = BINANCE_BASE_URL,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key or os.getenv("BINANCE_API_KEY", "")
        self.api_secret = api_secret or os.getenv("BINANCE_API_SECRET", "")

    async def get_trades(self, symbol: str, limit: int = 500) -> list[dict]:
        """Return the raw trade list for a symbol (TTL-cached)."""
        cache_key = f"trades:{symbol.upper()}:{limit}"
        if cache_key not in _trade_cache:
            _trade_cache[cache_key] = await self._fetch_trades(symbol, limit)
        return _trade_cache[cache_key]

    async def get_klines(
        self,
        symbol: str,
        interval: str = "15m",
        limit: int = 96,
    ) -> list[CandleEvent]:
        """Public OHLC candle history for the chart."""
        safe_limit = max(1, min(limit, 1000))
        cache_key = f"klines:{symbol.upper()}:{interval}:{safe_limit}"
        if cache_key not in _kline_cache:
            _kline_cache[cache_key] = await self._fetch_klines(
                symbol,
                interval,
                safe_limit,
            )
        return _kline_cache[cache_key]

    async def get_historical_klines(
        self,
        symbol: str,
        interval: str,
        start_time_ms: int,
        end_time_ms: int,
    ) -> list[CandleEvent]:
        """Public OHLC candles for an exact historical time range."""
        if end_time_ms <= start_time_ms:
            return []

        current_start = start_time_ms
        candles: list[CandleEvent] = []
        while current_start < end_time_ms:
            page = await self._fetch_klines(
                symbol,
                interval,
                limit=1000,
                start_time_ms=current_start,
                end_time_ms=end_time_ms - 1,
            )
            if not page:
                break

            candles.extend(c for c in page if c.open_time_ms < end_time_ms)
            next_start = page[-1].open_time_ms + 1
            if next_start <= current_start:
                break
            current_start = next_start

            if len(page) < 1000:
                break

        return candles

    async def get_trade_summary(self, symbol: str, limit: int = 500) -> dict:
        """Return a FIFO PnL summary dict for the symbol (TTL-cached)."""
        cache_key = f"{symbol.upper()}:{limit}"

        summary_key = f"summary:{symbol.upper()}:{limit}"
        if summary_key in _trade_cache:
            return _trade_cache[summary_key]

        trades = await self.get_trades(symbol, limit)
        summary = _calculate_summary(symbol, trades)
        _trade_cache[summary_key] = summary
        return summary

    async def get_balances(self) -> tuple[list[dict], Decimal]:
        """
        Return all non-zero balances with USD-equivalent values (TTL-cached).

        Fetches /api/v3/account and /api/v3/ticker/price in parallel, then
        maps each asset to its USD value via FDUSD or USDT pairs.

        Returns:
            A tuple of (balances, fdusd_usdt_rate) where balances is a list of
            dicts with keys: asset, free, locked, usd_value — sorted by
            usd_value descending.
        """
        if "balances" in _balance_cache:
            return _balance_cache["balances"], _balance_cache["fdusd_rate"]

        account, all_prices = await self._fetch_account_and_prices()

        price_map: dict[str, Decimal] = {
            p["symbol"]: Decimal(p["price"]) for p in all_prices
        }

        fdusd_rate: Decimal = price_map.get("FDUSDUSDT", Decimal("1"))

        result = []
        for b in account.get("balances", []):
            asset = b["asset"]
            free = Decimal(b["free"])
            locked = Decimal(b["locked"])
            total = free + locked

            if total == 0:
                continue

            if asset in _STABLECOINS:
                usd_value = total
            else:
                pair_price = price_map.get(f"{asset}FDUSD") or price_map.get(f"{asset}USDT", Decimal("0"))
                usd_value = total * pair_price

            result.append({
                "asset": asset,
                "free": free,
                "locked": locked,
                "usd_value": usd_value,
            })

        result.sort(key=lambda x: x["usd_value"], reverse=True)
        _balance_cache["balances"] = result
        _balance_cache["fdusd_rate"] = fdusd_rate
        return result, fdusd_rate

    async def _fetch_account_and_prices(self) -> tuple[dict, list]:
        """Fetch account info and all ticker prices concurrently."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            import asyncio
            account_task = asyncio.create_task(self._fetch_account(client))
            prices_task = asyncio.create_task(
                client.get(f"{self.base_url}/api/v3/ticker/price")
            )
            account = await account_task
            prices_resp = await prices_task
            prices_resp.raise_for_status()
            return account, prices_resp.json()

    async def _fetch_account(self, client: httpx.AsyncClient) -> dict:
        """Send a signed /api/v3/account request and return the JSON payload."""
        params = {"timestamp": int(time.time() * 1000)}
        signed = self._signed_query(params)
        url = f"{self.base_url}/api/v3/account?{signed}"
        try:
            resp = await client.get(url, headers={"X-MBX-APIKEY": self.api_key})
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise BinanceClientError(_extract_error(exc.response)) from exc
        return resp.json()

    async def _fetch_klines(
        self,
        symbol: str,
        interval: str,
        limit: int,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> list[CandleEvent]:
        """Binance public /api/v3/klines response parsed into candle events."""
        params = {
            "symbol": symbol.upper(),
            "interval": interval,
            "limit": limit,
        }
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        if end_time_ms is not None:
            params["endTime"] = end_time_ms
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(f"{self.base_url}/api/v3/klines", params=params)
                resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise BinanceClientError(_extract_error(exc.response)) from exc

        payload = resp.json()
        if not isinstance(payload, list):
            raise BinanceClientError("Binance returned an unexpected kline response.")
        return [_parse_kline_row(symbol, interval, row) for row in payload]

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type((httpx.NetworkError, httpx.TimeoutException)),
    )
    async def _fetch_trades(self, symbol: str, limit: int) -> list[dict]:
        """Fetch raw trade list from Binance with retry on network errors."""
        if not self.api_key or not self.api_secret:
            raise BinanceClientError(
                "BINANCE_API_KEY and BINANCE_API_SECRET are required."
            )

        params = {
            "symbol": symbol.upper(),
            "limit": max(1, min(limit, 1000)),
            "timestamp": int(time.time() * 1000),
        }
        signed = self._signed_query(params)
        url = f"{self.base_url}/api/v3/myTrades?{signed}"

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, headers={"X-MBX-APIKEY": self.api_key})
                resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise BinanceClientError(_extract_error(exc.response)) from exc

        payload = resp.json()
        if not isinstance(payload, list):
            raise BinanceClientError("Binance returned an unexpected trade response.")
        return payload

    def _signed_query(self, params: dict) -> str:
        query = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode(),
            query.encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"{query}&signature={signature}"


def _calculate_summary(symbol: str, trades: list[dict]) -> dict:
    """
    Compute a FIFO average-cost PnL summary from a raw trade list.

    Pure function — no HTTP calls, straightforward to test independently.
    """
    symbol = symbol.upper()
    base_asset, quote_asset = _infer_assets(symbol)
    ordered = sorted(trades, key=lambda t: t["time"])

    buy_count = sell_count = 0
    bought_qty = sold_qty = Decimal("0")
    buy_quote = sell_quote = Decimal("0")
    remaining_qty = remaining_cost = realized_pnl = Decimal("0")
    exact_commission_quote = Decimal("0")
    external_commissions: dict[str, Decimal] = {}

    for t in ordered:
        qty = Decimal(t["qty"])
        quote_qty = Decimal(t["quoteQty"])
        price = Decimal(t["price"])
        commission = Decimal(str(t.get("commission", "0")))
        commission_asset = str(t.get("commissionAsset", "")).upper()

        quote_commission = Decimal("0")
        if commission > 0 and commission_asset == quote_asset:
            quote_commission = commission
            exact_commission_quote += commission
        elif commission > 0 and commission_asset and commission_asset != base_asset:
            external_commissions[commission_asset] = (
                external_commissions.get(commission_asset, Decimal("0")) + commission
            )

        if t["isBuyer"]:
            buy_count += 1
            net_qty = qty
            if commission > 0 and commission_asset == base_asset:
                net_qty = max(Decimal("0"), qty - commission)

            bought_qty += net_qty
            buy_quote += quote_qty
            remaining_qty += net_qty
            remaining_cost += quote_qty + quote_commission
        else:
            sell_count += 1
            net_quote = quote_qty - quote_commission
            base_commission = commission if commission > 0 and commission_asset == base_asset else Decimal("0")
            qty_to_close = qty + base_commission
            sold_qty += qty_to_close
            sell_quote += net_quote

            if remaining_qty > 0:
                avg_cost = remaining_cost / remaining_qty
                qty_to_remove = min(qty_to_close, remaining_qty)
                cost_to_remove = qty_to_remove * avg_cost
                proceeds_for_closed_qty = net_quote * _safe_ratio(qty_to_remove, qty_to_close)
                realized_pnl += proceeds_for_closed_qty - cost_to_remove
                remaining_qty -= qty_to_remove
                remaining_cost -= cost_to_remove

    if remaining_qty <= 0:
        remaining_qty = remaining_cost = Decimal("0")
    average_cost = _safe_div(remaining_cost, remaining_qty)

    return {
        "symbol": symbol,
        "base_asset": base_asset,
        "quote_asset": quote_asset,
        "trade_count": len(trades),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "bought_qty": bought_qty,
        "sold_qty": sold_qty,
        "buy_quote_qty": buy_quote,
        "sell_quote_qty": sell_quote,
        "average_buy_price": _safe_div(buy_quote, bought_qty),
        "average_sell_price": _safe_div(sell_quote, sold_qty),
        "remaining_qty": remaining_qty,
        "remaining_cost_quote": remaining_cost,
        "average_cost": average_cost,
        "break_even_price": average_cost,
        "exact_commission_quote": exact_commission_quote,
        "external_commissions": external_commissions,
        "realized_pnl_quote": realized_pnl,
    }


def _safe_div(a: Decimal, b: Decimal) -> Decimal | None:
    return None if b == 0 else a / b


def _safe_ratio(part: Decimal, total: Decimal) -> Decimal:
    return Decimal("0") if total == 0 else part / total


def _infer_assets(symbol: str) -> tuple[str, str]:
    for quote_asset in sorted(_STABLECOINS, key=len, reverse=True):
        if symbol.endswith(quote_asset):
            return symbol[: -len(quote_asset)], quote_asset
    return symbol[:-4], symbol[-4:]


def _parse_kline_row(symbol: str, interval: str, row: list) -> CandleEvent:
    """Parse Binance REST kline array."""
    return CandleEvent(
        symbol=symbol.upper(),
        interval=interval,
        open_time_ms=int(row[0]),
        open=Decimal(row[1]),
        high=Decimal(row[2]),
        low=Decimal(row[3]),
        close=Decimal(row[4]),
        volume=Decimal(row[5]),
        close_time_ms=int(row[6]),
        quote_volume=Decimal(row[7]),
        trade_count=int(row[8]),
        is_closed=True,
    )


def _extract_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict) and payload.get("msg"):
            return str(payload["msg"])
    except ValueError:
        pass
    return f"Binance HTTP {response.status_code}"

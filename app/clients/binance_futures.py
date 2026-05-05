"""Read-only Binance USD-M futures REST client."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from decimal import Decimal
from urllib.parse import urlencode

import httpx

from app.clients.binance_rest import BinanceClientError, _extract_error, _parse_kline_row
from app.models.events import CandleEvent

BINANCE_FUTURES_BASE_URL = "https://fapi.binance.com"


class BinanceFuturesRestClient:
    """Small USD-M futures client matching the methods the TUI already calls."""

    market_label = "USD-M Futures"

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        base_url: str = BINANCE_FUTURES_BASE_URL,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key or os.getenv("BINANCE_API_KEY", "")
        self.api_secret = api_secret or os.getenv("BINANCE_API_SECRET", "")

    async def get_klines(
        self,
        symbol: str,
        interval: str = "15m",
        limit: int = 96,
    ) -> list[CandleEvent]:
        safe_limit = max(1, min(limit, 1500))
        return await self._fetch_klines(symbol, interval, safe_limit)

    async def get_historical_klines(
        self,
        symbol: str,
        interval: str,
        start_time_ms: int,
        end_time_ms: int,
    ) -> list[CandleEvent]:
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

    async def get_trades(self, symbol: str, limit: int = 500) -> list[dict]:
        payload = await self._signed_get(
            "/fapi/v1/userTrades",
            {"symbol": symbol.upper(), "limit": max(1, min(limit, 1000))},
        )
        if not isinstance(payload, list):
            raise BinanceClientError("Binance geçersiz futures trade yanıtı döndürdü.")
        return [_normalize_futures_trade(row) for row in payload]

    async def get_trade_summary(self, symbol: str, limit: int = 500) -> dict:
        positions = await self._signed_get("/fapi/v3/positionRisk", {"symbol": symbol.upper()})
        if not isinstance(positions, list):
            raise BinanceClientError("Binance geçersiz futures position yanıtı döndürdü.")
        position = _pick_position(symbol, positions)

        trades: list[dict] = []
        try:
            trades = await self.get_trades(symbol, limit)
        except BinanceClientError:
            trades = []

        return _position_summary(symbol, position, trades)

    async def get_balances(self) -> tuple[list[dict], Decimal]:
        account = await self._signed_get("/fapi/v3/account", {})
        if not isinstance(account, dict):
            raise BinanceClientError("Binance geçersiz futures account yanıtı döndürdü.")

        balances = []
        for asset in account.get("assets", []):
            wallet = Decimal(asset.get("walletBalance", "0"))
            unrealized = Decimal(asset.get("unrealizedProfit", "0"))
            margin_balance = Decimal(asset.get("marginBalance", wallet + unrealized))
            if margin_balance == 0:
                continue
            balances.append(
                {
                    "asset": asset.get("asset", ""),
                    "free": Decimal(asset.get("availableBalance", "0")),
                    "locked": Decimal(asset.get("initialMargin", "0")),
                    "usd_value": margin_balance,
                }
            )
        balances.sort(key=lambda x: x["usd_value"], reverse=True)
        return balances, Decimal("1")

    async def _fetch_klines(
        self,
        symbol: str,
        interval: str,
        limit: int,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> list[CandleEvent]:
        params = {"symbol": symbol.upper(), "interval": interval, "limit": limit}
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        if end_time_ms is not None:
            params["endTime"] = end_time_ms

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(f"{self.base_url}/fapi/v1/klines", params=params)
                resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise BinanceClientError(_extract_error(exc.response)) from exc

        payload = resp.json()
        if not isinstance(payload, list):
            raise BinanceClientError("Binance geçersiz futures kline yanıtı döndürdü.")
        return [_parse_kline_row(symbol, interval, row) for row in payload]

    async def _signed_get(self, path: str, params: dict) -> object:
        if not self.api_key or not self.api_secret:
            raise BinanceClientError("BINANCE_API_KEY ve BINANCE_API_SECRET gerekli.")

        signed = self._signed_query({**params, "timestamp": int(time.time() * 1000)})
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    f"{self.base_url}{path}?{signed}",
                    headers={"X-MBX-APIKEY": self.api_key},
                )
                resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise BinanceClientError(_extract_error(exc.response)) from exc
        return resp.json()

    def _signed_query(self, params: dict) -> str:
        query = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode(),
            query.encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"{query}&signature={signature}"


def _pick_position(symbol: str, positions: list[dict]) -> dict:
    symbol = symbol.upper()
    active = [
        row for row in positions
        if row.get("symbol") == symbol and Decimal(row.get("positionAmt", "0")) != 0
    ]
    if active:
        return active[0]
    for row in positions:
        if row.get("symbol") == symbol:
            return row
    return {"symbol": symbol, "positionAmt": "0", "breakEvenPrice": "0"}


def _position_summary(symbol: str, position: dict, trades: list[dict]) -> dict:
    qty = Decimal(position.get("positionAmt", "0"))
    abs_qty = abs(qty)
    break_even = Decimal(position.get("breakEvenPrice") or position.get("entryPrice") or "0")
    entry_price = Decimal(position.get("entryPrice", "0"))
    unrealized = Decimal(position.get("unRealizedProfit", "0"))
    margin_asset = position.get("marginAsset", "USDT")
    known_fees = sum(
        Decimal(t.get("commission", "0"))
        for t in trades
        if t.get("commissionAsset") == margin_asset
    )
    net_realized = sum(
        Decimal(t.get("realizedPnl", "0")) for t in trades
    ) - known_fees

    return {
        "symbol": symbol.upper(),
        "base_asset": symbol.upper().removesuffix(margin_asset),
        "quote_asset": margin_asset,
        "position_side": position.get("positionSide", "BOTH"),
        "trade_count": len(trades),
        "buy_count": sum(1 for t in trades if t.get("isBuyer")),
        "sell_count": sum(1 for t in trades if not t.get("isBuyer")),
        "bought_qty": abs_qty if qty > 0 else Decimal("0"),
        "sold_qty": abs_qty if qty < 0 else Decimal("0"),
        "remaining_qty": abs_qty,
        "remaining_cost_quote": abs_qty * break_even,
        "average_cost": entry_price,
        "break_even_price": break_even,
        "exact_commission_quote": known_fees,
        "external_commissions": {},
        "realized_pnl_quote": net_realized,
        "unrealized_pnl_quote": unrealized,
    }


def _normalize_futures_trade(row: dict) -> dict:
    side = str(row.get("side", "")).upper()
    return {
        **row,
        "isBuyer": side == "BUY" or bool(row.get("buyer", False)),
    }

from __future__ import annotations

import hashlib
import hmac
import os
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, Field


BINANCE_BASE_URL = "https://api.binance.com"


class PriceSnapshot(BaseModel):
    symbol: str
    price: Decimal = Field(gt=0)


class TradeFill(BaseModel):
    id: int
    orderId: int
    price: Decimal
    qty: Decimal
    quoteQty: Decimal
    commission: Decimal
    commissionAsset: str
    time: int
    isBuyer: bool
    isMaker: bool
    isBestMatch: bool


class TradeSummary(BaseModel):
    symbol: str
    trade_count: int
    buy_count: int
    sell_count: int
    bought_qty: Decimal
    sold_qty: Decimal
    buy_quote_qty: Decimal
    sell_quote_qty: Decimal
    average_buy_price: Decimal | None
    average_sell_price: Decimal | None
    remaining_qty: Decimal
    remaining_cost_quote: Decimal
    average_cost: Decimal | None
    realized_pnl_quote: Decimal


class BinanceClientError(RuntimeError):
    pass


class BinanceClient:
    def __init__(
        self,
        base_url: str = BINANCE_BASE_URL,
        timeout: float = 10.0,
        api_key: str | None = None,
        api_secret: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key or os.getenv("BINANCE_API_KEY")
        self.api_secret = api_secret or os.getenv("BINANCE_API_SECRET")

    async def get_price(self, symbol: str) -> PriceSnapshot:
        normalized_symbol = symbol.upper().strip()
        if not normalized_symbol:
            raise BinanceClientError("Symbol is required.")

        url = f"{self.base_url}/api/v3/ticker/price"
        params = {"symbol": normalized_symbol}

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _extract_binance_error(exc.response)
            raise BinanceClientError(detail) from exc
        except httpx.HTTPError as exc:
            raise BinanceClientError(f"Binance request failed: {exc}") from exc

        try:
            return PriceSnapshot.model_validate(response.json())
        except ValueError as exc:
            raise BinanceClientError("Binance returned an invalid price response.") from exc

    async def get_my_trades(self, symbol: str, limit: int = 500) -> list[TradeFill]:
        normalized_symbol = symbol.upper().strip()
        if not normalized_symbol:
            raise BinanceClientError("Symbol is required.")

        if not self.api_key or not self.api_secret:
            raise BinanceClientError(
                "BINANCE_API_KEY and BINANCE_API_SECRET are required for account trades."
            )

        safe_limit = max(1, min(limit, 1000))
        params = {
            "symbol": normalized_symbol,
            "limit": safe_limit,
            "timestamp": int(time.time() * 1000),
        }
        signed_query = self._signed_query(params)
        url = f"{self.base_url}/api/v3/myTrades?{signed_query}"
        headers = {"X-MBX-APIKEY": self.api_key}

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _extract_binance_error(exc.response)
            raise BinanceClientError(detail) from exc
        except httpx.HTTPError as exc:
            raise BinanceClientError(f"Binance request failed: {exc}") from exc

        try:
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError
            return [TradeFill.model_validate(item) for item in payload]
        except ValueError as exc:
            raise BinanceClientError("Binance returned an invalid trades response.") from exc

    async def get_trade_summary(self, symbol: str, limit: int = 500) -> TradeSummary:
        normalized_symbol = symbol.upper().strip()
        trades = await self.get_my_trades(normalized_symbol, limit=limit)
        ordered_trades = sorted(trades, key=lambda trade: trade.time)

        buy_count = 0
        sell_count = 0
        bought_qty = Decimal("0")
        sold_qty = Decimal("0")
        buy_quote_qty = Decimal("0")
        sell_quote_qty = Decimal("0")
        remaining_qty = Decimal("0")
        remaining_cost_quote = Decimal("0")
        realized_pnl_quote = Decimal("0")

        for trade in ordered_trades:
            if trade.isBuyer:
                buy_count += 1
                bought_qty += trade.qty
                buy_quote_qty += trade.quoteQty
                remaining_qty += trade.qty
                remaining_cost_quote += trade.quoteQty
                continue

            sell_count += 1
            sold_qty += trade.qty
            sell_quote_qty += trade.quoteQty

            if remaining_qty <= 0:
                continue

            average_cost = remaining_cost_quote / remaining_qty
            qty_to_remove = min(trade.qty, remaining_qty)
            cost_to_remove = qty_to_remove * average_cost
            realized_pnl_quote += (trade.price * qty_to_remove) - cost_to_remove
            remaining_qty -= qty_to_remove
            remaining_cost_quote -= cost_to_remove

        if remaining_qty <= 0:
            remaining_qty = Decimal("0")
            remaining_cost_quote = Decimal("0")

        return TradeSummary(
            symbol=normalized_symbol,
            trade_count=len(trades),
            buy_count=buy_count,
            sell_count=sell_count,
            bought_qty=bought_qty,
            sold_qty=sold_qty,
            buy_quote_qty=buy_quote_qty,
            sell_quote_qty=sell_quote_qty,
            average_buy_price=_safe_divide(buy_quote_qty, bought_qty),
            average_sell_price=_safe_divide(sell_quote_qty, sold_qty),
            remaining_qty=remaining_qty,
            remaining_cost_quote=remaining_cost_quote,
            average_cost=_safe_divide(remaining_cost_quote, remaining_qty),
            realized_pnl_quote=realized_pnl_quote,
        )

    def _signed_query(self, params: dict[str, int | str]) -> str:
        query = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{query}&signature={signature}"


def _safe_divide(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator == 0:
        return None
    return numerator / denominator


def _extract_binance_error(response: httpx.Response) -> str:
    try:
        payload: Any = response.json()
    except ValueError:
        return f"Binance returned HTTP {response.status_code}."

    if isinstance(payload, dict) and payload.get("msg"):
        return str(payload["msg"])

    return f"Binance returned HTTP {response.status_code}."

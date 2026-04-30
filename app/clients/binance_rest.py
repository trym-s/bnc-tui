"""
Adım 5: BinanceRestClient — FastAPI olmadan direkt Binance REST

Eski akış:  TUI → FastAPI → BinanceClient → Binance
Yeni akış:  TUI → BinanceRestClient → Binance

Değişenler vs binance_client.py:
  - get_price() kaldırıldı (WebSocket yapıyor)
  - TTL cache eklendi: aynı sembol 30s içinde tekrar sorulmaz
  - Retry eklendi: ağ hatası olursa 3 kez dener, exponential backoff ile

TTL Cache nedir?
  TTL = Time To Live. Cache'e koyduğun değer N saniye sonra otomatik silinir.
  Sonraki istekte cache'de yoksa Binance'e gidilir, yenisi konur.

  Neden 30s?
    Trade history saniyede değişmez. 30s yeterli.
    0s olursa her render'da Binance'e gidilir → rate limit riski.
    300s olursa yeni trade'ler geç görünür.

Retry nedir?
  Ağ geçici olarak koparsa (timeout, DNS hatası) hemen hata vermek yerine
  birkaç kez daha dener. Kalıcı hatada (401 Unauthorized, 400 Bad Request)
  retry anlamsız olduğu için sadece ağ hatalarında dener.
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

# İki ayrı cache: trade summary ve bakiye farklı TTL'e sahip
_trade_cache: TTLCache = TTLCache(maxsize=10, ttl=30)
_balance_cache: TTLCache = TTLCache(maxsize=1, ttl=30)
_kline_cache: TTLCache = TTLCache(maxsize=20, ttl=30)


class BinanceClientError(RuntimeError):
    pass


class BinanceRestClient:
    """
    Binance REST API ile doğrudan konuşan client.
    Sadece trade summary — fiyat WebSocket'ten geliyor.
    """

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
        """Ham trade listesi (cached). Trade list ekranı için kullanılır."""
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
        """
        Trade summary döner.

        Cache'de varsa Binance'e gitmez, cache'den döner.
        Cache'de yoksa (ilk istek veya 30s geçti) Binance'e gider.

        dict döndürüyoruz çünkü TUI zaten dict bekliyor
        (eski FastAPI endpoint JSON döndürüyordu).
        """
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
        Hesaptaki tüm sıfır olmayan bakiyeleri USD değeriyle döner.

        Akış:
          1. /api/v3/account → tüm coin bakiyeleri
          2. /api/v3/ticker/price → tüm parite fiyatları (tek istek, ~2000 satır)
          3. Her coin için {asset}USDT fiyatına bakarak USD değeri hesapla
          4. Stablecoin'ler (USDT, USDC...) direkt 1:1

        Neden tüm fiyatları tek seferde çekiyoruz?
          Her coin için ayrı ayrı istek atmak yerine (N istek)
          hepsini tek seferde alıp Python'da filtrelemek çok daha verimli.
          Binance bu endpoint'i cache'liyor, hızlı döner.

        Returns:
          ([{"asset": "BTC", "free": Decimal, "locked": Decimal, "usd_value": Decimal}, ...],
           fdusd_usdt_rate)
          USD değerine göre büyükten küçüğe sıralı.
        """
        if "balances" in _balance_cache:
            return _balance_cache["balances"], _balance_cache["fdusd_rate"]

        account, all_prices = await self._fetch_account_and_prices()

        # {symbol: price} dict'i — örn. {"BTCUSDT": Decimal("94000"), ...}
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
                # FDUSD öncelikli, bulamazsa USDT'ye bak
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
        """Account ve tüm fiyatları paralel çeker."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            # İki isteği paralel gönder — biri diğerini beklemiyor
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
        """Signed /api/v3/account isteği."""
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
            raise BinanceClientError("Binance geçersiz kline yanıtı döndürdü.")
        return [_parse_kline_row(symbol, interval, row) for row in payload]

    @retry(
        # Kaç kez denesin? 3 (ilk deneme + 2 retry)
        stop=stop_after_attempt(3),
        # Ne kadar beklesin denemeler arasında?
        # wait_exponential: 1s → 2s → 4s (max 8s)
        wait=wait_exponential(multiplier=1, min=1, max=8),
        # Hangi hatalarda retry? Sadece ağ hataları.
        # 401/403/400 gibi kalıcı hatalarda retry anlamsız.
        retry=retry_if_exception_type((httpx.NetworkError, httpx.TimeoutException)),
    )
    async def _fetch_trades(self, symbol: str, limit: int) -> list[dict]:
        """Binance'den ham trade listesini çeker (retry ile)."""
        if not self.api_key or not self.api_secret:
            raise BinanceClientError(
                "BINANCE_API_KEY ve BINANCE_API_SECRET gerekli."
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
            raise BinanceClientError("Binance geçersiz trade yanıtı döndürdü.")
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
    Ham trade listesinden özet hesaplar.
    Mantık binance_client.py'deki get_trade_summary ile aynı.
    Ayrı fonksiyon çünkü pure hesaplama — HTTP yok, test edilmesi kolay.
    """
    ordered = sorted(trades, key=lambda t: t["time"])

    buy_count = sell_count = 0
    bought_qty = sold_qty = Decimal("0")
    buy_quote = sell_quote = Decimal("0")
    remaining_qty = remaining_cost = realized_pnl = Decimal("0")

    for t in ordered:
        qty = Decimal(t["qty"])
        quote_qty = Decimal(t["quoteQty"])
        price = Decimal(t["price"])

        if t["isBuyer"]:
            buy_count += 1
            bought_qty += qty
            buy_quote += quote_qty
            remaining_qty += qty
            remaining_cost += quote_qty
        else:
            sell_count += 1
            sold_qty += qty
            sell_quote += quote_qty

            if remaining_qty > 0:
                avg_cost = remaining_cost / remaining_qty
                qty_to_remove = min(qty, remaining_qty)
                cost_to_remove = qty_to_remove * avg_cost
                realized_pnl += (price * qty_to_remove) - cost_to_remove
                remaining_qty -= qty_to_remove
                remaining_cost -= cost_to_remove

    if remaining_qty <= 0:
        remaining_qty = remaining_cost = Decimal("0")

    return {
        "symbol": symbol.upper(),
        "trade_count": len(trades),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "bought_qty": remaining_qty and bought_qty,  # always return
        "sold_qty": sold_qty,
        "buy_quote_qty": buy_quote,
        "sell_quote_qty": sell_quote,
        "average_buy_price": _safe_div(buy_quote, bought_qty),
        "average_sell_price": _safe_div(sell_quote, sold_qty),
        "remaining_qty": remaining_qty,
        "remaining_cost_quote": remaining_cost,
        "average_cost": _safe_div(remaining_cost, remaining_qty),
        "realized_pnl_quote": realized_pnl,
    }


def _safe_div(a: Decimal, b: Decimal) -> Decimal | None:
    return None if b == 0 else a / b


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

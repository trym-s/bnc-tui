"""
Adım 1: WebSocket'e İlk Bağlantı

Bu script Binance'in public WebSocket stream'ine bağlanır ve
gelen ham mesajları terminale basar. Başka hiçbir şey yapmaz.

Çalıştırmak için:
    python scratch/ws_hello.py

Binance miniTicker stream formatı:
    {
      "e": "24hrMiniTicker",  # event tipi
      "E": 1234567890123,     # event zamanı (ms)
      "s": "BTCUSDT",         # sembol
      "c": "94123.50",        # son fiyat (close)
      "o": "93000.00",        # açılış fiyatı (open, 24h önce)
      "h": "95000.00",        # 24h en yüksek
      "l": "92500.00",        # 24h en düşük
      "v": "12345.678",       # baz hacim (BTC)
      "q": "1160000000.00"    # quote hacim (USDT)
    }

Neden miniTicker?
  - ticker: 70+ alan, çok veri
  - miniTicker: 9 alan, ihtiyacımız olan her şey var
  - bookTicker: sadece bid/ask, fiyat yok
"""

import asyncio
import json
import websockets  # pip install websockets


STREAM_URL = "wss://stream.binance.com:9443/ws/btcusdt@miniTicker"


async def main() -> None:
    print(f"Bağlanıyor: {STREAM_URL}")
    print("Çıkmak için Ctrl+C\n")

    # websockets.connect() bir context manager döner.
    # `async with` bloğundan çıkınca bağlantı otomatik kapanır.
    async with websockets.connect(STREAM_URL) as ws:
        print("Bağlantı kuruldu. Mesajlar geliyor...\n")

        # ws.__aiter__() → gelen her mesaj için bir döngü
        # Binance her fiyat güncellemesinde bir JSON string gönderir
        async for raw_message in ws:
            data = json.loads(raw_message)

            symbol = data["s"]
            price = data["c"]   # close = son işlem fiyatı
            high = data["h"]
            low = data["l"]

            print(f"{symbol}  fiyat={price}  24h_high={high}  24h_low={low}")


if __name__ == "__main__":
    asyncio.run(main())

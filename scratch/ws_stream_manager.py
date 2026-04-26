"""
Adım 2 Test: StreamManager'ı dene

Çalıştır:
    python scratch/ws_stream_manager.py

Reconnect'i test etmek için:
    Çalışırken internet bağlantını kes, birkaç saniye bekle, aç.
    Log'da şunu göreceksin:
      WARNING  Ağ hatası: ...
      INFO     2.0s sonra yeniden bağlanılacak...
      INFO     Bağlanıyor: wss://...
      INFO     Bağlantı kuruldu
"""

import asyncio
import logging
import sys

# Logları görmek için — normalde uygulamada daha temiz yapılır
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s %(message)s",
    stream=sys.stdout,
)

# app klasöründen import edebilmek için path ayarı
sys.path.insert(0, ".")

from app.streams.binance_ws import StreamManager


async def main() -> None:
    manager = StreamManager("BTCUSDT")
    print("StreamManager başlatıldı. Ctrl+C ile durdur.\n")

    async for msg in manager.messages():
        print(
            f"[{manager.state.name}] "
            f"{msg.symbol}  "
            f"fiyat={msg.price}  "
            f"24h_high={msg.high_24h}  "
            f"24h_low={msg.low_24h}"
        )


if __name__ == "__main__":
    asyncio.run(main())

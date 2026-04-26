from __future__ import annotations

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from dotenv import load_dotenv

from app.binance_client import BinanceClient, BinanceClientError, PriceSnapshot, TradeSummary


load_dotenv()

app = FastAPI(title="bnc-tui API")
binance = BinanceClient()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/price/{symbol}", response_model=PriceSnapshot)
async def price(symbol: str) -> PriceSnapshot:
    try:
        return await binance.get_price(symbol)
    except BinanceClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/trades/{symbol}/summary", response_model=TradeSummary)
async def trade_summary(
    symbol: str,
    limit: int = Query(default=500, ge=1, le=1000),
) -> TradeSummary:
    try:
        return await binance.get_trade_summary(symbol, limit=limit)
    except BinanceClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def main() -> None:
    uvicorn.run("app.api:app", host="127.0.0.1", port=8000, reload=True)


if __name__ == "__main__":
    main()

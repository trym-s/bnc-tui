# bnc-tui

Minimal Binance dashboard starter using FastAPI and Textual.

Current scope:

- show the live price for one trading pair
- summarize your executed Spot trades for that pair

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Run

Account trades use Binance signed endpoints. Create a read-only API key and put
it in `.env`:

```bash
cp .env.example .env
```

Then edit `.env`:

```dotenv
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
```

Terminal 1:

```bash
uvicorn app.api:app --reload
```

Terminal 2:

```bash
python -m app.tui --symbol BTCUSDT
```

Optional settings:

```bash
python -m app.tui --symbol ETHUSDT --api-url http://127.0.0.1:8000 --refresh 2
```

## API

```bash
curl "http://127.0.0.1:8000/price/BTCUSDT"
curl "http://127.0.0.1:8000/trades/BTCUSDT/summary"
```

Price uses Binance's public `/api/v3/ticker/price` endpoint. Trade summaries use
the signed `/api/v3/myTrades` endpoint.

The first trade summary uses a simple average-cost method:

- buys increase open quantity and cost
- sells reduce open quantity at the current average cost
- commissions are shown by Binance in each trade but are not included in the
  first cost-basis calculation yet

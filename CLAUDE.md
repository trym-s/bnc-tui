# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env  # fill in BINANCE_API_KEY and BINANCE_API_SECRET
```

## Commands

```bash
# Run the TUI
bnc-tui --symbol BTCUSDT

# Run all tests
python -m unittest discover tests

# Run a single test file
python -m unittest tests.test_streams

# Run a single test case
python -m unittest tests.test_streams.EventBusTest.test_broadcasts_to_each_subscriber
```

## Architecture

The app replaced a FastAPI middleman with a direct WebSocket + REST architecture. The README still describes the old flow — ignore it.

**Data flow:**
```
Binance WebSocket → StreamManager → EventBus[PriceEvent] → PriceApp._listen_prices() → dashboard (instant)
                  → EventBus[ConnectionEvent] → PriceApp._listen_connection() → status label

Binance REST → BinanceRestClient (TTL-cached, retried) → PriceApp._refresh_trades_loop() → dashboard (every 30s)
```

**Key components:**

- `app/streams/event_bus.py` — Generic asyncio pub/sub bus. Each subscriber gets its own `asyncio.Queue`. When full, drops oldest (not newest). Used as `async with bus.subscribe() as queue`.
- `app/streams/binance_ws.py` — `StreamManager` connects to Binance `miniTicker` WebSocket, publishes `PriceEvent` and `ConnectionEvent` to their respective buses. Handles reconnect with exponential backoff (1s→30s). Auto-reconnects after Binance's 23h connection limit.
- `app/clients/binance_rest.py` — `BinanceRestClient` fetches trade history (signed `/api/v3/myTrades`) and account balances. Both are TTL-cached (30s). Trade fetches retry on network errors only (not on 4xx). `_calculate_summary()` is a pure function that implements FIFO average-cost PnL.
- `app/tui/app.py` — `PriceApp` (Textual `App`). Wires the buses and REST client together. Uses `run_worker()` for all background coroutines so Textual manages their lifecycle. `_render_dashboard()` always uses the latest cached state from both WebSocket and REST paths.
- `app/tui/screens/trade_list.py` — Modal screen for viewing trades and assigning them to named groups. Opens with `t`, closes with `ESC`.
- `app/storage/groups.py` — `GroupStore` persists trade-to-group assignments as JSON at `~/.bnc-tui/groups.json`. Trade IDs are Binance's stable integer IDs.
- `app/models/events.py` — Shared frozen dataclasses (`PriceEvent`, `ConnectionEvent`) and `ConnectionState` enum. Both `StreamManager` and `PriceApp` depend on these, not on each other.
- `app/tui/palette.py` — Centralized color palette (`P`). Rich markup uses `P.positive` etc. as hex strings directly; Textual CSS uses `$background`, `$muted` etc. via `get_css_variables()` override in `PriceApp`.

**Dependency graph (no cycles):**
```
models/events ← streams/* ← tui/app
                           ← clients/*
storage/groups ← tui/screens/*
tui/palette ← tui/*
```

## Environment

`BINANCE_API_KEY` and `BINANCE_API_SECRET` are read from `.env` (via `python-dotenv`) or the environment. The REST client reads them from `os.getenv` at instantiation time. Price streaming via WebSocket uses only a public endpoint and requires no credentials.

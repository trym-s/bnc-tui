# Backtest Strategies

This covers everything you can configure in `bnc-backtest`. If you just want to run something quickly, the examples at the bottom of each section are a good starting point.

All backtests are long-only: a BUY signal opens one position, a SELL signal closes it. Only one position can be open at a time. If a position is still open when the data runs out, it's closed on the last candle.

## Picking a candle range

You have two options: a fixed number of recent candles, or a specific date range.

**Recent candles** (default):

```bash
bnc-backtest --symbol BTCFDUSD --interval 4h --limit 500
```

This takes the 500 most recent 4h candles from the local cache. If you haven't fetched them yet, add `--fetch`.

**Date range:**

```bash
bnc-backtest --symbol BTCFDUSD --interval 4h \
  --start 2024-01-01 --end 2025-01-01 --fetch
```

`--start` is inclusive, `--end` is exclusive, both treated as UTC. When `--start` and `--end` are given, `--limit` is ignored.

`--fetch` always downloads only what's missing — candles already in the local SQLite cache are reused without hitting the API.

## Parameters that apply to every strategy

| Parameter | Default | What it does |
|-----------|--------:|--------------|
| `--symbol` | `BTCFDUSD` | Trading pair |
| `--interval` | `1m` | Candle interval: `1m`, `15m`, `1h`, `4h`, `1d` |
| `--limit` | `500` | Number of recent candles (ignored when `--start`/`--end` are set) |
| `--cash` | `1000` | Starting quote balance |
| `--order-size` | `100` | Quote amount spent per BUY |
| `--fee-rate` | `0.001` | Fee applied on both entry and exit (0.1% = Binance standard) |
| `--fetch` | off | Download missing candles before running |

## Strategy: MA Cross (`ma-cross` / `sma`)

Buys when the fast moving average crosses above the slow one; sells on the reverse cross.

Supports three average types: `sma`, `ema`, `wma`.

| Parameter | Default | What it does |
|-----------|--------:|--------------|
| `--ma-type` | `sma` | Average type: `sma`, `ema`, `wma` |
| `--short-window` | `20` | Fast average period |
| `--long-window` | `100` | Slow average period |

```bash
# SMA 20/100
bnc-backtest --symbol BTCFDUSD --interval 4h \
  --strategy ma-cross --ma-type sma --short-window 20 --long-window 100

# EMA 12/50 — reacts faster than SMA, more signals
bnc-backtest --symbol BTCFDUSD --interval 4h \
  --strategy ma-cross --ma-type ema --short-window 12 --long-window 50
```

`--strategy sma` is an alias for `--strategy ma-cross --ma-type sma`.

## Strategy: Bollinger Band Reversion (`bollinger`)

Looks for a lower-band wick followed by a bullish reclaim, then exits when price reaches the middle or upper band.

**Entry conditions (all must be true):**
1. Previous candle's low touched or broke through the lower band
2. Current candle closes back above the lower band
3. Current candle is bullish (`close > open`)
4. RSI confirmation passes (if enabled)

**Exit:** current close reaches the middle band (`--bb-exit middle`, default) or upper band (`--bb-exit upper`).

| Parameter | Default | What it does |
|-----------|--------:|--------------|
| `--bb-period` | `30` | Lookback period for the bands |
| `--bb-std` | `3` | Standard deviation multiplier (wider = fewer signals) |
| `--bb-exit` | `middle` | Exit at `middle` or `upper` band |

```bash
bnc-backtest --symbol BTCFDUSD --interval 4h \
  --strategy bollinger --bb-period 30 --bb-std 3 --bb-exit middle
```

### RSI confirmation (optional)

RSI can be added as an entry filter for the Bollinger strategy. It has no effect on the exit.

| Parameter | Default | What it does |
|-----------|--------:|--------------|
| `--rsi-period` | `14` | RSI lookback period |
| `--rsi-confirm` | `off` | Mode: `off`, `rising`, `turn-up`, `cross-above`, `below` |
| `--rsi-level` | `30` | Threshold used by `cross-above` and `below` |

| Mode | Passes when |
|------|-------------|
| `off` | Always (RSI ignored) |
| `rising` | RSI is higher than the previous bar |
| `turn-up` | RSI was falling last bar and rose this bar |
| `cross-above` | RSI crossed above `--rsi-level` |
| `below` | RSI is at or below `--rsi-level` |

```bash
# Turn-up: RSI had been falling and just ticked up — good for catching early reversals
bnc-backtest --symbol BTCFDUSD --interval 4h \
  --strategy bollinger --bb-period 30 --bb-std 3 \
  --rsi-confirm turn-up --rsi-period 14

# Cross-above: RSI comes from below 30 and crosses it — classic oversold signal
bnc-backtest --symbol BTCFDUSD --interval 4h \
  --strategy bollinger --bb-period 30 --bb-std 3 \
  --rsi-confirm cross-above --rsi-level 30
```

## Indicators in the codebase not yet usable as strategies

These are implemented in `app/backtest/strategies.py` and can be used from Python, but aren't wired into the CLI yet.

### ATR (Average True Range)

Measures candle-to-candle volatility. Possible uses: dynamic stop distance (`entry_price - ATR * N`), skipping trades when the market is too quiet or too wild, position sizing.

### MACD

```
macd_line    = EMA(close, 12) - EMA(close, 26)
signal_line  = EMA(macd_line, 9)
histogram    = macd_line - signal_line
```

Possible uses: MACD crossing above its signal line, histogram turning positive, divergence from price.

### Volume Profile

Not implemented. The SQLite cache stores per-candle OHLCV totals, not tick-level data. Real volume profile needs trade-level data (e.g. Binance `aggTrades`), aggregated into price buckets. Approximating from OHLCV candles is possible but misleading.

## Current limitations

- Long-only. No short positions, no hedging.
- One position at a time.
- RSI is only available as a Bollinger entry filter, not as a standalone strategy.
- ATR and MACD aren't wired to any strategy CLI flags yet.
- No slippage modelling — entries and exits execute at candle close.
- No partial fills or position sizing based on volatility.

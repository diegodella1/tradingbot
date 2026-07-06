# Polymarket BTC Up/Down Bot

Safety-first Python framework for the recurring Polymarket Bitcoin Up or Down 5 minute and 15 minute markets.

This project can discover active markets, validate UP/DOWN token mappings, read CLOB order books, consume BTC spot prices, evaluate pluggable strategies, simulate paper orders, log events to SQLite, and expose CLI health checks.

It does not guarantee profit, bypass access restrictions, bypass KYC, bypass sanctions controls, bypass rate limits, scrape the Polymarket frontend, or enable live trading by default.

## Safety Defaults

- Paper trading is the default path.
- `NoTradeStrategy` is the default strategy and always returns `HOLD`.
- Experimental strategy requires `ENABLE_EXPERIMENTAL_STRATEGY=true`.
- Live mode requires `ENABLE_LIVE_TRADING=true`, complete credentials, geoblock pass, explicit CLI confirmation, and additional live-order implementation review.
- Any uncertainty around market discovery, token mapping, stale feeds, geoblock state, liquidity, spread, clock skew, or kill switch blocks trading.

## Legal And Risk Notice

Prediction markets and derivatives-like products can be restricted by jurisdiction. Use only where permitted and only with your own compliant account. Never bypass geoblocking, KYC, sanctions, or platform restrictions.

Trading can lose money. This software is not financial advice and is not a profit system.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

For live SDK experimentation only:

```bash
pip install -e ".[live]"
```

Copy `.env.example` to `.env` and fill only the values you need. Do not commit secrets.

## Environment

Important values:

- `ENABLE_LIVE_TRADING=false`
- `ENABLE_EXPERIMENTAL_STRATEGY=false`
- `MAX_POSITION_USDC=5`
- `MAX_MARKET_POSITION_USDC=5`
- `MAX_DAILY_LOSS_USDC=10`
- `MAX_SPREAD_CENTS=4`
- `MIN_ORDERBOOK_LIQUIDITY_USDC=50`
- `KILL_SWITCH_FILE=./KILL_SWITCH`

## Commands

Discover active verified markets:

```bash
python -m bot.cli discover
```

Run one paper cycle:

```bash
python -m bot.cli paper --max-cycles 1
```

Run continuous real-market paper mode:

```bash
python -m bot.cli paper
```

Check health:

```bash
python -m bot.cli health
```

Index/search local RAG notes:

```bash
python -m bot.cli rag-index
python -m bot.cli rag-search "risk"
python -m bot.cli learn --note "No market found; discovery rejected expired markets" --tags paper,discovery
```

Generate deterministic learning recommendations from paper results:

```bash
python -m bot.cli learning-report
```

Backtest recorded signals, with out-of-sample gate sweep (model trained on the first 80% of markets, gates evaluated on the held-out 20%):

```bash
python -m bot.cli backtest --buckets --sweep
```

Train the calibrated probability model:

```bash
python -m bot.cli calibrate
```

Refetch closed markets without a verified winner (also runs automatically inside the paper loop every `OUTCOME_BACKFILL_CYCLES`):

```bash
python -m bot.cli backfill-outcomes
```

Delete market snapshots and BTC ticks older than `DATA_RETENTION_DAYS` (also runs automatically once per day inside the paper loop):

```bash
python -m bot.cli prune --vacuum
```

Compare maker (post at bid, zero fee) vs taker EV on settled paper trades:

```bash
python -m bot.cli maker-sim
```

## Dashboard auth

All `/api/*` dashboard endpoints require a Bearer token. Set `DASHBOARD_TOKEN` in `.env`; without it every API call returns 403 (fail closed). Open the dashboard once with `?token=<value>` in the URL (stored in the browser) or send `Authorization: Bearer <value>`.

Check wallet readiness without placing live orders:

```bash
python -m bot.cli wallet-check
```

Attempt live mode:

```bash
ENABLE_LIVE_TRADING=true python -m bot.cli live
```

The live broker adapter can create/post guarded limit orders through the SDK, but the CLI scaffold does not start a long-running live loop yet.

## Paper Policy

- Paper uses real Polymarket/Gamma/CLOB and BTC data.
- Paper does not use mock markets outside tests.
- Default paper bankroll is `$100`.
- Default paper trade size is `$1`.
- Paper mode estimates Polymarket crypto taker fees using the documented formula `shares * fee_rate * price * (1 - price)` with `PAPER_TAKER_FEE_RATE=0.07`.
- Maximum open markets is `1`.
- Filled positions are held to resolution; the bot does not simulate early exits by default.
- If no real verified BTC Up/Down 5m/15m market is available, the bot records `no_market` and does not simulate a trade.
- Learning recommendations are report-only. They use settled paper outcomes, fees, price buckets, timeframe results, and RAG snippets to suggest safer config changes, but they never auto-change bot parameters.

## Kill Switch

Create the configured kill switch file:

```bash
touch KILL_SWITCH
```

Risk checks will block new execution while the file exists.

## SQLite Logs

The database defaults to `./bot.sqlite3`.

```bash
sqlite3 bot.sqlite3 ".tables"
sqlite3 bot.sqlite3 "select * from signals order by id desc limit 10;"
sqlite3 bot.sqlite3 "select * from orders order by created_at desc limit 10;"
```

Tables include `markets`, `market_snapshots`, `btc_ticks`, `signals`, `orders`, `fills`, `positions`, `pnl`, `risk_events`, and `health_events`.

## Implementation Notes

Official Polymarket docs referenced:

- Trading overview recommends SDK clients and currently lists `py-clob-client-v2` for Python.
- Market WebSocket endpoint: `wss://ws-subscriptions-clob.polymarket.com/ws/market`.
- Gamma and CLOB API references are used for market discovery and order book snapshots.
- Geoblock API is treated as a live-trading blocker if unavailable or blocked.

## Known Limitations

- Paper mode currently runs one cycle, useful for safe validation and cron-style iteration.
- WebSocket user channel reconciliation is scaffolded but not fully integrated.
- Long-running live loop orchestration is intentionally minimal; review it before unattended live use.
- Market discovery depends on public API field names and rejects ambiguous mappings.
- No strategy has been validated for profitability.

## Suggested Next Steps

- Add long-running supervisor loop with graceful shutdown.
- Persist market snapshots and BTC ticks during paper mode.
- Integrate authenticated user WebSocket for live reconciliation.
- Add exchange heartbeat/cancel-all watchdog before enabling live posting.
- Backtest experimental strategy before any live use.

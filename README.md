# pump.fun sniper research

A dry-run sniper for new [pump.fun](https://pump.fun) tokens plus a research toolkit that answers one
question honestly: **can a retail bot make money sniping pump.fun launches?**

Short answer, based on ~26,000 tokens recorded in October 2026: **no**. Every strategy family tested
loses money out of sample, including variants that looked very profitable before a subtle modelling
bug was found. The full write-up is in [docs/REPORT.md](docs/REPORT.md).

No real transactions are ever sent. The bot only simulates trades against real bonding-curve
reserves, with latency, slippage and all fees.

## What's inside

- `main.py` — recorder and paper trader. It listens for new tokens on the free PumpPortal websocket.
  After 10 minutes it downloads each token's full trade history from `swap-api.pump.fun` and saves it
  to `recorded/`, then replays the strategy as if it had traded live. Tokens heading for graduation
  are re-fetched after 40 minutes and 3 hours, including post-migration PumpSwap trades.
- `sim.py` — bonding-curve maths, cost model (pump.fun fee, PumpPortal fee, priority and base fees,
  token-account rent), position logic and statistics.
- `backtest.py` — loads recorded data and rebuilds the curve state after every trade.
- `research.py` — entry features, flexible exits, filter × exit search, robustness checks, fast-flip
  and curve-progress strategies.
- `research_migration.py` — strategies around graduation to PumpSwap.
- `report.py` — summary of paper trades.
- `backfill.py` — re-downloads tokens that `main.py` failed to fetch, within the API rate limit.
- `legacy/` — the first prototype's scripts for the old `frontend-api-v3` (no longer works).

## Methodology in one paragraph

Data is split **chronologically**: the first 60% of tokens are used to choose parameters, and the last
40% (holdout) are used once to evaluate them. Wallet reputation and creator history are computed on a
rolling basis, using only tokens whose data was complete before the evaluated token was created.
Results come with 95% bootstrap intervals. Every promising result is stress-tested by dropping the
best trades, splitting the holdout by time, and varying latency, fees, position size and neighbouring
parameters.

## Running

```
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env              # optional: Telegram notifications, parameter overrides

.venv/bin/python main.py --no-tg  # record and paper-trade (needs access to pump.fun APIs)
.venv/bin/python report.py        # paper trading summary

.venv/bin/python research.py features --price-only
.venv/bin/python research.py combo --price-only
.venv/bin/python research.py flip --price-only
.venv/bin/python research.py progress --price-only
.venv/bin/python research_migration.py
```

Every parameter in `config.py` can be overridden with `--set key=value` or an upper-case
environment variable.

## Practical notes

- PumpPortal streams token creations for free. Per-token trade streams need an API key funded with
  at least 0.02 SOL, so this project fetches trade history from `swap-api.pump.fun` instead.
- The effective limit of `swap-api.pump.fun` is about **20 requests per minute per IP**, although its
  headers advertise 1000.
- pump.fun is geo-restricted in some regions: the API answers 403 with a redirect to `/blocked`.

## License

MIT, see [LICENSE](LICENSE).

# Sniping pump.fun in 2026: why every simple strategy loses, and the Mayhem-curve trap

*Research log, October 8–12, 2026. All numbers are simulated trades on recorded on-chain data.
No real money was traded.*

## TL;DR

- About **26,000 new pump.fun tokens** were recorded over four days, together with every bonding-curve
  trade in their first 10 minutes. Tokens heading for graduation were tracked for up to 3 hours,
  including their trades on PumpSwap.
- Every strategy family tested **loses money on the holdout** (the last 40% of tokens by time, never
  used for tuning):
  - entering in the first 0–60 s with any combination of filters and exits;
  - fast flips with 3–60 s holds;
  - entering by curve progress;
  - buying before graduation and selling after;
  - buying right after graduation.
- About **one third of new tokens are launched in pump.fun's "Mayhem mode"**. Their virtual SOL
  reserves change *between* trades, so the usual `x·y=k` reconstruction from trade volumes is wrong for
  them. This bug produced a "strategy" with **+15–18% ROI out of sample** that survived every robustness
  check — and disappeared once reserves were rebuilt from the API price.
- Costs alone kill small positions. With a 0.001 SOL entry, the token-account rent (~0.00204 SOL) means
  the price has to rise **~230%** just to break even. At 0.1 SOL the break-even move is ~7%.

## 1. Data collection

| Source | What | Cost / limit |
|---|---|---|
| PumpPortal websocket `subscribeNewToken` | token creation events (mint, creator, dev buy, initial reserves) | free |
| PumpPortal `subscribeTokenTrade` | live trades | needs an API key funded with ≥ 0.02 SOL — not used |
| `swap-api.pump.fun/v2/coins/{mint}/trades` | full trade history with `priceSol`, `fillPriceSol`, slot index | free, **~20 req/min per IP** (headers claim 1000) |

The recorder waits 10 minutes after each creation, downloads the token's history, and replays the
strategy on it as if trading live. Decisions only use data that would have been visible at decision
time. Tokens whose curve is at least 40% sold after 10 minutes are re-fetched after 40 minutes and,
if they graduated, after 3 hours. Only new trades are fetched, including those on PumpSwap
(`program: "pump_amm"`).

The API cursor has the form `<slotIndexId>-<unix ms>`. A cursor of `x-<unix ms>` returns trades
*before* that moment, which allows fetching only the first minutes of an old token's history.
This is what made it possible to backfill ~4,000 tokens within the rate limit.

## 2. Simulation model

- **Curve state after every trade.** Tokens in the curve are tracked from trade volumes (starting at
  1.073B virtual tokens). Virtual SOL = API price × virtual tokens (see section 4 for why).
  Graduation happens when 793.1M tokens are sold.
- **Our own trades** go through the constant-product formula on that state, so price impact is
  included.
- **Costs:**
  - pump.fun curve fee 1.25%, PumpPortal Lightning fee 1% per side;
  - priority fee 0.00005 SOL and base fee 0.000005 SOL per transaction;
  - token-account rent 0.00204 SOL per buy, lost unless the account is closed;
  - PumpSwap fee 0.3%.

  A "lean" scenario removes the PumpPortal fee (own transactions) and reclaims the rent.
- **Execution.** Signal → fill after 800 ms (0–3,000 ms tested). The buy fails if the price moved
  more than 10% (slippage). Exits are also delayed by the latency.
- **Validation.**
  - Reconstructed prices match API prices on standard tokens with a median error of 0.00%.
  - The API trade list matches successful on-chain transactions one-to-one (73 of 73 on a sampled
    token).

## 3. Methodology

- **Chronological split.** The first 60% of tokens are used for selection, the last 40% are the
  holdout. Each chosen configuration is evaluated on the holdout once.
- **No lookahead in features.** Wallet reputation (+1 per token where a wallet ended in profit, −1 where
  it lost) and creator history are built only from tokens whose data was complete before the evaluated
  token was created.
- **Selection by lower confidence bound.** Among the top configurations on the selection set, the one
  with the best 2.5% bootstrap percentile is picked, not the highest mean.
- **Robustness checks** for anything positive:
  - drop the top 1/3/5/10 trades;
  - split the holdout into quarters;
  - live-recorded vs backfilled data;
  - latency from 400 ms to 3 s, fees +1% per side, position size from 0.02 to 1 SOL;
  - neighbouring parameter values.

## 4. The Mayhem-curve trap

The first full search over 27,000 combinations of filters, exits and entry windows found a clear
winner:

> wait 30 s · dev buy ≤ 0.1 SOL · early buyers' reputation ≥ 20 · dev hasn't sold · TP +500%, hold 9 min

It produced **+18.3% ROI on the holdout** (95% CI +8.2…+29.5%). It stayed positive:

- without its 5 best trades;
- in every holdout quarter;
- on both live and backfilled data;
- at 3 s latency, with +1% fees and with 1 SOL positions.

By every usual test it looked real.

Inspecting the top trades told a different story. Their **curve reconstruction error was 90–99%**,
while the median over all tokens was 0.00%, which had hidden the problem. In fact **32% of tokens**
did not follow the standard curve. A buy of 0.024 SOL moved their price by +20%, and some trades
executed *below* the launch price — impossible on a standard pump.fun curve.

Decoding the pump.fun trade events of these transactions explained it:

- they go through a separate program, `MAyhSmzX…` (pump.fun's "Mayhem mode");
- in the event logs, virtual **token** reserves change only with trades, but virtual **SOL** reserves
  jump between trades: for example 52.6 → 63.8 SOL around a 0.02 SOL buy;
- the API's `priceSol` matches the on-chain reserves exactly.

So `x·y=k` from volumes is wrong for these tokens. Rebuilding reserves as `price × virtual tokens`
fixes it for both curve types. With exact reserves, the same "winning" strategy on standard-curve
tokens collapses to noise, and Mayhem tokens turn out to be the **worst** segment:

| Segment (holdout, enter at 30 s, TP +500%, hold 9 min) | ROI |
|---|---|
| all tokens | −7.5% |
| standard curve only | −11.7% |
| Mayhem only | −17.0% |

Mayhem tokens can be recognised early: if the first trades don't fit the standard curve (median
error > 2%), the token is Mayhem. This early check misclassified 1 of ~2,900 tokens.

**Lesson:** a result that survives every statistical robustness test can still be a modelling
artefact. Always inspect the individual trades behind the best numbers.

## 5. Results by strategy family

All figures are holdout ROI per SOL invested at 0.1 SOL per trade, on tokens with exact reserves,
unless noted otherwise.

**Early entry, filters and exits** (7,966 tokens)
- Best of 27,648 filter × exit × window combinations:
  - on the selection set: +14…+16% (lower CI bound already −6%);
  - on the holdout: **−20%**.
- **Trailing stops hurt in every configuration.** Memecoins are too noisy, and the trail exits before
  the rare large move.
- The best exit family is a distant take-profit (+200…+500%) with no trail and a long hold. Even so,
  the spread between the best and the worst exit is only ~3 percentage points.
- Weak signals that do exist:
  - small dev buys (0.05–0.1 SOL) are slightly better;
  - "hype" launches (many buyers, more than 3–4 SOL volume, price already up in the first seconds)
    are much worse: −12…−16%;
  - a dev who sells early is a bad sign.

  None of these overcomes costs.

**Fast flip** — enter at creation, exit after 3–60 s
- Current costs: **−6…−7%** at any latency.
- Lean costs, even with **same-block entry** (0 ms): **−1.9%** (CI −3.1…−0.6%).
- Latency barely matters: in the first seconds the average price is flat, and the pump.fun fee
  (2 × 1.25%) is the whole loss.
- No creation-time feature (dev buy size, creator history) helps consistently.

**Entry by curve progress** — buy when 20…90% of the curve is sold
- **−4…−15%** with lean costs. The closer to graduation, the worse.

**Graduation** (924 tracked tokens, 132 graduations)
- Buy on the curve before graduation, sell on PumpSwap after: **−12…−33%**. Insiders and bundles
  sell into late curve buyers. In one case 149 SOL was bought in the very first PumpSwap transaction.
- Buy on PumpSwap right after graduation:
  - best on the selection set: +7.5% (lean +11.9%) with a 10 s hold;
  - holdout: **−7.7%** (lean −3.9%).

  This is the typical signature of noise.

## 6. Why it doesn't work

1. **New tokens drift down on average.** Most launches are abandoned within minutes.
2. **Costs are large relative to the edge.**
   - ~2.5% in pump.fun fees per round trip, +2% with PumpPortal Lightning;
   - fixed transaction fees;
   - for small positions, token-account rent that is many times the expected edge.
3. **The profitable flow belongs to faster or better-informed participants.**
   - Creators and their bundles buy in the creation transaction itself.
   - Professional snipers land in the same block via Jito bundles and staked connections.

   A retail bot reacting in 0.4–1 s is structurally on the other side of their trades. Even the
   simulated same-block entry was negative.

## 7. Limitations

- About four days of data from a single market regime.
- Only ~1 day of PumpSwap data (132 graduations), so the post-graduation results have wide intervals.
- Between trades, Mayhem tokens' price can jump; the simulation uses the last known state.
- The simulation ignores failed transactions, MEV against our own orders and the market impact of other
  bots reacting to us. All of these would make real results worse, not better.
- Fees are modelled at fixed rates; pump.fun has used dynamic fees at times.

## 8. Reproducing

```
.venv/bin/python main.py --no-tg                       # collect data (needs pump.fun API access)
.venv/bin/python research.py features --price-only     # which early features matter
.venv/bin/python research.py exits --price-only        # exit rules
.venv/bin/python research.py combo --price-only        # filters × exits, holdout check
.venv/bin/python research.py check --price-only        # robustness checks for one strategy
.venv/bin/python research.py flip --price-only         # fast flip
.venv/bin/python research.py progress --price-only     # entry by curve progress
.venv/bin/python research_migration.py                 # graduation strategies
```

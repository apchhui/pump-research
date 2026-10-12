"""Прогон стратегии снайпера по историческим данным.

Понимает два формата:
  mints/     — история сделок из API pump.fun (data_collector.py); резервы кривой
               восстанавливаются по x*y=k из объёмов сделок
  recorded/  — живая запись main.py: резервы приходят прямо из PumpPortal

    python backtest.py                                  # параметры из config.py / .env
    python backtest.py --data recorded --set take_profit_pct=80 --set stop_loss_pct=-25
    python backtest.py --amounts 0.001,0.01,0.1,1
    python backtest.py --data recorded --search         # подбор фильтров с проверкой вне выборки
"""
import argparse
import bisect
import itertools
import json
import os
import statistics
from dataclasses import dataclass, field, replace

from config import Config, parse_sets
from sim import (INITIAL_V_TOKENS, K, Position, TradeResult, build_reputation, migrated,
                 early_features, format_summary, passes_filters, price, simulate_buy,
                 simulate_sell, summarize, tx_cost)

SLOT_SEC = 0.4
TOKEN_DECIMALS = 6
SOL_DECIMALS = 9


@dataclass
class Token:
    mint: str
    created_t: float
    dev_buy: float
    times: list[float]                       # время каждого состояния кривой
    states: list[tuple[float, float]]        # (v_sol, v_tokens) после события
    trades: list[dict]                       # сделки после создания: t, user, is_buy, sol
    rep_trades: list[dict] = field(default_factory=list)  # + покупка дева, для репутации
    data_end: float = float("inf")
    curve_error: float = 0.0
    creator: str = ""
    trade_errs: list[float] = field(default_factory=list)  # расхождение каждой сделки со стандартной кривой
    has_price: bool = False                                 # резервы восстановлены по цене из API


def build_token(mint: str, rows: list[dict], dev_in_rows: bool, dev_buy: float,
                created_t: float | None = None, data_end: float = float("inf")) -> Token:
    """rows — сделки по порядку исполнения: slot, ts, user, is_buy, sol, tokens (SOL и токены в UI-единицах).
    Резервы кривой восстанавливаются по x*y=k, время внутри секунды — по номеру слота."""
    base_t, base_slot = rows[0]["ts"], rows[0]["slot"]
    times, states, trades, errors, trade_errs = [], [], [], [], []
    v_tokens = INITIAL_V_TOKENS
    # Если есть цена из API, SOL-резервы берём из неё: у токенов режима Mayhem виртуальные SOL-резервы
    # меняются и между сделками, x*y=k по объёмам для них не работает. Токены в кривой — по сделкам.
    has_price = all(r.get("price") for r in rows)
    for tr in rows:
        prev_v_sol = K / v_tokens
        v_tokens += -tr["tokens"] if tr["is_buy"] else tr["tokens"]
        delta = abs(K / v_tokens - prev_v_sol)
        err = abs(tr["sol"] / delta - 1) if delta else 0.0
        if delta:
            errors.append(err)
        trade_errs.append(err)
        t = round(base_t + (tr["slot"] - base_slot) * SLOT_SEC, 3)  # без round 2 * 0.4 > 0.8
        times.append(t)
        states.append((tr["price"] * v_tokens if has_price else K / v_tokens, v_tokens))
        trades.append({"t": t, "user": tr["user"], "is_buy": tr["is_buy"], "sol": tr["sol"]})
    if dev_in_rows:
        created_t, early = times[0], trades[1:]
    else:
        # дев не покупал: первое состояние — пустая кривая в момент создания
        created_t = min(created_t, times[0])  # created_t — локальные часы, могут спешить
        times.insert(0, created_t)
        states.insert(0, (K / INITIAL_V_TOKENS, INITIAL_V_TOKENS))
        early = trades
    return Token(mint, created_t, dev_buy, times, states, early, rep_trades=trades,
                 data_end=data_end, curve_error=statistics.median(errors) if errors else 0.0,
                 trade_errs=trade_errs, has_price=has_price)


def load_pumpfun(mint: str, raw: list[dict]) -> Token:
    """Формат старого frontend-api-v3 (mints/)."""
    # API отдаёт от новых к старым; reversed + стабильная сортировка сохраняет порядок внутри транзакции
    ordered = sorted(reversed(raw), key=lambda t: (t["slot"], t["tx_index"]))
    rows = [{"slot": t["slot"], "ts": t["timestamp"], "user": t["user"], "is_buy": t["is_buy"],
             "sol": t["sol_amount"] / 10**SOL_DECIMALS, "tokens": t["token_amount"] / 10**TOKEN_DECIMALS}
            for t in ordered]
    dev_buy = rows[0]["sol"] if rows[0]["is_buy"] else 0.0
    return build_token(mint, rows, dev_in_rows=True, dev_buy=dev_buy)


def load_recorded(raw: dict) -> Token | None:
    """Формат main.py (recorded/): событие create из PumpPortal + сделки из swap-api.pump.fun."""
    rows = raw["trades"]
    if not rows:
        return None
    dev_in_rows = rows[0]["tx"] == raw["signature"]
    created_t = rows[0]["ts"] if dev_in_rows else raw["created_at"]
    tok = build_token(raw["mint"], rows, dev_in_rows, raw["dev_buy_sol"], created_t,
                      data_end=raw["fetched_at"])
    tok.creator = raw.get("creator") or ""
    return tok


def load_tokens(path: str) -> list[Token]:
    tokens = []
    for file in os.listdir(path):
        if not file.endswith(".json"):
            continue
        with open(os.path.join(path, file)) as f:
            raw = json.load(f)
        if isinstance(raw, dict) and "created_at" in raw:
            if tok := load_recorded(raw):
                tokens.append(tok)
            continue
        if isinstance(raw, dict):
            raw = raw.get("trades", [])
        if raw:
            tokens.append(load_pumpfun(file.removesuffix(".json"), raw))
    return sorted(tokens, key=lambda t: t.created_t)


def state_at(tok: Token, t: float) -> tuple[float, float]:
    """Состояние кривой с учётом всех событий до момента t включительно."""
    i = bisect.bisect_right(tok.times, t) - 1
    return tok.states[max(i, 0)]


def signal_time(cfg: Config, tok: Token) -> float:
    """Момент решения. При нулевом окне видно только событие create — сделки того же слота
    (бандлы и самые быстрые снайперы) имеют то же время, но к нам ещё не пришли."""
    return tok.created_t + cfg.window_sec if cfg.window_sec else tok.created_t - 1e-6


def simulate_token(cfg: Config, tok: Token) -> TradeResult:
    """Вход через window_sec после создания (+ задержка), без проверки фильтров."""
    lat = cfg.latency_ms / 1000
    signal_t = signal_time(cfg, tok)
    entry_t = tok.created_t + cfg.window_sec + lat
    fill = simulate_buy(cfg, *state_at(tok, entry_t), expected_price=price(*state_at(tok, signal_t)))
    if fill is None:
        return TradeResult(tok.mint, "buy_failed", "slippage", entry_t, entry_t,
                           cost_sol=tx_cost(cfg), proceeds_sol=0.0)

    pos = Position(cfg, tok.mint, fill, opened_at=entry_t)
    deadline = entry_t + cfg.max_hold_sec if cfg.max_hold_sec else float("inf")
    exit_t, reason = None, None
    start = bisect.bisect_right(tok.times, entry_t)
    for t, (v_sol, v_tokens) in zip(tok.times[start:], tok.states[start:]):
        if t >= deadline:
            exit_t, reason = deadline + lat, "timeout"
            break
        if migrated(v_sol, v_tokens):
            exit_t, reason = t, "migrated"
            break
        reason = pos.check(price(v_sol, v_tokens), t)
        if reason:
            exit_t = t + lat
            break
    if exit_t is None:
        if deadline <= tok.data_end:
            exit_t, reason = deadline + lat, "timeout"
        else:
            exit_t, reason = tok.times[-1], "end_of_data"

    v_sol, v_tokens = state_at(tok, exit_t)
    return TradeResult(tok.mint, "closed", reason, entry_t, exit_t,
                       cost_sol=fill.cost_sol, proceeds_sol=simulate_sell(cfg, v_sol, v_tokens, fill.tokens),
                       entry_price=fill.entry_price, exit_price=price(v_sol, v_tokens), peak_price=pos.peak)


def run(cfg: Config, tokens: list[Token], rep: dict | None = None) -> list[TradeResult]:
    results = []
    for tok in tokens:
        feats = early_features(tok.trades, signal_time(cfg, tok), rep or {})
        if passes_filters(cfg, tok.dev_buy, feats):
            results.append(simulate_token(cfg, tok))
    return results


# --- подбор фильтров ---

WINDOWS = (0, 2, 5, 10, 20)
EXITS = ((40, -25), (80, -25), (200, -50), (1000, -50))
MIN_BUYERS = (0, 3, 5, 10, 20)
MIN_VOLUME = (0, 1, 3, 5)
MIN_REP = (None, 0, 2, 5)


def evaluate_grid(cfg: Config, tokens: list[Token], rep: dict) -> list[tuple[dict, dict]]:
    """Исход сделки зависит только от окна и ТП/СЛ, фильтры лишь отбирают токены — поэтому
    симулируем каждый токен один раз на (окно, ТП, СЛ), а комбинации фильтров считаем масками."""
    rows = []
    for window, (tp, sl) in itertools.product(WINDOWS, EXITS):
        c = replace(cfg, window_sec=window, take_profit_pct=tp, stop_loss_pct=sl)
        sims = [(t.dev_buy, early_features(t.trades, signal_time(c, t), rep), simulate_token(c, t))
                for t in tokens]
        for buyers, volume, min_rep in itertools.product(MIN_BUYERS, MIN_VOLUME, MIN_REP):
            params = {"window_sec": window, "take_profit_pct": tp, "stop_loss_pct": sl,
                      "min_buyers": buyers, "min_volume_sol": volume,
                      "use_rep": min_rep is not None, "min_rep_score": min_rep or 0}
            fc = replace(c, **params)
            picked = [r for dev_buy, f, r in sims if passes_filters(fc, dev_buy, f)]
            rows.append((params, summarize(picked, fc)))
    return rows


def describe(p: dict) -> str:
    rep = f", репутация ≥ {p['min_rep_score']:g}" if p["use_rep"] else ""
    return (f"окно {p['window_sec']:g}с, покупателей ≥ {p['min_buyers']}, объём ≥ {p['min_volume_sol']:g} SOL"
            f"{rep}, TP {p['take_profit_pct']:g}%, SL {p['stop_loss_pct']:g}%")


def line(s: dict) -> str:
    return (f"сделок {s['trades']:>4}, PnL {s['total_pnl_sol']:+8.4f} SOL, ROI {s['roi_pct']:+6.1f}%, "
            f"винрейт {s['winrate_pct']:4.0f}%")


def search(cfg: Config, tokens: list[Token], min_trades: int):
    """Хронологически: A (40%) — строим репутацию, B (30%) — подбираем параметры, C (30%) — проверяем.
    Для проверки на C репутация пересобирается по A+B, т.е. только по прошлому."""
    n = len(tokens)
    a, b, c = tokens[:int(n * 0.4)], tokens[int(n * 0.4):int(n * 0.7)], tokens[int(n * 0.7):]
    rep_a = build_reputation(t.rep_trades for t in a)
    rep_ab = build_reputation(t.rep_trades for t in a + b)
    print(f"Разбиение по времени: репутация {len(a)} токенов → подбор {len(b)} → проверка {len(c)}\n")

    rows = [r for r in evaluate_grid(cfg, b, rep_a) if r[1]["trades"] >= min_trades]
    rows.sort(key=lambda r: r[1]["roi_pct"], reverse=True)
    print(f"Лучшие на выборке подбора (минимум {min_trades} сделок):")
    for params, s in rows[:10]:
        print(f"  {line(s)}  |  {describe(params)}")

    print("\nТе же параметры на проверочной выборке (данные, которых подбор не видел):")
    for params, _ in rows[:10]:
        s = summarize(run(replace(cfg, **params), c, rep_ab), cfg)
        print(f"  {line(s)}  |  {describe(params)}")

    base = replace(cfg, window_sec=0, min_buyers=0, min_volume_sol=0, use_rep=False)
    print(f"\nБез фильтров на проверочной: {line(summarize(run(base, c), base))}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default="mints")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", help="переопределить параметр из config.py")
    p.add_argument("--amounts", help="сравнить несколько размеров входа, через запятую")
    p.add_argument("--search", action="store_true", help="подбор фильтров с проверкой вне выборки")
    p.add_argument("--min-trades", type=int, default=20, help="минимум сделок для --search")
    p.add_argument("--trades", action="store_true", help="вывести каждую сделку")
    p.add_argument("--save-rep", metavar="FILE", help="сохранить репутацию кошельков по этим данным")
    args = p.parse_args()

    cfg = Config.from_env(**parse_sets(args.set))
    tokens = load_tokens(args.data)
    print(f"Токенов в {args.data}/: {len(tokens)}, проходят фильтр dev buy ≥ {cfg.min_dev_buy_sol} SOL: "
          f"{sum(t.dev_buy >= cfg.min_dev_buy_sol for t in tokens)}")
    errors = [t.curve_error for t in tokens if t.curve_error]
    if errors:
        print(f"Точность реконструкции кривой: медианная ошибка {100 * statistics.median(errors):.2f}%")
    print()

    if args.save_rep:
        with open(args.save_rep, "w") as f:
            json.dump(build_reputation(t.rep_trades for t in tokens), f)
        print(f"Репутация сохранена в {args.save_rep}")
        return

    if args.search:
        search(cfg, tokens, args.min_trades)
        return

    rep = {}
    if cfg.use_rep:
        with open(cfg.rep_file) as f:
            rep = json.load(f)

    if args.amounts:
        for amount in (float(a) for a in args.amounts.split(",")):
            c = replace(cfg, amount_sol=amount)
            s = summarize(run(c, tokens, rep), c)
            print(f"Вход {amount:>7} SOL: {line(s)}, безубыток при росте цены на {s['breakeven_move_pct']:.0f}%")
        return

    results = run(cfg, tokens, rep)
    if args.trades:
        for r in results:
            move = (r.exit_price / r.entry_price - 1) * 100 if r.entry_price else 0
            print(f"{r.mint[:10]}…  {r.reason:<12} цена {move:+7.1f}%  PnL {r.pnl_sol:+.6f} SOL")
        print()
    print(f"TP {cfg.take_profit_pct}%, SL {cfg.stop_loss_pct}% от пика, вход {cfg.amount_sol} SOL, "
          f"задержка {cfg.latency_ms:.0f} мс, окно {cfg.window_sec:g} с\n")
    print(format_summary(summarize(results, cfg), cfg))


if __name__ == "__main__":
    main()

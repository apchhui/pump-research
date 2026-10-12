"""Исследование стратегии на recorded/: какие признаки ранней активности отличают удачные токены
и какие правила выхода работают лучше.

Методика:
- репутация кошельков и история создателя считаются скользящим окном: для каждого токена —
  только по токенам, данные которых были полностью известны к моменту его создания;
- данные делятся по времени: первые 60% — подбор (dev), последние 40% — проверка (holdout).
  Всё, что выбрано на dev, оценивается на holdout с бутстреп-интервалом.

    python research.py features      # какие признаки влияют на результат (dev)
    python research.py exits         # перебор правил выхода (dev)
    python research.py combo         # фильтры × выходы: подбор на dev, проверка на holdout
"""
import argparse
import bisect
import itertools
import multiprocessing as mp
import random
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, replace

from backtest import load_tokens, state_at
from config import Config
from sim import curve_sell, migrated, price, progress_pct, simulate_buy, tx_cost, wallet_pnls

WINDOWS = (0, 5, 15, 20, 30, 45, 60)
DEV_SHARE = 0.6

TOKENS = []   # заполняется до форка, воркеры наследуют
CFG = Config()


@dataclass(frozen=True)
class Exit:
    tp: float | None = None          # полный выход, % от входа
    sl: float | None = None          # жёсткий стоп, % от входа
    trail: float | None = None       # трейлинг-стоп, % от пика
    trail_after: float = 0.0         # трейлинг включается, когда пик выше входа на столько %
    tp1: float | None = None         # частичная фиксация, % от входа
    tp1_frac: float = 0.5
    hold: float = 300.0              # максимум удержания, с

    def __str__(self):
        parts = []
        if self.tp1 is not None:
            parts.append(f"продать {self.tp1_frac:.0%} при +{self.tp1:g}%")
        if self.tp is not None:
            parts.append(f"TP +{self.tp:g}%")
        if self.sl is not None:
            parts.append(f"SL {self.sl:g}%")
        if self.trail is not None:
            after = f" после +{self.trail_after:g}%" if self.trail_after else ""
            parts.append(f"трейл {self.trail:g}%{after}")
        parts.append(f"держать ≤{self.hold:g}с")
        return ", ".join(parts)


# --- симуляция ---

def simulate(cfg: Config, tok, window: float, ex: Exit, signal_at: float | None = None) -> tuple[float, float, str]:
    """(pnl, вложено, причина выхода). Вход через window с после создания или, если задан
    signal_at, в момент сигнала (плюс задержка)."""
    lat = cfg.latency_ms / 1000
    if signal_at is not None:
        signal_t, entry_t = signal_at, signal_at + lat
    else:
        signal_t = tok.created_t + window if window else tok.created_t - 1e-6
        entry_t = tok.created_t + window + lat
    txc = tx_cost(cfg)
    fill = simulate_buy(cfg, *state_at(tok, entry_t), expected_price=price(*state_at(tok, signal_t)))
    if fill is None:
        return -txc, txc, "slippage"

    fee_mult = (1 - cfg.pump_fee_pct / 100) * (1 - cfg.portal_fee_pct / 100)

    def sell(t: float, amount: float) -> float:
        v_sol, v_tokens = state_at(tok, t)
        return curve_sell(v_sol, v_tokens, amount) * fee_mult - txc

    held, proceeds = fill.tokens, 0.0
    entry = peak = fill.entry_price
    deadline = entry_t + ex.hold
    tp1_done = ex.tp1 is None
    reason, exit_t = None, None
    for i in range(bisect.bisect_right(tok.times, entry_t), len(tok.times)):
        t = tok.times[i]
        if t >= deadline:
            reason, exit_t = "timeout", deadline + lat
            break
        v_sol, v_tokens = tok.states[i]
        if migrated(v_sol, v_tokens):
            reason, exit_t = "migrated", t
            break
        p = v_sol / v_tokens
        peak = max(peak, p)
        r = p / entry
        if not tp1_done and r >= 1 + ex.tp1 / 100:
            amount = held * ex.tp1_frac
            proceeds += sell(t + lat, amount)
            held -= amount
            tp1_done = True
        if ex.tp is not None and r >= 1 + ex.tp / 100:
            reason = "tp"
        elif ex.sl is not None and r <= 1 + ex.sl / 100:
            reason = "sl"
        elif (ex.trail is not None and peak >= entry * (1 + ex.trail_after / 100)
              and p <= peak * (1 + ex.trail / 100)):
            reason = "trail"
        if reason:
            exit_t = t + lat
            break
    if reason is None:
        if deadline <= tok.data_end:
            reason, exit_t = "timeout", deadline + lat
        else:
            reason, exit_t = "end", tok.times[-1]
    proceeds += sell(exit_t, held)
    if cfg.reclaim_rent:
        proceeds += cfg.ata_rent_sol - txc
    return proceeds - fill.cost_sol, fill.cost_sol, reason


def _sim_chunk(args):
    idxs, plan = args
    return [[simulate(CFG, TOKENS[i], w, ex)[:2] for w, ex in plan] for i in idxs]


def simulate_all(plan: list[tuple[float, Exit]], idxs: list[int]) -> list[list[tuple[float, float]]]:
    """res[k][j] — (pnl, вложено) токена idxs[k] для plan[j]."""
    chunks = [idxs[i::48] for i in range(48)]
    with mp.get_context("fork").Pool() as pool:
        parts = pool.map(_sim_chunk, [(c, plan) for c in chunks])
    res = [None] * len(idxs)
    pos = {i: k for k, i in enumerate(idxs)}
    for c, part in zip(chunks, parts):
        for i, row in zip(c, part):
            res[pos[i]] = row
    return res


# --- признаки ---

def build_features(tokens) -> list[dict[float, dict]]:
    """feats[i][window] — признаки токена i в момент решения. Репутация — только по прошлому."""
    order = sorted(range(len(tokens)), key=lambda i: tokens[i].data_end)
    rep: dict[str, int] = defaultdict(int)
    launches: Counter = Counter()
    j = 0
    feats = []
    for tok in tokens:
        while j < len(order) and tokens[order[j]].data_end <= tok.created_t:
            done = tokens[order[j]]
            for user, pnl in wallet_pnls(done.rep_trades).items():
                if pnl:
                    rep[user] += 1 if pnl > 0 else -1
            if done.creator:
                launches[done.creator] += 1
            j += 1
        per_window = {}
        for w in WINDOWS:
            until = tok.created_t + w if w else tok.created_t - 1e-6
            buyers: dict[str, float] = defaultdict(float)
            buys = sells = 0
            buy_vol = sell_vol = 0.0
            dev_sold = False
            for tr in tok.trades:
                if tr["t"] > until:
                    break
                if tr["is_buy"]:
                    buys += 1
                    buy_vol += tr["sol"]
                    buyers[tr["user"]] += tr["sol"]
                else:
                    sells += 1
                    sell_vol += tr["sol"]
                    dev_sold |= tr["user"] == tok.creator
            reps = [rep.get(u, 0) for u in buyers]
            # ложатся ли уже видимые сделки на стандартную кривую (у ~трети токенов кривая другая)
            early_errs = [e for tr, e in zip(tok.rep_trades, tok.trade_errs) if tr["t"] <= until]
            per_window[w] = {
                "dev_buy": tok.dev_buy,
                "buyers": len(buyers),
                "buys": buys,
                "sells": sells,
                "buy_vol": buy_vol,
                "net_vol": buy_vol - sell_vol,
                "price_chg": (price(*state_at(tok, until)) / price(*tok.states[0]) - 1) * 100,
                "top_share": max(buyers.values()) / buy_vol if buy_vol else 0.0,
                "rep_sum": sum(reps),
                "good": sum(r >= 2 for r in reps),
                "bad": sum(r <= -2 for r in reps),
                "creator_launches": launches.get(tok.creator, 0),
                "creator_rep": rep.get(tok.creator, 0),
                "dev_sold": int(dev_sold),
                "curve_err": statistics.median(early_errs) if len(early_errs) >= 2 else -1.0,
            }
        feats.append(per_window)
    return feats


# --- статистика ---

def roi(rows) -> float:
    cost = sum(c for _, c in rows)
    return 100 * sum(p for p, _ in rows) / cost if cost else 0.0


def bootstrap(rows, n: int = 2000) -> tuple[float, float, float]:
    """ROI, 2.5% и 97.5% перцентили, доля положительных бутстреп-выборок."""
    if not rows:
        return 0.0, 0.0, 0.0
    rng = random.Random(0)
    vals = sorted(roi(rng.choices(rows, k=len(rows))) for _ in range(n))
    return vals[int(n * 0.025)], vals[int(n * 0.975)], sum(v > 0 for v in vals) / n


def fmt(rows) -> str:
    lo, hi, p_pos = bootstrap(rows)
    return (f"сделок {len(rows):>5}, ROI {roi(rows):+6.1f}% [{lo:+.1f}; {hi:+.1f}], "
            f"P(>0) {p_pos:.0%}, PnL {sum(p for p, _ in rows):+.2f} SOL")


# --- команды ---

BASE_EXITS = [
    Exit(tp=80, trail=-25),
    Exit(tp=200, trail=-50),
    Exit(trail=-30, trail_after=30, sl=-35),
]


def cmd_features(dev, feats):
    plan = [(w, ex) for w in WINDOWS for ex in BASE_EXITS]
    res = simulate_all(plan, dev)
    for wi, w in enumerate(WINDOWS):
        print(f"\n=== окно {w} с ===")
        for ei, ex in enumerate(BASE_EXITS):
            print(f"  все токены, {ex}: {fmt([r[wi * len(BASE_EXITS) + ei] for r in res])}")
        col = wi * len(BASE_EXITS) + 1  # Exit(tp=200, trail=-50)
        for name in feats[dev[0]][w]:
            vals = [(feats[i][w][name], res[k][col]) for k, i in enumerate(dev)]
            vals.sort(key=lambda x: x[0])
            # квинтили; одинаковые значения не разрываем
            cuts = sorted({vals[int(len(vals) * q)][0] for q in (0.2, 0.4, 0.6, 0.8)})
            buckets, lo = [], None
            for cut in cuts + [None]:
                part = [r for v, r in vals if (lo is None or v > lo) and (cut is None or v <= cut)]
                if part:
                    label = f"≤{cut:g}" if cut is not None else f">{lo:g}"
                    buckets.append(f"{label}: {roi(part):+.0f}% ({len(part)})")
                lo = cut
            print(f"  {name:>16}  " + " | ".join(buckets))


EXIT_GRID = (
    [Exit(tp=tp, sl=sl, trail=tr, trail_after=ta, hold=h)
     for tp in (None, 50, 100, 200, 500)
     for sl in (None, -20, -35, -50)
     for tr, ta in ((None, 0), (-15, 0), (-25, 0), (-40, 0), (-25, 30), (-40, 50), (-25, 100))
     for h in (120, 300)
     if tr is not None or sl is not None or tp is not None]
    + [Exit(tp1=tp1, tp1_frac=f, trail=tr, trail_after=ta, sl=sl, hold=300)
       for tp1 in (30, 60, 100) for f in (0.5, 0.75)
       for tr, ta in ((-25, 0), (-40, 50)) for sl in (None, -35)]
)


def cmd_exits(dev, feats, window: float, top: int = 15):
    res = simulate_all([(window, ex) for ex in EXIT_GRID], dev)
    rows = [(roi([r[j] for r in res]), j) for j in range(len(EXIT_GRID))]
    rows.sort(reverse=True)
    print(f"Окно {window} с, без фильтров, {len(dev)} токенов dev. Лучшие выходы из {len(EXIT_GRID)}:")
    for value, j in rows[:top]:
        print(f"  ROI {value:+6.1f}%  |  {EXIT_GRID[j]}")
    print("Худшие:")
    for value, j in rows[-3:]:
        print(f"  ROI {value:+6.1f}%  |  {EXIT_GRID[j]}")


COMBO_WINDOWS = (0, 5, 15, 30)
COMBO_EXITS = [
    Exit(tp=200, sl=-35, hold=300),
    Exit(tp=500, hold=300),
    Exit(tp=500, hold=540),
    Exit(tp=1000, hold=540),
    Exit(tp=500, sl=-50, hold=540),
    Exit(tp1=100, tp1_frac=0.5, tp=500, hold=540),
]
FILTERS = {
    "dev_buy": [None, (0.03, 0.12), (0, 0.1), (0, 0.5)],
    "max_buy_vol": [None, 1.0, 3.0],
    "min_rep_sum": [None, 0, 20, 80],
    "max_bad": [None, 1],
    "no_dev_sell": [False, True],
    "known_creator": [False, True],
    "curve": [None, "std", "mayhem"],
}
STD_CURVE_MAX_ERR = 0.02  # ранние сделки ложатся на стандартную кривую; иначе — токен режима Mayhem
NO_FILTER = {"dev_buy": None, "max_buy_vol": None, "min_rep_sum": None, "max_bad": None,
             "no_dev_sell": False, "known_creator": False}


def passes(f: dict, flt: dict) -> bool:
    if flt.get("curve") == "std" and not 0 <= f["curve_err"] <= STD_CURVE_MAX_ERR:
        return False
    if flt.get("curve") == "mayhem" and not f["curve_err"] > STD_CURVE_MAX_ERR:
        return False
    if flt["dev_buy"] and not flt["dev_buy"][0] < f["dev_buy"] <= flt["dev_buy"][1]:
        return False
    if flt["max_buy_vol"] is not None and f["buy_vol"] > flt["max_buy_vol"]:
        return False
    if flt["min_rep_sum"] is not None and f["rep_sum"] < flt["min_rep_sum"]:
        return False
    if flt["max_bad"] is not None and f["bad"] > flt["max_bad"]:
        return False
    if flt["no_dev_sell"] and f["dev_sold"]:
        return False
    if flt["known_creator"] and not f["creator_launches"]:
        return False
    return True


def describe_filter(flt: dict) -> str:
    parts = []
    if flt["dev_buy"]:
        parts.append(f"дев {flt['dev_buy'][0]:g}–{flt['dev_buy'][1]:g} SOL")
    if flt["max_buy_vol"] is not None:
        parts.append(f"объём ≤{flt['max_buy_vol']:g}")
    if flt["min_rep_sum"] is not None:
        parts.append(f"реп ≥{flt['min_rep_sum']:g}")
    if flt["max_bad"] is not None:
        parts.append(f"плохих ≤{flt['max_bad']}")
    if flt["no_dev_sell"]:
        parts.append("дев не продавал")
    if flt["known_creator"]:
        parts.append("создатель не новый")
    if flt.get("curve") == "std":
        parts.append("стандартная кривая")
    if flt.get("curve") == "mayhem":
        parts.append("только Mayhem")
    return ", ".join(parts) or "без фильтров"


def cmd_combo(dev, hold, feats, min_trades: int = 150, top: int = 12):
    plan = [(w, ex) for w in COMBO_WINDOWS for ex in COMBO_EXITS]
    res_dev = simulate_all(plan, dev)
    filters = [dict(zip(FILTERS, combo)) for combo in itertools.product(*FILTERS.values())]
    print(f"Комбинаций: {len(filters)} фильтров × {len(COMBO_WINDOWS)} окон × {len(COMBO_EXITS)} выходов "
          f"= {len(filters) * len(plan)}", flush=True)

    scored = []
    for wi, w in enumerate(COMBO_WINDOWS):
        for flt in filters:
            picked = [k for k, i in enumerate(dev) if passes(feats[i][w], flt)]
            if len(picked) < min_trades:
                continue
            for ei in range(len(COMBO_EXITS)):
                j = wi * len(COMBO_EXITS) + ei
                rows = [res_dev[k][j] for k in picked]
                scored.append((roi(rows), w, flt, ei, rows))
    scored.sort(key=lambda s: s[0], reverse=True)

    # из лучших по ROI берём самые устойчивые по нижней границе бутстрепа
    candidates = []
    for value, w, flt, ei, rows in scored[:200]:
        lo, _, _ = bootstrap(rows, n=500)
        candidates.append((lo, value, w, flt, ei, len(rows)))
    candidates.sort(key=lambda c: c[0], reverse=True)
    chosen = candidates[:top]

    res_hold = simulate_all(plan, hold)
    print(f"\nЛучшие на dev (≥{min_trades} сделок, отбор по нижней границе 95% интервала) → их результат на holdout:")
    for lo, value, w, flt, ei, n in chosen:
        j = COMBO_WINDOWS.index(w) * len(COMBO_EXITS) + ei
        rows = [res_hold[k][j] for k, i in enumerate(hold) if passes(feats[i][w], flt)]
        print(f"\n  окно {w}с | {describe_filter(flt)} | {COMBO_EXITS[ei]}")
        print(f"    dev:     сделок {n:>5}, ROI {value:+6.1f}% (нижняя граница {lo:+.1f}%)")
        print(f"    holdout: {fmt(rows)}")

    base = [res_hold[k][COMBO_EXITS.index(Exit(tp=500, hold=300))] for k in range(len(hold))]
    print(f"\nБез фильтров на holdout, окно 0с, TP +500%: {fmt(base)}")
    j = COMBO_WINDOWS.index(30) * len(COMBO_EXITS) + COMBO_EXITS.index(Exit(tp=500, hold=540))
    for curve in ("std", "mayhem"):
        rows = [res_hold[k][j] for k, i in enumerate(hold) if passes(feats[i][30], {"curve": curve, **NO_FILTER})]
        print(f"Только {curve} на holdout, окно 30с, TP +500%, 540с: {fmt(rows)}")


STRATEGY = {
    "window": 30,
    "filter": {"dev_buy": (0, 0.1), "max_buy_vol": 3.0, "min_rep_sum": 20, "max_bad": None,
               "no_dev_sell": True, "known_creator": False, "curve": None},
    "exit": Exit(tp=500, hold=540),
}


def run_strategy(idxs, feats, window=None, flt=None, ex=None, cfg_overrides=None):
    """[(pnl, вложено, причина, индекс токена)] для выбранной стратегии."""
    global CFG
    window = STRATEGY["window"] if window is None else window
    flt = STRATEGY["filter"] if flt is None else flt
    ex = STRATEGY["exit"] if ex is None else ex
    saved = CFG
    if cfg_overrides:
        CFG = replace(CFG, **cfg_overrides)
    try:
        return [(*simulate(CFG, TOKENS[i], window, ex), i) for i in idxs if passes(feats[i][window], flt)]
    finally:
        CFG = saved


def cmd_check(dev, hold, feats):
    pr = lambda rows: fmt([(p, c) for p, c, *_ in rows])
    print(f"Стратегия: окно {STRATEGY['window']}с | {describe_filter(STRATEGY['filter'])} | {STRATEGY['exit']}")
    rows = run_strategy(hold, feats)
    print(f"\nholdout: {pr(rows)}")
    print(f"dev:     {pr(run_strategy(dev, feats))}")

    print("\n1) Зависимость от лучших сделок (holdout):")
    by_pnl = sorted(rows, key=lambda r: r[0], reverse=True)
    for k in (1, 3, 5, 10):
        print(f"   без {k:>2} лучших: {pr(by_pnl[k:])}")
    print(f"   лучшие 5 сделок, SOL: {[round(r[0], 3) for r in by_pnl[:5]]}")
    print(f"   причины выхода: {dict(Counter(r[2] for r in rows))}")

    print("\n2) Стабильность во времени (holdout по четвертям):")
    q = len(hold) // 4
    for k in range(4):
        part = set(hold[k * q:(k + 1) * q])
        print(f"   четверть {k + 1}: {pr([r for r in rows if r[3] in part])}")

    print("\n3) Живые vs дозагруженные (весь набор):")
    all_rows = run_strategy(dev + hold, feats)
    live = [r for r in all_rows if TOKENS[r[3]].data_end - TOKENS[r[3]].created_t > 601]
    print(f"   живая запись:   {pr(live)}")
    print(f"   дозагрузка:     {pr([r for r in all_rows if r not in live])}")

    print("\n4) Исполнение и издержки (holdout):")
    for name, ov in [("задержка 400 мс", {"latency_ms": 400}), ("задержка 1.5 с", {"latency_ms": 1500}),
                     ("задержка 3 с", {"latency_ms": 3000}), ("комиссии +1% на сторону",
                      {"pump_fee_pct": CFG.pump_fee_pct + 1}), ("вход 0.5 SOL", {"amount_sol": 0.5}),
                     ("вход 1 SOL", {"amount_sol": 1.0}), ("вход 0.02 SOL", {"amount_sol": 0.02})]:
        print(f"   {name:<24} {pr(run_strategy(hold, feats, cfg_overrides=ov))}")

    print("\n5) Соседние параметры (holdout):")
    base = STRATEGY["filter"]
    for name, kw in [("окно 15с", {"window": 15}), ("окно 20с", {"window": 20}), ("окно 45с", {"window": 45}),
                     ("окно 60с", {"window": 60}),
                     ("реп ≥0", {"flt": {**base, "min_rep_sum": 0}}), ("реп ≥50", {"flt": {**base, "min_rep_sum": 50}}),
                     ("реп ≥150", {"flt": {**base, "min_rep_sum": 150}}),
                     ("дев 0–0.2", {"flt": {**base, "dev_buy": (0, 0.2)}}),
                     ("дев 0–0.5", {"flt": {**base, "dev_buy": (0, 0.5)}}),
                     ("дев любой", {"flt": {**base, "dev_buy": None}}),
                     ("без фильтра объёма", {"flt": {**base, "max_buy_vol": None}}),
                     ("дев может продавать", {"flt": {**base, "no_dev_sell": False}}),
                     ("TP +200%", {"ex": Exit(tp=200, hold=540)}), ("TP +1000%", {"ex": Exit(tp=1000, hold=540)}),
                     ("держать 300с", {"ex": Exit(tp=500, hold=300)}),
                     ("+ SL -50%", {"ex": Exit(tp=500, sl=-50, hold=540)}),
                     ("+ трейл -40% после +100%", {"ex": Exit(tp=500, trail=-40, trail_after=100, hold=540)})]:
        w = kw.get("window", STRATEGY["window"])
        if w not in WINDOWS:
            continue
        print(f"   {name:<26} {pr(run_strategy(hold, feats, **kw))}")


FLIP_LATENCIES_MS = (0, 400, 800, 1500)
FLIP_COSTS = {
    "как сейчас": {},
    "экономно": {"portal_fee_pct": 0.0, "reclaim_rent": True},  # свои транзакции в pump + закрытие аккаунта
}
FLIP_EXITS = [Exit(tp=tp, sl=sl, hold=h)
              for h in (3, 5, 10, 20, 30, 60)
              for tp in (None, 10, 20, 40)
              for sl in (None, -10, -20)]


def cmd_flip(dev, hold, feats):
    """Быстрый флип: вход сразу после создания, выход через секунды."""
    global CFG
    base_cfg = CFG
    results = {}
    for lat in FLIP_LATENCIES_MS:
        for cost_name, ov in FLIP_COSTS.items():
            CFG = replace(base_cfg, latency_ms=lat, **ov)
            plan = [(0, ex) for ex in FLIP_EXITS]
            results[lat, cost_name] = (simulate_all(plan, dev), simulate_all(plan, hold))
    CFG = base_cfg

    print("\nТолько удержание (без ТП/СЛ), dev, ROI по задержке входа:")
    print("  держать " + "".join(f"{lat:>9}мс" for lat in FLIP_LATENCIES_MS))
    for h in (3, 5, 10, 20, 30, 60):
        j = FLIP_EXITS.index(Exit(hold=h))
        cells = [roi([r[j] for r in results[lat, 'как сейчас'][0]]) for lat in FLIP_LATENCIES_MS]
        print(f"  {h:>5}с  " + "".join(f"{c:>+10.1f}%" for c in cells))

    for (lat, cost_name), (res_dev, res_hold) in results.items():
        ranked = sorted(range(len(FLIP_EXITS)), key=lambda j: roi([r[j] for r in res_dev]), reverse=True)
        j = ranked[0]
        print(f"\nзадержка {lat} мс, издержки «{cost_name}»: лучший выход на dev — {FLIP_EXITS[j]}")
        print(f"    dev:     {fmt([r[j] for r in res_dev])}")
        print(f"    holdout: {fmt([r[j] for r in res_hold])}")

    # тип кривой на момент входа неизвестен (видна одна сделка дева) — разбивка только для понимания
    print("\nРазбивка лучшего варианта при 0 мс «экономно» по типу кривой (по полным данным, holdout):")
    res_dev, res_hold = results[0, "экономно"]
    j = max(range(len(FLIP_EXITS)), key=lambda j: roi([r[j] for r in res_dev]))
    for name, cond in (("обычная", lambda t: t.curve_error <= STD_CURVE_MAX_ERR),
                       ("Mayhem", lambda t: t.curve_error > STD_CURVE_MAX_ERR)):
        rows = [res_hold[k][j] for k, i in enumerate(hold) if cond(TOKENS[i])]
        print(f"  {name:<8} {fmt(rows)}")


PROGRESS_LEVELS = (20, 30, 40, 50, 60, 70, 80, 90)
PROGRESS_EXITS = [Exit(tp=tp, sl=sl, hold=h)
                  for h in (30, 60, 120, 300)
                  for tp in (None, 25, 50, 100)
                  for sl in (None, -15, -30)]


def progress_trigger(tok, level: float) -> float | None:
    """Время первой сделки, после которой продано ≥ level% токенов кривой (и она ещё не завершена)."""
    for t, (v_sol, v_tokens) in zip(tok.times, tok.states):
        if migrated(v_sol, v_tokens):
            return None
        if progress_pct(v_tokens) >= level:
            return t
    return None


def _progress_chunk(args):
    idxs, exits = args
    out = []
    for i in idxs:
        tok = TOKENS[i]
        row = {}
        for level in PROGRESS_LEVELS:
            at = progress_trigger(tok, level)
            if at is not None:
                row[level] = [simulate(CFG, tok, 0, ex, signal_at=at)[:2] for ex in exits]
        out.append(row)
    return out


def simulate_progress(idxs: list[int]) -> list[dict]:
    chunks = [idxs[i::48] for i in range(48)]
    with mp.get_context("fork").Pool() as pool:
        parts = pool.map(_progress_chunk, [(c, PROGRESS_EXITS) for c in chunks])
    pos = {i: k for k, i in enumerate(idxs)}
    res = [None] * len(idxs)
    for c, part in zip(chunks, parts):
        for i, row in zip(c, part):
            res[pos[i]] = row
    return res


def cmd_progress(dev, hold, feats):
    """Вход, когда кривая заполнена на N% (продано N% от 793 млн токенов), а не по времени."""
    global CFG
    base_cfg = CFG
    for cost_name, ov in FLIP_COSTS.items():
        CFG = replace(base_cfg, **ov)
        res_dev, res_hold = simulate_progress(dev), simulate_progress(hold)
        print(f"\n=== издержки «{cost_name}», задержка {CFG.latency_ms:.0f} мс ===")
        for level in PROGRESS_LEVELS:
            d = [r[level] for r in res_dev if level in r]
            h = [r[level] for r in res_hold if level in r]
            if len(d) < 50:
                continue
            j = max(range(len(PROGRESS_EXITS)), key=lambda j: roi([x[j] for x in d]))
            hold_only = PROGRESS_EXITS.index(Exit(hold=120))
            print(f"\n  прогресс ≥{level}%: сигналов dev {len(d)}, holdout {len(h)}")
            print(f"    просто держать 120с — dev ROI {roi([x[hold_only] for x in d]):+.1f}%, "
                  f"holdout ROI {roi([x[hold_only] for x in h]):+.1f}%")
            print(f"    лучший выход на dev: {PROGRESS_EXITS[j]}")
            print(f"      dev:     {fmt([x[j] for x in d])}")
            print(f"      holdout: {fmt([x[j] for x in h])}")
    CFG = base_cfg


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["features", "exits", "combo", "check", "flip", "progress"])
    p.add_argument("--data", default="recorded")
    p.add_argument("--window", type=float, default=15)
    p.add_argument("--amount", type=float, default=0.1)
    p.add_argument("--price-only", action="store_true", help="только токены с ценой из API (точные резервы)")
    args = p.parse_args()

    global TOKENS, CFG
    CFG = Config.from_env(amount_sol=args.amount, min_dev_buy_sol=0)
    TOKENS = load_tokens(args.data)
    if args.price_only:
        TOKENS = [t for t in TOKENS if t.has_price]
    n_dev = int(len(TOKENS) * DEV_SHARE)
    dev, hold = list(range(n_dev)), list(range(n_dev, len(TOKENS)))
    print(f"Токенов: {len(TOKENS)} (dev {len(dev)}, holdout {len(hold)}), вход {CFG.amount_sol} SOL", flush=True)
    feats = build_features(TOKENS)

    if args.command == "features":
        cmd_features(dev, feats)
    elif args.command == "exits":
        cmd_exits(dev, feats, args.window)
    elif args.command == "check":
        cmd_check(dev, hold, feats)
    elif args.command == "flip":
        cmd_flip(dev, hold, feats)
    elif args.command == "progress":
        cmd_progress(dev, hold, feats)
    else:
        cmd_combo(dev, hold, feats)


if __name__ == "__main__":
    sys.setrecursionlimit(10000)
    main()

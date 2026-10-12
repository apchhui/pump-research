"""Стратегии вокруг миграции токена с bonding curve на PumpSwap.

Данные — recorded/ с полем amm_trades (main.py после 2026-10-11): токены, у которых к 10-й минуте
распродано ≥40% кривой, докачиваются через 40 минут и, если мигрировали, через 3 часа.

  A: купить на кривой, когда распродано N%, продать на PumpSwap через X после миграции
     (или по ТП/СЛ/таймауту, если миграции не случилось).
  B: купить на PumpSwap сразу после миграции, держать H.

Резервы: кривая — токены по сделкам, SOL = цена API × токены (работает и для Mayhem);
пул PumpSwap — 206.9 млн токенов на старте, дальше по сделкам, SOL = цена × токены.
Подбор на первых 60% токенов по времени, проверка на последних 40%.

    python research_migration.py
"""
import argparse
import bisect
import itertools
import json
import multiprocessing as mp
import os
from dataclasses import dataclass, replace

from config import Config
from research import bootstrap, fmt, roi
from sim import INITIAL_V_TOKENS, SOLD_AT_MIGRATION, curve_buy, curve_sell, progress_pct, tx_cost

SLOT_SEC = 0.4
POOL_START_TOKENS = INITIAL_V_TOKENS - SOLD_AT_MIGRATION - 73_000_000  # 1 млрд − 793.1 млн = 206.9 млн
AMM_FEE_PCT = 0.30            # PumpSwap: LP + протокол + создатель
FIRST_FETCH_SEC = 600         # триггер должен случиться в пределах первой загрузки

TOKENS = []
CFG = Config()


@dataclass
class MToken:
    mint: str
    created_t: float
    curve_t: list[float]
    curve: list[tuple[float, float]]      # (v_sol, v_tokens) после сделки
    amm_t: list[float]
    amm: list[tuple[float, float]]        # (sol в пуле, токены в пуле) после сделки
    data_end: float
    path_t: tuple = ()                    # общая лента цен: кривая до миграции, потом пул
    path_p: tuple = ()

    @property
    def migrated(self) -> bool:
        return bool(self.amm_t)

    @property
    def migration_t(self) -> float:
        return self.amm_t[0] if self.amm_t else float("inf")


def load(path: str) -> list[MToken]:
    out = []
    for f in os.listdir(path):
        if not f.endswith(".json"):
            continue
        with open(os.path.join(path, f)) as fh:
            raw = json.load(fh)
        if not raw.get("refetches") or not raw.get("trades"):
            continue
        rows = raw["trades"]
        base_t, base_slot = rows[0]["ts"], rows[0]["slot"]
        to_t = lambda r: round(base_t + (r["slot"] - base_slot) * SLOT_SEC, 3)
        v_tok, curve_t, curve = INITIAL_V_TOKENS, [], []
        for r in rows:
            v_tok += -r["tokens"] if r["is_buy"] else r["tokens"]
            curve_t.append(to_t(r))
            curve.append((r["price"] * v_tok, v_tok))
        base, amm_t, amm = POOL_START_TOKENS, [], []
        for r in raw.get("amm_trades") or []:
            base += -r["tokens"] if r["is_buy"] else r["tokens"]
            if base <= 0:
                break
            amm_t.append(to_t(r))
            amm.append((r["price"] * base, base))
        last = max(curve_t[-1], amm_t[-1] if amm_t else 0)
        mig_t = amm_t[0] if amm_t else float("inf")
        tape = [(t, vs / vt) for t, (vs, vt) in zip(curve_t, curve) if t < mig_t]
        tape += [(t, q / b) for t, (q, b) in zip(amm_t, amm)]
        out.append(MToken(raw["mint"], curve_t[0], curve_t, curve, amm_t, amm,
                          data_end=max(last, raw["fetched_at"]),
                          path_t=tuple(t for t, _ in tape), path_p=tuple(p for _, p in tape)))
    return sorted(out, key=lambda t: t.created_t)


def state(times, states, t):
    i = bisect.bisect_right(times, t) - 1
    return states[i] if i >= 0 else None


def price_at(tok: MToken, t: float) -> tuple[float, str]:
    if tok.migrated and t >= tok.migration_t:
        q, b = state(tok.amm_t, tok.amm, t)
        return q / b, "amm"
    v_sol, v_tok = state(tok.curve_t, tok.curve, t)
    return v_sol / v_tok, "curve"


def sell_value(cfg: Config, tok: MToken, t: float, tokens: float) -> float:
    portal = 1 - cfg.portal_fee_pct / 100
    if tok.migrated and t >= tok.migration_t:
        q, b = state(tok.amm_t, tok.amm, t)
        out = curve_sell(q, b, tokens * (1 - AMM_FEE_PCT / 100))
    else:
        v_sol, v_tok = state(tok.curve_t, tok.curve, t)
        out = curve_sell(v_sol, v_tok, tokens) * (1 - cfg.pump_fee_pct / 100)
    return out * portal - tx_cost(cfg)


@dataclass(frozen=True)
class Plan:
    mode: str                 # "A" — вход на кривой по прогрессу, "B" — вход на PumpSwap после миграции
    level: float = 0          # A: % распроданной кривой
    after_mig: float | None = None   # A: продать через столько секунд после миграции
    hold: float = 1800        # максимум удержания от входа
    tp: float | None = None
    sl: float | None = None

    def __str__(self):
        s = (f"A: вход при {self.level:g}% кривой" if self.mode == "A" else "B: вход на PumpSwap сразу после миграции")
        if self.after_mig is not None:
            s += f", продать через {self.after_mig:g}с после миграции"
        s += f", держать ≤{self.hold / 60:g} мин"
        if self.tp is not None:
            s += f", TP +{self.tp:g}%"
        if self.sl is not None:
            s += f", SL {self.sl:g}%"
        return s


def simulate(cfg: Config, tok: MToken, p: Plan):
    """(pnl, вложено) или None, если сигнала не было."""
    lat = cfg.latency_ms / 1000
    txc = tx_cost(cfg)
    portal = 1 - cfg.portal_fee_pct / 100
    rent = 0.0 if cfg.reclaim_rent else cfg.ata_rent_sol

    if p.mode == "A":
        signal = next((t for t, (vs, vt) in zip(tok.curve_t, tok.curve)
                       if progress_pct(vt) >= p.level), None)
        if signal is None or signal > tok.created_t + FIRST_FETCH_SEC or signal >= tok.migration_t:
            return None
        entry_t = signal + lat
        if entry_t >= tok.migration_t:          # кривая закрылась раньше, чем дошла наша покупка
            return -txc, txc
        v_sol, v_tok = state(tok.curve_t, tok.curve, entry_t)
        sig_sol, sig_tok = state(tok.curve_t, tok.curve, signal)
        got = curve_buy(v_sol, v_tok, cfg.amount_sol * portal * (1 - cfg.pump_fee_pct / 100))
        if got > v_tok - (INITIAL_V_TOKENS - SOLD_AT_MIGRATION):   # не больше, чем осталось в кривой
            return -txc, txc
        if got < cfg.amount_sol / (sig_sol / sig_tok) * (1 - cfg.slippage_pct / 100):
            return -txc, txc
    else:
        if not tok.migrated:
            return None
        entry_t = tok.migration_t + lat
        q, b = state(tok.amm_t, tok.amm, entry_t)
        q0, b0 = tok.amm[0]
        got = curve_buy(q, b, cfg.amount_sol * portal * (1 - AMM_FEE_PCT / 100))
        if got < cfg.amount_sol / (q0 / b0) * (1 - cfg.slippage_pct / 100):
            return -txc, txc

    cost = cfg.amount_sol + txc + rent
    entry_price = cfg.amount_sol / got
    deadline = entry_t + p.hold
    if p.after_mig is not None and tok.migrated:
        deadline = min(deadline, max(entry_t, tok.migration_t + p.after_mig))
    exit_t = None
    if p.tp is not None or p.sl is not None:
        hi = entry_price * (1 + p.tp / 100) if p.tp is not None else float("inf")
        lo = entry_price * (1 + p.sl / 100) if p.sl is not None else -1.0
        for i in range(bisect.bisect_right(tok.path_t, entry_t), len(tok.path_t)):
            t = tok.path_t[i]
            if t >= deadline:
                break
            if not lo < tok.path_p[i] < hi:
                exit_t = t + lat
                break
    if exit_t is None:
        exit_t = min(deadline + lat, tok.data_end)
    proceeds = sell_value(cfg, tok, exit_t, got)
    if cfg.reclaim_rent:
        proceeds -= txc
    return proceeds - cost, cost


PLANS = (
    [Plan("A", level=lv, after_mig=am, hold=h, tp=tp, sl=sl)
     for lv in (40, 50, 60, 70, 80, 90, 95)
     for am in (None, 5, 30, 120, 600)
     for h in (120, 600, 1800)
     for tp, sl in ((None, None), (50, None), (100, -30), (None, -30))]
    + [Plan("B", hold=h, tp=tp, sl=sl)
       for h in (10, 60, 300, 900, 1800, 7200)
       for tp, sl in ((None, None), (20, None), (50, -20), (100, -30), (None, -20))]
)


def _chunk(args):
    idxs, cfg = args
    return [[simulate(cfg, TOKENS[i], p) for p in PLANS] for i in idxs]


def run_all(idxs, cfg):
    chunks = [idxs[i::24] for i in range(24)]
    with mp.get_context("fork").Pool() as pool:
        parts = pool.map(_chunk, [(c, cfg) for c in chunks])
    res = {}
    for c, part in zip(chunks, parts):
        res.update(zip(c, part))
    return [res[i] for i in idxs]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default="recorded")
    p.add_argument("--amount", type=float, default=0.1)
    p.add_argument("--min-trades", type=int, default=40)
    args = p.parse_args()

    global TOKENS
    TOKENS = load(args.data)
    n_dev = int(len(TOKENS) * 0.6)
    dev, hold = list(range(n_dev)), list(range(n_dev, len(TOKENS)))
    print(f"Токенов с докачкой: {len(TOKENS)}, мигрировали: {sum(t.migrated for t in TOKENS)} "
          f"(dev {sum(TOKENS[i].migrated for i in dev)}, holdout {sum(TOKENS[i].migrated for i in hold)})")

    base = Config.from_env(amount_sol=args.amount)
    for cost_name, ov in (("как сейчас", {}), ("экономно", {"portal_fee_pct": 0.0, "reclaim_rent": True})):
        cfg = replace(base, **ov)
        rd, rh = run_all(dev, cfg), run_all(hold, cfg)
        scored = []
        for j, plan in enumerate(PLANS):
            d = [r[j] for r in rd if r[j] is not None]
            if len(d) >= args.min_trades:
                scored.append((bootstrap(d, n=300)[0], roi(d), j, d))
        scored.sort(reverse=True)
        print(f"\n=== издержки «{cost_name}» — лучшие на dev по нижней границе 95% интервала ===")
        for lo, value, j, d in scored[:10]:
            h = [r[j] for r in rh if r[j] is not None]
            print(f"\n  {PLANS[j]}")
            print(f"    dev:     {fmt(d)}")
            print(f"    holdout: {fmt(h)}")
        for mode in ("A", "B"):
            simple = Plan(mode, level=70 if mode == "A" else 0, after_mig=30 if mode == "A" else None,
                          hold=1800 if mode == "A" else 300)
            j = PLANS.index(simple)
            print(f"\n  ориентир {simple}: dev {fmt([r[j] for r in rd if r[j] is not None])}")


if __name__ == "__main__":
    main()

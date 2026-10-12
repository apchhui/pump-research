"""Модель bonding curve pump.fun, издержек и статистики. Общая для backtest.py и main.py."""
import statistics
from collections import Counter
from dataclasses import dataclass, asdict

from config import Config

# Начальные виртуальные резервы bonding curve pump.fun (SOL и токены в UI-единицах, decimals=6)
INITIAL_V_SOL = 30.0
INITIAL_V_TOKENS = 1_073_000_000.0
K = INITIAL_V_SOL * INITIAL_V_TOKENS
# Кривая завершается, когда продано ~793.1 млн токенов. По токенам — надёжнее, чем по SOL:
# у токенов режима Mayhem SOL-резервы нестандартные.
SOLD_AT_MIGRATION = 793_100_000.0
MIGRATION_V_TOKENS = INITIAL_V_TOKENS - SOLD_AT_MIGRATION


def progress_pct(v_tokens: float) -> float:
    return (INITIAL_V_TOKENS - v_tokens) / SOLD_AT_MIGRATION * 100


def migrated(v_sol: float, v_tokens: float) -> bool:
    return v_tokens <= MIGRATION_V_TOKENS


def price(v_sol: float, v_tokens: float) -> float:
    return v_sol / v_tokens


def curve_buy(v_sol: float, v_tokens: float, sol_in: float) -> float:
    """Сколько токенов даст кривая за sol_in (уже за вычетом комиссий)."""
    k = v_sol * v_tokens
    return v_tokens - k / (v_sol + sol_in)


def curve_sell(v_sol: float, v_tokens: float, tokens_in: float) -> float:
    """Сколько SOL даст кривая за tokens_in (до комиссий)."""
    k = v_sol * v_tokens
    return v_sol - k / (v_tokens + tokens_in)


def tx_cost(cfg: Config) -> float:
    return cfg.priority_fee_sol + cfg.base_fee_sol


def early_features(trades: list[dict], until_t: float, rep: dict[str, float]) -> dict:
    """trades: [{'t', 'user', 'is_buy', 'sol'}], только то, что видно к моменту until_t."""
    buyers, volume = set(), 0.0
    for tr in trades:
        if tr["t"] > until_t:
            break
        if tr["is_buy"]:
            buyers.add(tr["user"])
            volume += tr["sol"]
    return {"buyers": len(buyers), "volume": volume, "rep_score": sum(rep.get(u, 0) for u in buyers)}


def passes_filters(cfg: Config, dev_buy: float, feats: dict) -> bool:
    return (dev_buy >= cfg.min_dev_buy_sol
            and feats["buyers"] >= cfg.min_buyers
            and feats["volume"] >= cfg.min_volume_sol
            and (not cfg.use_rep or feats["rep_score"] >= cfg.min_rep_score))


def wallet_pnls(trades: list[dict]) -> dict[str, float]:
    pnl: dict[str, float] = {}
    for tr in trades:
        pnl[tr["user"]] = pnl.get(tr["user"], 0.0) + (-tr["sol"] if tr["is_buy"] else tr["sol"])
    return pnl


def build_reputation(tokens: list[list[dict]]) -> dict[str, int]:
    """Как в rep.py: +1 за каждый токен, где кошелёк в плюсе, -1 где в минусе."""
    rep: dict[str, int] = {}
    for trades in tokens:
        for user, pnl in wallet_pnls(trades).items():
            if pnl:
                rep[user] = rep.get(user, 0) + (1 if pnl > 0 else -1)
    return rep


@dataclass
class Fill:
    tokens: float
    entry_price: float  # эффективная цена: amount / tokens
    cost_sol: float     # всё, что ушло с кошелька


def simulate_buy(cfg: Config, v_sol: float, v_tokens: float, expected_price: float) -> Fill | None:
    """None — покупка не прошла из-за слиппеджа (комиссию за транзакцию всё равно платим)."""
    sol_into_curve = cfg.amount_sol * (1 - cfg.portal_fee_pct / 100) * (1 - cfg.pump_fee_pct / 100)
    tokens = curve_buy(v_sol, v_tokens, sol_into_curve)
    min_tokens = cfg.amount_sol / expected_price * (1 - cfg.slippage_pct / 100)
    if tokens < min_tokens:
        return None
    cost = cfg.amount_sol + tx_cost(cfg) + cfg.ata_rent_sol
    return Fill(tokens=tokens, entry_price=cfg.amount_sol / tokens, cost_sol=cost)


def simulate_sell(cfg: Config, v_sol: float, v_tokens: float, tokens: float) -> float:
    """Сколько SOL вернётся на кошелёк."""
    gross = curve_sell(v_sol, v_tokens, tokens)
    net = gross * (1 - cfg.pump_fee_pct / 100) * (1 - cfg.portal_fee_pct / 100) - tx_cost(cfg)
    if cfg.reclaim_rent:
        net += cfg.ata_rent_sol - tx_cost(cfg)
    return net


class Position:
    """Логика ТП / трейлинг-СЛ / таймаута из старого TP_SL()."""

    def __init__(self, cfg: Config, mint: str, fill: Fill, opened_at: float):
        self.cfg = cfg
        self.mint = mint
        self.fill = fill
        self.opened_at = opened_at
        self.peak = fill.entry_price

    def check(self, current_price: float, now: float) -> str | None:
        self.peak = max(self.peak, current_price)
        from_entry = (current_price / self.fill.entry_price - 1) * 100
        from_peak = (current_price / self.peak - 1) * 100
        if from_entry >= self.cfg.take_profit_pct:
            return "take_profit"
        if from_peak <= self.cfg.stop_loss_pct:
            return "stop_loss"
        if self.cfg.max_hold_sec and now - self.opened_at >= self.cfg.max_hold_sec:
            return "timeout"
        return None


@dataclass
class TradeResult:
    mint: str
    status: str            # closed / buy_failed
    reason: str            # take_profit / stop_loss / timeout / end_of_data / migrated / slippage
    opened_at: float
    closed_at: float
    cost_sol: float
    proceeds_sol: float
    entry_price: float = 0.0
    exit_price: float = 0.0
    peak_price: float = 0.0

    @property
    def pnl_sol(self) -> float:
        return self.proceeds_sol - self.cost_sol

    def to_dict(self) -> dict:
        return {**asdict(self), "pnl_sol": self.pnl_sol}


def breakeven_move_pct(cfg: Config) -> float:
    """На сколько % должна вырасти рыночная цена, чтобы сделка вышла в ноль (без учёта импакта)."""
    fees_mult = ((1 - cfg.pump_fee_pct / 100) * (1 - cfg.portal_fee_pct / 100)) ** 2
    # покупка + продажа; аренда аккаунта либо теряется, либо возвращается ценой ещё одной транзакции
    fixed = 2 * tx_cost(cfg) + (tx_cost(cfg) if cfg.reclaim_rent else cfg.ata_rent_sol)
    needed = (cfg.amount_sol + fixed) / (cfg.amount_sol * fees_mult)
    return (needed - 1) * 100


def summarize(results: list[TradeResult], cfg: Config) -> dict:
    trades = [r for r in results if r.status == "closed"]
    failed = [r for r in results if r.status == "buy_failed"]
    pnls = [r.pnl_sol for r in results]
    invested = sum(r.cost_sol for r in trades)
    wins = [r for r in trades if r.pnl_sol > 0]
    gross_moves = [(r.exit_price / r.entry_price - 1) * 100 for r in trades if r.entry_price]
    return {
        "trades": len(trades),
        "buy_failed": len(failed),
        "winrate_pct": 100 * len(wins) / len(trades) if trades else 0.0,
        "total_pnl_sol": sum(pnls),
        "avg_pnl_sol": statistics.mean(pnls) if pnls else 0.0,
        "roi_pct": 100 * sum(pnls) / invested if invested else 0.0,
        "best_sol": max(pnls, default=0.0),
        "worst_sol": min(pnls, default=0.0),
        "median_price_move_pct": statistics.median(gross_moves) if gross_moves else 0.0,
        "breakeven_move_pct": breakeven_move_pct(cfg),
        "reasons": dict(Counter(r.reason for r in results)),
    }


def format_summary(s: dict, cfg: Config) -> str:
    reasons = ", ".join(f"{k}: {v}" for k, v in sorted(s["reasons"].items()))
    return (
        f"Сделок: {s['trades']} (не купилось из-за слиппеджа: {s['buy_failed']})\n"
        f"Винрейт: {s['winrate_pct']:.1f}%\n"
        f"PnL: {s['total_pnl_sol']:+.6f} SOL, в среднем {s['avg_pnl_sol']:+.6f} SOL на сделку\n"
        f"ROI на вложенное: {s['roi_pct']:+.1f}%\n"
        f"Лучшая / худшая: {s['best_sol']:+.6f} / {s['worst_sol']:+.6f} SOL\n"
        f"Медианное движение цены вход→выход: {s['median_price_move_pct']:+.1f}%\n"
        f"Безубыток при входе {cfg.amount_sol} SOL требует роста цены на {s['breakeven_move_pct']:.1f}%\n"
        f"Выходы: {reasons}"
    )

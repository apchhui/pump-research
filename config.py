import os
from dataclasses import dataclass, fields


def load_dotenv(path: str = ".env"):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Config:
    # --- стратегия (как в старом main.py) ---
    min_dev_buy_sol: float = 2.0      # LIMIT: минимальная покупка дева при создании токена
    amount_sol: float = 0.001         # AMOUNT: размер входа
    take_profit_pct: float = 40.0     # от цены входа
    stop_loss_pct: float = -15.0      # от пика (трейлинг)
    max_hold_sec: float = 300.0       # принудительный выход, 0 = выключено
    max_positions: int = 15

    # --- фильтр по ранней активности: решение принимается через window_sec после создания ---
    window_sec: float = 0.0
    min_buyers: int = 0               # уникальных покупателей за окно
    min_volume_sol: float = 0.0       # объём покупок за окно
    use_rep: bool = False             # учитывать репутацию кошельков
    min_rep_score: float = 0.0        # сумма репутаций покупателей за окно
    rep_file: str = "wallet_reputation.json"

    # --- запись данных для бэктеста (main.py) ---
    record_sec: float = 600.0         # сколько секунд писать сделки каждого нового токена, 0 = не писать

    # --- исполнение ---
    latency_ms: float = 800.0         # от сигнала до попадания транзакции в блок
    slippage_pct: float = 10.0        # покупка не проходит, если цена ушла дальше

    # --- издержки ---
    pump_fee_pct: float = 1.25        # комиссия bonding curve pump.fun (протокол + креатор)
    portal_fee_pct: float = 1.0       # PumpPortal Lightning API (Local API = 0.5)
    priority_fee_sol: float = 0.00005
    base_fee_sol: float = 0.000005
    ata_rent_sol: float = 0.00203928  # аренда токен-аккаунта, списывается при первой покупке
    reclaim_rent: bool = False        # закрываем ли аккаунт после продажи (PumpPortal сам не закрывает)

    @classmethod
    def from_env(cls, **overrides) -> "Config":
        load_dotenv()
        cfg = cls()
        for f in fields(cls):
            raw = os.environ.get(f.name.upper())
            if raw is not None:
                setattr(cfg, f.name, _cast(f.type, raw))
        for key, value in overrides.items():
            if value is not None:
                setattr(cfg, key, value)
        return cfg


def parse_sets(items: list[str] | None) -> dict:
    """['take_profit_pct=80', 'use_rep=1'] -> {'take_profit_pct': 80.0, 'use_rep': True}"""
    types = {f.name: f.type for f in fields(Config)}
    result = {}
    for item in items or []:
        key, raw = item.split("=", 1)
        if key not in types:
            raise SystemExit(f"Неизвестный параметр: {key}. Есть: {', '.join(types)}")
        result[key] = _cast(types[key], raw)
    return result


def _cast(typ, raw: str):
    if typ in (bool, "bool"):
        return raw.lower() in ("1", "true", "yes", "on")
    if typ in (int, "int"):
        return int(raw)
    if typ in (str, "str"):
        return raw
    return float(raw)

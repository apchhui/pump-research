import os
import json
from collections import defaultdict
from datetime import datetime

INPUT_DIR = "mints"
OUTPUT_DIR = "analyzed"
os.makedirs(OUTPUT_DIR, exist_ok=True)

PERIOD_SECONDS = 60 * 60  # история цены за первый час
DECIMALS = 9  # SOL = 10^9 лампортов

def analyze_trades(data):
    if not data:
        return {}

    # Временная метка начала (первая сделка)
    start_ts = min(tx["timestamp"] for tx in data)

    # Анализируем каждую сделку
    wallet_stats = defaultdict(lambda: {
        "buys": 0,
        "sells": 0,
        "buy_sol": 0.0,
        "sell_sol": 0.0,
        "buy_token": 0,
        "sell_token": 0
    })

    price_points = []

    for tx in sorted(data, key=lambda t: (t["slot"], t["tx_index"])):
        ts = tx["timestamp"]
        user = tx["user"]
        sol = tx["sol_amount"] / 10**DECIMALS
        tokens = tx["token_amount"]
        is_buy = tx["is_buy"]

        if ts <= start_ts + PERIOD_SECONDS:
            # Цена токена в момент транзакции
            price = sol / tokens if tokens > 0 else 0
            price_points.append({
                "timestamp": ts,
                "price": price,
                "is_buy": is_buy,
                "sol": sol,
                "tokens": tokens
            })

        # Сводка по пользователям
        stats = wallet_stats[user]
        if is_buy:
            stats["buys"] += 1
            stats["buy_sol"] += sol
            stats["buy_token"] += tokens
        else:
            stats["sells"] += 1
            stats["sell_sol"] += sol
            stats["sell_token"] += tokens

    # Формируем финальный отчёт
    result = {
        "first_timestamp": start_ts,
        "period_seconds": PERIOD_SECONDS,
        "price_history": price_points,
        "wallets": {}
    }

    for user, stats in wallet_stats.items():
        profit = stats["sell_sol"] - stats["buy_sol"]
        result["wallets"][user] = {
            **stats,
            "profit_sol": round(profit, 6)
        }

    return result

def main():
    for file in os.listdir(INPUT_DIR):
        if not file.endswith(".json"):
            continue

        mint = file.replace(".json", "")
        input_path = os.path.join(INPUT_DIR, file)
        output_path = os.path.join(OUTPUT_DIR, f"{mint}.json")

        with open(input_path, "r") as f:
            try:
                data = json.load(f)
                if isinstance(data, dict):  # может быть {"trades": [...]}?
                    data = data.get("trades", [])
            except Exception as e:
                print(f"[!] Ошибка чтения {file}: {e}")
                continue

        analyzed = analyze_trades(data)

        with open(output_path, "w") as f:
            json.dump(analyzed, f, indent=2)

        print(f"[✓] Проанализировано {mint}, сохранено в analyzed/{mint}.json")

if __name__ == "__main__":
    main()

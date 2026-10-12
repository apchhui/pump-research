import os
import json
from collections import defaultdict

INPUT_DIR = "analyzed"
OUTPUT_FILE = "wallet_reputation.json"

wallet_reputation = defaultdict(int)

def main():
    for file in os.listdir(INPUT_DIR):
        if not file.endswith(".json"):
            continue

        path = os.path.join(INPUT_DIR, file)
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[!] Ошибка чтения {file}: {e}")
            continue

        wallets = data.get("wallets", {})
        for wallet, stats in wallets.items():
            profit = stats.get("profit_sol", 0)
            if profit > 0:
                wallet_reputation[wallet] += 1
            elif profit < 0:
                wallet_reputation[wallet] -= 1

    # Сохраняем результат
    with open(OUTPUT_FILE, "w") as f:
        json.dump(wallet_reputation, f, indent=2)

    print(f"[✓] Репутация кошельков сохранена в {OUTPUT_FILE}")

if __name__ == "__main__":
    main()

import os
import json
import matplotlib.pyplot as plt
from datetime import datetime

INPUT_DIR = "analyzed"
OUTPUT_DIR = "graph"
os.makedirs(OUTPUT_DIR, exist_ok=True)

def plot_price_history(mint, price_history):
    if not price_history:
        return

    timestamps = [entry["timestamp"] for entry in price_history]
    prices = [entry["price"] for entry in price_history]

    # Нормализуем время относительно первой точки
    start_time = timestamps[0]
    seconds = [t - start_time for t in timestamps]

    plt.figure(figsize=(10, 5))
    plt.plot(seconds, prices, marker='o', linestyle='-', label='Цена токена (SOL)')
    plt.title(f"Цена токена за время (mint: {mint})")
    plt.xlabel("Секунды с начала")
    plt.ylabel("Цена (SOL)")
    plt.grid(True)
    plt.legend()
    plt.tight_layout()

    output_path = os.path.join(OUTPUT_DIR, f"{mint}.png")
    plt.savefig(output_path)
    plt.close()
    print(f"[✓] График сохранён: {output_path}")

def main():
    for file in os.listdir(INPUT_DIR):
        if not file.endswith(".json"):
            continue

        mint = file.replace(".json", "")
        input_path = os.path.join(INPUT_DIR, file)

        with open(input_path, "r") as f:
            try:
                data = json.load(f)
                price_history = data.get("price_history", [])
                plot_price_history(mint, price_history)
            except Exception as e:
                print(f"[!] Ошибка при обработке {file}: {e}")

if __name__ == "__main__":
    main()

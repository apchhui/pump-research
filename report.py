"""Статистика по сделкам dry-run из paper_trades.jsonl."""
import json
import sys

from config import Config
from sim import TradeResult, format_summary, summarize

TRADES_FILE = sys.argv[1] if len(sys.argv) > 1 else "paper_trades.jsonl"


def main():
    results, cfg = [], Config.from_env()
    with open(TRADES_FILE) as f:
        for line in f:
            row = json.loads(line)
            row.pop("pnl_sol", None)
            cfg = Config(**row.pop("config"))  # берём параметры последней сессии
            results.append(TradeResult(**row))
    if not results:
        print("Сделок пока нет")
        return
    hours = (max(r.closed_at for r in results) - min(r.opened_at for r in results)) / 3600
    print(f"Период: {hours:.1f} ч\n")
    print(format_summary(summarize(results, cfg), cfg))


if __name__ == "__main__":
    main()

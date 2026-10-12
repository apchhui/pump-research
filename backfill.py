"""Дозагрузка истории токенов, которые main.py не смог скачать (адреса берутся из [!]-строк лога).

У swap-api.pump.fun фактический лимит ~20 запросов в минуту на IP (заголовки обещают 1000),
поэтому качаем экономно. Время создания токена оцениваем по логу: main.py пишет [статус] раз
в 300 с, а ошибка появляется через record_sec после создания. Курсор вида "x-<unix ms>" означает
«сделки раньше этого момента» — ставим его чуть позже конца окна и листаем назад до начала
истории. Обычно это одна страница.

Событие create для этих токенов не сохранилось, поэтому считаем, что первая сделка — покупка
дева в транзакции создания (у токенов без покупки дева момент создания сдвинется на первую сделку).
Токены обрабатываются в случайном порядке, так что частичная дозагрузка — честная выборка.

    python backfill.py run.log --start 2026-10-08T11:37:14Z --from-line 907 --to-line 21569
"""
import argparse
import asyncio
import json
import os
import random
import re
import time
from datetime import datetime

from config import Config
from main import RECORD_DIR, STATUS_SEC, TRADES_API, PaperSniper, time_from_iso

REQUESTS_PER_MIN = 12  # остальное от ~20/мин оставляем живому main.py
MARGIN_SEC = 120       # запас на ретраи и дрейф таймера статуса
FALLBACK_LOOKBACK_SEC = 8 * 3600
SEARCH_PRECISION_SEC = 30


class Backfiller:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.sniper = PaperSniper(cfg, telegram=False)
        self.done = self.empty = self.failed = self.requests = 0

    async def page(self, mint: str, cursor: str, limit: int = 100) -> dict:
        await asyncio.sleep(60 / REQUESTS_PER_MIN)
        self.requests += 1
        return await self.sniper.get_json(TRADES_API.format(mint=mint), {"limit": limit, "cursor": cursor})

    async def fetch(self, mint: str, created_before: float) -> list[dict]:
        rows, oldest = await self.fetch_before(mint, created_before + self.cfg.record_sec + MARGIN_SEC)
        if oldest is not None:
            # оценка уехала далеко вперёд (очередь в main.py), а токен активный — ищем начало бинарным поиском
            start = await self.find_start(mint, oldest - FALLBACK_LOOKBACK_SEC, oldest)
            rows, _ = await self.fetch_before(mint, start + SEARCH_PRECISION_SEC + self.cfg.record_sec,
                                              max_pages=100)
        rows = sorted({r["id"]: r for r in rows}.values(), key=lambda r: r["id"])
        if rows:
            end = rows[0]["ts"] + self.cfg.record_sec
            rows = [r for r in rows if r["ts"] <= end]
        return rows

    async def find_start(self, mint: str, lo: float, hi: float) -> float:
        while hi - lo > SEARCH_PRECISION_SEC:
            mid = (lo + hi) / 2
            data = await self.page(mint, f"x-{int(mid * 1000)}", limit=1)
            if data["trades"]:
                hi = mid
            else:
                lo = mid
        return lo

    async def fetch_before(self, mint: str, before: float, max_pages: int = 3) -> tuple[list[dict], float | None]:
        """Сделки на bonding curve раньше before, листая назад. Второе значение — время, до которого
        дошли, если за max_pages не добрались до начала истории (None — история целиком)."""
        # дальше листаем настоящим nextCursor: в нём есть slotIndexId, и сделки одной секунды не теряются
        rows, cursor = [], f"x-{int(before * 1000)}"
        for _ in range(max_pages):
            data = await self.page(mint, cursor)
            for tr in data["trades"]:
                if tr.get("program") == "pump" and tr.get("type") in ("buy", "sell"):
                    rows.append({
                        "id": tr["slotIndexId"], "slot": int(tr["slotIndexId"][:12]), "tx": tr["tx"],
                        "ts": time_from_iso(tr["timestamp"]), "user": tr["userAddress"],
                        "is_buy": tr["type"] == "buy", "sol": float(tr["amountSol"]),
                        "tokens": float(tr["baseAmount"]),
                        "price": float(tr["priceSol"]), "fill_price": float(tr["fillPriceSol"]),
                    })
            cursor = data["pagination"].get("nextCursor")
            if not data["pagination"].get("hasMore") or not cursor:
                return rows, None
        return rows, int(cursor.rsplit("-", 1)[1]) / 1000  # курсор: "<slotIndexId>-<unix ms>"

    async def one(self, mint: str, created_before: float):
        try:
            trades = await self.fetch(mint, created_before)
        except Exception as e:
            self.failed += 1
            print(f"[!] {mint}: {type(e).__name__}: {e}", flush=True)
            return
        if not trades:
            self.empty += 1
            return
        first = trades[0]
        token = {
            "mint": mint, "signature": first["tx"], "created_at": first["ts"],
            "fetched_at": first["ts"] + self.cfg.record_sec, "creator": first["user"],
            "dev_buy_sol": first["sol"] if first["is_buy"] else 0.0,
            "name": None, "symbol": None, "backfilled": True, "trades": trades,
        }
        with open(os.path.join(RECORD_DIR, f"{mint}.json"), "w") as f:
            json.dump(token, f)
        self.done += 1

    async def run(self, jobs: list[tuple[str, float]]):
        started = time.time()
        for i, (mint, created_before) in enumerate(jobs, 1):
            await self.one(mint, created_before)
            if i % 50 == 0:
                rate = i / (time.time() - started) * 3600
                print(f"[backfill] {i}/{len(jobs)}: записано {self.done}, пустых {self.empty}, "
                      f"ошибок {self.failed}, запросов {self.requests}, ~{rate:.0f} токенов/ч", flush=True)
        print(f"[backfill] готово: записано {self.done}, пустых {self.empty}, ошибок {self.failed}", flush=True)


def parse_jobs(log: str, start: float, from_line: int, to_line: int) -> list[tuple[str, float]]:
    """[(mint, создан не позже)] для упавших токенов из куска лога одного запуска main.py."""
    existing = {f.removesuffix(".json") for f in os.listdir(RECORD_DIR)}
    jobs, seen, statuses = [], set(), 0
    with open(log) as f:
        for n, line in enumerate(f, 1):
            if n < from_line or n > to_line:
                continue
            if line.startswith("[статус]"):
                statuses += 1
                continue
            m = re.match(r"\[!\] (\w+):", line)
            if m and m.group(1) not in existing and m.group(1) not in seen:
                seen.add(m.group(1))
                # строка напечатана до (statuses + 1)-го статуса, т.е. не позже start + 300 * (statuses + 1)
                jobs.append((m.group(1), start + STATUS_SEC * (statuses + 1) - Config.record_sec))
    return jobs


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("log")
    p.add_argument("--start", required=True, help="время запуска main.py, ISO (из journalctl)")
    p.add_argument("--from-line", type=int, required=True)
    p.add_argument("--to-line", type=int, required=True)
    args = p.parse_args()

    start = datetime.fromisoformat(args.start.replace("Z", "+00:00")).timestamp()
    jobs = parse_jobs(args.log, start, args.from_line, args.to_line)
    random.Random(0).shuffle(jobs)
    print(f"[backfill] к загрузке: {len(jobs)}, ~{len(jobs) * 60 / REQUESTS_PER_MIN / 3600:.0f} ч "
          f"при одном запросе на токен", flush=True)
    asyncio.run(Backfiller(Config.from_env()).run(jobs))


if __name__ == "__main__":
    main()

"""Снайпер pump.fun в режиме dry-run (paper trading) + сбор данных для бэктеста.

Поток сделок PumpPortal теперь платный (API-ключ с балансом ≥ 0.02 SOL), бесплатно приходят
только события создания токенов. Поэтому бот работает с задержкой:
  1. ловит новый токен через вебсокет PumpPortal;
  2. через record_sec скачивает всю его историю сделок из публичного swap-api.pump.fun
     и сохраняет в recorded/ (для backtest.py);
  3. прогоняет на этой истории стратегию так, как если бы торговал вживую: решение принимается
     только по данным первых window_sec, вход и выход — по реальным резервам кривой
     с задержкой, слиппеджем и комиссиями. Результат пишется в paper_trades.jsonl.
Никаких транзакций не отправляется.

    python main.py
    python main.py --set take_profit_pct=80 --set stop_loss_pct=-25 --no-tg
    python report.py        # статистика по накопленным сделкам

Уведомления в Telegram — если в .env заданы TG_TOKEN и TG_CHAT_IDS (отключаются --no-tg).
"""
import argparse
import asyncio
import json
import os
import random
import time

import httpx
import websockets

from backtest import load_recorded, signal_time, simulate_token
from config import Config, parse_sets
from sim import (INITIAL_V_TOKENS, early_features, format_summary, passes_filters, progress_pct,
                 summarize)

WS_URI = "wss://pumpportal.fun/api/data"
TRADES_API = "https://swap-api.pump.fun/v2/coins/{mint}/trades"
TRADES_FILE = "paper_trades.jsonl"
RECORD_DIR = "recorded"
MAX_PAGES = 100  # по 100 сделок
STATUS_SEC = 300
REFETCH_MIN_PROGRESS = 40.0           # % распроданной кривой к первой загрузке
REFETCH_AFTER_SEC = (40 * 60, 3 * 3600)  # от создания токена


def max_progress(trades: list[dict]) -> float:
    """Максимальный % распроданной кривой по сделкам (токены в кривой считаем по объёмам)."""
    v_tokens, best = INITIAL_V_TOKENS, 0.0
    for tr in trades:
        v_tokens += -tr["tokens"] if tr["is_buy"] else tr["tokens"]
        best = max(best, progress_pct(v_tokens))
    return best


def last_id(token: dict) -> str | None:
    ids = [r["id"] for r in token["trades"] + token.get("amm_trades", [])]
    return max(ids) if ids else None


def merge(old: list[dict], new: list[dict]) -> list[dict]:
    return sorted({r["id"]: r for r in old + new}.values(), key=lambda r: r["id"])


class PaperSniper:
    def __init__(self, cfg: Config, telegram: bool = True):
        self.cfg = cfg
        self.results = []
        self.seen = 0
        self.recorded = 0
        self.refetched = 0
        self.rep = self.load_rep() if cfg.use_rep else {}
        self.http = httpx.AsyncClient(timeout=20)
        self.fetch_limit = asyncio.Semaphore(3)
        self.tg_token = os.environ.get("TG_TOKEN") if telegram else None
        self.tg_chats = [c for c in os.environ.get("TG_CHAT_IDS", "").split(",") if c]
        os.makedirs(RECORD_DIR, exist_ok=True)

    def load_rep(self) -> dict:
        with open(self.cfg.rep_file) as f:
            return json.load(f)

    async def run(self):
        status = asyncio.create_task(self.status_loop())
        while True:
            try:
                # PumpPortal не всегда отвечает на ping — живость проверяем по входящим сообщениям
                async with websockets.connect(WS_URI, max_size=None, ping_interval=None) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    print("Подключено к PumpPortal, жду новые токены…", flush=True)
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), 120)
                        except asyncio.TimeoutError:
                            print("2 минуты без новых токенов, переподключаюсь", flush=True)
                            break
                        msg = json.loads(raw)
                        if msg.get("txType") == "create" and msg.get("pool", "pump") == "pump":
                            self.seen += 1
                            asyncio.create_task(self.process(msg, time.time()))
            # WebSocketException — в т.ч. отказ сервера при рукопожатии (InvalidStatus): падать нельзя,
            # иначе пропадут запланированные загрузки токенов
            except (websockets.exceptions.WebSocketException, OSError, asyncio.TimeoutError) as e:
                print(f"Соединение потеряно ({e}), переподключаюсь через 3 с", flush=True)
                await asyncio.sleep(3)

    async def process(self, create: dict, received_at: float):
        mint = create["mint"]
        try:
            await asyncio.sleep(self.cfg.record_sec)
            async with self.fetch_limit:
                trades, amm = await self.fetch_trades(mint)
            token = {
                "mint": mint, "signature": create["signature"], "created_at": received_at,
                "fetched_at": time.time(), "creator": create.get("traderPublicKey"),
                "dev_buy_sol": create.get("solAmount", 0.0), "name": create.get("name"),
                "symbol": create.get("symbol"), "trades": trades, "amm_trades": amm,
            }
            self.save(token)
            self.recorded += 1
            await self.paper_trade(token)

            # Токены, которые идут к миграции, докачиваем позже вместе со сделками на PumpSwap:
            # иначе стратегию «купить до миграции, продать после» не проверить.
            if max_progress(trades) < REFETCH_MIN_PROGRESS:
                return
            for after_sec in REFETCH_AFTER_SEC:
                await asyncio.sleep(max(0.0, received_at + after_sec - time.time()))
                async with self.fetch_limit:
                    new_trades, new_amm = await self.fetch_trades(mint, since_id=last_id(token))
                token["trades"] = merge(token["trades"], new_trades)
                token["amm_trades"] = merge(token["amm_trades"], new_amm)
                token["fetched_at"] = time.time()
                token["refetches"] = token.get("refetches", 0) + 1
                self.save(token)
                self.refetched += 1
                if not token["amm_trades"]:  # так и не мигрировал — дальше не следим
                    return
        except Exception as e:
            print(f"[!] {mint}: {type(e).__name__}: {e}", flush=True)

    def save(self, token: dict):
        with open(os.path.join(RECORD_DIR, f"{token['mint']}.json"), "w") as f:
            json.dump(token, f)

    async def fetch_trades(self, mint: str, since_id: str | None = None) -> tuple[list[dict], list[dict]]:
        """Сделки на bonding curve и на PumpSwap (после миграции), по порядку исполнения.
        since_id — листаем от новых к старым только до уже известной сделки."""
        curve, amm, cursor = [], [], None
        for _ in range(MAX_PAGES):
            params = {"limit": 100, **({"cursor": cursor} if cursor else {})}
            data = await self.get_json(TRADES_API.format(mint=mint), params)
            reached_known = False
            for tr in data["trades"]:
                if since_id and tr["slotIndexId"] <= since_id:
                    reached_known = True
                    break
                if tr.get("type") not in ("buy", "sell") or tr.get("program") not in ("pump", "pump_amm"):
                    continue
                row = {
                    "id": tr["slotIndexId"], "slot": int(tr["slotIndexId"][:12]), "tx": tr["tx"],
                    "ts": time_from_iso(tr["timestamp"]), "user": tr["userAddress"],
                    "is_buy": tr["type"] == "buy", "sol": float(tr["amountSol"]),
                    "tokens": float(tr["baseAmount"]),
                    # цена от API: у части токенов кривая нестандартная, и по объёмам её не восстановить
                    "price": float(tr["priceSol"]), "fill_price": float(tr["fillPriceSol"]),
                }
                (curve if tr["program"] == "pump" else amm).append(row)
            cursor = data["pagination"].get("nextCursor")
            if reached_known or not data["pagination"].get("hasMore") or not cursor:
                break
        else:
            print(f"[!] {mint}: больше {MAX_PAGES} страниц, часть сделок пропущена", flush=True)
        return sorted(curve, key=lambda r: r["id"]), sorted(amm, key=lambda r: r["id"])

    async def get_json(self, url: str, params: dict) -> dict:
        attempts = 10
        for attempt in range(attempts):
            try:
                r = await self.http.get(url, params=params)
                if r.status_code == 429 or r.status_code >= 500:
                    raise httpx.HTTPStatusError(str(r.status_code), request=r.request, response=r)
                r.raise_for_status()
                return r.json()
            except httpx.HTTPError:
                if attempt == attempts - 1:
                    raise
                # лимит pump.fun жёсткий: ждём дольше, с разбросом, чтобы запросы не шли пачкой
                await asyncio.sleep(min(60, 2 ** attempt) * (0.5 + random.random()))

    async def paper_trade(self, raw: dict):
        tok = load_recorded(raw)
        if tok is None:
            return
        feats = early_features(tok.trades, signal_time(self.cfg, tok), self.rep)
        if not passes_filters(self.cfg, tok.dev_buy, feats):
            return
        r = simulate_token(self.cfg, tok)
        self.results.append(r)
        with open(TRADES_FILE, "a") as f:
            f.write(json.dumps({**r.to_dict(), "config": vars(self.cfg)}) + "\n")
        if r.status == "closed":
            move = (r.exit_price / r.entry_price - 1) * 100
            text = (f"[DRY] {raw.get('symbol') or ''} {r.mint}\n"
                    f"{r.reason}, держали {r.closed_at - r.opened_at:.0f} с, цена {move:+.1f}%, "
                    f"PnL {r.pnl_sol:+.5f} SOL")
        else:
            text = f"[DRY] Не купился {r.mint}: цена ушла дальше слиппеджа"
        s = summarize(self.results, self.cfg)
        text += (f"\nИтого: {s['trades']} сделок, PnL {s['total_pnl_sol']:+.4f} SOL, "
                 f"ROI {s['roi_pct']:+.1f}%, винрейт {s['winrate_pct']:.0f}%")
        print(text, flush=True)
        await self.notify(text)

    async def status_loop(self):
        while True:
            await asyncio.sleep(STATUS_SEC)
            print(f"[статус] новых токенов: {self.seen}, записано: {self.recorded}, "
                  f"докачано (к миграции): {self.refetched}, "
                  f"сделок dry-run: {len(self.results)}", flush=True)

    async def notify(self, text: str):
        if not self.tg_token:
            return
        for chat_id in self.tg_chats:
            try:
                await self.http.post(f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                                     json={"chat_id": chat_id, "text": text})
            except httpx.HTTPError as e:
                print(f"Ошибка отправки в Telegram {chat_id}: {e}")


def time_from_iso(s: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--set", action="append", metavar="KEY=VALUE", help="переопределить параметр из config.py")
    p.add_argument("--no-tg", action="store_true", help="не слать уведомления в Telegram")
    args = p.parse_args()

    cfg = Config.from_env(**parse_sets(args.set))
    print(f"DRY-RUN: вход {cfg.amount_sol} SOL, dev buy ≥ {cfg.min_dev_buy_sol} SOL, "
          f"TP {cfg.take_profit_pct}%, SL {cfg.stop_loss_pct}% от пика, задержка {cfg.latency_ms:.0f} мс. "
          f"Результат по каждому токену — через {cfg.record_sec:.0f} с после создания", flush=True)
    sniper = PaperSniper(cfg, telegram=not args.no_tg)
    try:
        asyncio.run(sniper.run())
    except KeyboardInterrupt:
        print("\nИтоги сессии:")
        print(format_summary(summarize(sniper.results, cfg), cfg))


if __name__ == "__main__":
    main()

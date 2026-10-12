import asyncio
import websockets
import json
import os

MINT_FILE = "mint.json"
MAX_MINTS = 100

async def listen_to_new_tokens():
    uri = "wss://pumpportal.fun/api/data"
    collected_mints = set()

    if os.path.exists(MINT_FILE):
        with open(MINT_FILE, "r") as f:
            try:
                existing = json.load(f)
                collected_mints.update(existing)
            except json.JSONDecodeError:
                pass

    async with websockets.connect(uri) as websocket:

        subscribe_message = {
            "method": "subscribeNewToken"
        }
        await websocket.send(json.dumps(subscribe_message))

        async for message in websocket:
            try:
                data = json.loads(message)
                if "signature" in data and data.get("pool") == "pump":
                    mint = data.get("mint")
                    if mint and mint not in collected_mints:
                        collected_mints.add(mint)
                        print(f"[+] Найден mint в пуле 'pump': {mint} ({len(collected_mints)}/{MAX_MINTS})")

                        with open(MINT_FILE, "w") as f:
                            json.dump(list(collected_mints), f, indent=2)

                        if len(collected_mints) >= MAX_MINTS:
                            print(f"[✓] Собрано {MAX_MINTS} уникальных mint'ов из пула 'pump'. Завершение.")
                            break
            except Exception as e:
                print(f"[!] Ошибка обработки сообщения: {e}")

if __name__ == "__main__":
    asyncio.run(listen_to_new_tokens())

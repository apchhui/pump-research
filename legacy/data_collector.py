import requests
import json
import os

PATH = "mints/"
os.makedirs(PATH, exist_ok=True)

with open("mint.json", "r") as f:
    MINTS = json.load(f)

for mint in MINTS:
    uri = f"https://frontend-api-v3.pump.fun/trades/count/{mint}?minimumSize=0"
    response = requests.get(uri)
    url = f"https://frontend-api-v3.pump.fun/trades/all/{mint}?limit={response.text}&offset=0&minimumSize=0"
    
    try:
        response = requests.get(url)
        response.raise_for_status()  # Проверка на ошибки HTTP
        data = response.json()

        with open(f"{PATH}{mint}.json", "w") as f:
            json.dump(data, f, indent=2)

        print(f"[+] Сохранил трейды для {mint}")

    except Exception as e:
        print(f"[!] Ошибка при получении {mint}: {e}")

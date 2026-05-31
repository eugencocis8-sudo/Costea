import time
import requests
from flask import Flask
import threading

app = Flask(__name__)

@app.route('/')
def home():
    return "Paper Trend Bot Running"

def run_web():
    app.run(host='0.0.0.0', port=10000)

SYMBOL = "BTCUSDT"
VIRTUAL_USDT = 1000.0
BTC_AMOUNT = 0.0
POSITION = None
ENTRY_PRICE = 0.0

TREND_MIN_PERCENT = 0.80
CANDLE_RATIO_MIN = 70.0

def get_24h_candles():
    url = "https://api.bitget.com/api/v2/spot/market/candles"
    params = {
        "symbol": SYMBOL,
        "granularity": "1h",
        "limit": "24"
    }

    r = requests.get(url, params=params, timeout=10)
    data = r.json()

    if "data" not in data or not data["data"]:
        print("Raspuns Bitget:", data, flush=True)
        return []

    candles = data["data"]
    candles.sort(key=lambda x: int(x[0]))
    return candles

def analyze_trend(candles):
    first_open = float(candles[0][1])
    last_close = float(candles[-1][4])

    total_change = ((last_close - first_open) / first_open) * 100

    green = 0
    red = 0

    for candle in candles:
        open_price = float(candle[1])
        close_price = float(candle[4])

        if close_price > open_price:
            green += 1
        elif close_price < open_price:
            red += 1

    total_candles = green + red

    if total_candles == 0:
        green_percent = 0
        red_percent = 0
    else:
        green_percent = (green / total_candles) * 100
        red_percent = (red / total_candles) * 100

    return total_change, green_percent, red_percent, last_close

threading.Thread(target=run_web, daemon=True).start()

print("Paper Trend Bot pornit cu 1000 USDT virtuali", flush=True)

while True:
    try:
        print("Analizez piata Bitget...", flush=True)

        candles = get_24h_candles()

        if not candles:
            print("Nu am primit candles de la Bitget.", flush=True)
            time.sleep(30)
            continue

        total_change, green_percent, red_percent, price = analyze_trend(candles)

        print("--------------------------------------------------", flush=True)
        print(f"Simbol: {SYMBOL}", flush=True)
        print(f"Pret actual: {price}", flush=True)
        print(f"Trend 24h: {total_change:.2f}%", flush=True)
        print(f"Lumanari crestere: {green_percent:.2f}%", flush=True)
        print(f"Lumanari scadere: {red_percent:.2f}%", flush=True)
        print(f"Pozitie: {POSITION}", flush=True)
        print(f"USDT virtual: {VIRTUAL_USDT:.2f}", flush=True)
        print(f"BTC virtual: {BTC_AMOUNT:.8f}", flush=True)

        if POSITION is None:
            if total_change >= TREND_MIN_PERCENT and green_percent >= CANDLE_RATIO_MIN:
                BTC_AMOUNT = VIRTUAL_USDT / price
                ENTRY_PRICE = price
                VIRTUAL_USDT = 0.0
                POSITION = "LONG"
                print(f"BUY virtual la {price}", flush=True)
            else:
                print("Nu cumpar: conditiile BUY nu sunt indeplinite.", flush=True)

        elif POSITION == "LONG":
            if total_change <= -TREND_MIN_PERCENT and red_percent >= CANDLE_RATIO_MIN:
                VIRTUAL_USDT = BTC_AMOUNT * price
                pnl = VIRTUAL_USDT - 1000.0
                BTC_AMOUNT = 0.0
                POSITION = None
                print(f"SELL virtual la {price}", flush=True)
                print(f"Profit/Pierdere total: {pnl:.2f} USDT", flush=True)
            else:
                print("Tin pozitia: conditiile SELL nu sunt indeplinite.", flush=True)

        time.sleep(30)

    except Exception as e:
        print("Eroare:", e, flush=True)
        time.sleep(10)

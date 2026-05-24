import time
import requests
from flask import Flask
import threading

# ---------------- WEB SERVER PENTRU RENDER ---------------- #

app = Flask(__name__)

@app.route('/')
def home():
    return "Paper Trend Bot Running"

def run_web():
    app.run(host='0.0.0.0', port=10000)

# ---------------- BOT SETTINGS ---------------- #

SYMBOL = "BTCUSDT"
VIRTUAL_USDT = 1000.0
BTC_AMOUNT = 0.0
POSITION = None
ENTRY_PRICE = 0.0

TREND_MIN_PERCENT = 0.80
CANDLE_RATIO_MIN = 70.0

# ---------------- GET CANDLES ---------------- #

def get_24h_candles():
    url = "https://api.bitget.com/api/v2/spot/market/candles"

    params = {
        "symbol": SYMBOL,
        "granularity": "1h",
        "limit": "24"
    }

    r = requests.get(url, params=params, timeout=10)
    data = r.json()

    candles = data["data"]

    candles.sort(key=lambda x: int(x[0]))

    return candles

# ---------------- ANALYZE TREND ---------------- #

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

# ---------------- START ---------------- #

threading.Thread(target=run_web).start()

print("Paper Trend Bot pornit cu 1000 USDT virtuali")

# ---------------- MAIN LOOP ---------------- #

while True:

    try:

        candles = get_24h_candles()

        total_change, green_percent, red_percent, price = analyze_trend(candles)

        print("--------------------------------------------------")
        print(f"Simbol: {SYMBOL}")
        print(f"Pret actual: {price}")
        print(f"Trend 24h: {total_change:.2f}%")
        print(f"Lumanari crestere: {green_percent:.2f}%")
        print(f"Lumanari scadere: {red_percent:.2f}%")
        print(f"Pozitie: {POSITION}")
        print(f"USDT virtual: {VIRTUAL_USDT:.2f}")
        print(f"BTC virtual: {BTC_AMOUNT:.8f}")

        # ---------------- BUY ---------------- #

        if POSITION is None:

            if total_change >= TREND_MIN_PERCENT and green_percent >= CANDLE_RATIO_MIN:

                BTC_AMOUNT = VIRTUAL_USDT / price

                ENTRY_PRICE = price

                VIRTUAL_USDT = 0.0

                POSITION = "LONG"

                print(f"BUY virtual la {price}")

        # ---------------- SELL ---------------- #

        elif POSITION == "LONG":

            if total_change <= -TREND_MIN_PERCENT and red_percent >= CANDLE_RATIO_MIN:

                VIRTUAL_USDT = BTC_AMOUNT * price

                pnl = VIRTUAL_USDT - 1000.0

                BTC_AMOUNT = 0.0

                POSITION = None

                print(f"SELL virtual la {price}")
                print(f"Profit/Pierdere total: {pnl:.2f} USDT")

        time.sleep(300)

    except Exception as e:

        print("Eroare:", e)

        time.sleep(60)

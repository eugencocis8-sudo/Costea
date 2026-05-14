import requests
import time
import csv
import os
import json
from datetime import datetime

# ============================================
# BTC SEMAFOR PAPER BOT
# LIVE BITGET DATA
# FARA BANI REALI
# 6 SUME VIRTUALE
# SAVE / LOAD ACTIV
# ============================================

SYMBOL = "BTCUSDT"
START_BALANCE = 1000.0
CHECK_INTERVAL = 60
STATE_FILE = "bot_state.json"
LOG_FILE = "trades_log.csv"

BOTS = {
    "BOT_30M": {"minutes": 30},
    "BOT_1H": {"minutes": 60},
    "BOT_2H": {"minutes": 120},
    "BOT_3H": {"minutes": 180},
    "BOT_4H": {"minutes": 240},
    "BOT_5H": {"minutes": 300}
}

state = {}


def create_default_state():
    new_state = {}

    for bot_name in BOTS:
        new_state[bot_name] = {
            "usdt": START_BALANCE,
            "btc": 0.0,
            "position": "USDT",
            "last_action": "NONE"
        }

    return new_state


def save_state():
    with open(STATE_FILE, "w") as file:
        json.dump(state, file, indent=4)


def load_state():
    global state

    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as file:
            loaded_state = json.load(file)

        state = create_default_state()

        for bot_name in BOTS:
            if bot_name in loaded_state:
                state[bot_name] = loaded_state[bot_name]

        print("STATE LOADED FROM bot_state.json")

    else:
        state = create_default_state()
        save_state()
        print("NO SAVED STATE. NEW PAPER ACCOUNT CREATED.")


def get_candles(limit=300):
    url = "https://api.bitget.com/api/v2/spot/market/candles"

    params = {
        "symbol": SYMBOL,
        "granularity": "1min",
        "limit": str(limit)
    }

    response = requests.get(url, params=params, timeout=10)
    data = response.json()

    if data.get("code") != "00000":
        raise Exception(data)

    candles = data["data"]
    candles.reverse()

    closes = [float(candle[4]) for candle in candles]

    return closes


def candle_color(prev_price, current_price):
    if current_price > prev_price:
        return "GREEN"
    elif current_price < prev_price:
        return "RED"
    else:
        return "YELLOW"


def analyze_market(closes, minutes):
    selected = closes[-minutes:]

    green = 0
    red = 0
    yellow = 0

    for i in range(1, len(selected)):
        color = candle_color(selected[i - 1], selected[i])

        if color == "GREEN":
            green += 1
        elif color == "RED":
            red += 1
        else:
            yellow += 1

    total = green + red + yellow

    if total == 0:
        return {
            "signal": "HOLD",
            "green": green,
            "red": red,
            "yellow": yellow,
            "green_percent": 0,
            "red_percent": 0
        }

    green_percent = (green / total) * 100
    red_percent = (red / total) * 100

    signal = "HOLD"

    if green_percent >= 80:
        signal = "BUY"
    elif red_percent >= 80:
        signal = "SELL"

    return {
        "signal": signal,
        "green": green,
        "red": red,
        "yellow": yellow,
        "green_percent": green_percent,
        "red_percent": red_percent
    }


def portfolio_value(bot, price):
    return bot["usdt"] + bot["btc"] * price


def log_trade(bot_name, action, price, green_percent, red_percent, value):
    file_exists = os.path.exists(LOG_FILE)

    with open(LOG_FILE, "a", newline="") as file:
        writer = csv.writer(file)

        if not file_exists:
            writer.writerow([
                "time",
                "bot",
                "action",
                "price",
                "green_percent",
                "red_percent",
                "portfolio_value"
            ])

        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            bot_name,
            action,
            round(price, 2),
            round(green_percent, 2),
            round(red_percent, 2),
            round(value, 2)
        ])


def print_start_info():
    print("===================================")
    print(" BTC SEMAFOR PAPER BOT")
    print(" LIVE BITGET DATA")
    print(" FARA BANI REALI")
    print(" 6 SUME VIRTUALE")
    print(" 30M / 1H / 2H / 3H / 4H / 5H")
    print(" BUY >= 80% GREEN")
    print(" SELL >= 80% RED")
    print(" SAVE / LOAD ACTIV")
    print("===================================")


load_state()
print_start_info()

while True:
    try:
        closes = get_candles(300)
        current_price = closes[-1]

        print("\n===================================")
        print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        print(f"BTC PRICE: {current_price:.2f} USDT")
        print("===================================")

        for bot_name, config in BOTS.items():
            bot = state[bot_name]

            result = analyze_market(closes, config["minutes"])
            signal = result["signal"]

            action = "HOLD"

            if signal == "BUY" and bot["position"] == "USDT":
                bot["btc"] = bot["usdt"] / current_price
                bot["usdt"] = 0.0
                bot["position"] = "BTC"
                action = "BUY ALL"

                save_state()

            elif signal == "SELL" and bot["position"] == "BTC":
                bot["usdt"] = bot["btc"] * current_price
                bot["btc"] = 0.0
                bot["position"] = "USDT"
                action = "SELL ALL"

                save_state()

            value = portfolio_value(bot, current_price)
            bot["last_action"] = action
            save_state()

            print(
                f"{bot_name} | "
                f"GREEN: {result['green_percent']:.1f}% | "
                f"RED: {result['red_percent']:.1f}% | "
                f"SIGNAL: {signal} | "
                f"ACTION: {action} | "
                f"VALUE: {value:.2f} USDT | "
                f"POSITION: {bot['position']}"
            )

            if action != "HOLD":
                log_trade(
                    bot_name,
                    action,
                    current_price,
                    result["green_percent"],
                    result["red_percent"],
                    value
                )

        time.sleep(CHECK_INTERVAL)

    except Exception as error:
        print("ERROR:", error)
        time.sleep(10)
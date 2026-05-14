import requests
import time
import csv
import os
import json
from datetime import datetime

SYMBOL = "BTCUSDT"
START_BALANCE = 1000.0
CHECK_INTERVAL = 60

MIN_MOVE_PERCENT = 0.10
DOMINANCE_PERCENT = 80.0

STATE_FILE = "percent_momentum_state.json"
LOG_FILE = "percent_momentum_trades.csv"

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
    return {
        bot_name: {
            "usdt": START_BALANCE,
            "btc": 0.0,
            "position": "USDT",
            "last_action": "NONE"
        }
        for bot_name in BOTS
    }


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

        print("STATE LOADED")
    else:
        state = create_default_state()
        save_state()
        print("NEW PAPER ACCOUNT CREATED")


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


def analyze_percent_momentum(closes, minutes):
    selected = closes[-minutes:]

    base_price = selected[0]
    current_price = selected[-1]

    plus_count = 0
    minus_count = 0
    neutral_count = 0

    for price in selected[1:]:
        change_percent = ((price - base_price) / base_price) * 100

        if change_percent >= MIN_MOVE_PERCENT:
            plus_count += 1
        elif change_percent <= -MIN_MOVE_PERCENT:
            minus_count += 1
        else:
            neutral_count += 1

    total = plus_count + minus_count + neutral_count

    if total == 0:
        plus_percent = 0
        minus_percent = 0
        neutral_percent = 0
    else:
        plus_percent = (plus_count / total) * 100
        minus_percent = (minus_count / total) * 100
        neutral_percent = (neutral_count / total) * 100

    interval_change = ((current_price - base_price) / base_price) * 100

    signal = "HOLD"

    if plus_percent >= DOMINANCE_PERCENT:
        signal = "BUY"
    elif minus_percent >= DOMINANCE_PERCENT:
        signal = "SELL"

    return {
        "signal": signal,
        "base_price": base_price,
        "current_price": current_price,
        "interval_change": interval_change,
        "plus_percent": plus_percent,
        "minus_percent": minus_percent,
        "neutral_percent": neutral_percent,
        "plus_count": plus_count,
        "minus_count": minus_count,
        "neutral_count": neutral_count
    }


def portfolio_value(bot, price):
    return bot["usdt"] + bot["btc"] * price


def log_trade(bot_name, action, price, result, value):
    file_exists = os.path.exists(LOG_FILE)

    with open(LOG_FILE, "a", newline="") as file:
        writer = csv.writer(file)

        if not file_exists:
            writer.writerow([
                "time",
                "bot",
                "action",
                "price",
                "interval_change",
                "plus_percent",
                "minus_percent",
                "neutral_percent",
                "portfolio_value"
            ])

        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            bot_name,
            action,
            round(price, 2),
            round(result["interval_change"], 4),
            round(result["plus_percent"], 2),
            round(result["minus_percent"], 2),
            round(result["neutral_percent"], 2),
            round(value, 2)
        ])


def print_start_info():
    print("===================================")
    print(" BTC PERCENT MOMENTUM PAPER BOT")
    print(" LIVE BITGET DATA")
    print(" FARA BANI REALI")
    print(" 6 SUME VIRTUALE")
    print(" COMPARA CU INCEPUTUL INTERVALULUI")
    print(" BUY  = 80% timp peste +0.10%")
    print(" SELL = 80% timp sub -0.10%")
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

            result = analyze_percent_momentum(closes, config["minutes"])
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
                f"CHANGE: {result['interval_change']:.4f}% | "
                f"PLUS: {result['plus_percent']:.1f}% | "
                f"MINUS: {result['minus_percent']:.1f}% | "
                f"NEUTRU: {result['neutral_percent']:.1f}% | "
                f"SIGNAL: {signal} | "
                f"ACTION: {action} | "
                f"VALUE: {value:.2f} USDT | "
                f"POSITION: {bot['position']}"
            )

            if action != "HOLD":
                log_trade(bot_name, action, current_price, result, value)

        time.sleep(CHECK_INTERVAL)

    except Exception as error:
        print("ERROR:", error)
        time.sleep(10)
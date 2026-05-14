import time
import requests
import pandas as pd

SYMBOL = "BTCUSDT"
GRANULARITY = "5min"
START_BALANCE = 10.0
LOOP_SECONDS = 20

balance = START_BALANCE
in_position = False
entry_price = 0.0
btc_amount = 0.0


def get_data():
    url = "https://api.bitget.com/api/v2/spot/market/candles"
    params = {
        "symbol": SYMBOL,
        "granularity": GRANULARITY,
        "limit": "100"
    }

    r = requests.get(url, params=params, timeout=10)
    data = r.json()["data"]

    df = pd.DataFrame(data)
    df = df.iloc[:, :5]
    df.columns = ["time", "open", "high", "low", "close"]

    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)

    return df


def signal(df):
    ma5 = df["close"].rolling(5).mean()
    ma20 = df["close"].rolling(20).mean()

    last = df["close"].iloc[-1]

    if ma5.iloc[-2] < ma20.iloc[-2] and ma5.iloc[-1] > ma20.iloc[-1]:
        return "BUY", last

    if ma5.iloc[-2] > ma20.iloc[-2] and ma5.iloc[-1] < ma20.iloc[-1]:
        return "SELL", last

    return "HOLD", last


print("Starting Multi Strategy Simple Tester")

while True:
    try:
        df = get_data()
        sig, price = signal(df)

        if not in_position and sig == "BUY":
            btc_amount = balance / price
            entry_price = price
            in_position = True
            print(f"BUY at {price:.2f}")

        elif in_position and sig == "SELL":
            balance = btc_amount * price
            profit = balance - START_BALANCE
            in_position = False
            btc_amount = 0
            print(f"SELL at {price:.2f} | BALANCE {balance:.4f} | PROFIT {profit:+.4f}")

        else:
            if in_position:
                live = btc_amount * price
                profit = live - START_BALANCE
                print(f"IN TRADE | PRICE {price:.2f} | BALANCE {live:.4f} | PROFIT {profit:+.4f}")
            else:
                print(f"WAIT | PRICE {price:.2f} | BALANCE {balance:.4f}")

    except Exception as e:
        print("ERROR:", e)

    time.sleep(LOOP_SECONDS)
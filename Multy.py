import time
import requests
import pandas as pd

SYMBOL = "BTCUSDT"
GRANULARITY = "5min"
START_BALANCE = 10.0
LOOP_SECONDS = 20

FEE_AND_SLIP = 0.0025  # 0.25% cost estimat buy+sell+slippage

strategies = [
    {"name": "MA5/20", "type": "ma", "fast": 5, "slow": 20, "balance": START_BALANCE, "in": False},
    {"name": "MA7/25", "type": "ma", "fast": 7, "slow": 25, "balance": START_BALANCE, "in": False},
    {"name": "MA10/40", "type": "ma", "fast": 10, "slow": 40, "balance": START_BALANCE, "in": False},
    {"name": "EMA9/21", "type": "ema", "fast": 9, "slow": 21, "balance": START_BALANCE, "in": False},
    {"name": "EMA12/26", "type": "ema", "fast": 12, "slow": 26, "balance": START_BALANCE, "in": False},
    {"name": "RSI30/70", "type": "rsi", "low": 30, "high": 70, "balance": START_BALANCE, "in": False},
    {"name": "RSI35/65", "type": "rsi", "low": 35, "high": 65, "balance": START_BALANCE, "in": False},
    {"name": "BOLL2.0", "type": "boll", "std": 2.0, "balance": START_BALANCE, "in": False},
    {"name": "BREAKOUT20", "type": "breakout", "lookback": 20, "balance": START_BALANCE, "in": False},
    {"name": "BREAKOUT50", "type": "breakout", "lookback": 50, "balance": START_BALANCE, "in": False},
]

for s in strategies:
    s["entry"] = 0.0
    s["amount"] = 0.0
    s["trades"] = 0


def get_data():
    url = "https://api.bitget.com/api/v2/spot/market/candles"
    params = {"symbol": SYMBOL, "granularity": GRANULARITY, "limit": "120"}
    r = requests.get(url, params=params, timeout=10)
    rows = r.json()["data"]

    df = pd.DataFrame(rows)
    df = df.iloc[:, :6]
    df.columns = ["time", "open", "high", "low", "close", "volume"]

    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)

    return df.sort_values("time").reset_index(drop=True)


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def signal(df, s):
    close = df["close"]
    price = close.iloc[-1]

    if s["type"] == "ma":
        fast = close.rolling(s["fast"]).mean()
        slow = close.rolling(s["slow"]).mean()
        if fast.iloc[-2] < slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]:
            return "BUY"
        if fast.iloc[-2] > slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]:
            return "SELL"

    if s["type"] == "ema":
        fast = close.ewm(span=s["fast"]).mean()
        slow = close.ewm(span=s["slow"]).mean()
        if fast.iloc[-2] < slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]:
            return "BUY"
        if fast.iloc[-2] > slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]:
            return "SELL"

    if s["type"] == "rsi":
        rr = rsi(close)
        if rr.iloc[-2] < s["low"] and rr.iloc[-1] >= s["low"]:
            return "BUY"
        if rr.iloc[-1] >= s["high"]:
            return "SELL"

    if s["type"] == "boll":
        ma = close.rolling(20).mean()
        sd = close.rolling(20).std()
        lower = ma - s["std"] * sd
        middle = ma
        if close.iloc[-2] < lower.iloc[-2] and close.iloc[-1] > lower.iloc[-1]:
            return "BUY"
        if close.iloc[-1] > middle.iloc[-1]:
            return "SELL"

    if s["type"] == "breakout":
        high = df["high"].iloc[-s["lookback"]:-1].max()
        vol_now = df["volume"].iloc[-1]
        vol_avg = df["volume"].rolling(s["lookback"]).mean().iloc[-1]
        if price > high and vol_now > vol_avg:
            return "BUY"
        if close.iloc[-1] < close.rolling(5).mean().iloc[-1]:
            return "SELL"

    return "HOLD"


def equity(s, price):
    if s["in"]:
        return s["amount"] * price
    return s["balance"]


print("Starting Bitget Multi Strategy Arena")

while True:
    try:
        df = get_data()
        price = df["close"].iloc[-1]

        for s in strategies:
            sig = signal(df, s)

            if not s["in"] and sig == "BUY":
                s["amount"] = s["balance"] / price
                s["entry"] = price
                s["in"] = True
                s["trades"] += 1

            elif s["in"] and sig == "SELL":
                gross_balance = s["amount"] * price
                cost = gross_balance * FEE_AND_SLIP
                s["balance"] = gross_balance - cost
                s["in"] = False
                s["amount"] = 0.0

        ranked = sorted(strategies, key=lambda x: equity(x, price), reverse=True)

        print("\n==================== ARENA ====================")
        print(f"{SYMBOL} PRICE {price:.2f}")
        print("-----------------------------------------------")

        for i, s in enumerate(ranked, 1):
            eq = equity(s, price)
            profit = eq - START_BALANCE
            pos = "IN" if s["in"] else "OUT"
            print(
                f"#{i:02d} {s['name']:12s} | BALANCE {eq:8.4f} | "
                f"P/L {profit:+.4f} | {pos} | trades {s['trades']}"
            )

        print("===============================================\n")

    except Exception as e:
        print("ERROR:", e)

    time.sleep(LOOP_SECONDS)
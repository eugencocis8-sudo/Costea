import os
import time
import requests
import pandas as pd
from dataclasses import dataclass

# ================= SETTINGS =================
SYMBOL = "BTCUSDT"
GRANULARITY = "5min"
START_BALANCE = 10.0
LOOP_SECONDS = 20
FEE = 0.0025
TOP_SHOW = 15
BASE_URL = "https://api.bitget.com"

# ================= BOT =================
@dataclass
class Bot:
    name: str
    fast: int
    slow: int
    balance: float = START_BALANCE
    in_position: bool = False
    entry: float = 0.0
    qty: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    last: str = "WAIT"

bots = []

# 50 strategii
pairs = [
(3,10),(4,12),(5,15),(5,20),(6,22),(7,25),(8,30),(9,35),(10,40),(12,50),
(13,55),(14,60),(15,65),(16,70),(17,75),(18,80),(19,85),(20,90),(21,95),(22,100),
(23,105),(24,110),(25,115),(26,120),(27,125),(28,130),(29,135),(30,140),(31,145),(32,150),
(33,155),(34,160),(35,165),(36,170),(37,175),(38,180),(39,185),(40,190),(41,195),(42,200),
(43,205),(44,210),(45,215),(46,220),(47,225),(48,230),(49,235),(50,240),(51,245),(52,250)
]

for p in pairs:
    bots.append(Bot(f"MA{p[0]}/{p[1]}", p[0], p[1]))

# ================= DATA =================
def get_data():
    url = BASE_URL + "/api/v2/spot/market/candles"
    params = {
        "symbol": SYMBOL,
        "granularity": GRANULARITY,
        "limit": "300"
    }

    r = requests.get(url, params=params, timeout=10)
    js = r.json()

    rows = js["data"]

    df = pd.DataFrame(rows)
    df = df.iloc[:, :6]
    df.columns = ["time","open","high","low","close","volume"]

    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)

    df = df.sort_values("time").reset_index(drop=True)
    return df

# ================= ENGINE =================
def process_bot(bot, df, price):

    fast = df["close"].rolling(bot.fast).mean()
    slow = df["close"].rolling(bot.slow).mean()

    if len(fast.dropna()) < 3:
        return

    buy_signal = fast.iloc[-2] < slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]
    sell_signal = fast.iloc[-2] > slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]

    if not bot.in_position:
        if buy_signal:
            bot.qty = bot.balance / price
            bot.entry = price
            bot.in_position = True
            bot.trades += 1
            bot.last = "BUY"
        else:
            bot.last = "WAIT"
        return

    # daca are pozitie
    pnl = (price - bot.entry) / bot.entry

    if pnl >= 0.006 or pnl <= -0.003 or sell_signal:
        gross = bot.qty * price
        net = gross - gross * FEE

        if net > bot.balance:
            bot.wins += 1
        else:
            bot.losses += 1

        bot.balance = net
        bot.qty = 0
        bot.in_position = False
        bot.entry = 0
        bot.last = "SELL"
    else:
        bot.last = "HOLD"

# ================= SCREEN =================
def show(price):

    ranked = sorted(bots, key=lambda x: x.balance if not x.in_position else x.qty*price, reverse=True)

    best = ranked[0]
    best_bal = best.balance if not best.in_position else best.qty*price

    print("")
    print("=============== BITGET 50 AI ARENA ===============")
    print("SYMBOL:", SYMBOL, "PRICE:", round(price,2))
    print("BEST:", best.name, "BALANCE:", round(best_bal,4))
    print("--------------------------------------------------")

    for i,b in enumerate(ranked[:TOP_SHOW],1):

        bal = b.balance if not b.in_position else b.qty*price
        pl = bal - START_BALANCE
        wr = 0

        total = b.wins + b.losses
        if total > 0:
            wr = (b.wins / total) * 100

        pos = "IN" if b.in_position else "OUT"

        print(
            f"#{i:02d} {b.name:12s} | BAL {bal:8.4f} | "
            f"P/L {pl:+7.4f} | T {b.trades:2d} | "
            f"W {wr:5.1f}% | {pos:3s} | {b.last}"
        )

    print("==================================================")

# ================= MAIN =================
print("Starting Bitget 50 AI Arena")

while True:
    try:
        df = get_data()
        price = float(df["close"].iloc[-1])

        for bot in bots:
            process_bot(bot, df, price)

        show(price)

    except Exception as e:
        print("ERROR:", e)

    time.sleep(LOOP_SECONDS)
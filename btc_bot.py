import requests
import time
import json
import os
from datetime import datetime

# =========================
# SETARI BTC
# =========================
SYMBOL = "BTCUSDT"
CHECK_EVERY_SEC = 10

START_USDT = 10_000.0
FEE_RATE = 0.001

GREEN_MIN_PCT = 0.80         # prag verde: +0.80%
GREEN_CONFIRMATIONS = 3      # confirmari consecutive
HEARTBEAT_EVERY_SEC = 60

STATE_FILE = "btc_state.json"
TRADES_LOG_FILE = "btc_trades.log"
RUN_LOG_FILE = "btc_heartbeat.log"

# =========================
def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def now():
    return datetime.now().strftime("%H:%M:%S")

def icon(p):
    if p > 0: return "🟢"
    if p < 0: return "🔴"
    return "⚪"

def log_line(path, line):
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "USDT": float(START_USDT),
            "holding": False,
            "qty": 0.0,
            "entry": 0.0,
            "green_streak": 0,
            "last_heartbeat_ts": 0.0
        }
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        s = json.load(f)
    s.setdefault("USDT", float(START_USDT))
    s.setdefault("holding", False)
    s.setdefault("qty", 0.0)
    s.setdefault("entry", 0.0)
    s.setdefault("green_streak", 0)
    s.setdefault("last_heartbeat_ts", 0.0)
    return s

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def heartbeat(state, extra=""):
    t = time.time()
    if (t - float(state.get("last_heartbeat_ts", 0.0))) >= HEARTBEAT_EVERY_SEC:
        hold = SYMBOL if state["holding"] else "USDT"
        log_line(RUN_LOG_FILE, f"{ts()} ALIVE holding={hold} {extra}".strip())
        state["last_heartbeat_ts"] = t
        save_state(state)

def fetch_symbol():
    url = "https://api.bitget.com/api/v2/spot/market/tickers"
    r = requests.get(url, timeout=10)
    j = r.json()
    if r.status_code != 200 or j.get("code") != "00000":
        raise RuntimeError(f"API error: HTTP={r.status_code} body={j}")

    for t in j["data"]:
        if t.get("symbol") == SYMBOL:
            last = float(t["lastPr"])
            chg = float(t["change24h"]) * 100.0
            bid = float(t.get("bidPr") or last)
            ask = float(t.get("askPr") or last)
            vol = float(t.get("usdtVolume") or t.get("quoteVolume") or 0.0)
            return {"last": last, "chg": chg, "bid": bid, "ask": ask, "vol": vol}

    raise RuntimeError(f"Symbol not found: {SYMBOL}")

def buy_all(state, price, reason=""):
    if state["USDT"] <= 0:
        return
    usdt_before = state["USDT"]
    fee = usdt_before * FEE_RATE
    usdt_after_fee = usdt_before - fee
    qty = usdt_after_fee / price

    state["holding"] = True
    state["qty"] = qty
    state["entry"] = price
    state["USDT"] = 0.0

    log_line(TRADES_LOG_FILE,
             f"{ts()} BUY  {SYMBOL} qty={qty:.8f} price={price:.2f} spent={usdt_after_fee:.2f} fee={fee:.2f} from={usdt_before:.2f} {reason}".strip())
    save_state(state)

def sell_all(state, price, reason=""):
    if (not state["holding"]) or state["qty"] <= 0:
        return
    gross = state["qty"] * price
    fee = gross * FEE_RATE
    net = gross - fee

    log_line(TRADES_LOG_FILE,
             f"{ts()} SELL {SYMBOL} qty={state['qty']:.8f} price={price:.2f} gross={gross:.2f} fee={fee:.2f} net={net:.2f} {reason}".strip())

    state["USDT"] = net
    state["holding"] = False
    state["qty"] = 0.0
    state["entry"] = 0.0
    state["green_streak"] = 0
    save_state(state)

# =========================
# RUN
# =========================
state = load_state()
print("=== BTC Semafor Bot (change24h) ===")
print(f"BUY: change24h >= +{GREEN_MIN_PCT:.2f}% confirm {GREEN_CONFIRMATIONS}x | SELL: change24h < 0")
print(f"Files: {STATE_FILE}, {TRADES_LOG_FILE}, {RUN_LOG_FILE}")
print(f"Loaded: USDT={state['USDT']:.2f} holding={state['holding']} qty={state['qty']:.8f}\n")

while True:
    try:
        m = fetch_symbol()
        chg = m["chg"]

        # streak confirmare verde
        if chg >= GREEN_MIN_PCT:
            state["green_streak"] += 1
        else:
            state["green_streak"] = 0
        save_state(state)

        print(f"{now()} {SYMBOL} last={m['last']:.2f} 24h={chg:+.2f}% {icon(chg)} streak={state['green_streak']}/{GREEN_CONFIRMATIONS}")

        # SELL instant daca rosu
        if state["holding"] and chg < 0:
            sell_all(state, m["bid"], reason="(red -> sell instant)")
            print(f"   ⚠️ SELL {SYMBOL} @ bid {m['bid']:.2f}")

        # BUY daca avem confirmari
        if (not state["holding"]) and state["green_streak"] >= GREEN_CONFIRMATIONS:
            buy_all(state, m["ask"], reason=f"(green confirmed {state['green_streak']}/{GREEN_CONFIRMATIONS})")
            print(f"   ✅ BUY {SYMBOL} @ ask {m['ask']:.2f}")

        # status PnL (evaluare la bid)
        if state["holding"]:
            total = state["qty"] * m["bid"]
        else:
            total = state["USDT"]
        pnl = total - START_USDT
        pnl_pct = (pnl / START_USDT) * 100.0
        print(f"   Holding={'BTC' if state['holding'] else 'USDT'} Total≈{total:.2f} PnL≈{pnl:+.2f} ({pnl_pct:+.2f}%)")

        heartbeat(state, extra=f"(chg={chg:+.2f}%)")
        time.sleep(CHECK_EVERY_SEC)

    except Exception as e:
        log_line(RUN_LOG_FILE, f"{ts()} ERROR {repr(e)}")
        print(f"{now()} Eroare: {e}")
        time.sleep(5)

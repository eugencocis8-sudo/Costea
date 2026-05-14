import requests
import time
import json
import os
from datetime import datetime

# =========================
# SETARI
# =========================
SYMBOL = "BTCUSDT"
CHECK_EVERY_SEC = 10

START_USDT = 10000.0
FEE_RATE = 0.001

STATE_FILE = "btc_hill_state.json"
TRADES_LOG_FILE = "btc_hill_trades.log"
RUN_LOG_FILE = "btc_hill_run.log"

HEARTBEAT_EVERY_SEC = 60

# =========================
# PARAMETRI STRATEGIE
# =========================
PRICE_WINDOW = 80              # cate preturi pastram
SHORT_MA_LEN = 12             # trend scurt
LONG_MA_LEN = 48              # trend general
SLOPE_LOOKBACK = 8            # pentru inclinarea mediei lungi

RECENT_LOW_WINDOW = 24        # cauta minimul recent aici
MIN_DROP_FROM_RECENT_HIGH_PCT = 0.20   # trebuie sa fi existat o mica scadere
MIN_REBOUND_FROM_LOW_PCT = 0.18         # si apoi o revenire din minim

TRAILING_ARM_PCT = 1.00
TRAILING_GIVEBACK_PCT = 0.35
STOP_LOSS_PCT = -1.20

COOLDOWN_AFTER_SELL_SEC = 300

# =========================
# HELPERS
# =========================
def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def now():
    return datetime.now().strftime("%H:%M:%S")

def log_line(path, line):
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "USDT": START_USDT,
            "holding": False,
            "qty": 0.0,
            "entry": 0.0,
            "last_heartbeat_ts": 0.0,
            "prices": [],
            "peak_pnl_pct": 0.0,
            "cooldown_until_ts": 0.0
        }

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        s = json.load(f)

    s.setdefault("USDT", START_USDT)
    s.setdefault("holding", False)
    s.setdefault("qty", 0.0)
    s.setdefault("entry", 0.0)
    s.setdefault("last_heartbeat_ts", 0.0)
    s.setdefault("prices", [])
    s.setdefault("peak_pnl_pct", 0.0)
    s.setdefault("cooldown_until_ts", 0.0)
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
            bid = float(t.get("bidPr") or last)
            ask = float(t.get("askPr") or last)
            chg = float(t["change24h"]) * 100.0
            return {"last": last, "bid": bid, "ask": ask, "chg": chg}

    raise RuntimeError(f"Symbol not found: {SYMBOL}")

def avg(values):
    if not values:
        return 0.0
    return sum(values) / len(values)

def pct_change(a, b):
    if a == 0:
        return 0.0
    return ((b - a) / a) * 100.0

def is_in_cooldown(state):
    return time.time() < float(state.get("cooldown_until_ts", 0.0))

def cooldown_left_sec(state):
    return max(0, int(float(state.get("cooldown_until_ts", 0.0)) - time.time()))

def update_prices(state, bid):
    prices = state.get("prices", [])
    prices.append(float(bid))
    if len(prices) > PRICE_WINDOW:
        prices = prices[-PRICE_WINDOW:]
    state["prices"] = prices

def analyze_market(prices):
    result = {
        "ready": False,
        "current": prices[-1] if prices else 0.0,
        "short_ma": 0.0,
        "long_ma": 0.0,
        "long_ma_prev": 0.0,
        "long_slope_pct": 0.0,
        "trend": "flat",
        "recent_low": 0.0,
        "recent_high": 0.0,
        "drop_from_recent_high_pct": 0.0,
        "rebound_from_low_pct": 0.0
    }

    need = max(LONG_MA_LEN + SLOPE_LOOKBACK, RECENT_LOW_WINDOW)
    if len(prices) < need:
        return result

    current = prices[-1]
    short_slice = prices[-SHORT_MA_LEN:]
    long_slice = prices[-LONG_MA_LEN:]

    short_ma = avg(short_slice)
    long_ma = avg(long_slice)

    prev_long_slice = prices[-(LONG_MA_LEN + SLOPE_LOOKBACK):-SLOPE_LOOKBACK]
    long_ma_prev = avg(prev_long_slice)
    long_slope_pct = pct_change(long_ma_prev, long_ma)

    recent_slice = prices[-RECENT_LOW_WINDOW:]
    recent_low = min(recent_slice)
    recent_high = max(recent_slice)

    drop_from_recent_high_pct = pct_change(recent_high, current)
    rebound_from_low_pct = pct_change(recent_low, current)

    trend = "flat"
    if short_ma > long_ma and long_slope_pct > 0:
        trend = "up"
    elif short_ma < long_ma and long_slope_pct < 0:
        trend = "down"

    result.update({
        "ready": True,
        "current": current,
        "short_ma": short_ma,
        "long_ma": long_ma,
        "long_ma_prev": long_ma_prev,
        "long_slope_pct": long_slope_pct,
        "trend": trend,
        "recent_low": recent_low,
        "recent_high": recent_high,
        "drop_from_recent_high_pct": pct_change(recent_high, current),
        "rebound_from_low_pct": rebound_from_low_pct
    })
    return result

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
    state["peak_pnl_pct"] = ((qty * price - START_USDT) / START_USDT) * 100.0

    log_line(
        TRADES_LOG_FILE,
        f"{ts()} BUY {SYMBOL} qty={qty:.8f} price={price:.2f} spent={usdt_after_fee:.2f} fee={fee:.2f} reason={reason}"
    )
    save_state(state)

def sell_all(state, price, reason=""):
    if (not state["holding"]) or state["qty"] <= 0:
        return

    gross = state["qty"] * price
    fee = gross * FEE_RATE
    net = gross - fee
    pnl = net - START_USDT
    pnl_pct = (pnl / START_USDT) * 100.0

    log_line(
        TRADES_LOG_FILE,
        f"{ts()} SELL {SYMBOL} qty={state['qty']:.8f} price={price:.2f} gross={gross:.2f} fee={fee:.2f} net={net:.2f} pnl={pnl:+.2f} ({pnl_pct:+.2f}%) reason={reason}"
    )

    state["USDT"] = net
    state["holding"] = False
    state["qty"] = 0.0
    state["entry"] = 0.0
    state["peak_pnl_pct"] = 0.0
    state["cooldown_until_ts"] = time.time() + COOLDOWN_AFTER_SELL_SEC
    save_state(state)

def current_total_and_pnl(state, bid_price):
    if state["holding"]:
        total = state["qty"] * bid_price
    else:
        total = state["USDT"]
    pnl = total - START_USDT
    pnl_pct = (pnl / START_USDT) * 100.0
    return total, pnl, pnl_pct

def should_buy(state, market):
    if is_in_cooldown(state):
        return False, f"cooldown {cooldown_left_sec(state)}s"

    if not market["ready"]:
        return False, "not enough data"

    if market["trend"] != "up":
        return False, "general trend not up"

    if market["current"] < market["short_ma"]:
        return False, "price below short MA"

    # vrem sa fi existat o mica scadere inainte
    if market["drop_from_recent_high_pct"] > -MIN_DROP_FROM_RECENT_HIGH_PCT:
        return False, "no real dip before rebound"

    # vrem revenire din minim
    if market["rebound_from_low_pct"] < MIN_REBOUND_FROM_LOW_PCT:
        return False, "rebound too weak"

    return True, (
        f"trend=up slope={market['long_slope_pct']:+.3f}% "
        f"rebound={market['rebound_from_low_pct']:+.3f}% "
        f"drop={market['drop_from_recent_high_pct']:+.3f}%"
    )

def should_sell(state, market, pnl_pct):
    if not market["ready"]:
        return False, ""

    if pnl_pct > state["peak_pnl_pct"]:
        state["peak_pnl_pct"] = pnl_pct

    giveback = state["peak_pnl_pct"] - pnl_pct

    if pnl_pct <= STOP_LOSS_PCT:
        return True, f"stop loss {pnl_pct:+.2f}%"

    if state["peak_pnl_pct"] >= TRAILING_ARM_PCT and giveback >= TRAILING_GIVEBACK_PCT:
        return True, f"trailing stop peak={state['peak_pnl_pct']:+.2f}% now={pnl_pct:+.2f}%"

    if market["trend"] == "down":
        return True, "general trend turned down"

    if market["current"] < market["short_ma"] and market["short_ma"] < market["long_ma"]:
        return True, "price under short MA and short MA under long MA"

    if market["long_slope_pct"] < 0:
        return True, f"long slope negative {market['long_slope_pct']:+.3f}%"

    return False, ""

# =========================
# RUN
# =========================
state = load_state()

print("=== BTC Hill Trend Bot ===")
print("BUY: trend general up + panta pozitiva + dip + rebound")
print("SELL: trend down / slope down / trailing / stop loss")
print()

while True:
    try:
        m = fetch_symbol()
        bid = m["bid"]
        ask = m["ask"]

        update_prices(state, bid)
        market = analyze_market(state["prices"])

        print(
            f"{now()} {SYMBOL} last={m['last']:.2f} 24h={m['chg']:+.2f}% "
            f"trend={market['trend']} slope={market['long_slope_pct']:+.3f}% "
            f"rebound={market['rebound_from_low_pct']:+.3f}%"
        )

        if not state["holding"]:
            ok, reason = should_buy(state, market)
            if ok:
                buy_all(state, ask, reason=reason)
                print(f"   ✅ BUY @ {ask:.2f} | {reason}")
            else:
                print(f"   waiting buy: {reason}")

        if state["holding"]:
            total, pnl, pnl_pct = current_total_and_pnl(state, bid)
            ok, reason = should_sell(state, market, pnl_pct)
            if ok:
                sell_all(state, bid, reason=reason)
                print(f"   ⚠️ SELL @ {bid:.2f} | {reason}")

        total, pnl, pnl_pct = current_total_and_pnl(state, bid)
        if state["holding"]:
            giveback = state["peak_pnl_pct"] - pnl_pct
            print(
                f"   Holding=BTC Total≈{total:.2f} PnL≈{pnl:+.2f} ({pnl_pct:+.2f}%) "
                f"peak≈{state['peak_pnl_pct']:+.2f}% giveback≈{giveback:.2f}%"
            )
        else:
            print(
                f"   Holding=USDT Total≈{total:.2f} PnL≈{pnl:+.2f} ({pnl_pct:+.2f}%) "
                f"cooldown={cooldown_left_sec(state)}s"
            )

        save_state(state)
        heartbeat(state, extra=f"(trend={market['trend']} slope={market['long_slope_pct']:+.3f}%)")
        time.sleep(CHECK_EVERY_SEC)

    except Exception as e:
        log_line(RUN_LOG_FILE, f"{ts()} ERROR {repr(e)}")
        print(f"{now()} Eroare: {e}")
        time.sleep(5)
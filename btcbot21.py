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

# Filtru general
GREEN_MIN_PCT = 0.80

# Heartbeat
HEARTBEAT_EVERY_SEC = 60

# Momentum local
MOMENTUM_WINDOW = 12                 # cate tick-uri tinem in memorie
MIN_UP_TICKS_FOR_BUY = 8             # cate miscari in sus din fereastra pentru BUY
MIN_DOWN_TICKS_FOR_SELL = 8          # cate miscari in jos din fereastra pentru SELL
MIN_WINDOW_MOVE_PCT_BUY = 0.12       # crestere minima in fereastra pt BUY
MIN_WINDOW_MOVE_PCT_SELL = -0.10     # scadere minima in fereastra pt SELL
USE_SHORT_MA_FILTER = True           # pretul trebuie sa fie peste media scurta la BUY

# Management pozitie
STOP_LOSS_PCT = -1.20
TRAILING_ARM_PCT = 1.00
TRAILING_GIVEBACK_PCT = 0.35

# Filtru slabire trend general
WEAK_CHG_EXIT_PCT = 0.25

STATE_FILE = "btc_state_v2.json"
TRADES_LOG_FILE = "btc_trades_v2.log"
RUN_LOG_FILE = "btc_heartbeat_v2.log"

# =========================
# HELPERS
# =========================
def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def now():
    return datetime.now().strftime("%H:%M:%S")

def icon(p):
    if p > 0:
        return "🟢"
    if p < 0:
        return "🔴"
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
            "last_heartbeat_ts": 0.0,

            "peak_total": float(START_USDT),
            "peak_price": 0.0,
            "peak_pnl_pct": 0.0,

            "prices": [],
            "last_bid": 0.0
        }

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        s = json.load(f)

    s.setdefault("USDT", float(START_USDT))
    s.setdefault("holding", False)
    s.setdefault("qty", 0.0)
    s.setdefault("entry", 0.0)
    s.setdefault("last_heartbeat_ts", 0.0)

    s.setdefault("peak_total", float(START_USDT))
    s.setdefault("peak_price", 0.0)
    s.setdefault("peak_pnl_pct", 0.0)

    s.setdefault("prices", [])
    s.setdefault("last_bid", 0.0)

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
            return {
                "last": last,
                "chg": chg,
                "bid": bid,
                "ask": ask,
                "vol": vol
            }

    raise RuntimeError(f"Symbol not found: {SYMBOL}")

def reset_position_tracking(state):
    state["peak_total"] = state["USDT"]
    state["peak_price"] = 0.0
    state["peak_pnl_pct"] = 0.0

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

    state["peak_total"] = qty * price
    state["peak_price"] = price
    state["peak_pnl_pct"] = ((state["peak_total"] - START_USDT) / START_USDT) * 100.0

    log_line(
        TRADES_LOG_FILE,
        f"{ts()} BUY  {SYMBOL} qty={qty:.8f} price={price:.2f} spent={usdt_after_fee:.2f} fee={fee:.2f} from={usdt_before:.2f} {reason}".strip()
    )
    save_state(state)

def sell_all(state, price, reason=""):
    if (not state["holding"]) or state["qty"] <= 0:
        return

    gross = state["qty"] * price
    fee = gross * FEE_RATE
    net = gross - fee

    trade_pnl = net - START_USDT
    trade_pnl_pct = (trade_pnl / START_USDT) * 100.0

    log_line(
        TRADES_LOG_FILE,
        f"{ts()} SELL {SYMBOL} qty={state['qty']:.8f} price={price:.2f} gross={gross:.2f} fee={fee:.2f} net={net:.2f} pnl={trade_pnl:+.2f} ({trade_pnl_pct:+.2f}%) {reason}".strip()
    )

    state["USDT"] = net
    state["holding"] = False
    state["qty"] = 0.0
    state["entry"] = 0.0
    reset_position_tracking(state)
    save_state(state)

def current_total_and_pnl(state, bid_price):
    if state["holding"]:
        total = state["qty"] * bid_price
    else:
        total = state["USDT"]

    pnl = total - START_USDT
    pnl_pct = (pnl / START_USDT) * 100.0
    return total, pnl, pnl_pct

def update_prices(state, bid):
    prices = state.get("prices", [])
    prices.append(float(bid))
    if len(prices) > MOMENTUM_WINDOW:
        prices = prices[-MOMENTUM_WINDOW:]
    state["prices"] = prices

def analyze_momentum(prices):
    """
    Returneaza:
      - up_moves
      - down_moves
      - move_pct (primul -> ultimul)
      - avg_price
      - current_price
    """
    if len(prices) < 2:
        return {
            "ready": False,
            "up_moves": 0,
            "down_moves": 0,
            "move_pct": 0.0,
            "avg_price": prices[-1] if prices else 0.0,
            "current_price": prices[-1] if prices else 0.0
        }

    up_moves = 0
    down_moves = 0

    for i in range(1, len(prices)):
        if prices[i] > prices[i - 1]:
            up_moves += 1
        elif prices[i] < prices[i - 1]:
            down_moves += 1

    first_price = prices[0]
    last_price = prices[-1]
    move_pct = ((last_price - first_price) / first_price) * 100.0 if first_price else 0.0
    avg_price = sum(prices) / len(prices)

    return {
        "ready": len(prices) >= MOMENTUM_WINDOW,
        "up_moves": up_moves,
        "down_moves": down_moves,
        "move_pct": move_pct,
        "avg_price": avg_price,
        "current_price": last_price
    }

def should_buy(chg, mom):
    if chg < GREEN_MIN_PCT:
        return False, "change24h too low"

    if not mom["ready"]:
        return False, "not enough momentum data"

    if mom["up_moves"] < MIN_UP_TICKS_FOR_BUY:
        return False, "not enough up moves"

    if mom["move_pct"] < MIN_WINDOW_MOVE_PCT_BUY:
        return False, "window move too small"

    if USE_SHORT_MA_FILTER and mom["current_price"] < mom["avg_price"]:
        return False, "price below short average"

    return True, (
        f"buy_momentum up={mom['up_moves']} down={mom['down_moves']} "
        f"move={mom['move_pct']:+.3f}% avg={mom['avg_price']:.2f}"
    )

def should_sell(chg, mom, pnl_pct, peak_pnl_pct):
    giveback = peak_pnl_pct - pnl_pct

    # 1. stop loss
    if pnl_pct <= STOP_LOSS_PCT:
        return True, f"stop loss {pnl_pct:+.2f}% <= {STOP_LOSS_PCT:+.2f}%"

    # 2. trailing profit
    if peak_pnl_pct >= TRAILING_ARM_PCT and giveback >= TRAILING_GIVEBACK_PCT:
        return True, (
            f"trailing stop peak={peak_pnl_pct:+.2f}% now={pnl_pct:+.2f}% "
            f"giveback={giveback:.2f}%"
        )

    # 3. momentum negativ local
    if mom["ready"]:
        if (
            mom["down_moves"] >= MIN_DOWN_TICKS_FOR_SELL and
            mom["move_pct"] <= MIN_WINDOW_MOVE_PCT_SELL
        ):
            return True, (
                f"negative momentum down={mom['down_moves']} "
                f"move={mom['move_pct']:+.3f}%"
            )

    # 4. trend general slabit + momentum negativ
    if mom["ready"]:
        if chg < WEAK_CHG_EXIT_PCT and mom["down_moves"] > mom["up_moves"]:
            return True, (
                f"weak trend chg={chg:+.2f}% down={mom['down_moves']} "
                f"up={mom['up_moves']}"
            )

    # 5. 24h rosu
    if chg < 0:
        return True, f"24h red {chg:+.2f}%"

    return False, ""

# =========================
# RUN
# =========================
state = load_state()

print("=== BTC Momentum Bot v2 ===")
print("BUY: change24h filter + momentum pozitiv local")
print("SELL: trailing stop + stop loss + momentum negativ")
print(f"Files: {STATE_FILE}, {TRADES_LOG_FILE}, {RUN_LOG_FILE}")
print(f"Loaded: USDT={state['USDT']:.2f} holding={state['holding']} qty={state['qty']:.8f}\n")

while True:
    try:
        m = fetch_symbol()
        chg = m["chg"]
        bid = m["bid"]
        ask = m["ask"]

        update_prices(state, bid)
        mom = analyze_momentum(state["prices"])

        print(
            f"{now()} {SYMBOL} last={m['last']:.2f} 24h={chg:+.2f}% {icon(chg)} "
            f"up={mom['up_moves']} down={mom['down_moves']} move={mom['move_pct']:+.3f}%"
        )

        # BUY
        if not state["holding"]:
            buy_ok, buy_reason = should_buy(chg, mom)
            if buy_ok:
                buy_all(state, ask, reason=f"({buy_reason})")
                print(f"   ✅ BUY {SYMBOL} @ ask {ask:.2f}")
            else:
                print(f"   Waiting BUY: {buy_reason}")

        # Daca tinem BTC, calculeaza PnL si peak
        if state["holding"]:
            total, pnl, pnl_pct = current_total_and_pnl(state, bid)

            if total > float(state.get("peak_total", 0.0)):
                state["peak_total"] = total
            if bid > float(state.get("peak_price", 0.0)):
                state["peak_price"] = bid
            if pnl_pct > float(state.get("peak_pnl_pct", -9999.0)):
                state["peak_pnl_pct"] = pnl_pct

            peak_pnl_pct = float(state["peak_pnl_pct"])

            sell_ok, sell_reason = should_sell(chg, mom, pnl_pct, peak_pnl_pct)
            if sell_ok:
                sell_all(state, bid, reason=f"({sell_reason})")
                print(f"   ⚠️ SELL {SYMBOL} @ bid {bid:.2f} | {sell_reason}")

        # status final
        total, pnl, pnl_pct = current_total_and_pnl(state, bid)

        if state["holding"]:
            peak_pnl_pct = float(state.get("peak_pnl_pct", 0.0))
            giveback = peak_pnl_pct - pnl_pct
            print(
                f"   Holding=BTC Total≈{total:.2f} PnL≈{pnl:+.2f} ({pnl_pct:+.2f}%) "
                f"peak≈{peak_pnl_pct:+.2f}% giveback≈{giveback:.2f}%"
            )
        else:
            print(f"   Holding=USDT Total≈{total:.2f} PnL≈{pnl:+.2f} ({pnl_pct:+.2f}%)")

        save_state(state)
        heartbeat(state, extra=f"(chg={chg:+.2f}% move={mom['move_pct']:+.3f}%)")
        time.sleep(CHECK_EVERY_SEC)

    except Exception as e:
        log_line(RUN_LOG_FILE, f"{ts()} ERROR {repr(e)}")
        print(f"{now()} Eroare: {e}")
        time.sleep(5)
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

GREEN_MIN_PCT = 0.80
GREEN_CONFIRMATIONS = 3
HEARTBEAT_EVERY_SEC = 60

# =========================
# MANAGEMENT POZITIE
# =========================
STOP_LOSS_PCT = -1.20             # vinde daca trade-ul ajunge sub -1.20%
TRAILING_ARM_PCT = 1.00           # porneste trailing dupa ce trade-ul trece de +1.00%
TRAILING_GIVEBACK_PCT = 0.35      # vinde daca profitul scade cu 0.35% de la maxim
WEAK_CHG_EXIT_PCT = 0.45          # trend slabit daca 24h% scade sub acest prag
WEAK_DOWN_TICKS_TO_SELL = 3       # cate scaderi consecutive de pret pentru exit pe slabire

STATE_FILE = "btc_state.json"
TRADES_LOG_FILE = "btc_trades.log"
RUN_LOG_FILE = "btc_heartbeat.log"

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
            "green_streak": 0,
            "last_heartbeat_ts": 0.0,

            # noi
            "peak_total": float(START_USDT),
            "peak_price": 0.0,
            "peak_pnl_pct": 0.0,
            "last_bid": 0.0,
            "down_ticks": 0
        }

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        s = json.load(f)

    s.setdefault("USDT", float(START_USDT))
    s.setdefault("holding", False)
    s.setdefault("qty", 0.0)
    s.setdefault("entry", 0.0)
    s.setdefault("green_streak", 0)
    s.setdefault("last_heartbeat_ts", 0.0)

    s.setdefault("peak_total", float(START_USDT))
    s.setdefault("peak_price", 0.0)
    s.setdefault("peak_pnl_pct", 0.0)
    s.setdefault("last_bid", 0.0)
    s.setdefault("down_ticks", 0)

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
    state["last_bid"] = 0.0
    state["down_ticks"] = 0

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

    # reset / init tracking
    state["peak_total"] = qty * price
    state["peak_price"] = price
    state["peak_pnl_pct"] = ((state["peak_total"] - START_USDT) / START_USDT) * 100.0
    state["last_bid"] = price
    state["down_ticks"] = 0

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
    state["green_streak"] = 0
    reset_position_tracking(state)
    save_state(state)

def update_down_ticks(state, current_bid):
    last_bid = float(state.get("last_bid", 0.0))
    if last_bid > 0:
        if current_bid < last_bid:
            state["down_ticks"] += 1
        elif current_bid > last_bid:
            state["down_ticks"] = 0
    state["last_bid"] = current_bid

def current_total_and_pnl(state, bid_price):
    if state["holding"]:
        total = state["qty"] * bid_price
    else:
        total = state["USDT"]

    pnl = total - START_USDT
    pnl_pct = (pnl / START_USDT) * 100.0
    return total, pnl, pnl_pct

# =========================
# RUN
# =========================
state = load_state()

print("=== BTC Semafor Bot (improved) ===")
print(f"BUY: change24h >= +{GREEN_MIN_PCT:.2f}% confirm {GREEN_CONFIRMATIONS}x")
print(f"SELL: trailing profit, weak trend, stop loss")
print(f"Files: {STATE_FILE}, {TRADES_LOG_FILE}, {RUN_LOG_FILE}")
print(f"Loaded: USDT={state['USDT']:.2f} holding={state['holding']} qty={state['qty']:.8f}\n")

while True:
    try:
        m = fetch_symbol()
        chg = m["chg"]
        bid = m["bid"]
        ask = m["ask"]

        # streak confirmare verde
        if chg >= GREEN_MIN_PCT:
            state["green_streak"] += 1
        else:
            state["green_streak"] = 0

        print(f"{now()} {SYMBOL} last={m['last']:.2f} 24h={chg:+.2f}% {icon(chg)} streak={state['green_streak']}/{GREEN_CONFIRMATIONS}")

        # =========================
        # DACA TINEM BTC -> urmarire profit / slabire trend
        # =========================
        if state["holding"]:
            update_down_ticks(state, bid)

            total, pnl, pnl_pct = current_total_and_pnl(state, bid)

            # actualizeaza maximele
            if total > float(state.get("peak_total", 0.0)):
                state["peak_total"] = total
            if bid > float(state.get("peak_price", 0.0)):
                state["peak_price"] = bid
            if pnl_pct > float(state.get("peak_pnl_pct", -9999.0)):
                state["peak_pnl_pct"] = pnl_pct

            peak_pnl_pct = float(state["peak_pnl_pct"])
            giveback = peak_pnl_pct - pnl_pct

            # 1) STOP LOSS
            if pnl_pct <= STOP_LOSS_PCT:
                sell_all(state, bid, reason=f"(stop loss {pnl_pct:+.2f}% <= {STOP_LOSS_PCT:+.2f}%)")
                print(f"   ⛔ SELL {SYMBOL} @ bid {bid:.2f} | stop loss")
                total, pnl, pnl_pct = current_total_and_pnl(state, bid)

            # 2) TRAILING PROFIT
            elif peak_pnl_pct >= TRAILING_ARM_PCT and giveback >= TRAILING_GIVEBACK_PCT:
                sell_all(
                    state,
                    bid,
                    reason=f"(trailing stop: peak={peak_pnl_pct:+.2f}% now={pnl_pct:+.2f}% giveback={giveback:.2f}%)"
                )
                print(f"   💰 SELL {SYMBOL} @ bid {bid:.2f} | trailing stop")
                total, pnl, pnl_pct = current_total_and_pnl(state, bid)

            # 3) EXIT PE SLABIRE DE TREND
            elif chg < WEAK_CHG_EXIT_PCT and state["down_ticks"] >= WEAK_DOWN_TICKS_TO_SELL:
                sell_all(
                    state,
                    bid,
                    reason=f"(weak trend: 24h={chg:+.2f}% down_ticks={state['down_ticks']})"
                )
                print(f"   ⚠️ SELL {SYMBOL} @ bid {bid:.2f} | trend weakening")
                total, pnl, pnl_pct = current_total_and_pnl(state, bid)

            # 4) SELL instant daca devine rosu pe 24h
            elif chg < 0:
                sell_all(state, bid, reason="(red -> sell instant)")
                print(f"   🔴 SELL {SYMBOL} @ bid {bid:.2f} | 24h red")
                total, pnl, pnl_pct = current_total_and_pnl(state, bid)

        # =========================
        # DACA NU TINEM BTC -> putem cumpara
        # =========================
        if (not state["holding"]) and state["green_streak"] >= GREEN_CONFIRMATIONS:
            buy_all(state, ask, reason=f"(green confirmed {state['green_streak']}/{GREEN_CONFIRMATIONS})")
            print(f"   ✅ BUY {SYMBOL} @ ask {ask:.2f}")

        # status final
        if state["holding"]:
            total, pnl, pnl_pct = current_total_and_pnl(state, bid)
            peak_pnl_pct = float(state.get("peak_pnl_pct", 0.0))
            giveback = peak_pnl_pct - pnl_pct
            print(
                f"   Holding=BTC Total≈{total:.2f} PnL≈{pnl:+.2f} ({pnl_pct:+.2f}%) "
                f"peak≈{peak_pnl_pct:+.2f}% giveback≈{giveback:.2f}% down_ticks={state['down_ticks']}"
            )
        else:
            total, pnl, pnl_pct = current_total_and_pnl(state, bid)
            print(f"   Holding=USDT Total≈{total:.2f} PnL≈{pnl:+.2f} ({pnl_pct:+.2f}%)")

        save_state(state)
        heartbeat(state, extra=f"(chg={chg:+.2f}%)")
        time.sleep(CHECK_EVERY_SEC)

    except Exception as e:
        log_line(RUN_LOG_FILE, f"{ts()} ERROR {repr(e)}")
        print(f"{now()} Eroare: {e}")
        time.sleep(5)
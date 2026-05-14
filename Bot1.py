import requests
import time
import json
import os
from datetime import datetime

# =========================
# SETARI
# =========================
FAVORITES = ["BTCUSDT", "ETHUSDT", "BGBUSDT", "XRPUSDT", "UNIUSDT", "DOGEUSDT"]

CHECK_EVERY_SEC = 10
COOLDOWN_SEC = 60

START_USDT = 10_000.0
FEE_RATE = 0.001  # 0.1% (simulare fee)

GREEN_MIN_PCT = 0.80          # ✅ prag verde: +0.80%
CONFIRM_BUY = 3              # ✅ confirmari consecutive
PRIORITY_SYMBOL = "BTCUSDT"  # prioritate pe BTC daca e eligibil

STATE_FILE = "paper_state.json"
TRADES_LOG = "paper_trades.log"
HEARTBEAT_LOG = "run_heartbeat.log"
HEARTBEAT_EVERY_SEC = 60

# =========================
# HELPERS
# =========================
def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def now():
    return datetime.now().strftime("%H:%M:%S")

def icon(pct: float) -> str:
    if pct > 0: return "🟢"
    if pct < 0: return "🔴"
    return "⚪"

def log_line(path: str, line: str):
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "USDT": float(START_USDT),
            "holding": None,
            "qty": 0.0,
            "entry": 0.0,
            "last_trade_ts": 0.0,
            "last_heartbeat_ts": 0.0,
            "ok_counts": {}  # sym -> streak
        }
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        s = json.load(f)

    s.setdefault("USDT", float(START_USDT))
    s.setdefault("holding", None)
    s.setdefault("qty", 0.0)
    s.setdefault("entry", 0.0)
    s.setdefault("last_trade_ts", 0.0)
    s.setdefault("last_heartbeat_ts", 0.0)
    s.setdefault("ok_counts", {})
    return s

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def heartbeat(state, extra=""):
    t = time.time()
    if (t - float(state.get("last_heartbeat_ts", 0.0))) >= HEARTBEAT_EVERY_SEC:
        hold = state["holding"] or "USDT"
        log_line(HEARTBEAT_LOG, f"{ts()} ALIVE holding={hold} {extra}".strip())
        state["last_heartbeat_ts"] = t
        save_state(state)

def can_trade(state):
    return (time.time() - float(state.get("last_trade_ts", 0.0))) >= COOLDOWN_SEC

def mark_trade(state):
    state["last_trade_ts"] = time.time()

# =========================
# API
# =========================
def fetch_spot_tickers():
    url = "https://api.bitget.com/api/v2/spot/market/tickers"
    r = requests.get(url, timeout=10)
    j = r.json()
    if r.status_code != 200 or j.get("code") != "00000":
        raise RuntimeError(f"API error: HTTP={r.status_code} body={j}")
    return j["data"]

def parse_float(x, default=0.0):
    try:
        return float(x)
    except:
        return default

def build_market(data):
    market = {}
    for t in data:
        sym = t.get("symbol")
        if sym not in FAVORITES:
            continue
        last = parse_float(t.get("lastPr"))
        chg = parse_float(t.get("change24h")) * 100.0  # procent
        bid = parse_float(t.get("bidPr"), last)
        ask = parse_float(t.get("askPr"), last)
        vol = parse_float(t.get("usdtVolume")) or parse_float(t.get("quoteVolume")) or 0.0
        market[sym] = {"last": last, "chg": chg, "bid": bid, "ask": ask, "vol": vol}
    return market

# =========================
# PAPER TRADING
# =========================
def total_value(state, market):
    if state["holding"] and state["holding"] in market and state["qty"] > 0:
        return state["USDT"] + state["qty"] * market[state["holding"]]["bid"]
    return state["USDT"]

def buy_all(state, sym, market, reason=""):
    if state["USDT"] <= 0:
        return
    price = market[sym]["ask"]
    usdt_before = state["USDT"]
    fee = usdt_before * FEE_RATE
    usdt_after_fee = usdt_before - fee
    qty = usdt_after_fee / price

    state["holding"] = sym
    state["qty"] = qty
    state["entry"] = price
    state["USDT"] = 0.0

    log_line(TRADES_LOG,
             f"{ts()} BUY  {sym} qty={qty:.6f} price={price:.6f} spent={usdt_after_fee:.2f} fee={fee:.2f} from={usdt_before:.2f} {reason}".strip())
    mark_trade(state)
    save_state(state)

def sell_all(state, market, reason=""):
    sym = state["holding"]
    if not sym or sym not in market or state["qty"] <= 0:
        return
    price = market[sym]["bid"]
    gross = state["qty"] * price
    fee = gross * FEE_RATE
    net = gross - fee

    log_line(TRADES_LOG,
             f"{ts()} SELL {sym} qty={state['qty']:.6f} price={price:.6f} gross={gross:.2f} fee={fee:.2f} net={net:.2f} {reason}".strip())

    state["USDT"] = net
    state["holding"] = None
    state["qty"] = 0.0
    state["entry"] = 0.0
    mark_trade(state)
    save_state(state)

# =========================
# LOGICA SEMAFOR (UPDATED)
# =========================
def update_ok_counts(state, market):
    """
    ✅ ok_count este plafonat la CONFIRM_BUY (nu mai ajunge 1919/3).
    """
    ok = state.get("ok_counts", {})
    for sym, v in market.items():
        if v["chg"] >= GREEN_MIN_PCT:
            ok[sym] = min(int(ok.get(sym, 0)) + 1, CONFIRM_BUY)
        else:
            ok[sym] = 0
    state["ok_counts"] = ok
    save_state(state)

def pick_buy_candidate(state, market):
    """
    Prioritate BTC daca are ok=3/3.
    Altfel alege cea cu volum mare dintre cele confirmate.
    """
    ok = state["ok_counts"]

    # prioritate BTC
    if PRIORITY_SYMBOL in market and ok.get(PRIORITY_SYMBOL, 0) >= CONFIRM_BUY:
        return PRIORITY_SYMBOL

    # altfel, din confirmate alege volum maxim
    candidates = [sym for sym in market.keys() if ok.get(sym, 0) >= CONFIRM_BUY]
    if not candidates:
        return None
    candidates.sort(key=lambda s: market[s]["vol"], reverse=True)
    return candidates[0]

# =========================
# RUN
# =========================
state = load_state()

print("=== BOT PAPER (Semafor UPDATED) ===")
print(f"Verde >= +{GREEN_MIN_PCT:.2f}% | Confirmari BUY: {CONFIRM_BUY} | Check: {CHECK_EVERY_SEC}s | Cooldown: {COOLDOWN_SEC}s")
print(f"SELL: daca holding scade sub +{GREEN_MIN_PCT:.2f}% (sau rosu)")
print(f"State: {STATE_FILE} | Trades: {TRADES_LOG} | Heartbeat: {HEARTBEAT_LOG}")
print(f"Loaded: USDT={state['USDT']:.2f} holding={state['holding']} qty={state['qty']:.6f}\n")

while True:
    try:
        data = fetch_spot_tickers()
        market = build_market(data)

        # update streak (plafonat)
        update_ok_counts(state, market)
        ok = state["ok_counts"]

        print(f"\n{now()} --- FAVORITES --- (verde>=+{GREEN_MIN_PCT:.2f}%, confirm={CONFIRM_BUY})")
        for sym in FAVORITES:
            if sym not in market:
                continue
            v = market[sym]
            ok_show = ok.get(sym, 0)  # deja plafonat 0..3
            print(f"{sym:8} last={v['last']:10.4f}  24h={v['chg']:>+6.2f}% {icon(v['chg'])}  vol={v['vol']:.0f}  ok={ok_show}/{CONFIRM_BUY}")

        # SELL: daca holding coboara sub prag (inclusiv rosu)
        if state["holding"]:
            h = state["holding"]
            if h in market:
                hchg = market[h]["chg"]
                if hchg < GREEN_MIN_PCT:
                    sell_all(state, market, reason=f"(chg<{GREEN_MIN_PCT:.2f}% => exit)")
                    print(f"⚠️ SELL {h} (chg {hchg:+.2f}% < +{GREEN_MIN_PCT:.2f}%)")

        # BUY: doar daca nu avem holding + cooldown ok
        if state["holding"] is None:
            if can_trade(state):
                cand = pick_buy_candidate(state, market)
                if cand:
                    buy_all(state, cand, market, reason=f"(green confirmed {CONFIRM_BUY}/{CONFIRM_BUY})")
                    print(f"✅ BUY {cand} (confirmat verde)")
            else:
                rem = int(COOLDOWN_SEC - (time.time() - state["last_trade_ts"]))
                if rem > 0:
                    print(f"⏳ Cooldown activ: ~{rem}s")

        # status
        total = total_value(state, market)
        pnl = total - START_USDT
        pnl_pct = (pnl / START_USDT) * 100.0

        if state["holding"]:
            h = state["holding"]
            hv = market[h]
            print(f"   Holding: {h} qty={state['qty']:.6f} entry={state['entry']:.6f} | 24h {hv['chg']:+.2f}% {icon(hv['chg'])}")
        else:
            print("   Holding: USDT only")

        print(f"   Total: {total:.2f} | PnL: {pnl:+.2f} ({pnl_pct:+.2f}%)")
        print("   (stare salvata automat)")

        heartbeat(state)
        time.sleep(CHECK_EVERY_SEC)

    except Exception as e:
        log_line(HEARTBEAT_LOG, f"{ts()} ERROR {repr(e)}")
        print(f"{now()} Eroare: {e}")
        time.sleep(5)

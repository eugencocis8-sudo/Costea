# btc_momentum_fee_bot.py
# === BTC MOMENTUM BOT (PAPER) | BUY on rising price | SELL on flat/down | SELL only if covers fees ===
# - Uses Bitget public ticker (last price)
# - Paper portfolio (USDT/BTC) with fee
# - State saved (resume after restart) + heartbeat + trades log
#
# Strategy:
#   BUY when short-term momentum turns positive (slope up + % up)
#   SELL when momentum turns negative OR flat too long
#   SELL only if exit price >= break-even including buy+sell fees (optionally +min extra profit)
#   Optional emergency stop-loss (disable by setting STOP_LOSS_PCT = 0)

import os, json, time, math, shutil
from collections import deque
from datetime import datetime, timezone
from typing import Dict, Any, Tuple, Optional
import requests

# ---------------- CONFIG ----------------
SYMBOL = "BTCUSDT"

CHECK_SECONDS = 5                      # poll ticker every 5s
WINDOW_POINTS = 36                     # 36 points * 5s = ~3 minutes window for momentum
BUY_PCT_MIN = 0.0015                   # +0.15% over window to consider "rising"
SLOPE_MIN = 0.0                        # slope must be > 0 (basic)
BUY_CONFIRM = 3                        # need 3 consecutive BUY signals before entering
COOLDOWN_SECONDS = 30                  # avoid overtrading

# "Flat a lot of time" definition
FLAT_POINTS = 60                       # 60 points * 5s = 5 minutes
FLAT_RANGE_MAX = 0.0006                # if (max-min)/mid <= 0.06% => flat

FEE = 0.001                            # 0.10% per trade
MIN_EXTRA_PROFIT = 0.0                 # 0.0 = only cover fees; set e.g. 0.001 = +0.10% extra

# Safety (optional). If you set STOP_LOSS_PCT=0 => disabled.
STOP_LOSS_PCT = 0.02                   # 2% emergency stop (avoids holding forever in a dump)

STATE_FILE = "btc_momentum_state.json"
STATE_BAK  = "btc_momentum_state.bak.json"
TRADES_LOG = "btc_momentum_trades.log"
HEARTBEAT  = "btc_momentum_heartbeat.log"

BASE = "https://api.bitget.com"
TICKER_ENDPOINTS = [
    "/api/v2/spot/market/tickers",     # works for many users: ?symbol=BTCUSDT
]

TIMEOUT = 10

# ---------------- UTIL ----------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def atomic_save(path: str, data: Dict[str, Any], bak: Optional[str] = None) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    if bak and os.path.exists(path):
        try:
            shutil.copy2(path, bak)
        except Exception:
            pass
    os.replace(tmp, path)

def log_line(path: str, msg: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(msg + "\n")
        f.flush()
        os.fsync(f.fileno())

def safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default

def fmt_pct(x: float) -> str:
    return f"{x*100:+.2f}%"

# ---------------- BITGET ----------------
def fetch_last(symbol: str) -> float:
    last_err = None
    for ep in TICKER_ENDPOINTS:
        url = BASE + ep
        try:
            r = requests.get(url, params={"symbol": symbol}, timeout=TIMEOUT)
            if r.status_code != 200:
                last_err = f"HTTP={r.status_code} {r.text[:200]}"
                continue
            j = r.json()
            if str(j.get("code")) != "00000":
                last_err = f"API code={j.get('code')} msg={j.get('msg')}"
                continue
            data = j.get("data")
            d = data[0] if isinstance(data, list) and data else data
            last = d.get("lastPr") or d.get("last") or d.get("close") or d.get("price")
            last_f = safe_float(last)
            if last_f <= 0:
                last_err = f"bad last={last}"
                continue
            return last_f
        except Exception as e:
            last_err = str(e)
    raise RuntimeError(f"ticker failed: {last_err}")

# ---------------- STRATEGY MATH ----------------
def simple_slope(prices: list) -> float:
    """
    Returns a simple slope estimate using linear regression on index vs price.
    Positive => rising.
    """
    n = len(prices)
    if n < 3:
        return 0.0
    # x = 0..n-1
    x_mean = (n - 1) / 2.0
    y_mean = sum(prices) / n
    num = 0.0
    den = 0.0
    for i, y in enumerate(prices):
        dx = i - x_mean
        num += dx * (y - y_mean)
        den += dx * dx
    if den == 0:
        return 0.0
    return num / den  # price units per index step

def pct_change(a: float, b: float) -> float:
    if a <= 0:
        return 0.0
    return (b - a) / a

def is_flat(prices: list) -> bool:
    if len(prices) < 10:
        return False
    mx = max(prices)
    mn = min(prices)
    mid = (mx + mn) / 2.0
    if mid <= 0:
        return False
    rng = (mx - mn) / mid
    return rng <= FLAT_RANGE_MAX

# ---------------- PAPER ENGINE ----------------
def break_even_price(entry: float) -> float:
    """
    Minimum exit price so that after buy fee and sell fee you're >= 0 profit.
    Buy uses USDT*(1-fee), sell uses proceeds*(1-fee).
    Break-even in price terms: exit >= entry * (1+fee) / (1-fee)
    (because you effectively paid fee on buy and will pay fee on sell)
    """
    return entry * (1.0 + FEE) / (1.0 - FEE)

def required_exit_price(entry: float) -> float:
    # add optional extra profit target
    be = break_even_price(entry)
    return be * (1.0 + MIN_EXTRA_PROFIT)

def portfolio_total(usdt: float, btc: float, last: float) -> float:
    return usdt + btc * last

def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
        # migrations
        st.setdefault("created", now_iso())
        st.setdefault("usdt", 10000.0)
        st.setdefault("btc", 0.0)
        st.setdefault("entry", 0.0)
        st.setdefault("holding", False)
        st.setdefault("buy_streak", 0)
        st.setdefault("last_trade_ts", 0.0)
        return st
    st = {
        "created": now_iso(),
        "usdt": 10000.0,
        "btc": 0.0,
        "entry": 0.0,
        "holding": False,
        "buy_streak": 0,
        "last_trade_ts": 0.0,
    }
    atomic_save(STATE_FILE, st, STATE_BAK)
    return st

def can_trade(st: Dict[str, Any]) -> bool:
    return (time.time() - float(st.get("last_trade_ts", 0.0))) >= COOLDOWN_SECONDS

def do_buy(st: Dict[str, Any], price: float) -> None:
    usdt = float(st["usdt"])
    if usdt <= 0:
        return
    fee = usdt * FEE
    net = usdt - fee
    qty = net / price

    st["holding"] = True
    st["btc"] = qty
    st["usdt"] = 0.0
    st["entry"] = price
    st["last_trade_ts"] = time.time()
    st["buy_streak"] = 0

    log_line(TRADES_LOG, f"{now_iso()} BUY price={price:.2f} usdt={usdt:.2f} fee={fee:.2f} btc={qty:.8f}")

def do_sell(st: Dict[str, Any], price: float, reason: str) -> None:
    btc = float(st["btc"])
    if btc <= 0:
        return
    gross = btc * price
    fee = gross * FEE
    net = gross - fee

    entry = float(st.get("entry", 0.0))
    req = required_exit_price(entry) if entry > 0 else 0.0
    log_line(
        TRADES_LOG,
        f"{now_iso()} SELL price={price:.2f} gross={gross:.2f} fee={fee:.2f} net={net:.2f} "
        f"entry={entry:.2f} req_exit={req:.2f} reason={reason}"
    )

    st["holding"] = False
    st["usdt"] = net
    st["btc"] = 0.0
    st["entry"] = 0.0
    st["last_trade_ts"] = time.time()
    st["buy_streak"] = 0

# ---------------- MAIN ----------------
def main():
    print("=== BTC MOMENTUM BOT (PAPER) | BUY rising | SELL flat/down but only if covers fees ===")
    print(f"Symbol={SYMBOL} | Check={CHECK_SECONDS}s | Window≈{WINDOW_POINTS*CHECK_SECONDS}s | Flat≈{FLAT_POINTS*CHECK_SECONDS}s")
    print(f"BUY: pct_window >= {BUY_PCT_MIN*100:.2f}% AND slope>0 (confirm {BUY_CONFIRM})")
    print(f"SELL: if flat too long OR slope<=0 / down, BUT only if price>=break-even(+extra)")
    print(f"Fee={FEE*100:.2f}% | MinExtraProfit={MIN_EXTRA_PROFIT*100:.2f}% | StopLoss={STOP_LOSS_PCT*100:.2f}% (0=off)")
    print(f"State={STATE_FILE} (bak {STATE_BAK}) | Trades={TRADES_LOG} | Heartbeat={HEARTBEAT}\n")

    st = load_state()

    prices = deque(maxlen=max(WINDOW_POINTS, FLAT_POINTS) + 5)

    while True:
        try:
            last = fetch_last(SYMBOL)
            prices.append(last)

            # build windows
            w = list(prices)[-WINDOW_POINTS:] if len(prices) >= WINDOW_POINTS else list(prices)
            f = list(prices)[-FLAT_POINTS:] if len(prices) >= FLAT_POINTS else []

            slope = simple_slope(w)
            ch = pct_change(w[0], w[-1]) if len(w) >= 2 else 0.0
            flat = is_flat(f) if f else False

            holding = bool(st["holding"])
            entry = float(st.get("entry", 0.0))
            total = portfolio_total(float(st["usdt"]), float(st["btc"]), last)

            # BUY signal
            buy_signal = (ch >= BUY_PCT_MIN) and (slope > SLOPE_MIN)

            # SELL signal (direction/flat)
            down_signal = (ch < 0) or (slope <= 0)
            sell_signal = flat or down_signal

            # required exit to cover fees (+ extra)
            req_exit = required_exit_price(entry) if holding and entry > 0 else 0.0
            can_sell_profitably = (last >= req_exit) if holding and entry > 0 else False

            # emergency stop-loss (optional)
            pnl = (last - entry) / entry if holding and entry > 0 else 0.0
            stoploss_hit = (STOP_LOSS_PCT > 0) and holding and (pnl <= -STOP_LOSS_PCT)

            ts = time.strftime("%H:%M:%S")
            arrow = "⬆️" if slope > 0 else ("⬇️" if slope < 0 else "➡️")
            print(f"{ts} price={last:.2f} win={fmt_pct(ch)} slope={slope:+.3f} {arrow} flat={'YES' if flat else 'no'} | "
                  f"Holding={'BTC' if holding else 'USDT'} total≈{total:.2f}", flush=True)

            if not holding:
                if buy_signal:
                    st["buy_streak"] = int(st.get("buy_streak", 0)) + 1
                else:
                    st["buy_streak"] = 0

                print(f"   BUYsig={'YES' if buy_signal else 'no'} streak={st['buy_streak']}/{BUY_CONFIRM} usdt={st['usdt']:.2f}")

                if st["buy_streak"] >= BUY_CONFIRM and can_trade(st):
                    do_buy(st, last)
                    print(f"   ✅ BUY at {last:.2f} (confirmat)\n")
                else:
                    print("")
            else:
                print(f"   entry={entry:.2f} pnl={fmt_pct(pnl)} req_exit≈{req_exit:.2f} sell_ok={'YES' if can_sell_profitably else 'no'} "
                      f"SELLsig={'YES' if sell_signal else 'no'}")
                # SELL decision
                if stoploss_hit and can_trade(st):
                    do_sell(st, last, "STOPLOSS")
                    print(f"   ⚠️ SELL STOPLOSS at {last:.2f}\n")
                elif sell_signal and can_sell_profitably and can_trade(st):
                    reason = "FLAT" if flat else "DOWN"
                    do_sell(st, last, reason)
                    print(f"   ✅ SELL {reason} at {last:.2f} (acopera fee)\n")
                else:
                    if sell_signal and not can_sell_profitably:
                        print("   ⏳ Vrea sa vanda, DAR nu acopera fee inca. Astept…\n")
                    else:
                        print("")

            # save + heartbeat
            atomic_save(STATE_FILE, st, STATE_BAK)
            log_line(HEARTBEAT, f"{now_iso()} alive holding={st['holding']} usdt={st['usdt']:.2f} btc={st['btc']:.8f} price={last:.2f}")

        except Exception as e:
            print(f"{time.strftime('%H:%M:%S')} Eroare: {e}")

        time.sleep(CHECK_SECONDS)

if __name__ == "__main__":
    main()

# multi_semafor_bot.py
# === MULTI SEMAFOR BOT (BTC + ETH) | change24h LIVE | 6 praguri | PAPER ===
# Upgrade: TakeProfit + TrailingStop(activate after +0.5%) + StopLoss + atomic state + backup + heartbeat

import json
import os
import time
import math
import shutil
from datetime import datetime, timezone
from typing import Dict, Any, Tuple, Optional
import requests

# ---------------- CONFIG ----------------
SYMBOLS = ["BTCUSDT", "ETHUSDT"]

THRESHOLDS = [0.30, 0.80, 1.30, 2.00, 3.00, 5.00]  # change24h thresholds in %

CONFIRM_BUY = 3                 # consecutive checks meeting condition
CHECK_SECONDS = 10              # how often to poll
COOLDOWN_SECONDS = 30           # min seconds between trades per portfolio
FEE_RATE = 0.001                # 0.10%

# Profit protection
TRAILING_PCT = 0.005            # 0.5% trailing
TRAIL_ACTIVATE_PROFIT = 0.005   # activate trailing only after +0.5% from entry
STOP_LOSS_PCT = 0.012           # -1.2% hard stop-loss (safety)

# Take Profit mapping per threshold (prag -> TP % over entry)
# "cum e mai bine": prag mic = TP mai mic (iese mai des), prag mare = TP mai mare (lasă trendul)
TAKE_PROFIT_MAP = {
    0.30: 0.006,   # +0.6%
    0.80: 0.008,   # +0.8%
    1.30: 0.010,   # +1.0%
    2.00: 0.012,   # +1.2%
    3.00: 0.015,   # +1.5%
    5.00: 0.020,   # +2.0%
}

STATE_FILE = "multi_semafor_state.json"
STATE_BAK = "multi_semafor_state.bak.json"
TRADES_LOG = "multi_semafor_trades.log"
HEARTBEAT_LOG = "multi_semafor_heartbeat.log"

# Bitget public base
BASE = "https://api.bitget.com"

# These endpoints are used by many working bots; keep fallback in case of path changes.
TICKER_ENDPOINTS = [
    "/api/v2/spot/market/tickers",   # ?symbol=BTCUSDT
    "/api/v2/spot/market/ticker",    # some installs show 404, but keep fallback
]

TIMEOUT = 10

# -------------- UTIL -------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def log_trade(msg: str) -> None:
    line = f"{now_iso()} {msg}\n"
    with open(TRADES_LOG, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())

def log_heartbeat(state: Dict[str, Any]) -> None:
    # quick summary
    holding_cnt = sum(1 for p in state["portfolios"].values() if p.get("holding"))
    usdt_sum = sum(float(p.get("usdt", 0.0)) for p in state["portfolios"].values() if not p.get("holding"))
    line = f"{now_iso()} alive holding_ports={holding_cnt} usdt_idle≈{usdt_sum:.2f}\n"
    with open(HEARTBEAT_LOG, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())

def atomic_save_json(path: str, data: Dict[str, Any], bak_path: Optional[str] = None) -> None:
    tmp = path + ".tmp"
    # write temp
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    # backup old
    if bak_path and os.path.exists(path):
        try:
            shutil.copy2(path, bak_path)
        except Exception:
            pass
    # atomic replace
    os.replace(tmp, path)

def safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default

def format_pct(x: float) -> str:
    return f"{x:+.2f}%"

# -------------- BITGET DATA -------------------

def fetch_ticker(symbol: str) -> Tuple[float, float]:
    """
    Returns (last_price, change24h_percent)
    change24h is percent value like +0.78, -0.12
    """
    last_err = None
    for ep in TICKER_ENDPOINTS:
        url = BASE + ep
        try:
            r = requests.get(url, params={"symbol": symbol}, timeout=TIMEOUT)
            if r.status_code != 200:
                last_err = f"HTTP={r.status_code} url={url} body={r.text[:200]}"
                continue
            j = r.json()
            if str(j.get("code")) != "00000":
                last_err = f"API code={j.get('code')} msg={j.get('msg')} url={url}"
                continue

            data = j.get("data")
            # Some endpoints return dict, some list
            if isinstance(data, list) and len(data) > 0:
                d = data[0]
            elif isinstance(data, dict):
                d = data
            else:
                last_err = f"bad data shape url={url}"
                continue

            # common keys seen in Bitget responses:
            # lastPr / last / close ; change24h / change24H / priceChangePercent
            last = (
                d.get("lastPr")
                or d.get("last")
                or d.get("close")
                or d.get("price")
            )
            ch = (
                d.get("change24h")
                or d.get("change24H")
                or d.get("priceChangePercent")
                or d.get("chg24h")
            )

            last_f = safe_float(last)
            ch_f = safe_float(ch)

            # Some APIs return change as ratio (0.0078) vs percent (0.78). Heuristic:
            if abs(ch_f) < 0.2 and abs(ch_f) > 0.00001:
                # likely ratio -> convert to %
                ch_f *= 100.0

            if last_f <= 0:
                last_err = f"invalid last price url={url} last={last}"
                continue

            return last_f, ch_f

        except Exception as e:
            last_err = f"exception url={url}: {e}"

    raise RuntimeError(f"ticker failed for {symbol}: {last_err}")

# -------------- PAPER ENGINE -------------------

def new_portfolio(symbol: str, thr: float, usdt: float) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "threshold": thr,
        "usdt": usdt,
        "holding": False,
        "qty": 0.0,
        "entry": 0.0,
        "peak": 0.0,            # for trailing
        "trailing_on": False,   # activated after +0.5%
        "streak": 0,
        "last_trade_ts": 0.0,
    }

def total_value(port: Dict[str, Any], last_price: float) -> float:
    if port["holding"]:
        return float(port["qty"]) * last_price
    return float(port["usdt"])

def can_trade(port: Dict[str, Any]) -> bool:
    return (time.time() - float(port.get("last_trade_ts", 0.0))) >= COOLDOWN_SECONDS

def do_buy(port: Dict[str, Any], ask: float) -> None:
    usdt = float(port["usdt"])
    if usdt <= 0:
        return
    fee = usdt * FEE_RATE
    net = usdt - fee
    qty = net / ask

    port["holding"] = True
    port["qty"] = qty
    port["entry"] = ask
    port["usdt"] = 0.0
    port["peak"] = ask
    port["trailing_on"] = False
    port["last_trade_ts"] = time.time()

    log_trade(f"BUY {port['symbol']} thr={port['threshold']:.2f}% ask={ask:.2f} usdt={usdt:.2f} fee={fee:.2f} qty={qty:.8f}")

def do_sell(port: Dict[str, Any], bid: float, reason: str) -> None:
    qty = float(port["qty"])
    if qty <= 0:
        return
    gross = qty * bid
    fee = gross * FEE_RATE
    net = gross - fee

    port["holding"] = False
    port["usdt"] = net
    port["qty"] = 0.0
    port["entry"] = 0.0
    port["peak"] = 0.0
    port["trailing_on"] = False
    port["streak"] = 0
    port["last_trade_ts"] = time.time()

    log_trade(f"SELL {port['symbol']} thr={port['threshold']:.2f}% bid={bid:.2f} net={net:.2f} fee={fee:.2f} reason={reason}")

# -------------- MAIN LOOP -------------------

def ensure_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
        # migrate missing fields if older state
        ports = st.get("portfolios", {})
        for k, p in ports.items():
            p.setdefault("peak", 0.0)
            p.setdefault("trailing_on", False)
        st["portfolios"] = ports
        st.setdefault("created", now_iso())
        return st

    # initial: split total virtual capital across all portfolios equally
    total_capital = 10000.0
    n = len(SYMBOLS) * len(THRESHOLDS)
    per = total_capital / n

    ports: Dict[str, Any] = {}
    for sym in SYMBOLS:
        for thr in THRESHOLDS:
            key = f"{sym}@{thr:.2f}"
            ports[key] = new_portfolio(sym, thr, per)

    st = {"created": now_iso(), "portfolios": ports}
    atomic_save_json(STATE_FILE, st, STATE_BAK)
    return st

def main():
    print("=== MULTI SEMAFOR BOT (BTC + ETH) | change24h LIVE | 6 praguri | PAPER (TP+Trailing+SL) ===")
    print("Praguri:", ", ".join([str(x) for x in THRESHOLDS]), "%")
    print(f"Confirm BUY: {CONFIRM_BUY} | Check: {CHECK_SECONDS}s | Cooldown: {COOLDOWN_SECONDS}s")
    print(f"Fee: {FEE_RATE*100:.2f}%")
    print(f"TP map: { {k: f'{v*100:.2f}%' for k,v in TAKE_PROFIT_MAP.items()} }")
    print(f"Trailing: {TRAILING_PCT*100:.2f}% (activate after +{TRAIL_ACTIVATE_PROFIT*100:.2f}%) | StopLoss: -{STOP_LOSS_PCT*100:.2f}%")
    print(f"State: {STATE_FILE} (backup {STATE_BAK})")
    print(f"Trades: {TRADES_LOG}")
    print(f"Heartbeat: {HEARTBEAT_LOG}")
    print("BUY: verde >= prag (confirmat) | SELL: rosu (<0) instant | plus TP/Trailing/SL")
    print("")

    state = ensure_state()

    # Small cache for console clarity
    last_print_ts = 0.0

    while True:
        try:
            tickers: Dict[str, Tuple[float, float]] = {}
            for sym in SYMBOLS:
                last, ch24 = fetch_ticker(sym)
                tickers[sym] = (last, ch24)

            # print live
            print_time = time.strftime("%H:%M:%S")
            print(f"\n{print_time} --- LIVE ---")
            for sym in SYMBOLS:
                last, ch24 = tickers[sym]
                light = "🟢" if ch24 >= 0 else "🔴"
                print(f"{sym} last={last:.2f} 24h={format_pct(ch24)} {light}")

                # iterate portfolios for this symbol in threshold order
                for thr in THRESHOLDS:
                    key = f"{sym}@{thr:.2f}"
                    p = state["portfolios"][key]

                    # update streak logic for BUY (only if not holding)
                    if not p["holding"]:
                        if ch24 >= thr:
                            p["streak"] = int(p.get("streak", 0)) + 1
                        else:
                            p["streak"] = 0

                    # decide actions
                    if p["holding"]:
                        entry = float(p["entry"])
                        qty = float(p["qty"])
                        # update peak for trailing
                        if float(p.get("peak", 0.0)) <= 0:
                            p["peak"] = entry
                        p["peak"] = max(float(p["peak"]), last)

                        # profit ratio
                        pnl_pct = (last - entry) / entry if entry > 0 else 0.0

                        # activate trailing after +0.5%
                        if (not p.get("trailing_on", False)) and pnl_pct >= TRAIL_ACTIVATE_PROFIT:
                            p["trailing_on"] = True
                            log_trade(f"TRAIL_ON {sym} thr={thr:.2f}% at price={last:.2f} pnl={pnl_pct*100:.2f}%")

                        # SELL rules priority:
                        # 1) instant sell if red
                        if ch24 < 0 and can_trade(p):
                            do_sell(p, bid=last, reason="RED(<0)")

                        # 2) hard stop-loss
                        elif pnl_pct <= -STOP_LOSS_PCT and can_trade(p):
                            do_sell(p, bid=last, reason=f"STOPLOSS({-STOP_LOSS_PCT*100:.2f}%)")

                        # 3) take profit
                        else:
                            tp = TAKE_PROFIT_MAP.get(thr, 0.01)
                            if pnl_pct >= tp and can_trade(p):
                                do_sell(p, bid=last, reason=f"TAKEPROFIT(+{tp*100:.2f}%)")

                            # 4) trailing stop (only if active)
                            elif p.get("trailing_on", False):
                                peak = float(p.get("peak", entry))
                                trail_level = peak * (1.0 - TRAILING_PCT)
                                if last <= trail_level and can_trade(p):
                                    do_sell(p, bid=last, reason=f"TRAIL({TRAILING_PCT*100:.2f}%)")

                        # print status
                        tot = total_value(p, last)
                        print(f"  thr {thr:.2f}% | HOLD qty={qty:.8f} entry={entry:.2f} peak={float(p['peak']):.2f} "
                              f"pnl={pnl_pct*100:+.2f}% | total≈{tot:.2f} | ok={p.get('streak',0)}/{CONFIRM_BUY}")

                    else:
                        # not holding -> maybe BUY
                        tot = total_value(p, last)
                        ok = int(p.get("streak", 0))
                        if ok >= CONFIRM_BUY and ch24 >= thr and can_trade(p):
                            do_buy(p, ask=last)
                        print(f"  thr {thr:.2f}% | USDT={float(p['usdt']):.2f} | total≈{tot:.2f} | ok={ok}/{CONFIRM_BUY}")

            # save state + heartbeat each loop
            atomic_save_json(STATE_FILE, state, STATE_BAK)
            log_heartbeat(state)

        except Exception as e:
            # don't crash, keep looping
            print(f"{time.strftime('%H:%M:%S')} Eroare: {e}")

        time.sleep(CHECK_SECONDS)

if __name__ == "__main__":
    main()

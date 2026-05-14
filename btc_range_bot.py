# btc_range_bot.py
# === BTC RANGE BOT (1h) | Bollinger + RSI | trailing 0.5% (activate +0.5%) ===
# Paper trading (fara bani reali). Salveaza stare si loguri.
#
# Strategie (disciplinata):
# BUY: close <= BB_lower AND RSI < 35 AND regim RANGE (nu trending puternic)
# SELL: close >= BB_upper OR RSI > 65 OR trailing(-0.5%) [dupa +0.5%] OR stop(-1.2%)
#
# Robust:
# - foloseste Bitget V2 candles: /api/v2/spot/market/candles
# - retry la erori, heartbeat chiar si in timpul fetch-ului initial
# - evita crash silent

import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Tuple

import requests

# ---------------- CONFIG ----------------
BASE_URL = "https://api.bitget.com"

SYMBOL = "BTCUSDT"
GRANULARITY = "1h"   # trebuie sa fie exact din lista Bitget: 1h, 4h, 1day etc.
LOOKBACK_DAYS = 30

# Indicatori
BB_WINDOW = 20
BB_STD = 2.0
RSI_PERIOD = 14
EMA_PERIOD = 20

# Detectie regim (trend vs range): procent schimbare EMA20 in ultimele 10h
EMA_DELTA_HOURS = 10
TRENDING_THRESHOLD_PCT = 0.8  # daca EMA20 a urcat/coborat >0.8% in 10h => trending (nu cumparam)

# Trade params
FEE_PCT = 0.001  # 0.10%
CAPITAL_USDT_START = 10000.0

TRAIL_PCT = 0.005          # trailing stop 0.5%
TRAIL_ACTIVATE_PCT = 0.005 # activate trailing dupa +0.5% peste entry
STOP_PCT = 0.012           # stop loss 1.2%

# Timing
CHECK_EVERY_SEC = 30

# API fetching
CANDLE_LIMIT = 200  # Bitget returneaza lista; 200 e ok
REQUEST_TIMEOUT = 12
MAX_FETCH_ERRORS = 5

# Files
STATE_FILE = "btc_range_state.json"
TRADES_LOG = "btc_range_trades.log"
HEARTBEAT_LOG = "btc_range_heartbeat.log"

# ---------------- HELPERS ----------------
def now_str() -> str:
    return datetime.now().strftime("%H:%M:%S")

def utc_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)

def safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default

def heartbeat(line: str) -> None:
    # scrie heartbeat in fisier (append)
    try:
        with open(HEARTBEAT_LOG, "a", encoding="utf-8") as f:
            f.write(line.strip() + "\n")
    except Exception:
        pass

def log_trade(line: str) -> None:
    try:
        with open(TRADES_LOG, "a", encoding="utf-8") as f:
            f.write(line.strip() + "\n")
    except Exception:
        pass

def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "usdt": CAPITAL_USDT_START,
        "holding": False,
        "qty": 0.0,
        "entry": 0.0,
        "peak": 0.0,
        "last_action_ts": None,
    }

def save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)

# ---------------- BITGET API ----------------
def http_get(url: str, params: Dict[str, Any]) -> Any:
    r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP={r.status_code} body={r.text}")
    return r.json()

def get_spot_candles(symbol: str, granularity: str, start_ms: int, end_ms: int, limit: int) -> List[List[Any]]:
    """
    V2 endpoint:
    GET /api/v2/spot/market/candles?symbol=BTCUSDT&granularity=1h&limit=5
    Optional: startTime/endTime in ms (la Bitget sunt acceptate in multe endpoints; daca nu, tot merge cu limit).
    Din testul tau: functioneaza cu symbol+granularity+limit.
    Ca sa luam lookback, incercam cu startTime/endTime; daca serverul ignora, tot primim ultimul 'limit'.
    """
    url = f"{BASE_URL}/api/v2/spot/market/candles"
    params = {
        "symbol": symbol,
        "granularity": granularity,
        "limit": limit
    }
    # incercam si intervalul; daca Bitget nu-l accepta, poate ignora dar nu strica
    params["startTime"] = str(start_ms)
    params["endTime"] = str(end_ms)

    data = http_get(url, params)
    if data.get("code") != "00000":
        raise RuntimeError(f"Candles API error: {data}")
    arr = data.get("data") or []
    # fiecare candle: [ts, open, high, low, close, baseVol, quoteVol, ...]
    # ts e string ms
    out = []
    for row in arr:
        if not row or len(row) < 5:
            continue
        ts = int(row[0])
        o = safe_float(row[1])
        h = safe_float(row[2])
        l = safe_float(row[3])
        c = safe_float(row[4])
        out.append([ts, o, h, l, c])
    # Uneori vine in ordine descrescatoare; sortam crescator
    out.sort(key=lambda x: x[0])
    return out

# ---------------- INDICATORS ----------------
def sma(values: List[float], window: int) -> Optional[float]:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window

def stdev(values: List[float], window: int) -> Optional[float]:
    if len(values) < window:
        return None
    m = sma(values, window)
    if m is None:
        return None
    var = sum((x - m) ** 2 for x in values[-window:]) / window
    return math.sqrt(var)

def ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    out = [values[0]]
    for i in range(1, len(values)):
        out.append(values[i] * k + out[-1] * (1 - k))
    return out

def rsi(values: List[float], period: int) -> Optional[float]:
    if len(values) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))

def bollinger(values: List[float], window: int, num_std: float) -> Optional[Tuple[float, float]]:
    m = sma(values, window)
    s = stdev(values, window)
    if m is None or s is None:
        return None
    lower = m - num_std * s
    upper = m + num_std * s
    return lower, upper

def regime_range(close_values: List[float]) -> Tuple[bool, float]:
    """
    Determina daca e RANGE pe baza EMA20Δ in ultimele EMA_DELTA_HOURS.
    Return: (is_range, ema_delta_pct)
    """
    if len(close_values) < max(EMA_PERIOD, EMA_DELTA_HOURS + 2):
        return True, 0.0
    ema_series = ema(close_values, EMA_PERIOD)
    idx_now = len(ema_series) - 1
    idx_past = max(0, idx_now - EMA_DELTA_HOURS)
    ema_now = ema_series[idx_now]
    ema_past = ema_series[idx_past]
    if ema_past == 0:
        return True, 0.0
    delta_pct = (ema_now - ema_past) / ema_past * 100.0
    is_range = abs(delta_pct) < TRENDING_THRESHOLD_PCT
    return is_range, delta_pct

# ---------------- FETCH LOOKBACK (ROBUST) ----------------
def fetch_initial_lookback() -> List[List[Any]]:
    """
    Ia candles aproximativ 30d. Daca API nu respecta intervalul, tot va returna ceva.
    Robust: retry + heartbeat in timpul fetch-ului.
    """
    print(f"{now_str()} fetching initial {LOOKBACK_DAYS}d candles...")
    heartbeat(f"{datetime.now().isoformat(timespec='seconds')} alive (fetching lookback)")

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=LOOKBACK_DAYS)
    start_ms = utc_ms(start_dt)
    end_ms = utc_ms(end_dt)

    all_candles: List[List[Any]] = []
    cursor = start_ms
    step_ms = int(CANDLE_LIMIT * 60 * 60 * 1000)  # 200 candles * 1h

    retries = 0
    # hard cap safety
    max_total = 3000

    while cursor < end_ms and len(all_candles) < max_total:
        try:
            chunk_end = min(end_ms, cursor + step_ms)
            chunk = get_spot_candles(SYMBOL, GRANULARITY, cursor, chunk_end, CANDLE_LIMIT)

            if chunk:
                # evita duplicate timestamp
                if all_candles and chunk[0][0] == all_candles[-1][0]:
                    chunk = chunk[1:]
                all_candles.extend(chunk)
                cursor = all_candles[-1][0] + 1
            else:
                cursor = chunk_end + 1

            print(f"{now_str()} candles loaded: {len(all_candles)}")
            heartbeat(f"{datetime.now().isoformat(timespec='seconds')} alive (fetch {len(all_candles)})")

            retries = 0

            # daca API tot ne da aceleasi ultime candles, iesim
            if len(all_candles) >= 600:
                # e suficient pentru BB/RSI (20/14) + regim
                break

        except Exception as e:
            retries += 1
            print(f"{now_str()} ⚠️ fetch error ({retries}/{MAX_FETCH_ERRORS}): {e}")
            heartbeat(f"{datetime.now().isoformat(timespec='seconds')} fetch error retry={retries}")
            time.sleep(5)
            if retries >= MAX_FETCH_ERRORS:
                raise RuntimeError("Too many fetch errors during initial lookback") from e

    if len(all_candles) < 100:
        raise RuntimeError("Not enough candles loaded (API returned too few)")

    print(f"{now_str()} initial candles ready: {len(all_candles)}")
    heartbeat(f"{datetime.now().isoformat(timespec='seconds')} alive (lookback ready {len(all_candles)})")
    return all_candles

def fetch_latest_candles(limit: int = 5) -> List[List[Any]]:
    """
    Ia ultimele N candles (fara time range). Stabil.
    """
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=2)
    data = get_spot_candles(SYMBOL, GRANULARITY, utc_ms(start_dt), utc_ms(end_dt), limit)
    return data

def merge_candles(history: List[List[Any]], latest: List[List[Any]]) -> List[List[Any]]:
    """
    Adauga candles noi in history, fara duplicate.
    """
    if not latest:
        return history
    known = {c[0] for c in history}
    for c in latest:
        if c[0] not in known:
            history.append(c)
    history.sort(key=lambda x: x[0])
    # pastreaza max 1200 candles
    if len(history) > 1200:
        history = history[-1200:]
    return history

# ---------------- TRADING (PAPER) ----------------
def do_buy(state: Dict[str, Any], price: float) -> None:
    usdt = state["usdt"]
    if usdt <= 0:
        return
    fee = usdt * FEE_PCT
    spend = usdt - fee
    qty = spend / price
    state["usdt"] = 0.0
    state["holding"] = True
    state["qty"] = qty
    state["entry"] = price
    state["peak"] = price
    state["last_action_ts"] = datetime.now().isoformat(timespec="seconds")
    log_trade(f"{datetime.now().isoformat(timespec='seconds')} BUY {SYMBOL} price={price:.2f} qty={qty:.8f} fee_usdt={fee:.2f}")
    print(f"{now_str()}   ✅ BUY close={price:.2f}")

def do_sell(state: Dict[str, Any], price: float, reason: str) -> None:
    qty = state["qty"]
    gross = qty * price
    fee = gross * FEE_PCT
    usdt = gross - fee
    entry = state["entry"]
    pnl = usdt - CAPITAL_USDT_START  # total pnl vs start (approx)
    state["usdt"] = usdt
    state["holding"] = False
    state["qty"] = 0.0
    state["entry"] = 0.0
    state["peak"] = 0.0
    state["last_action_ts"] = datetime.now().isoformat(timespec="seconds")
    log_trade(f"{datetime.now().isoformat(timespec='seconds')} SELL {SYMBOL} price={price:.2f} reason={reason} usdt={usdt:.2f} fee_usdt={fee:.2f}")
    print(f"{now_str()}   ⚠️ SELL ({reason}) close={price:.2f}")

# ---------------- MAIN LOOP ----------------
def main():
    print("=== BTC RANGE BOT (1h) | Bollinger + RSI | trailing 0.5% (activate +0.5%) ===")
    print(f"Symbol={SYMBOL}  Lookback={LOOKBACK_DAYS}d  Granularity={GRANULARITY}")
    print("BUY: close<=BB_lower AND RSI<35.0 AND NOT trending")
    print("SELL: close>=BB_upper OR RSI>65.0 OR trailing(-0.5%) [after +0.5%] OR stop(-1.2%)")
    print(f"Fee={FEE_PCT*100:.2f}%  Capital=100% (paper)")
    print(f"State={STATE_FILE} Trades={TRADES_LOG} Heartbeat={HEARTBEAT_LOG}\n")

    state = load_state()
    print(f"Loaded: USDT={state['usdt']:.2f} holding={state['holding']} qty={state['qty']:.8f} entry={state['entry']:.2f}\n")

    # fetch initial candles
    history = fetch_initial_lookback()

    fetch_errors = 0

    while True:
        try:
            # update history with latest candles
            latest = fetch_latest_candles(limit=5)
            history = merge_candles(history, latest)

            closes = [c[4] for c in history]
            close = closes[-1]

            bb = bollinger(closes, BB_WINDOW, BB_STD)
            r = rsi(closes, RSI_PERIOD)
            is_range, ema_delta = regime_range(closes)

            if bb is None or r is None:
                print(f"{now_str()} waiting indicators (need more candles)...")
                heartbeat(f"{datetime.now().isoformat(timespec='seconds')} alive (warming up)")
                time.sleep(CHECK_EVERY_SEC)
                continue

            bb_lower, bb_upper = bb
            regime = "RANGE" if is_range else "TREND"
            print(f"{now_str()} price={close:.2f} | close(1h)={close:.2f} | BB[{bb_lower:.2f} .. {bb_upper:.2f}] | RSI={r:.1f} | {regime} (EMA{EMA_PERIOD}Δ{EMA_DELTA_HOURS}h={ema_delta:+.2f}%)")

            holding = bool(state["holding"])
            entry = float(state["entry"]) if holding else 0.0

            # update peak if holding
            if holding:
                state["peak"] = max(float(state.get("peak", entry)), close)
            peak = float(state.get("peak", 0.0)) if holding else 0.0

            # trailing active only after +0.5%
            trailing_active = False
            if holding and entry > 0:
                trailing_active = (peak >= entry * (1.0 + TRAIL_ACTIVATE_PCT))

            # Decisions
            if not holding:
                # BUY only if disciplined conditions
                if (close <= bb_lower) and (r < 35.0) and is_range:
                    do_buy(state, close)
            else:
                # SELL reasons priority
                reason = None

                # trailing (only if active)
                if trailing_active:
                    trail_stop = peak * (1.0 - TRAIL_PCT)
                    if close <= trail_stop:
                        reason = "TRAIL"

                # stop-loss always active
                if reason is None and close <= entry * (1.0 - STOP_PCT):
                    reason = "STOP"

                # take profit / exit signals
                if reason is None and close >= bb_upper:
                    reason = "BB_UPPER"
                if reason is None and r > 65.0:
                    reason = "RSI_HIGH"

                if reason:
                    do_sell(state, close, reason)

            # Totals
            total = state["usdt"] if not state["holding"] else (state["qty"] * close)
            pnl = total - CAPITAL_USDT_START
            pnl_pct = (pnl / CAPITAL_USDT_START) * 100.0
            if state["holding"]:
                print(f"   Holding=BTC qty={state['qty']:.8f} entry={entry:.2f} peak={peak:.2f} trailing_active={'YES' if trailing_active else 'no'} | Total≈{total:.2f} PnL≈{pnl:+.2f} ({pnl_pct:+.2f}%)")
            else:
                print(f"   Holding=USDT {state['usdt']:.2f} | Total≈{total:.2f} PnL≈{pnl:+.2f} ({pnl_pct:+.2f}%)")

            # heartbeat + save
            heartbeat(f"{datetime.now().isoformat(timespec='seconds')} alive holding={state['holding']} usdt={state['usdt']:.2f}")
            save_state(state)

            fetch_errors = 0
            time.sleep(CHECK_EVERY_SEC)

        except KeyboardInterrupt:
            print("\nStopped by user.")
            heartbeat(f"{datetime.now().isoformat(timespec='seconds')} stopped_by_user")
            save_state(state)
            break
        except Exception as e:
            fetch_errors += 1
            print(f"{now_str()} Eroare: {e}")
            heartbeat(f"{datetime.now().isoformat(timespec='seconds')} error={str(e)[:120]}")
            # nu moare imediat
            if fetch_errors >= MAX_FETCH_ERRORS:
                print(f"{now_str()} Too many errors, sleeping 60s then retry...")
                fetch_errors = 0
                time.sleep(60)
            else:
                time.sleep(5)

if __name__ == "__main__":
    main()

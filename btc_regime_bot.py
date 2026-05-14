#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
BTC REGIME BOT (SPOT, PAPER) – RANGE + TREND
- Date: Bitget public API v2 (candles 1h)
- Strategie:
  * Detectare regim cu ADX(14):
      RANGE  : ADX < 20
      TREND  : ADX > 25
      NEUTRAL: altfel (nu intră, doar gestionează ieșirea dacă e în poziție)
  * RANGE entry:
      close <= BB_lower(20,2) AND RSI(14) < 35 AND ADX < 20
  * RANGE exit:
      TP +0.9%  OR  close >= BB_upper  OR RSI > 65
      TRAIL: activare după +0.6% ; trailing 0.5% (peak)
      STOP: entry - 1.2*ATR(14)
      TIME STOP: după 8h dacă nu atinge +0.2% => exit
  * TREND entry (UP trend):
      EMA50 > EMA200 AND ADX > 25 AND pullback spre EMA20
      (RSI între ~40..60)
  * TREND exit:
      EMA20 < EMA50 OR trailing ATR (1.2*ATR) după activare +0.8%
      STOP: entry - 1.2*ATR

- Paper trading cu fee (0.10% buy + 0.10% sell)
- Persistență: state json (resume după restart)
- Log trades + heartbeat
"""

import time
import json
import math
import os
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

# ===================== CONFIG =====================
BASE_URL = "https://api.bitget.com"
SYMBOL = "BTCUSDT"
GRANULARITY = "1h"          # Bitget v2 acceptă "1h"
LOOKBACK_DAYS = 30
CANDLE_LIMIT = 200          # per request
CHECK_INTERVAL_SEC = 60     # cât de des recalculează (1 min e ok)
COOLDOWN_SEC = 60 * 30      # după un trade, pauză 30 min

FEE_RATE = 0.001            # 0.10%
CAPITAL_FRACTION = 0.50     # 50% din USDT per trade (mai sigur decât 100%)

# RANGE parameters
BB_PERIOD = 20
BB_STD = 2.0
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14

ADX_RANGE_MAX = 20.0
ADX_TREND_MIN = 25.0

TP_RANGE = 0.009            # +0.9%
TRAIL_ACTIVATE_RANGE = 0.006 # +0.6% activează trailing
TRAIL_PCT_RANGE = 0.005     # 0.5% trailing

TIME_STOP_HOURS = 8
TIME_STOP_MIN_PROFIT = 0.002 # +0.2% minim, altfel iese

# TREND parameters
TRAIL_ACTIVATE_TREND = 0.008  # +0.8% activează trailing
TRAIL_ATR_MULT_TREND = 1.2

STOP_ATR_MULT = 1.2

# Risk control
DAILY_LOSS_LIMIT_PCT = 0.015  # -1.5% pe zi => nu mai intră în trade-uri noi azi

# Files
STATE_FILE = "btc_regime_state.json"
TRADES_LOG = "btc_regime_trades.log"
HEARTBEAT_LOG = "btc_regime_heartbeat.log"

# ===================== UTIL =====================

def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def local_now_str():
    return datetime.now().strftime("%H:%M:%S")

def safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

def write_heartbeat(state, note="alive"):
    # scrie un heartbeat ca să vezi că rulează
    line = f"{utc_now_iso()} {note} holding={state['holding']} usdt={state['usdt']:.2f} qty={state['qty']:.8f} total≈{state['last_total']:.2f}\n"
    with open(HEARTBEAT_LOG, "a", encoding="utf-8") as f:
        f.write(line)

def log_trade(side, price, qty, usdt_after, reason):
    line = f"{utc_now_iso()} {side} price={price:.2f} qty={qty:.8f} usdt={usdt_after:.2f} reason={reason}\n"
    with open(TRADES_LOG, "a", encoding="utf-8") as f:
        f.write(line)

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
            # fill defaults
            s.setdefault("usdt", 10000.0)
            s.setdefault("holding", False)
            s.setdefault("qty", 0.0)
            s.setdefault("entry", 0.0)
            s.setdefault("peak", 0.0)
            s.setdefault("entry_ts", None)
            s.setdefault("last_trade_ts", 0.0)
            s.setdefault("trail_active", False)
            s.setdefault("start_day", datetime.now().strftime("%Y-%m-%d"))
            s.setdefault("start_day_equity", 10000.0)
            s.setdefault("trading_blocked_today", False)
            s.setdefault("last_total", s.get("usdt", 10000.0))
            return s
        except Exception:
            pass
    # default
    return {
        "usdt": 10000.0,
        "holding": False,
        "qty": 0.0,
        "entry": 0.0,
        "peak": 0.0,
        "entry_ts": None,
        "last_trade_ts": 0.0,
        "trail_active": False,
        "start_day": datetime.now().strftime("%Y-%m-%d"),
        "start_day_equity": 10000.0,
        "trading_blocked_today": False,
        "last_total": 10000.0,
    }

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

# ===================== BITGET API (public v2) =====================

def http_get(path, params=None, timeout=10):
    url = BASE_URL + path
    if params:
        url = url + "?" + urlencode(params)
    r = requests.get(url, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP={r.status_code} url={url} body={r.text}")
    data = r.json()
    if data.get("code") != "00000":
        raise RuntimeError(f"API code={data.get('code')} msg={data.get('msg')} url={url}")
    return data.get("data")

def fetch_candles_chunk(symbol, granularity, limit=200, end_ms=None):
    """
    Bitget v2 candles: /api/v2/spot/market/candles
    param: symbol, granularity, limit, endTime (ms) optional
    return: list of [ts, open, high, low, close, baseVol, quoteVol, ...]
    """
    params = {
        "symbol": symbol,
        "granularity": granularity,
        "limit": str(limit),
    }
    if end_ms is not None:
        params["endTime"] = str(int(end_ms))
    return http_get("/api/v2/spot/market/candles", params=params)

def fetch_candles_lookback(symbol, granularity, days, limit_per_req=200):
    need = int(days * 24) + 50  # buffer
    all_rows = []
    end_ms = None
    tries = 0

    while len(all_rows) < need and tries < 50:
        tries += 1
        chunk = fetch_candles_chunk(symbol, granularity, limit=limit_per_req, end_ms=end_ms)
        if not chunk:
            break

        # normalize & add
        for row in chunk:
            # row: [ts, open, high, low, close, ...]
            if len(row) < 5:
                continue
            ts = int(row[0])
            o = safe_float(row[1])
            h = safe_float(row[2])
            l = safe_float(row[3])
            c = safe_float(row[4])
            all_rows.append((ts, o, h, l, c))

        # move end_ms to older
        # find oldest timestamp in chunk
        oldest_ts = min(int(r[0]) for r in chunk if len(r) >= 1)
        end_ms = oldest_ts - 1

        time.sleep(0.15)

    # de-duplicate + sort ascending
    dedup = {}
    for ts, o, h, l, c in all_rows:
        dedup[ts] = (ts, o, h, l, c)
    rows = sorted(dedup.values(), key=lambda x: x[0])

    # keep last 'need'
    if len(rows) > need:
        rows = rows[-need:]
    return rows

# ===================== INDICATORS =====================

def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period

def stddev(values, period):
    if len(values) < period:
        return None
    m = sma(values, period)
    var = sum((x - m) ** 2 for x in values[-period:]) / period
    return math.sqrt(var)

def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e

def rsi(close, period=14):
    if len(close) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    # initial average (Wilder)
    for i in range(-period, 0):
        diff = close[i] - close[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses += -diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def atr(high, low, close, period=14):
    if len(close) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        h = high[i]
        l = low[i]
        prev_c = close[i - 1]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    return sum(trs) / period

def adx(high, low, close, period=14):
    """
    Simplified Wilder ADX over last period (enough for regime filter).
    """
    if len(close) < period + 2:
        return None

    trs = []
    plus_dm = []
    minus_dm = []

    for i in range(-(period + 1), 0):
        h = high[i]
        l = low[i]
        prev_h = high[i - 1]
        prev_l = low[i - 1]
        prev_c = close[i - 1]

        up_move = h - prev_h
        down_move = prev_l - l

        pdm = up_move if (up_move > down_move and up_move > 0) else 0.0
        mdm = down_move if (down_move > up_move and down_move > 0) else 0.0

        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))

        trs.append(tr)
        plus_dm.append(pdm)
        minus_dm.append(mdm)

    tr_sum = sum(trs[-period:])
    pdm_sum = sum(plus_dm[-period:])
    mdm_sum = sum(minus_dm[-period:])

    if tr_sum == 0:
        return 0.0

    plus_di = 100.0 * (pdm_sum / tr_sum)
    minus_di = 100.0 * (mdm_sum / tr_sum)

    denom = plus_di + minus_di
    if denom == 0:
        return 0.0

    dx = 100.0 * abs(plus_di - minus_di) / denom
    # pentru un filtru simplu de regim, dx e suficient ca aproximație ADX.
    # (ADX complet ar fi EMA/Wilder pe dx; aici păstrăm simplu și stabil)
    return dx

# ===================== STRATEGY =====================

def compute_indicators(rows):
    ts = [r[0] for r in rows]
    o = [r[1] for r in rows]
    h = [r[2] for r in rows]
    l = [r[3] for r in rows]
    c = [r[4] for r in rows]

    mid = sma(c, BB_PERIOD)
    sd = stddev(c, BB_PERIOD)
    bb_lower = bb_upper = None
    if mid is not None and sd is not None:
        bb_lower = mid - BB_STD * sd
        bb_upper = mid + BB_STD * sd

    rsi_v = rsi(c, RSI_PERIOD)
    atr_v = atr(h, l, c, ATR_PERIOD)

    ema20 = ema(c[-BB_PERIOD*3:], 20) if len(c) >= 20 else None
    ema50 = ema(c[-200:], 50) if len(c) >= 50 else None
    ema200 = ema(c[-400:], 200) if len(c) >= 200 else None

    adx_v = adx(h, l, c, ADX_PERIOD)

    return {
        "ts": ts,
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "bb_lower": bb_lower,
        "bb_upper": bb_upper,
        "bb_mid": mid,
        "rsi": rsi_v,
        "atr": atr_v,
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "adx": adx_v
    }

def determine_regime(ind):
    a = ind["adx"]
    if a is None:
        return "UNKNOWN"
    if a < ADX_RANGE_MAX:
        return "RANGE"
    if a > ADX_TREND_MIN:
        return "TREND"
    return "NEUTRAL"

def can_trade_now(state):
    now = time.time()
    return (now - state["last_trade_ts"]) >= COOLDOWN_SEC

def update_daily_limits(state, total_equity):
    today = datetime.now().strftime("%Y-%m-%d")
    if state["start_day"] != today:
        # new day reset
        state["start_day"] = today
        state["start_day_equity"] = total_equity
        state["trading_blocked_today"] = False

    # check daily loss
    start_eq = state["start_day_equity"]
    if start_eq > 0:
        pnl_day = total_equity - start_eq
        if pnl_day <= -start_eq * DAILY_LOSS_LIMIT_PCT:
            state["trading_blocked_today"] = True

def paper_buy_allocation(state, price, reason):
    usdt = state["usdt"]
    if usdt <= 1.0:
        return False

    spend = usdt * CAPITAL_FRACTION
    if spend < 10:
        return False

    fee = spend * FEE_RATE
    qty = (spend - fee) / price

    state["usdt"] -= spend
    state["holding"] = True
    state["qty"] = qty
    state["entry"] = price
    state["peak"] = price
    state["trail_active"] = False
    state["entry_ts"] = utc_now_iso()
    state["last_trade_ts"] = time.time()

    log_trade("BUY", price, qty, state["usdt"], reason)
    save_state(state)
    return True

def paper_sell_all(state, price, reason):
    if not state["holding"] or state["qty"] <= 0:
        return False

    qty = state["qty"]
    gross = qty * price
    fee = gross * FEE_RATE
    net = gross - fee

    state["usdt"] += net
    state["holding"] = False
    state["qty"] = 0.0
    state["entry"] = 0.0
    state["peak"] = 0.0
    state["trail_active"] = False
    state["entry_ts"] = None
    state["last_trade_ts"] = time.time()

    log_trade("SELL", price, qty, state["usdt"], reason)
    save_state(state)
    return True

def hours_since_entry(state):
    if not state.get("entry_ts"):
        return 0.0
    try:
        dt = datetime.fromisoformat(state["entry_ts"].replace("Z","+00:00"))
        delta = datetime.now(timezone.utc) - dt
        return delta.total_seconds() / 3600.0
    except Exception:
        return 0.0

def strategy_step(state, ind):
    close = ind["close"][-1]
    bb_l = ind["bb_lower"]
    bb_u = ind["bb_upper"]
    rsi_v = ind["rsi"]
    atr_v = ind["atr"]
    ema20 = ind["ema20"]
    ema50 = ind["ema50"]
    ema200 = ind["ema200"]
    adx_v = ind["adx"]
    regime = determine_regime(ind)

    # compute equity
    total = state["usdt"] + (state["qty"] * close if state["holding"] else 0.0)
    state["last_total"] = float(total)

    # daily risk update
    update_daily_limits(state, total)

    # manage peak
    if state["holding"]:
        if close > state["peak"]:
            state["peak"] = close

    # ---- EXIT MANAGEMENT (always) ----
    if state["holding"] and atr_v is not None:
        entry = state["entry"]
        peak = state["peak"]

        # stop loss (ATR)
        stop_price = entry - STOP_ATR_MULT * atr_v
        if close <= stop_price:
            paper_sell_all(state, close, f"STOP_ATR({STOP_ATR_MULT}*ATR)")
            return regime, close

        # TIME STOP
        hs = hours_since_entry(state)
        if hs >= TIME_STOP_HOURS:
            if close < entry * (1.0 + TIME_STOP_MIN_PROFIT):
                paper_sell_all(state, close, f"TIME_STOP({TIME_STOP_HOURS}h)")
                return regime, close

        # RANGE exits
        if regime == "RANGE":
            # activate trailing
            if not state["trail_active"] and close >= entry * (1.0 + TRAIL_ACTIVATE_RANGE):
                state["trail_active"] = True
                save_state(state)

            # take profit
            if close >= entry * (1.0 + TP_RANGE):
                paper_sell_all(state, close, f"TP_RANGE(+{TP_RANGE*100:.2f}%)")
                return regime, close

            # bb_upper or rsi high
            if bb_u is not None and close >= bb_u:
                paper_sell_all(state, close, "BB_UPPER_EXIT")
                return regime, close

            if rsi_v is not None and rsi_v > 65.0:
                paper_sell_all(state, close, "RSI>65_EXIT")
                return regime, close

            # trailing
            if state["trail_active"]:
                trail_stop = peak * (1.0 - TRAIL_PCT_RANGE)
                if close <= trail_stop:
                    paper_sell_all(state, close, f"TRAIL_RANGE(-{TRAIL_PCT_RANGE*100:.2f}%)")
                    return regime, close

        # TREND exits
        if regime == "TREND":
            # ema cross down
            if ema20 is not None and ema50 is not None and ema20 < ema50:
                paper_sell_all(state, close, "TREND_EMA20<EMA50")
                return regime, close

            # activate trailing trend after +0.8%
            if not state["trail_active"] and close >= entry * (1.0 + TRAIL_ACTIVATE_TREND):
                state["trail_active"] = True
                save_state(state)

            # trailing by ATR distance
            if state["trail_active"] and atr_v is not None:
                trail_stop = peak - TRAIL_ATR_MULT_TREND * atr_v
                if close <= trail_stop:
                    paper_sell_all(state, close, f"TRAIL_TREND({TRAIL_ATR_MULT_TREND}*ATR)")
                    return regime, close

    # ---- ENTRY (only if not holding, not blocked, cooldown ok) ----
    if (not state["holding"]) and (not state["trading_blocked_today"]) and can_trade_now(state):
        # RANGE entry
        if regime == "RANGE":
            if bb_l is not None and rsi_v is not None:
                if close <= bb_l and rsi_v < 35.0:
                    paper_buy_allocation(state, close, "RANGE_ENTRY(BB_lower+RSI<35)")
                    return regime, close

        # TREND entry (trend up + pullback)
        if regime == "TREND":
            if ema50 is not None and ema200 is not None and ema20 is not None and rsi_v is not None:
                uptrend = ema50 > ema200
                pullback = close <= ema20 * 1.002  # aproape de EMA20
                rsi_ok = 40.0 <= rsi_v <= 60.0
                if uptrend and pullback and rsi_ok:
                    paper_buy_allocation(state, close, "TREND_ENTRY(uptrend+pullback)")
                    return regime, close

    save_state(state)
    return regime, close

# ===================== MAIN LOOP =====================

def main():
    print("=== BTC REGIME BOT (SPOT PAPER) | 1h | RANGE+TREND ===")
    print(f"Symbol={SYMBOL} Lookback={LOOKBACK_DAYS}d Granularity={GRANULARITY}")
    print("RANGE: ADX<20 | BUY: close<=BB_lower & RSI<35")
    print("      SELL: TP +0.9% OR BB_upper OR RSI>65 OR trail(0.5% after +0.6%) OR stop(1.2*ATR) OR time-stop(8h)")
    print("TREND: ADX>25 | BUY: EMA50>EMA200 + pullback EMA20 + RSI 40..60")
    print("      SELL: EMA20<EMA50 OR trail(1.2*ATR after +0.8%) OR stop(1.2*ATR) OR time-stop")
    print(f"Fee={FEE_RATE*100:.2f}% Capital/trade={CAPITAL_FRACTION*100:.0f}% DailyLossLimit={DAILY_LOSS_LIMIT_PCT*100:.2f}%")
    print(f"State={STATE_FILE} Trades={TRADES_LOG} Heartbeat={HEARTBEAT_LOG}")
    print()

    state = load_state()
    print(f"Loaded: USDT={state['usdt']:.2f} holding={state['holding']} qty={state['qty']:.8f} entry={state['entry']:.2f}")

    # initial fetch candles
    try:
        print(f"{local_now_str()} fetching initial {LOOKBACK_DAYS}d candles...")
        rows = fetch_candles_lookback(SYMBOL, GRANULARITY, LOOKBACK_DAYS, CANDLE_LIMIT)
        print(f"{local_now_str()} candles loaded: {len(rows)}")
    except Exception as e:
        print(f"{local_now_str()} Eroare initial candles: {e}")
        return

    last_heartbeat = 0

    while True:
        try:
            # refresh candles (fetch more and merge) – simplu: re-fetch lookback la fiecare ciclu
            # (e ok pentru 1h și check 60s; public API suportă)
            rows = fetch_candles_lookback(SYMBOL, GRANULARITY, LOOKBACK_DAYS, CANDLE_LIMIT)
            ind = compute_indicators(rows)

            if len(ind["close"]) < 220 or ind["bb_lower"] is None or ind["atr"] is None or ind["rsi"] is None:
                print(f"{local_now_str()} waiting enough data... candles={len(ind['close'])}")
                time.sleep(CHECK_INTERVAL_SEC)
                continue

            regime, price = strategy_step(state, ind)

            # display status
            bb_l = ind["bb_lower"]; bb_u = ind["bb_upper"]
            rsi_v = ind["rsi"]; atr_v = ind["atr"]; adx_v = ind["adx"]
            ema20 = ind["ema20"]; ema50 = ind["ema50"]; ema200 = ind["ema200"]

            total = state["last_total"]
            pnl = total - state["start_day_equity"] if state["start_day_equity"] else 0.0

            hold_str = "USDT"
            if state["holding"]:
                hold_str = f"BTC qty={state['qty']:.8f} entry={state['entry']:.2f} peak={state['peak']:.2f} trail={state['trail_active']}"

            blocked = "BLOCKED_TODAY" if state["trading_blocked_today"] else "OK"

            print(
                f"{local_now_str()} price={price:.2f} | "
                f"BB[{bb_l:.2f}..{bb_u:.2f}] RSI={rsi_v:.1f} ATR={atr_v:.2f} ADX≈{adx_v:.1f} | {regime} | {blocked}\n"
                f"   Holding={hold_str} | USDT={state['usdt']:.2f} | Total≈{total:.2f} | DayPnL≈{pnl:+.2f}\n"
            )

            # heartbeat every 60s
            now = time.time()
            if now - last_heartbeat >= 60:
                write_heartbeat(state, note="alive")
                last_heartbeat = now

        except Exception as e:
            # log error & heartbeat
            print(f"{local_now_str()} Eroare: {e}")
            try:
                write_heartbeat(state, note=f"error={str(e)[:120]}")
            except Exception:
                pass

        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()

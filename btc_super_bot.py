# btc_super_bot.py
# === BTC SUPER BOT (SPOT | PAPER) ===
# AUTO Regime: TREND vs RANGE
# Filters: ATR% volatility filter + anti-spike filter
# Entries: TREND pullback / RANGE mean-reversion
# Position mgmt: DCA 3 tranches + avg entry
# Exits: fee+net-profit guard + trailing + stoploss + flat-exit
# Risk: daily loss guard (pause until next day)
#
# Bitget data: /api/v2/spot/market/candles

import json
import time
import math
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, date
from pathlib import Path
from typing import List, Optional, Tuple
import urllib.request


# -------------------------
# CONFIG (tune here)
# -------------------------
SYMBOL = "BTCUSDT"

CHECK_SECONDS = 5

# Candles
GRAN_FAST = "1min"     # for micro momentum + spike
GRAN_SLOW = "15min"    # for regime + ATR/Bollinger
LIMIT_FAST = 240       # 4h of 1m
LIMIT_SLOW = 300       # ~3 days of 15m

# Fees
FEE_RATE = 0.001       # 0.10% each side

# Capital (paper)
START_USDT = 10000.0
TOTAL_RISK_FRACTION = 0.35   # max fraction of USDT committed per cycle (sum of DCA tranches)

# DCA tranches (sum = 1.0)
DCA_WEIGHTS = [0.50, 0.30, 0.20]
# When to add tranche 2/3 (drawdown from avg entry)
DCA_DD_LEVELS = [0.0, 0.006, 0.012]  # 0%, -0.6%, -1.2%

# Profit requirements
MIN_NET_PROFIT = 0.006      # +0.6% net minimum before SELL on signals/flat/trail (covers fee + profit)
TRAIL_ACTIVATE_NET = 0.006  # activate trailing after net >= 0.6%
TRAIL_DROP = 0.004          # 0.4% drop from peak triggers trailing sell (after activation)

# Protection
STOPLOSS_PCT = 0.018        # -1.8% from avg entry (hard protection)
COOLDOWN_SECONDS = 60       # after SELL, wait before re-enter

# Flat exit
FLAT_WINDOW_SEC = 180       # 3 min
FLAT_BAND_PCT = 0.0012      # +/-0.12%

# Regime detection (15m)
EMA_FAST = 20
EMA_SLOW = 50
TREND_MIN_DIST = 0.0015     # EMA20 above EMA50 by at least 0.15% -> trend bias
TREND_MIN_SLOPE = 0.0010    # EMA20 slope over window >= 0.10% -> trending

# Volatility filter (ATR% on 15m)
ATR_PERIOD = 14
ATR_MIN_PCT = 0.0025        # 0.25% (prea mic => mort)
ATR_MAX_PCT = 0.0200        # 2.0%  (prea mare => haos)

# Anti-spike filter (1m)
SPIKE_MULT_ATR = 2.5        # last candle range > 2.5 * ATR(1m) => no buy

# Entry rules
# TREND entry:
TREND_RSI_MAX = 72.0
TREND_RSI_MIN = 40.0
MICRO_SLOPE_POINTS = 20
MICRO_SLOPE_MIN_PCT = 0.0012  # +0.12% over last 20m
BUY_CONFIRM = 3

# RANGE entry:
BB_PERIOD = 20
BB_STD = 2.0
RANGE_RSI_MAX = 35.0

# Daily loss guard
DAILY_LOSS_LIMIT = 0.03     # -3% in a day -> pause until next day

# Files
STATE_FILE = Path("btc_super_state.json")
TRADES_LOG = Path("btc_super_trades.log")
HEARTBEAT_LOG = Path("btc_super_heartbeat.log")


# -------------------------
# Helpers
# -------------------------
def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def append_log(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")

def safe_write(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")

def http_get_json(url: str, timeout: int = 10) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

BASE = "https://api.bitget.com"

def fetch_candles(symbol: str, granularity: str, limit: int) -> List[List[str]]:
    url = f"{BASE}/api/v2/spot/market/candles?symbol={symbol}&granularity={granularity}&limit={limit}"
    j = http_get_json(url)
    if j.get("code") != "00000" or not j.get("data"):
        raise RuntimeError(f"Candles API error: {j}")
    return j["data"]

def last_close_from(candles: List[List[str]]) -> float:
    # candles are usually newest-first -> candles[0] close
    return float(candles[0][4])

def to_chrono(candles: List[List[str]]) -> List[List[str]]:
    # oldest -> newest
    return list(reversed(candles))


# -------------------------
# Indicators
# -------------------------
def ema(values: List[float], period: int) -> Optional[List[float]]:
    if len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    out = []
    e = sum(values[:period]) / period
    out.extend([None] * (period - 1))
    out.append(e)
    for v in values[period:]:
        e = v * k + e * (1.0 - k)
        out.append(e)
    return out

def rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses += -d
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))

def true_range(h: float, l: float, prev_c: float) -> float:
    return max(h - l, abs(h - prev_c), abs(l - prev_c))

def atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(true_range(highs[i], lows[i], closes[i - 1]))
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period

def bollinger(closes: List[float], period: int = 20, std_mult: float = 2.0) -> Optional[Tuple[float, float, float]]:
    if len(closes) < period:
        return None
    window = closes[-period:]
    m = sum(window) / period
    var = sum((x - m) ** 2 for x in window) / period
    sd = math.sqrt(var)
    upper = m + std_mult * sd
    lower = m - std_mult * sd
    return lower, m, upper

def linreg_slope_pct(values: List[float]) -> Optional[float]:
    n = len(values)
    if n < 5:
        return None
    xs = list(range(n))
    mx = (n - 1) / 2.0
    my = sum(values) / n
    if my == 0:
        return None
    num = 0.0
    den = 0.0
    for i, y in enumerate(values):
        dx = xs[i] - mx
        num += dx * (y - my)
        den += dx * dx
    if den == 0:
        return None
    slope = num / den
    pct = (slope * (n - 1)) / my
    return pct


# -------------------------
# Trading math
# -------------------------
def req_exit_price(avg_entry: float, fee: float, net_profit: float) -> float:
    return avg_entry * (1.0 + 2.0 * fee + net_profit)

def qty_from_usdt(usdt: float, price: float, fee: float) -> float:
    return (usdt * (1.0 - fee)) / price

def usdt_from_qty(qty: float, price: float, fee: float) -> float:
    return (qty * price) * (1.0 - fee)

def total_value(usdt: float, qty: float, price: float) -> float:
    return usdt + qty * price


# -------------------------
# State
# -------------------------
@dataclass
class State:
    created: str
    usdt: float
    holding: bool
    qty: float
    avg_entry: float
    peak: float
    buy_streak: int
    last_trade_ts: float

    # DCA progress
    tranche_index: int  # 0..len(DCA_WEIGHTS)-1, next tranche to add
    committed_usdt: float  # sum invested this cycle

    # flat tracking
    last_price_for_flat: float
    last_flat_ts: float

    # daily guard
    day: str
    day_start_equity: float
    paused_today: bool

def default_state() -> State:
    today = date.today().isoformat()
    return State(
        created=utc_iso(),
        usdt=START_USDT,
        holding=False,
        qty=0.0,
        avg_entry=0.0,
        peak=0.0,
        buy_streak=0,
        last_trade_ts=0.0,
        tranche_index=0,
        committed_usdt=0.0,
        last_price_for_flat=0.0,
        last_flat_ts=time.time(),
        day=today,
        day_start_equity=START_USDT,
        paused_today=False
    )

def load_state() -> State:
    if not STATE_FILE.exists():
        st = default_state()
        save_state(st)
        return st
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return State(**data)
    except Exception:
        st = default_state()
        save_state(st)
        return st

def save_state(st: State) -> None:
    safe_write(STATE_FILE, asdict(st))


# -------------------------
# Regime + Filters
# -------------------------
def detect_regime_and_filters(c_slow: List[List[str]], c_fast: List[List[str]]) -> Tuple[str, dict]:
    """
    Returns:
      regime: "TREND" or "RANGE"
      info dict: atr_pct, ema20, ema50, trend_dist, trend_slope, bb_lower/upper, spike_ok, vol_ok, etc.
    """
    info = {}

    slow = to_chrono(c_slow)
    fast = to_chrono(c_fast)

    closes_s = [float(x[4]) for x in slow]
    highs_s = [float(x[2]) for x in slow]
    lows_s = [float(x[3]) for x in slow]

    closes_f = [float(x[4]) for x in fast]
    highs_f = [float(x[2]) for x in fast]
    lows_f = [float(x[3]) for x in fast]

    price = closes_f[-1]
    info["price"] = price

    e20 = ema(closes_s, EMA_FAST)
    e50 = ema(closes_s, EMA_SLOW)
    ema20 = e20[-1] if e20 and e20[-1] is not None else None
    ema50 = e50[-1] if e50 and e50[-1] is not None else None
    info["ema20"] = ema20
    info["ema50"] = ema50

    # trend distance
    trend_dist = None
    if ema20 and ema50 and ema50 != 0:
        trend_dist = (ema20 - ema50) / ema50
    info["trend_dist"] = trend_dist

    # trend slope (ema20 over last 10 points of 15m ~ 2.5h)
    trend_slope = None
    if e20 and len(e20) >= 60:
        tail = [x for x in e20[-30:] if x is not None]
        if len(tail) >= 10:
            trend_slope = linreg_slope_pct(tail[-10:])
    info["trend_slope"] = trend_slope

    # ATR% filter on 15m
    a = atr(highs_s, lows_s, closes_s, ATR_PERIOD)
    atr_pct = (a / price) if a else None
    info["atr_pct"] = atr_pct
    vol_ok = (atr_pct is not None and ATR_MIN_PCT <= atr_pct <= ATR_MAX_PCT)
    info["vol_ok"] = vol_ok

    # Bollinger on 15m (for range buys)
    bb = bollinger(closes_s, BB_PERIOD, BB_STD)
    if bb:
        bb_lower, bb_mid, bb_upper = bb
        info["bb_lower"] = bb_lower
        info["bb_upper"] = bb_upper
        info["bb_mid"] = bb_mid

    # anti-spike on 1m using ATR(1m)
    a1 = atr(highs_f, lows_f, closes_f, 14)
    spike_ok = True
    if a1:
        last_high = highs_f[-1]
        last_low = lows_f[-1]
        last_range = last_high - last_low
        spike_ok = (last_range <= SPIKE_MULT_ATR * a1)
    info["spike_ok"] = spike_ok

    # regime decision
    trending = False
    if trend_dist is not None and trend_slope is not None:
        trending = (trend_dist >= TREND_MIN_DIST and trend_slope >= TREND_MIN_SLOPE)
    regime = "TREND" if trending else "RANGE"
    info["regime"] = regime

    # RSI slow
    info["rsi_15m"] = rsi(closes_s, 14)
    info["rsi_1m"] = rsi(closes_f, 14)

    # micro momentum on 1m
    lastN = closes_f[-MICRO_SLOPE_POINTS:] if len(closes_f) >= MICRO_SLOPE_POINTS else closes_f
    info["micro_slope_pct"] = linreg_slope_pct(lastN)

    return regime, info


# -------------------------
# Signals
# -------------------------
def buy_signal(regime: str, info: dict) -> bool:
    if not info.get("vol_ok", False):
        return False
    if not info.get("spike_ok", True):
        return False

    price = info["price"]
    r15 = info.get("rsi_15m")
    r1 = info.get("rsi_1m")
    micro = info.get("micro_slope_pct")

    if regime == "TREND":
        ema20 = info.get("ema20")
        ema50 = info.get("ema50")
        if ema20 is None or ema50 is None:
            return False
        # uptrend + pullback-ish: price >= ema20 and ema20 >= ema50
        if not (ema20 >= ema50 and price >= ema20):
            return False
        # rsi not overheated
        if r15 is not None and not (TREND_RSI_MIN <= r15 <= TREND_RSI_MAX):
            return False
        if r1 is not None and r1 > 75:
            return False
        # micro momentum must be positive
        if micro is None or micro < MICRO_SLOPE_MIN_PCT:
            return False
        return True

    # RANGE
    bb_lower = info.get("bb_lower")
    if bb_lower is None:
        return False
    # mean reversion: price near/below lower band + low RSI
    if price > bb_lower * 1.002:  # allow slight above
        return False
    if r15 is None or r15 > RANGE_RSI_MAX:
        return False
    # optional: micro slope should not be strongly negative (avoid catching knife)
    if micro is not None and micro < -0.002:
        return False
    return True


def sell_signal(regime: str, info: dict, st: State) -> Tuple[bool, str]:
    """
    Returns sell_sig, reason (not checking fee+profit guard here).
    """
    price = info["price"]
    micro = info.get("micro_slope_pct")
    r15 = info.get("rsi_15m")

    # If TREND: exit if micro turns negative significantly or RSI too high and stalls
    if regime == "TREND":
        if micro is not None and micro < -0.0012:
            return True, "MOM_TURN"
        if r15 is not None and r15 > 78:
            return True, "RSI_TOO_HIGH"
        return False, ""

    # RANGE: exit if price reaches mid/upper band or RSI normalized
    bb_mid = info.get("bb_mid")
    bb_upper = info.get("bb_upper")
    if bb_upper and price >= bb_upper * 0.998:
        return True, "BB_UPPER"
    if bb_mid and price >= bb_mid:
        return True, "BB_MID"
    if r15 is not None and r15 >= 55:
        return True, "RSI_BACK"
    return False, ""


# -------------------------
# Bot loop
# -------------------------
def reset_daily_guard_if_needed(st: State, equity_now: float) -> None:
    today = date.today().isoformat()
    if st.day != today:
        st.day = today
        st.day_start_equity = equity_now
        st.paused_today = False

def should_pause_today(st: State, equity_now: float) -> bool:
    if st.paused_today:
        return True
    dd = (equity_now / st.day_start_equity) - 1.0
    if dd <= -DAILY_LOSS_LIMIT:
        st.paused_today = True
        append_log(TRADES_LOG, f"{utc_iso()} DAILY_GUARD PAUSE dd={dd*100:.2f}% equity={equity_now:.2f}")
        return True
    return False


def main():
    print("=== BTC SUPER BOT (AUTO TREND/RANGE) | SPOT PAPER ===")
    print(f"Symbol={SYMBOL} | check={CHECK_SECONDS}s | fees={FEE_RATE*100:.2f}% each side")
    print(f"Risk: total_fraction={TOTAL_RISK_FRACTION*100:.0f}% | DCA weights={DCA_WEIGHTS} | dd_levels={DCA_DD_LEVELS}")
    print(f"Profit guard: MIN_NET={MIN_NET_PROFIT*100:.2f}% | trail_act={TRAIL_ACTIVATE_NET*100:.2f}% | trail_drop={TRAIL_DROP*100:.2f}%")
    print(f"Vol filter ATR%: [{ATR_MIN_PCT*100:.2f}% .. {ATR_MAX_PCT*100:.2f}%] | anti-spike: {SPIKE_MULT_ATR}x ATR(1m)")
    print(f"Daily loss guard: {DAILY_LOSS_LIMIT*100:.1f}%")
    print(f"State={STATE_FILE} Trades={TRADES_LOG} Heartbeat={HEARTBEAT_LOG}")
    print()

    st = load_state()
    print(f"Loaded: USDT={st.usdt:.2f} holding={st.holding} qty={st.qty:.8f} avg_entry={st.avg_entry:.2f} tranche={st.tranche_index}")

    last_hb = 0.0

    while True:
        try:
            now = time.time()

            c_fast = fetch_candles(SYMBOL, GRAN_FAST, LIMIT_FAST)
            c_slow = fetch_candles(SYMBOL, GRAN_SLOW, LIMIT_SLOW)

            price = last_close_from(c_fast)

            equity = total_value(st.usdt, st.qty, price)
            reset_daily_guard_if_needed(st, equity)

            regime, info = detect_regime_and_filters(c_slow, c_fast)
            info["price"] = price

            # flat detection
            if st.last_price_for_flat == 0.0:
                st.last_price_for_flat = price
                st.last_flat_ts = now
            move = abs(price - st.last_price_for_flat) / st.last_price_for_flat if st.last_price_for_flat else 0.0
            if move > FLAT_BAND_PCT:
                st.last_price_for_flat = price
                st.last_flat_ts = now
            is_flat = (now - st.last_flat_ts) >= FLAT_WINDOW_SEC

            in_cooldown = (now - st.last_trade_ts) < COOLDOWN_SECONDS

            # daily guard
            paused = should_pause_today(st, equity)

            ts = datetime.now().strftime("%H:%M:%S")
            atr_pct = info.get("atr_pct")
            atr_str = "n/a" if atr_pct is None else f"{atr_pct*100:.2f}%"
            micro = info.get("micro_slope_pct")
            micro_str = "n/a" if micro is None else f"{micro*100:+.2f}%/{MICRO_SLOPE_POINTS}m"
            r15 = info.get("rsi_15m")
            r15_str = "n/a" if r15 is None else f"{r15:.1f}"
            vol_ok = info.get("vol_ok", False)
            spike_ok = info.get("spike_ok", True)

            if not st.holding:
                sig = buy_signal(regime, info) and (not in_cooldown) and (not paused)
                st.buy_streak = st.buy_streak + 1 if sig else 0
                buy_ok = st.buy_streak >= BUY_CONFIRM

                equity = total_value(st.usdt, st.qty, price)
                print(f"{ts} price={price:.2f} | REGIME={regime} ATR={atr_str} vol_ok={'YES' if vol_ok else 'no'} spike_ok={'YES' if spike_ok else 'no'} "
                      f"micro={micro_str} RSI15={r15_str} flat={'YES' if is_flat else 'no'} paused={'YES' if paused else 'no'}")
                print(f"   Holding=USDT usdt={st.usdt:.2f} total≈{equity:.2f} BUYsig={'YES' if sig else 'no'} streak={st.buy_streak}/{BUY_CONFIRM} cooldown={'YES' if in_cooldown else 'no'}")

                if buy_ok:
                    # allocate first tranche
                    total_alloc = st.usdt * TOTAL_RISK_FRACTION
                    tranche_usdt = total_alloc * DCA_WEIGHTS[0]

                    if tranche_usdt > 10:
                        qty = qty_from_usdt(tranche_usdt, price, FEE_RATE)
                        st.usdt -= tranche_usdt
                        st.qty += qty
                        st.holding = True
                        st.avg_entry = price  # first entry
                        st.peak = price
                        st.tranche_index = 1
                        st.committed_usdt = tranche_usdt
                        st.buy_streak = 0
                        st.last_trade_ts = now
                        st.last_price_for_flat = price
                        st.last_flat_ts = now

                        append_log(TRADES_LOG, f"{utc_iso()} BUY_T1 price={price:.2f} usdt={tranche_usdt:.2f} qty={qty:.8f} regime={regime}")
                        print(f"   ✅ BUY T1 at {price:.2f} usdt={tranche_usdt:.2f} qty={qty:.8f} (regime={regime})")

            else:
                # update peak
                if price > st.peak:
                    st.peak = price

                # compute pnl
                pnl_gross = (price / st.avg_entry) - 1.0
                pnl_net = pnl_gross - 2.0 * FEE_RATE

                # minimum exit price
                req_exit = req_exit_price(st.avg_entry, FEE_RATE, MIN_NET_PROFIT)
                sell_ok = price >= req_exit

                # stoploss
                stop_price = st.avg_entry * (1.0 - STOPLOSS_PCT)
                stop_hit = price <= stop_price

                # trailing
                trailing_active = pnl_net >= TRAIL_ACTIVATE_NET
                trail_stop = st.peak * (1.0 - TRAIL_DROP)
                trailing_hit = trailing_active and (price <= trail_stop)

                # add DCA if needed (and not paused)
                dd_from_avg = (st.avg_entry - price) / st.avg_entry if st.avg_entry else 0.0
                can_dca = (not paused) and (st.tranche_index < len(DCA_WEIGHTS))
                need_dca = False
                if can_dca:
                    level = DCA_DD_LEVELS[st.tranche_index]
                    need_dca = dd_from_avg >= level

                equity = total_value(st.usdt, st.qty, price)

                # base sell signal (regime-aware)
                base_sell, reason = sell_signal(regime, info, st)

                # flat-exit only if sell_ok (profit guard)
                flat_exit = is_flat and sell_ok

                print(f"{ts} price={price:.2f} | REGIME={regime} ATR={atr_str} micro={micro_str} RSI15={r15_str} flat={'YES' if is_flat else 'no'} paused={'YES' if paused else 'no'}")
                print(f"   Holding=BTC qty={st.qty:.8f} avg_entry={st.avg_entry:.2f} peak={st.peak:.2f} pnl_net≈{pnl_net*100:+.2f}% total≈{equity:.2f}")
                print(f"   req_exit≈{req_exit:.2f} sell_ok={'YES' if sell_ok else 'no'} trail_act={'YES' if trailing_active else 'no'} trail_stop≈{trail_stop:.2f}")

                # 1) STOPLOSS (hard protection)
                if stop_hit:
                    usdt_got = usdt_from_qty(st.qty, price, FEE_RATE)
                    st.usdt += usdt_got
                    append_log(TRADES_LOG, f"{utc_iso()} SELL_STOP price={price:.2f} qty={st.qty:.8f} usdt_got={usdt_got:.2f}")
                    print(f"   ⚠️ SELL STOPLOSS at {price:.2f} (protectie)")

                    # reset position
                    st.qty = 0.0
                    st.holding = False
                    st.avg_entry = 0.0
                    st.peak = 0.0
                    st.tranche_index = 0
                    st.committed_usdt = 0.0
                    st.last_trade_ts = now
                    st.last_price_for_flat = price
                    st.last_flat_ts = now

                else:
                    # 2) DCA (if needed)
                    if need_dca:
                        total_alloc = st.usdt * TOTAL_RISK_FRACTION
                        tranche_usdt = total_alloc * DCA_WEIGHTS[st.tranche_index]
                        if tranche_usdt > 10 and st.usdt >= tranche_usdt:
                            qty_add = qty_from_usdt(tranche_usdt, price, FEE_RATE)

                            # update avg entry by weighted cost
                            # approximate cost = tranche_usdt (already fee-adjusted in qty)
                            prev_cost = st.avg_entry * st.qty
                            new_cost = prev_cost + (price * qty_add)
                            st.qty += qty_add
                            st.avg_entry = new_cost / st.qty if st.qty > 0 else st.avg_entry

                            st.usdt -= tranche_usdt
                            st.committed_usdt += tranche_usdt
                            st.tranche_index += 1
                            st.last_trade_ts = now
                            st.last_price_for_flat = price
                            st.last_flat_ts = now

                            append_log(TRADES_LOG, f"{utc_iso()} BUY_DCA_T{st.tranche_index} price={price:.2f} usdt={tranche_usdt:.2f} qty_add={qty_add:.8f}")
                            print(f"   ✅ DCA BUY T{st.tranche_index} at {price:.2f} usdt={tranche_usdt:.2f} qty_add={qty_add:.8f} new_avg={st.avg_entry:.2f}")

                    # 3) TRAILING SELL (only if profit-guard OK)
                    if trailing_hit and sell_ok:
                        usdt_got = usdt_from_qty(st.qty, price, FEE_RATE)
                        st.usdt += usdt_got
                        append_log(TRADES_LOG, f"{utc_iso()} SELL_TRAIL price={price:.2f} qty={st.qty:.8f} usdt_got={usdt_got:.2f} peak={st.peak:.2f}")
                        print(f"   ✅ SELL TRAIL at {price:.2f} (peak={st.peak:.2f}, trail_stop≈{trail_stop:.2f})")

                        st.qty = 0.0
                        st.holding = False
                        st.avg_entry = 0.0
                        st.peak = 0.0
                        st.tranche_index = 0
                        st.committed_usdt = 0.0
                        st.last_trade_ts = now
                        st.last_price_for_flat = price
                        st.last_flat_ts = now

                    # 4) BASE SELL / FLAT EXIT (only if profit guard OK)
                    elif (flat_exit or base_sell) and sell_ok:
                        tag = "SELL_FLAT" if flat_exit else f"SELL_{reason}"
                        usdt_got = usdt_from_qty(st.qty, price, FEE_RATE)
                        st.usdt += usdt_got
                        append_log(TRADES_LOG, f"{utc_iso()} {tag} price={price:.2f} qty={st.qty:.8f} usdt_got={usdt_got:.2f} regime={regime}")
                        print(f"   ✅ SELL at {price:.2f} ({tag})")

                        st.qty = 0.0
                        st.holding = False
                        st.avg_entry = 0.0
                        st.peak = 0.0
                        st.tranche_index = 0
                        st.committed_usdt = 0.0
                        st.last_trade_ts = now
                        st.last_price_for_flat = price
                        st.last_flat_ts = now

                    elif (flat_exit or base_sell) and not sell_ok:
                        print("   ⏳ SELL signal, dar NU acopera fee+profit minim. Aștept…")

            # heartbeat
            if (time.time() - last_hb) >= 30:
                equity = total_value(st.usdt, st.qty, price)
                append_log(HEARTBEAT_LOG, f"{utc_iso()} alive holding={st.holding} usdt={st.usdt:.2f} qty={st.qty:.8f} equity≈{equity:.2f} paused={st.paused_today}")
                last_hb = time.time()

            save_state(st)
            time.sleep(CHECK_SECONDS)

        except KeyboardInterrupt:
            print("\nStop by user.")
            save_state(st)
            break
        except Exception as e:
            print(f"{datetime.now().strftime('%H:%M:%S')} Eroare: {e}")
            time.sleep(3)


if __name__ == "__main__":
    main()

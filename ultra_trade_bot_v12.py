# ultra_trade_bot_v12.py
# Coords hardcoded (din mesajul tău), OBI din două chenare (roșu sus, verde jos),
# OCR pe procent și pe cutia de disponibil, TP/SL/trailing, + închidere notificare.
# Rulare: python ultra_trade_bot_v12.py

import time, re, sys, os
from collections import deque
import numpy as np
import cv2
import pyautogui
import pytesseract

# ================== COORDONATE (ABSOLUTE, din ecranul tău) ==================
# Procent variație mic (%)
PROCENTAJ_REGION = (350, 307, 40, 18)

# Order book împărțit: sus roșu / jos verde
OBI_RED   = (1323, 325, 289, 242)   # SELL (roșu, sus)
OBI_GREEN = (1325, 590, 286, 238)   # BUY  (verde, jos)

# Avail (USDT la BUY; COIN la SELL)
AVAIL_BOX = (1805, 395, 91, 23)

# Butoane
BTN_BUY     = (1695, 215)
BTN_SELL    = (1844, 220)
BTN_100     = (1901, 379)
BTN_CONFIRM = (1780, 476)

# X pentru notificarea care blochează
NOTIF_X     = (1901, 195)   # dacă nu ai pop-up, se ignoră

# ================== MOD OPERARE ==================
DRY_RUN    = False            # True = NU face click-uri (doar log)
COUNTDOWN  = 3
BASE_FPS   = 8.0              # ~0.125s/loop
pyautogui.FAILSAFE = True
pyautogui.PAUSE    = 0.03

# ================== TAXE & PRAGURI ==================
# Taker ~0.10%/sens; total dus-întors ~0.20%. Buffer mic agresiv.
FEE_BPS_TAKER   = 10.0
TOTAL_FEES_PCT  = (2 * FEE_BPS_TAKER) / 100.0  # ~0.20%
EDGE_BUFFER_PCT = 0.01
ENTRY_EDGE_MIN  = TOTAL_FEES_PCT + EDGE_BUFFER_PCT  # ~0.21%

# ================== STRATEGIE ==================
MA_WINDOW        = 2
DELTA_STEP_MIN   = 0.004
RISE_STREAK_L    = 1
FALL_STREAK_L    = 1

USE_OBI_FILTER   = True
OBI_GATE_LONG    = 0.08       # agresiv (dacă ai prea multe false-positives, urcă la 0.10-0.12)
CHOP_ABS_PCT_MIN = 0.02       # ignoră micro-zgomotul sub ±0.02%

# TP/SL/Trailing (scalp)
TP_PCT_L   = 0.30             # +0.30% profit estimat (din % mic OCR)
SL_PCT_L   = 0.15             # -0.15% stop (strâns)
USE_TRAIL  = True
TRAIL_ARM  = 0.20             # când profitul depășește +0.20%, armează trailing
TRAIL_GIV  = 0.12             # dacă dă înapoi 0.12%, ieșim

# Money management / confirmări (OCR)
MIN_USDT_FOR_BUY        = 5.0
MIN_COIN_FOR_SELL       = 0.00020
MIN_USDT_DELTA_CONFIRM  = 0.60
MIN_COIN_DELTA_CONFIRM  = 0.00012

# Anti-overtrading
TRADE_COOLDOWN_SEC = 6.0
MAX_TRADES_PER_MIN = 12
_last_trade_ts     = 0.0
_trade_times       = deque(maxlen=30)

# Throttle click după confirmare
MIN_SECONDS_BETWEEN_ORDERS = 1.0
_last_order_ts = 0.0

# Double-click opțional
DOUBLE_BUY     = False
DOUBLE_CONFIRM = False

# ================== OCR & GRAB ==================
_pct_re   = re.compile(r'([+\-]?\d[\d,]*\.?\d*)\s*%')
_float_re = re.compile(r'([0-9][0-9,\.]*)')

def grab(region):
    x, y, w, h = region
    img = pyautogui.screenshot(region=(x, y, w, h))
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)

def read_percent(region):
    """OCR pe procentaj mic, returnează % ca float (ex: +0.08 => 0.08)."""
    img = grab(region)
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, None, fx=3.0, fy=3.0, interpolation=cv2.INTER_CUBIC)
    _, th = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    txt = pytesseract.image_to_string(th, config='--oem 1 --psm 7 -c tessedit_char_whitelist=0123456789.+-%')
    m = _pct_re.search(txt)
    if not m:
        return 0.0
    try:
        return float(m.group(1).replace(',', ''))
    except:
        return 0.0

def ocr_number_region(region):
    """OCR pe cifre în AVAIL_BOX (USDT/COIN). Ignoră literele (USDT/COIN)."""
    img = grab(region)
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, None, fx=2.5, fy=2.5, interpolation=cv2.INTER_CUBIC)
    _, th = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    txt = pytesseract.image_to_string(th, config='--oem 1 --psm 7 -c tessedit_char_whitelist=0123456789.,')
    txt = txt.replace(' ', '')
    m = _float_re.search(txt)
    if not m:
        return 0.0
    try:
        return float(m.group(1).replace(',', ''))
    except:
        return 0.0

def obi_from_split_regions():
    """OBI din două chenare: sum(verde jos) vs sum(roșu sus) → [-1,+1]."""
    img_r = pyautogui.screenshot(region=OBI_RED)
    img_g = pyautogui.screenshot(region=OBI_GREEN)
    arr_r = np.array(img_r)
    arr_g = np.array(img_g)
    R_sum = int(arr_r[:, :, 0].sum())
    G_sum = int(arr_g[:, :, 1].sum())
    den = R_sum + G_sum
    if den < 10:
        return 0.0
    return (G_sum - R_sum) / float(den)

# ================== UTILITARE ==================
def moving_avg_push(deq, val, window):
    deq.append(val)
    while len(deq) > window:
        deq.popleft()
    return sum(deq) / len(deq)

def trade_cooldown_ok():
    now = time.time()
    if (now - _last_trade_ts) < TRADE_COOLDOWN_SEC:
        return False
    # curățăm >60s
    while _trade_times and now - _trade_times[0] > 60.0:
        _trade_times.popleft()
    return len(_trade_times) < MAX_TRADES_PER_MIN

def mark_trade():
    global _last_trade_ts
    _last_trade_ts = time.time()
    _trade_times.append(_last_trade_ts)

def safe_click(x, y, label="", throttle=True, clicks=1):
    global _last_order_ts
    if not DRY_RUN:
        if throttle and (time.time() - _last_order_ts) < MIN_SECONDS_BETWEEN_ORDERS:
            print(f"\n[THROTTLE] {label} sărit")
            return False
        pyautogui.moveTo(x, y, duration=0.10)
        for _ in range(max(1, clicks)):
            pyautogui.click(x, y); time.sleep(0.05)
        if throttle:
            _last_order_ts = time.time()
    print(f"\n[CLICK] {label} @ ({x},{y}) {'[DRY]' if DRY_RUN else ''}")
    return True

def maybe_close_notification():
    # încearcă să apese pe X de 2 ori, ignoră dacă nu e nimic
    time.sleep(0.20)
    pyautogui.moveTo(*NOTIF_X, duration=0.10)
    for _ in range(2):
        pyautogui.click(); time.sleep(0.07)
    time.sleep(0.12)

def place_entry_long_seq():
    safe_click(*BTN_BUY,     label="BUY",     throttle=False, clicks=(2 if DOUBLE_BUY else 1))
    time.sleep(0.12)
    safe_click(*BTN_100,     label="100%",    throttle=False)
    time.sleep(0.12)
    safe_click(*BTN_CONFIRM, label="CONFIRM", throttle=True,  clicks=(2 if DOUBLE_CONFIRM else 1))
    maybe_close_notification()

def place_exit_long_seq():
    safe_click(*BTN_SELL,    label="SELL",    throttle=False)
    time.sleep(0.12)
    safe_click(*BTN_100,     label="100%",    throttle=False)
    time.sleep(0.12)
    safe_click(*BTN_CONFIRM, label="CONFIRM", throttle=True,  clicks=(2 if DOUBLE_CONFIRM else 1))
    maybe_close_notification()

def read_usdt_on_buy_tab():
    pyautogui.moveTo(*BTN_BUY, duration=0.10); pyautogui.click(); time.sleep(0.10)
    return ocr_number_region(AVAIL_BOX)

def read_coin_on_sell_tab():
    pyautogui.moveTo(*BTN_SELL, duration=0.10); pyautogui.click(); time.sleep(0.10)
    return ocr_number_region(AVAIL_BOX)

def burst_read(samples=3, gap=0.05):
    """Mică rafală pentru confirmare intrare: medie pe % și OBI."""
    pct_vals, obi_vals = [], []
    for _ in range(samples):
        pct_vals.append(read_percent(PROCENTAJ_REGION))
        obi_vals.append(obi_from_split_regions())
        time.sleep(gap)
    return (sum(pct_vals)/len(pct_vals), sum(obi_vals)/len(obi_vals))

# ================== BOT ==================
class Bot:
    def __init__(self):
        self._pct_ma_deq = deque(maxlen=MA_WINDOW)
        self.prev_pct = None
        self.up_streak = 0
        self.down_streak = 0

        self.pos = 0              # 0=flat, +1=long
        self.entry_pct = None
        self.best_run = 0.0
        self.trail_armed = False

    def should_enter_long(self):
        pct_now, obi_now = burst_read(samples=3, gap=0.05)

        if USE_OBI_FILTER and (obi_now < OBI_GATE_LONG):
            print(f"\n[NO-ENTER] OBI prea mic {obi_now:+.3f} < {OBI_GATE_LONG:.3f}")
            return False

        edge_ok = (pct_now >= ENTRY_EDGE_MIN) and (abs(pct_now) >= CHOP_ABS_PCT_MIN)
        if not edge_ok:
            print(f"\n[NO-ENTER] edge slab {pct_now:.3f}% < {ENTRY_EDGE_MIN:.3f}%")
            return False

        print(f"\n[ENTER?] edge={pct_now:.3f}% | OBI={obi_now:+.3f}")
        return True

    def try_enter_long(self):
        if self.pos != 0:
            return
        if self.up_streak < RISE_STREAK_L or not trade_cooldown_ok():
            return
        if not self.should_enter_long():
            return

        usdt_before = read_usdt_on_buy_tab()
        if usdt_before < MIN_USDT_FOR_BUY:
            print(f"\n[BLOCK BUY] USDT insuficient ({usdt_before:.4f} < {MIN_USDT_FOR_BUY:.2f})")
            return

        cur_pct = read_percent(PROCENTAJ_REGION)
        place_entry_long_seq()
        time.sleep(0.35)

        coin_after = read_coin_on_sell_tab()
        usdt_after = read_usdt_on_buy_tab()

        ok = (coin_after >= MIN_COIN_DELTA_CONFIRM) or ((usdt_before - usdt_after) >= MIN_USDT_DELTA_CONFIRM)
        if not ok:
            print("[ORDER WARN] BUY posibil nereușit → încerc re-Confirm o dată.")
            safe_click(*BTN_CONFIRM, label="RECONFIRM", throttle=True)
            maybe_close_notification()
            time.sleep(0.30)
            coin_after2 = read_coin_on_sell_tab()
            usdt_after2 = read_usdt_on_buy_tab()
            ok = (coin_after2 >= MIN_COIN_DELTA_CONFIRM) or ((usdt_before - usdt_after2) >= MIN_USDT_DELTA_CONFIRM)

        if ok:
            self.pos = +1
            self.entry_pct = cur_pct
            self.best_run = 0.0
            self.trail_armed = False
            mark_trade()
            print(f"[ENTER LONG OK] entry_pct={self.entry_pct:+.3f}% | USDT {usdt_before:.4f}->{usdt_after:.4f} | COIN ≈{max(coin_after, coin_after2 if 'coin_after2' in locals() else 0.0):.6f}")
        else:
            print("[ORDER FAILED] BUY nereușit (balanțe neschimbate)")

    def try_exit_long(self, reason):
        if self.pos != +1:
            return
        coin_before = read_coin_on_sell_tab()
        if coin_before < MIN_COIN_FOR_SELL:
            print(f"\n[BLOCK SELL] COIN insuficient ({coin_before:.6f} < {MIN_COIN_FOR_SELL:.6f})")
            return
        usdt_before = read_usdt_on_buy_tab()

        place_exit_long_seq()
        time.sleep(0.35)

        coin_after = read_coin_on_sell_tab()
        usdt_after = read_usdt_on_buy_tab()

        ok = ((coin_before - coin_after) >= MIN_COIN_DELTA_CONFIRM) or ((usdt_after - usdt_before) >= MIN_USDT_DELTA_CONFIRM)
        if not ok:
            print("[ORDER WARN] SELL posibil nereușit → încerc re-Confirm o dată.")
            safe_click(*BTN_CONFIRM, label="RECONFIRM", throttle=True)
            maybe_close_notification()
            time.sleep(0.30)
            coin_after2 = read_coin_on_sell_tab()
            usdt_after2 = read_usdt_on_buy_tab()
            ok = ((coin_before - coin_after2) >= MIN_COIN_DELTA_CONFIRM) or ((usdt_after2 - usdt_before) >= MIN_USDT_DELTA_CONFIRM)

        if ok:
            cur_pct = read_percent(PROCENTAJ_REGION)
            run_now = cur_pct - (self.entry_pct or 0.0)
            self.pos = 0
            self.entry_pct = None
            self.best_run = 0.0
            self.trail_armed = False
            mark_trade()
            print(f"[EXIT LONG {reason} OK] run_est={run_now:+.3f}% | COIN {coin_before:.6f}->{max(coin_after, coin_after2 if 'coin_after2' in locals() else 0.0):.6f} | USDT {usdt_before:.4f}->{max(usdt_after, usdt_after2 if 'usdt_after2' in locals() else 0.0):.4f}")
        else:
            print("[ORDER FAILED] SELL nereușit (balanțe neschimbate)")

    def step(self):
        pct_raw = read_percent(PROCENTAJ_REGION)
        pct_ma  = moving_avg_push(self._pct_ma_deq, pct_raw, MA_WINDOW)
        obi_now = obi_from_split_regions()

        if self.prev_pct is None:
            self.prev_pct = pct_ma
            delta = 0.0
        else:
            delta = pct_ma - self.prev_pct
            if   delta >  +DELTA_STEP_MIN: self.up_streak += 1;  self.down_streak = 0
            elif delta <  -DELTA_STEP_MIN: self.down_streak += 1; self.up_streak   = 0
            self.prev_pct = pct_ma

        # Exit logic dacă suntem în poziție
        if self.pos == +1:
            run_now = pct_ma - (self.entry_pct or pct_ma)
            if   run_now >= TP_PCT_L:  self.try_exit_long("TP")
            elif run_now <= -SL_PCT_L: self.try_exit_long("SL")
            else:
                # trailing
                if USE_TRAIL:
                    if run_now > self.best_run:
                        self.best_run = run_now
                        if self.best_run >= TRAIL_ARM:
                            self.trail_armed = True
                    if self.trail_armed and (self.best_run - run_now) >= TRAIL_GIV:
                        self.try_exit_long("TRAIL")
                # cădere vizibilă
                if self.down_streak >= FALL_STREAK_L:
                    self.try_exit_long("FALL")

        # Enter logic dacă suntem flat
        if self.pos == 0:
            self.try_enter_long()

        # status line
        sys.stdout.write(
            f"\rOBI={obi_now:+.3f} | %_MA={pct_ma:+.3f} | Δ={delta:+.3f} | up={self.up_streak} down={self.down_streak} | "
            f"pos={self.pos:+d} | best={self.best_run:+.3f} | edgeMin={ENTRY_EDGE_MIN:.2f}%   "
        ); sys.stdout.flush()

# ================== MAIN ==================
def main():
    print(f"[START ultra_v12] DRY_RUN={DRY_RUN} | FAILSAFE={'ON' if pyautogui.FAILSAFE else 'OFF'}")
    print(f"Taxe: 2x taker ≈ {TOTAL_FEES_PCT:.2f}% | edgeMin={ENTRY_EDGE_MIN:.2f}% | OBI_gate={OBI_GATE_LONG:.2f}")
    print("Adu fereastra Bitget în față. Pornesc în:")
    for i in range(COUNTDOWN, 0, -1):
        print(f"  -> {i}"); time.sleep(1)

    bot = Bot()
    dt = 1.0 / max(1e-6, BASE_FPS)

    try:
        while True:
            t0 = time.time()
            bot.step()
            elapsed = time.time() - t0
            if elapsed < dt:
                time.sleep(dt - elapsed)
    except pyautogui.FailSafeException:
        print("\n[STOP] FAILSAFE (mouse în colț).")
    except KeyboardInterrupt:
        print("\n[STOP] Oprit de utilizator.")
    finally:
        print("\nGata.")

if __name__ == "__main__":
    main()

import asyncio
import contextlib
import json
import os
import time
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, List, Optional

import websockets
from dotenv import load_dotenv

load_dotenv()

# =========================
# CONFIG
# =========================
WS_URL = os.getenv("BITGET_WS_URL", "wss://ws.bitget.com/v2/ws/public")
SYMBOL = os.getenv("SYMBOL", "BTCUSDT")
INST_TYPE = os.getenv("PRODUCT_TYPE", "USDT-FUTURES").upper()

START_BALANCE_USDT = float(os.getenv("PAPER_BALANCE_USDT", "1000"))
TRADE_ALLOCATION_PCT = float(os.getenv("TRADE_ALLOCATION_PCT", "8"))
LEVERAGE = float(os.getenv("LEVERAGE", "1"))

# Futures fees
ENTRY_FEE_PCT = float(os.getenv("ENTRY_FEE_PCT", "0.06"))
EXIT_FEE_PCT = float(os.getenv("EXIT_FEE_PCT", "0.06"))
SLIPPAGE_BUFFER_PCT = float(os.getenv("SLIPPAGE_BUFFER_PCT", "0.02"))
MIN_NET_PROFIT_PCT = float(os.getenv("MIN_NET_PROFIT_PCT", "0.06"))

# Entry filters - SAFE mode
BUY_STREAK_MIN = int(os.getenv("BUY_STREAK_MIN", "6"))
MIN_SLOPE_PCT = float(os.getenv("MIN_SLOPE_PCT", "0.025"))
MIN_LONG_SLOPE_PCT = float(os.getenv("MIN_LONG_SLOPE_PCT", "0.040"))
MIN_ENTRY_EDGE_PCT = float(os.getenv("MIN_ENTRY_EDGE_PCT", "0.06"))
MAX_DROP_PROB_TO_BUY = float(os.getenv("MAX_DROP_PROB_TO_BUY", "25.0"))
MAX_OVEREXTENSION_PCT = float(os.getenv("MAX_OVEREXTENSION_PCT", "0.14"))

# Risk/profit protection - SAFE mode
HARD_STOP_PCT = float(os.getenv("HARD_STOP_PCT", "0.14"))
ARM_PROFIT_LOCK_PCT = float(os.getenv("ARM_PROFIT_LOCK_PCT", "0.10"))
PROFIT_GIVEBACK_PCT = float(os.getenv("PROFIT_GIVEBACK_PCT", "10.0"))
FAST_DUMP_TICKS = int(os.getenv("FAST_DUMP_TICKS", "4"))
FAST_DUMP_DROP_PCT = float(os.getenv("FAST_DUMP_DROP_PCT", "0.08"))
SELL_STREAK_MIN = int(os.getenv("SELL_STREAK_MIN", "2"))
MAX_FLAT_HOLD_SECONDS = int(os.getenv("MAX_FLAT_HOLD_SECONDS", "90"))
LOSS_COOLDOWN_SECONDS = int(os.getenv("LOSS_COOLDOWN_SECONDS", "90"))
LOSS_STREAK_PAUSE_COUNT = int(os.getenv("LOSS_STREAK_PAUSE_COUNT", "3"))
LOSS_STREAK_PAUSE_SECONDS = int(os.getenv("LOSS_STREAK_PAUSE_SECONDS", "180"))

PRICE_BUFFER_SIZE = int(os.getenv("PRICE_BUFFER_SIZE", "320"))
PRINT_EVERY_SECONDS = float(os.getenv("PRINT_EVERY_SECONDS", "2.0"))
VERBOSE_STATUS = os.getenv("VERBOSE_STATUS", "false").lower() == "true"

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
TRADES_FILE = DATA_DIR / "futures_safe_v5_trades.jsonl"
STATUS_FILE = DATA_DIR / "futures_safe_v5_status.json"


# =========================
# HELPERS
# =========================
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_ts() -> float:
    return time.time()


def pct_change(a: float, b: float) -> float:
    if a == 0:
        return 0.0
    return ((b - a) / a) * 100.0


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def safe_mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def total_roundtrip_cost_pct() -> float:
    return ENTRY_FEE_PCT + EXIT_FEE_PCT + SLIPPAGE_BUFFER_PCT


def required_exit_price(entry_price: float) -> float:
    return entry_price * (1.0 + (total_roundtrip_cost_pct() + MIN_NET_PROFIT_PCT) / 100.0)


# =========================
# DATA MODELS
# =========================
@dataclass
class LongPosition:
    qty_btc: float
    entry_price: float
    margin_usdt: float
    notional_usdt: float
    entry_fee_usdt: float
    highest_price: float
    best_net_profit_pct: float
    opened_at: str
    opened_ts: float


@dataclass
class Snapshot:
    ts: str
    symbol: str
    inst_type: str
    price: float
    status: str
    usdt_balance: float
    equity_usdt: float
    realized_pnl_usdt: float
    total_fees_usdt: float
    trade_count: int
    buy_streak: int
    sell_streak: int
    slope_pct: float
    long_slope_pct: float
    vol_pct: float
    drop_prob_pct: float
    loss_streak: int
    reason: str
    cooldown_left_s: int
    position: Optional[dict]
    required_exit_price: float
    net_profit_pct: float
    net_profit_usdt: float
    sell_ok: bool


# =========================
# PAPER FUTURES WALLET (LONG ONLY)
# =========================
class PaperFuturesWallet:
    def __init__(self, balance_usdt: float):
        self.start_balance_usdt = balance_usdt
        self.usdt_balance = balance_usdt
        self.position: Optional[LongPosition] = None
        self.realized_pnl_usdt = 0.0
        self.total_fees_usdt = 0.0
        self.trade_count = 0

    def has_position(self) -> bool:
        return self.position is not None

    def equity(self, mark_price: Optional[float]) -> float:
        eq = self.usdt_balance
        if self.position and mark_price is not None:
            pnl = self.position.qty_btc * (mark_price - self.position.entry_price)
            eq += self.position.margin_usdt + pnl
        return eq

    def buy_long(self, price: float, reason: str) -> dict:
        if self.position is not None:
            raise RuntimeError("Position already open")

        margin = self.usdt_balance * (TRADE_ALLOCATION_PCT / 100.0)
        if margin <= 0:
            raise RuntimeError("No balance available")

        notional = margin * LEVERAGE
        entry_fee = notional * (ENTRY_FEE_PCT / 100.0)
        effective_notional = max(0.0, notional - entry_fee)
        qty = effective_notional / price

        self.usdt_balance -= margin
        self.total_fees_usdt += entry_fee
        self.position = LongPosition(
            qty_btc=qty,
            entry_price=price,
            margin_usdt=margin,
            notional_usdt=notional,
            entry_fee_usdt=entry_fee,
            highest_price=price,
            best_net_profit_pct=-999.0,
            opened_at=utc_now(),
            opened_ts=now_ts(),
        )

        return {
            "type": "BUY_LONG",
            "ts": utc_now(),
            "price": price,
            "qty_btc": qty,
            "margin_usdt": margin,
            "notional_usdt": notional,
            "entry_fee_usdt": entry_fee,
            "reason": reason,
        }

    def sell_long(self, price: float, reason: str) -> dict:
        if self.position is None:
            raise RuntimeError("No position open")

        pos = self.position
        gross_pnl = pos.qty_btc * (price - pos.entry_price)
        exit_fee = (pos.qty_btc * price) * (EXIT_FEE_PCT / 100.0)
        net_pnl = gross_pnl - exit_fee
        returned_margin = pos.margin_usdt + net_pnl

        self.usdt_balance += returned_margin
        self.realized_pnl_usdt += net_pnl
        self.total_fees_usdt += exit_fee
        self.trade_count += 1
        self.position = None

        return {
            "type": "SELL_LONG",
            "ts": utc_now(),
            "price": price,
            "qty_btc": pos.qty_btc,
            "gross_pnl_usdt": gross_pnl,
            "exit_fee_usdt": exit_fee,
            "pnl_usdt": net_pnl,
            "reason": reason,
        }


# =========================
# MARKET BRAIN
# =========================
class MarketBrain:
    def __init__(self):
        self.prices: Deque[float] = deque(maxlen=PRICE_BUFFER_SIZE)
        self.buy_streak = 0
        self.sell_streak = 0

    def update(self, price: float) -> None:
        if self.prices:
            prev = self.prices[-1]
            if price > prev:
                self.buy_streak += 1
                self.sell_streak = 0
            elif price < prev:
                self.sell_streak += 1
                self.buy_streak = 0
        self.prices.append(price)

    def slope_pct(self, window: int = 20) -> float:
        if len(self.prices) < max(2, window):
            return 0.0
        arr = list(self.prices)[-window:]
        return pct_change(arr[0], arr[-1])

    def vol_pct(self, window: int = 20) -> float:
        if len(self.prices) < max(2, window):
            return 0.0
        arr = list(self.prices)[-window:]
        avg = safe_mean(arr)
        if avg == 0:
            return 0.0
        return ((max(arr) - min(arr)) / avg) * 100.0

    def fast_dump_detected(self) -> bool:
        if len(self.prices) < FAST_DUMP_TICKS:
            return False
        arr = list(self.prices)[-FAST_DUMP_TICKS:]
        if any(arr[i] >= arr[i - 1] for i in range(1, len(arr))):
            return False
        return pct_change(arr[0], arr[-1]) <= -FAST_DUMP_DROP_PCT

    def drop_probability_pct(self) -> float:
        slope = self.slope_pct(18)
        vol = self.vol_pct(18)
        score = 50.0
        score += (-slope * 900.0)
        score += (self.sell_streak * 5.0)
        score -= (self.buy_streak * 3.5)
        score += (vol * 120.0)
        return clamp(score, 0.0, 100.0)

    def estimated_entry_edge_pct(self) -> float:
        if len(self.prices) < 8:
            return 0.0
        arr = list(self.prices)[-8:]
        return pct_change(arr[0], arr[-1])

    def overextension_pct(self, window: int = 12) -> float:
        if len(self.prices) < max(2, window):
            return 0.0
        arr = list(self.prices)[-window:]
        low = min(arr)
        last = arr[-1]
        return pct_change(low, last)


# =========================
# PNL / FEE STATS
# =========================
def long_position_stats(pos: Optional[LongPosition], price: Optional[float]) -> dict:
    if pos is None or price is None:
        return {
            "gross_pnl_usdt": 0.0,
            "net_pnl_usdt": 0.0,
            "net_pnl_pct_on_margin": 0.0,
            "required_exit_price": 0.0,
            "sell_ok": False,
        }

    gross_pnl = pos.qty_btc * (price - pos.entry_price)
    exit_fee = (pos.qty_btc * price) * (EXIT_FEE_PCT / 100.0)
    net_pnl = gross_pnl - exit_fee
    net_pct_on_margin = (net_pnl / pos.margin_usdt) * 100.0 if pos.margin_usdt > 0 else 0.0
    req_exit = required_exit_price(pos.entry_price)
    sell_ok = price >= req_exit and net_pct_on_margin >= MIN_NET_PROFIT_PCT

    return {
        "gross_pnl_usdt": gross_pnl,
        "net_pnl_usdt": net_pnl,
        "net_pnl_pct_on_margin": net_pct_on_margin,
        "required_exit_price": req_exit,
        "sell_ok": sell_ok,
    }


# =========================
# BOT
# =========================
class BTCFuturesSafeBot:
    def __init__(self):
        self.wallet = PaperFuturesWallet(START_BALANCE_USDT)
        self.brain = MarketBrain()
        self.current_price: Optional[float] = None
        self.last_print_at = 0.0
        self.last_reason = "Collecting data"
        self.cooldown_until_ts = 0.0
        self.loss_streak = 0

    def cooldown_left(self) -> int:
        return max(0, int(self.cooldown_until_ts - now_ts()))

    def set_loss_cooldown(self, seconds: int) -> None:
        self.cooldown_until_ts = max(self.cooldown_until_ts, now_ts() + seconds)

    def record_loss_result(self) -> None:
        self.loss_streak += 1
        self.set_loss_cooldown(LOSS_COOLDOWN_SECONDS)
        if self.loss_streak >= LOSS_STREAK_PAUSE_COUNT:
            self.set_loss_cooldown(LOSS_STREAK_PAUSE_SECONDS)

    def record_win_result(self) -> None:
        self.loss_streak = 0

    def save_trade(self, trade: dict) -> None:
        enriched = dict(trade)
        enriched["account_equity_usdt"] = self.wallet.equity(self.current_price)
        enriched["realized_pnl_usdt_total"] = self.wallet.realized_pnl_usdt
        enriched["loss_streak"] = self.loss_streak
        with TRADES_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(enriched, ensure_ascii=False) + "\n")

    def save_status(self) -> None:
        stats = long_position_stats(self.wallet.position, self.current_price)
        status = "COOLDOWN" if (not self.wallet.has_position() and self.cooldown_left() > 0) else ("HOLD" if not self.wallet.has_position() else "LONG")
        snap = Snapshot(
            ts=utc_now(),
            symbol=SYMBOL,
            inst_type=INST_TYPE,
            price=self.current_price or 0.0,
            status=status,
            usdt_balance=self.wallet.usdt_balance,
            equity_usdt=self.wallet.equity(self.current_price),
            realized_pnl_usdt=self.wallet.realized_pnl_usdt,
            total_fees_usdt=self.wallet.total_fees_usdt,
            trade_count=self.wallet.trade_count,
            buy_streak=self.brain.buy_streak,
            sell_streak=self.brain.sell_streak,
            slope_pct=self.brain.slope_pct(14),
            long_slope_pct=self.brain.slope_pct(50),
            vol_pct=self.brain.vol_pct(),
            drop_prob_pct=self.brain.drop_probability_pct(),
            loss_streak=self.loss_streak,
            reason=self.last_reason,
            cooldown_left_s=self.cooldown_left(),
            position=asdict(self.wallet.position) if self.wallet.position else None,
            required_exit_price=stats["required_exit_price"],
            net_profit_pct=stats["net_pnl_pct_on_margin"],
            net_profit_usdt=stats["net_pnl_usdt"],
            sell_ok=stats["sell_ok"],
        )
        STATUS_FILE.write_text(json.dumps(asdict(snap), ensure_ascii=False, indent=2), encoding="utf-8")

    def should_buy(self) -> tuple[bool, str]:
        if self.wallet.has_position():
            return False, "Already in position"
        if self.cooldown_left() > 0:
            return False, f"Cooldown active ({self.cooldown_left()}s)"
        if len(self.brain.prices) < 60:
            return False, "Not enough ticks"

        slope_short = self.brain.slope_pct(14)
        slope_long = self.brain.slope_pct(50)
        edge = self.brain.estimated_entry_edge_pct()
        drop_prob = self.brain.drop_probability_pct()
        overextension = self.brain.overextension_pct(12)

        if self.brain.buy_streak < BUY_STREAK_MIN:
            return False, f"Buy streak too weak ({self.brain.buy_streak}/{BUY_STREAK_MIN})"
        if slope_short < MIN_SLOPE_PCT:
            return False, f"Short slope too weak ({slope_short:.3f}% < {MIN_SLOPE_PCT:.3f}%)"
        if slope_long < MIN_LONG_SLOPE_PCT:
            return False, f"Long slope too weak ({slope_long:.3f}% < {MIN_LONG_SLOPE_PCT:.3f}%)"
        if edge < MIN_ENTRY_EDGE_PCT:
            return False, f"Entry edge too low ({edge:.3f}% < {MIN_ENTRY_EDGE_PCT:.3f}%)"
        if drop_prob > MAX_DROP_PROB_TO_BUY:
            return False, f"Drop risk too high ({drop_prob:.1f}% > {MAX_DROP_PROB_TO_BUY:.1f}%)"
        if overextension > MAX_OVEREXTENSION_PCT:
            return False, f"Move already too extended ({overextension:.3f}% > {MAX_OVEREXTENSION_PCT:.3f}%)"

        return True, "BUY LONG confirmed"

    def should_sell(self) -> tuple[bool, str]:
        pos = self.wallet.position
        price = self.current_price
        if pos is None or price is None:
            return False, "No open position"

        stats = long_position_stats(pos, price)
        net_pct = stats["net_pnl_pct_on_margin"]
        sell_ok = stats["sell_ok"]

        pos.highest_price = max(pos.highest_price, price)
        pos.best_net_profit_pct = max(pos.best_net_profit_pct, net_pct)

        stop_price = pos.entry_price * (1.0 - HARD_STOP_PCT / 100.0)
        if price <= stop_price:
            return True, f"Hard stop hit ({HARD_STOP_PCT:.2f}%)"

        if self.brain.fast_dump_detected() and self.brain.sell_streak >= 2:
            return True, "Fast dump detected"

        hold_seconds = now_ts() - pos.opened_ts
        if hold_seconds >= MAX_FLAT_HOLD_SECONDS and net_pct <= 0:
            return True, "Timed out without edge"

        if not sell_ok:
            # Safe mode: do not let weak losing positions hang around
            if self.brain.sell_streak >= 2 and self.brain.drop_probability_pct() >= 55:
                return True, "Weak before fee pass"
            return False, "Waiting for real futures edge"

        if pos.best_net_profit_pct >= ARM_PROFIT_LOCK_PCT:
            allowed_floor = pos.best_net_profit_pct * (1.0 - PROFIT_GIVEBACK_PCT / 100.0)
            if net_pct <= allowed_floor:
                return True, "Protecting profit"

        if pos.best_net_profit_pct > 0 and net_pct < 0:
            return True, "Lost open profit"

        if self.brain.sell_streak >= SELL_STREAK_MIN and self.brain.drop_probability_pct() >= 48:
            return True, "Weakness after fee-covered profit"

        return False, "Holding profit"

    def print_status(self) -> None:
        if not VERBOSE_STATUS or self.current_price is None:
            return

        stats = long_position_stats(self.wallet.position, self.current_price)
        status = "COOLDOWN" if (not self.wallet.has_position() and self.cooldown_left() > 0) else ("HOLD" if not self.wallet.has_position() else "LONG")
        print(
            f"Price={self.current_price:.2f} | Status={status} | Bal={self.wallet.usdt_balance:.2f} | "
            f"Eq={self.wallet.equity(self.current_price):.2f} | s14={self.brain.slope_pct(14):+.3f}% | "
            f"s50={self.brain.slope_pct(50):+.3f}% | dropProb={self.brain.drop_probability_pct():.1f}% | "
            f"loss_streak={self.loss_streak} | cooldown={self.cooldown_left()}s | reason={self.last_reason}"
        )
        if self.wallet.position:
            print(
                f"LONG {self.wallet.position.entry_price:.2f} | NEED {stats['required_exit_price']:.2f} | "
                f"NET {stats['net_pnl_usdt']:+.4f} USDT | sell_ok={stats['sell_ok']}"
            )

    def on_price(self, price: float) -> None:
        self.current_price = price
        self.brain.update(price)

        if not self.wallet.has_position():
            buy_ok, reason = self.should_buy()
            self.last_reason = reason
            if buy_ok:
                trade = self.wallet.buy_long(price, reason)
                self.save_trade(trade)
                req_exit = required_exit_price(price)
                print(f"LONG {price:.2f} | NEED {req_exit:.2f} TO BEAT FEES | BAL {self.wallet.usdt_balance:.2f}")
                self.save_status()
                return
        else:
            sell_ok, reason = self.should_sell()
            self.last_reason = reason
            if sell_ok:
                entry_price = self.wallet.position.entry_price if self.wallet.position else 0.0
                trade = self.wallet.sell_long(price, reason)

                if trade["pnl_usdt"] < 0:
                    self.record_loss_result()
                else:
                    self.record_win_result()

                trade["entry_price"] = entry_price
                self.save_trade(trade)
                print(f"LONG {entry_price:.2f} | EXIT {price:.2f} | RESULT {trade['pnl_usdt']:+.4f} USDT | BAL {self.wallet.usdt_balance:.2f}")
                self.save_status()
                return

        now = time.time()
        if now - self.last_print_at >= PRINT_EVERY_SECONDS:
            self.print_status()
            self.save_status()
            self.last_print_at = now


# =========================
# WEBSOCKET LOOP
# =========================
async def ping_loop(ws):
    while True:
        try:
            await asyncio.sleep(25)
            await ws.send("ping")
        except Exception:
            break


async def run_bot() -> None:
    bot = BTCFuturesSafeBot()
    print(f"Starting futures-safe bot v5 SAFE | SYMBOL={SYMBOL} | INST_TYPE={INST_TYPE}")

    while True:
        try:
            async with websockets.connect(
                WS_URL,
                ping_interval=None,
                ping_timeout=None,
            ) as ws:
                sub = {
                    "op": "subscribe",
                    "args": [
                        {
                            "instType": INST_TYPE,
                            "channel": "ticker",
                            "instId": SYMBOL,
                        }
                    ],
                }
                await ws.send(json.dumps(sub))
                print(f"Connected to {SYMBOL} futures ticker")

                ping_task = asyncio.create_task(ping_loop(ws))

                try:
                    async for raw in ws:
                        if raw == "pong":
                            continue

                        msg = json.loads(raw)
                        if msg.get("event") == "error":
                            print("WS error:", msg)
                            continue

                        data = msg.get("data") or []
                        if not data:
                            continue

                        item = data[0]
                        price = float(item.get("lastPr") or item.get("price") or 0.0)
                        if price <= 0:
                            continue
                        bot.on_price(price)
                finally:
                    ping_task.cancel()
                    with contextlib.suppress(Exception):
                        await ping_task
        except Exception as e:
            print(f"Reconnect... {e}")
            await asyncio.sleep(2)


if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        print("Stopped by user")
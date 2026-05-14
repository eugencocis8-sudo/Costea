import asyncio
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
INST_TYPE = os.getenv("PRODUCT_TYPE", "SPOT").upper()

START_BALANCE_USDT = float(os.getenv("PAPER_BALANCE_USDT", "1000"))
TRADE_ALLOCATION_PCT = float(os.getenv("TRADE_ALLOCATION_PCT", "25"))

# Fees as percentages, e.g. 0.1 means 0.1%
ENTRY_FEE_PCT = float(os.getenv("ENTRY_FEE_PCT", "0.1"))
EXIT_FEE_PCT = float(os.getenv("EXIT_FEE_PCT", "0.1"))
SLIPPAGE_BUFFER_PCT = float(os.getenv("SLIPPAGE_BUFFER_PCT", "0.03"))

# Entry filters
MIN_ENTRY_EDGE_PCT = float(os.getenv("MIN_ENTRY_EDGE_PCT", "0.08"))
BUY_STREAK_MIN = int(os.getenv("BUY_STREAK_MIN", "2"))
MIN_SLOPE_PCT = float(os.getenv("MIN_SLOPE_PCT", "0.005"))
MAX_DROP_PROB_TO_BUY = float(os.getenv("MAX_DROP_PROB_TO_BUY", "52.0"))

# Risk and profit protection
HARD_STOP_PCT = float(os.getenv("HARD_STOP_PCT", "0.45"))
ARM_PROFIT_LOCK_PCT = float(os.getenv("ARM_PROFIT_LOCK_PCT", "0.18"))
PROFIT_GIVEBACK_PCT = float(os.getenv("PROFIT_GIVEBACK_PCT", "35.0"))
FAST_DUMP_TICKS = int(os.getenv("FAST_DUMP_TICKS", "4"))
FAST_DUMP_DROP_PCT = float(os.getenv("FAST_DUMP_DROP_PCT", "0.08"))
SELL_STREAK_MIN = int(os.getenv("SELL_STREAK_MIN", "3"))

# Runtime
PRICE_BUFFER_SIZE = int(os.getenv("PRICE_BUFFER_SIZE", "180"))
PRINT_EVERY_SECONDS = float(os.getenv("PRINT_EVERY_SECONDS", "2.0"))
PING_INTERVAL = int(os.getenv("PING_INTERVAL", "20"))
PING_TIMEOUT = int(os.getenv("PING_TIMEOUT", "20"))

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
TRADES_FILE = DATA_DIR / "fee_guard_trades.jsonl"
STATUS_FILE = DATA_DIR / "fee_guard_status.json"


# =========================
# HELPERS
# =========================
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def pct_change(a: float, b: float) -> float:
    if a == 0:
        return 0.0
    return ((b - a) / a) * 100.0


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def safe_mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


# =========================
# DATA MODELS
# =========================
@dataclass
class Position:
    qty_btc: float
    entry_price: float
    gross_entry_usdt: float
    entry_fee_usdt: float
    highest_price: float
    best_net_profit_pct: float
    opened_at: str


@dataclass
class Snapshot:
    ts: str
    symbol: str
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
    vol_pct: float
    drop_prob_pct: float
    reason: str
    position: Optional[dict]
    fee_cover_pct: float
    required_exit_price: float
    net_profit_pct: float
    net_profit_usdt: float
    sell_ok: bool


# =========================
# PAPER WALLET
# =========================
class PaperWallet:
    def __init__(self, balance_usdt: float):
        self.start_balance_usdt = balance_usdt
        self.usdt_balance = balance_usdt
        self.position: Optional[Position] = None
        self.realized_pnl_usdt = 0.0
        self.total_fees_usdt = 0.0
        self.trade_count = 0

    def equity(self, mark_price: Optional[float]) -> float:
        eq = self.usdt_balance
        if self.position and mark_price is not None:
            eq += self.position.qty_btc * mark_price
        return eq

    def has_position(self) -> bool:
        return self.position is not None

    def buy(self, price: float, reason: str) -> dict:
        if self.position is not None:
            raise RuntimeError("Position already open")

        gross_entry = self.usdt_balance * (TRADE_ALLOCATION_PCT / 100.0)
        if gross_entry <= 0:
            raise RuntimeError("No balance available")

        entry_fee = gross_entry * (ENTRY_FEE_PCT / 100.0)
        net_spend = gross_entry - entry_fee
        qty = net_spend / price

        self.usdt_balance -= gross_entry
        self.total_fees_usdt += entry_fee
        self.position = Position(
            qty_btc=qty,
            entry_price=price,
            gross_entry_usdt=gross_entry,
            entry_fee_usdt=entry_fee,
            highest_price=price,
            best_net_profit_pct=-999.0,
            opened_at=utc_now(),
        )

        return {
            "type": "BUY",
            "ts": utc_now(),
            "price": price,
            "qty_btc": qty,
            "gross_entry_usdt": gross_entry,
            "entry_fee_usdt": entry_fee,
            "reason": reason,
        }

    def sell_all(self, price: float, reason: str) -> dict:
        if self.position is None:
            raise RuntimeError("No position open")

        pos = self.position
        gross_value = pos.qty_btc * price
        exit_fee = gross_value * (EXIT_FEE_PCT / 100.0)
        net_value = gross_value - exit_fee
        pnl = net_value - pos.gross_entry_usdt

        self.usdt_balance += net_value
        self.realized_pnl_usdt += pnl
        self.total_fees_usdt += exit_fee
        self.trade_count += 1
        self.position = None

        return {
            "type": "SELL",
            "ts": utc_now(),
            "price": price,
            "qty_btc": pos.qty_btc,
            "gross_value_usdt": gross_value,
            "exit_fee_usdt": exit_fee,
            "pnl_usdt": pnl,
            "reason": reason,
        }


# =========================
# INTELLIGENCE LAYER
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
        span = max(arr) - min(arr)
        return (span / avg) * 100.0

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
        arr = list(self.prices)
        recent = arr[-8:]
        return pct_change(recent[0], recent[-1])


# =========================
# FEE / PNL LOGIC
# =========================
def total_roundtrip_cost_pct() -> float:
    return ENTRY_FEE_PCT + EXIT_FEE_PCT + SLIPPAGE_BUFFER_PCT


def required_exit_price(entry_price: float) -> float:
    total_needed_pct = total_roundtrip_cost_pct()
    return entry_price * (1.0 + total_needed_pct / 100.0)


def position_stats(pos: Optional[Position], price: Optional[float]) -> dict:
    if pos is None or price is None:
        return {
            "gross_pnl_usdt": 0.0,
            "gross_pnl_pct": 0.0,
            "net_pnl_usdt": 0.0,
            "net_pnl_pct": 0.0,
            "fee_cover_pct": 0.0,
            "required_exit_price": 0.0,
            "sell_ok": False,
        }

    gross_value = pos.qty_btc * price
    exit_fee = gross_value * (EXIT_FEE_PCT / 100.0)
    net_value = gross_value - exit_fee

    gross_pnl_usdt = gross_value - pos.gross_entry_usdt
    net_pnl_usdt = net_value - pos.gross_entry_usdt
    gross_pnl_pct = (gross_pnl_usdt / pos.gross_entry_usdt) * 100.0
    net_pnl_pct = (net_pnl_usdt / pos.gross_entry_usdt) * 100.0

    req_exit = required_exit_price(pos.entry_price)
    fee_cover_pct = 100.0 if price >= req_exit else max(0.0, (price / req_exit) * 100.0)
    sell_ok = price >= req_exit and net_pnl_usdt > 0

    return {
        "gross_pnl_usdt": gross_pnl_usdt,
        "gross_pnl_pct": gross_pnl_pct,
        "net_pnl_usdt": net_pnl_usdt,
        "net_pnl_pct": net_pnl_pct,
        "fee_cover_pct": fee_cover_pct,
        "required_exit_price": req_exit,
        "sell_ok": sell_ok,
    }


# =========================
# BOT
# =========================
class BTCFeeGuardBot:
    def __init__(self):
        self.wallet = PaperWallet(START_BALANCE_USDT)
        self.brain = MarketBrain()
        self.current_price: Optional[float] = None
        self.last_print_at = 0.0
        self.last_reason = "Collecting data"

    def build_position_explain(self, price: float) -> dict:
        pos = self.wallet.position
        stats = position_stats(pos, price)
        if pos is None:
            return {
                "entry_price": 0.0,
                "current_price": price,
                "required_exit_price": 0.0,
                "distance_to_fee_pass_pct": 0.0,
                "distance_to_fee_pass_usdt": 0.0,
                "gross_pnl_usdt": 0.0,
                "net_pnl_usdt": 0.0,
                "best_net_profit_pct": 0.0,
                "missed_profit_from_peak_usdt": 0.0,
            }

        distance_pct = pct_change(price, stats["required_exit_price"]) if price < stats["required_exit_price"] else 0.0
        distance_usdt = max(0.0, stats["required_exit_price"] - price)

        best_net_profit_pct = max(0.0, pos.best_net_profit_pct)
        best_price = pos.entry_price * (1.0 + best_net_profit_pct / 100.0)
        missed_profit_from_peak_usdt = max(0.0, (best_price - price) * pos.qty_btc)

        return {
            "entry_price": pos.entry_price,
            "current_price": price,
            "required_exit_price": stats["required_exit_price"],
            "distance_to_fee_pass_pct": distance_pct,
            "distance_to_fee_pass_usdt": distance_usdt,
            "gross_pnl_usdt": stats["gross_pnl_usdt"],
            "net_pnl_usdt": stats["net_pnl_usdt"],
            "best_net_profit_pct": pos.best_net_profit_pct,
            "missed_profit_from_peak_usdt": missed_profit_from_peak_usdt,
        }

    def analyze_trade_reason(
        self,
        entry_price: float,
        sell_price: float,
        sell_reason: str,
        pos_before_sell: Optional[Position],
        stats_before_sell: dict,
    ) -> list[str]:
        reasons: list[str] = []

        move_pct = pct_change(entry_price, sell_price)
        net_pct = stats_before_sell.get("net_pnl_pct", 0.0)
        req_exit = stats_before_sell.get("required_exit_price", 0.0)
        drop_prob = self.brain.drop_probability_pct()
        slope = self.brain.slope_pct()
        vol = self.brain.vol_pct()

        if "Hard stop" in sell_reason:
            reasons.append("prețul a coborât până la hard stop înainte să apară profit suficient")
        if "Fast dump" in sell_reason:
            reasons.append("piața a căzut brusc pe tick-uri consecutive și botul a ieșit defensiv")
        if "Defensive exit" in sell_reason:
            reasons.append("riscul de scădere a devenit prea mare înainte ca ieșirea să fie confortabilă")
        if "Protecting profit" in sell_reason:
            reasons.append("a existat profit net, dar prețul a dat înapoi prea mult din profitul capturat")
        if "Weakness after fee-covered profit" in sell_reason:
            reasons.append("după ce fee-ul a fost acoperit, impulsul s-a slăbit și botul a protejat profitul")

        if sell_price < req_exit and net_pct < 0:
            reasons.append("prețul de vânzare a rămas sub nivelul minim necesar pentru a bate fee-ul")
        if move_pct < 0:
            reasons.append("direcția după buy a fost greșită sau s-a întors prea repede împotriva poziției")
        if self.brain.sell_streak >= SELL_STREAK_MIN:
            reasons.append("au apărut mai multe tick-uri negative consecutive după intrare")
        if drop_prob >= 60:
            reasons.append("probabilitatea internă de scădere a devenit ridicată")
        if slope < 0:
            reasons.append("panta scurtă a devenit negativă după intrare")
        if vol > 0.03:
            reasons.append("volatilitatea scurtă a fost ridicată și a crescut riscul de whipsaw")
        if pos_before_sell is not None and pos_before_sell.best_net_profit_pct > 0 and net_pct < 0:
            reasons.append("poziția a avut șansă de profit, dar întoarcerea a fost prea rapidă și profitul nu a mai putut fi păstrat")

        if not reasons:
            reasons.append("mișcarea a fost mică sau neclară și trade-ul nu a reușit să dezvolte avantaj net")

        unique_reasons: list[str] = []
        for item in reasons:
            if item not in unique_reasons:
                unique_reasons.append(item)
        return unique_reasons

    def save_trade(self, trade: dict) -> None:
        enriched = dict(trade)
        if self.current_price is not None:
            enriched["account_equity_usdt"] = self.wallet.equity(self.current_price)
        enriched["realized_pnl_usdt_total"] = self.wallet.realized_pnl_usdt

        with TRADES_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(enriched, ensure_ascii=False) + "\n")

    def save_status(self) -> None:
        stats = position_stats(self.wallet.position, self.current_price)
        snap = Snapshot(
            ts=utc_now(),
            symbol=SYMBOL,
            price=self.current_price or 0.0,
            status="HOLD" if not self.wallet.has_position() else "IN_POSITION",
            usdt_balance=self.wallet.usdt_balance,
            equity_usdt=self.wallet.equity(self.current_price),
            realized_pnl_usdt=self.wallet.realized_pnl_usdt,
            total_fees_usdt=self.wallet.total_fees_usdt,
            trade_count=self.wallet.trade_count,
            buy_streak=self.brain.buy_streak,
            sell_streak=self.brain.sell_streak,
            slope_pct=self.brain.slope_pct(),
            vol_pct=self.brain.vol_pct(),
            drop_prob_pct=self.brain.drop_probability_pct(),
            reason=self.last_reason,
            position=asdict(self.wallet.position) if self.wallet.position else None,
            fee_cover_pct=stats["fee_cover_pct"],
            required_exit_price=stats["required_exit_price"],
            net_profit_pct=stats["net_pnl_pct"],
            net_profit_usdt=stats["net_pnl_usdt"],
            sell_ok=stats["sell_ok"],
        )
        STATUS_FILE.write_text(json.dumps(asdict(snap), ensure_ascii=False, indent=2), encoding="utf-8")

    def should_buy(self) -> tuple[bool, str]:
        if self.wallet.has_position():
            return False, "Already in position"
        if len(self.brain.prices) < 12:
            return False, "Not enough ticks"

        slope = self.brain.slope_pct(12)
        edge = self.brain.estimated_entry_edge_pct()
        drop_prob = self.brain.drop_probability_pct()

        if self.brain.buy_streak < BUY_STREAK_MIN:
            return False, f"Buy streak too weak ({self.brain.buy_streak}/{BUY_STREAK_MIN})"
        if slope < MIN_SLOPE_PCT:
            return False, f"Slope too weak ({slope:.3f}%)"
        if edge < MIN_ENTRY_EDGE_PCT:
            return False, f"Entry edge too low ({edge:.3f}%)"
        if drop_prob > MAX_DROP_PROB_TO_BUY:
            return False, f"Drop risk too high ({drop_prob:.1f}%)"

        return True, "BUY confirmed: momentum + slope + edge after costs"

    def should_sell(self) -> tuple[bool, str]:
        pos = self.wallet.position
        price = self.current_price
        if pos is None or price is None:
            return False, "No open position"

        stats = position_stats(pos, price)
        net_pct = stats["net_pnl_pct"]
        sell_ok = stats["sell_ok"]

        pos.highest_price = max(pos.highest_price, price)
        pos.best_net_profit_pct = max(pos.best_net_profit_pct, net_pct)

        stop_price = pos.entry_price * (1.0 - HARD_STOP_PCT / 100.0)
        if price <= stop_price:
            return True, f"Hard stop hit ({HARD_STOP_PCT:.2f}%)"

        if self.brain.fast_dump_detected() and self.brain.sell_streak >= SELL_STREAK_MIN:
            return True, "Fast dump detected"

        if not sell_ok:
            if self.brain.sell_streak >= SELL_STREAK_MIN + 2 and self.brain.drop_probability_pct() > 72:
                return True, "Defensive exit before larger loss"
            return False, "Wants SELL, but fee not covered yet"

        if pos.best_net_profit_pct >= ARM_PROFIT_LOCK_PCT:
            allowed_floor = pos.best_net_profit_pct * (1.0 - PROFIT_GIVEBACK_PCT / 100.0)
            if net_pct <= allowed_floor:
                return True, (
                    f"Protecting profit: net fell from {pos.best_net_profit_pct:.3f}% "
                    f"to {net_pct:.3f}%"
                )

        if self.brain.sell_streak >= SELL_STREAK_MIN and self.brain.drop_probability_pct() >= 58:
            return True, "Weakness after fee-covered profit"

        return False, "Holding profit"

    def print_status(self) -> None:
        if self.current_price is None:
            return

        stats = position_stats(self.wallet.position, self.current_price)
        pos = self.wallet.position
        status = "HOLD" if pos is None else "IN_POSITION"

        print(
            f"Price={self.current_price:.2f} | Status={status} | "
            f"Bal={self.wallet.usdt_balance:.2f} | Eq={self.wallet.equity(self.current_price):.2f} | "
            f"buyStreak={self.brain.buy_streak} sellStreak={self.brain.sell_streak} | "
            f"slope={self.brain.slope_pct():+.3f}% vol={self.brain.vol_pct():.3f}% | "
            f"dropProb={self.brain.drop_probability_pct():.1f}%"
        )

        if pos is not None:
            explain = self.build_position_explain(self.current_price)
            if not stats["sell_ok"]:
                print(
                    f"BUY at {explain['entry_price']:.2f} | now {explain['current_price']:.2f} | "
                    f"WAIT FOR FEE PASS -> need {explain['required_exit_price']:.2f} | "
                    f"missing {explain['distance_to_fee_pass_usdt']:.2f} on price ({explain['distance_to_fee_pass_pct']:.3f}%)"
                )
            else:
                print(
                    f"BUY at {explain['entry_price']:.2f} | now {explain['current_price']:.2f} | "
                    f"FEE PASSED | gross={explain['gross_pnl_usdt']:.4f} USDT | "
                    f"net={explain['net_pnl_usdt']:.4f} USDT"
                )

            print(
                f"best_net={explain['best_net_profit_pct']:.3f}% | "
                f"missed_from_peak={explain['missed_profit_from_peak_usdt']:.4f} USDT | "
                f"sell_ok={stats['sell_ok']} | reason={self.last_reason}"
            )
        else:
            print(f"reason={self.last_reason}")

    def on_price(self, price: float) -> None:
        self.current_price = price
        self.brain.update(price)

        if not self.wallet.has_position():
            buy_ok, reason = self.should_buy()
            self.last_reason = reason
            if buy_ok:
                trade = self.wallet.buy(price, reason)
                req_exit = required_exit_price(price)

                print(
                    f"BUY at {price:.2f} | qty={trade['qty_btc']:.8f} BTC | "
                    f"entry_fee={trade['entry_fee_usdt']:.4f} | wait for fee pass >= {req_exit:.2f} | {reason}"
                )

                self.save_trade(trade)
                self.save_status()
                return

        else:
            sell_ok, reason = self.should_sell()
            self.last_reason = reason
            if sell_ok:
                pos_before_sell = self.wallet.position
                stats_before_sell = position_stats(pos_before_sell, price)
                sell_explain = self.build_position_explain(price)
                entry_price = sell_explain["entry_price"]

                trade = self.wallet.sell_all(price, reason)
                trade["entry_price"] = entry_price
                trade["analysis_reasons"] = self.analyze_trade_reason(
                    entry_price, price, reason, pos_before_sell, stats_before_sell
                )

                print("\n=============================")
                print(f"BUY AT  {entry_price:.2f}")
                print(f"SELL AT {price:.2f}")
                print(f"RESULT  = {trade['pnl_usdt']:+.4f} USDT")
                print(f"BALANCE = {self.wallet.usdt_balance:.2f} USDT")
                print(f"EQUITY  = {self.wallet.equity(self.current_price):.2f} USDT")
                print("\nREASON:")

                if trade["pnl_usdt"] >= 0:
                    print(f"- {reason}")
                    print("- trade-ul a trecut peste fee și a închis pe profit net")
                else:
                    for item in trade["analysis_reasons"]:
                        print(f"- {item}")

                print("=============================\n")

                self.save_trade(trade)
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
async def run_bot() -> None:
    bot = BTCFeeGuardBot()
    print(f"Starting fee-guard bot | SYMBOL={SYMBOL} | INST_TYPE={INST_TYPE}")

    while True:
        try:
            async with websockets.connect(
                WS_URL,
                ping_interval=PING_INTERVAL,
                ping_timeout=PING_TIMEOUT,
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
                print(f"Connected to {SYMBOL} ticker")

                async for raw in ws:
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

        except Exception as e:
            print(f"Reconnect... {e}")
            await asyncio.sleep(2)


if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        print("Stopped by user")
"""
Bitget SPOT Paper Bot
Strategy: MA5/MA20 + acceleration + anti-fee + trailing stop + paper balance

Default: PAPER_MODE=true, so it does NOT trade real money.

Install:
    pip install requests pandas python-dotenv

Run:
    python BitgetMaTrendBot2.py

.env optional:
    SYMBOL=BTCUSDT
    GRANULARITY=5min
    PAPER_MODE=true
    TRADE_QUOTE_SIZE=10
    TAKE_PROFIT_PCT=0.006
    STOP_LOSS_PCT=0.0025
    TRAILING_STOP_PCT=0.0025
    FEE_PCT=0.001
    SLIPPAGE_PCT=0.0005
    MIN_EDGE_PCT=0.0000
    MIN_ACCEL_PCT=0.00001
    MAX_TRADES_PER_DAY=3
    COOLDOWN_AFTER_LOSS_SECONDS=1800
    LOOP_SECONDS=30
"""

import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.bitget.com"


@dataclass
class Config:
    symbol: str = os.getenv("SYMBOL", "BTCUSDT")
    granularity: str = os.getenv("GRANULARITY", "5min")
    paper_mode: bool = os.getenv("PAPER_MODE", "true").lower() == "true"

    trade_quote_size: float = float(os.getenv("TRADE_QUOTE_SIZE", "10"))
    take_profit_pct: float = float(os.getenv("TAKE_PROFIT_PCT", "0.006"))
    stop_loss_pct: float = float(os.getenv("STOP_LOSS_PCT", "0.0025"))
    trailing_stop_pct: float = float(os.getenv("TRAILING_STOP_PCT", "0.0025"))

    fee_pct: float = float(os.getenv("FEE_PCT", "0.001"))
    slippage_pct: float = float(os.getenv("SLIPPAGE_PCT", "0.0005"))
    min_edge_pct: float = float(os.getenv("MIN_EDGE_PCT", "0.0000"))
    min_accel_pct: float = float(os.getenv("MIN_ACCEL_PCT", "0.00001"))

    max_trades_per_day: int = int(os.getenv("MAX_TRADES_PER_DAY", "3"))
    cooldown_after_loss_seconds: int = int(os.getenv("COOLDOWN_AFTER_LOSS_SECONDS", "1800"))
    loop_seconds: int = int(os.getenv("LOOP_SECONDS", "30"))


class BitgetMarket:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()

    def get_candles(self, limit: int = 100) -> pd.DataFrame:
        url = f"{BASE_URL}/api/v2/spot/market/candles"
        params = {
            "symbol": self.cfg.symbol,
            "granularity": self.cfg.granularity,
            "limit": str(limit),
        }

        r = self.session.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()

        if data.get("code") != "00000":
            raise RuntimeError(data)

        rows = data["data"]
        cols = ["time", "open", "high", "low", "close", "base_vol", "quote_vol", "usdt_vol"]
        df = pd.DataFrame(rows, columns=cols[: len(rows[0])])

        df["time"] = pd.to_numeric(df["time"])
        for col in ["open", "high", "low", "close"]:
            df[col] = pd.to_numeric(df[col])

        return df.sort_values("time").reset_index(drop=True)


def calculate_signal(df: pd.DataFrame, cfg: Config) -> Dict[str, Any]:
    df = df.copy()

    df["ma5"] = df["close"].rolling(5).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["signal"] = df["ma5"] - df["ma20"]
    df["acceleration"] = df["signal"] - df["signal"].shift(1)
    df["candle_body_pct"] = (df["close"] - df["open"]) / df["open"]

    last = df.iloc[-1]

    price = float(last["close"])
    signal_pct = float(last["signal"] / price)
    acceleration_pct = float(last["acceleration"] / price)
    candle_green = float(last["candle_body_pct"]) > 0

    needed_edge = (cfg.fee_pct * 2) + cfg.slippage_pct + cfg.min_edge_pct

    action = "HOLD"

    if (
        signal_pct > needed_edge
        and acceleration_pct > cfg.min_accel_pct
        and candle_green
    ):
        action = "BUY"
    elif last["signal"] < 0 and last["acceleration"] < 0:
        action = "SELL"

    return {
        "action": action,
        "price": price,
        "ma5": float(last["ma5"]),
        "ma20": float(last["ma20"]),
        "signal_pct": signal_pct,
        "acceleration_pct": acceleration_pct,
        "needed_edge": needed_edge,
        "candle_green": candle_green,
    }


class PaperBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.market = BitgetMarket(cfg)

        self.balance = cfg.trade_quote_size
        self.in_position = False
        self.entry_price: Optional[float] = None
        self.highest_price: Optional[float] = None
        self.base_amount = 0.0

        self.trades_today = 0
        self.current_day = time.strftime("%Y-%m-%d")
        self.cooldown_until = 0.0

    def reset_day_if_needed(self):
        today = time.strftime("%Y-%m-%d")
        if today != self.current_day:
            self.current_day = today
            self.trades_today = 0

    def can_buy(self) -> bool:
        self.reset_day_if_needed()
        if time.time() < self.cooldown_until:
            return False
        if self.trades_today >= self.cfg.max_trades_per_day:
            return False
        if self.in_position:
            return False
        return True

    def buy(self, price: float):
        self.entry_price = price
        self.highest_price = price
        self.base_amount = self.balance / price
        self.in_position = True
        self.trades_today += 1
        print(f"[PAPER BUY] BUY at {price:.6f} | amount {self.balance:.4f} USDT")

    def sell(self, price: float, reason: str):
        if not self.in_position or self.entry_price is None:
            return

        pnl = (price - self.entry_price) * self.base_amount
        pnl_pct = ((price - self.entry_price) / self.entry_price) * 100

        # Simulated round-trip fees: buy + sell
        fee_cost = self.balance * self.cfg.fee_pct * 2
        slippage_cost = self.balance * self.cfg.slippage_pct
        net_pnl = pnl - fee_cost - slippage_cost

        self.balance += net_pnl

        print(
            f"[PAPER SELL] {reason} | SELL at {price:.6f} | "
            f"gross {pnl:+.4f} USDT | fees/slip -{fee_cost + slippage_cost:.4f} | "
            f"net {net_pnl:+.4f} | BALANCE {self.balance:.4f} USDT"
        )

        if net_pnl < 0:
            self.cooldown_until = time.time() + self.cfg.cooldown_after_loss_seconds

        self.in_position = False
        self.entry_price = None
        self.highest_price = None
        self.base_amount = 0.0

    def exit_reason(self, price: float, signal_action: str) -> Optional[str]:
        if not self.in_position or self.entry_price is None:
            return None

        if self.highest_price is None or price > self.highest_price:
            self.highest_price = price

        change = (price - self.entry_price) / self.entry_price
        pullback = (self.highest_price - price) / self.highest_price

        if change >= self.cfg.take_profit_pct:
            return "TAKE_PROFIT"
        if change > 0 and pullback >= self.cfg.trailing_stop_pct:
            return "TRAILING_STOP"
        if change <= -self.cfg.stop_loss_pct:
            return "STOP_LOSS"
        if signal_action == "SELL":
            return "TREND_REVERSAL"

        return None

    def print_status(self, info: Dict[str, Any]):
        price = info["price"]

        if self.in_position and self.entry_price is not None:
            gross_pnl = (price - self.entry_price) * self.base_amount
            pnl_pct = ((price - self.entry_price) / self.entry_price) * 100
            tp = self.entry_price * (1 + self.cfg.take_profit_pct)
            sl = self.entry_price * (1 - self.cfg.stop_loss_pct)
            high = self.highest_price or price
            trail = high * (1 - self.cfg.trailing_stop_pct)

            print(
                f"{self.cfg.symbol} | BUY at {self.entry_price:.6f} | NOW {price:.6f} | "
                f"SELL TP {tp:.6f} | TRAIL {trail:.6f} | STOP {sl:.6f} | "
                f"P/L {gross_pnl:+.4f} USDT ({pnl_pct:+.3f}%) | BALANCE {self.balance + gross_pnl:.4f} USDT"
            )
        else:
            tp = price * (1 + self.cfg.take_profit_pct)
            sl = price * (1 - self.cfg.stop_loss_pct)
            status = "READY" if self.can_buy() else "COOLDOWN/LIMIT"
            expected_profit = self.balance * self.cfg.take_profit_pct
            expected_loss = self.balance * self.cfg.stop_loss_pct

            print(
                f"{self.cfg.symbol} | {status} | possible BUY {price:.6f} | "
                f"SELL TP {tp:.6f} = profit +{expected_profit:.4f} | "
                f"STOP {sl:.6f} = loss -{expected_loss:.4f} | "
                f"signal {info['action']} | edge {info['signal_pct']*100:.3f}% / needed {info['needed_edge']*100:.3f}% | "
                f"BALANCE {self.balance:.4f} USDT"
            )

    def run_once(self):
        df = self.market.get_candles(limit=100)
        info = calculate_signal(df, self.cfg)
        price = info["price"]

        self.print_status(info)

        reason = self.exit_reason(price, info["action"])
        if reason:
            self.sell(price, reason)
            return

        if info["action"] == "BUY" and self.can_buy():
            self.buy(price)

    def run_forever(self):
        print(
            f"Starting bot | symbol={self.cfg.symbol} | granularity={self.cfg.granularity} | "
            f"paper_mode={self.cfg.paper_mode} | balance={self.balance:.4f} USDT"
        )

        if not self.cfg.paper_mode:
            raise SystemExit("This version is PAPER ONLY. Keep PAPER_MODE=true.")

        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                print("Stopped by user.")
                break
            except Exception as e:
                print("ERROR:", repr(e))

            time.sleep(self.cfg.loop_seconds)


if __name__ == "__main__":
    PaperBot(Config()).run_forever()

"""
Bitget SPOT bot V2: Weather-style MA trend + anti-fee mode

IMPORTANT:
- Starts in PAPER_MODE=True by default. It will NOT place real orders until you set PAPER_MODE=false in .env.
- Strategy:
    Signal = MA5 - MA20
    Acceleration = Signal_now - Signal_previous
    BUY only when trend is strong enough to beat fee + slippage.
    SELL by take profit, stop loss, trailing stop, or trend reversal.

Install:
    pip install requests pandas python-dotenv

.env example:
    BITGET_API_KEY=your_key
    BITGET_API_SECRET=your_secret
    BITGET_PASSPHRASE=your_passphrase
    SYMBOL=BTCUSDT
    GRANULARITY=5min
    PAPER_MODE=true
    TRADE_QUOTE_SIZE=10
    TAKE_PROFIT_PCT=0.009
    STOP_LOSS_PCT=0.0025
    TRAILING_STOP_PCT=0.003
    FEE_PCT=0.001
    SLIPPAGE_PCT=0.0005
    MIN_EDGE_PCT=0.003
    MIN_ACCEL_PCT=0.00008
    COOLDOWN_AFTER_LOSS_SECONDS=1800
    MAX_TRADES_PER_DAY=3
    LOOP_SECONDS=30
"""

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.bitget.com"


@dataclass
class Config:
    api_key: str = os.getenv("BITGET_API_KEY", "")
    api_secret: str = os.getenv("BITGET_API_SECRET", "")
    passphrase: str = os.getenv("BITGET_PASSPHRASE", "")
    symbol: str = os.getenv("SYMBOL", "BTCUSDT")
    granularity: str = os.getenv("GRANULARITY", "1min")
    paper_mode: bool = os.getenv("PAPER_MODE", "true").lower() == "true"
    trade_quote_size: float = float(os.getenv("TRADE_QUOTE_SIZE", "10"))  # USDT for market BUY
    take_profit_pct: float = float(os.getenv("TAKE_PROFIT_PCT", "0.009"))  # 0.9%
    stop_loss_pct: float = float(os.getenv("STOP_LOSS_PCT", "0.0025"))     # 0.25%
    trailing_stop_pct: float = float(os.getenv("TRAILING_STOP_PCT", "0.003"))  # 0.3%
    fee_pct: float = float(os.getenv("FEE_PCT", "0.001"))  # estimated one-side fee, example 0.1%
    slippage_pct: float = float(os.getenv("SLIPPAGE_PCT", "0.0005"))
    min_edge_pct: float = float(os.getenv("MIN_EDGE_PCT", "0.003"))
    min_accel_pct: float = float(os.getenv("MIN_ACCEL_PCT", "0.00008"))
    cooldown_after_loss_seconds: int = int(os.getenv("COOLDOWN_AFTER_LOSS_SECONDS", "1800"))
    max_trades_per_day: int = int(os.getenv("MAX_TRADES_PER_DAY", "3"))
    loop_seconds: int = int(os.getenv("LOOP_SECONDS", "30"))


class BitgetClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()

    @staticmethod
    def _timestamp_ms() -> str:
        return str(int(time.time() * 1000))

    def _sign(self, timestamp: str, method: str, path: str, query: str = "", body: str = "") -> str:
        if query:
            message = f"{timestamp}{method.upper()}{path}?{query}{body}"
        else:
            message = f"{timestamp}{method.upper()}{path}{body}"

        mac = hmac.new(
            self.cfg.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(mac).decode("utf-8")

    def _headers(self, method: str, path: str, query: str = "", body: str = "") -> Dict[str, str]:
        ts = self._timestamp_ms()
        return {
            "ACCESS-KEY": self.cfg.api_key,
            "ACCESS-SIGN": self._sign(ts, method, path, query, body),
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self.cfg.passphrase,
            "Content-Type": "application/json",
            "locale": "en-US",
        }

    def public_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        r = self.session.get(BASE_URL + path, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != "00000":
            raise RuntimeError(data)
        return data

    def private_post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":"))
        headers = self._headers("POST", path, body=body)
        r = self.session.post(BASE_URL + path, headers=headers, data=body, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != "00000":
            raise RuntimeError(data)
        return data

    def get_candles(self, limit: int = 100) -> pd.DataFrame:
        path = "/api/v2/spot/market/candles"
        params = {
            "symbol": self.cfg.symbol,
            "granularity": self.cfg.granularity,
            "limit": str(limit),
        }
        data = self.public_get(path, params=params)["data"]

        # Bitget candles normally: timestamp, open, high, low, close, base volume, quote volume, usdt volume
        cols = ["time", "open", "high", "low", "close", "base_vol", "quote_vol", "usdt_vol"]
        df = pd.DataFrame(data, columns=cols[: len(data[0])])
        df["time"] = pd.to_numeric(df["time"])
        for col in ["open", "high", "low", "close"]:
            df[col] = pd.to_numeric(df[col])
        return df.sort_values("time").reset_index(drop=True)

    def place_market_buy(self, quote_amount: float) -> Dict[str, Any]:
        payload = {
            "symbol": self.cfg.symbol,
            "side": "buy",
            "orderType": "market",
            "size": str(round(quote_amount, 6)),  # market BUY size = quote coin amount, e.g. USDT
            "clientOid": f"ma-bot-{uuid.uuid4().hex[:20]}",
        }
        return self.private_post("/api/v2/spot/trade/place-order", payload)

    def place_market_sell(self, base_amount: float) -> Dict[str, Any]:
        payload = {
            "symbol": self.cfg.symbol,
            "side": "sell",
            "orderType": "market",
            "size": str(round(base_amount, 8)),  # market SELL size = base coin amount, e.g. BTC
            "clientOid": f"ma-bot-{uuid.uuid4().hex[:20]}",
        }
        return self.private_post("/api/v2/spot/trade/place-order", payload)


def calculate_signal(df: pd.DataFrame, cfg: Config) -> Dict[str, Any]:
    df = df.copy()
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["signal"] = df["ma5"] - df["ma20"]
    df["acceleration"] = df["signal"] - df["signal"].shift(1)
    df["candle_body_pct"] = (df["close"] - df["open"]) / df["open"]

    last = df.iloc[-1]
    prev = df.iloc[-2]

    signal_pct = float(last["signal"] / last["close"])
    acceleration_pct = float(last["acceleration"] / last["close"])
    required_edge = (cfg.fee_pct * 2) + cfg.slippage_pct + cfg.min_edge_pct
    candle_green = float(last["candle_body_pct"]) > 0

    action = "HOLD"

    # Anti-fee BUY: trend must be strong enough to pay buy fee + sell fee + slippage + extra edge.
    if (
        signal_pct > required_edge
        and acceleration_pct > cfg.min_accel_pct
        and candle_green
    ):
        action = "BUY"
    elif last["signal"] < 0 and last["acceleration"] < 0:
        action = "SELL"

    return {
        "action": action,
        "price": float(last["close"]),
        "ma5": float(last["ma5"]),
        "ma20": float(last["ma20"]),
        "signal": float(last["signal"]),
        "signal_pct": signal_pct,
        "acceleration": float(last["acceleration"]),
        "acceleration_pct": acceleration_pct,
        "previous_signal": float(prev["signal"]),
        "required_edge": required_edge,
        "candle_green": candle_green,
    }


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = BitgetClient(cfg)
        self.in_position = False
        self.entry_price: Optional[float] = None
        self.highest_price: Optional[float] = None
        self.base_amount: float = 0.0
        self.trade_count_today: int = 0
        self.current_day: str = time.strftime("%Y-%m-%d")
        self.cooldown_until: float = 0.0
        self.start_balance: float = self.cfg.trade_quote_size
        self.paper_balance: float = self.cfg.trade_quote_size

    def buy(self, price: float):
        self.base_amount = self.cfg.trade_quote_size / price
        if self.cfg.paper_mode:
            self.paper_balance = self.cfg.trade_quote_size
        self.entry_price = price
        self.highest_price = price
        self.in_position = True
        self.trade_count_today += 1

        if self.cfg.paper_mode:
            print(f"[PAPER BUY] {self.cfg.trade_quote_size} USDT at ~{price:.6f}, base≈{self.base_amount:.8f}")
        else:
            print("[REAL BUY] sending market buy...")
            print(self.client.place_market_buy(self.cfg.trade_quote_size))

    def sell(self, price: float, reason: str):
        if not self.in_position:
            return

        pnl_value = (price - self.entry_price) * self.base_amount if self.entry_price else 0
        pnl_pct = ((price - self.entry_price) / self.entry_price) * 100 if self.entry_price else 0

        if self.cfg.paper_mode:
            self.paper_balance += pnl_value
            print(f"[PAPER SELL] reason={reason}, price≈{price:.6f}, pnl≈{pnl_pct:.3f}%")
        else:
            print(f"[REAL SELL] reason={reason}, sending market sell...")
            print(self.client.place_market_sell(self.base_amount))

        self.in_position = False
        if self.entry_price and price < self.entry_price:
            self.cooldown_until = time.time() + self.cfg.cooldown_after_loss_seconds

        self.entry_price = None
        self.highest_price = None
        self.base_amount = 0.0

    def check_exit_rules(self, price: float) -> Optional[str]:
        if not self.in_position or self.entry_price is None:
            return None

        if self.highest_price is None or price > self.highest_price:
            self.highest_price = price

        change = (price - self.entry_price) / self.entry_price
        pullback_from_high = (self.highest_price - price) / self.highest_price

        if change >= self.cfg.take_profit_pct:
            return "TAKE_PROFIT"
        if change > 0 and pullback_from_high >= self.cfg.trailing_stop_pct:
            return "TRAILING_PROFIT"
        if change <= -self.cfg.stop_loss_pct:
            return "STOP_LOSS"
        return None

    def reset_daily_counter_if_needed(self):
        today = time.strftime("%Y-%m-%d")
        if today != self.current_day:
            self.current_day = today
            self.trade_count_today = 0

    def can_enter_trade(self) -> bool:
        self.reset_daily_counter_if_needed()
        if time.time() < self.cooldown_until:
            return False
        if self.trade_count_today >= self.cfg.max_trades_per_day:
            return False
        return True

    def run_once(self):
        df = self.client.get_candles(limit=100)
        info = calculate_signal(df, self.cfg)
        price = info["price"]

        if self.in_position and self.entry_price is not None:
            pnl = (price - self.entry_price) * self.base_amount
            pnl_pct = ((price - self.entry_price) / self.entry_price) * 100
            tp_price = self.entry_price * (1 + self.cfg.take_profit_pct)
            sl_price = self.entry_price * (1 - self.cfg.stop_loss_pct)
            high = self.highest_price if self.highest_price else price
            trailing_price = high * (1 - self.cfg.trailing_stop_pct)
            line = (
                f"{self.cfg.symbol} | BUY at {self.entry_price:.6f} | NOW {price:.6f} | "
                f"SELL TP at {tp_price:.6f} | TRAIL at {trailing_price:.6f} | STOP at {sl_price:.6f} | "
                f"PROFIT/LOSS {pnl:+.4f} USDT ({pnl_pct:+.3f}%) | BALANCE {self.paper_balance + pnl:.4f} USDT"
            )
        else:
            expected_buy = price
            expected_tp = expected_buy * (1 + self.cfg.take_profit_pct)
            expected_sl = expected_buy * (1 - self.cfg.stop_loss_pct)
            expected_profit = self.cfg.trade_quote_size * self.cfg.take_profit_pct
            expected_loss = self.cfg.trade_quote_size * self.cfg.stop_loss_pct
            status = "READY" if self.can_enter_trade() else "COOLDOWN/LIMIT"
            line = (
                f"{self.cfg.symbol} | {status} | possible BUY at {expected_buy:.6f} | "
                f"SELL TP at {expected_tp:.6f} = profit +{expected_profit:.4f} USDT | "
                f"STOP at {expected_sl:.6f} = loss -{expected_loss:.4f} USDT | "
                f"signal {info['action']} | edge {info['signal_pct']*100:.3f}% / needed {info['required_edge']*100:.3f}% | BALANCE {self.paper_balance:.4f} USDT"
            )

        print(line)

        exit_reason = self.check_exit_rules(price)
        if exit_reason:
            self.sell(price, exit_reason)
            return

        if not self.in_position and info["action"] == "BUY" and self.can_enter_trade():
            self.buy(price)
            return

        if self.in_position and info["action"] == "SELL":
            self.sell(price, "TREND_REVERSAL")

    def run_forever(self):
        print(f"Starting bot | symbol={self.cfg.symbol} | paper_mode={self.cfg.paper_mode}")
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
    config = Config()

    if not config.paper_mode:
        missing = []
        if not config.api_key:
            missing.append("BITGET_API_KEY")
        if not config.api_secret:
            missing.append("BITGET_API_SECRET")
        if not config.passphrase:
            missing.append("BITGET_PASSPHRASE")
        if missing:
            raise SystemExit(f"Missing env vars for real trading: {', '.join(missing)}")

    Bot(config).run_forever()

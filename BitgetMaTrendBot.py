"""
Bitget SPOT bot: MA5/MA20 + acceleration

IMPORTANT:
- Starts in PAPER_MODE=True by default. It will NOT place real orders until you set PAPER_MODE=false in .env.
- Uses Bitget Spot REST API v2.
- Strategy:
    Signal = MA5 - MA20
    Acceleration = Signal_now - Signal_previous
    BUY when Signal > 0 and Acceleration > 0
    SELL when Signal < 0 and Acceleration < 0

Install:
    pip install requests pandas python-dotenv

.env example:
    BITGET_API_KEY=your_key
    BITGET_API_SECRET=your_secret
    BITGET_PASSPHRASE=your_passphrase
    SYMBOL=BTCUSDT
    GRANULARITY=1min
    PAPER_MODE=true
    TRADE_QUOTE_SIZE=10
    TAKE_PROFIT_PCT=0.004
    STOP_LOSS_PCT=0.0025
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
    take_profit_pct: float = float(os.getenv("TAKE_PROFIT_PCT", "0.004"))  # 0.4%
    stop_loss_pct: float = float(os.getenv("STOP_LOSS_PCT", "0.0025"))     # 0.25%
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


def calculate_signal(df: pd.DataFrame) -> Dict[str, Any]:
    df = df.copy()
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["signal"] = df["ma5"] - df["ma20"]
    df["acceleration"] = df["signal"] - df["signal"].shift(1)

    last = df.iloc[-1]
    prev = df.iloc[-2]

    action = "HOLD"
    if last["signal"] > 0 and last["acceleration"] > 0:
        action = "BUY"
    elif last["signal"] < 0 and last["acceleration"] < 0:
        action = "SELL"

    return {
        "action": action,
        "price": float(last["close"]),
        "ma5": float(last["ma5"]),
        "ma20": float(last["ma20"]),
        "signal": float(last["signal"]),
        "acceleration": float(last["acceleration"]),
        "previous_signal": float(prev["signal"]),
    }


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = BitgetClient(cfg)
        self.in_position = False
        self.entry_price: Optional[float] = None
        self.base_amount: float = 0.0

    def buy(self, price: float):
        self.base_amount = self.cfg.trade_quote_size / price
        self.entry_price = price
        self.in_position = True

        if self.cfg.paper_mode:
            print(f"[PAPER BUY] {self.cfg.trade_quote_size} USDT at ~{price:.6f}, base≈{self.base_amount:.8f}")
        else:
            print("[REAL BUY] sending market buy...")
            print(self.client.place_market_buy(self.cfg.trade_quote_size))

    def sell(self, price: float, reason: str):
        if not self.in_position:
            return

        pnl_pct = ((price - self.entry_price) / self.entry_price) * 100 if self.entry_price else 0

        if self.cfg.paper_mode:
            print(f"[PAPER SELL] reason={reason}, price≈{price:.6f}, pnl≈{pnl_pct:.3f}%")
        else:
            print(f"[REAL SELL] reason={reason}, sending market sell...")
            print(self.client.place_market_sell(self.base_amount))

        self.in_position = False
        self.entry_price = None
        self.base_amount = 0.0

    def check_exit_rules(self, price: float) -> Optional[str]:
        if not self.in_position or self.entry_price is None:
            return None

        change = (price - self.entry_price) / self.entry_price
        if change >= self.cfg.take_profit_pct:
            return "TAKE_PROFIT"
        if change <= -self.cfg.stop_loss_pct:
            return "STOP_LOSS"
        return None

    def run_once(self):
        df = self.client.get_candles(limit=100)
        info = calculate_signal(df)
        price = info["price"]

        if self.in_position and self.entry_price is not None:
            pnl = (price - self.entry_price) * self.base_amount
            pnl_pct = ((price - self.entry_price) / self.entry_price) * 100
            tp_price = self.entry_price * (1 + self.cfg.take_profit_pct)
            sl_price = self.entry_price * (1 - self.cfg.stop_loss_pct)
            line = (
                f"{self.cfg.symbol} | BUY at {self.entry_price:.6f} | NOW {price:.6f} | "
                f"SELL TP at {tp_price:.6f} | STOP at {sl_price:.6f} | "
                f"PROFIT/LOSS {pnl:+.4f} USDT ({pnl_pct:+.3f}%)"
            )
        else:
            expected_buy = price
            expected_tp = expected_buy * (1 + self.cfg.take_profit_pct)
            expected_sl = expected_buy * (1 - self.cfg.stop_loss_pct)
            expected_profit = self.cfg.trade_quote_size * self.cfg.take_profit_pct
            expected_loss = self.cfg.trade_quote_size * self.cfg.stop_loss_pct
            line = (
                f"{self.cfg.symbol} | WAIT | possible BUY at {expected_buy:.6f} | "
                f"SELL TP at {expected_tp:.6f} = profit +{expected_profit:.4f} USDT | "
                f"STOP at {expected_sl:.6f} = loss -{expected_loss:.4f} USDT | signal {info['action']}"
            )

        print(line)

        exit_reason = self.check_exit_rules(price)
        if exit_reason:
            self.sell(price, exit_reason)
            return

        if not self.in_position and info["action"] == "BUY":
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

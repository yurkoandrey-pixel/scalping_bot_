"""
Bybit API Client
=================
Исправления:
  B2 — свежая цена + буфер перед отправкой ордера
  B4 — calculate_qty принимает цену, не делает лишний запрос
"""

import time
import hashlib
import hmac
import json
import requests
from typing import Dict, List, Optional, Tuple
import config


class BybitClient:

    def __init__(self):
        self.api_key    = config.API_KEY
        self.api_secret = config.API_SECRET

        self.base_url = (
            "https://api-testnet.bybit.com"
            if config.USE_TESTNET
            else "https://api.bybit.com"
        )

        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

        self._instruments_cache = {}
        self._cache_time = 0

    # ──────────────────────────────────────────────
    # ВНУТРЕННИЕ МЕТОДЫ
    # ──────────────────────────────────────────────

    def _sign(self, timestamp: str, params_str: str) -> str:
        raw = f"{timestamp}{self.api_key}5000{params_str}"
        return hmac.new(
            self.api_secret.encode(),
            raw.encode(),
            hashlib.sha256
        ).hexdigest()

    def _request(self, method: str, endpoint: str,
                 params: Dict = None, signed: bool = False) -> Dict:
        url = f"{self.base_url}{endpoint}"
        if params is None:
            params = {}

        headers = {}

        if signed:
            ts = str(int(time.time() * 1000))
            if method == "GET":
                ps = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
            else:
                ps = json.dumps(params) if params else ""
            headers = {
                "X-BAPI-API-KEY"    : self.api_key,
                "X-BAPI-TIMESTAMP"  : ts,
                "X-BAPI-SIGN"       : self._sign(ts, ps),
                "X-BAPI-RECV-WINDOW": "5000"
            }

        try:
            if method == "GET":
                r = self.session.get(url, params=params,
                                     headers=headers, timeout=5)
            else:
                r = self.session.post(url, json=params,
                                      headers=headers, timeout=5)

            data = r.json()
            if data.get("retCode") not in (0, None):
                pass  # ошибки обрабатываются выше
            return data

        except Exception as e:
            return {"retCode": -1, "retMsg": str(e), "result": {}}

    # ──────────────────────────────────────────────
    # РЫНОЧНЫЕ ДАННЫЕ
    # ──────────────────────────────────────────────

    def get_instruments(self) -> List[Dict]:
        if time.time() - self._cache_time < 300 and self._instruments_cache:
            return list(self._instruments_cache.values())

        r = self._request("GET", "/v5/market/instruments-info", {
            "category": "linear", "limit": 1000
        })
        if r.get("retCode") == 0:
            items = r.get("result", {}).get("list", [])
            self._instruments_cache = {i["symbol"]: i for i in items}
            self._cache_time = time.time()
            return items
        return []

    def get_instrument_info(self, symbol: str) -> Dict:
        if not self._instruments_cache:
            self.get_instruments()
        return self._instruments_cache.get(symbol, {})

    def get_klines(self, symbol: str, interval: str = "1",
                   limit: int = 50) -> List[Dict]:
        r = self._request("GET", "/v5/market/kline", {
            "category": "linear",
            "symbol"  : symbol,
            "interval": interval,
            "limit"   : limit
        })
        if r.get("retCode") == 0:
            raw = r.get("result", {}).get("list", [])
            return [
                {
                    "timestamp": int(k[0]),
                    "open"     : float(k[1]),
                    "high"     : float(k[2]),
                    "low"      : float(k[3]),
                    "close"    : float(k[4]),
                    "volume"   : float(k[5])
                }
                for k in reversed(raw)
            ]
        return []

    def get_ticker(self, symbol: str) -> Dict:
        r = self._request("GET", "/v5/market/tickers", {
            "category": "linear", "symbol": symbol
        })
        if r.get("retCode") == 0:
            items = r.get("result", {}).get("list", [])
            if items:
                return items[0]
        return {}

    def get_current_price(self, symbol: str) -> float:
        t = self.get_ticker(symbol)
        return float(t.get("lastPrice", 0))

    def get_bid_ask(self, symbol: str) -> Tuple[float, float]:
        t = self.get_ticker(symbol)
        return float(t.get("bid1Price", 0)), float(t.get("ask1Price", 0))

    # ──────────────────────────────────────────────
    # АККАУНТ
    # ──────────────────────────────────────────────

    def get_available_balance(self) -> float:
        r = self._request("GET", "/v5/account/wallet-balance",
                          {"accountType": "UNIFIED"}, signed=True)
        if r.get("retCode") == 0:
            accounts = r.get("result", {}).get("list", [])
            if accounts:
                return float(accounts[0].get("totalAvailableBalance", 0))
        return 0.0

    # ──────────────────────────────────────────────
    # ПОЗИЦИИ
    # ──────────────────────────────────────────────

    def get_positions(self, symbol: str = None) -> List[Dict]:
        params = {"category": "linear", "settleCoin": "USDT"}
        if symbol:
            params["symbol"] = symbol
        r = self._request("GET", "/v5/position/list", params, signed=True)
        if r.get("retCode") == 0:
            items = r.get("result", {}).get("list", [])
            return [p for p in items if float(p.get("size", 0)) > 0]
        return []

    def get_position(self, symbol: str) -> Optional[Dict]:
        for p in self.get_positions(symbol):
            if p.get("symbol") == symbol:
                return p
        return None

    def has_position(self, symbol: str) -> bool:
        return self.get_position(symbol) is not None

    # ──────────────────────────────────────────────
    # РЕАЛЬНЫЙ P&L ЗАКРЫТЫХ СДЕЛОК (исправление B1)
    # ──────────────────────────────────────────────

    def get_last_closed_pnl(self, symbol: str) -> Optional[float]:
        """
        Получить P&L последней закрытой позиции с биржи.
        Возвращает процент от размера позиции или None.
        """
        r = self._request("GET", "/v5/position/closed-pnl", {
            "category": "linear",
            "symbol"  : symbol,
            "limit"   : 1
        }, signed=True)

        if r.get("retCode") == 0:
            records = r.get("result", {}).get("list", [])
            if records:
                rec      = records[0]
                closed   = float(rec.get("closedPnl", 0))
                avg_exit = float(rec.get("avgExitPrice", 0))
                avg_entry= float(rec.get("avgEntryPrice", 0))
                size     = float(rec.get("qty", 0))

                # P&L в %
                if avg_entry > 0 and size > 0:
                    pnl_pct = closed / (avg_entry * size) * 100
                    return pnl_pct
        return None

    # ──────────────────────────────────────────────
    # ОРДЕРА
    # ──────────────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        r = self._request("POST", "/v5/position/set-leverage", {
            "category"     : "linear",
            "symbol"       : symbol,
            "buyLeverage"  : str(leverage),
            "sellLeverage" : str(leverage)
        }, signed=True)
        return r.get("retCode") in (0, 110043)

    def place_order(self, symbol: str, side: str, qty: float,
                    take_profit: float = None,
                    stop_loss: float   = None) -> Dict:
        """
        Открыть рыночный ордер с TP и SL.
        FIX B2: цена передаётся снаружи, буфер добавляется в bot.py
        """
        params = {
            "category"   : "linear",
            "symbol"     : symbol,
            "side"       : side,
            "orderType"  : "Market",
            "qty"        : str(qty),
            "timeInForce": "GTC"
        }
        if take_profit:
            params["takeProfit"] = str(take_profit)
        if stop_loss:
            params["stopLoss"] = str(stop_loss)

        return self._request("POST", "/v5/order/create", params, signed=True)

    def set_tp_sl(self, symbol: str,
                  take_profit: float = None,
                  stop_loss: float   = None) -> Dict:
        params = {
            "category"  : "linear",
            "symbol"    : symbol,
            "tpslMode"  : "Full",
            "positionIdx": 0
        }
        if take_profit:
            params["takeProfit"] = str(take_profit)
        if stop_loss:
            params["stopLoss"] = str(stop_loss)
        return self._request("POST", "/v5/position/trading-stop",
                             params, signed=True)

    def close_position(self, symbol: str) -> Dict:
        pos = self.get_position(symbol)
        if not pos:
            return {"retCode": -1, "retMsg": "No position"}

        size       = float(pos.get("size", 0))
        side       = pos.get("side", "")
        close_side = "Sell" if side == "Buy" else "Buy"

        return self._request("POST", "/v5/order/create", {
            "category"   : "linear",
            "symbol"     : symbol,
            "side"       : close_side,
            "orderType"  : "Market",
            "qty"        : str(size),
            "timeInForce": "GTC",
            "reduceOnly" : True
        }, signed=True)

    # ──────────────────────────────────────────────
    # ВСПОМОГАТЕЛЬНЫЕ
    # ──────────────────────────────────────────────

    def calculate_qty(self, symbol: str, usdt_amount: float,
                      price: float = None) -> float:
        """
        Рассчитать количество.
        FIX B4: принимает уже известную цену — не делает лишний запрос.
        """
        if price is None:
            price = self.get_current_price(symbol)
        if price <= 0:
            return 0.0

        qty = usdt_amount / price

        inst = self.get_instrument_info(symbol)
        if inst:
            lsf      = inst.get("lotSizeFilter", {})
            min_qty  = float(lsf.get("minOrderQty", 1))
            qty_step = float(lsf.get("qtyStep", 0.001))
            qty = max(min_qty, round(qty / qty_step) * qty_step)

        return round(qty, 6)

    def round_price(self, symbol: str, price: float) -> float:
        inst = self.get_instrument_info(symbol)
        if inst:
            tick = float(inst.get("priceFilter", {}).get("tickSize", 0.0001))
            return round(round(price / tick) * tick, 10)
        return price

"""
Bybit API Client v3.0
======================
С проверкой реальных позиций и ордеров
"""

import time
import hashlib
import hmac
import json
import requests
from typing import Dict, List, Optional
import config


class BybitClient:
    """Улучшенный клиент для Bybit API v5"""
    
    def __init__(self):
        self.api_key = config.API_KEY
        self.api_secret = config.API_SECRET
        
        if config.USE_TESTNET:
            self.base_url = "https://api-testnet.bybit.com"
        else:
            self.base_url = "https://api.bybit.com"
        
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json"
        })
        
        # Кэш информации об инструментах
        self._instruments_cache = {}
        self._cache_time = 0
    
    def _generate_signature(self, timestamp: str, params: str) -> str:
        """Генерация подписи"""
        param_str = f"{timestamp}{self.api_key}{5000}{params}"
        return hmac.new(
            self.api_secret.encode('utf-8'),
            param_str.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
    
    def _request(self, method: str, endpoint: str, params: Dict = None, signed: bool = False) -> Dict:
        """Выполнение запроса к API"""
        url = f"{self.base_url}{endpoint}"
        
        if params is None:
            params = {}
        
        headers = {}
        
        if signed:
            timestamp = str(int(time.time() * 1000))
            
            if method == "GET":
                param_str = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
            else:
                param_str = json.dumps(params) if params else ""
            
            signature = self._generate_signature(timestamp, param_str)
            
            headers = {
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-SIGN": signature,
                "X-BAPI-RECV-WINDOW": "5000"
            }
        
        try:
            if method == "GET":
                response = self.session.get(url, params=params, headers=headers, timeout=5)
            else:
                response = self.session.post(url, json=params, headers=headers, timeout=5)
            
            data = response.json()
            
            if data.get("retCode") != 0:
                print(f"⚠️ API Error: {data.get('retMsg')}")
            
            return data
            
        except Exception as e:
            print(f"❌ Request Error: {e}")
            return {"retCode": -1, "retMsg": str(e), "result": {}}
    
    # ═══════════════════════════════════════════════════════════
    # MARKET DATA
    # ═══════════════════════════════════════════════════════════
    
    def get_instruments(self) -> List[Dict]:
        """Получить список инструментов (с кэшированием)"""
        # Кэш на 5 минут
        if time.time() - self._cache_time < 300 and self._instruments_cache:
            return list(self._instruments_cache.values())
        
        response = self._request("GET", "/v5/market/instruments-info", {
            "category": "linear",
            "limit": 1000
        })
        
        if response.get("retCode") == 0:
            instruments = response.get("result", {}).get("list", [])
            self._instruments_cache = {i["symbol"]: i for i in instruments}
            self._cache_time = time.time()
            return instruments
        return []
    
    def get_instrument_info(self, symbol: str) -> Dict:
        """Получить информацию об инструменте"""
        if not self._instruments_cache:
            self.get_instruments()
        return self._instruments_cache.get(symbol, {})
    
    def get_klines(self, symbol: str, interval: str = "1", limit: int = 50) -> List[Dict]:
        """Получить свечи"""
        response = self._request("GET", "/v5/market/kline", {
            "category": "linear",
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        })
        
        if response.get("retCode") == 0:
            klines = response.get("result", {}).get("list", [])
            result = []
            for k in reversed(klines):
                result.append({
                    "timestamp": int(k[0]),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5])
                })
            return result
        return []
    
    def get_ticker(self, symbol: str) -> Dict:
        """Получить тикер одной монеты"""
        response = self._request("GET", "/v5/market/tickers", {
            "category": "linear",
            "symbol": symbol
        })
        
        if response.get("retCode") == 0:
            tickers = response.get("result", {}).get("list", [])
            if tickers:
                return tickers[0]
        return {}
    
    def get_current_price(self, symbol: str) -> float:
        """Получить текущую цену"""
        ticker = self.get_ticker(symbol)
        return float(ticker.get("lastPrice", 0))
    
    def get_bid_ask(self, symbol: str) -> tuple:
        """Получить лучшие bid/ask цены"""
        ticker = self.get_ticker(symbol)
        bid = float(ticker.get("bid1Price", 0))
        ask = float(ticker.get("ask1Price", 0))
        return bid, ask
    
    # ═══════════════════════════════════════════════════════════
    # ACCOUNT DATA
    # ═══════════════════════════════════════════════════════════
    
    def get_wallet_balance(self) -> Dict:
        """Получить баланс кошелька"""
        response = self._request("GET", "/v5/account/wallet-balance", {
            "accountType": "UNIFIED"
        }, signed=True)
        
        if response.get("retCode") == 0:
            accounts = response.get("result", {}).get("list", [])
            if accounts:
                return accounts[0]
        return {}
    
    def get_available_balance(self) -> float:
        """Получить доступный баланс"""
        wallet = self.get_wallet_balance()
        return float(wallet.get("totalAvailableBalance", 0))
    
    # ═══════════════════════════════════════════════════════════
    # POSITIONS — РЕАЛЬНЫЕ ПОЗИЦИИ С БИРЖИ
    # ═══════════════════════════════════════════════════════════
    
    def get_positions(self, symbol: str = None) -> List[Dict]:
        """Получить РЕАЛЬНЫЕ открытые позиции с биржи"""
        params = {
            "category": "linear",
            "settleCoin": "USDT"
        }
        if symbol:
            params["symbol"] = symbol
        
        response = self._request("GET", "/v5/position/list", params, signed=True)
        
        if response.get("retCode") == 0:
            positions = response.get("result", {}).get("list", [])
            # Фильтруем только активные позиции (size > 0)
            active = [p for p in positions if float(p.get("size", 0)) > 0]
            return active
        return []
    
    def get_position(self, symbol: str) -> Optional[Dict]:
        """Получить позицию по конкретному символу"""
        positions = self.get_positions(symbol)
        for p in positions:
            if p.get("symbol") == symbol and float(p.get("size", 0)) > 0:
                return p
        return None
    
    def has_position(self, symbol: str) -> bool:
        """Проверить есть ли открытая позиция"""
        return self.get_position(symbol) is not None
    
    # ═══════════════════════════════════════════════════════════
    # ORDERS — РЕАЛЬНЫЕ ОРДЕРА
    # ═══════════════════════════════════════════════════════════
    
    def get_open_orders(self, symbol: str = None) -> List[Dict]:
        """Получить открытые ордера"""
        params = {"category": "linear"}
        if symbol:
            params["symbol"] = symbol
        
        response = self._request("GET", "/v5/order/realtime", params, signed=True)
        
        if response.get("retCode") == 0:
            return response.get("result", {}).get("list", [])
        return []
    
    def has_tp_order(self, symbol: str) -> bool:
        """Проверить есть ли TP ордер для позиции"""
        position = self.get_position(symbol)
        if position:
            tp = position.get("takeProfit", "")
            return tp and float(tp) > 0
        return False
    
    # ═══════════════════════════════════════════════════════════
    # TRADING
    # ═══════════════════════════════════════════════════════════
    
    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Установить плечо"""
        params = {
            "category": "linear",
            "symbol": symbol,
            "buyLeverage": str(leverage),
            "sellLeverage": str(leverage)
        }
        response = self._request("POST", "/v5/position/set-leverage", params, signed=True)
        # Игнорируем ошибку "leverage not modified"
        return response.get("retCode") in [0, 110043]
    
    def place_order(self, symbol: str, side: str, qty: float,
                    order_type: str = "Market", take_profit: float = None) -> Dict:
        """
        Создать ордер с TP
        """
        params = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": str(qty),
            "timeInForce": "GTC"
        }
        
        if take_profit:
            params["takeProfit"] = str(take_profit)
        
        return self._request("POST", "/v5/order/create", params, signed=True)
    
    def set_tp_sl(self, symbol: str, take_profit: float = None, stop_loss: float = None) -> Dict:
        """Установить TP/SL для существующей позиции"""
        params = {
            "category": "linear",
            "symbol": symbol,
            "tpslMode": "Full",
            "positionIdx": 0
        }
        
        if take_profit:
            params["takeProfit"] = str(take_profit)
        if stop_loss:
            params["stopLoss"] = str(stop_loss)
        
        return self._request("POST", "/v5/position/trading-stop", params, signed=True)
    
    def close_position(self, symbol: str) -> Dict:
        """Закрыть позицию полностью"""
        position = self.get_position(symbol)
        if not position:
            return {"retCode": -1, "retMsg": "No position found"}
        
        size = float(position.get("size", 0))
        side = position.get("side", "")
        
        # Для закрытия: Buy → Sell, Sell → Buy
        close_side = "Sell" if side == "Buy" else "Buy"
        
        params = {
            "category": "linear",
            "symbol": symbol,
            "side": close_side,
            "orderType": "Market",
            "qty": str(size),
            "timeInForce": "GTC",
            "reduceOnly": True
        }
        
        return self._request("POST", "/v5/order/create", params, signed=True)
    
    # ═══════════════════════════════════════════════════════════
    # HELPERS
    # ═══════════════════════════════════════════════════════════
    
    def calculate_qty(self, symbol: str, usdt_amount: float) -> float:
        """Рассчитать количество для ордера"""
        price = self.get_current_price(symbol)
        if price <= 0:
            return 0.0
        
        qty = usdt_amount / price
        
        # Округляем до правильного шага
        inst = self.get_instrument_info(symbol)
        if inst:
            min_qty = float(inst.get("lotSizeFilter", {}).get("minOrderQty", 1))
            qty_step = float(inst.get("lotSizeFilter", {}).get("qtyStep", 0.001))
            qty = max(min_qty, round(qty / qty_step) * qty_step)
        
        return round(qty, 6)
    
    def round_price(self, symbol: str, price: float) -> float:
        """Округлить цену до правильного шага"""
        inst = self.get_instrument_info(symbol)
        if inst:
            tick_size = float(inst.get("priceFilter", {}).get("tickSize", 0.0001))
            return round(price / tick_size) * tick_size
        return price


# Тест
if __name__ == "__main__":
    client = BybitClient()
    
    print("🔗 Тест подключения...")
    print(f"📡 URL: {client.base_url}")
    
    # Баланс
    balance = client.get_available_balance()
    print(f"💰 Баланс: ${balance:.2f}")
    
    # Позиции
    positions = client.get_positions()
    print(f"📊 Открытых позиций: {len(positions)}")
    
    # Цена
    price = client.get_current_price("BTCUSDT")
    print(f"₿ BTC цена: ${price:.2f}")

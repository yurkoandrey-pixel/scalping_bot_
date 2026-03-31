"""
Клиент для работы с Bybit API v5
================================
"""

import time
import hashlib
import hmac
import requests
from typing import Dict, List, Optional
import config


class BybitClient:
    """Клиент для Bybit API v5"""
    
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
    
    def _generate_signature(self, timestamp: str, params: str) -> str:
        """Генерация подписи для запроса"""
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
                import json
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
                response = self.session.get(url, params=params, headers=headers, timeout=10)
            else:
                import json
                response = self.session.post(url, json=params, headers=headers, timeout=10)
            
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
    
    def get_instruments(self, category: str = "linear") -> List[Dict]:
        """Получить список всех торговых инструментов"""
        response = self._request("GET", "/v5/market/instruments-info", {
            "category": category,
            "limit": 1000
        })
        
        if response.get("retCode") == 0:
            return response.get("result", {}).get("list", [])
        return []
    
    def get_tickers(self, category: str = "linear") -> List[Dict]:
        """Получить тикеры всех монет"""
        response = self._request("GET", "/v5/market/tickers", {
            "category": category
        })
        
        if response.get("retCode") == 0:
            return response.get("result", {}).get("list", [])
        return []
    
    def get_klines(self, symbol: str, interval: str = "1", limit: int = 50) -> List[Dict]:
        """Получить свечи (klines)"""
        response = self._request("GET", "/v5/market/kline", {
            "category": "linear",
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        })
        
        if response.get("retCode") == 0:
            klines = response.get("result", {}).get("list", [])
            # Bybit возвращает в обратном порядке (новые первые)
            # Конвертируем в формат: [timestamp, open, high, low, close, volume]
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
    
    def get_orderbook(self, symbol: str, limit: int = 25) -> Dict:
        """Получить стакан"""
        response = self._request("GET", "/v5/market/orderbook", {
            "category": "linear",
            "symbol": symbol,
            "limit": limit
        })
        
        if response.get("retCode") == 0:
            return response.get("result", {})
        return {}
    
    # ═══════════════════════════════════════════════════════════
    # ACCOUNT DATA (требует подпись)
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
    
    def get_positions(self, symbol: str = None) -> List[Dict]:
        """Получить открытые позиции"""
        params = {
            "category": "linear",
            "settleCoin": "USDT"
        }
        if symbol:
            params["symbol"] = symbol
        
        response = self._request("GET", "/v5/position/list", params, signed=True)
        
        if response.get("retCode") == 0:
            return response.get("result", {}).get("list", [])
        return []
    
    def get_open_orders(self, symbol: str = None) -> List[Dict]:
        """Получить открытые ордера"""
        params = {
            "category": "linear"
        }
        if symbol:
            params["symbol"] = symbol
        
        response = self._request("GET", "/v5/order/realtime", params, signed=True)
        
        if response.get("retCode") == 0:
            return response.get("result", {}).get("list", [])
        return []
    
    # ═══════════════════════════════════════════════════════════
    # TRADING (требует подпись + права Trade)
    # ═══════════════════════════════════════════════════════════
    
    def place_order(self, symbol: str, side: str, qty: float, 
                    order_type: str = "Market", price: float = None,
                    take_profit: float = None, stop_loss: float = None) -> Dict:
        """
        Создать ордер
        
        Args:
            symbol: Символ (например, "BTCUSDT")
            side: "Buy" или "Sell"
            qty: Количество
            order_type: "Market" или "Limit"
            price: Цена (для Limit ордера)
            take_profit: Цена Take Profit
            stop_loss: Цена Stop Loss
        """
        params = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": str(qty),
            "timeInForce": "GTC"
        }
        
        if price and order_type == "Limit":
            params["price"] = str(price)
        
        if take_profit:
            params["takeProfit"] = str(take_profit)
        
        if stop_loss:
            params["stopLoss"] = str(stop_loss)
        
        return self._request("POST", "/v5/order/create", params, signed=True)
    
    def set_leverage(self, symbol: str, leverage: int) -> Dict:
        """Установить плечо для символа"""
        params = {
            "category": "linear",
            "symbol": symbol,
            "buyLeverage": str(leverage),
            "sellLeverage": str(leverage)
        }
        return self._request("POST", "/v5/position/set-leverage", params, signed=True)
    
    def cancel_order(self, symbol: str, order_id: str) -> Dict:
        """Отменить ордер"""
        params = {
            "category": "linear",
            "symbol": symbol,
            "orderId": order_id
        }
        return self._request("POST", "/v5/order/cancel", params, signed=True)
    
    def close_position(self, symbol: str, side: str, qty: float) -> Dict:
        """Закрыть позицию"""
        # Для закрытия Long — нужен Sell, для Short — Buy
        close_side = "Sell" if side == "Buy" else "Buy"
        return self.place_order(symbol, close_side, qty)
    
    # ═══════════════════════════════════════════════════════════
    # HELPER METHODS
    # ═══════════════════════════════════════════════════════════
    
    def get_usdt_balance(self) -> float:
        """Получить баланс USDT"""
        wallet = self.get_wallet_balance()
        coins = wallet.get("coin", [])
        
        for coin in coins:
            if coin.get("coin") == "USDT":
                return float(coin.get("walletBalance", 0))
        return 0.0
    
    def get_available_balance(self) -> float:
        """Получить доступный баланс"""
        wallet = self.get_wallet_balance()
        return float(wallet.get("totalAvailableBalance", 0))
    
    def get_current_price(self, symbol: str) -> float:
        """Получить текущую цену"""
        response = self._request("GET", "/v5/market/tickers", {
            "category": "linear",
            "symbol": symbol
        })
        
        if response.get("retCode") == 0:
            tickers = response.get("result", {}).get("list", [])
            if tickers:
                return float(tickers[0].get("lastPrice", 0))
        return 0.0


# Тест подключения
if __name__ == "__main__":
    client = BybitClient()
    
    print("🔗 Тестирование подключения к Bybit...")
    print(f"📡 URL: {client.base_url}")
    
    # Тест публичного API
    tickers = client.get_tickers()
    print(f"✅ Получено {len(tickers)} тикеров")
    
    # Тест приватного API
    balance = client.get_usdt_balance()
    print(f"💰 Баланс USDT: ${balance:.2f}")
    
    # Тест получения свечей
    klines = client.get_klines("BTCUSDT", "1", 10)
    print(f"📊 Получено {len(klines)} свечей BTCUSDT")

#!/usr/bin/env python3
"""
🤖 СКАЛЬПИНГ БОТ — РЕАЛЬНАЯ ТОРГОВЛЯ
=====================================
Сканирует рынок Bybit, находит сигналы SAR,
открывает РЕАЛЬНЫЕ сделки с Take Profit.

Запуск: python bot.py
"""

import time
import sys
from datetime import datetime
from typing import Dict, List, Optional
import config
from bybit_client import BybitClient
from strategy import (
    calculate_parabolic_sar,
    detect_sar_signal,
    calculate_volatility,
    check_tp_hit,
    check_sl_hit
)


class ScalpingBot:
    """Скальпинг бот с реальной торговлей"""
    
    def __init__(self):
        self.client = BybitClient()
        self.active_coins: List[str] = []
        self.positions: Dict[str, Dict] = {}  # symbol -> position
        self.closed_trades: List[Dict] = []
        self.stats = {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl_percent": 0.0,
            "start_time": datetime.now()
        }
        self.last_coin_update = 0
        self.log_file = None
        self.balance = 0.0
        
        if config.SAVE_LOGS:
            self.log_file = open(config.LOG_FILE, "a", encoding="utf-8")
    
    def log(self, message: str, force: bool = False):
        """Логирование"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        full_message = f"[{timestamp}] {message}"
        
        if config.VERBOSE or force:
            print(full_message)
        
        if self.log_file:
            self.log_file.write(full_message + "\n")
            self.log_file.flush()
    
    def update_balance(self):
        """Обновить баланс"""
        self.balance = self.client.get_available_balance()
        return self.balance
    
    def get_instrument_info(self, symbol: str) -> Dict:
        """Получить информацию об инструменте (минимальный размер, шаг цены и т.д.)"""
        instruments = self.client.get_instruments()
        for inst in instruments:
            if inst.get("symbol") == symbol:
                return inst
        return {}
    
    def calculate_quantity(self, symbol: str, price: float) -> float:
        """Рассчитать количество для ордера"""
        # Получаем баланс
        balance = self.update_balance()
        
        if balance <= 0:
            self.log(f"❌ Недостаточно баланса: ${balance:.2f}", force=True)
            return 0.0
        
        # Размер позиции в USDT
        position_usdt = balance * config.POSITION_SIZE_PERCENT
        
        # Количество с учётом плеча
        qty = (position_usdt * config.LEVERAGE) / price
        
        # Получаем минимальный размер и шаг
        inst = self.get_instrument_info(symbol)
        if inst:
            min_qty = float(inst.get("lotSizeFilter", {}).get("minOrderQty", 1))
            qty_step = float(inst.get("lotSizeFilter", {}).get("qtyStep", 0.001))
            
            # Округляем до шага
            qty = max(min_qty, round(qty / qty_step) * qty_step)
        
        return round(qty, 6)
    
    def update_coin_list(self):
        """Обновить список монет для торговли"""
        self.log("🔄 Обновляю список монет...")
        
        # Получаем все тикеры
        tickers = self.client.get_tickers()
        
        if not tickers:
            self.log("❌ Не удалось получить тикеры")
            return
        
        qualified_coins = []
        
        for ticker in tickers:
            symbol = ticker.get("symbol", "")
            
            # Пропускаем если не USDT пара
            if not symbol.endswith("USDT"):
                continue
            
            # Пропускаем чёрный список
            if symbol in config.BLACKLIST:
                continue
            
            # Проверяем объём за 24ч
            volume_24h = float(ticker.get("turnover24h", 0))
            if volume_24h < config.MIN_VOLUME_24H:
                continue
            
            # Получаем свечи для проверки волатильности
            klines = self.client.get_klines(symbol, config.TIMEFRAME, 20)
            
            if len(klines) < 10:
                continue
            
            # Рассчитываем волатильность
            volatility = calculate_volatility(klines)
            
            # Проверяем волатильность
            if config.MIN_VOLATILITY <= volatility <= config.MAX_VOLATILITY:
                qualified_coins.append({
                    "symbol": symbol,
                    "volatility": volatility,
                    "volume_24h": volume_24h
                })
        
        # Сортируем по волатильности (больше = лучше для скальпинга)
        qualified_coins.sort(key=lambda x: x["volatility"], reverse=True)
        
        # Берём топ 50
        self.active_coins = [c["symbol"] for c in qualified_coins[:50]]
        
        self.log(f"✅ Найдено {len(self.active_coins)} подходящих монет", force=True)
        
        if config.VERBOSE and self.active_coins[:5]:
            top5 = [c["symbol"] for c in qualified_coins[:5]]
            self.log(f"   Топ 5: {', '.join(top5)}")
        
        self.last_coin_update = time.time()
    
    def scan_for_signals(self) -> List[Dict]:
        """Сканировать все монеты на сигналы"""
        signals = []
        
        for symbol in self.active_coins:
            # Пропускаем если уже есть позиция
            if symbol in self.positions:
                continue
            
            # Получаем свечи
            klines = self.client.get_klines(symbol, config.TIMEFRAME, config.CANDLES_LIMIT)
            
            if len(klines) < 10:
                continue
            
            # Рассчитываем SAR
            klines_with_sar = calculate_parabolic_sar(klines)
            
            # Ищем сигнал
            signal = detect_sar_signal(klines_with_sar)
            
            if signal:
                signal["symbol"] = symbol
                signals.append(signal)
        
        return signals
    
    def open_position(self, signal: Dict):
        """Открыть позицию (реальную или виртуальную)"""
        symbol = signal["symbol"]
        
        if config.LIVE_TRADING:
            # ═══════════════════════════════════════════════════
            # РЕАЛЬНАЯ ТОРГОВЛЯ
            # ═══════════════════════════════════════════════════
            
            try:
                # Получаем текущую цену
                current_price = self.client.get_current_price(symbol)
                if current_price <= 0:
                    self.log(f"❌ Не удалось получить цену {symbol}", force=True)
                    return
                
                # Рассчитываем количество
                qty = self.calculate_quantity(symbol, current_price)
                if qty <= 0:
                    self.log(f"❌ Недостаточно средств для {symbol}", force=True)
                    return
                
                # Устанавливаем плечо
                self.client.set_leverage(symbol, config.LEVERAGE)
                
                # Определяем направление
                side = "Buy" if signal["type"] == "LONG" else "Sell"
                
                # Рассчитываем TP
                if signal["type"] == "LONG":
                    tp_price = current_price * (1 + config.TP_PERCENT / 100)
                else:
                    tp_price = current_price * (1 - config.TP_PERCENT / 100)
                
                # Округляем TP до правильного шага цены
                inst = self.get_instrument_info(symbol)
                if inst:
                    tick_size = float(inst.get("priceFilter", {}).get("tickSize", 0.0001))
                    tp_price = round(tp_price / tick_size) * tick_size
                
                # Открываем ордер
                result = self.client.place_order(
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    order_type="Market",
                    take_profit=tp_price
                )
                
                if result.get("retCode") == 0:
                    # Успешно открыли позицию
                    order_id = result.get("result", {}).get("orderId", "")
                    
                    position = {
                        "symbol": symbol,
                        "type": signal["type"],
                        "side": side,
                        "entry_price": current_price,
                        "tp_price": tp_price,
                        "qty": qty,
                        "order_id": order_id,
                        "entry_time": datetime.now(),
                        "is_real": True
                    }
                    
                    self.positions[symbol] = position
                    
                    # Красивый вывод
                    emoji = "🟢" if signal["type"] == "LONG" else "🔴"
                    
                    self.log("━" * 60, force=True)
                    self.log(f"🔔 РЕАЛЬНАЯ СДЕЛКА: {symbol} | {emoji} {signal['type']}", force=True)
                    self.log(f"   📍 Вход: {current_price:.8f}", force=True)
                    self.log(f"   🎯 TP: {tp_price:.8f} (+{config.TP_PERCENT}%)", force=True)
                    self.log(f"   📊 Размер: {qty} ({qty * current_price:.2f} USDT)", force=True)
                    self.log(f"   💰 ОРДЕР ОТКРЫТ!", force=True)
                    self.log("━" * 60, force=True)
                    
                else:
                    error_msg = result.get("retMsg", "Unknown error")
                    self.log(f"❌ Ошибка открытия {symbol}: {error_msg}", force=True)
                    
            except Exception as e:
                self.log(f"❌ Исключение при открытии {symbol}: {e}", force=True)
        
        else:
            # ═══════════════════════════════════════════════════
            # СИМУЛЯЦИЯ
            # ═══════════════════════════════════════════════════
            
            position = {
                "symbol": symbol,
                "type": signal["type"],
                "entry_price": signal["price"],
                "tp_price": signal["tp"],
                "entry_time": datetime.now(),
                "candle_time": signal.get("candle_time"),
                "is_real": False
            }
            
            self.positions[symbol] = position
            
            # Красивый вывод
            emoji = "🟢" if signal["type"] == "LONG" else "🔴"
            tp_diff = abs(signal["tp"] - signal["price"]) / signal["price"] * 100
            
            self.log("━" * 60, force=True)
            self.log(f"🔔 СИГНАЛ: {symbol} | {emoji} {signal['type']}", force=True)
            self.log(f"   📍 Вход: {signal['price']:.8f}", force=True)
            self.log(f"   🎯 TP: {signal['tp']:.8f} (+{tp_diff:.2f}%)", force=True)
            self.log("━" * 60, force=True)
    
    def check_positions(self):
        """Проверить позиции на достижение TP/SL"""
        positions_to_close = []
        
        for symbol, position in self.positions.items():
            # Получаем текущую цену
            current_price = self.client.get_current_price(symbol)
            
            if current_price <= 0:
                continue
            
            entry_price = position["entry_price"]
            direction = position["type"]
            
            # Проверяем TP
            tp_hit, pnl_percent = check_tp_hit(entry_price, current_price, direction)
            
            if tp_hit:
                positions_to_close.append({
                    "symbol": symbol,
                    "reason": "TP",
                    "pnl_percent": pnl_percent,
                    "exit_price": current_price
                })
                continue
            
            # Проверяем SL (если включён)
            if config.SL_PERCENT > 0:
                sl_hit, _ = check_sl_hit(entry_price, current_price, direction)
                if sl_hit:
                    positions_to_close.append({
                        "symbol": symbol,
                        "reason": "SL",
                        "pnl_percent": pnl_percent,
                        "exit_price": current_price
                    })
        
        # Закрываем позиции
        for close_info in positions_to_close:
            self.close_position(close_info)
    
    def close_position(self, close_info: Dict):
        """Закрыть позицию"""
        symbol = close_info["symbol"]
        position = self.positions.pop(symbol)
        
        # Записываем сделку
        trade = {
            "symbol": symbol,
            "type": position["type"],
            "entry_price": position["entry_price"],
            "exit_price": close_info["exit_price"],
            "pnl_percent": close_info["pnl_percent"],
            "reason": close_info["reason"],
            "entry_time": position["entry_time"],
            "exit_time": datetime.now(),
            "duration": (datetime.now() - position["entry_time"]).total_seconds(),
            "is_real": position.get("is_real", False)
        }
        
        self.closed_trades.append(trade)
        
        # Обновляем статистику
        self.stats["total_trades"] += 1
        self.stats["total_pnl_percent"] += close_info["pnl_percent"]
        
        if close_info["pnl_percent"] > 0:
            self.stats["wins"] += 1
        else:
            self.stats["losses"] += 1
        
        # Красивый вывод
        emoji = "✅" if close_info["pnl_percent"] > 0 else "❌"
        pnl_sign = "+" if close_info["pnl_percent"] > 0 else ""
        duration = trade["duration"]
        real_tag = "💰 РЕАЛЬНАЯ" if trade["is_real"] else ""
        
        self.log("━" * 60, force=True)
        self.log(f"{emoji} ЗАКРЫТО: {symbol} | {close_info['reason']} {real_tag}", force=True)
        self.log(f"   📊 P&L: {pnl_sign}{close_info['pnl_percent']:.2f}%", force=True)
        self.log(f"   ⏱️ Время: {duration:.0f} сек", force=True)
        self.log("━" * 60, force=True)
        
        self.print_stats()
    
    def print_stats(self):
        """Вывести статистику"""
        total = self.stats["total_trades"]
        wins = self.stats["wins"]
        losses = self.stats["losses"]
        pnl = self.stats["total_pnl_percent"]
        
        if total == 0:
            return
        
        win_rate = wins / total * 100
        avg_pnl = pnl / total
        
        self.log("", force=True)
        self.log("📊 СТАТИСТИКА:", force=True)
        self.log(f"   Сделок: {total} | Win: {wins} | Loss: {losses}", force=True)
        self.log(f"   Win Rate: {win_rate:.1f}%", force=True)
        self.log(f"   Общий P&L: {'+' if pnl > 0 else ''}{pnl:.2f}%", force=True)
        self.log(f"   Средний P&L: {'+' if avg_pnl > 0 else ''}{avg_pnl:.2f}%", force=True)
        self.log("", force=True)
    
    def print_open_positions(self):
        """Вывести открытые позиции"""
        if not self.positions:
            return
        
        self.log(f"📂 Открытых позиций: {len(self.positions)}", force=True)
        
        for symbol, pos in self.positions.items():
            current_price = self.client.get_current_price(symbol)
            if current_price > 0:
                _, pnl = check_tp_hit(pos["entry_price"], current_price, pos["type"])
                emoji = "🟢" if pos["type"] == "LONG" else "🔴"
                pnl_sign = "+" if pnl > 0 else ""
                real_tag = "💰" if pos.get("is_real") else ""
                self.log(f"   {emoji} {symbol}: {pnl_sign}{pnl:.2f}% {real_tag}", force=True)
    
    def run(self):
        """Главный цикл бота"""
        mode = "РЕАЛЬНАЯ ТОРГОВЛЯ" if config.LIVE_TRADING else "СИМУЛЯЦИЯ"
        
        print()
        print("═" * 60)
        print("🤖 СКАЛЬПИНГ БОТ ЗАПУЩЕН")
        print("═" * 60)
        print(f"   Режим: {mode}")
        print(f"   TP: {config.TP_PERCENT}%")
        print(f"   SL: {config.SL_PERCENT}%" if config.SL_PERCENT > 0 else "   SL: Выключен")
        print(f"   Волатильность: {config.MIN_VOLATILITY}% - {config.MAX_VOLATILITY}%")
        
        if config.LIVE_TRADING:
            balance = self.update_balance()
            print(f"   💰 Баланс: ${balance:.2f}")
        
        print("═" * 60)
        print()
        print("Для остановки нажми Ctrl+C")
        print()
        
        # Начальное обновление списка монет
        self.update_coin_list()
        
        scan_count = 0
        
        try:
            while True:
                # Обновляем список монет периодически
                if time.time() - self.last_coin_update > config.COIN_UPDATE_INTERVAL:
                    self.update_coin_list()
                
                # Сканируем на сигналы
                signals = self.scan_for_signals()
                
                # Открываем позиции по сигналам
                for signal in signals:
                    self.open_position(signal)
                
                # Проверяем открытые позиции
                self.check_positions()
                
                # Периодический отчёт
                scan_count += 1
                if scan_count % 12 == 0:  # Каждые ~2 минуты
                    self.print_open_positions()
                
                # Пауза
                time.sleep(config.SCAN_INTERVAL)
                
        except KeyboardInterrupt:
            print()
            print("═" * 60)
            print("🛑 БОТ ОСТАНОВЛЕН")
            print("═" * 60)
            self.print_stats()
            
            if self.log_file:
                self.log_file.close()


def main():
    """Точка входа"""
    # Проверяем подключение
    print("🔗 Проверка подключения к Bybit...")
    
    client = BybitClient()
    tickers = client.get_tickers()
    
    if not tickers:
        print("❌ Не удалось подключиться к Bybit")
        print("   Проверь интернет и API ключи")
        sys.exit(1)
    
    print(f"✅ Подключено! Доступно {len(tickers)} торговых пар")
    
    # Запускаем бота
    bot = ScalpingBot()
    bot.run()


if __name__ == "__main__":
    main()

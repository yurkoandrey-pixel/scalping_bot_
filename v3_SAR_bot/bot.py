#!/usr/bin/env python3
"""
🤖 СКАЛЬПИНГ БОТ v3.1
======================
• Таймфрейм 5m (или из config)
• Проверка РЕАЛЬНЫХ позиций через API
• Правильный расчёт TP с валидацией
• TRAILING TP — TP следует за ценой
• Лимит одновременных позиций
• Только проверенные монеты

Запуск: python3 bot.py
"""

import time
import sys
from datetime import datetime
from typing import Dict, List, Optional
import config
from bybit_client import BybitClient
from strategy import (
    calculate_parabolic_sar,
    detect_sar_reversal,
    calculate_tp_price,
    check_pnl,
    validate_tp
)


class ScalpingBotV3:
    """Скальпинг бот v3.1 с Trailing TP"""
    
    def __init__(self):
        self.client = BybitClient()
        self.tracked_positions: Dict[str, Dict] = {}
        self.last_signals: Dict[str, int] = {}
        self.stats = {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
            "start_time": datetime.now()
        }
        self.log_file = None
        
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
    
    def get_open_positions_count(self) -> int:
        """Получить количество РЕАЛЬНЫХ открытых позиций"""
        if config.LIVE_TRADING:
            positions = self.client.get_positions()
            count = sum(1 for p in positions if p.get("symbol") in config.WHITELIST)
            return count
        else:
            return len(self.tracked_positions)
    
    def can_open_position(self) -> bool:
        """Можно ли открыть новую позицию"""
        return self.get_open_positions_count() < config.MAX_POSITIONS
    
    def has_recent_signal(self, symbol: str, candle_timestamp: int) -> bool:
        """Проверить был ли уже сигнал на этой свече"""
        last = self.last_signals.get(symbol, 0)
        return candle_timestamp <= last
    
    def scan_for_signals(self) -> List[Dict]:
        """Сканировать монеты на сигналы SAR"""
        signals = []
        
        for symbol in config.WHITELIST:
            # Пропускаем если уже есть позиция
            if config.LIVE_TRADING:
                if self.client.has_position(symbol):
                    continue
            else:
                if symbol in self.tracked_positions:
                    continue
            
            # Получаем свечи
            klines = self.client.get_klines(symbol, config.TIMEFRAME, config.CANDLES_LIMIT)
            
            if len(klines) < 10:
                continue
            
            # Рассчитываем SAR
            klines_with_sar = calculate_parabolic_sar(klines)
            
            # Ищем разворот
            signal = detect_sar_reversal(klines_with_sar)
            
            if signal:
                # Проверяем что это новый сигнал
                if self.has_recent_signal(symbol, signal["timestamp"]):
                    continue
                
                signal["symbol"] = symbol
                signals.append(signal)
        
        return signals
    
    def open_position(self, signal: Dict) -> bool:
        """Открыть позицию"""
        symbol = signal["symbol"]
        direction = signal["type"]
        
        start_time = time.time()
        
        if config.LIVE_TRADING:
            # ═══════════════════════════════════════════════════
            # РЕАЛЬНАЯ ТОРГОВЛЯ
            # ═══════════════════════════════════════════════════
            
            try:
                # 1. Получаем актуальную цену
                bid, ask = self.client.get_bid_ask(symbol)
                if bid <= 0 or ask <= 0:
                    self.log(f"❌ {symbol}: Не удалось получить цену", force=True)
                    return False
                
                # Цена входа: для LONG берём ask, для SHORT берём bid
                entry_price = ask if direction == "LONG" else bid
                
                # 2. Рассчитываем размер позиции
                balance = self.client.get_available_balance()
                position_usdt = balance * config.POSITION_SIZE_PERCENT * config.LEVERAGE
                qty = self.client.calculate_qty(symbol, position_usdt)
                
                if qty <= 0:
                    self.log(f"❌ {symbol}: Недостаточно средств", force=True)
                    return False
                
                # 3. Рассчитываем TP
                tp_price = calculate_tp_price(entry_price, direction, config.TP_PERCENT)
                tp_price = self.client.round_price(symbol, tp_price)
                
                # ВАЛИДАЦИЯ TP!
                if not validate_tp(entry_price, tp_price, direction):
                    self.log(f"❌ {symbol}: Неверный TP! Entry={entry_price}, TP={tp_price}, Dir={direction}", force=True)
                    # Пересчитываем
                    if direction == "LONG":
                        tp_price = entry_price * (1 + config.TP_PERCENT / 100)
                    else:
                        tp_price = entry_price * (1 - config.TP_PERCENT / 100)
                    tp_price = self.client.round_price(symbol, tp_price)
                    self.log(f"   Пересчитан TP: {tp_price}", force=True)
                
                # 4. Устанавливаем плечо
                self.client.set_leverage(symbol, config.LEVERAGE)
                
                # 5. Открываем ордер с TP
                side = "Buy" if direction == "LONG" else "Sell"
                result = self.client.place_order(
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    order_type="Market",
                    take_profit=tp_price
                )
                
                elapsed = time.time() - start_time
                
                if result.get("retCode") != 0:
                    error = result.get("retMsg", "Unknown error")
                    self.log(f"❌ {symbol}: Ошибка открытия — {error}", force=True)
                    return False
                
                # 6. Проверяем что позиция открылась
                time.sleep(0.5)
                
                position = self.client.get_position(symbol)
                if not position:
                    self.log(f"❌ {symbol}: Позиция не открылась!", force=True)
                    return False
                
                # 7. Проверяем TP
                real_tp = position.get("takeProfit", "")
                if not real_tp or float(real_tp) == 0:
                    self.log(f"⚠️ {symbol}: TP не выставлен! Выставляю...", force=True)
                    tp_result = self.client.set_tp_sl(symbol, take_profit=tp_price)
                    
                    if tp_result.get("retCode") != 0:
                        self.log(f"❌ {symbol}: Не удалось выставить TP!", force=True)
                        if config.CLOSE_IF_NO_TP:
                            self.client.close_position(symbol)
                        return False
                
                # 8. Записываем позицию
                real_entry = float(position.get("avgPrice", entry_price))
                real_size = float(position.get("size", qty))
                
                self.tracked_positions[symbol] = {
                    "symbol": symbol,
                    "type": direction,
                    "side": side,
                    "entry_price": real_entry,
                    "tp_price": tp_price,
                    "best_price": real_entry,  # Для trailing
                    "qty": real_size,
                    "entry_time": datetime.now(),
                    "is_real": True
                }
                
                self.last_signals[symbol] = signal["timestamp"]
                
                # Вывод
                emoji = "🟢" if direction == "LONG" else "🔴"
                value = real_size * real_entry
                
                self.log("━" * 60, force=True)
                self.log(f"💰 СДЕЛКА: {symbol} | {emoji} {direction}", force=True)
                self.log(f"   📍 Вход: {real_entry:.8f}", force=True)
                self.log(f"   🎯 TP: {tp_price:.8f} (+{config.TP_PERCENT}%)", force=True)
                self.log(f"   📊 Размер: {real_size:.4f} ({value:.2f} USDT)", force=True)
                self.log(f"   ⏱️ Вход за: {elapsed:.2f} сек", force=True)
                self.log("━" * 60, force=True)
                
                return True
                
            except Exception as e:
                self.log(f"❌ {symbol}: Исключение — {e}", force=True)
                return False
        
        else:
            # ═══════════════════════════════════════════════════
            # СИМУЛЯЦИЯ
            # ═══════════════════════════════════════════════════
            
            entry_price = signal["price"]
            tp_price = calculate_tp_price(entry_price, direction)
            
            self.tracked_positions[symbol] = {
                "symbol": symbol,
                "type": direction,
                "entry_price": entry_price,
                "tp_price": tp_price,
                "best_price": entry_price,
                "entry_time": datetime.now(),
                "is_real": False
            }
            
            self.last_signals[symbol] = signal["timestamp"]
            
            emoji = "🟢" if direction == "LONG" else "🔴"
            
            self.log("━" * 60, force=True)
            self.log(f"🔔 СИГНАЛ: {symbol} | {emoji} {direction}", force=True)
            self.log(f"   📍 Вход: {entry_price:.8f}", force=True)
            self.log(f"   🎯 TP: {tp_price:.8f} (+{config.TP_PERCENT}%)", force=True)
            self.log("━" * 60, force=True)
            
            return True
    
    def update_trailing_tp(self, symbol: str, current_price: float):
        """
        Обновить Trailing TP
        
        Если цена ушла в плюс на TRAIL_STEP, подтягиваем TP
        """
        if not hasattr(config, 'TRAILING_TP') or not config.TRAILING_TP:
            return
        
        if symbol not in self.tracked_positions:
            return
        
        pos = self.tracked_positions[symbol]
        direction = pos["type"]
        entry_price = pos["entry_price"]
        best_price = pos.get("best_price", entry_price)
        current_tp = pos["tp_price"]
        
        # Проверяем нужно ли обновить best_price
        if direction == "LONG":
            if current_price > best_price:
                pos["best_price"] = current_price
                best_price = current_price
        else:  # SHORT
            if current_price < best_price:
                pos["best_price"] = current_price
                best_price = current_price
        
        # Рассчитываем новый TP
        trail_distance = getattr(config, 'TRAIL_DISTANCE', 0.1)
        
        if direction == "LONG":
            new_tp = best_price * (1 - trail_distance / 100)
            # TP должен быть выше входа и выше текущего TP
            if new_tp > current_tp and new_tp > entry_price:
                new_tp = self.client.round_price(symbol, new_tp)
                
                if config.LIVE_TRADING:
                    result = self.client.set_tp_sl(symbol, take_profit=new_tp)
                    if result.get("retCode") == 0:
                        pos["tp_price"] = new_tp
                        self.log(f"📈 {symbol}: Trailing TP → {new_tp:.8f}", force=True)
                else:
                    pos["tp_price"] = new_tp
                    self.log(f"📈 {symbol}: Trailing TP → {new_tp:.8f}", force=True)
        
        else:  # SHORT
            new_tp = best_price * (1 + trail_distance / 100)
            # TP должен быть ниже входа и ниже текущего TP
            if new_tp < current_tp and new_tp < entry_price:
                new_tp = self.client.round_price(symbol, new_tp)
                
                if config.LIVE_TRADING:
                    result = self.client.set_tp_sl(symbol, take_profit=new_tp)
                    if result.get("retCode") == 0:
                        pos["tp_price"] = new_tp
                        self.log(f"📉 {symbol}: Trailing TP → {new_tp:.8f}", force=True)
                else:
                    pos["tp_price"] = new_tp
                    self.log(f"📉 {symbol}: Trailing TP → {new_tp:.8f}", force=True)
    
    def check_positions(self):
        """Проверить позиции"""
        
        if config.LIVE_TRADING:
            # ═══════════════════════════════════════════════════
            # РЕАЛЬНАЯ ТОРГОВЛЯ
            # ═══════════════════════════════════════════════════
            
            real_positions = self.client.get_positions()
            real_symbols = {p.get("symbol") for p in real_positions}
            
            # Обновляем Trailing TP для активных позиций
            for symbol in list(self.tracked_positions.keys()):
                if symbol in real_symbols:
                    current_price = self.client.get_current_price(symbol)
                    if current_price > 0:
                        self.update_trailing_tp(symbol, current_price)
            
            # Проверяем закрытые
            closed = []
            for symbol, tracked in list(self.tracked_positions.items()):
                if symbol not in real_symbols:
                    closed.append(symbol)
            
            for symbol in closed:
                tracked = self.tracked_positions.pop(symbol)
                current_price = self.client.get_current_price(symbol)
                pnl = check_pnl(tracked["entry_price"], current_price, tracked["type"])
                duration = (datetime.now() - tracked["entry_time"]).total_seconds()
                
                self.stats["total_trades"] += 1
                self.stats["total_pnl"] += pnl
                
                if pnl > 0:
                    self.stats["wins"] += 1
                    emoji = "✅"
                else:
                    self.stats["losses"] += 1
                    emoji = "❌"
                
                self.log("━" * 60, force=True)
                self.log(f"{emoji} ЗАКРЫТО: {symbol} | P&L: {'+' if pnl > 0 else ''}{pnl:.2f}%", force=True)
                self.log(f"   ⏱️ Время: {duration:.0f} сек", force=True)
                self.log("━" * 60, force=True)
                
                self.print_stats()
        
        else:
            # ═══════════════════════════════════════════════════
            # СИМУЛЯЦИЯ
            # ═══════════════════════════════════════════════════
            
            closed = []
            
            for symbol, tracked in list(self.tracked_positions.items()):
                current_price = self.client.get_current_price(symbol)
                if current_price <= 0:
                    continue
                
                # Обновляем trailing
                self.update_trailing_tp(symbol, current_price)
                
                pnl = check_pnl(tracked["entry_price"], current_price, tracked["type"])
                tp_pnl = check_pnl(tracked["entry_price"], tracked["tp_price"], tracked["type"])
                
                # TP достигнут?
                if pnl >= tp_pnl:
                    closed.append({
                        "symbol": symbol,
                        "pnl": pnl,
                        "reason": "TP"
                    })
            
            for info in closed:
                symbol = info["symbol"]
                tracked = self.tracked_positions.pop(symbol)
                duration = (datetime.now() - tracked["entry_time"]).total_seconds()
                
                self.stats["total_trades"] += 1
                self.stats["total_pnl"] += info["pnl"]
                self.stats["wins"] += 1
                
                self.log("━" * 60, force=True)
                self.log(f"✅ ЗАКРЫТО: {symbol} | TP | +{info['pnl']:.2f}%", force=True)
                self.log(f"   ⏱️ Время: {duration:.0f} сек", force=True)
                self.log("━" * 60, force=True)
                
                self.print_stats()
    
    def print_stats(self):
        """Вывести статистику"""
        total = self.stats["total_trades"]
        if total == 0:
            return
        
        wins = self.stats["wins"]
        losses = self.stats["losses"]
        pnl = self.stats["total_pnl"]
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
        """Показать открытые позиции"""
        count = self.get_open_positions_count()
        if count == 0:
            return
        
        self.log(f"📂 Открытых позиций: {count}/{config.MAX_POSITIONS}", force=True)
        
        for symbol, pos in self.tracked_positions.items():
            current = self.client.get_current_price(symbol)
            if current > 0:
                pnl = check_pnl(pos["entry_price"], current, pos["type"])
                emoji = "🟢" if pos["type"] == "LONG" else "🔴"
                sign = "+" if pnl > 0 else ""
                self.log(f"   {emoji} {symbol}: {sign}{pnl:.2f}%", force=True)
    
    def run(self):
        """Главный цикл"""
        mode = "💰 РЕАЛЬНАЯ ТОРГОВЛЯ" if config.LIVE_TRADING else "📊 СИМУЛЯЦИЯ"
        trailing = "ВКЛ" if getattr(config, 'TRAILING_TP', False) else "ВЫКЛ"
        
        print()
        print("═" * 60)
        print("🤖 СКАЛЬПИНГ БОТ v3.1")
        print("═" * 60)
        print(f"   Режим: {mode}")
        print(f"   Таймфрейм: {config.TIMEFRAME}m")
        print(f"   Монеты: {len(config.WHITELIST)} шт")
        print(f"   TP: {config.TP_PERCENT}%")
        print(f"   Trailing TP: {trailing}")
        print(f"   Макс. позиций: {config.MAX_POSITIONS}")
        
        if config.LIVE_TRADING:
            balance = self.client.get_available_balance()
            print(f"   💰 Баланс: ${balance:.2f}")
        
        print("═" * 60)
        print()
        print(f"📋 Монеты: {', '.join(config.WHITELIST[:5])}...")
        print()
        print("Для остановки: Ctrl+C")
        print()
        
        scan_count = 0
        
        try:
            while True:
                self.check_positions()
                
                if self.can_open_position():
                    signals = self.scan_for_signals()
                    
                    for signal in signals:
                        if not self.can_open_position():
                            break
                        self.open_position(signal)
                
                scan_count += 1
                if scan_count % 20 == 0:
                    self.print_open_positions()
                
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
    print("🔗 Проверка подключения...")
    
    client = BybitClient()
    
    price = client.get_current_price("BTCUSDT")
    if price <= 0:
        print("❌ Не удалось подключиться к Bybit!")
        sys.exit(1)
    
    print(f"✅ Подключено! BTC = ${price:.2f}")
    
    balance = client.get_available_balance()
    print(f"💰 Баланс: ${balance:.2f}")
    
    bot = ScalpingBotV3()
    bot.run()


if __name__ == "__main__":
    main()

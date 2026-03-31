#!/usr/bin/env python3
"""
SUPERTREND БОТ v5
==================
Что нового vs v4:
  1. Автовыбор монет через Bybit API (объём + волатильность)
  2. Фильтр старшего TF: вход только если ST 5m совпадает с ST 1m
  3. До MAX_SYMBOLS монет одновременно
  4. Обновление списка монет каждые 15 мин
  5. Все исправления B1-B7 из аудита
"""

import time
import sys
from datetime import datetime
from typing import Dict, List, Optional

import config
from bybit_client import BybitClient
from strategy import (
    calculate_supertrend, detect_signal,
    calculate_tp_price, validate_tp, check_pnl
)
from scanner import CoinScanner


class SupertrendBotV5:

    def __init__(self):
        self.client  = BybitClient()
        self.scanner = CoinScanner(self.client)

        # symbol → данные позиции
        self.positions: Dict[str, Dict] = {}

        self.last_trade_time: float = 0

        self.stats = {
            "total"    : 0,
            "wins"     : 0,
            "losses"   : 0,
            "pnl_usdt" : 0.0,
        }

        self.log_file = None
        if config.SAVE_LOGS:
            self.log_file = open(config.LOG_FILE, "a", encoding="utf-8")

    # ──────────────────────────────────────
    # ЛОГИРОВАНИЕ
    # ──────────────────────────────────────

    def log(self, msg: str, force: bool = False):
        ts  = datetime.now().strftime("%H:%M:%S")
        out = f"[{ts}] {msg}"
        if config.VERBOSE or force:
            print(out)
        if self.log_file:
            self.log_file.write(out + "\n")
            self.log_file.flush()

    # ──────────────────────────────────────
    # ФИЛЬТР СТАРШЕГО TF
    # ──────────────────────────────────────

    def check_higher_tf(self, symbol: str, signal_direction: str) -> bool:
        """
        Проверить что ST на 5m совпадает с направлением сигнала.
        LONG разрешён только если 5m ST = UP
        SHORT разрешён только если 5m ST = DOWN
        """
        try:
            klines_5m = self.client.get_klines(
                symbol, config.TIMEFRAME_FILTER, 20
            )
            if len(klines_5m) < 5:
                return True  # нет данных — не блокируем

            st_5m = calculate_supertrend(
                klines_5m,
                length=config.ST_LENGTH,
                factor=config.ST_FACTOR
            )

            direction_5m = st_5m[-1]["st_direction"]

            if signal_direction == "LONG"  and direction_5m == "up":
                return True
            if signal_direction == "SHORT" and direction_5m == "down":
                return True

            self.log(
                f"   ⛔ {symbol}: 5m ST={direction_5m.upper()} "
                f"не совпадает с {signal_direction} — пропускаем"
            )
            return False

        except Exception as e:
            self.log(f"   ⚠️  {symbol}: ошибка проверки 5m ST: {e}")
            return True  # при ошибке не блокируем

    # ──────────────────────────────────────
    # ОТКРЫТИЕ ПОЗИЦИИ
    # ──────────────────────────────────────

    def open_position(self, symbol: str, signal: Dict) -> bool:
        direction = signal["type"]

        if not config.LIVE_TRADING:
            return self._open_sim(symbol, signal)

        try:
            start = time.time()

            # 1. Баланс
            balance = self.client.get_available_balance()
            if balance <= 0:
                return False

            # 2. Свежая цена (прямо перед ордером)
            bid, ask = self.client.get_bid_ask(symbol)
            if bid <= 0 or ask <= 0:
                return False

            entry_price = ask if direction == "LONG" else bid

            # 3. Количество
            usdt_size = balance * config.POSITION_SIZE_PERCENT * config.LEVERAGE
            qty = self.client.calculate_qty(symbol, usdt_size, price=entry_price)
            if qty <= 0:
                return False

            # 4. TP
            tp_price = calculate_tp_price(entry_price, direction, config.TP_PERCENT)
            tp_price = self.client.round_price(symbol, tp_price)
            if not validate_tp(entry_price, tp_price, direction):
                return False

            real_tp_pct = abs(tp_price - entry_price) / entry_price * 100

            # 5. Плечо
            self.client.set_leverage(symbol, config.LEVERAGE)

            # 6. Ордер (без SL — ручной контроль)
            side   = "Buy" if direction == "LONG" else "Sell"
            result = self.client.place_order(
                symbol      = symbol,
                side        = side,
                qty         = qty,
                take_profit = tp_price
            )

            elapsed = time.time() - start

            if result.get("retCode") != 0:
                self.log(f"❌ {symbol}: {result.get('retMsg')}", force=True)
                return False

            # 7. Подтверждение
            pos = None
            for _ in range(4):
                time.sleep(0.2)
                pos = self.client.get_position(symbol)
                if pos:
                    break

            if not pos:
                return False

            real_entry = float(pos.get("avgPrice", entry_price))
            real_size  = float(pos.get("size", qty))
            pos_usdt   = real_size * real_entry

            # 8. Проверка TP на бирже
            if not pos.get("takeProfit") or float(pos.get("takeProfit", 0)) == 0:
                self.client.set_tp_sl(symbol, take_profit=tp_price)

            # 9. Сохраняем
            self.positions[symbol] = {
                "type"       : direction,
                "side"       : side,
                "entry_price": real_entry,
                "tp_price"   : tp_price,
                "qty"        : real_size,
                "pos_usdt"   : pos_usdt,
                "entry_time" : datetime.now(),
            }

            self.last_trade_time = time.time()

            emoji = "🟢" if direction == "LONG" else "🔴"
            self.log("━" * 55, force=True)
            self.log(f"💰 {symbol} | {emoji} {direction}", force=True)
            self.log(f"   📍 Вход:  {real_entry:.6f}", force=True)
            self.log(f"   🎯 TP:    {tp_price:.6f}  (+{real_tp_pct:.3f}%)", force=True)
            self.log(f"   📊 {real_size} ед. ({pos_usdt:.2f} USDT) | {elapsed:.2f}с", force=True)
            self.log("━" * 55, force=True)
            return True

        except Exception as e:
            self.log(f"❌ {symbol}: исключение: {e}", force=True)
            return False

    def _open_sim(self, symbol: str, signal: Dict) -> bool:
        entry = signal["price"]
        tp    = calculate_tp_price(entry, signal["type"], config.TP_PERCENT)
        self.positions[symbol] = {
            "type"       : signal["type"],
            "entry_price": entry,
            "tp_price"   : tp,
            "qty"        : 100.0,
            "pos_usdt"   : 100.0,
            "entry_time" : datetime.now(),
        }
        self.last_trade_time = time.time()
        emoji = "🟢" if signal["type"] == "LONG" else "🔴"
        self.log(f"🔔 СИМ {symbol} | {emoji} {signal['type']} | вход={entry:.6f} TP={tp:.6f}", force=True)
        return True

    # ──────────────────────────────────────
    # МОНИТОРИНГ ПОЗИЦИЙ
    # ──────────────────────────────────────

    def check_positions(self):
        if not self.positions:
            return

        if config.LIVE_TRADING:
            real_pos  = self.client.get_positions()
            real_syms = {p["symbol"] for p in real_pos}

            # Проверяем закрытые
            closed = [s for s in list(self.positions) if s not in real_syms]
            for symbol in closed:
                self._on_closed(symbol)

            # Показываем P&L открытых
            for symbol in list(self.positions):
                if symbol in real_syms:
                    price = self.client.get_current_price(symbol)
                    if price > 0:
                        pos = self.positions[symbol]
                        pnl = check_pnl(pos["entry_price"], price, pos["type"])
                        dur = (datetime.now() - pos["entry_time"]).total_seconds()
                        emoji = "🟢" if pos["type"] == "LONG" else "🔴"
                        sign  = "+" if pnl >= 0 else ""
                        self.log(f"   {emoji} {symbol}: {sign}{pnl:.3f}% | {dur:.0f}с")
        else:
            for symbol in list(self.positions):
                price = self.client.get_current_price(symbol)
                if price <= 0:
                    continue
                pos = self.positions[symbol]
                pnl = check_pnl(pos["entry_price"], price, pos["type"])
                if pos["type"] == "LONG"  and price >= pos["tp_price"]:
                    self._record(symbol, pnl, "TP")
                elif pos["type"] == "SHORT" and price <= pos["tp_price"]:
                    self._record(symbol, pnl, "TP")

    def _on_closed(self, symbol: str):
        """Позиция закрылась на бирже (TP/SL/вручную)"""
        pos      = self.positions.pop(symbol)
        duration = (datetime.now() - pos["entry_time"]).total_seconds()

        # Реальный P&L с биржи
        pnl_pct = self.client.get_last_closed_pnl(symbol)
        if pnl_pct is None:
            price   = self.client.get_current_price(symbol)
            pnl_pct = check_pnl(pos["entry_price"], price, pos["type"]) if price > 0 else 0.0
            self.log(f"⚠️  {symbol}: P&L с биржи недоступен, считаем по цене")

        self._record_raw(symbol, pos, pnl_pct, duration, "Биржа (TP/SL/ручное)")

    def _record(self, symbol: str, pnl_pct: float, reason: str):
        pos      = self.positions.pop(symbol)
        duration = (datetime.now() - pos["entry_time"]).total_seconds()
        self._record_raw(symbol, pos, pnl_pct, duration, reason)

    def _record_raw(self, symbol: str, pos: Dict,
                    pnl_pct: float, duration: float, reason: str):
        pnl_usdt = pos.get("pos_usdt", 0) * pnl_pct / 100
        self.stats["total"]    += 1
        self.stats["pnl_usdt"] += pnl_usdt

        if pnl_pct > 0:
            self.stats["wins"]  += 1
            emoji = "✅"
        else:
            self.stats["losses"] += 1
            emoji = "❌"

        self.log("━" * 55, force=True)
        self.log(
            f"{emoji} ЗАКРЫТО: {symbol} | "
            f"{'+' if pnl_pct >= 0 else ''}{pnl_pct:.3f}% | "
            f"{'+' if pnl_usdt >= 0 else ''}{pnl_usdt:.4f} USDT | "
            f"{reason}",
            force=True
        )
        self.log(f"   ⏱️  {duration:.0f}с", force=True)
        self.log("━" * 55, force=True)
        self._print_stats()

    # ──────────────────────────────────────
    # СТАТИСТИКА
    # ──────────────────────────────────────

    def _print_stats(self):
        t = self.stats["total"]
        if t == 0:
            return
        wr  = self.stats["wins"] / t * 100
        pnl = self.stats["pnl_usdt"]
        avg = pnl / t
        self.log("", force=True)
        self.log("📊 СТАТИСТИКА:", force=True)
        self.log(f"   Сделок: {t} | Win: {self.stats['wins']} | Loss: {self.stats['losses']}", force=True)
        self.log(f"   Win Rate: {wr:.1f}%", force=True)
        self.log(f"   P&L итого:  {'+' if pnl >= 0 else ''}{pnl:.4f} USDT", force=True)
        self.log(f"   P&L средний: {'+' if avg >= 0 else ''}{avg:.4f} USDT", force=True)
        self.log("", force=True)

    # ──────────────────────────────────────
    # ГЛАВНЫЙ ЦИКЛ
    # ──────────────────────────────────────

    def run(self):
        print()
        print("═" * 55)
        print("🤖 SUPERTREND БОТ v5")
        print("═" * 55)
        print(f"   Режим:      {'💰 РЕАЛЬНАЯ' if config.LIVE_TRADING else '📊 СИМУЛЯЦИЯ'}")
        print(f"   ST:         Length={config.ST_LENGTH}, Factor={config.ST_FACTOR}")
        print(f"   TF вход:    {config.TIMEFRAME_ENTRY}m")
        print(f"   TF фильтр:  {config.TIMEFRAME_FILTER}m")
        print(f"   TP:         {config.TP_PERCENT}%")
        print(f"   Плечо:      {config.LEVERAGE}x")
        print(f"   Авто монет: {'ДА' if config.AUTO_SELECT_COINS else 'НЕТ'}")

        if config.LIVE_TRADING:
            balance = self.client.get_available_balance()
            print(f"   💰 Баланс:  ${balance:.2f}")

        print("═" * 55)
        print()

        scan_n = 0

        try:
            while True:
                # 1. Получаем список монет
                symbols = self.scanner.get_symbols()

                if not symbols:
                    self.log("⚠️  Нет монет для торговли, ждём...")
                    time.sleep(30)
                    continue

                # 2. Проверяем открытые позиции
                self.check_positions()

                # 3. Считаем сколько слотов свободно
                busy = set(self.positions.keys())
                if config.LIVE_TRADING:
                    real_pos  = self.client.get_positions()
                    real_syms = {p["symbol"] for p in real_pos}
                    busy = busy | real_syms

                free_slots = config.MAX_SYMBOLS - len(busy)

                # 4. Ищем сигналы
                if free_slots > 0:
                    cooldown = time.time() - self.last_trade_time
                    if cooldown < config.MIN_TRADE_INTERVAL:
                        pass  # ждём
                    else:
                        for symbol in symbols:
                            if free_slots <= 0:
                                break
                            if symbol in busy:
                                continue

                            # Свечи 1m
                            klines = self.client.get_klines(
                                symbol, config.TIMEFRAME_ENTRY, config.CANDLES_LIMIT
                            )
                            if len(klines) < 10:
                                continue

                            # ST на 1m
                            st = calculate_supertrend(
                                klines,
                                length=config.ST_LENGTH,
                                factor=config.ST_FACTOR
                            )
                            signal = detect_signal(st)

                            if not signal:
                                continue

                            direction = signal["type"]

                            # Фильтр: направление разрешено?
                            if direction == "LONG"  and not config.TRADE_LONG:
                                continue
                            if direction == "SHORT" and not config.TRADE_SHORT:
                                continue

                            # Фильтр старшего TF
                            if not self.check_higher_tf(symbol, direction):
                                continue

                            # Открываем
                            ok = self.open_position(symbol, signal)
                            if ok:
                                busy.add(symbol)
                                free_slots -= 1

                # 5. Статус каждые ~1 мин
                scan_n += 1
                if scan_n % 12 == 0:
                    active = list(self.positions.keys())
                    self.log(
                        f"📡 Монет в работе: {len(active)}/{config.MAX_SYMBOLS} "
                        f"| Список: {symbols[:5]}"
                    )

                time.sleep(config.SCAN_INTERVAL)

        except KeyboardInterrupt:
            print()
            print("═" * 55)
            print("🛑 ОСТАНОВЛЕН")
            print("═" * 55)
            self._print_stats()
            if self.log_file:
                self.log_file.close()


def main():
    print("🔗 Подключение к Bybit...")
    client = BybitClient()
    price  = client.get_current_price("BTCUSDT")
    if price <= 0:
        print("❌ Нет связи с Bybit!")
        sys.exit(1)
    print(f"✅ BTC = ${price:.2f}")
    balance = client.get_available_balance()
    print(f"💰 Баланс: ${balance:.2f}")
    print()
    bot = SupertrendBotV5()
    bot.run()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
SUPERTREND БОТ v6
==================
Новое vs v5:
  1. FLIP логика — ST развернулся = закрываем + открываем в обратную сторону
  2. Секундный тайминг — bid/ask прямо перед ордером для точного входа
  3. Скан каждые 2 сек вместо 5
  4. Защита от мгновенного флипа — MIN_HOLD_BEFORE_FLIP секунд
  5. Показываем направление ST на 5m рядом с каждой позицией
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


class SupertrendBotV6:

    def __init__(self):
        self.client  = BybitClient()
        self.scanner = CoinScanner(self.client)

        # symbol → данные позиции
        self.positions: Dict[str, Dict] = {}

        self.last_trade_time: float = 0

        self.stats = {
            "total"   : 0,
            "wins"    : 0,
            "losses"  : 0,
            "pnl_usdt": 0.0,
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
    # НАПРАВЛЕНИЕ 5m ST (для контекста)
    # ──────────────────────────────────────

    def get_5m_direction(self, symbol: str) -> str:
        try:
            klines = self.client.get_klines(symbol, config.TIMEFRAME_FILTER, 10)
            if len(klines) < 5:
                return "?"
            st = calculate_supertrend(klines, config.ST_LENGTH, config.ST_FACTOR)
            d  = st[-1]["st_direction"]
            return "↑UP" if d == "up" else "↓DOWN"
        except Exception:
            return "?"

    def check_5m_allows(self, symbol: str, direction: str) -> bool:
        try:
            klines = self.client.get_klines(symbol, config.TIMEFRAME_FILTER, 10)
            if len(klines) < 5:
                return True
            st  = calculate_supertrend(klines, config.ST_LENGTH, config.ST_FACTOR)
            d5m = st[-1]["st_direction"]
            if direction == "LONG"  and d5m == "up":   return True
            if direction == "SHORT" and d5m == "down":  return True
            self.log(f"   ⛔ {symbol}: 5m={d5m.upper()} ≠ {direction} — пропуск")
            return False
        except Exception:
            return True

    # ──────────────────────────────────────
    # ОТКРЫТИЕ / ПЕРЕВОРОТ
    # ──────────────────────────────────────

    def open_position(self, symbol: str, direction: str,
                      is_flip: bool = False) -> bool:
        """
        Открыть позицию.
        is_flip=True — это переворот, пропускаем фильтр 5m и паузу.
        """
        if not config.LIVE_TRADING:
            return self._open_sim(symbol, direction)

        try:
            start = time.time()

            # 1. Баланс
            balance = self.client.get_available_balance()
            if balance <= 0:
                self.log(f"❌ {symbol}: нулевой баланс", force=True)
                return False

            # 2. Свежая цена — прямо перед ордером
            bid, ask = self.client.get_bid_ask(symbol)
            if bid <= 0 or ask <= 0:
                self.log(f"❌ {symbol}: нет цены", force=True)
                return False

            entry_price = ask if direction == "LONG" else bid

            # 3. Объём
            usdt_size = balance * config.POSITION_SIZE_PERCENT * config.LEVERAGE
            qty = self.client.calculate_qty(symbol, usdt_size, price=entry_price)
            if qty <= 0:
                self.log(f"❌ {symbol}: нет средств", force=True)
                return False

            # 4. TP
            tp_price = calculate_tp_price(entry_price, direction, config.TP_PERCENT)
            tp_price = self.client.round_price(symbol, tp_price)
            if not validate_tp(entry_price, tp_price, direction):
                self.log(f"❌ {symbol}: TP невалиден", force=True)
                return False

            real_tp_pct = abs(tp_price - entry_price) / entry_price * 100

            # 5. Плечо
            self.client.set_leverage(symbol, config.LEVERAGE)

            # 6. Ордер
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
                time.sleep(0.15)
                pos = self.client.get_position(symbol)
                if pos:
                    break

            if not pos:
                self.log(f"❌ {symbol}: позиция не подтверждена", force=True)
                return False

            real_entry = float(pos.get("avgPrice", entry_price))
            real_size  = float(pos.get("size", qty))
            pos_usdt   = real_size * real_entry

            # 8. Проверка TP
            if not pos.get("takeProfit") or float(pos.get("takeProfit", 0)) == 0:
                self.client.set_tp_sl(symbol, take_profit=tp_price)

            # 9. Направление 5m для контекста
            d5m = self.get_5m_direction(symbol)

            # 10. Сохраняем
            self.positions[symbol] = {
                "type"       : direction,
                "side"       : side,
                "entry_price": real_entry,
                "tp_price"   : tp_price,
                "qty"        : real_size,
                "pos_usdt"   : pos_usdt,
                "entry_time" : datetime.now(),
                "is_flip"    : is_flip,
            }

            self.last_trade_time = time.time()

            emoji  = "🟢" if direction == "LONG" else "🔴"
            prefix = "🔄 ФЛИП" if is_flip else "💰 СДЕЛКА"

            self.log("━" * 55, force=True)
            self.log(f"{prefix}: {symbol} | {emoji} {direction}", force=True)
            self.log(f"   📍 Вход:   {real_entry:.6f}", force=True)
            self.log(f"   🎯 TP:     {tp_price:.6f}  (+{real_tp_pct:.3f}%)", force=True)
            self.log(f"   📊 {real_size} ед. ({pos_usdt:.2f} USDT) | {elapsed:.2f}с", force=True)
            self.log(f"   📈 5m ST:  {d5m}", force=True)
            self.log("━" * 55, force=True)

            return True

        except Exception as e:
            self.log(f"❌ {symbol}: исключение: {e}", force=True)
            return False

    def _open_sim(self, symbol: str, direction: str) -> bool:
        price = self.client.get_current_price(symbol)
        tp    = calculate_tp_price(price, direction, config.TP_PERCENT)
        self.positions[symbol] = {
            "type"       : direction,
            "entry_price": price,
            "tp_price"   : tp,
            "qty"        : 100.0,
            "pos_usdt"   : 100.0,
            "entry_time" : datetime.now(),
            "is_flip"    : False,
        }
        self.last_trade_time = time.time()
        emoji = "🟢" if direction == "LONG" else "🔴"
        self.log(f"🔔 СИМ {symbol} | {emoji} {direction} | вход={price:.6f}", force=True)
        return True

    # ──────────────────────────────────────
    # ЗАКРЫТИЕ
    # ──────────────────────────────────────

    def close_position(self, symbol: str, reason: str) -> bool:
        """Закрыть позицию на бирже"""
        result = self.client.close_position(symbol)
        if result.get("retCode") == 0:
            self.log(f"🔒 {symbol}: позиция закрыта ({reason})")
            return True
        else:
            self.log(f"⚠️  {symbol}: ошибка закрытия: {result.get('retMsg')}", force=True)
            return False

    # ──────────────────────────────────────
    # МОНИТОРИНГ + FLIP
    # ──────────────────────────────────────

    def check_and_flip(self, symbol: str, klines_st: list):
        """
        Проверить позицию.
        Если ST развернулся — закрыть и открыть в обратную сторону.
        """
        if symbol not in self.positions:
            return

        pos       = self.positions[symbol]
        direction = pos["type"]
        held_secs = (datetime.now() - pos["entry_time"]).total_seconds()

        # Текущее направление ST на 1m
        current_st_dir = klines_st[-1]["st_direction"]
        current_signal = klines_st[-1].get("st_signal")

        # Текущая цена
        current_price = self.client.get_current_price(symbol)
        pnl = check_pnl(pos["entry_price"], current_price, direction) if current_price > 0 else 0

        # Показываем P&L
        d5m  = self.get_5m_direction(symbol)
        sign = "+" if pnl >= 0 else ""
        emoji = "🟢" if direction == "LONG" else "🔴"
        self.log(f"   {emoji} {symbol}: {sign}{pnl:.3f}% | ST={current_st_dir.upper()} | 5m:{d5m} | {held_secs:.0f}с")

        # Проверяем разворот ST
        if not config.FLIP_ON_REVERSAL:
            return

        # ST развернулся против нашей позиции?
        should_flip = False
        new_direction = None

        if direction == "LONG" and current_st_dir == "down" and current_signal == "SELL":
            should_flip   = True
            new_direction = "SHORT"
        elif direction == "SHORT" and current_st_dir == "up" and current_signal == "BUY":
            should_flip   = True
            new_direction = "LONG"

        if not should_flip:
            return

        # Защита от мгновенного флипа
        if held_secs < config.MIN_HOLD_BEFORE_FLIP:
            self.log(
                f"   ⏳ {symbol}: ST развернулся но ждём "
                f"({held_secs:.0f}с < {config.MIN_HOLD_BEFORE_FLIP}с)"
            )
            return

        self.log(f"🔄 {symbol}: ST развернулся! {direction} → {new_direction}", force=True)

        if config.LIVE_TRADING:
            # Закрываем текущую
            closed = self.close_position(symbol, f"ST flip → {new_direction}")
            if not closed:
                return

            # Ждём подтверждения закрытия
            for _ in range(5):
                time.sleep(0.2)
                if not self.client.has_position(symbol):
                    break

        # Записываем результат закрытой позиции
        self._on_closed(symbol, reason=f"ST flip → {new_direction}")

        # Открываем в обратную сторону
        time.sleep(0.3)
        self.open_position(symbol, new_direction, is_flip=True)

    # ──────────────────────────────────────
    # ОБРАБОТКА ЗАКРЫТИЯ
    # ──────────────────────────────────────

    def check_closed_positions(self):
        """Проверить позиции которые закрылись по TP/SL на бирже"""
        if not config.LIVE_TRADING:
            return

        real_pos  = self.client.get_positions()
        real_syms = {p["symbol"] for p in real_pos}

        for symbol in list(self.positions.keys()):
            if symbol not in real_syms:
                self._on_closed(symbol, "Биржа (TP/SL)")

    def _on_closed(self, symbol: str, reason: str = "закрыто"):
        if symbol not in self.positions:
            return

        pos      = self.positions.pop(symbol)
        duration = (datetime.now() - pos["entry_time"]).total_seconds()

        # Реальный P&L с биржи
        pnl_pct = self.client.get_last_closed_pnl(symbol)
        if pnl_pct is None:
            price   = self.client.get_current_price(symbol)
            pnl_pct = check_pnl(pos["entry_price"], price, pos["type"]) if price > 0 else 0.0

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
        self.log(f"   Win Rate:    {wr:.1f}%", force=True)
        self.log(f"   P&L итого:   {'+' if pnl >= 0 else ''}{pnl:.4f} USDT", force=True)
        self.log(f"   P&L средний: {'+' if avg >= 0 else ''}{avg:.4f} USDT", force=True)
        self.log("", force=True)

    # ──────────────────────────────────────
    # ГЛАВНЫЙ ЦИКЛ
    # ──────────────────────────────────────

    def run(self):
        print()
        print("═" * 55)
        print("🤖 SUPERTREND БОТ v6")
        print("═" * 55)
        print(f"   Режим:       {'💰 РЕАЛЬНАЯ' if config.LIVE_TRADING else '📊 СИМУЛЯЦИЯ'}")
        print(f"   ST:          Length={config.ST_LENGTH}, Factor={config.ST_FACTOR}")
        print(f"   TF сигнал:   {config.TIMEFRAME_FAST}m")
        print(f"   TF фильтр:   {config.TIMEFRAME_FILTER}m")
        print(f"   TP:          {config.TP_PERCENT}%")
        print(f"   Плечо:       {config.LEVERAGE}x")
        print(f"   Flip:        {'ВКЛ' if config.FLIP_ON_REVERSAL else 'ВЫКЛ'}")
        print(f"   Мин. держать: {config.MIN_HOLD_BEFORE_FLIP}с перед флипом")
        print(f"   Скан:        каждые {config.SCAN_INTERVAL}с")

        if config.LIVE_TRADING:
            balance = self.client.get_available_balance()
            print(f"   💰 Баланс:   ${balance:.2f}")

        print("═" * 55)
        print()

        scan_n = 0

        try:
            while True:
                # 1. Список монет
                symbols = self.scanner.get_symbols()
                if not symbols:
                    time.sleep(30)
                    continue

                # 2. Проверяем закрытые по TP/SL
                self.check_closed_positions()

                # 3. Для каждой монеты — считаем ST и проверяем флип
                for symbol in symbols:
                    # Получаем свечи 1m
                    klines = self.client.get_klines(
                        symbol, config.TIMEFRAME_FAST, config.CANDLES_LIMIT
                    )
                    if len(klines) < 10:
                        continue

                    # Считаем ST
                    klines_st = calculate_supertrend(
                        klines,
                        length=config.ST_LENGTH,
                        factor=config.ST_FACTOR
                    )

                    # Если есть позиция — проверяем флип
                    if symbol in self.positions:
                        self.check_and_flip(symbol, klines_st)
                        continue

                    # Если нет позиции — ищем новый вход
                    busy = set(self.positions.keys())
                    if config.LIVE_TRADING:
                        real_pos  = self.client.get_positions()
                        real_syms = {p["symbol"] for p in real_pos}
                        busy = busy | real_syms

                    free_slots = config.MAX_SYMBOLS - len(busy)
                    if free_slots <= 0:
                        continue

                    # Пауза между сделками
                    cooldown = time.time() - self.last_trade_time
                    if cooldown < config.MIN_TRADE_INTERVAL:
                        continue

                    # Сигнал
                    signal = detect_signal(klines_st)
                    if not signal:
                        continue

                    direction = signal["type"]

                    if direction == "LONG"  and not config.TRADE_LONG:
                        continue
                    if direction == "SHORT" and not config.TRADE_SHORT:
                        continue

                    # Фильтр 5m
                    if not self.check_5m_allows(symbol, direction):
                        continue

                    # Открываем
                    self.open_position(symbol, direction, is_flip=False)

                # 4. Статус каждые ~30 сек (15 сканов по 2 сек)
                scan_n += 1
                if scan_n % 15 == 0:
                    active = list(self.positions.keys())
                    self.log(
                        f"📡 Позиций: {len(active)}/{config.MAX_SYMBOLS} "
                        f"| Монеты: {symbols[:3]}"
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
    bot = SupertrendBotV6()
    bot.run()


if __name__ == "__main__":
    main()

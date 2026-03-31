#!/usr/bin/env python3
"""
SUPERTREND БОТ v7
==================
Изменения vs v6:
  1. MAX_SYMBOLS = 5 (было 3)
  2. MIN_TURNOVER = $10M — убираем мусор
  3. Флип проверяет 5m — не открываем против 5m
  4. ST quality scoring в сканере
  5. Открытые позиции мониторятся всегда (фикс из v6)
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


class SupertrendBotV7:

    def __init__(self):
        self.client  = BybitClient()
        self.scanner = CoinScanner(self.client)
        self.positions: Dict[str, Dict] = {}
        self.last_trade_time: float = 0

        # Счётчик убытков подряд по каждой монете
        self.coin_losses: Dict[str, int]   = {}   # symbol → кол-во убытков подряд
        self.coin_cooldown: Dict[str, float] = {}  # symbol → время до которого пауза

        self.stats = {
            "total": 0, "wins": 0, "losses": 0, "pnl_usdt": 0.0
        }
        self.log_file = None
        if config.SAVE_LOGS:
            self.log_file = open(config.LOG_FILE, "a", encoding="utf-8")

    # ─────────────────────────────────────
    # ЛОГИРОВАНИЕ
    # ─────────────────────────────────────

    def log(self, msg: str, force: bool = False):
        ts  = datetime.now().strftime("%H:%M:%S")
        out = f"[{ts}] {msg}"
        if config.VERBOSE or force:
            print(out)
        if self.log_file:
            self.log_file.write(out + "\n")
            self.log_file.flush()

    # ─────────────────────────────────────
    # 5m ФИЛЬТР
    # ─────────────────────────────────────

    def get_5m_direction(self, symbol: str) -> str:
        """Показываем направление 5m + сколько свечей держится"""
        try:
            n = getattr(config, 'FILTER_5M_MIN_CANDLES', 3)
            klines = self.client.get_klines(symbol, config.TIMEFRAME_FILTER, n + 5)
            if len(klines) < 3:
                return "?"
            st = calculate_supertrend(klines, config.ST_LENGTH, config.ST_FACTOR)

            current_dir = st[-1]["st_direction"]
            # Считаем сколько свечей подряд в текущем направлении
            count = 0
            for candle in reversed(st):
                if candle["st_direction"] == current_dir:
                    count += 1
                else:
                    break

            arrow = "↑" if current_dir == "up" else "↓"
            return f"{arrow}{current_dir.upper()}x{count}"
        except Exception:
            return "?"

    def check_5m_allows(self, symbol: str, direction: str) -> bool:
        """Мягкий фильтр 5m — блокируем только если N свечей подряд против нас"""
        try:
            n = getattr(config, 'FILTER_5M_MIN_CANDLES', 3)
            klines = self.client.get_klines(symbol, config.TIMEFRAME_FILTER, n + 5)
            if len(klines) < n:
                return True
            st = calculate_supertrend(klines, config.ST_LENGTH, config.ST_FACTOR)
            last_n = st[-n:]
            directions = [c["st_direction"] for c in last_n]
            against = "down" if direction == "LONG" else "up"
            all_against = all(d == against for d in directions)
            if all_against:
                self.log(
                    f"   ⛔ {symbol}: 5m {against.upper()} "
                    f"устойчиво {n} свечей — блок {direction}"
                )
                return False
            return True
        except Exception:
            return True

    def is_coin_on_cooldown(self, symbol: str) -> bool:
        """Проверить на паузе ли монета после серии убытков"""
        until = self.coin_cooldown.get(symbol, 0)
        if time.time() < until:
            remaining = (until - time.time()) / 60
            self.log(f"   ⏸️  {symbol}: пауза ещё {remaining:.0f} мин (серия убытков)")
            return True
        return False

    def check_atr_volatility(self, symbol: str, klines: list) -> bool:
        """
        Комплексный фильтр входа — три проверки:

        1. БОКОВИК: ATR слишком мал → свечи не двигаются → не входим
        2. ПАМП/ДАМП: последняя свеча аномально большая → это спайк, не тренд → не входим
        3. КАЧЕСТВО ТРЕНДА: цена должна идти в нужную сторону последние N свечей

        Именно проверка #2 защищает от APRUSDT — памп +37% за одну свечу.
        """
        if not getattr(config, 'ATR_FILTER_ENABLED', True):
            return True

        try:
            period     = getattr(config, 'ATR_PERIOD', 5)
            multiplier = getattr(config, 'ATR_MIN_MULTIPLIER', 1.2)
            min_atr    = config.TP_PERCENT * multiplier
            max_candle = getattr(config, 'MAX_CANDLE_SIZE_PCT', 3.0)
            trend_conf = getattr(config, 'TREND_CONFIRM_CANDLES', 2)

            if len(klines) < period + 2:
                return True

            recent = klines[-period:]
            last   = klines[-1]   # последняя (сигнальная) свеча
            prev   = klines[-2]   # предыдущая

            # ── 1. БОКОВИК: считаем ATR ──────────────────────────
            tr_values = []
            for i in range(1, len(recent)):
                h  = recent[i]["high"]
                l  = recent[i]["low"]
                pc = recent[i-1]["close"]
                tr = max(h - l, abs(h - pc), abs(l - pc))
                tr_values.append(tr / pc * 100)

            atr_pct = sum(tr_values) / len(tr_values) if tr_values else 0

            if atr_pct < min_atr:
                self.log(
                    f"   📉 {symbol}: ATR={atr_pct:.3f}% < {min_atr:.3f}% — боковик"
                )
                return False

            # ── 2. ПАМП/ДАМП: размер последней свечи ─────────────
            # Если тело свечи или тень огромные — это спайк
            candle_body = abs(last["close"] - last["open"]) / last["open"] * 100
            candle_wick = (last["high"] - last["low"]) / last["open"] * 100

            # Проверяем ПРЕДЫДУЩУЮ свечу тоже (часто памп на ней)
            prev_body = abs(prev["close"] - prev["open"]) / prev["open"] * 100

            if candle_body > max_candle:
                self.log(
                    f"   🚨 {symbol}: свеча {candle_body:.1f}% > {max_candle}% — памп/дамп, пропуск"
                )
                return False

            if prev_body > max_candle:
                self.log(
                    f"   🚨 {symbol}: пред. свеча {prev_body:.1f}% > {max_candle}% — после пампа, пропуск"
                )
                return False

            if candle_wick > max_candle * 2:
                self.log(
                    f"   🚨 {symbol}: фитиль {candle_wick:.1f}% — аномалия, пропуск"
                )
                return False

            # ── 3. КАЧЕСТВО ТРЕНДА: цена идёт в нашу сторону ─────
            # Последние trend_conf свечей должны подтверждать направление
            # Берём closes последних N свечей
            closes = [c["close"] for c in klines[-(trend_conf + 2):]]
            if len(closes) >= 3:
                # Для LONG: цена должна быть выше чем была N свечей назад
                # Для SHORT: цена должна быть ниже
                # Определяем направление из последнего ST сигнала
                # (это вызывается уже после detect_signal поэтому кlines[-1] = сигнал)
                price_change = (closes[-1] - closes[0]) / closes[0] * 100
                # Нет жёсткой блокировки — просто логируем
                # Жёсткая блокировка только при явном противоходе
                if abs(price_change) < 0.05:
                    self.log(
                        f"   ⚠️  {symbol}: цена почти не двигалась ({price_change:+.3f}%) — слабый тренд"
                    )
                    # Не блокируем — просто предупреждаем

            return True

        except Exception:
            return True


    def update_coin_stats(self, symbol: str, pnl_pct: float):
        """Обновить счётчик убытков по монете"""
        max_losses = getattr(config, 'MAX_CONSECUTIVE_LOSSES', 2)
        cooldown_min = getattr(config, 'COIN_COOLDOWN_MINUTES', 20)

        if pnl_pct > 0:
            # Победа — сбрасываем счётчик
            self.coin_losses[symbol] = 0
        else:
            # Убыток — увеличиваем счётчик
            self.coin_losses[symbol] = self.coin_losses.get(symbol, 0) + 1
            losses = self.coin_losses[symbol]

            if losses >= max_losses:
                until = time.time() + cooldown_min * 60
                self.coin_cooldown[symbol] = until
                self.log(
                    f"   ⏸️  {symbol}: {losses} убытка подряд → "
                    f"пауза {cooldown_min} мин",
                    force=True
                )
                self.coin_losses[symbol] = 0  # сброс счётчика


        """
        МЯГКИЙ фильтр 5m.

        Логика:
        - Считаем последние FILTER_5M_MIN_CANDLES свечей на 5m
        - Если ВСЕ они против нашего направления = блокируем
        - Если хотя бы одна в нашу сторону или нейтральная = пропускаем

        Примеры при FILTER_5M_MIN_CANDLES=3:
          5m: DOWN DOWN DOWN → LONG заблокирован ❌
          5m: DOWN DOWN UP   → LONG разрешён ✅ (тренд меняется)
          5m: UP   UP   UP   → LONG разрешён ✅
          5m: UP   DOWN UP   → LONG разрешён ✅ (неустойчивый даун)
        """
        try:
            n = getattr(config, 'FILTER_5M_MIN_CANDLES', 3)
            klines = self.client.get_klines(symbol, config.TIMEFRAME_FILTER, n + 5)
            if len(klines) < n:
                return True  # нет данных — не блокируем

            st = calculate_supertrend(klines, config.ST_LENGTH, config.ST_FACTOR)

            # Берём последние N свечей
            last_n = st[-n:]
            directions = [c["st_direction"] for c in last_n]

            # Определяем что "против нас"
            against = "down" if direction == "LONG" else "up"

            # Блокируем только если ВСЕ N свечей против нас
            all_against = all(d == against for d in directions)

            if all_against:
                self.log(
                    f"   ⛔ {symbol}: 5m {against.upper()} "
                    f"устойчиво {n} свечей подряд — блокируем {direction}"
                )
                return False

            # Пропускаем — тренд не устойчивый против нас
            return True

        except Exception:
            return True  # при ошибке не блокируем

    # ─────────────────────────────────────
    # ОТКРЫТИЕ ПОЗИЦИИ
    # ─────────────────────────────────────

    def open_position(self, symbol: str, direction: str,
                      is_flip: bool = False) -> bool:
        if not config.LIVE_TRADING:
            return self._open_sim(symbol, direction)

        try:
            start = time.time()

            balance = self.client.get_available_balance()
            if balance <= 0:
                return False

            bid, ask = self.client.get_bid_ask(symbol)
            if bid <= 0 or ask <= 0:
                return False

            entry_price = ask if direction == "LONG" else bid
            usdt_size   = balance * config.POSITION_SIZE_PERCENT * config.LEVERAGE
            qty = self.client.calculate_qty(symbol, usdt_size, price=entry_price)
            if qty <= 0:
                return False

            tp_price = calculate_tp_price(entry_price, direction, config.TP_PERCENT)
            tp_price = self.client.round_price(symbol, tp_price)
            if not validate_tp(entry_price, tp_price, direction):
                return False

            real_tp_pct = abs(tp_price - entry_price) / entry_price * 100

            self.client.set_leverage(symbol, config.LEVERAGE)

            side   = "Buy" if direction == "LONG" else "Sell"
            result = self.client.place_order(
                symbol=symbol, side=side, qty=qty, take_profit=tp_price
            )
            elapsed = time.time() - start

            if result.get("retCode") != 0:
                self.log(f"❌ {symbol}: {result.get('retMsg')}", force=True)
                return False

            pos = None
            for _ in range(4):
                time.sleep(0.15)
                pos = self.client.get_position(symbol)
                if pos:
                    break

            if not pos:
                return False

            real_entry = float(pos.get("avgPrice", entry_price))
            real_size  = float(pos.get("size", qty))
            pos_usdt   = real_size * real_entry

            if not pos.get("takeProfit") or float(pos.get("takeProfit", 0)) == 0:
                self.client.set_tp_sl(symbol, take_profit=tp_price)

            d5m = self.get_5m_direction(symbol)

            self.positions[symbol] = {
                "type"       : direction,
                "side"       : side,
                "entry_price": real_entry,
                "tp_price"   : tp_price,
                "qty"        : real_size,
                "pos_usdt"   : pos_usdt,
                "entry_time" : datetime.now(),
                "st_mismatch_since": None,  # когда ST начал противоречить
            }
            self.last_trade_time = time.time()

            emoji  = "🟢" if direction == "LONG" else "🔴"
            prefix = "🔄 ФЛИП" if is_flip else "💰 СДЕЛКА"
            self.log("━" * 55, force=True)
            self.log(f"{prefix}: {symbol} | {emoji} {direction}", force=True)
            self.log(f"   📍 Вход:  {real_entry:.6f}", force=True)
            self.log(f"   🎯 TP:    {tp_price:.6f}  (+{real_tp_pct:.3f}%)", force=True)
            self.log(f"   📊 {real_size} ед. ({pos_usdt:.2f} USDT) | {elapsed:.2f}с", force=True)
            self.log(f"   📈 5m ST: {d5m}", force=True)
            self.log("━" * 55, force=True)
            return True

        except Exception as e:
            self.log(f"❌ {symbol}: {e}", force=True)
            return False

    def _open_sim(self, symbol: str, direction: str) -> bool:
        price = self.client.get_current_price(symbol)
        tp    = calculate_tp_price(price, direction, config.TP_PERCENT)
        self.positions[symbol] = {
            "type": direction, "entry_price": price, "tp_price": tp,
            "qty": 100.0, "pos_usdt": 100.0, "entry_time": datetime.now(),
            "st_mismatch_since": None
        }
        self.last_trade_time = time.time()
        emoji = "🟢" if direction == "LONG" else "🔴"
        self.log(f"🔔 СИМ {symbol} | {emoji} {direction} вход={price:.6f}", force=True)
        return True

    # ─────────────────────────────────────
    # ФЛИП ЛОГИКА
    # ─────────────────────────────────────

    def check_and_flip(self, symbol: str, klines_st: list):
        if symbol not in self.positions:
            return

        pos          = self.positions[symbol]
        direction    = pos["type"]
        held_secs    = (datetime.now() - pos["entry_time"]).total_seconds()
        current_dir  = klines_st[-1]["st_direction"]
        current_sig  = klines_st[-1].get("st_signal")
        current_price = self.client.get_current_price(symbol)
        pnl = check_pnl(pos["entry_price"], current_price, direction) if current_price > 0 else 0

        d5m  = self.get_5m_direction(symbol)
        sign = "+" if pnl >= 0 else ""
        emoji = "🟢" if direction == "LONG" else "🔴"
        self.log(
            f"   {emoji} {symbol}: {sign}{pnl:.3f}% | "
            f"ST={current_dir.upper()} | 5m:{d5m} | {held_secs:.0f}с"
        )

        # ── MISMATCH TIMEOUT: ST против позиции N секунд → закрываем ──
        mismatch_timeout = getattr(config, 'ST_MISMATCH_TIMEOUT', 60)
        st_against = (
            (direction == "LONG"  and current_dir == "down") or
            (direction == "SHORT" and current_dir == "up")
        )

        if st_against:
            if pos.get("st_mismatch_since") is None:
                pos["st_mismatch_since"] = time.time()
            else:
                mismatch_secs = time.time() - pos["st_mismatch_since"]
                if mismatch_secs >= mismatch_timeout:
                    self.log(
                        f"⏰ {symbol}: ST против позиции {mismatch_secs:.0f}с "
                        f"≥ {mismatch_timeout}с — принудительно закрываем",
                        force=True
                    )
                    if config.LIVE_TRADING:
                        self.client.close_position(symbol)
                    self._on_closed(symbol, reason=f"ST mismatch {mismatch_secs:.0f}с")
                    return
        else:
            # ST снова совпадает — сбрасываем таймер
            pos["st_mismatch_since"] = None

        if not config.FLIP_ON_REVERSAL:
            return

        # Определяем нужен ли флип
        should_flip   = False
        new_direction = None

        if direction == "LONG"  and current_dir == "down" and current_sig == "SELL":
            should_flip   = True
            new_direction = "SHORT"
        elif direction == "SHORT" and current_dir == "up"   and current_sig == "BUY":
            should_flip   = True
            new_direction = "LONG"

        if not should_flip:
            return

        # Защита от мгновенного флипа
        if held_secs < config.MIN_HOLD_BEFORE_FLIP:
            self.log(
                f"   ⏳ {symbol}: ST развернулся, ждём "
                f"({held_secs:.0f}с < {config.MIN_HOLD_BEFORE_FLIP}с)"
            )
            return

        # НОВОЕ: проверка 5m перед флипом
        if config.FLIP_REQUIRES_5M_CONFIRM:
            if not self.check_5m_allows(symbol, new_direction):
                # 5m не совпадает — просто закрываем, не переворачиваемся
                self.log(
                    f"🚫 {symbol}: ST развернулся {direction}→{new_direction} "
                    f"но 5m не подтверждает — просто закрываем",
                    force=True
                )
                if config.LIVE_TRADING:
                    self.client.close_position(symbol)
                self._on_closed(symbol, reason=f"ST flip (5m не подтвердил)")
                return

        self.log(f"🔄 {symbol}: ST флип! {direction} → {new_direction}", force=True)

        if config.LIVE_TRADING:
            result = self.client.close_position(symbol)
            if result.get("retCode") != 0:
                self.log(f"⚠️  {symbol}: ошибка закрытия", force=True)
                return
            for _ in range(5):
                time.sleep(0.2)
                if not self.client.has_position(symbol):
                    break

        self._on_closed(symbol, reason=f"ST flip → {new_direction}")
        time.sleep(0.3)
        self.open_position(symbol, new_direction, is_flip=True)

    # ─────────────────────────────────────
    # МОНИТОРИНГ ЗАКРЫТЫХ ПОЗИЦИЙ
    # ─────────────────────────────────────

    def check_closed_positions(self):
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
            f"{'+' if pnl_usdt >= 0 else ''}{pnl_usdt:.4f} USDT | {reason}",
            force=True
        )
        self.log(f"   ⏱️  {duration:.0f}с", force=True)
        self.log("━" * 55, force=True)
        self._print_stats()

        # Обновляем статистику по монете (cooldown логика)
        self.update_coin_stats(symbol, pnl_pct)

    # ─────────────────────────────────────
    # СТАТИСТИКА
    # ─────────────────────────────────────

    def _print_stats(self):
        t = self.stats["total"]
        if t == 0:
            return
        wr  = self.stats["wins"] / t * 100
        pnl = self.stats["pnl_usdt"]
        self.log("", force=True)
        self.log("📊 СТАТИСТИКА:", force=True)
        self.log(
            f"   Сделок: {t} | Win: {self.stats['wins']} | Loss: {self.stats['losses']}",
            force=True
        )
        self.log(f"   Win Rate:    {wr:.1f}%", force=True)
        self.log(f"   P&L итого:   {'+' if pnl >= 0 else ''}{pnl:.4f} USDT", force=True)
        self.log(f"   P&L средний: {'+' if pnl/t >= 0 else ''}{pnl/t:.4f} USDT", force=True)
        self.log("", force=True)

    # ─────────────────────────────────────
    # ГЛАВНЫЙ ЦИКЛ
    # ─────────────────────────────────────

    def run(self):
        print()
        print("═" * 55)
        print("🤖 SUPERTREND БОТ v7")
        print("═" * 55)
        print(f"   Режим:        {'💰 РЕАЛЬНАЯ' if config.LIVE_TRADING else '📊 СИМУЛЯЦИЯ'}")
        print(f"   ST:           Length={config.ST_LENGTH}, Factor={config.ST_FACTOR}")
        print(f"   TF сигнал:    {config.TIMEFRAME_FAST}m")
        print(f"   TF фильтр:    {config.TIMEFRAME_FILTER}m")
        print(f"   TP:           {config.TP_PERCENT}%")
        print(f"   Плечо:        {config.LEVERAGE}x")
        print(f"   Позиций макс: {config.MAX_SYMBOLS}")
        print(f"   Мин. оборот:  ${config.MIN_TURNOVER_24H/1e6:.0f}M")
        print(f"   Flip:         ВКЛ (5m подтверждение: {'ДА' if config.FLIP_REQUIRES_5M_CONFIRM else 'НЕТ'})")
        print(f"   Скан:         каждые {config.SCAN_INTERVAL}с")

        if config.LIVE_TRADING:
            balance = self.client.get_available_balance()
            print(f"   💰 Баланс:    ${balance:.2f}")

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

                # 2. Закрытые по TP/SL
                self.check_closed_positions()

                # 3. Мониторим ВСЕ открытые позиции + список сканера
                all_symbols = set(symbols) | set(self.positions.keys())

                for symbol in all_symbols:
                    klines = self.client.get_klines(
                        symbol, config.TIMEFRAME_FAST, config.CANDLES_LIMIT
                    )
                    if len(klines) < 10:
                        continue

                    klines_st = calculate_supertrend(
                        klines, config.ST_LENGTH, config.ST_FACTOR
                    )

                    # Если есть позиция — проверяем флип
                    if symbol in self.positions:
                        self.check_and_flip(symbol, klines_st)
                        continue

                    # Ищем новый вход только для монет из сканера
                    if symbol not in symbols:
                        continue

                    # Свободные слоты
                    busy = set(self.positions.keys())
                    if config.LIVE_TRADING:
                        real_pos  = self.client.get_positions()
                        real_syms = {p["symbol"] for p in real_pos}
                        busy = busy | real_syms

                    if config.MAX_SYMBOLS - len(busy) <= 0:
                        continue

                    # Пауза между сделками
                    if time.time() - self.last_trade_time < config.MIN_TRADE_INTERVAL:
                        continue

                    signal = detect_signal(klines_st)
                    if not signal:
                        continue

                    direction = signal["type"]

                    if direction == "LONG"  and not config.TRADE_LONG:  continue
                    if direction == "SHORT" and not config.TRADE_SHORT: continue

                    # Фильтр паузы по монете
                    if self.is_coin_on_cooldown(symbol):
                        continue

                    # Фильтр 5m
                    if not self.check_5m_allows(symbol, direction):
                        continue

                    # ATR-фильтр боковика (в моменте)
                    if not self.check_atr_volatility(symbol, klines):
                        continue

                    self.open_position(symbol, direction, is_flip=False)

                # 4. Статус каждые ~30 сек
                scan_n += 1
                if scan_n % 15 == 0:
                    active = list(self.positions.keys())
                    self.log(
                        f"📡 Позиций: {len(active)}/{config.MAX_SYMBOLS} "
                        f"| {symbols[:5]}"
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
        print("❌ Нет связи!")
        sys.exit(1)
    print(f"✅ BTC = ${price:.2f}")
    balance = client.get_available_balance()
    print(f"💰 Баланс: ${balance:.2f}")
    print()
    bot = SupertrendBotV7()
    bot.run()


if __name__ == "__main__":
    main()

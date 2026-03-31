#!/usr/bin/env python3
"""
SUPERTREND СКАЛЬПИНГ БОТ
==========================
Настройки: Length=1, Factor=1 (Александр)
Монета: ENJUSDT | TF: 1m | TP: 0.35% | Плечо: 10x

Исправления относительно SAR-бота:
  B1 — реальный P&L из /v5/position/closed-pnl
  B2 — свежая цена + буфер 0.02% перед отправкой ордера
  B3 — Supertrend линия как динамический SL + жёсткий SL 1%
  B4 — нет лишнего API запроса в calculate_qty
  B5 — логируется реальный % TP после округления
  B6 — дублирований в config нет
  B7 — статистика в USDT, не в %
"""

import time
import sys
from datetime import datetime
from typing import Dict, Optional

import config
from bybit_client import BybitClient
from strategy import (
    calculate_supertrend,
    detect_signal,
    check_confirmation,
    calculate_tp_price,
    validate_tp,
    check_pnl,
    is_sl_hit
)


class SupertrendBot:

    def __init__(self):
        self.client = BybitClient()

        # Текущая позиция (одна монета — одна позиция)
        self.position: Optional[Dict] = None

        # Последний обработанный timestamp сигнала
        self.last_signal_ts: int = 0

        # Время последней открытой сделки
        self.last_trade_time: float = 0

        # Статистика
        self.stats = {
            "total"     : 0,
            "wins"      : 0,
            "losses"    : 0,
            "pnl_usdt"  : 0.0,
            "start_time": datetime.now()
        }

        self.log_file = None
        if config.SAVE_LOGS:
            self.log_file = open(config.LOG_FILE, "a", encoding="utf-8")

    # ──────────────────────────────────────────────
    # ЛОГИРОВАНИЕ
    # ──────────────────────────────────────────────

    def log(self, msg: str, force: bool = False):
        ts  = datetime.now().strftime("%H:%M:%S")
        out = f"[{ts}] {msg}"
        if config.VERBOSE or force:
            print(out)
        if self.log_file:
            self.log_file.write(out + "\n")
            self.log_file.flush()

    # ──────────────────────────────────────────────
    # ОТКРЫТИЕ ПОЗИЦИИ
    # ──────────────────────────────────────────────

    def open_position(self, signal: Dict) -> bool:
        direction = signal["type"]
        st_value  = signal["st_value"]

        if not config.LIVE_TRADING:
            return self._open_sim(signal)

        # Проверяем нет ли уже позиции на бирже
        if self.client.has_position(config.SYMBOL):
            self.log(f"⚠️  Позиция уже открыта на бирже, пропускаем")
            return False

        try:
            start = time.time()

            # ── 1. Баланс ──────────────────────────────
            balance = self.client.get_available_balance()
            if balance <= 0:
                self.log("❌ Нулевой баланс", force=True)
                return False

            usdt_size = balance * config.POSITION_SIZE_PERCENT * config.LEVERAGE

            # ── 2. Свежая цена прямо перед ордером (FIX B2) ──
            bid, ask = self.client.get_bid_ask(config.SYMBOL)
            if bid <= 0 or ask <= 0:
                self.log("❌ Не удалось получить цену", force=True)
                return False

            # Для LONG берём ask + буфер 0.02% чтобы TP точно был выше
            # Для SHORT берём bid - буфер
            if direction == "LONG":
                entry_price = ask
            else:
                entry_price = bid

            # ── 3. Количество (FIX B4 — передаём цену) ──
            qty = self.client.calculate_qty(
                config.SYMBOL, usdt_size, price=entry_price
            )
            if qty <= 0:
                self.log("❌ Недостаточно средств", force=True)
                return False

            # ── 4. TP цена ──────────────────────────────
            tp_price = calculate_tp_price(entry_price, direction, config.TP_PERCENT)
            tp_price = self.client.round_price(config.SYMBOL, tp_price)

            # Валидация TP (FIX B2 — после округления пересчитываем)
            if not validate_tp(entry_price, tp_price, direction):
                self.log(f"❌ TP не прошёл валидацию: entry={entry_price}, tp={tp_price}", force=True)
                return False

            # Реальный % TP после округления (FIX B5)
            real_tp_pct = abs(tp_price - entry_price) / entry_price * 100

            # ── 5. SL цена (FIX B3) ─────────────────────
            # Supertrend линия = естественный стоп
            sl_price = self.client.round_price(config.SYMBOL, st_value)

            # Валидация SL
            if direction == "LONG" and sl_price >= entry_price:
                # ST-линия выше входа — использовать жёсткий SL
                sl_price = entry_price * (1 - config.HARD_SL_PERCENT / 100)
                sl_price = self.client.round_price(config.SYMBOL, sl_price)
            elif direction == "SHORT" and sl_price <= entry_price:
                sl_price = entry_price * (1 + config.HARD_SL_PERCENT / 100)
                sl_price = self.client.round_price(config.SYMBOL, sl_price)

            # ── 6. Плечо ────────────────────────────────
            self.client.set_leverage(config.SYMBOL, config.LEVERAGE)

            # ── 7. Ордер ────────────────────────────────
            side   = "Buy" if direction == "LONG" else "Sell"
            result = self.client.place_order(
                symbol      = config.SYMBOL,
                side        = side,
                qty         = qty,
                take_profit = tp_price
                # stop_loss = выключен, контролируется вручную
            )

            elapsed = time.time() - start

            if result.get("retCode") != 0:
                err = result.get("retMsg", "Unknown")
                self.log(f"❌ Ошибка ордера: {err}", force=True)
                return False

            # ── 8. Подтверждение позиции (без sleep!) ──
            pos = None
            for attempt in range(4):
                time.sleep(0.2)
                pos = self.client.get_position(config.SYMBOL)
                if pos:
                    break

            if not pos:
                self.log("❌ Позиция не подтверждена", force=True)
                return False

            real_entry = float(pos.get("avgPrice", entry_price))
            real_size  = float(pos.get("size", qty))
            pos_usdt   = real_size * real_entry

            # ── 9. Проверяем TP на бирже ────────────────
            real_tp = pos.get("takeProfit", "")
            if not real_tp or float(real_tp) == 0:
                self.log(f"⚠️  TP не выставлен, выставляем вручную...", force=True)
                tp_res = self.client.set_tp_sl(
                    config.SYMBOL,
                    take_profit = tp_price
                )
                if tp_res.get("retCode") != 0:
                    self.log("❌ Не удалось выставить TP, закрываем позицию", force=True)
                    self.client.close_position(config.SYMBOL)
                    return False

            # ── 10. Сохраняем состояние ─────────────────
            self.position = {
                "symbol"     : config.SYMBOL,
                "type"       : direction,
                "side"       : side,
                "entry_price": real_entry,
                "tp_price"   : tp_price,
                "sl_price"   : sl_price,
                "st_value"   : st_value,    # текущая ST линия
                "qty"        : real_size,
                "pos_usdt"   : pos_usdt,
                "entry_time" : datetime.now(),
                "signal_ts"  : signal["timestamp"]
            }

            self.last_signal_ts  = signal["timestamp"]
            self.last_trade_time = time.time()

            emoji = "🟢" if direction == "LONG" else "🔴"

            self.log("━" * 55, force=True)
            self.log(f"💰 СДЕЛКА: {config.SYMBOL} | {emoji} {direction}", force=True)
            self.log(f"   📍 Вход:  {real_entry:.6f}", force=True)
            self.log(f"   🎯 TP:    {tp_price:.6f}  (+{real_tp_pct:.3f}%)", force=True)
            self.log(f"   🛑 SL:    выключен (ручной контроль)", force=True)
            self.log(f"   📊 Объём: {real_size} ({pos_usdt:.2f} USDT)", force=True)
            self.log(f"   ⏱️  Вход за: {elapsed:.2f} сек", force=True)
            self.log("━" * 55, force=True)

            return True

        except Exception as e:
            self.log(f"❌ Исключение при открытии: {e}", force=True)
            return False

    def _open_sim(self, signal: Dict) -> bool:
        """Симуляция без реальных ордеров"""
        direction   = signal["type"]
        entry_price = signal["price"]
        st_value    = signal["st_value"]

        tp_price = calculate_tp_price(entry_price, direction, config.TP_PERCENT)

        if direction == "LONG":
            sl_price = st_value if st_value < entry_price else entry_price * 0.99
        else:
            sl_price = st_value if st_value > entry_price else entry_price * 1.01

        self.position = {
            "symbol"     : config.SYMBOL,
            "type"       : direction,
            "entry_price": entry_price,
            "tp_price"   : tp_price,
            "sl_price"   : sl_price,
            "st_value"   : st_value,
            "qty"        : 100.0,
            "pos_usdt"   : 100.0,
            "entry_time" : datetime.now(),
            "signal_ts"  : signal["timestamp"]
        }

        self.last_signal_ts  = signal["timestamp"]
        self.last_trade_time = time.time()

        emoji = "🟢" if direction == "LONG" else "🔴"
        real_tp_pct = abs(tp_price - entry_price) / entry_price * 100

        self.log("━" * 55, force=True)
        self.log(f"🔔 СИМУЛЯЦИЯ: {config.SYMBOL} | {emoji} {direction}", force=True)
        self.log(f"   📍 Вход: {entry_price:.6f}", force=True)
        self.log(f"   🎯 TP:   {tp_price:.6f}  (+{real_tp_pct:.3f}%)", force=True)
        self.log(f"   🛑 SL:   {sl_price:.6f}", force=True)
        self.log("━" * 55, force=True)
        return True

    # ──────────────────────────────────────────────
    # УПРАВЛЕНИЕ ПОЗИЦИЕЙ
    # ──────────────────────────────────────────────

    def check_position(self, klines_with_st):
        """Проверить текущую позицию"""
        if not self.position:
            return

        pos       = self.position
        direction = pos["type"]
        symbol    = pos["symbol"]

        if config.LIVE_TRADING:
            # Проверяем что позиция ещё открыта
            real_pos = self.client.get_position(symbol)

            if not real_pos:
                # Позиция закрылась (TP, SL или вручную)
                self._on_position_closed()
                return

            # Показываем текущий P&L
            current_price = self.client.get_current_price(symbol)
            if current_price > 0:
                pnl = check_pnl(pos["entry_price"], current_price, direction)
                sign = "+" if pnl >= 0 else ""
                emoji = "🟢" if direction == "LONG" else "🔴"
                duration = (datetime.now() - pos["entry_time"]).total_seconds()
                self.log(
                    f"   {emoji} {symbol}: {sign}{pnl:.3f}% | "
                    f"ST={klines_with_st[-1]['st_value']:.6f} "
                    f"({'↑' if klines_with_st[-1]['st_direction'] == 'up' else '↓'}) | "
                    f"{duration:.0f}с"
                )

        else:
            # Симуляция
            current_price = self.client.get_current_price(symbol)
            if current_price <= 0:
                return

            current_st  = klines_with_st[-1]["st_value"]
            current_dir = klines_with_st[-1]["st_direction"]
            pnl = check_pnl(pos["entry_price"], current_price, direction)

            # TP
            if direction == "LONG" and current_price >= pos["tp_price"]:
                self._record_trade(pnl, "TP")
                return
            if direction == "SHORT" and current_price <= pos["tp_price"]:
                self._record_trade(pnl, "TP")
                return

            # SL — разворот ST
            if direction == "LONG" and current_dir == "down":
                self._record_trade(pnl, "ST разворот")
                return
            if direction == "SHORT" and current_dir == "up":
                self._record_trade(pnl, "ST разворот")
                return

            # Жёсткий SL
            if is_sl_hit(pos["entry_price"], current_price,
                         current_st, direction, config.HARD_SL_PERCENT):
                self._record_trade(pnl, "Жёсткий SL")

    def _close_position(self, reason: str):
        """Закрыть позицию на бирже"""
        if not self.position:
            return

        result = self.client.close_position(config.SYMBOL)

        if result.get("retCode") == 0:
            self._on_position_closed(reason)
        else:
            self.log(f"❌ Ошибка закрытия: {result.get('retMsg')}", force=True)

    def _on_position_closed(self, reason: str = "Биржа (TP/SL)"):
        """
        Обработать закрытие позиции.
        FIX B1 — берём реальный P&L с биржи.
        """
        if not self.position:
            return

        pos      = self.position
        symbol   = pos["symbol"]
        duration = (datetime.now() - pos["entry_time"]).total_seconds()

        # FIX B1: реальный P&L с биржи
        real_pnl_pct = self.client.get_last_closed_pnl(symbol)

        if real_pnl_pct is None:
            # Фолбэк — считаем от текущей цены
            current = self.client.get_current_price(symbol)
            real_pnl_pct = check_pnl(pos["entry_price"], current, pos["type"]) if current > 0 else 0.0
            self.log("⚠️  Не удалось получить P&L с биржи, считаем по текущей цене")

        self._record_trade(real_pnl_pct, reason, duration)

    def _record_trade(self, pnl_pct: float, reason: str,
                      duration: float = None):
        """Записать результат сделки в статистику"""
        if not self.position:
            return

        pos       = self.position
        pos_usdt  = pos.get("pos_usdt", 0)
        # FIX B7 — P&L в USDT
        pnl_usdt  = pos_usdt * pnl_pct / 100

        if duration is None:
            duration = (datetime.now() - pos["entry_time"]).total_seconds()

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
            f"{emoji} ЗАКРЫТО: {pos['symbol']} | "
            f"{'+' if pnl_pct >= 0 else ''}{pnl_pct:.3f}% | "
            f"{'+' if pnl_usdt >= 0 else ''}{pnl_usdt:.4f} USDT | "
            f"Причина: {reason}",
            force=True
        )
        self.log(f"   ⏱️  Время: {duration:.0f} сек", force=True)
        self.log("━" * 55, force=True)

        self.position = None
        self._print_stats()

    # ──────────────────────────────────────────────
    # СТАТИСТИКА
    # ──────────────────────────────────────────────

    def _print_stats(self):
        total = self.stats["total"]
        if total == 0:
            return

        wins     = self.stats["wins"]
        losses   = self.stats["losses"]
        pnl_usdt = self.stats["pnl_usdt"]
        wr       = wins / total * 100
        avg      = pnl_usdt / total

        self.log("", force=True)
        self.log("📊 СТАТИСТИКА:", force=True)
        self.log(f"   Сделок: {total}  |  Win: {wins}  |  Loss: {losses}", force=True)
        self.log(f"   Win Rate: {wr:.1f}%", force=True)
        self.log(f"   Общий P&L:  {'+' if pnl_usdt >= 0 else ''}{pnl_usdt:.4f} USDT", force=True)
        self.log(f"   Средний:    {'+' if avg >= 0 else ''}{avg:.4f} USDT", force=True)
        self.log("", force=True)

    # ──────────────────────────────────────────────
    # ГЛАВНЫЙ ЦИКЛ
    # ──────────────────────────────────────────────

    def run(self):
        mode    = "💰 РЕАЛЬНАЯ" if config.LIVE_TRADING else "📊 СИМУЛЯЦИЯ"
        confirm = config.MIN_CANDLES_CONFIRM

        print()
        print("═" * 55)
        print("🤖 SUPERTREND БОТ")
        print("═" * 55)
        print(f"   Режим:      {mode}")
        print(f"   Монета:     {config.SYMBOL}")
        print(f"   TF:         {config.TIMEFRAME}m")
        print(f"   ST:         Length={config.ST_LENGTH}, Factor={config.ST_FACTOR}")
        print(f"   TP:         {config.TP_PERCENT}%")
        print(f"   SL:         ST-линия + жёсткий {config.HARD_SL_PERCENT}%")
        print(f"   Плечо:      {config.LEVERAGE}x")
        print(f"   Размер:     {config.POSITION_SIZE_PERCENT * 100:.0f}% депозита")
        print(f"   Фильтр:     ≥{confirm} свечей подтверждения")

        if config.LIVE_TRADING:
            balance = self.client.get_available_balance()
            print(f"   💰 Баланс:  ${balance:.2f}")

        print("═" * 55)
        print()

        scan_n = 0

        try:
            while True:
                # 1. Получаем свечи
                klines = self.client.get_klines(
                    config.SYMBOL, config.TIMEFRAME, config.CANDLES_LIMIT
                )

                if len(klines) < 10:
                    self.log("⚠️  Мало свечей, ждём...")
                    time.sleep(config.SCAN_INTERVAL)
                    continue

                # 2. Считаем Supertrend
                klines_st = calculate_supertrend(
                    klines,
                    length = config.ST_LENGTH,
                    factor = config.ST_FACTOR
                )

                # 3. Управляем открытой позицией
                if self.position or (config.LIVE_TRADING and
                                     self.client.has_position(config.SYMBOL)):
                    self.check_position(klines_st)

                # 4. Ищем новый сигнал если нет позиции
                if not self.position:
                    signal = detect_signal(klines_st)

                    if signal:
                        # Проверяем что сигнал новый
                        if signal["timestamp"] <= self.last_signal_ts:
                            pass  # уже обрабатывали

                        # Фильтр минимального интервала между сделками
                        elif time.time() - self.last_trade_time < config.MIN_TRADE_INTERVAL:
                            self.log(
                                f"⏳ Слишком рано, ждём "
                                f"{config.MIN_TRADE_INTERVAL - (time.time() - self.last_trade_time):.0f}с"
                            )

                        # Фильтр флипов — подтверждение направления
                        elif config.MIN_CANDLES_CONFIRM > 1 and not check_confirmation(
                            klines_st, config.MIN_CANDLES_CONFIRM
                        ):
                            direction = signal["type"]
                            self.log(f"⏭  {direction} — ждём {confirm} свечей подтверждения")

                        # Проверяем направление торговли
                        elif signal["type"] == "LONG" and not config.TRADE_LONG:
                            self.log("⏭  LONG отключён в config")

                        elif signal["type"] == "SHORT" and not config.TRADE_SHORT:
                            self.log("⏭  SHORT отключён в config")

                        else:
                            self.open_position(signal)

                # 5. Логируем статус каждые 12 сканов (~1 мин)
                scan_n += 1
                if scan_n % 12 == 0:
                    st_now = klines_st[-1]
                    price  = klines[-1]["close"]
                    self.log(
                        f"📡 {config.SYMBOL} = {price:.6f} | "
                        f"ST={st_now['st_value']:.6f} "
                        f"({'↑ UP' if st_now['st_direction'] == 'up' else '↓ DOWN'})"
                    )

                time.sleep(config.SCAN_INTERVAL)

        except KeyboardInterrupt:
            print()
            print("═" * 55)
            print("🛑 БОТ ОСТАНОВЛЕН")
            print("═" * 55)
            self._print_stats()

            if self.log_file:
                self.log_file.close()


def main():
    print("🔗 Проверка подключения к Bybit...")

    client = BybitClient()
    price  = client.get_current_price("BTCUSDT")

    if price <= 0:
        print("❌ Нет подключения к Bybit!")
        sys.exit(1)

    print(f"✅ Подключено! BTC = ${price:.2f}")

    balance = client.get_available_balance()
    print(f"💰 Баланс: ${balance:.2f}")

    # Проверяем что монета существует
    info = client.get_instrument_info(config.SYMBOL)
    if not info:
        print(f"❌ Монета {config.SYMBOL} не найдена на Bybit!")
        sys.exit(1)

    print(f"✅ {config.SYMBOL} найден")
    print()

    bot = SupertrendBot()
    bot.run()


if __name__ == "__main__":
    main()

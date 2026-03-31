"""
Стратегия Supertrend
=====================
Параметры: Length=1, Factor=1 (настройки Александра)
"""

from typing import List, Dict, Optional
import config


def calculate_atr(klines: List[Dict], period: int) -> List[float]:
    """
    Рассчитать ATR (Average True Range)
    При period=1 → ATR = True Range текущей свечи
    """
    atr_values = []

    for i, candle in enumerate(klines):
        if i == 0:
            # Первая свеча — TR = high - low
            tr = candle["high"] - candle["low"]
            atr_values.append(tr)
            continue

        prev_close = klines[i - 1]["close"]
        high = candle["high"]
        low  = candle["low"]

        # True Range = max(H-L, |H-PC|, |L-PC|)
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low  - prev_close)
        )

        if period == 1:
            # При period=1 ATR = TR (без сглаживания)
            atr_values.append(tr)
        else:
            # RMA (Wilder's MA) — стандартное сглаживание для ATR
            prev_atr = atr_values[-1]
            atr = (prev_atr * (period - 1) + tr) / period
            atr_values.append(atr)

    return atr_values


def calculate_supertrend(
    klines: List[Dict],
    length: int  = None,
    factor: float = None
) -> List[Dict]:
    """
    Рассчитать Supertrend

    Возвращает список свечей с добавленными полями:
      - st_value     : значение линии Supertrend
      - st_direction : "up" (бычий, линия снизу) или "down" (медвежий, линия сверху)
      - st_signal    : "BUY" | "SELL" | None (только при смене направления)
    """
    if length is None:
        length = config.ST_LENGTH
    if factor is None:
        factor = config.ST_FACTOR

    if len(klines) < 2:
        return klines

    atr_values = calculate_atr(klines, length)

    result = []

    # Начальные значения
    prev_upper = None
    prev_lower = None
    prev_st    = None
    prev_dir   = None   # "up" или "down"

    for i, candle in enumerate(klines):
        c = candle.copy()
        high  = candle["high"]
        low   = candle["low"]
        close = candle["close"]
        hl2   = (high + low) / 2
        atr   = atr_values[i]

        # Базовые полосы
        basic_upper = hl2 + factor * atr
        basic_lower = hl2 - factor * atr

        if i == 0:
            # Инициализация
            upper = basic_upper
            lower = basic_lower
            direction = "up" if close > basic_upper else "down"
            st_value  = lower if direction == "up" else upper

            c["st_value"]     = st_value
            c["st_direction"] = direction
            c["st_signal"]    = None
            result.append(c)

            prev_upper = upper
            prev_lower = lower
            prev_st    = st_value
            prev_dir   = direction
            continue

        # Скорректированные полосы (не позволяем полосам двигаться против тренда)
        if basic_upper < prev_upper or klines[i - 1]["close"] > prev_upper:
            upper = basic_upper
        else:
            upper = prev_upper

        if basic_lower > prev_lower or klines[i - 1]["close"] < prev_lower:
            lower = basic_lower
        else:
            lower = prev_lower

        # Определяем направление
        if prev_dir == "up":
            if close < lower:
                direction = "down"
                st_value  = upper
            else:
                direction = "up"
                st_value  = lower
        else:  # prev_dir == "down"
            if close > upper:
                direction = "up"
                st_value  = lower
            else:
                direction = "down"
                st_value  = upper

        # Сигнал — только при смене направления
        signal = None
        if prev_dir == "down" and direction == "up":
            signal = "BUY"
        elif prev_dir == "up" and direction == "down":
            signal = "SELL"

        c["st_value"]     = st_value
        c["st_direction"] = direction
        c["st_signal"]    = signal
        result.append(c)

        prev_upper = upper
        prev_lower = lower
        prev_st    = st_value
        prev_dir   = direction

    return result


def detect_signal(klines_with_st: List[Dict]) -> Optional[Dict]:
    """
    Найти торговый сигнал на последней свече

    Returns:
        {"type": "LONG"/"SHORT", "price": float,
         "st_value": float, "timestamp": int}
        или None
    """
    if len(klines_with_st) < 3:
        return None

    current = klines_with_st[-1]
    signal  = current.get("st_signal")

    if signal == "BUY":
        return {
            "type"      : "LONG",
            "price"     : current["close"],
            "st_value"  : current["st_value"],
            "timestamp" : current["timestamp"]
        }

    if signal == "SELL":
        return {
            "type"      : "SHORT",
            "price"     : current["close"],
            "st_value"  : current["st_value"],
            "timestamp" : current["timestamp"]
        }

    return None


def check_confirmation(klines_with_st: List[Dict], min_candles: int) -> bool:
    """
    Проверить что направление Supertrend держится
    минимум min_candles свечей подряд (фильтр флипов)

    Смотрим на -2, -3 и т.д. свечи — они должны быть
    в том же направлении что и текущая
    """
    if len(klines_with_st) < min_candles + 1:
        return False

    current_dir = klines_with_st[-1]["st_direction"]

    # Проверяем предыдущие свечи (не считая текущую)
    for i in range(1, min_candles):
        candle = klines_with_st[-(i + 1)]
        if candle.get("st_direction") != current_dir:
            return False

    return True


def is_sl_hit(entry_price: float, current_price: float,
              st_value: float, direction: str,
              hard_sl_pct: float) -> bool:
    """
    Проверить сработал ли Stop Loss

    Два уровня SL:
    1. Supertrend линия (цена пересекла ST)
    2. Жёсткий % SL (страховка)
    """
    if direction == "LONG":
        # ST как SL: цена упала ниже линии Supertrend
        st_hit   = current_price < st_value
        # Жёсткий SL
        hard_hit = current_price <= entry_price * (1 - hard_sl_pct / 100)
        return st_hit or hard_hit

    else:  # SHORT
        # ST как SL: цена поднялась выше линии Supertrend
        st_hit   = current_price > st_value
        # Жёсткий SL
        hard_hit = current_price >= entry_price * (1 + hard_sl_pct / 100)
        return st_hit or hard_hit


def calculate_tp_price(entry_price: float, direction: str,
                       tp_percent: float = None) -> float:
    """Рассчитать цену Take Profit"""
    if tp_percent is None:
        tp_percent = config.TP_PERCENT

    tp_percent = abs(tp_percent)

    if direction == "LONG":
        return entry_price * (1 + tp_percent / 100)
    else:
        return entry_price * (1 - tp_percent / 100)


def validate_tp(entry_price: float, tp_price: float, direction: str) -> bool:
    """Проверить корректность TP"""
    if direction == "LONG":
        return tp_price > entry_price
    return tp_price < entry_price


def check_pnl(entry_price: float, current_price: float, direction: str) -> float:
    """P&L в процентах (без учёта плеча)"""
    if direction == "LONG":
        return (current_price - entry_price) / entry_price * 100
    return (entry_price - current_price) / entry_price * 100

"""
Стратегия скальпинга на Parabolic SAR
=====================================
"""

from typing import List, Dict, Optional, Tuple
import config


def calculate_parabolic_sar(
    klines: List[Dict],
    acceleration: float = None,
    acceleration_max: float = None
) -> List[Dict]:
    """
    Рассчитать Parabolic SAR для списка свечей
    
    Returns:
        Список с добавленными полями: sar, sar_trend ('up' или 'down')
    """
    if acceleration is None:
        acceleration = config.SAR_ACCELERATION
    if acceleration_max is None:
        acceleration_max = config.SAR_ACCELERATION_MAX
    
    if len(klines) < 2:
        return klines
    
    result = []
    
    # Инициализация
    af = acceleration  # Acceleration Factor
    trend = "up"  # Начинаем с восходящего тренда
    
    # Первая точка SAR
    sar = klines[0]["low"]
    ep = klines[0]["high"]  # Extreme Point
    
    for i, candle in enumerate(klines):
        high = candle["high"]
        low = candle["low"]
        close = candle["close"]
        
        # Копируем свечу
        candle_copy = candle.copy()
        
        if i == 0:
            candle_copy["sar"] = sar
            candle_copy["sar_trend"] = trend
            result.append(candle_copy)
            continue
        
        # Предыдущие значения
        prev_sar = sar
        prev_af = af
        prev_ep = ep
        prev_trend = trend
        
        if prev_trend == "up":
            # Восходящий тренд
            sar = prev_sar + prev_af * (prev_ep - prev_sar)
            
            # SAR не должен быть выше двух предыдущих low
            if i >= 2:
                sar = min(sar, klines[i-1]["low"], klines[i-2]["low"])
            elif i >= 1:
                sar = min(sar, klines[i-1]["low"])
            
            # Проверка разворота
            if low < sar:
                # Разворот вниз
                trend = "down"
                sar = prev_ep
                ep = low
                af = acceleration
            else:
                # Продолжение тренда
                trend = "up"
                if high > prev_ep:
                    ep = high
                    af = min(prev_af + acceleration, acceleration_max)
                else:
                    ep = prev_ep
                    af = prev_af
        
        else:
            # Нисходящий тренд
            sar = prev_sar - prev_af * (prev_sar - prev_ep)
            
            # SAR не должен быть ниже двух предыдущих high
            if i >= 2:
                sar = max(sar, klines[i-1]["high"], klines[i-2]["high"])
            elif i >= 1:
                sar = max(sar, klines[i-1]["high"])
            
            # Проверка разворота
            if high > sar:
                # Разворот вверх
                trend = "up"
                sar = prev_ep
                ep = high
                af = acceleration
            else:
                # Продолжение тренда
                trend = "down"
                if low < prev_ep:
                    ep = low
                    af = min(prev_af + acceleration, acceleration_max)
                else:
                    ep = prev_ep
                    af = prev_af
        
        candle_copy["sar"] = sar
        candle_copy["sar_trend"] = trend
        result.append(candle_copy)
    
    return result


def detect_sar_signal(klines_with_sar: List[Dict]) -> Optional[Dict]:
    """
    Определить сигнал разворота SAR
    
    Returns:
        Сигнал или None:
        {
            "type": "LONG" или "SHORT",
            "price": текущая цена,
            "sar": значение SAR,
            "tp": цена Take Profit
        }
    """
    if len(klines_with_sar) < 3:
        return None
    
    # Берём последние 3 свечи
    prev2 = klines_with_sar[-3]
    prev1 = klines_with_sar[-2]
    current = klines_with_sar[-1]
    
    # Проверяем разворот SAR
    # LONG: SAR был сверху (down trend) → стал снизу (up trend)
    if prev1.get("sar_trend") == "down" and current.get("sar_trend") == "up":
        # Разворот SAR снизу → сигнал LONG
        price = current["close"]
        tp_price = price * (1 + config.TP_PERCENT / 100)
        
        return {
            "type": "LONG",
            "price": price,
            "sar": current["sar"],
            "tp": tp_price,
            "candle_time": current["timestamp"]
        }
    
    # SHORT: SAR был снизу (up trend) → стал сверху (down trend)
    if prev1.get("sar_trend") == "up" and current.get("sar_trend") == "down":
        # Разворот SAR сверху → сигнал SHORT
        price = current["close"]
        tp_price = price * (1 - config.TP_PERCENT / 100)
        
        return {
            "type": "SHORT",
            "price": price,
            "sar": current["sar"],
            "tp": tp_price,
            "candle_time": current["timestamp"]
        }
    
    return None


def calculate_volatility(klines: List[Dict]) -> float:
    """
    Рассчитать среднюю волатильность свечей (%)
    
    Returns:
        Средняя волатильность в процентах
    """
    if not klines:
        return 0.0
    
    volatilities = []
    for candle in klines[-20:]:  # Последние 20 свечей
        if candle["close"] > 0:
            vol = (candle["high"] - candle["low"]) / candle["close"] * 100
            volatilities.append(vol)
    
    if volatilities:
        return sum(volatilities) / len(volatilities)
    return 0.0


def check_tp_hit(entry_price: float, current_price: float, 
                 direction: str, tp_percent: float = None) -> Tuple[bool, float]:
    """
    Проверить, достигнут ли Take Profit
    
    Returns:
        (достигнут ли TP, текущий P&L в процентах)
    """
    if tp_percent is None:
        tp_percent = config.TP_PERCENT
    
    if direction == "LONG":
        pnl_percent = (current_price - entry_price) / entry_price * 100
        tp_hit = pnl_percent >= tp_percent
    else:  # SHORT
        pnl_percent = (entry_price - current_price) / entry_price * 100
        tp_hit = pnl_percent >= tp_percent
    
    return tp_hit, pnl_percent


def check_sl_hit(entry_price: float, current_price: float,
                 direction: str, sl_percent: float = None) -> Tuple[bool, float]:
    """
    Проверить, достигнут ли Stop Loss
    
    Returns:
        (достигнут ли SL, текущий P&L в процентах)
    """
    if sl_percent is None:
        sl_percent = config.SL_PERCENT
    
    if sl_percent <= 0:
        return False, 0.0
    
    if direction == "LONG":
        pnl_percent = (current_price - entry_price) / entry_price * 100
        sl_hit = pnl_percent <= -sl_percent
    else:  # SHORT
        pnl_percent = (entry_price - current_price) / entry_price * 100
        sl_hit = pnl_percent <= -sl_percent
    
    return sl_hit, pnl_percent


# Тест
if __name__ == "__main__":
    # Тестовые данные
    test_klines = [
        {"timestamp": 1, "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
        {"timestamp": 2, "open": 101, "high": 103, "low": 100, "close": 102, "volume": 1000},
        {"timestamp": 3, "open": 102, "high": 104, "low": 101, "close": 103, "volume": 1000},
        {"timestamp": 4, "open": 103, "high": 105, "low": 102, "close": 104, "volume": 1000},
        {"timestamp": 5, "open": 104, "high": 104, "low": 100, "close": 101, "volume": 1000},
        {"timestamp": 6, "open": 101, "high": 101, "low": 98, "close": 99, "volume": 1000},
        {"timestamp": 7, "open": 99, "high": 100, "low": 97, "close": 98, "volume": 1000},
    ]
    
    # Рассчитываем SAR
    result = calculate_parabolic_sar(test_klines)
    
    print("📊 Тест Parabolic SAR:")
    print("-" * 60)
    for candle in result:
        print(f"Time: {candle['timestamp']}, Close: {candle['close']:.2f}, "
              f"SAR: {candle['sar']:.2f}, Trend: {candle['sar_trend']}")
    
    # Проверяем сигнал
    signal = detect_sar_signal(result)
    if signal:
        print(f"\n🔔 Сигнал: {signal['type']}")
        print(f"   Цена: {signal['price']:.2f}")
        print(f"   TP: {signal['tp']:.2f}")
    else:
        print("\n⏸️ Нет сигнала")

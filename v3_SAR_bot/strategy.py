"""
Стратегия Parabolic SAR v3.0
=============================
"""

from typing import List, Dict, Optional
import config


def calculate_parabolic_sar(
    klines: List[Dict],
    acceleration: float = None,
    acceleration_max: float = None
) -> List[Dict]:
    """
    Рассчитать Parabolic SAR
    """
    if acceleration is None:
        acceleration = config.SAR_ACCELERATION
    if acceleration_max is None:
        acceleration_max = config.SAR_ACCELERATION_MAX
    
    if len(klines) < 2:
        return klines
    
    result = []
    
    af = acceleration
    trend = "up"
    sar = klines[0]["low"]
    ep = klines[0]["high"]
    
    for i, candle in enumerate(klines):
        high = candle["high"]
        low = candle["low"]
        
        candle_copy = candle.copy()
        
        if i == 0:
            candle_copy["sar"] = sar
            candle_copy["sar_trend"] = trend
            result.append(candle_copy)
            continue
        
        prev_sar = sar
        prev_af = af
        prev_ep = ep
        prev_trend = trend
        
        if prev_trend == "up":
            sar = prev_sar + prev_af * (prev_ep - prev_sar)
            
            if i >= 2:
                sar = min(sar, klines[i-1]["low"], klines[i-2]["low"])
            elif i >= 1:
                sar = min(sar, klines[i-1]["low"])
            
            if low < sar:
                trend = "down"
                sar = prev_ep
                ep = low
                af = acceleration
            else:
                trend = "up"
                if high > prev_ep:
                    ep = high
                    af = min(prev_af + acceleration, acceleration_max)
                else:
                    ep = prev_ep
                    af = prev_af
        else:
            sar = prev_sar - prev_af * (prev_sar - prev_ep)
            
            if i >= 2:
                sar = max(sar, klines[i-1]["high"], klines[i-2]["high"])
            elif i >= 1:
                sar = max(sar, klines[i-1]["high"])
            
            if high > sar:
                trend = "up"
                sar = prev_ep
                ep = high
                af = acceleration
            else:
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


def detect_sar_reversal(klines_with_sar: List[Dict]) -> Optional[Dict]:
    """
    Обнаружить разворот SAR на ТЕКУЩЕЙ свече
    
    Returns:
        Сигнал или None
    """
    if len(klines_with_sar) < 3:
        return None
    
    # Берём последние 2 свечи
    prev = klines_with_sar[-2]
    current = klines_with_sar[-1]
    
    # LONG: был down trend → стал up trend (SAR перешёл снизу)
    if prev.get("sar_trend") == "down" and current.get("sar_trend") == "up":
        return {
            "type": "LONG",
            "price": current["close"],
            "sar": current["sar"],
            "timestamp": current["timestamp"]
        }
    
    # SHORT: был up trend → стал down trend (SAR перешёл сверху)
    if prev.get("sar_trend") == "up" and current.get("sar_trend") == "down":
        return {
            "type": "SHORT",
            "price": current["close"],
            "sar": current["sar"],
            "timestamp": current["timestamp"]
        }
    
    return None


def calculate_tp_price(entry_price: float, direction: str, tp_percent: float = None) -> float:
    """
    Рассчитать цену Take Profit
    
    ВАЖНО: Для LONG - TP должен быть ВЫШЕ entry
           Для SHORT - TP должен быть НИЖЕ entry
    """
    if tp_percent is None:
        tp_percent = config.TP_PERCENT
    
    # Убеждаемся что tp_percent положительный
    tp_percent = abs(tp_percent)
    
    if direction == "LONG":
        tp_price = entry_price * (1 + tp_percent / 100)
        # Валидация: TP должен быть ВЫШЕ входа
        if tp_price <= entry_price:
            tp_price = entry_price * 1.002  # Минимум 0.2%
    else:  # SHORT
        tp_price = entry_price * (1 - tp_percent / 100)
        # Валидация: TP должен быть НИЖЕ входа
        if tp_price >= entry_price:
            tp_price = entry_price * 0.998  # Минимум 0.2%
    
    return tp_price


def validate_tp(entry_price: float, tp_price: float, direction: str) -> bool:
    """
    Проверить корректность TP
    
    Returns:
        True если TP корректен
    """
    if direction == "LONG":
        return tp_price > entry_price
    else:  # SHORT
        return tp_price < entry_price


def check_pnl(entry_price: float, current_price: float, direction: str) -> float:
    """Рассчитать текущий P&L в процентах"""
    if direction == "LONG":
        return (current_price - entry_price) / entry_price * 100
    else:  # SHORT
        return (entry_price - current_price) / entry_price * 100

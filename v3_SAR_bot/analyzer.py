#!/usr/bin/env python3
"""
🔍 ПРОДВИНУТЫЙ АНАЛИЗАТОР МОНЕТ v2.0
=====================================
• Анализирует разные таймфреймы (5m, 15m, 1H, 4H, 1D)
• Определяет ОПТИМАЛЬНЫЙ TP для каждой монеты
• Находит монеты где SAR работает со 100% точностью
• Показывает рекомендуемые настройки

Запуск: python3 analyzer.py
"""

import time
import sys
from typing import Dict, List, Tuple
from bybit_client import BybitClient

# SAR параметры
SAR_ACCELERATION = 0.02
SAR_ACCELERATION_MAX = 0.2


def calculate_parabolic_sar(klines: List[Dict]) -> List[Dict]:
    """Рассчитать SAR"""
    if len(klines) < 2:
        return klines
    
    result = []
    af = SAR_ACCELERATION
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
                af = SAR_ACCELERATION
            else:
                trend = "up"
                if high > prev_ep:
                    ep = high
                    af = min(prev_af + SAR_ACCELERATION, SAR_ACCELERATION_MAX)
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
                af = SAR_ACCELERATION
            else:
                trend = "down"
                if low < prev_ep:
                    ep = low
                    af = min(prev_af + SAR_ACCELERATION, SAR_ACCELERATION_MAX)
                else:
                    ep = prev_ep
                    af = prev_af
        
        candle_copy["sar"] = sar
        candle_copy["sar_trend"] = trend
        result.append(candle_copy)
    
    return result


def find_max_move_after_signal(klines: List[Dict], signal_index: int, direction: str, max_candles: int = 20) -> float:
    """
    Найти максимальное движение цены в нужном направлении после сигнала
    """
    entry_price = klines[signal_index]["close"]
    max_move = 0.0
    
    for j in range(signal_index + 1, min(signal_index + max_candles + 1, len(klines))):
        candle = klines[j]
        
        if direction == "LONG":
            move = (candle["high"] - entry_price) / entry_price * 100
        else:
            move = (entry_price - candle["low"]) / entry_price * 100
        
        if move > max_move:
            max_move = move
    
    return max_move


def find_optimal_tp(klines_with_sar: List[Dict], max_candles: int = 20) -> Dict:
    """
    Найти оптимальный TP для монеты
    """
    max_moves = []
    
    i = 1
    while i < len(klines_with_sar) - max_candles:
        prev = klines_with_sar[i - 1]
        current = klines_with_sar[i]
        
        signal = None
        
        if prev["sar_trend"] == "down" and current["sar_trend"] == "up":
            signal = "LONG"
        elif prev["sar_trend"] == "up" and current["sar_trend"] == "down":
            signal = "SHORT"
        
        if signal:
            max_move = find_max_move_after_signal(klines_with_sar, i, signal, max_candles)
            max_moves.append(max_move)
            i += 3
        else:
            i += 1
    
    if not max_moves:
        return {"tp_100": None, "tp_90": None, "tp_80": None, "max_moves": []}
    
    sorted_moves = sorted(max_moves)
    
    # TP со 100% win rate = минимальное движение
    tp_100 = sorted_moves[0] if sorted_moves else None
    
    # TP с 90% win rate
    idx_90 = max(0, int(len(sorted_moves) * 0.10))
    tp_90 = sorted_moves[idx_90] if sorted_moves else None
    
    # TP с 80% win rate
    idx_80 = max(0, int(len(sorted_moves) * 0.20))
    tp_80 = sorted_moves[idx_80] if sorted_moves else None
    
    return {
        "tp_100": tp_100,
        "tp_90": tp_90,
        "tp_80": tp_80,
        "avg_move": sum(max_moves) / len(max_moves) if max_moves else 0,
        "total_signals": len(max_moves)
    }


def analyze_coin(client: BybitClient, symbol: str, timeframe: str, candles: int) -> Dict:
    """Анализировать одну монету"""
    klines = client.get_klines(symbol, timeframe, candles)
    
    if len(klines) < 50:
        return None
    
    klines_with_sar = calculate_parabolic_sar(klines)
    optimal = find_optimal_tp(klines_with_sar)
    
    if optimal["total_signals"] < 5:
        return None
    
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "total_signals": optimal["total_signals"],
        "tp_100": optimal["tp_100"],
        "tp_90": optimal["tp_90"],
        "tp_80": optimal["tp_80"],
        "avg_move": optimal["avg_move"],
    }


def main():
    print()
    print("═" * 70)
    print("🔍 ПРОДВИНУТЫЙ АНАЛИЗАТОР МОНЕТ v2.0")
    print("═" * 70)
    print()
    print("Анализирую монеты на разных таймфреймах...")
    print("Ищу оптимальный TP для каждой монеты...")
    print()
    
    client = BybitClient()
    
    # Таймфреймы
    timeframes = [
        ("5", 500, "5 минут"),
        ("15", 400, "15 минут"),
        ("60", 300, "1 час"),
        ("D", 100, "1 день"),
    ]
    
    print("📡 Получаю список монет...")
    instruments = client.get_instruments()
    
    symbols = [i["symbol"] for i in instruments 
               if i["symbol"].endswith("USDT") 
               and i.get("status") == "Trading"]
    
    blacklist = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "USDCUSDT"]
    symbols = [s for s in symbols if s not in blacklist]
    
    print(f"✅ Найдено {len(symbols)} монет")
    
    all_results = {}
    
    for tf, candles, tf_name in timeframes:
        print(f"\n{'═' * 70}")
        print(f"📊 ТАЙМФРЕЙМ: {tf_name}")
        print(f"{'═' * 70}")
        
        results = []
        analyzed = 0
        
        for symbol in symbols[:100]:
            try:
                result = analyze_coin(client, symbol, tf, candles)
                if result:
                    results.append(result)
                
                analyzed += 1
                if analyzed % 25 == 0:
                    print(f"   Проанализировано: {analyzed}...")
                
                time.sleep(0.1)
                
            except:
                pass
        
        if not results:
            print("   ❌ Не найдено подходящих монет")
            continue
        
        # Сортируем по tp_100
        results.sort(key=lambda x: (x["tp_100"] or 0), reverse=True)
        all_results[tf_name] = results
        
        print()
        print(f"🏆 ТОП 10 МОНЕТ ({tf_name}):")
        print("-" * 70)
        print(f"{'Монета':<12} {'Сигналов':<10} {'TP 100%':<10} {'TP 90%':<10} {'Avg Move':<10}")
        print("-" * 70)
        
        for r in results[:10]:
            symbol = r["symbol"]
            signals = r["total_signals"]
            tp100 = f"{r['tp_100']:.3f}%" if r['tp_100'] else "N/A"
            tp90 = f"{r['tp_90']:.3f}%" if r['tp_90'] else "N/A"
            avg = f"{r['avg_move']:.2f}%"
            
            marker = "⭐" if r['tp_100'] and r['tp_100'] >= 0.15 else "  "
            
            print(f"{symbol:<12} {signals:<10} {tp100:<10} {tp90:<10} {avg:<10} {marker}")
        
        # Рекомендации
        good_coins = [r for r in results if r['tp_100'] and r['tp_100'] >= 0.15]
        
        if good_coins:
            print()
            print(f"⭐ РЕКОМЕНДУЕМЫЕ для {tf_name} (TP 100% >= 0.15%):")
            print()
            print(f"TIMEFRAME = \"{tf}\"")
            print(f"TP_PERCENT = {min(r['tp_100'] for r in good_coins[:5]):.2f}")
            print()
            print("WHITELIST = [")
            for r in good_coins[:10]:
                print(f'    "{r["symbol"]}",  # TP100: {r["tp_100"]:.3f}%')
            print("]")
    
    print()
    print("═" * 70)
    print("✅ АНАЛИЗ ЗАВЕРШЁН!")
    print("═" * 70)
    print()
    print("📋 ИНСТРУКЦИЯ:")
    print("1. Выбери таймфрейм с лучшими результатами")
    print("2. Скопируй WHITELIST и настройки в config.py")
    print("3. Запусти: python3 bot.py")
    print()


if __name__ == "__main__":
    main()

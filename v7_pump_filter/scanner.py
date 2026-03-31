"""
Сканер монет v7
================
Улучшения:
  1. Минимальный оборот $10M — убираем мусор
  2. ST quality score — оцениваем насколько монета совместима с ST
  3. Исключаем монеты где ST постоянно флипает (шум)
"""

import time
from typing import List, Dict
import config
from strategy import calculate_supertrend


class CoinScanner:

    def __init__(self, client):
        self.client        = client
        self.active_symbols: List[str] = []
        self.last_refresh  = 0

    def get_symbols(self) -> List[str]:
        now = time.time()
        age = (now - self.last_refresh) / 60
        if age >= config.SYMBOL_REFRESH_MIN or not self.active_symbols:
            print(f"\n🔍 Обновляю список монет (прошло {age:.1f} мин)...")
            self.active_symbols = self._scan()
            self.last_refresh   = now
        return self.active_symbols

    def force_refresh(self):
        self.last_refresh = 0

    def _scan(self) -> List[str]:
        tickers = self._get_all_tickers()
        if not tickers:
            print("⚠️  Не удалось получить тикеры")
            return self.active_symbols

        candidates = []

        for t in tickers:
            symbol = t.get("symbol", "")

            if not symbol.endswith("USDT"):
                continue
            if symbol in config.BLACKLIST:
                continue

            try:
                price    = float(t.get("lastPrice", 0))
                turnover = float(t.get("turnover24h", 0))
                high_24h = float(t.get("highPrice24h", 0))
                low_24h  = float(t.get("lowPrice24h", 0))

                if price <= 0 or turnover <= 0:
                    continue

                # Фильтр оборота — убираем мусор
                if turnover < config.MIN_TURNOVER_24H:
                    continue

                vol_24h = (high_24h - low_24h) / low_24h * 100 if low_24h > 0 else 0

                if vol_24h < config.MIN_VOLATILITY_24H:
                    continue
                if vol_24h > config.MAX_VOLATILITY_24H:
                    continue

                candidates.append({
                    "symbol"  : symbol,
                    "turnover": turnover,
                    "vol_24h" : vol_24h,
                    "price"   : price,
                })

            except Exception:
                continue

        if not candidates:
            print("⚠️  Нет кандидатов после базовых фильтров")
            return self.active_symbols

        # Предварительная сортировка — берём топ-30 для детального анализа
        max_t = max(c["turnover"] for c in candidates) or 1
        max_v = max(c["vol_24h"]  for c in candidates) or 1
        for c in candidates:
            c["base_score"] = (c["turnover"] / max_t) * 0.4 + (c["vol_24h"] / max_v) * 0.6

        candidates.sort(key=lambda x: x["base_score"], reverse=True)
        top_candidates = candidates[:30]

        # Детальный анализ ST-качества
        scored = self._score_by_st_quality(top_candidates)

        # Финальная сортировка по итоговому score
        scored.sort(key=lambda x: x["final_score"], reverse=True)

        # Берём топ MAX_SYMBOLS * 2 чтобы был запас
        result = [c["symbol"] for c in scored[:config.MAX_SYMBOLS * 2]]

        if result:
            print(f"✅ Выбрано {len(result)} монет:")
            for c in scored[:config.MAX_SYMBOLS * 2]:
                quality = c.get("st_quality", "?")
                print(
                    f"   {c['symbol']:<20} "
                    f"об=${c['turnover']/1e6:.0f}M  "
                    f"vol={c['vol_24h']:.1f}%  "
                    f"ST-качество={quality:.2f}  "
                    f"score={c['final_score']:.3f}"
                )
        else:
            print("⚠️  После ST-фильтрации нет монет")

        return result

    def _score_by_st_quality(self, candidates: List[Dict]) -> List[Dict]:
        """
        Оцениваем качество монеты для ST стратегии:
        - Считаем ST на 5m за последние 60 свечей
        - Смотрим среднюю длину тренда (чем длиннее — тем лучше)
        - Смотрим % свечей где ST и 5m совпадают
        Монеты где ST часто флипает (< 3 свечи тренд) — исключаем
        """
        scored = []

        for c in candidates:
            symbol = c["symbol"]
            try:
                # Получаем 5m свечи
                klines_5m = self.client.get_klines(symbol, "5", 60)
                if len(klines_5m) < 20:
                    c["st_quality"]   = 0.5
                    c["final_score"]  = c["base_score"] * 0.5
                    scored.append(c)
                    time.sleep(0.05)
                    continue

                st_5m = calculate_supertrend(
                    klines_5m,
                    length=config.ST_LENGTH,
                    factor=config.ST_FACTOR
                )

                # Считаем длины трендов
                trend_lengths = []
                current_trend = st_5m[0]["st_direction"]
                current_len   = 1

                for candle in st_5m[1:]:
                    if candle["st_direction"] == current_trend:
                        current_len += 1
                    else:
                        trend_lengths.append(current_len)
                        current_trend = candle["st_direction"]
                        current_len   = 1
                trend_lengths.append(current_len)

                if not trend_lengths:
                    c["st_quality"]  = 0.3
                    c["final_score"] = c["base_score"] * 0.3
                    scored.append(c)
                    continue

                avg_trend_len = sum(trend_lengths) / len(trend_lengths)
                min_trend_len = min(trend_lengths)

                # Количество флипов — чем меньше тем лучше
                flip_count = len(trend_lengths) - 1
                flip_rate  = flip_count / len(klines_5m)  # флипов на свечу

                # Качество ST:
                # - Средняя длина тренда > 5 свечей = хорошо
                # - Флипов меньше 0.15 на свечу = хорошо
                # - Минимальный тренд > 2 свечи = фильтруем шум

                if min_trend_len < 2:
                    # Монета флипает каждую свечу — мусор
                    st_quality = 0.1
                elif avg_trend_len < 3:
                    st_quality = 0.3
                elif flip_rate > 0.2:
                    st_quality = 0.4
                elif avg_trend_len >= 5 and flip_rate < 0.15:
                    st_quality = 1.0
                elif avg_trend_len >= 4:
                    st_quality = 0.8
                else:
                    st_quality = 0.6

                # Проверяем 5m ST направление (для информации)
                direction_5m = st_5m[-1]["st_direction"]
                c["st_5m_direction"] = direction_5m
                c["avg_trend_len"]   = avg_trend_len
                c["flip_rate"]       = flip_rate
                c["st_quality"]      = st_quality
                c["final_score"]     = c["base_score"] * st_quality

                # Исключаем совсем мусорные
                if st_quality >= 0.3:
                    scored.append(c)

                time.sleep(0.1)

            except Exception as e:
                c["st_quality"]  = 0.4
                c["final_score"] = c["base_score"] * 0.4
                scored.append(c)
                continue

        return scored

    def _get_all_tickers(self) -> List[Dict]:
        try:
            r = self.client._request("GET", "/v5/market/tickers", {
                "category": "linear"
            })
            if r.get("retCode") == 0:
                return r.get("result", {}).get("list", [])
        except Exception as e:
            print(f"❌ Ошибка тикеров: {e}")
        return []

"""
Сканер монет — автовыбор через Bybit API
==========================================
Критерии:
  1. Оборот 24ч > MIN_TURNOVER_24H
  2. Волатильность 1ч в диапазоне [MIN, MAX]
  3. Supertrend 5m совпадает с 1m (фильтр тренда)
  4. Не в BLACKLIST
"""

import time
from typing import List, Dict, Optional
import config
from strategy import calculate_supertrend


class CoinScanner:

    def __init__(self, client):
        self.client        = client
        self.active_symbols: List[str] = []
        self.last_refresh  = 0

    # ──────────────────────────────────────
    # ПУБЛИЧНЫЙ МЕТОД
    # ──────────────────────────────────────

    def get_symbols(self) -> List[str]:
        """
        Вернуть актуальный список монет.
        Обновляет каждые SYMBOL_REFRESH_MIN минут.
        """
        now = time.time()
        age = (now - self.last_refresh) / 60

        if age >= config.SYMBOL_REFRESH_MIN or not self.active_symbols:
            print(f"\n🔍 Обновляю список монет (прошло {age:.1f} мин)...")
            self.active_symbols = self._scan()
            self.last_refresh   = now

        return self.active_symbols

    def force_refresh(self):
        self.last_refresh = 0

    # ──────────────────────────────────────
    # СКАНИРОВАНИЕ
    # ──────────────────────────────────────

    def _scan(self) -> List[str]:
        tickers = self._get_all_tickers()
        if not tickers:
            print("⚠️  Не удалось получить тикеры, используем предыдущий список")
            return self.active_symbols

        candidates = []

        for t in tickers:
            symbol = t.get("symbol", "")

            # Только USDT фьючерсы
            if not symbol.endswith("USDT"):
                continue

            # Блэклист
            if symbol in config.BLACKLIST:
                continue

            try:
                price     = float(t.get("lastPrice", 0))
                turnover  = float(t.get("turnover24h", 0))
                high_24h  = float(t.get("highPrice24h", 0))
                low_24h   = float(t.get("lowPrice24h", 0))

                if price <= 0 or turnover <= 0:
                    continue

                # Фильтр оборота
                if turnover < config.MIN_TURNOVER_24H:
                    continue

                # Волатильность за 24ч как прокси
                # (1ч свечи запрашивать для всех монет — слишком много запросов)
                vol_24h = (high_24h - low_24h) / low_24h * 100 if low_24h > 0 else 0

                # Адаптируем порог: волатильность 24ч должна быть > MIN*4
                # (предполагаем что 1ч ≈ 24ч/6)
                min_vol = getattr(config, 'MIN_VOLATILITY_24H',
                          getattr(config, 'MIN_VOLATILITY_1H', 0.3) * 4)
                max_vol = getattr(config, 'MAX_VOLATILITY_24H',
                          getattr(config, 'MAX_VOLATILITY_1H', 15) * 8)
                min_vol_24h = min_vol
                max_vol_24h = max_vol

                if vol_24h < min_vol_24h:
                    continue
                if vol_24h > max_vol_24h:
                    continue

                candidates.append({
                    "symbol"   : symbol,
                    "turnover" : turnover,
                    "vol_24h"  : vol_24h,
                    "price"    : price,
                })

            except Exception:
                continue

        if not candidates:
            print("⚠️  Нет кандидатов после базовых фильтров")
            return self.active_symbols

        # Сортируем: хороший баланс оборота и волатильности
        # Score = нормализованный оборот + нормализованная волатильность
        max_t = max(c["turnover"] for c in candidates) or 1
        max_v = max(c["vol_24h"]  for c in candidates) or 1

        for c in candidates:
            c["score"] = (c["turnover"] / max_t) * 0.4 + (c["vol_24h"] / max_v) * 0.6

        candidates.sort(key=lambda x: x["score"], reverse=True)

        # Берём топ-20 кандидатов и проверяем ST на 5m
        top_candidates = candidates[:20]
        confirmed = self._filter_by_supertrend(top_candidates)

        # Финальный список
        result = [c["symbol"] for c in confirmed[:config.MAX_SYMBOLS * 3]]

        if result:
            print(f"✅ Выбрано {len(result)} монет:")
            for c in confirmed[:config.MAX_SYMBOLS * 3]:
                print(f"   {c['symbol']:<20} оборот=${c['turnover']/1e6:.1f}M  "
                      f"vol24h={c['vol_24h']:.1f}%  score={c['score']:.3f}")
        else:
            print("⚠️  ST-фильтр не прошла ни одна монета, берём топ по score")
            result = [c["symbol"] for c in candidates[:config.MAX_SYMBOLS * 3]]

        return result

    def _filter_by_supertrend(self, candidates: List[Dict]) -> List[Dict]:
        """
        Проверяем ST на 5m — берём только монеты где
        ST 5m имеет чёткое направление (не флипал последние 3 свечи)
        """
        confirmed = []

        for c in candidates:
            symbol = c["symbol"]
            try:
                klines_5m = self.client.get_klines(symbol, "5", 30)
                if len(klines_5m) < 10:
                    continue

                st_5m = calculate_supertrend(
                    klines_5m,
                    length=config.ST_LENGTH,
                    factor=config.ST_FACTOR
                )

                # Проверяем последние 3 свечи на 5m — одно направление
                last_3 = st_5m[-3:]
                directions = [s["st_direction"] for s in last_3]

                if len(set(directions)) == 1:  # все одинаковые
                    c["st_5m_direction"] = directions[-1]
                    confirmed.append(c)
                    time.sleep(0.05)  # не спамим API

            except Exception:
                continue

        return confirmed

    def _get_all_tickers(self) -> List[Dict]:
        """Получить все тикеры с Bybit"""
        try:
            r = self.client._request("GET", "/v5/market/tickers", {
                "category": "linear"
            })
            if r.get("retCode") == 0:
                return r.get("result", {}).get("list", [])
        except Exception as e:
            print(f"❌ Ошибка получения тикеров: {e}")
        return []

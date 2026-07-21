"""
Глобальный in-memory кэш загруженных данных ДТП.

Кэширует результат запросов к API ГИБДД по ключу (reg_code, dat_tuple).
Разделяется между всеми пользователями — если один пользователь
загрузил данные за регион/период, другие получат их мгновенно.

Используется:
  - _fetch_cards_for_period() в bot.py для основного и прошлого года
  - preload-загрузкой в фоне после выгрузки текущего периода

Типы записей:
  - "current": данные за запрошенный период (set при выгрузке)
  - "prev":    данные за прошлый год (set при анализе/очагах или preload)
"""

import logging
import threading
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

# ========================
# Настройки кэша
# ========================
_MAX_ENTRIES = 50        # максимум записей в кэше
_TTL_SECONDS = 3600      # время жизни записи (1 час)


class _DataCache:
    """Потокобезопасный LRU-кэш с TTL."""

    def __init__(self, max_entries: int = _MAX_ENTRIES, ttl: int = _TTL_SECONDS):
        self._cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._max_entries = max_entries
        self._ttl = ttl
        # Счётчики попаданий/промахов для диагностики
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _make_key(reg_code: str, dat_list: list[str]) -> str:
        """Формирует ключ кэша из кода региона и списка дат."""
        return f"{reg_code}:{','.join(dat_list)}"

    def get(self, reg_code: str, dat_list: list[str]) -> tuple[list[dict], list[str]] | None:
        """
        Возвращает (cards, errors) из кэша или None, если запись
        отсутствует или просрочена.
        """
        key = self._make_key(reg_code, dat_list)
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self.misses += 1
                return None

            if time.monotonic() - entry["ts"] > self._ttl:
                # Просрочена — удаляем
                del self._cache[key]
                self.misses += 1
                logger.debug(f"Кэш: запись {key} просрочена, удалена")
                return None

            # Перемещаем в конец (LRU)
            self._cache.move_to_end(key)
            self.hits += 1
            logger.debug(
                f"Кэш: HIT {key} "
                f"({len(entry['cards'])} ДТП, возраст {time.monotonic() - entry['ts']:.0f}с)"
            )
            return entry["cards"], entry["errors"]

    def put(self, reg_code: str, dat_list: list[str],
            cards: list[dict], errors: list[str]) -> None:
        """Сохраняет результат в кэш."""
        key = self._make_key(reg_code, dat_list)
        with self._lock:
            # Если ключ уже есть — обновляем
            if key in self._cache:
                self._cache.move_to_end(key)

            self._cache[key] = {
                "cards": cards,
                "errors": errors,
                "ts": time.monotonic(),
            }

            # Evict старых записей (LRU)
            while len(self._cache) > self._max_entries:
                evicted_key, _ = self._cache.popitem(last=False)
                logger.debug(f"Кэш: evict {evicted_key} (лимит {_max_entries})")

            logger.debug(
                f"Кэш: PUT {key} ({len(cards)} ДТП), "
                f"размер кэша: {len(self._cache)}/{self._max_entries}"
            )

    def has(self, reg_code: str, dat_list: list[str]) -> bool:
        """Быстрая проверка наличия валидной записи (без извлечения)."""
        return self.get(reg_code, dat_list) is not None

    def invalidate(self, reg_code: str, dat_list: list[str]) -> None:
        """Удаляет конкретную запись из кэша."""
        key = self._make_key(reg_code, dat_list)
        with self._lock:
            self._cache.pop(key, None)

    def clear(self) -> None:
        """Очищает весь кэш."""
        with self._lock:
            self._cache.clear()
            logger.info("Кэш: полностью очищен")

    def stats_dict(self) -> dict[str, int]:
        """Статистика кэша для программного доступа."""
        with self._lock:
            now = time.monotonic()
            valid = sum(
                1 for e in self._cache.values()
                if now - e["ts"] <= self._ttl
            )
            total_cards = sum(len(e["cards"]) for e in self._cache.values())
            return {
                "entries": len(self._cache),
                "valid": valid,
                "total_cards_cached": total_cards,
            }

    def stats(self) -> str:
        """Статистика кэша в виде строки для логирования."""
        s = self.stats_dict()
        return (
            f"cache: {s['entries']}/{self._max_entries} записей, "
            f"hits={self.hits}, misses={self.misses}"
        )


# Глобальный экземпляр
data_cache = _DataCache()
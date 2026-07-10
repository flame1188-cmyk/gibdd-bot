"""
Файловый кэш справочника регионов.

При успешной загрузке справочника с API — сохраняем в файл.
При недоступности API (5xx, таймаут) — читаем из файла.

Файл: data/regions_cache.json
Формат: [{"code": "1101", "name": "Республика Адыгея"}, ...]
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

DATA_DIR = os.environ.get("CAMERA_DATA_DIR", "data")
CACHE_FILE = os.path.join(DATA_DIR, "regions_cache.json")


def _ensure_dir() -> None:
    """Создаёт директорию для кэша если нужно."""
    os.makedirs(DATA_DIR, exist_ok=True)


def save_regions_to_cache(regions: list[dict[str, str]]) -> None:
    """
    Сохраняет список регионов в файловый кэш.

    Args:
        regions: Список словарей [{"code": "1101", "name": "..."}, ...]
    """
    try:
        _ensure_dir()
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(regions, f, ensure_ascii=False, indent=2)
        logger.info(
            f"Справочник регионов сохранён в кэш: "
            f"{len(regions)} записей, путь={os.path.abspath(CACHE_FILE)}"
        )
    except Exception as e:
        logger.error(f"Не удалось сохранить справочник регионов в кэш: {e}")


def load_regions_from_cache() -> list[dict[str, str]]:
    """
    Загружает список регионов из файлового кэша.

    Returns:
        Список словарей [{"code": "1101", "name": "..."}, ...]
        или пустой список, если кэш отсутствует или повреждён.
    """
    if not os.path.isfile(CACHE_FILE):
        logger.debug(f"Кэш регионов не найден: {CACHE_FILE}")
        return []

    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            regions = json.load(f)

        if not isinstance(regions, list) or len(regions) == 0:
            logger.warning("Кэш регионов пуст или повреждён")
            return []

        # Базовая валидация структуры
        valid = []
        for r in regions:
            if isinstance(r, dict) and "code" in r and "name" in r:
                valid.append(r)
            else:
                logger.warning(f"Кэш регионов: пропущена невалидная запись: {r}")

        logger.info(
            f"Справочник регионов загружен из кэша: "
            f"{len(valid)} записей"
        )
        return valid

    except Exception as e:
        logger.error(f"Ошибка чтения кэша регионов: {e}")
        return []
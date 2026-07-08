"""
Файловый кэш камер фотовидеофиксации по регионам.

Каждый регион хранится отдельным файлом:
    data/cameras_{reg_code}.xls

При загрузке через Telegram — файл сохраняется на диск.
Перед расчётом очагов — бот сначала ищет файл в кэше.

Путь к папке данных можно переопределить через переменную окружения
CAMERA_DATA_DIR (по умолчанию — "data" относительно рабочего каталога).
"""

import logging
import os
from typing import Optional

from camera_loader import parse_camera_file

logger = logging.getLogger(__name__)

# Папка с файлами камер (постоянный том на Amvera)
DATA_DIR = os.environ.get("CAMERA_DATA_DIR", "data")


def _ensure_data_dir() -> str:
    """Создаёт папку данных если нет, возвращает путь."""
    os.makedirs(DATA_DIR, exist_ok=True)
    return DATA_DIR


def _camera_filepath(reg_code: str) -> str:
    """Путь к файлу камер для региона."""
    return os.path.join(DATA_DIR, f"cameras_{reg_code}.xls")


def save_camera_file(reg_code: str, file_bytes: bytes) -> str:
    """Сохраняет загруженный файл камер на диск.

    Args:
        reg_code: Код региона (например "1122", "1119").
        file_bytes: Сырые байты файла.

    Returns:
        Путь к сохранённому файлу.
    """
    _ensure_data_dir()
    path = _camera_filepath(reg_code)
    with open(path, "wb") as f:
        f.write(file_bytes)
    logger.info(
        f"Камеры региона {reg_code} сохранены: {path} "
        f"({len(file_bytes)} байт)"
    )
    return path


def load_cameras_from_cache(reg_code: str) -> Optional[list[dict]]:
    """Загружает камеры из кэша на диске.

    Returns:
        Список камер или None если файла нет / ошибка парсинга.
    """
    path = _camera_filepath(reg_code)
    if not os.path.isfile(path):
        logger.debug(f"Файл камер региона {reg_code} не найден: {path}")
        return None

    try:
        with open(path, "rb") as f:
            file_bytes = f.read()

        cameras = parse_camera_file(file_bytes)
        if cameras:
            logger.info(
                f"Загружены камеры региона {reg_code} из кэша: "
                f"{len(cameras)} камер"
            )
            return cameras
        else:
            logger.warning(
                f"Файл камер региона {reg_code} есть, "
                f"но парсер вернул пустой список: {path}"
            )
            return None

    except Exception as e:
        logger.error(
            f"Ошибка загрузки камер региона {reg_code} из кэша: {e}"
        )
        return None


def has_cached_cameras(reg_code: str) -> bool:
    """Быстренько проверяет наличие файла (без парсинга)."""
    return os.path.isfile(_camera_filepath(reg_code))


def delete_cached_cameras(reg_code: str) -> bool:
    """Удаляет кэш камер региона. Возвращает True если файл был удалён."""
    path = _camera_filepath(reg_code)
    if os.path.isfile(path):
        os.remove(path)
        logger.info(f"Удалён кэш камер региона {reg_code}: {path}")
        return True
    return False


def list_cached_regions() -> list[str]:
    """Возвращает список кодов регионов, для которых есть файлы камер."""
    if not os.path.isdir(DATA_DIR):
        return []
    result = []
    for fname in os.listdir(DATA_DIR):
        if fname.startswith("cameras_") and fname.endswith(".xls"):
            # cameras_1122.xls → 1122
            code = fname[len("cameras_"):-len(".xls")]
            result.append(code)
    return sorted(result)
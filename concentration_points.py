"""
Модуль расчёта очагов концентрации ДТП (мест концентрации аварийности).

Два алгоритма:
  1. Населённые пункты (НП) — 3 прохода:
     - 1-й проход: перекрёстки (sdor содержит «перекрёсток»):
       Шаг 1a: ДТП на дороге с пикетажем — сначала проверка
       по пикетажу (±50 м по той же дороге, только «перекрёстки»);
       если очаг не сформирован — радиус 50 м по GPS (только «перекрёстки»)
       с проверкой пикетажа (ДТП на той же дороге с piketаж > 50 м
       исключаются)
       Шаг 1b: ДТП без пикетажа — стандартный радиус 50 м по GPS
       (только «перекрёстки»)
     - 2-й проход: дороги с наименованием и пикетажем, скользящее окно 200 м
     - 3-й проход: радиус 100 м от точки, с проверкой пикетажа:
       если центр ДТП и другое ДТП в радиусе 100 м имеют одинаковое
       наименование дороги и пикетаж, проверяется окно 200 м по пикетажу
     - Порог: 3+ ДТП одного вида ИЛИ 5+ ДТП любых видов
  2. Вне НП (автодороги):
     - Группировка по названию дороги
     - Скользящее окно 1 км
     - Порог: 3+ ДТП одного вида ИЛИ 5+ ДТП любых видов

Определение НП/не НП через OSM Overpass API с реальными полигонами (Shapely).

Оптимизации нагрузки на OSM:
  - In-memory LRU-кэш (5 записей) для распарсенных полигонов
  - Адаптивный bbox с минимальным запасом (0.02° вместо 0.1°)
  - Разбиение больших bbox (>1.5°) на тайлы с перехлёстом
  - Параллельные запросы к зеркалам Overpass API
  - Дисковый кэш (TTL 24 ч) для элементов Overpass
  - Bbox-результаты никогда не кэшируются
"""

import math
import json
import os
import time
import hashlib
import logging
from collections import Counter, OrderedDict
from typing import Any, Callable, Awaitable
import asyncio

import httpx
from shapely.geometry import Polygon, MultiPolygon, Point, LineString
from shapely.ops import linemerge, polygonize, unary_union
from shapely.prepared import prep

from analytics import _safe_int

logger = logging.getLogger(__name__)


# ========================
# Константы
# ========================

EARTH_RADIUS_KM = 6371.0

# Радиусы для НП (метры)
SETTLEMENT_INTERSECTION_RADIUS_M = 50
SETTLEMENT_OTHER_RADIUS_M = 100

# Окно для дорог с пикетажем в НП (км)
SETTLEMENT_ROAD_WINDOW_KM = 0.2  # 200 метров

# Окно для вне НП с пикетажем (км)
NON_SETTLEMENT_WINDOW_KM = 1.0
# Окно для вне НП без пикетажа (км)
NON_SETTLEMENT_NO_PK_WINDOW_KM = 0.2  # 200 метров

# Пороги
SAME_TYPE_THRESHOLD = 3   # 3+ ДТП одного вида = очаг
ANY_TYPE_THRESHOLD = 5    # 5+ ДТП любых видов = очаг

# Ключевые слова для определения перекрёстка
INTERSECTION_KEYWORDS = [
    # перекрёсток — разные падежи/окончания
    "перекрёсток", "перекресток",
    "перекрёстка", "перекрестка",
    "перекрёстку", "перекрестку",
    "перекрёстке", "перекрестке",
    "перекрёстков", "перекрестков",
    # круговое движение — разные формы
    "круговое движение",
    "круговым движением",
]

# ДТП исключаются из расчёта очагов (произошли не на дороге).
# 1) Всегда: если sdor содержит эти значения — исключается
EXCLUDED_SDOR_ALWAYS = [
    "внутридворовая территория",
    "отделенная от проезжей части",
]
# 2) Только при k_ul="Иные места": если sdor содержит эти значения — исключается
EXCLUDED_K_UL = "иные места"
EXCLUDED_SDOR_FOR_KUL = [
    "выезд с прилегающей территории",
    "тротуар, пешеходная дорожка",
    "иное место",
]

# Кэширование границ НП
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 часа

# In-memory LRU-кэш распарсенных полигонов (избегает повторного парсинга JSON)
MEMORY_CACHE_MAX = 5  # максимум записей
_memory_cache: OrderedDict[str, tuple[float, list]] = OrderedDict()  # bbox → (timestamp, polygons)

# Параметры bbox
BBOX_MARGIN = 0.02  # ~2.2 км — минимальный запас вокруг ДТП
BBOX_TILE_MAX_DEG = 1.5  # макс. размер стороны тайла (при превышении — разбиение)
BBOX_TILE_OVERLAP = 0.02  # перехлёст тайлов, чтобы НП на границе не потерялись
BBOX_MIN_CLAMP = 0.01  # минимальный размер bbox (если ДТП в одной точке)

# ========================
# Параметры исторической динамики
# ========================

# Радиус сопоставления очагов между периодами (км)
MATCH_RADIUS_SETTLEMENT = 0.5       # 500 м — для НП
MATCH_RADIUS_NONSETTLEMENT = 2.0    # 2 км — для вне НП (участки длиннее)

DYNAMICS_STATUS_LABELS = {
    "new": "Новый",
    "lost": "Исчезнувший",
    "growing": "Рост",
    "shrinking": "Снижение",
    "stable": "Стабильный",
}


# ========================
# Вспомогательные функции
# ========================

def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние в метрах между двумя точками по формуле Гаверсинуса."""
    lat1_r, lon1_r = math.radians(lat1), math.radians(lon1)
    lat2_r, lon2_r = math.radians(lat2), math.radians(lon2)
    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.asin(math.sqrt(min(a, 1.0)))
    return EARTH_RADIUS_KM * c * 1000.0


def _parse_coords(card: dict) -> tuple[float, float] | None:
    """Извлечь координаты из карточки. Возвращает (lat, lon) или None."""
    try:
        lat = float(str(card.get("coord_w", "")).strip())
        lon = float(str(card.get("coord_l", "")).strip())
        if lat != 0 and lon != 0:
            return (lat, lon)
    except (ValueError, TypeError):
        pass
    return None


def _is_intersection(card: dict) -> bool:
    """Является ли место ДТП перекрёстком (по полю sdor).

    Поле sdor содержит объект УДС на месте ДТП: перекрёсток,
    перегон, пешеходный переход и т.д.
    Данные лежат внутри card["dor_usl"]["sdor"] — это массив строк.
    """
    dor_usl = card.get("dor_usl") or {}
    sdor_list = dor_usl.get("sdor") or []
    if isinstance(sdor_list, list):
        for item in sdor_list:
            item_lower = str(item).strip().lower()
            for keyword in INTERSECTION_KEYWORDS:
                if keyword in item_lower:
                    return True
    return False


def _is_off_road(card: dict) -> bool:
    """Произошло ли ДТП вне дороги (внутридворовая территория, автостоянка).

    Такие ДТП не могут входить в очаги аварийности.
    Двойная проверка:
    1. sdor содержит «внутридворовая территория» или «отделенная от проезжей части»
       → всегда исключается
    2. k_ul == «Иные места» И sdor содержит «Выезд с прилегающей территории»,
       «Тротуар, пешеходная дорожка» или «Иное место» → исключается
    """
    dor_usl = card.get("dor_usl") or {}
    sdor_list = dor_usl.get("sdor") or []
    sdor_lower = []
    if isinstance(sdor_list, list):
        sdor_lower = [str(item).strip().lower() for item in sdor_list]

    # 1) Всегда исключаем по sdor
    for item_lower in sdor_lower:
        for keyword in EXCLUDED_SDOR_ALWAYS:
            if keyword in item_lower:
                return True

    # 2) Исключаем по k_ul + sdor
    k_ul = str(card.get("k_ul", "")).strip().lower()
    if k_ul == EXCLUDED_K_UL:
        for item_lower in sdor_lower:
            for keyword in EXCLUDED_SDOR_FOR_KUL:
                if keyword in item_lower:
                    return True

    return False


def _get_dtp_type(card: dict) -> str:
    """Вид ДТП."""
    return str(card.get("dtpv", "")).strip()


def _get_road_name(card: dict) -> str:
    """Название дороги/улицы."""
    dor = str(card.get("dor", "")).strip()
    if dor:
        return dor
    return str(card.get("street", "")).strip()


def _get_date(card: dict) -> str:
    """Дата ДТП."""
    return str(card.get("date_dtp", "")).strip()


def _get_km_m(card: dict) -> float | None:
    """
    Пикетаж как float (км.ddd). km=12, m=500 -> 12.500

    Возвращает None если:
      - поле km пустое
      - оба значения равны 0 (0+000 = «не указан»)
    """
    km_str = str(card.get("km", "")).strip()
    m_str = str(card.get("m", "")).strip()
    if km_str:
        try:
            km_val = float(km_str)
            m_val = float(m_str) if m_str else 0.0
            total = km_val + m_val / 1000.0
            # 0+000 означает «пикетаж не указан»
            if total == 0.0:
                return None
            return total
        except ValueError:
            pass
    return None


def _has_road_and_piketazh(card: dict) -> bool:
    """Есть ли у карточки наименование дороги И пикетаж."""
    return bool(_get_road_name(card)) and _get_km_m(card) is not None


def _check_cluster_criteria(
    type_counter: Counter,
    total: int,
) -> tuple[bool, str | None]:
    """
    Проверяет, выполняется ли критерий очага.

    Returns:
        (is_cluster, dominant_type)
        dominant_type — вид ДТП, достигший порога 3+, или None при пороге 5+
    """
    for dtp_type, count in type_counter.most_common():
        if count >= SAME_TYPE_THRESHOLD:
            return True, dtp_type
    if total >= ANY_TYPE_THRESHOLD:
        return True, None
    return False, None


# ========================
# Кэширование границ НП
# ========================

# ========================
# In-memory кэш полигонов
# ========================

def _memory_cache_get(bbox_str: str) -> list[Polygon | MultiPolygon] | None:
    """Получить полигоны из in-memory кэша. Returns None если нет или просрочен."""
    if bbox_str in _memory_cache:
        ts, polygons = _memory_cache[bbox_str]
        age = time.time() - ts
        if age < CACHE_TTL_SECONDS:
            # Перемещаем в конец (LRU)
            _memory_cache.move_to_end(bbox_str)
            logger.info(
                f"In-memory кэш границ НП: hit (возраст {age / 3600:.1f} ч, "
                f"{len(polygons)} полигонов)"
            )
            return polygons
        else:
            del _memory_cache[bbox_str]
    return None


def _memory_cache_put(bbox_str: str, polygons: list[Polygon | MultiPolygon]) -> None:
    """Сохранить полигоны в in-memory LRU кэш."""
    while len(_memory_cache) >= MEMORY_CACHE_MAX:
        _memory_cache.popitem(last=False)  # удаляем самый старый
    _memory_cache[bbox_str] = (time.time(), polygons)
    logger.info(
        f"In-memory кэш границ НП: сохранено ({len(polygons)} полигонов, "
        f"LRU размер: {len(_memory_cache)}/{MEMORY_CACHE_MAX})"
    )


# ========================
# Дисковый кэш элементов Overpass
# ========================

def _cache_path(bbox_str: str) -> str:
    """Путь к файлу кэша для данного BBOX."""
    h = hashlib.md5(bbox_str.encode()).hexdigest()[:12]
    return os.path.join(CACHE_DIR, f"settlements_{h}.json")


def _load_cache(bbox_str: str) -> list[dict] | None:
    """
    Загружает кэшированный ответ Overpass API.

    Returns:
        Список elements из Overpass или None, если кэш отсутствует/просрочен.
    """
    path = _cache_path(bbox_str)
    if not os.path.exists(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        age = time.time() - data.get("timestamp", 0)
        if age > CACHE_TTL_SECONDS:
            logger.info(
                f"Кэш границ НП просрочен: {path} "
                f"(возраст: {age / 3600:.1f} ч)"
            )
            return None

        logger.info(
            f"Кэш границ НП загружен: {path} "
            f"(возраст: {age / 3600:.1f} ч, "
            f"{data.get('count', 0)} элементов)"
        )
        return data.get("elements", [])
    except Exception as e:
        logger.warning(f"Ошибка чтения кэша: {e}")
        return None


def _save_cache(bbox_str: str, elements: list[dict]) -> None:
    """Сохраняет ответ Overpass API в кэш."""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = _cache_path(bbox_str)
        data = {
            "timestamp": time.time(),
            "bbox": bbox_str,
            "count": len(elements),
            "elements": elements,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        logger.info(
            f"Кэш границ НП сохранён: {path} "
            f"({len(elements)} элементов)"
        )
    except Exception as e:
        logger.warning(f"Ошибка записи кэша: {e}")


# ========================
# OSM: Разбор полигонов
# ========================

def _way_to_polygon(element: dict) -> Polygon | None:
    """
    Преобразует way-элемент Overpass (out geom) в Shapely Polygon.

    Shapely использует (x, y) = (lon, lat), поэтому координаты
    переставляются при создании полигона.
    """
    geom = element.get("geometry", [])
    if len(geom) < 4:
        return None

    try:
        coords = [(n["lon"], n["lat"]) for n in geom]
        poly = Polygon(coords)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty or poly.area < 1e-10:
            return None
        return poly
    except Exception:
        return None


def _relation_to_polygon(
    element: dict,
) -> Polygon | MultiPolygon | None:
    """
    Преобразует relation-элемент Overpass (out geom) в Shapely Polygon.

    Алгоритм:
    1. Собирает outer-кольца из member-ов (role=outer или без роли)
    2. Объединяет через linemerge → замкнутые кольца
    3. polygonize → список Polygon
    4. Inner-кольца (role=inner) вычитаются как отверстия (holes)
    """
    members = element.get("members", [])
    if not members:
        return None

    outer_rings: list[list[tuple[float, float]]] = []
    inner_rings: list[list[tuple[float, float]]] = []

    for member in members:
        geom = member.get("geometry", [])
        if len(geom) < 2:
            continue

        coords = [(n["lon"], n["lat"]) for n in geom]
        role = member.get("role", "outer")

        if role == "inner":
            inner_rings.append(coords)
        else:
            outer_rings.append(coords)

    if not outer_rings:
        return None

    try:
        outer_lines = [LineString(ring) for ring in outer_rings]
        merged = linemerge(outer_lines)

        polygons: list[Polygon] = []

        if merged.geom_type == "LineString":
            if merged.is_closed:
                polygons.append(Polygon(merged))
        elif merged.geom_type == "MultiLineString":
            polygons.extend(polygonize(merged))
        else:
            return None

        if not polygons:
            return None

        # Обработка отверстий (inner-кольца)
        if inner_rings:
            for i, poly in enumerate(polygons):
                for hole_coords in inner_rings:
                    try:
                        hole_line = LineString(hole_coords)
                        if hole_line.is_closed and poly.contains(hole_line):
                            hole_poly = Polygon(hole_coords)
                            polygons[i] = poly.difference(hole_poly)
                    except Exception:
                        pass

        # Валидация
        valid_polygons: list[Polygon] = []
        for p in polygons:
            if not p.is_valid:
                p = p.buffer(0)
            if not p.is_empty and p.area > 1e-10:
                valid_polygons.append(p)

        if not valid_polygons:
            return None

        if len(valid_polygons) == 1:
            return valid_polygons[0]
        return MultiPolygon(valid_polygons)

    except Exception as e:
        logger.debug(
            f"Не удалось разобрать relation id={element.get('id')}: {e}"
        )
        return None


def _parse_overpass_elements(
    elements: list[dict],
) -> tuple[list[Polygon | MultiPolygon], bool]:
    """
    Преобразует элементы Overpass API в список Shapely-полигонов.

    Поддерживает два формата ответа:
    - «out geom»: поля geometry (ways) / members (relations)
    - «out bb»: поля bounds (прямоугольные оболочки)

    Приоритет: geom > bb. Если geom-данные есть — используются они,
    если нет — падаем обратно на bounding boxes (совместимость).

    Returns:
        (polygons, is_bbox_fallback) — список полигонов и флаг,
        что использовались bounding boxes (а не реальные полигоны).
    """
    polygons: list[Polygon | MultiPolygon] = []

    # Первый проход: проверяем, есть ли geom-данные
    has_geom = False
    for element in elements:
        if element.get("type") == "way" and element.get("geometry"):
            has_geom = True
        elif element.get("type") == "relation" and element.get("members"):
            has_geom = True

    if has_geom:
        for element in elements:
            if element.get("type") == "way":
                poly = _way_to_polygon(element)
                if poly is not None:
                    polygons.append(poly)
            elif element.get("type") == "relation":
                poly = _relation_to_polygon(element)
                if poly is not None:
                    polygons.append(poly)

    if polygons:
        logger.info(
            f"Разобрано {len(polygons)} полигонов НП из OSM (out geom)"
        )
        return polygons, False

    # Fallback: bounding boxes (out bb)
    for element in elements:
        if "bounds" in element:
            b = element["bounds"]
            coords = [
                (b["minlon"], b["minlat"]),
                (b["maxlon"], b["minlat"]),
                (b["maxlon"], b["maxlat"]),
                (b["minlon"], b["maxlat"]),
            ]
            try:
                poly = Polygon(coords)
                if poly.is_valid and poly.area > 0:
                    polygons.append(poly)
            except Exception:
                pass

    logger.info(
        f"Разобрано {len(polygons)} bounding boxes из OSM "
        f"(out bb fallback)"
    )
    return polygons, True


# ========================
# OSM: Определение границ НП
# ========================

# ========================
# Bbox утилиты
# ========================

def _compute_bbox_tiles(
    lat_min: float, lon_min: float,
    lat_max: float, lon_max: float,
) -> list[tuple[float, float, float, float]]:
    """
    Разбивает большой bbox на тайлы, если любая сторона > BBOX_TILE_MAX_DEG.

    Возвращает список (lat_min, lon_min, lat_max, lon_max) тайлов.
    Тайлы имеют перехлёст BBOX_TILE_OVERLAP, чтобы НП на границах не терялись.
    """
    lat_span = lat_max - lat_min
    lon_span = lon_max - lon_min

    if lat_span <= BBOX_TILE_MAX_DEG and lon_span <= BBOX_TILE_MAX_DEG:
        # Достаточно маленький — не разбиваем
        return [(lat_min, lon_min, lat_max, lon_max)]

    tiles: list[tuple[float, float, float, float]] = []
    overlap = BBOX_TILE_OVERLAP

    # Разбиваем по широте
    lat_steps = max(1, math.ceil(lat_span / BBOX_TILE_MAX_DEG))
    # Разбиваем по долготе
    lon_steps = max(1, math.ceil(lon_span / BBOX_TILE_MAX_DEG))

    for li in range(lat_steps):
        for lj in range(lon_steps):
            t_lat_min = lat_min + li * lat_span / lat_steps - overlap
            t_lat_max = lat_min + (li + 1) * lat_span / lat_steps + overlap
            t_lon_min = lon_min + lj * lon_span / lon_steps - overlap
            t_lon_max = lon_min + (lj + 1) * lon_span / lon_steps + overlap

            # Ограничиваем мировыми границами
            t_lat_min = max(t_lat_min, 41.0)
            t_lat_max = min(t_lat_max, 70.0)
            t_lon_min = max(t_lon_min, 19.0)
            t_lon_max = min(t_lon_max, 180.0)

            tiles.append((t_lat_min, t_lon_min, t_lat_max, t_lon_max))

    logger.info(
        f"Bbox разбит на {len(tiles)} тайлов "
        f"({lat_steps}x{lon_steps}, span: {lat_span:.2f}x{lon_span:.2f}°)"
    )
    return tiles


def _dedup_elements(elements: list[dict]) -> list[dict]:
    """
    Удаляет дубликаты элементов Overpass по (type, id).
    Нужно при слиянии результатов из нескольких тайлов.
    """
    seen: set[tuple[str, int]] = set()
    unique = []
    for el in elements:
        key = (el.get("type", ""), el.get("id", 0))
        if key not in seen:
            seen.add(key)
            unique.append(el)
    if len(unique) < len(elements):
        logger.info(
            f"Дедупликация элементов: {len(elements)} → {len(unique)} "
            f"(удалено {len(elements) - len(unique)} дублей)"
        )
    return unique


# ========================
# OSM: Определение границ НП
# ========================

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

OVERPASS_HEADERS = {
    "User-Agent": "GIBDD-DTP-Bot/1.0 (traffic-accident-analysis)",
    "Accept": "application/json",
}

PLACE_FILTER = "city|town|village|hamlet"


async def fetch_settlement_boundaries(
    cards: list[dict],
    progress_callback: Callable[[str], Awaitable[None]] | None = None,
) -> list[Polygon | MultiPolygon]:
    """
    Получает полигоны границ населённых пунктов через Overpass API.

    Оптимизации:
    1. In-memory LRU-кэш (5 записей) — избегает повторного парсинга JSON
    2. Адаптивный margin — bbox строится с минимальным запасом (0.02°)
    3. Разбиение на тайлы — bbox > 1.5° делится на части
    4. Параллельные запросы к зеркалам — asyncio.gather
    5. Дисковый кэш (TTL 24 ч) — для запросов после перезапуска

    Returns:
        Список Shapely-полигонов (Polygon или MultiPolygon).
    """
    valid_coords = [_parse_coords(c) for c in cards]
    valid_coords = [c for c in valid_coords if c is not None]

    if not valid_coords:
        return []

    lats = [c[0] for c in valid_coords]
    lons = [c[1] for c in valid_coords]

    # Адаптивный bbox: мин. запас 0.02° (~2.2 км) вокруг крайних ДТП
    raw_lat_min = min(lats) - BBOX_MARGIN
    raw_lon_min = min(lons) - BBOX_MARGIN
    raw_lat_max = max(lats) + BBOX_MARGIN
    raw_lon_max = max(lons) + BBOX_MARGIN

    # Ограничиваем минимальный размер bbox
    if raw_lat_max - raw_lat_min < BBOX_MIN_CLAMP:
        mid_lat = (raw_lat_max + raw_lat_min) / 2
        raw_lat_min = mid_lat - BBOX_MIN_CLAMP / 2
        raw_lat_max = mid_lat + BBOX_MIN_CLAMP / 2
    if raw_lon_max - raw_lon_min < BBOX_MIN_CLAMP:
        mid_lon = (raw_lon_max + raw_lon_min) / 2
        raw_lon_min = mid_lon - BBOX_MIN_CLAMP / 2
        raw_lon_max = mid_lon + BBOX_MIN_CLAMP / 2

    # Clamp к мировым границам
    lat_min = max(raw_lat_min, 41.0)
    lon_min = max(raw_lon_min, 19.0)
    lat_max = min(raw_lat_max, 70.0)
    lon_max = min(raw_lon_max, 180.0)

    bbox = f"{lat_min},{lon_min},{lat_max},{lon_max}"

    # --- Шаг 1: In-memory кэш ---
    mem_polygons = _memory_cache_get(bbox)
    if mem_polygons is not None:
        return mem_polygons

    # --- Шаг 2: Разбиваем на тайлы ---
    tiles = _compute_bbox_tiles(lat_min, lon_min, lat_max, lon_max)

    if progress_callback:
        tile_info = f" ({len(tiles)} тайлов)" if len(tiles) > 1 else ""
        await progress_callback(
            f"Загрузка границ НП из OpenStreetMap{tile_info}...\n"
            f"BBOX: {bbox}"
        )

    # --- Шаг 3: Для каждого тайла — запрос к Overpass ---
    all_elements: list[dict] = []
    bbox_tile_indices: set[int] = set()  # тайлы, вернувшие bbox (не кэшируем)

    for tile_idx, (t_lat_min, t_lon_min, t_lat_max, t_lon_max) in enumerate(tiles):
        tile_bbox = f"{t_lat_min},{t_lon_min},{t_lat_max},{t_lon_max}"

        # Проверяем дисковый кэш для тайла
        cached_elements = _load_cache(tile_bbox)
        if cached_elements is not None:
            tile_polys, is_bbox = _parse_overpass_elements(cached_elements)
            if tile_polys and not is_bbox:
                all_elements.extend(cached_elements)
                logger.info(
                    f"Тайл {tile_idx + 1}/{len(tiles)}: из дискового кэша "
                    f"({len(cached_elements)} элементов)"
                )
                continue
            elif tile_polys and is_bbox:
                # bbox из кэша — игнорируем, запросим заново
                logger.info(
                    f"Тайл {tile_idx + 1}/{len(tiles)}: кэш содержит bbox, "
                    f"запрашиваем заново"
                )
            else:
                logger.info(
                    f"Тайл {tile_idx + 1}/{len(tiles)}: кэш пуст, "
                    f"запрашиваем OSM"
                )

        # Запрос к Overpass с параллельными зеркалами
        elements = await _fetch_overpass_parallel(tile_bbox, tile_idx, len(tiles))
        if elements:
            # Проверяем: geom или bbox?
            _, tile_is_bbox = _parse_overpass_elements(elements)
            if tile_is_bbox:
                bbox_tile_indices.add(tile_idx)
                logger.info(
                    f"Тайл {tile_idx + 1}/{len(tiles)}: получен bbox "
                    f"(не кэшируем, {len(elements)} элементов)"
                )
            all_elements.extend(elements)

    # --- Шаг 4: Дедупликация (для тайлов с перехлёстом) ---
    if len(tiles) > 1 and all_elements:
        all_elements = _dedup_elements(all_elements)

    # --- Шаг 5: Парсинг ---
    polygons: list[Polygon | MultiPolygon] = []
    is_bbox = True  # по умолчанию — fallback, чтобы не кэшировать
    if all_elements:
        polygons, is_bbox = _parse_overpass_elements(all_elements)

    if not polygons:
        logger.error(
            "Все зеркала Overpass API недоступны. "
            "Не удалось получить границы НП."
        )
        return []

    # --- Шаг 6: Сохраняем в кэши ---
    # В in-memory — только geom (не bbox)
    if not is_bbox:
        _memory_cache_put(bbox, polygons)
        # На диск — сохраняем элементы по каждому тайлу
        # (кроме тайлов, вернувших bbox — их не кэшируем)
        for tile_idx, (t_lat_min, t_lon_min, t_lat_max, t_lon_max) in enumerate(tiles):
            if tile_idx in bbox_tile_indices:
                # Этот тайл вернул bbox — пропускаем, не кэшируем
                logger.info(
                    f"Тайл {tile_idx + 1}/{len(tiles)}: пропущен "
                    f"(был bbox fallback)"
                )
                continue
            tile_bbox = f"{t_lat_min},{t_lon_min},{t_lat_max},{t_lon_max}"
            tile_elements = _load_cache(tile_bbox)
            if tile_elements is None:
                # Фильтруем элементы, принадлежащие этому тайлу
                tile_elems = _filter_elements_for_bbox(
                    all_elements, t_lat_min, t_lon_min, t_lat_max, t_lon_max,
                )
                if tile_elems:
                    _save_cache(tile_bbox, tile_elems)
    else:
        # bbox fallback — НЕ кэшируем (ни в памяти, ни на диске)
        logger.warning(
            "Получены bounding boxes вместо реальных полигонов — "
            "результат НЕ кэширован"
        )

    logger.info(
        f"Итого границ НП: {len(polygons)} полигонов "
        f"(элементов: {len(all_elements)}, тайлов: {len(tiles)})"
    )
    return polygons


def _filter_elements_for_bbox(
    elements: list[dict],
    lat_min: float, lon_min: float,
    lat_max: float, lon_max: float,
) -> list[dict]:
    """
    Фильтрует элементы Overpass, оставляя только те, чей центр
    попадает в указанный bbox. Используется при кэшировании по тайлам.
    """
    filtered = []
    for el in elements:
        bounds = el.get("bounds")
        if bounds:
            center_lat = (bounds.get("minlat", 0) + bounds.get("maxlat", 0)) / 2
            center_lon = (bounds.get("minlon", 0) + bounds.get("maxlon", 0)) / 2
            if lat_min <= center_lat <= lat_max and lon_min <= center_lon <= lon_max:
                filtered.append(el)
            continue
        # Для элементов с geometry (out geom) — по первой координате
        geom = el.get("geometry") or []
        members = el.get("members") or []
        if geom:
            ref = geom[0]
            ref_lat = ref.get("lat", 0)
            ref_lon = ref.get("lon", 0)
            if lat_min <= ref_lat <= lat_max and lon_min <= ref_lon <= lon_max:
                filtered.append(el)
        elif members:
            for m in members:
                m_geom = m.get("geometry", [])
                if m_geom:
                    ref = m_geom[0]
                    ref_lat = ref.get("lat", 0)
                    ref_lon = ref.get("lon", 0)
                    if lat_min <= ref_lat <= lat_max and lon_min <= ref_lon <= lon_max:
                        filtered.append(el)
                        break
    return filtered


async def _fetch_overpass_parallel(
    bbox_str: str,
    tile_idx: int = 0,
    total_tiles: int = 1,
) -> list[dict] | None:
    """
    Параллельный запрос к зеркалам Overpass API.

    Стратегия:
    1. Параллельно запускаем запросы к 2 зеркалам (out geom)
    2. Если оба не удались — пауза 5 сек, повторная попытка geom
    3. Если и повторная попытка не удалась — fallback на out bb
    4. Последняя надежда: out geom последовательно на оставшихся зеркалах
    """
    geom_query = (
        "[out:json][timeout:90];\n"
        "(\n"
        f'  relation["place"~"{PLACE_FILTER}"]({bbox_str});\n'
        f'  way["place"~"{PLACE_FILTER}"]({bbox_str});\n'
        ");\n"
        "out geom;\n"
    )

    bb_query = (
        "[out:json][timeout:90];\n"
        "(\n"
        f'  relation["place"~"{PLACE_FILTER}"]({bbox_str});\n'
        f'  way["place"~"{PLACE_FILTER}"]({bbox_str});\n'
        ");\n"
        "out bb;\n"
    )

    # --- Пытаемся получить geom параллельно с 2 зеркал ---
    geom_urls = OVERPASS_URLS[:2]

    for attempt in range(1, 3):  # максимум 2 попытки geom
        geom_tasks = []
        for url in geom_urls:
            geom_tasks.append(
                asyncio.create_task(
                    _overpass_request(url, geom_query, OVERPASS_HEADERS, "geom"),
                    name=f"geom-{url}",
                )
            )

        geom_results = await asyncio.gather(*geom_tasks, return_exceptions=True)

        for result in geom_results:
            if isinstance(result, list) and result:
                polygons, is_bbox = _parse_overpass_elements(result)
                if polygons and not is_bbox:
                    logger.info(
                        f"Тайл {tile_idx + 1}/{total_tiles}: "
                        f"{len(polygons)} полигонов (out geom, parallel"
                        f"{f', попытка {attempt}' if attempt > 1 else ''})"
                    )
                    _save_cache(bbox_str, result)
                    return result

        # Перед второй попыткой — пауза, чтобы сервер Overpass успел
        if attempt == 1:
            logger.info(
                f"Тайл {tile_idx + 1}/{total_tiles}: "
                f"geom не удался, повторная попытка через 5 сек..."
            )
            await asyncio.sleep(5)

    # --- Fallback: параллельно out bb на 2 зеркалах ---
    bb_tasks = []
    for url in OVERPASS_URLS[:2]:
        bb_tasks.append(
            asyncio.create_task(
                _overpass_request(url, bb_query, OVERPASS_HEADERS, "bb"),
                name=f"bb-{url}",
            )
        )

    bb_results = await asyncio.gather(*bb_tasks, return_exceptions=True)

    for result in bb_results:
        if isinstance(result, list) and result:
            polygons, is_bbox = _parse_overpass_elements(result)
            if polygons:
                logger.info(
                    f"Тайл {tile_idx + 1}/{total_tiles}: "
                    f"{len(polygons)} bounding boxes (out bb, parallel)"
                )
                # НЕ сохраняем bb в кэш
                return result

    # --- Последняя попытка: seq через все зеркала (geom) ---
    for url in OVERPASS_URLS[2:]:
        elements = await _overpass_request(
            url, geom_query, OVERPASS_HEADERS, "geom",
        )
        if elements is not None:
            polygons, is_bbox = _parse_overpass_elements(elements)
            if polygons and not is_bbox:
                _save_cache(bbox_str, elements)
                return elements

    logger.warning(
        f"Тайл {tile_idx + 1}/{total_tiles}: все зеркала недоступны"
    )
    return None


async def _overpass_request(
    url: str,
    query: str,
    headers: dict,
    mode: str,
) -> list[dict] | None:
    """
    Выполняет единичный запрос к Overpass API.

    Args:
        url: URL зеркала Overpass
        query: Overpass QL запрос
        headers: HTTP-заголовки
        mode: «geom» или «bb» (для логирования)

    Returns:
        Список elements или None при ошибке.
    """
    try:
        logger.info(
            f"Overpass API ({url}): запрос (mode={mode})..."
        )
        async with httpx.AsyncClient(
            verify=False,
            headers=headers,
        ) as client:
            resp = await client.post(
                url, data={"data": query}, timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()

        elements = data.get("elements", [])
        logger.info(
            f"Overpass API ({url}): получено "
            f"{len(elements)} элементов (mode={mode})"
        )
        return elements

    except httpx.HTTPStatusError as e:
        logger.warning(
            f"Overpass API ({url}, mode={mode}): "
            f"HTTP {e.response.status_code}"
        )
        return None
    except Exception as e:
        logger.warning(
            f"Overpass API ({url}, mode={mode}): {e}"
        )
        return None


# ========================
# Классификация ДТП: НП / вне НП
# ========================

def _point_in_any_polygon(
    lat: float,
    lon: float,
    polygons: list[Polygon | MultiPolygon],
) -> bool:
    """
    Попадает ли точка хотя бы в один полигон НП.

    Использует Shapely Point.contains для точной проверки.
    Shapely: (x, y) = (lon, lat).
    """
    point = Point(lon, lat)
    for poly in polygons:
        try:
            if poly.contains(point):
                return True
        except Exception:
            continue
    return False


def classify_cards(
    cards: list[dict],
    settlement_polygons: list[Polygon | MultiPolygon],
) -> tuple[list[dict], list[dict]]:
    """
    Разделяет карточки на две группы: НП и вне НП.

    Использует Shapely unary_union + prepared geometry
    для быстрой проверки O(1) на точку после инициализации.

    Args:
        cards: Карточки ДТП с координатами
        settlement_polygons: Список Shapely-полигонов границ НП

    Returns:
        (settlement_cards, non_settlement_cards)
    """
    if not settlement_polygons:
        return [], list(cards)

    settlement_cards = []
    non_settlement_cards = []

    # Объединяем все полигоны в одну геометрию и подготавливаем
    # для O(1) проверки contains на каждую точку
    try:
        merged = unary_union(settlement_polygons)
        prepared = prep(merged)
        use_prepared = True
    except Exception as e:
        logger.warning(
            f"Не удалось создать prepared geometry: {e}. "
            f"Используется поцикличная проверка."
        )
        prepared = None
        use_prepared = False

    for card in cards:
        coords = _parse_coords(card)
        if coords is None:
            non_settlement_cards.append(card)
            continue

        # Shapely: (x, y) = (lon, lat)
        point = Point(coords[1], coords[0])
        in_settlement = False

        try:
            if use_prepared and prepared is not None:
                in_settlement = prepared.contains(point)
            else:
                in_settlement = _point_in_any_polygon(
                    coords[0], coords[1], settlement_polygons,
                )
        except Exception:
            pass

        if in_settlement:
            settlement_cards.append(card)
        else:
            non_settlement_cards.append(card)

    logger.info(
        f"Классификация: {len(settlement_cards)} в НП, "
        f"{len(non_settlement_cards)} вне НП "
        f"(всего {len(cards)}, полигонов: {len(settlement_polygons)})"
    )
    return settlement_cards, non_settlement_cards


# ========================
# Алгоритм: НП (перекрёстки 50 м, участки 100 м)
# ========================

def _build_cluster(
    cards: list[dict],
    center: tuple[float, float] | None,
    zone_type: str,
    road_name: str = "",
    start_pos: float | None = None,
    end_pos: float | None = None,
) -> dict:
    """Формирует словарь очага из группы карточек."""
    total_deaths = sum(_safe_int(c.get("pog")) for c in cards)
    total_injured = sum(_safe_int(c.get("ran")) for c in cards)
    dates = [_get_date(c) for c in cards]
    type_counter = Counter(_get_dtp_type(c) for c in cards)

    dominant = None
    for t, cnt in type_counter.most_common():
        if cnt >= SAME_TYPE_THRESHOLD:
            dominant = t
            break

    road = road_name or _get_road_name(cards[0])

    first_coords = _parse_coords(cards[0])
    last_coords = _parse_coords(cards[-1])

    return {
        "zone_type": zone_type,
        "road": road,
        "total_accidents": len(cards),
        "deaths": total_deaths,
        "injured": total_injured,
        "dates": dates,
        "type_counter": dict(type_counter),
        "dominant_type": dominant,
        "first_coords": first_coords,
        "last_coords": last_coords,
        "center": center or first_coords or (0, 0),
        "start_pos": start_pos,
        "end_pos": end_pos,
        "cards": cards,
        # Поля для camera_matcher
        "has_piketazh": start_pos is not None,
        "start_km": start_pos,
        "end_km": end_pos,
    }


def _cluster_cards_by_radius(
    cards_with_idx: list[tuple[int, dict]],
    radius_m: float,
    assigned: set[int],
) -> list[int] | None:
    """
    Для карточки cards_with_idx[0] ищет все карточки в радиусе radius_m.
    Если порог очага выполнен — возвращает список индексов (включая центральный),
    иначе None.
    """
    if not cards_with_idx:
        return None

    first_idx, first_card = cards_with_idx[0]
    center = _parse_coords(first_card)
    if center is None:
        return None

    group_indices = [first_idx]
    group_cards = [first_card]

    for idx, card in cards_with_idx[1:]:
        if idx in assigned:
            continue
        coords = _parse_coords(card)
        if coords is None:
            continue
        dist = haversine_meters(
            center[0], center[1], coords[0], coords[1],
        )
        if dist <= radius_m:
            group_indices.append(idx)
            group_cards.append(card)

    type_counter = Counter(_get_dtp_type(c) for c in group_cards)
    is_cluster, _ = _check_cluster_criteria(type_counter, len(group_cards))

    if is_cluster:
        return group_indices
    return None


def find_settlement_concentration_points(cards: list[dict]) -> list[dict]:
    """
    Поиск очагов в населённых пунктах — 3 прохода.

    1-й проход: перекрёстки (50 м) с проверкой пикетажа:
      Шаг 1a: ДТП с дорогой+пикетажем — сначала по пикетажу (±50 м),
              затем fallback радиус 50 м по GPS с piketаж-фильтром
      Шаг 1b: ДТП без пикетажа — стандартный радиус 50 м по GPS
    2-й проход: дороги с наименованием + пикетажем, окно 200 м
    3-й проход: радиус 100 м с проверкой пикетажа (200 м для ДТП
               с одинаковой дорогой и пикетажем)
    """
    if not cards:
        return []

    # Подготавливаем: индекс + карточка, сортируем по дате
    indexed = [(i, c) for i, c in enumerate(cards)]
    indexed.sort(key=lambda x: _get_date(x[1]))

    # Фильтруем только карточки с координатами
    indexed_with_coords = [(i, c) for i, c in indexed if _parse_coords(c)]

    assigned: set[int] = set()
    clusters: list[dict] = []

    # --- 1-й проход: перекрёстки (50 м) с проверкой пикетажа ---

    # Шаг 1a: Перекрёстки С наименованием дороги и пикетажем
    for idx, card in indexed_with_coords:
        if idx in assigned:
            continue
        if not _is_intersection(card):
            continue
        if not _has_road_and_piketazh(card):
            continue

        center_road = _get_road_name(card)
        center_km = _get_km_m(card)
        center = _parse_coords(card)
        if center is None:
            continue

        # 1a-1: Проверка по пикетажу: ±50 м по той же дороге,
        #        только ДТП с «перекрёсток»
        piketazh_candidates = []
        for j, c in indexed_with_coords:
            if j in assigned or j == idx:
                continue
            if _get_road_name(c) != center_road:
                continue
            other_km = _get_km_m(c)
            if other_km is None:
                continue
            if abs(center_km - other_km) * 1000.0 > SETTLEMENT_INTERSECTION_RADIUS_M:
                continue
            if not _is_intersection(c):
                continue
            piketazh_candidates.append((j, c))

        if piketazh_candidates:
            group_cards = [card] + [c for _, c in piketazh_candidates]
            type_counter = Counter(
                _get_dtp_type(c) for c in group_cards
            )
            is_cluster, _ = _check_cluster_criteria(
                type_counter, len(group_cards),
            )
            if is_cluster:
                assigned.add(idx)
                for j, _ in piketazh_candidates:
                    assigned.add(j)
                group_cards.sort(key=lambda c: _get_date(c))
                clusters.append(
                    _build_cluster(
                        group_cards, center, "settlement_intersection"
                    )
                )
                continue

        # 1a-2: Fallback — радиус 50 м по GPS (только «перекрёстки»),
        #        с проверкой пикетажа для ДТП на той же дороге
        gps_candidates = [
            (j, c) for j, c in indexed_with_coords
            if j not in assigned and j != idx
        ]

        group_indices = [idx]
        group_cards = [card]

        for j, c in gps_candidates:
            if not _is_intersection(c):
                continue
            coords = _parse_coords(c)
            if coords is None:
                continue
            dist = haversine_meters(
                center[0], center[1], coords[0], coords[1],
            )
            if dist > SETTLEMENT_INTERSECTION_RADIUS_M:
                continue

            # Проверка пикетажа: если ДТП на той же дороге
            # и имеет пикетаж — проверяем окно 50 м
            other_road = _get_road_name(c)
            other_km = _get_km_m(c)
            if (
                other_road == center_road
                and other_km is not None
            ):
                piketazh_diff_m = abs(other_km - center_km) * 1000.0
                if piketazh_diff_m > SETTLEMENT_INTERSECTION_RADIUS_M:
                    # Пикетаж различается более чем на 50 м — исключаем
                    continue

            group_indices.append(j)
            group_cards.append(c)

        type_counter = Counter(
            _get_dtp_type(c) for c in group_cards
        )
        is_cluster, _ = _check_cluster_criteria(
            type_counter, len(group_cards),
        )
        if is_cluster:
            assigned.update(group_indices)
            group_cards.sort(key=lambda c: _get_date(c))
            clusters.append(
                _build_cluster(
                    group_cards, center, "settlement_intersection"
                )
            )

    # Шаг 1b: Перекрёстки БЕЗ пикетажа — радиус 50 м по GPS
    # (с пикетажем уже обработаны в шаге 1a)
    # Кандидаты должны быть тоже «перекрёстками» (sdor)
    for idx, card in indexed_with_coords:
        if idx in assigned:
            continue
        if not _is_intersection(card):
            continue
        if _has_road_and_piketazh(card):
            continue  # уже обработаны в шаге 1a

        center = _parse_coords(card)
        if center is None:
            continue

        # Собираем кандидатов в радиусе 50 м (только «перекрёстки»)
        group_indices = [idx]
        group_cards = [card]

        for j, c in indexed_with_coords:
            if j in assigned or j == idx:
                continue
            if not _is_intersection(c):
                continue
            coords = _parse_coords(c)
            if coords is None:
                continue
            dist = haversine_meters(
                center[0], center[1], coords[0], coords[1],
            )
            if dist <= SETTLEMENT_INTERSECTION_RADIUS_M:
                group_indices.append(j)
                group_cards.append(c)

        type_counter = Counter(
            _get_dtp_type(c) for c in group_cards
        )
        is_cluster, _ = _check_cluster_criteria(
            type_counter, len(group_cards),
        )

        if is_cluster:
            assigned.update(group_indices)
            group_cards.sort(key=lambda c: _get_date(c))
            clusters.append(
                _build_cluster(group_cards, center, "settlement_intersection")
            )

    # --- 2-й проход: дороги с наименованием и пикетажем, окно 200 м ---
    road_cards_with_km = [
        (idx, card) for idx, card in indexed_with_coords
        if idx not in assigned and _has_road_and_piketazh(card)
    ]

    # Группируем по названию дороги
    road_groups: dict[str, list[tuple[int, dict]]] = {}
    for idx, card in road_cards_with_km:
        road = _get_road_name(card)
        road_groups.setdefault(road, []).append((idx, card))

    pass2_found = False

    for road_name, items in road_groups.items():
        # Подготавливаем (idx, card, pos_km)
        items_pos: list[tuple[int, dict, float]] = []
        for idx, card in items:
            pos = _get_km_m(card)
            if pos is not None:
                items_pos.append((idx, card, pos))

        if not items_pos:
            continue

        # Сортируем по пикетажу
        items_pos.sort(key=lambda x: x[2])

        # Скользящее окно 200 м
        for i, (idx, card, pos) in enumerate(items_pos):
            if idx in assigned:
                continue

            window_end = pos + SETTLEMENT_ROAD_WINDOW_KM

            group_indices = [idx]
            group_cards = [card]

            for j in range(i + 1, len(items_pos)):
                other_idx, other_card, other_pos = items_pos[j]
                if other_idx in assigned:
                    continue
                if other_pos <= window_end:
                    group_indices.append(other_idx)
                    group_cards.append(other_card)

            type_counter = Counter(_get_dtp_type(c) for c in group_cards)
            is_cluster, _ = _check_cluster_criteria(
                type_counter, len(group_cards),
            )

            if is_cluster:
                assigned.update(group_indices)
                group_cards.sort(key=lambda c: _get_date(c))
                center = _parse_coords(card)
                clusters.append(
                    _build_cluster(
                        group_cards, center, "settlement_road",
                        road_name=road_name,
                        start_pos=pos,
                        end_pos=window_end,
                    )
                )
                pass2_found = True
            # Неассигнированные карточки переходят в 3-й проход

    logger.info(
        f"НП 2-й проход (пикетаж): "
        f"{len(clusters)} очагов найдено" if pass2_found
        else "НП 2-й проход: очагов не найдено"
    )

    # --- 3-й проход: радиус 100 м с проверкой пикетажа ---
    for idx, card in indexed_with_coords:
        if idx in assigned:
            continue

        center = _parse_coords(card)
        if center is None:
            assigned.add(idx)
            continue

        center_road = _get_road_name(card)
        center_km = _get_km_m(card)
        center_has_road_km = bool(center_road) and center_km is not None

        # Собираем кандидатов в радиусе 100 м
        candidates = [
            (j, c) for j, c in indexed_with_coords
            if j not in assigned and j != idx
        ]

        group_indices = [idx]
        group_cards = [card]

        for j, c in candidates:
            coords = _parse_coords(c)
            if coords is None:
                continue
            dist = haversine_meters(
                center[0], center[1], coords[0], coords[1],
            )
            if dist > SETTLEMENT_OTHER_RADIUS_M:
                continue

            # Проверка пикетажа: если центр и кандидат на одной дороге
            # и оба имеют пикетаж — проверяем окно 200 м
            other_road = _get_road_name(c)
            other_km = _get_km_m(c)

            if (
                center_has_road_km
                and other_road == center_road
                and other_km is not None
            ):
                piketazh_diff_m = abs(other_km - center_km) * 1000.0
                if piketazh_diff_m > SETTLEMENT_ROAD_WINDOW_KM * 1000.0:
                    # Пикетаж различается более чем на 200 м — исключаем
                    continue

            group_indices.append(j)
            group_cards.append(c)

        type_counter = Counter(_get_dtp_type(c) for c in group_cards)
        is_cluster, _ = _check_cluster_criteria(type_counter, len(group_cards))

        if is_cluster:
            assigned.update(group_indices)
            group_cards.sort(key=lambda c: _get_date(c))
            clusters.append(
                _build_cluster(group_cards, center, "settlement_segment")
            )
        else:
            assigned.add(idx)

    logger.info(f"Очаги в НП (итого): {len(clusters)} найдено")
    return clusters


# ========================
# Алгоритм: Вне НП (окна 1 км по дорогам)
# ========================

def find_nonsettlement_concentration_points(cards: list[dict]) -> list[dict]:
    """
    Поиск очагов вне населённых пунктов.

    1. Группировка по названию дороги (поле dor)
    2. Сортировка по пикетажу (km+m) или по координатам
    3. Скользящее окно 1 км
    """
    if not cards:
        return []

    # Группируем по дороге
    road_groups: dict[str, list[dict]] = {}
    for card in cards:
        road = _get_road_name(card)
        if not road:
            continue
        road_groups.setdefault(road, []).append(card)

    all_clusters: list[dict] = []

    for road_name, road_cards in road_groups.items():
        # Подготавливаем: (card, position_km, coords)
        cards_pos: list[tuple[dict, float, tuple | None]] = []
        for card in road_cards:
            pos = _get_km_m(card)
            coords = _parse_coords(card)
            if pos is not None:
                cards_pos.append((card, pos, coords))
            elif coords is not None:
                cards_pos.append((card, 0.0, coords))  # позиция вычислим ниже

        if not cards_pos:
            continue

        # Если есть карточки без пикетажа — вычисляем по координатам
        ref_coords = None
        for card, pos, coords in cards_pos:
            if coords:
                ref_coords = coords
                break

        if ref_coords is None:
            continue

        # Пересчитываем позиции для карточек без km/m
        for i, (card, pos, coords) in enumerate(cards_pos):
            if pos == 0.0 and _get_km_m(card) is None and coords:
                dist_km = haversine_meters(
                    ref_coords[0], ref_coords[1],
                    coords[0], coords[1],
                ) / 1000.0
                cards_pos[i] = (card, dist_km, coords)

        # Сортируем по позиции, затем по дате
        cards_pos.sort(key=lambda x: (x[1], _get_date(x[0])))

        # Определяем окно: если на дороге есть хотя бы одно ДТП с пикетажем — 1 км,
        # если ни одного — 200 м (расчёт по GPS менее точен)
        has_piketazh = any(_get_km_m(card) is not None for card, _, _ in cards_pos)
        window_km = NON_SETTLEMENT_WINDOW_KM if has_piketazh else NON_SETTLEMENT_NO_PK_WINDOW_KM

        # Скользящее окно
        assigned: set[int] = set()

        for i, (card, pos, coords) in enumerate(cards_pos):
            if i in assigned:
                continue

            window_start = pos
            window_end = pos + window_km

            group_indices = [i]
            group_cards = [card]

            for j, (other_card, other_pos, other_coords) in enumerate(cards_pos):
                if j in assigned or j == i:
                    continue
                if window_start <= other_pos <= window_end:
                    group_indices.append(j)
                    group_cards.append(other_card)

            type_counter = Counter(_get_dtp_type(c) for c in group_cards)
            is_cluster, _ = _check_cluster_criteria(type_counter, len(group_cards))

            if is_cluster:
                assigned.update(group_indices)
                group_cards.sort(key=lambda c: _get_date(c))

                first_coords = _parse_coords(group_cards[0])
                last_coords = _parse_coords(group_cards[-1])

                all_clusters.append(
                    _build_cluster(
                        group_cards, coords or first_coords, "nonsettlement",
                        road_name=road_name,
                        start_pos=window_start,
                        end_pos=window_end,
                    )
                )
            else:
                assigned.add(i)

    logger.info(f"Очаги вне НП: {len(all_clusters)} найдено")
    return all_clusters


# ========================
# Excel-выход
# ========================

ZONE_TYPE_LABELS = {
    "settlement_intersection": "НП - Перекрёсток",
    "settlement_road": "НП - Участок дороги (пикетаж)",
    "settlement_segment": "НП - Участок дороги",
    "nonsettlement": "Вне НП",
}

CONCENTRATION_COLUMNS = [
    "№ очага",
    "Тип зоны",
    "Дорога/Улица",
    "Пикетаж начало",
    "Пикетаж конец",
    "Широта первого ДТП",
    "Долгота первого ДТП",
    "Широта последнего ДТП",
    "Долгота последнего ДТП",
    "Кол-во ДТП",
    "Виды ДТП (детализация)",
    "Доминирующий вид",
    "Погибло",
    "Ранено",
    "Дата первого ДТП",
    "Дата последнего ДТП",
    # --- Камеры фотовидеофиксации ---
    "Статус покрытия камерой",
    "Камера: номер",
    "Камера: адрес",
    "Камера: координаты",
    "Ближайшая камера: номер",
    "Ближайшая камера: адрес",
    "Ближайшая камера: координаты",
    "Расстояние до камеры (м)",
]

DETAIL_COLUMNS = [
    "№ очага",
    "Дата ДТП",
    "Вид ДТП",
    "Дорога/Улица",
    "Пикетаж",
    "Широта",
    "Долгота",
    "Погибло",
    "Ранено",
]


def _format_piketazh(pos: float | None) -> str:
    """Форматирует пикетаж из км.ddd в строку «КК+МММ»."""
    if pos is None:
        return ""
    km = int(pos)
    m = round((pos - km) * 1000)
    return f"{km}+{m:03d}"


def _first_last_piketazh(cards: list[dict]) -> tuple[float | None, float | None]:
    """
    Возвращает (пикетаж_первого_ДТП, пикетаж_последнего_ДТП)
    по минимальному и максимальному пикетажу среди карточек.
    """
    positions = []
    for card in cards:
        pos = _get_km_m(card)
        if pos is not None:
            positions.append(pos)
    if not positions:
        return None, None
    return min(positions), max(positions)


def get_concentration_column_names() -> list[str]:
    """Названия колонок для Excel-файла очагов."""
    return list(CONCENTRATION_COLUMNS)


def get_detail_column_names() -> list[str]:
    """Названия колонок для листа детализации ДТП в очагах."""
    return list(DETAIL_COLUMNS)


def _camera_row_fields(cluster: dict) -> dict[str, str]:
    """Формирует словарь с полями камер для строки Excel.

    Ожидает в cluster ключ "camera_match" — результат
    camera_loader.find_cameras_for_cluster().
    """
    match = cluster.get("camera_match") or {}

    # Статус покрытия
    status = match.get("status", "открыт")
    if status == "закрыт":
        status_display = "Закрыт"
    elif match.get("nearest"):
        status_display = "Открыт (есть ближайшая)"
    else:
        status_display = "Открыт"

    # Камера в очаге
    cam_in = match.get("in_cluster")
    if cam_in:
        cam_num = cam_in.get("number", "")
        cam_addr = cam_in.get("address", "")
        cam_coords = (
            f"{cam_in['lat']:.6f}, {cam_in['lon']:.6f}"
        )
    else:
        cam_num = ""
        cam_addr = ""
        cam_coords = ""

    # Ближайшая камера
    near = match.get("nearest")
    if near:
        near_num = near.get("number", "")
        near_addr = near.get("address", "")
        near_coords = f"{near['lat']:.6f}, {near['lon']:.6f}"
        near_dist = str(match.get("nearest_dist_m", ""))
    else:
        near_num = ""
        near_addr = ""
        near_coords = ""
        near_dist = ""

    return {
        "Статус покрытия камерой": status_display,
        "Камера: номер": cam_num,
        "Камера: адрес": cam_addr,
        "Камера: координаты": cam_coords,
        "Ближайшая камера: номер": near_num,
        "Ближайшая камера: адрес": near_addr,
        "Ближайшая камера: координаты": near_coords,
        "Расстояние до камеры (м)": near_dist,
    }


def enrich_clusters_with_cameras(
    clusters: list[dict],
    cameras: list[dict],
) -> None:
    """
    Обогащает кластеры результатами поиска камер.

    Модифицирует каждый кластер in-place, добавляя ключ
    "camera_match" с результатом camera_loader.find_cameras_for_cluster().
    """
    if not cameras:
        for c in clusters:
            c["camera_match"] = None
        return

    from camera_loader import find_cameras_for_cluster

    for cluster in clusters:
        cluster["camera_match"] = find_cameras_for_cluster(
            cluster, cameras,
        )

    # Статистика
    closed = sum(
        1 for c in clusters
        if (c.get("camera_match") or {}).get("status") == "закрыт"
    )
    logger.info(
        f"Камеры: {closed}/{len(clusters)} очагов закрыты "
        f"({len(cameras)} камер проверено)"
    )


def build_concentration_excel_data(
    clusters: list[dict],
) -> list[dict[str, str]]:
    """Строит данные для Excel-файла очагов концентрации ДТП."""
    rows = []

    for i, cluster in enumerate(clusters, start=1):
        # Виды ДТП
        types_parts = [
            f"{t}: {c}" for t, c in cluster["type_counter"].items()
        ]
        types_str = "; ".join(types_parts)

        # Координаты
        fc = cluster.get("first_coords")
        lc = cluster.get("last_coords")
        first_lat = f"{fc[0]:.6f}" if fc else ""
        first_lon = f"{fc[1]:.6f}" if fc else ""
        last_lat = f"{lc[0]:.6f}" if lc else ""
        last_lon = f"{lc[1]:.6f}" if lc else ""

        # Пикетаж: первое и последнее ДТП в очаге
        start_pos, end_pos = _first_last_piketazh(cluster["cards"])
        start_str = _format_piketazh(start_pos)
        end_str = _format_piketazh(end_pos)

        # Даты: первое и последнее ДТП
        dates = cluster["dates"]
        first_date = dates[0] if dates else ""
        last_date = dates[-1] if dates else ""

        zone_label = ZONE_TYPE_LABELS.get(
            cluster["zone_type"], cluster["zone_type"],
        )

        rows.append({
            "№ очага": str(i),
            "Тип зоны": zone_label,
            "Дорога/Улица": cluster["road"],
            "Пикетаж начало": start_str,
            "Пикетаж конец": end_str,
            "Широта первого ДТП": first_lat,
            "Долгота первого ДТП": first_lon,
            "Широта последнего ДТП": last_lat,
            "Долгота последнего ДТП": last_lon,
            "Кол-во ДТП": str(cluster["total_accidents"]),
            "Виды ДТП (детализация)": types_str,
            "Доминирующий вид": cluster.get("dominant_type", ""),
            "Погибло": str(cluster["deaths"]),
            "Ранено": str(cluster["injured"]),
            "Дата первого ДТП": first_date,
            "Дата последнего ДТП": last_date,
            # --- Камеры фотовидеофиксации ---
            **_camera_row_fields(cluster),
        })

    return rows


def build_concentration_detail_data(
    clusters: list[dict],
) -> list[dict[str, str]]:
    """
    Строит данные для листа детализации:
    все ДТП, попавшие в очаги, с указанием номера очага.
    """
    rows = []

    for i, cluster in enumerate(clusters, start=1):
        for card in cluster["cards"]:
            coords = _parse_coords(card)
            pos = _get_km_m(card)
            piketazh_str = _format_piketazh(pos)

            lat_str = f"{coords[0]:.6f}" if coords else ""
            lon_str = f"{coords[1]:.6f}" if coords else ""

            rows.append({
                "№ очага": str(i),
                "Дата ДТП": _get_date(card),
                "Вид ДТП": _get_dtp_type(card),
                "Дорога/Улица": _get_road_name(card),
                "Пикетаж": piketazh_str,
                "Широта": lat_str,
                "Долгота": lon_str,
                "Погибло": str(_safe_int(card.get("pog"))),
                "Ранено": str(_safe_int(card.get("ran"))),
            })

    return rows


# ========================
# Точка входа
# ========================

async def calculate_concentration_points(
    cards: list[dict],
    progress_callback: Callable[[str], Awaitable[None]] | None = None,
    settlement_polygons: list[Polygon | MultiPolygon] | None = None,
) -> list[dict]:
    """
    Главная функция: расчёт всех очагов концентрации ДТП.

    Args:
        cards: Список сырых карточек ДТП
        progress_callback: async-функция для обновления статуса
        settlement_polygons: Если переданы — используются вместо запроса к OSM.
            Это позволяет переиспользовать полигоны между вызовами
            (например, при сравнении с прошлым годом).

    Returns:
        Список словарей очагов
    """
    if not cards:
        return []

    # Шаг 1: Фильтр — только карточки с координатами
    #   и исключаем ДТП вне дороги (внутридворовые, автостоянки)
    cards_with_coords = [
        c for c in cards
        if _parse_coords(c) and not _is_off_road(c)
    ]
    no_coords = len(cards) - len(cards_with_coords)

    if no_coords > 0:
        logger.warning(f"{no_coords} карточек без координат или вне дороги пропущены")

    if not cards_with_coords:
        logger.warning("Нет карточек с координатами — расчёт невозможен")
        return []

    # Шаг 2: Границы НП
    # Если полигоны переданы снаружи — используем их (OSM не запрашиваем)
    if settlement_polygons is None:
        settlement_polygons = await fetch_settlement_boundaries(
            cards_with_coords, progress_callback,
        )
    else:
        logger.info(
            f"Границы НП переданы извне: {len(settlement_polygons)} полигонов "
            f"(OSM-запрос пропущен)"
        )

    if not settlement_polygons:
        logger.warning(
            "Не удалось получить границы НП из OSM. "
            "Все ДТП будут обработаны как вне НП."
        )

    # Шаг 3: Классификация
    if progress_callback:
        await progress_callback(
            f"Классификация ДТП...\n"
            f"Всего с координатами: {len(cards_with_coords)}"
        )

    if settlement_polygons:
        settlement_cards, non_settlement_cards = classify_cards(
            cards_with_coords, settlement_polygons,
        )
    else:
        # Fallback: все как вне НП
        settlement_cards = []
        non_settlement_cards = cards_with_coords

    # Шаг 4: Очаги в НП
    if progress_callback:
        await progress_callback(
            f"Поиск очагов в НП ({len(settlement_cards)} ДТП)..."
        )

    settlement_clusters = find_settlement_concentration_points(settlement_cards)

    # Шаг 5: Очаги вне НП
    if progress_callback:
        await progress_callback(
            f"Поиск очагов вне НП ({len(non_settlement_cards)} ДТП)..."
        )

    non_settlement_clusters = find_nonsettlement_concentration_points(
        non_settlement_cards,
    )

    # Объединяем: сначала НП, потом вне НП
    all_clusters = settlement_clusters + non_settlement_clusters

    logger.info(
        f"Итого очагов: {len(all_clusters)} "
        f"(НП: {len(settlement_clusters)}, "
        f"вне НП: {len(non_settlement_clusters)})"
    )

    return all_clusters


# ========================
# Историческая динамика очагов
# ========================

def _match_clusters(
    current_clusters: list[dict],
    prev_clusters: list[dict],
) -> dict[int, int | None]:
    """
    Сопоставляет текущие очаги с прошлыми по географической близости
    и совпадению названия дороги.

    Алгоритм: для каждого текущего очага ищет ближайший прошлый
    в пределах радиуса сопоставления (зависит от типа зоны).
    Дорога должна совпадать (если указана у обоих очагов).
    Каждый прошлый очаг сопоставляется не более одного раза.

    Returns:
        {current_index: prev_index | None}
    """
    matches: dict[int, int | None] = {}
    used_prev: set[int] = set()

    for ci, curr in enumerate(current_clusters):
        cc = curr.get("center")
        if not cc:
            matches[ci] = None
            continue

        # Радиус зависит от типа зоны
        radius = (
            MATCH_RADIUS_SETTLEMENT
            if curr["zone_type"].startswith("settlement")
            else MATCH_RADIUS_NONSETTLEMENT
        )

        best_dist = float("inf")
        best_idx: int | None = None

        for pi, prev in enumerate(prev_clusters):
            if pi in used_prev:
                continue

            # Предварительная фильтрация: название дороги должно совпадать
            curr_road = curr["road"].strip().lower()
            prev_road = prev["road"].strip().lower()
            if curr_road and prev_road and curr_road != prev_road:
                continue

            pc = prev.get("center")
            if not pc:
                continue

            dist = haversine_meters(cc[0], cc[1], pc[0], pc[1])
            if dist <= radius and dist < best_dist:
                best_dist = dist
                best_idx = pi

        matches[ci] = best_idx
        if best_idx is not None:
            used_prev.add(best_idx)

    logger.info(
        f"Сопоставление очагов: {len(current_clusters)} текущих, "
        f"{len(prev_clusters)} прошлых, "
        f"совпало {sum(1 for v in matches.values() if v is not None)}, "
        f"новых {sum(1 for v in matches.values() if v is None)}"
    )
    return matches


async def calculate_concentration_dynamics(
    current_cards: list[dict],
    prev_cards: list[dict],
    progress_callback: Callable[[str], Awaitable[None]] | None = None,
) -> list[dict]:
    """
    Рассчитывает очаги для двух периодов и определяет динамику каждого.

    Границы НП загружаются из OSM **один раз** по объединённому bbox
    обоих периодов — это сокращает нагрузку на Overpass API в 2 раза.

    Каждому очагу добавляется ключ ``dynamics``:
    {
        "status": "new" | "lost" | "growing" | "shrinking" | "stable",
        "prev_total": int | None,       # ДТП в прошлом периоде
        "prev_deaths": int | None,      # погибло в прошлом периоде
        "prev_injured": int | None,     # ранено в прошлом периоде
        "match_distance": float | None, # расстояние до прошлого очага (м)
    }

    Порядок результата: текущие очаги (с аннотацией динамики),
    затем исчезнувшие очаги (из прошлого периода).

    Args:
        current_cards: Карточки ДТП текущего периода
        prev_cards: Карточки ДТП прошлого периода (те же месяцы)
        progress_callback: async-функция для обновления статуса

    Returns:
        Список очагов с полем ``dynamics``
    """
    # --- Готовим карточки с координатами из обоих периодов ---
    current_filtered = [
        c for c in current_cards
        if _parse_coords(c) and not _is_off_road(c)
    ]
    prev_filtered = [
        c for c in prev_cards
        if _parse_coords(c) and not _is_off_road(c)
    ]

    if not current_filtered:
        logger.warning("Нет карточек текущего периода с координатами")
        return []

    # --- Загружаем границы НП ОДИН РАЗ по объединённому bbox ---
    combined_cards = current_filtered + prev_filtered
    if prev_filtered:
        if progress_callback:
            await progress_callback(
                f"Загрузка границ НП из OpenStreetMap...\n"
                f"(Один запрос для обоих периодов)\n"
                f"ДТП текущего: {len(current_filtered)}, "
                f"прошлого: {len(prev_filtered)}"
            )
        settlement_polygons = await fetch_settlement_boundaries(
            combined_cards, progress_callback,
        )
    else:
        settlement_polygons = await fetch_settlement_boundaries(
            current_filtered, progress_callback,
        )

    if settlement_polygons:
        logger.info(
            f"Динамика: границы НП загружены один раз: "
            f"{len(settlement_polygons)} полигонов "
            f"(OSM-запрос пропущен для прошлого периода)"
        )

    # --- Очаги текущего периода ---
    if progress_callback:
        await progress_callback("Расчёт очагов текущего периода...")
    current_clusters = await calculate_concentration_points(
        current_cards,
        progress_callback,
        settlement_polygons=settlement_polygons,
    )

    if not prev_cards:
        # Данных за прошлый год нет — все очаги помечаем как «новые»
        for c in current_clusters:
            c["dynamics"] = {
                "status": "new",
                "prev_total": None,
                "prev_deaths": None,
                "prev_injured": None,
                "match_distance": None,
            }
        logger.info(
            f"Динамика: нет данных за прошлый год, "
            f"{len(current_clusters)} очагов помечены как новые"
        )
        return current_clusters

    # --- Очаги прошлого периода (те же полигоны!) ---
    if progress_callback:
        await progress_callback(
            f"Расчёт очагов за прошлый год ({len(prev_cards)} ДТП)..."
        )
    prev_clusters = await calculate_concentration_points(
        prev_cards,
        progress_callback,
        settlement_polygons=settlement_polygons,
    )

    if not prev_clusters:
        # За прошлый год очагов не найдено — все текущие = новые
        for c in current_clusters:
            c["dynamics"] = {
                "status": "new",
                "prev_total": None,
                "prev_deaths": None,
                "prev_injured": None,
                "match_distance": None,
            }
        logger.info(
            f"Динамика: за прошлый год очагов не найдено, "
            f"{len(current_clusters)} очагов помечены как новые"
        )
        return current_clusters

    # --- Сопоставление ---
    if progress_callback:
        await progress_callback("Сопоставление очагов между периодами...")

    matches = _match_clusters(current_clusters, prev_clusters)

    # Аннотируем текущие очаги
    for ci, curr in enumerate(current_clusters):
        pi = matches.get(ci)
        if pi is not None:
            prev = prev_clusters[pi]
            cc = curr.get("center")
            pc = prev.get("center")
            dist = (
                haversine_meters(cc[0], cc[1], pc[0], pc[1])
                if cc and pc else None
            )

            curr_total = curr["total_accidents"]
            prev_total = prev["total_accidents"]

            if curr_total > prev_total:
                status = "growing"
            elif curr_total < prev_total:
                status = "shrinking"
            else:
                status = "stable"

            curr["dynamics"] = {
                "status": status,
                "prev_total": prev_total,
                "prev_deaths": prev["deaths"],
                "prev_injured": prev["injured"],
                "match_distance": dist,
            }
        else:
            curr["dynamics"] = {
                "status": "new",
                "prev_total": None,
                "prev_deaths": None,
                "prev_injured": None,
                "match_distance": None,
            }

    # --- Исчезнувшие очаги ---
    matched_prev = set(v for v in matches.values() if v is not None)
    lost_count = 0
    for pi, prev in enumerate(prev_clusters):
        if pi not in matched_prev:
            lost_count += 1
            lost_cluster = dict(prev)
            lost_cluster["dynamics"] = {
                "status": "lost",
                "prev_total": prev["total_accidents"],
                "prev_deaths": prev["deaths"],
                "prev_injured": prev["injured"],
                "match_distance": None,
            }
            # Флаг для корректного отображения в Excel
            lost_cluster["_is_lost"] = True
            current_clusters.append(lost_cluster)

    new_count = sum(
        1 for c in current_clusters
        if c["dynamics"]["status"] == "new"
    )
    logger.info(
        f"Динамика очагов: новых={new_count}, "
        f"исчезнувших={lost_count}, "
        f"всего={len(current_clusters)}"
    )

    return current_clusters


# ========================
# Excel-выход: динамика
# ========================

DYNAMICS_COLUMNS = [
    "№ очага",
    "Статус",
    "Тип зоны",
    "Дорога/Улица",
    "Пикетаж начало",
    "Пикетаж конец",
    "Широта",
    "Долгота",
    "Кол-во ДТП",
    "ДТП (пр. период)",
    "Изменение ДТП",
    "Виды ДТП (детализация)",
    "Доминирующий вид",
    "Погибло",
    "Ранено",
    "Погибло (пр. период)",
    "Ранено (пр. период)",
    "Дата первого ДТП",
    "Дата последнего ДТП",
]

DYNAMICS_DETAIL_COLUMNS = [
    "№ очага",
    "Статус",
    "Период",
    "Дата ДТП",
    "Вид ДТП",
    "Дорога/Улица",
    "Пикетаж",
    "Широта",
    "Долгота",
    "Погибло",
    "Ранено",
]


def get_dynamics_column_names() -> list[str]:
    """Названия колонок для Excel-файла очагов с динамикой."""
    return list(DYNAMICS_COLUMNS)


def get_dynamics_detail_column_names() -> list[str]:
    """Названия колонок для листа детализации с динамикой."""
    return list(DYNAMICS_DETAIL_COLUMNS)


def build_dynamics_excel_data(
    clusters: list[dict],
) -> list[dict[str, str]]:
    """
    Строит данные для Excel-файла очагов с исторической динамикой.

    Включает колонки: Статус, ДТП (пр. период), Изменение ДТП,
    Погибло/Ранено за прошлый период.
    Для исчезнувших очагов показывает данные прошлого периода.
    """
    rows = []

    for i, cluster in enumerate(clusters, start=1):
        dyn = cluster.get("dynamics", {})
        status = DYNAMICS_STATUS_LABELS.get(dyn.get("status", "new"), "?")
        is_lost = cluster.get("_is_lost", False)

        # Виды ДТП
        types_parts = [
            f"{t}: {c}" for t, c in cluster["type_counter"].items()
        ]
        types_str = "; ".join(types_parts)

        # Координаты: для lost показываем центр прошлого очага
        if is_lost:
            c = cluster.get("center")
            lat_str = f"{c[0]:.6f}" if c else ""
            lon_str = f"{c[1]:.6f}" if c else ""
        else:
            fc = cluster.get("first_coords")
            lat_str = f"{fc[0]:.6f}" if fc else ""
            lon_str = f"{fc[1]:.6f}" if fc else ""

        # Пикетаж
        start_pos, end_pos = _first_last_piketazh(cluster["cards"])
        start_str = _format_piketazh(start_pos)
        end_str = _format_piketazh(end_pos)

        # ДТП
        current_total = 0 if is_lost else cluster["total_accidents"]
        prev_total = dyn.get("prev_total")
        if prev_total is not None and not is_lost:
            delta = current_total - prev_total
            delta_str = f"{delta:+d}"
        elif is_lost and prev_total is not None:
            delta_str = f"-{prev_total}"
        else:
            delta_str = ""

        prev_total_str = str(prev_total) if prev_total is not None else ""

        # Даты
        dates = cluster.get("dates", [])
        first_date = dates[0] if dates else ""
        last_date = dates[-1] if dates else ""

        zone_label = ZONE_TYPE_LABELS.get(
            cluster["zone_type"], cluster["zone_type"],
        )

        rows.append({
            "№ очага": str(i),
            "Статус": status,
            "Тип зоны": zone_label,
            "Дорога/Улица": cluster["road"],
            "Пикетаж начало": start_str,
            "Пикетаж конец": end_str,
            "Широта": lat_str,
            "Долгота": lon_str,
            "Кол-во ДТП": str(current_total),
            "ДТП (пр. период)": prev_total_str,
            "Изменение ДТП": delta_str,
            "Виды ДТП (детализация)": types_str,
            "Доминирующий вид": cluster.get("dominant_type", ""),
            "Погибло": str(0 if is_lost else cluster["deaths"]),
            "Ранено": str(0 if is_lost else cluster["injured"]),
            "Погибло (пр. период)": str(dyn["prev_deaths"]) if dyn.get("prev_deaths") is not None else "",
            "Ранено (пр. период)": str(dyn["prev_injured"]) if dyn.get("prev_injured") is not None else "",
            "Дата первого ДТП": first_date,
            "Дата последнего ДТП": last_date,
        })

    return rows


def build_dynamics_detail_data(
    clusters: list[dict],
    current_label: str = "",
    prev_label: str = "",
) -> list[dict[str, str]]:
    """
    Строит данные для листа детализации с указанием периода и статуса.

    Для текущих очагов показывает ДТП текущего периода.
    Для исчезнувших очагов показывает ДТП прошлого периода
    с пометкой периода.
    """
    rows = []

    for i, cluster in enumerate(clusters, start=1):
        dyn = cluster.get("dynamics", {})
        status = DYNAMICS_STATUS_LABELS.get(dyn.get("status", "new"), "?")
        is_lost = cluster.get("_is_lost", False)
        period = prev_label if is_lost else current_label

        for card in cluster.get("cards", []):
            coords = _parse_coords(card)
            pos = _get_km_m(card)
            piketazh_str = _format_piketazh(pos)

            lat_str = f"{coords[0]:.6f}" if coords else ""
            lon_str = f"{coords[1]:.6f}" if coords else ""

            rows.append({
                "№ очага": str(i),
                "Статус": status,
                "Период": period,
                "Дата ДТП": _get_date(card),
                "Вид ДТП": _get_dtp_type(card),
                "Дорога/Улица": _get_road_name(card),
                "Пикетаж": piketazh_str,
                "Широта": lat_str,
                "Долгота": lon_str,
                "Погибло": str(_safe_int(card.get("pog"))),
                "Ранено": str(_safe_int(card.get("ran"))),
            })

    return rows


def build_dynamics_summary(clusters: list[dict]) -> dict:
    """
    Считает сводную статистику по динамике очагов.

    Returns:
        {
            "total": int,
            "new": int,
            "lost": int,
            "growing": int,
            "shrinking": int,
            "stable": int,
            "current_total_dtp": int,
            "prev_total_dtp": int,
        }
    """
    stats = {
        "total": len(clusters),
        "new": 0,
        "lost": 0,
        "growing": 0,
        "shrinking": 0,
        "stable": 0,
        "current_total_dtp": 0,
        "prev_total_dtp": 0,
    }

    for cluster in clusters:
        dyn = cluster.get("dynamics", {})
        status = dyn.get("status", "new")
        stats[status] = stats.get(status, 0) + 1

        if not cluster.get("_is_lost", False):
            stats["current_total_dtp"] += cluster["total_accidents"]

        prev_total = dyn.get("prev_total")
        if prev_total is not None:
            stats["prev_total_dtp"] += prev_total

    return stats

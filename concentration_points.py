"""
Модуль расчёта очагов концентрации ДТП (мест концентрации аварийности).

Два алгоритма:
  1. Населённые пункты (НП) — 3 прохода:
     - 1-й проход: перекрёстки (obj_dtp содержит «перекрёсток»), радиус 50 м
     - 2-й проход: дороги с наименованием и пикетажем, скользящее окно 200 м
     - 3-й проход: радиус 100 м от точки, с проверкой пикетажа:
       если центр ДТП и другое ДТП в радиусе 100 м имеют одинаковое
       наименование дороги и пикетаж, проверяется окно 200 м по пикетажу
     - Порог: 3+ ДТП одного вида ИЛИ 5+ ДТП любых видов
  2. Вне НП (автодороги):
     - Группировка по названию дороги
     - Скользящее окно 1 км
     - Порог: 3+ ДТП одного вида ИЛИ 5+ ДТП любых видов

Определение НП/не НП через Overpass API (OpenStreetMap).
"""

import math
import logging
from collections import Counter
from typing import Any, Callable, Awaitable

import httpx

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

# Окно для вне НП (км)
NON_SETTLEMENT_WINDOW_KM = 1.0

# Пороги
SAME_TYPE_THRESHOLD = 3   # 3+ ДТП одного вида = очаг
ANY_TYPE_THRESHOLD = 5    # 5+ ДТП любых видов = очаг

# Ключевые слова для определения перекрёстка
INTERSECTION_KEYWORDS = [
    "перекрёсток", "перекресток", "перекрёстка", "перекрестка",
]


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
    """Является ли место ДТП перекрёстком (по полю obj_dtp)."""
    dor_usl = card.get("dor_usl", {}) or {}
    obj_dtp_list = dor_usl.get("obj_dtp", []) or []
    if isinstance(obj_dtp_list, str):
        obj_dtp_list = [obj_dtp_list]
    obj_text = " ".join(str(o).lower() for o in obj_dtp_list)
    for keyword in INTERSECTION_KEYWORDS:
        if keyword in obj_text:
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
# OSM: Определение границ НП
# ========================

async def fetch_settlement_boundaries(
    cards: list[dict],
    progress_callback: Callable[[str], Awaitable[None]] | None = None,
) -> list[tuple[float, float, float, float]]:
    """
    Получает bounding boxes границ населённых пунктов через Overpass API.

    Returns:
        Список кортежей (lat_min, lon_min, lat_max, lon_max).
    """
    valid_coords = [_parse_coords(c) for c in cards]
    valid_coords = [c for c in valid_coords if c is not None]

    if not valid_coords:
        return []

    lats = [c[0] for c in valid_coords]
    lons = [c[1] for c in valid_coords]

    margin = 0.1  # ~11 км
    lat_min = max(min(lats) - margin, 41.0)
    lon_min = max(min(lons) - margin, 19.0)
    lat_max = min(max(lats) + margin, 70.0)
    lon_max = min(max(lons) + margin, 180.0)

    bbox = f"{lat_min},{lon_min},{lat_max},{lon_max}"

    query = (
        "[out:json][timeout:90];\n"
        "(\n"
        '  relation["place"~"city|town|village"](' + bbox + ');\n'
        '  way["place"~"city|town|village"](' + bbox + ');\n'
        ");\n"
        "out bb;\n"
    )

    # Список зеркал Overpass API с резервными серверами
    overpass_urls = [
        "https://overpass-api.de/api/interpreter",
        "https://lz4.overpass-api.de/api/interpreter",
        "https://z.overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
    ]

    headers = {
        "User-Agent": "GIBDD-DTP-Bot/1.0 (traffic-accident-analysis)",
        "Accept": "application/json",
    }

    if progress_callback:
        await progress_callback(
            f"Загрузка границ НП из OpenStreetMap...\n"
            f"BBOX: {bbox}"
        )

    for url in overpass_urls:
        try:
            logger.info(f"Попытка запроса к Overpass API: {url}")
            async with httpx.AsyncClient(
                verify=False,
                headers=headers,
            ) as client:
                resp = await client.post(
                    url, data={"data": query}, timeout=120,
                )
                resp.raise_for_status()
                data = resp.json()

            bboxes = []
            for element in data.get("elements", []):
                if "bounds" in element:
                    b = element["bounds"]
                    bboxes.append((
                        b["minlat"], b["minlon"],
                        b["maxlat"], b["maxlon"],
                    ))

            logger.info(
                f"Overpass API ({url}): {len(bboxes)} границ НП "
                f"для bbox {lat_min:.3f},{lon_min:.3f},{lat_max:.3f},{lon_max:.3f}"
            )
            return bboxes

        except httpx.HTTPStatusError as e:
            logger.warning(
                f"Overpass API ({url}): HTTP {e.response.status_code}"
            )
            continue
        except Exception as e:
            logger.warning(f"Overpass API ({url}): {e}")
            continue

    logger.error(
        "Все зеркала Overpass API недоступны. "
        "Не удалось получить границы НП."
    )
    return []


def _point_in_any_bbox(
    lat: float,
    lon: float,
    bboxes: list[tuple[float, float, float, float]],
) -> bool:
    """Попадает ли точка хотя бы в один bounding box."""
    for lat_min, lon_min, lat_max, lon_max in bboxes:
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            return True
    return False


def classify_cards(
    cards: list[dict],
    settlement_bboxes: list[tuple[float, float, float, float]],
) -> tuple[list[dict], list[dict]]:
    """
    Разделяет карточки на две группы: НП и вне НП.

    Returns:
        (settlement_cards, non_settlement_cards)
    """
    settlement_cards = []
    non_settlement_cards = []

    for card in cards:
        coords = _parse_coords(card)
        if coords is None:
            non_settlement_cards.append(card)
            continue
        if _point_in_any_bbox(coords[0], coords[1], settlement_bboxes):
            settlement_cards.append(card)
        else:
            non_settlement_cards.append(card)

    logger.info(
        f"Классификация: {len(settlement_cards)} в НП, "
        f"{len(non_settlement_cards)} вне НП "
        f"(всего {len(cards)})"
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

    1-й проход: перекрёстки (50 м)
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

    # --- 1-й проход: перекрёстки (50 м) ---
    for idx, card in indexed_with_coords:
        if idx in assigned:
            continue
        if not _is_intersection(card):
            continue

        candidates = [
            (j, c) for j, c in indexed_with_coords
            if j not in assigned
        ]
        group = _cluster_cards_by_radius(
            [(idx, card)] + [(j, c) for j, c in candidates if j != idx],
            SETTLEMENT_INTERSECTION_RADIUS_M,
            assigned,
        )

        if group is not None:
            assigned.update(group)
            group_cards = [cards[j] for j in group]
            group_cards.sort(key=lambda c: _get_date(c))
            center = _parse_coords(cards[idx])
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

        # Скользящее окно 1 км
        assigned: set[int] = set()

        for i, (card, pos, coords) in enumerate(cards_pos):
            if i in assigned:
                continue

            window_start = pos
            window_end = pos + NON_SETTLEMENT_WINDOW_KM

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
) -> list[dict]:
    """
    Главная функция: расчёт всех очагов концентрации ДТП.

    Args:
        cards: Список сырых карточек ДТП
        progress_callback: async-функция для обновления статуса

    Returns:
        Список словарей очагов
    """
    if not cards:
        return []

    # Шаг 1: Фильтр — только карточки с координатами
    cards_with_coords = [c for c in cards if _parse_coords(c)]
    no_coords = len(cards) - len(cards_with_coords)

    if no_coords > 0:
        logger.warning(f"{no_coords} карточек без координат пропущены")

    if not cards_with_coords:
        logger.warning("Нет карточек с координатами — расчёт невозможен")
        return []

    # Шаг 2: Границы НП из Overpass API
    settlement_bboxes = await fetch_settlement_boundaries(
        cards_with_coords, progress_callback,
    )

    if not settlement_bboxes:
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

    if settlement_bboxes:
        settlement_cards, non_settlement_cards = classify_cards(
            cards_with_coords, settlement_bboxes,
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

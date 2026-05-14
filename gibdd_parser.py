"""
Парсер данных ДТП с stat.gibdd.ru.

Превращает карточки ДТП из JSON API в структурированные данные
для генерации двух Excel-файлов:
  1. Один ДТП = одна строка (все поля карточки)
  2. Один участник = одна строка (с развёрткой по ТС и пешеходам)
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ========================
# Вспомогательные функции
# ========================

def _join(arr: Any, sep: str = "; ") -> str:
    """Склеивает список/значение в строку через разделитель."""
    if arr is None:
        return ""
    if isinstance(arr, list):
        return sep.join(str(item) for item in arr if item is not None and str(item).strip() != "")
    return str(arr)


def _safe_str(val: Any) -> str:
    """Безопасное приведение к строке."""
    if val is None:
        return ""
    return str(val).strip()


def _decimal_to_dms(decimal_str: str) -> tuple[str, str, str]:
    """
    Конвертирует десятичные координаты в градусы, минуты, секунды.

    Args:
        decimal_str: Строка с десятичными координатами (например, "53.346789")

    Returns:
        Кортеж (градусы, минуты, секунды) — строки
    """
    try:
        decimal = float(decimal_str)
        degrees = int(decimal)
        minutes_full = (decimal - degrees) * 60
        minutes = int(minutes_full)
        seconds = round((minutes_full - minutes) * 60, 2)
        return str(degrees), str(minutes), str(seconds)
    except (ValueError, TypeError):
        return "", "", ""


# ========================
# Файл 1: Одна строка = одно ДТП
# ========================

# Маппинг полей API → названия колонок Excel (файл 1)
FILE1_COLUMNS = [
    ("empt_number", "Номер ДТП"),
    ("date_dtp", "Дата ДТП"),
    ("time", "Время ДТП"),
    ("coord_w", "Широта"),
    ("coord_l", "Долгота"),
    ("dtpv", "Вид ДТП"),
    ("k_ts", "Количество ТС в ДТП"),
    ("k_uch", "Количество участников ДТП"),
    ("pog", "Число погибших в ДТП"),
    ("ran", "Число раненых в ДТП"),
    ("s_dtp", "Схема ДТП"),
    ("district", "Район"),
    ("house", "Дом"),
    ("km", "Километр"),
    ("m", "Метр"),
    ("np", "Населенный пункт"),
    ("street", "Улица"),
    ("dor", "Наименование дороги"),
    ("dor_z", "Значение дороги"),
    ("dor_k", "Категория дороги"),
    ("k_ul", "Категория улицы"),
    ("_sdor", "Дорожные условия"),
    ("_obj_dtp", "Объекты УДС на месте ДТП"),
    ("_ndu", "Недостатки транспортно-эксплуатационного содержания"),
    ("_factor", "Фактор режима движения"),
    ("_spog", "Состояние погоды"),
    ("s_pch", "Состояние проезжей части"),
    ("osv", "Освещение"),
    ("chom", "Изменения в режиме движения"),
    ("_ts_info", "Информация о ТС"),
    ("_n_ts", "Номер ТС"),
    ("ts_s", "Сведения об оставлении ТС места ДТП"),
    ("t_ts", "Тип ТС"),
    ("m_ts", "Модель ТС"),
    ("marka_ts", "Марка ТС"),
    ("color", "Цвет ТС"),
    ("_m_pov", "Места повреждения"),
    ("t_n", "Технические неисправности"),
    ("r_rul", "Расположение руля, тип привода"),
    ("g_v", "Год выпуска"),
    ("f_sob", "Форма собственности"),
    ("_ts_uch", "Инфо об участнике находящемся в ТС"),
    ("_uch_info", "Информация об участниках (без ТС)"),
    ("_n_uch", "Номер участника"),
    ("kt_uch", "Категория участника"),
    ("s_sm", "Сведения, скрылся ли участник с места ДТП"),
    ("pol", "Пол"),
    ("s_t", "Степень тяжести последствий"),
    ("_npdd", "Непосредственное нарушение ПДД"),
    ("_sop_npdd", "Сопутствующее нарушение ПДД"),
    ("safety_belt", "Сведения о пристегивании"),
    ("s_seat_group", "Тип детского удерживающего устройства"),
    ("alco", "Степень опьянения"),
    ("v_st", "Водительский стаж"),
]


def parse_card_to_row(card: dict[str, Any]) -> dict[str, str]:
    """
    Разбирает одну карточку ДТП в плоскую строку для Файла 1.

    Массивы (ТС, участники) склеиваются через ";".
    """
    row: dict[str, str] = {}

    # --- Поля верхнего уровня карточки (не вложенные) ---
    simple_fields = [
        "empt_number", "date_dtp", "time", "coord_w", "coord_l",
        "dtpv", "k_ts", "k_uch", "pog", "ran", "s_dtp",
        "district", "house", "km", "m", "np", "street",
        "dor", "dor_z", "dor_k", "k_ul",
    ]
    for field in simple_fields:
        row[field] = _safe_str(card.get(field, ""))

    # --- dor_usl (условия дороги) ---
    dor_usl = card.get("dor_usl", {}) or {}
    row["s_pch"] = _safe_str(dor_usl.get("s_pch", ""))
    row["osv"] = _safe_str(dor_usl.get("osv", ""))
    row["chom"] = _safe_str(dor_usl.get("chom", ""))
    row["_sdor"] = _join(dor_usl.get("sdor", []))
    row["_obj_dtp"] = _join(dor_usl.get("obj_dtp", []))
    row["_ndu"] = _join(dor_usl.get("ndu", []))
    row["_factor"] = _join(dor_usl.get("factor", []))
    row["_spog"] = _join(dor_usl.get("spog", []))

    # --- ts_info (транспортные средства) ---
    # Все поля ТС склеиваются через ";" (по одному значению на каждое ТС)
    ts_list = card.get("ts_info", []) or []

    row["_n_ts"] = _join([_safe_str(ts.get("n_ts", "")) for ts in ts_list])
    row["ts_s"] = _join([_safe_str(ts.get("ts_s", "")) for ts in ts_list])
    row["t_ts"] = _join([_safe_str(ts.get("t_ts", "")) for ts in ts_list])
    row["m_ts"] = _join([_safe_str(ts.get("m_ts", "")) for ts in ts_list])
    row["marka_ts"] = _join([_safe_str(ts.get("marka_ts", "")) for ts in ts_list])
    row["color"] = _join([_safe_str(ts.get("color", "")) for ts in ts_list])
    row["t_n"] = _join([_safe_str(ts.get("t_n", "")) for ts in ts_list])
    row["r_rul"] = _join([_safe_str(ts.get("r_rul", "")) for ts in ts_list])
    row["g_v"] = _join([_safe_str(ts.get("g_v", "")) for ts in ts_list])
    row["_m_pov"] = _join([_safe_str(ts.get("m_pov", "")) for ts in ts_list])
    # Форма собственности берём из o_pf
    row["f_sob"] = _join([_safe_str(ts.get("o_pf", "")) for ts in ts_list])

    # Собираем полную информацию по каждому ТС
    ts_parts = []
    for ts in ts_list:
        parts = [
            f"TC {_safe_str(ts.get('n_ts', ''))}: "
            f"Tip={_safe_str(ts.get('t_ts', ''))}, "
            f"Marka={_safe_str(ts.get('marka_ts', ''))}, "
            f"Model={_safe_str(ts.get('m_ts', ''))}, "
            f"Cvet={_safe_str(ts.get('color', ''))}, "
            f"God={_safe_str(ts.get('g_v', ''))}"
        ]
        ts_parts.append("; ".join(parts))
    row["_ts_info"] = "; ".join(ts_parts)

    # --- Участники в ТС (из ts_uch) ---
    all_ts_uch = []
    all_n_uch = []
    all_npdd = []
    all_sop_npdd = []
    all_kt_uch = []
    all_s_sm = []
    all_pol = []
    all_s_t = []
    all_safety_belt = []
    all_s_seat_group = []
    all_alco = []
    all_v_st = []

    for ts in ts_list:
        ts_uch_list = ts.get("ts_uch", []) or []
        for uch in ts_uch_list:
            all_ts_uch.append(_safe_str(uch.get("kt_uch", "")))
            all_n_uch.append(_safe_str(uch.get("n_uch", "")))
            all_npdd.append(_join(uch.get("npdd", [])))
            all_sop_npdd.append(_join(uch.get("sop_npdd", [])))
            all_kt_uch.append(_safe_str(uch.get("kt_uch", "")))
            all_s_sm.append(_safe_str(uch.get("s_sm", "")))
            all_pol.append(_safe_str(uch.get("pol", "")))
            all_s_t.append(_safe_str(uch.get("s_t", "")))
            all_safety_belt.append(_safe_str(uch.get("safety_belt", "")))
            all_s_seat_group.append(_safe_str(uch.get("s_seat_group", "")))
            all_alco.append(_safe_str(uch.get("alco", "")))
            all_v_st.append(_safe_str(uch.get("v_st", "")))

    row["_ts_uch"] = _join(all_ts_uch)
    row["_n_uch"] = _join(all_n_uch)
    row["_npdd"] = _join(all_npdd)
    row["_sop_npdd"] = _join(all_sop_npdd)

    # Поля участников — склеиваем значения всех участников через ";"
    row["kt_uch"] = _join(all_kt_uch)
    row["s_sm"] = _join(all_s_sm)
    row["pol"] = _join(all_pol)
    row["s_t"] = _join(all_s_t)
    row["safety_belt"] = _join(all_safety_belt)
    row["s_seat_group"] = _join(all_s_seat_group)
    row["alco"] = _join(all_alco)
    row["v_st"] = _join(all_v_st)

    # --- uch_info (участники без ТС: пешеходы и др.) ---
    uch_list = card.get("uch_info", []) or []

    # Добавляем участников из uch_info к общим спискам
    for uch in uch_list:
        all_kt_uch.append(_safe_str(uch.get("kt_uch", "")))
        all_pol.append(_safe_str(uch.get("pol", "")))
        all_s_t.append(_safe_str(uch.get("s_t", "")))
        all_alco.append(_safe_str(uch.get("alco", "")))
        all_npdd.append(_join(uch.get("npdd", [])))
        all_sop_npdd.append(_join(uch.get("sop_npdd", [])))
        all_s_sm.append(_safe_str(uch.get("s_sm", "")))
        all_n_uch.append(_safe_str(uch.get("n_uch", "")))

    # Пересчитываем с учётом uch_info
    row["kt_uch"] = _join(all_kt_uch)
    row["pol"] = _join(all_pol)
    row["s_t"] = _join(all_s_t)
    row["alco"] = _join(all_alco)
    row["s_sm"] = _join(all_s_sm)
    row["_n_uch"] = _join(all_n_uch)
    row["_npdd"] = _join(all_npdd)
    row["_sop_npdd"] = _join(all_sop_npdd)

    uch_info_parts = []
    for uch in uch_list:
        parts = [
            f"Uchastnik {_safe_str(uch.get('n_uch', ''))}: "
            f"Kategoriya={_safe_str(uch.get('kt_uch', ''))}, "
            f"Pol={_safe_str(uch.get('pol', ''))}, "
            f"Tyazhest={_safe_str(uch.get('s_t', ''))}"
        ]
        uch_info_parts.append("; ".join(parts))
    row["_uch_info"] = _join(uch_info_parts)

    return row


def build_file1_data(cards: list[dict[str, Any]]) -> list[dict[str, str]]:
    """
    Строит данные для Файла 1 (одна строка = одно ДТП).

    Returns:
        Список словарей {название_колонки: значение}
        Ключи совпадают с названиями колонок из get_file1_column_names()
    """
    # Строим маппинг: api_key -> column_name
    key_mapping = {api_key: col_name for api_key, col_name in FILE1_COLUMNS}

    rows = []
    for idx, card in enumerate(cards, start=1):
        raw_row = parse_card_to_row(card)

        # Пересобираем словарь с правильными ключами (названия колонок)
        row = {"№": str(idx)}
        for api_key, col_name in FILE1_COLUMNS:
            value = raw_row.get(api_key, "")
            row[col_name] = value

        rows.append(row)
    return rows


def get_file1_column_names() -> list[str]:
    """Возвращает названия колонок для Файла 1 в правильном порядке."""
    return ["№"] + [col_name for _, col_name in FILE1_COLUMNS]


# ========================
# Файл 2: Одна строка = один участник
# ========================

FILE2_COLUMNS = [
    "№",
    "Номер",
    "Дата",
    "Время",
    "Вид ДТП",
    "Место",
    "Улица",
    "Дом",
    "Дорога",
    "Километр",
    "Метр",
    "Долгота",
    "Долгота, градусы",
    "Долгота, минуты",
    "Долгота, секунды",
    "Широта",
    "Широта, градусы",
    "Широта, минуты",
    "Широта, секунды",
    "Погибло",
    "Погибло детей",
    "Ранено",
    "Ранено детей",
    "Подразделение, оформившее ДТП",
    "Недостатки экспл.состояния УДС",
    "Недостатки обустройства УДС",
    "НДУ",
    "Объекты УДС на месте",
    "Объекты УДС вблизи",
    "Факторы, влияющие на режим движения",
    "Состояние проезжей части",
    "Состояние погоды",
    "Освещение",
    "Является местом концентрации ДТП",
    "Дорога в плане",
    "Профиль дороги",
    "Количество полос",
    "Полоса, в которой произошло ДТП",
    "Ширина проезжей части",
    "Ширина обочины",
    "Ширина тротуара",
    "Ширина разделительной полосы",
    "Вид разделительной полосы",
    "Вид покрытия",
    "Категория дороги",
    "Значение дороги",
    "Номер СтатГИБДД",
    "Тип ТС",
    "Марка",
    "Модель",
    "Цвет",
    "Гос.номер",
    "Регион регистрации ТС",
    "Категория",
    "Пол",
    "Дата рождения",
    "Возраст",
    "Гражданство",
    "Место регистрации",
    "Место проживания",
    "Тяжесть последствий",
    "Соц.характеристика",
    "Непосредственные нарушения ПДД",
    "Сопутствующие нарушения ПДД",
    "Стаж(лет)",
    "Пристёгнут",
    "Возможность пристёгивания",
    "Прохождение МО",
    "Результат МО",
    "Вид опьянения",
    "Степень опьянения(‰)",
    "Степень опьянения(мкг/л)",
]


def _parse_participant(
    participant: dict[str, Any],
    card: dict[str, Any],
    vehicle: dict[str, Any] | None,
    row_number: int,
) -> dict[str, str]:
    """
    Формирует одну строку Файла 2 для одного участника ДТП.

    Args:
        participant: Данные участника (из ts_uch или uch_info)
        card: Полная карточка ДТП
        vehicle: Данные ТС (None для пешеходов из uch_info)
        row_number: Порядковый номер строки
    """
    row: dict[str, str] = {}

    # --- Общие поля ДТП ---
    row["№"] = str(row_number)
    row["Номер"] = _safe_str(card.get("empt_number", ""))
    row["Дата"] = _safe_str(card.get("date_dtp", ""))
    row["Время"] = _safe_str(card.get("time", ""))
    row["Вид ДТП"] = _safe_str(card.get("dtpv", ""))
    row["Место"] = _safe_str(card.get("district", ""))
    row["Улица"] = _safe_str(card.get("street", ""))
    row["Дом"] = _safe_str(card.get("house", ""))
    row["Дорога"] = _safe_str(card.get("dor", ""))
    row["Километр"] = _safe_str(card.get("km", ""))
    row["Метр"] = _safe_str(card.get("m", ""))

    # --- Координаты ---
    coord_l = _safe_str(card.get("coord_l", ""))
    coord_w = _safe_str(card.get("coord_w", ""))
    row["Долгота"] = coord_l
    lon_deg, lon_min, lon_sec = _decimal_to_dms(coord_l)
    row["Долгота, градусы"] = lon_deg
    row["Долгота, минуты"] = lon_min
    row["Долгота, секунды"] = lon_sec
    row["Широта"] = coord_w
    lat_deg, lat_min, lat_sec = _decimal_to_dms(coord_w)
    row["Широта, градусы"] = lat_deg
    row["Широта, минуты"] = lat_min
    row["Широта, секунды"] = lat_sec

    # --- Количество погибших/раненых ---
    row["Погибло"] = _safe_str(card.get("pog", ""))
    row["Погибло детей"] = ""
    row["Ранено"] = _safe_str(card.get("ran", ""))
    row["Ранено детей"] = ""

    # --- Дорожные условия ---
    dor_usl = card.get("dor_usl", {}) or {}
    row["Подразделение, оформившее ДТП"] = ""
    row["Недостатки экспл.состояния УДС"] = ""
    row["Недостатки обустройства УДС"] = ""
    row["НДУ"] = _join(dor_usl.get("ndu", []))
    row["Объекты УДС на месте"] = _join(dor_usl.get("sdor", []))
    row["Объекты УДС вблизи"] = _join(dor_usl.get("obj_dtp", []))
    row["Факторы, влияющие на режим движения"] = _join(dor_usl.get("factor", []))
    row["Состояние проезжей части"] = _safe_str(dor_usl.get("s_pch", ""))
    row["Состояние погоды"] = _join(dor_usl.get("spog", []))
    row["Освещение"] = _safe_str(dor_usl.get("osv", ""))

    # --- Нет на стат.гибдд.рф ---
    row["Является местом концентрации ДТП"] = ""
    row["Дорога в плане"] = ""
    row["Профиль дороги"] = ""
    row["Количество полос"] = ""
    row["Полоса, в которой произошло ДТП"] = ""
    row["Ширина проезжей части"] = ""
    row["Ширина обочины"] = ""
    row["Ширина тротуара"] = ""
    row["Ширина разделительной полосы"] = ""
    row["Вид разделительной полосы"] = ""
    row["Вид покрытия"] = ""

    # --- Категория и значение дороги ---
    row["Категория дороги"] = _safe_str(card.get("dor_k", ""))
    row["Значение дороги"] = _safe_str(card.get("dor_z", ""))
    row["Номер СтатГИБДД"] = _safe_str(card.get("empt_number", ""))

    # --- Данные ТС (если есть — для водителей и пассажиров) ---
    if vehicle is not None:
        row["Тип ТС"] = _safe_str(vehicle.get("t_ts", ""))
        row["Марка"] = _safe_str(vehicle.get("marka_ts", ""))
        row["Модель"] = _safe_str(vehicle.get("m_ts", ""))
        row["Цвет"] = _safe_str(vehicle.get("color", ""))
    else:
        row["Тип ТС"] = ""
        row["Марка"] = ""
        row["Модель"] = ""
        row["Цвет"] = ""

    row["Гос.номер"] = ""
    row["Регион регистрации ТС"] = ""

    # --- Данные участника ---
    row["Категория"] = _safe_str(participant.get("kt_uch", ""))
    row["Пол"] = _safe_str(participant.get("pol", ""))
    row["Дата рождения"] = ""
    row["Возраст"] = ""
    row["Гражданство"] = ""
    row["Место регистрации"] = ""
    row["Место проживания"] = ""
    row["Тяжесть последствий"] = _safe_str(participant.get("s_t", ""))
    row["Соц.характеристика"] = ""
    row["Непосредственные нарушения ПДД"] = _join(participant.get("npdd", []))
    row["Сопутствующие нарушения ПДД"] = _join(participant.get("sop_npdd", []))

    # --- Водительские данные (только для водителей) ---
    is_driver = _safe_str(participant.get("kt_uch", "")).lower() == "водитель"
    if is_driver:
        row["Стаж(лет)"] = _safe_str(participant.get("v_st", ""))
    else:
        row["Стаж(лет)"] = ""

    # --- Ремень безопасности (водители и пассажиры) ---
    if vehicle is not None:
        row["Пристёгнут"] = _safe_str(participant.get("safety_belt", ""))
    else:
        row["Пристёгнут"] = ""

    row["Возможность пристёгивания"] = ""
    row["Прохождение МО"] = ""

    # --- Результат МО (только для водителей) ---
    if is_driver:
        alco_val = _safe_str(participant.get("alco", ""))
        row["Результат МО"] = "да" if alco_val and alco_val != "00" else "нет"
        row["Степень опьянения(‰)"] = alco_val if alco_val and alco_val != "00" else ""
    else:
        row["Результат МО"] = ""
        row["Степень опьянения(‰)"] = ""

    row["Вид опьянения"] = ""
    row["Степень опьянения(мкг/л)"] = ""

    return row


def build_file2_data(cards: list[dict[str, Any]]) -> list[dict[str, str]]:
    """
    Строит данные для Файла 2 (одна строка = один участник ДТП).

    Для каждого ДТП создаётся строка для каждого участника:
      - Водители и пассажиры — из ts_info[i].ts_uch[j]
      - Пешеходы и прочие — из uch_info[k]

    Returns:
        Список словарей {название_колонки: значение}
    """
    rows: list[dict[str, str]] = []
    row_number = 1

    for card in cards:
        ts_list = card.get("ts_info", []) or []
        uch_list = card.get("uch_info", []) or []

        # Участники в ТС (водители, пассажиры)
        for vehicle in ts_list:
            ts_uch_list = vehicle.get("ts_uch", []) or []
            for participant in ts_uch_list:
                row = _parse_participant(
                    participant=participant,
                    card=card,
                    vehicle=vehicle,
                    row_number=row_number,
                )
                rows.append(row)
                row_number += 1

        # Участники без ТС (пешеходы и др.)
        for participant in uch_list:
            row = _parse_participant(
                participant=participant,
                card=card,
                vehicle=None,
                row_number=row_number,
            )
            rows.append(row)
            row_number += 1

    logger.info(f"Файл 2: {row_number - 1} строк участников")
    return rows


def get_file2_column_names() -> list[str]:
    """Возвращает названия колонок для Файла 2 в правильном порядке."""
    return list(FILE2_COLUMNS)

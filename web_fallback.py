"""
Запасной метод выгрузки данных ДТП через сайт stat.gibdd.ru.

Используется как fallback, когда API ГИБДД (opendataapi) недоступен (5xx).

Как работает:
  1. POST на /export/getCardsXML — генерация файла на сервере
  2. GET на /getFileById?data=<id> — скачивание XML
  3. Парсинг XML в формат, совместимый с extract_accident_cards() из API

Ограничения сайта:
  - Максимум 30 дней за один запрос
  - Код региона короткий (19), а не API-формат (1119)
"""

import asyncio
import logging
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Список базовых URL для web-fallback, перебираются по порядку.
# xn--80a7adb.xn--90adear.xn--p1ai — тот же домен, что использует API,
# поэтому с хостинга он гарантированно доступен (в отличие от stat.gibdd.ru).
WEB_FALLBACK_BASES = [
    "http://stat.gibdd.ru",
    "http://xn--80a7adb.xn--90adear.xn--p1ai",
]

# Таймауты для веб-запросов (сайт генерирует файлы медленнее API)
_WEB_CONNECT_TIMEOUT = 30
_WEB_READ_TIMEOUT = 120  # генерация файла может занимать время

# Задержка между запросами к сайту (не DDOS-ить)
_WEB_THROTTLE_SEC = 2

_last_web_request_time = 0.0


async def _web_throttle():
    """Минимальная пауза между запросами к сайту."""
    global _last_web_request_time
    import time
    now = time.monotonic()
    elapsed = now - _last_web_request_time
    if elapsed < _WEB_THROTTLE_SEC:
        await asyncio.sleep(_WEB_THROTTLE_SEC - elapsed)
    _last_web_request_time = time.monotonic()


def api_reg_to_web_reg(api_reg: str) -> str:
    """
    Конвертирует код региона из формата API в формат сайта.

    API использует 4-значные коды: 1119, 1122, 1101...
    Сайт использует короткие коды: 19, 22, 01...

    Правило: API код = "11" + двухзначный код сайта с ведущим нулём.
    Следовательно: сайт_код = API_код[2:].lstrip("0") или "0" если все нули.

    Examples:
        "1119" -> "19"
        "1122" -> "22"
        "1101" -> "01"
        "1182" -> "82"
    """
    short = api_reg[2:]  # убираем "11"
    # Сохраняем ведущий ноль если есть (01, 02...), но убираем лишние
    return short


def _split_period_to_intervals(
    start_date: date,
    end_date: date,
    max_days: int = 30,
) -> list[tuple[date, date]]:
    """
    Разбивает период на интервалы не более max_days.

    Сайт ГИБДД ограничивает один запрос 30 днями (проверка в JS:
    range >= -30 означает разница >= -30 дней).
    """
    intervals = []
    current = start_date
    while current <= end_date:
        interval_end = min(current + timedelta(days=max_days - 1), end_date)
        intervals.append((current, interval_end))
        current = interval_end + timedelta(days=1)
    return intervals


def _month_to_dat(month: int, year: int) -> str:
    """Конвертирует месяц и год в формат 'm.YYYY' для совместимости."""
    return f"{month}.{year}"


def _date_to_web_format(d: date) -> str:
    """Конвертирует дату в формат сайта: dd.mm.yy."""
    return d.strftime("%d.%m.%y")


async def _request_file_generation(
    client: httpx.AsyncClient,
    base_url: str,
    reg_web: str,
    date_st: str,
    date_end: str,
) -> str:
    """
    POST-запрос на генерацию файла карточек ДТП.

    Returns:
        ID файла для скачивания через getFileById.

    Raises:
        Exception: если сервер не вернул file_id.
    """
    url = f"{base_url}/export/getCardsXML"

    payload = {
        "date_st": date_st,
        "date_end": date_end,
        "ParReg": "877",
        "order": {"type": 1, "fieldName": "dat"},
        "reg": int(reg_web),
        "ind": 1,
        "exportType": 1,
    }

    await _web_throttle()

    logger.info(
        f"Web fallback: генерация файла "
        f"reg={reg_web}, {date_st} - {date_end}"
    )

    response = await client.post(
        url,
        json=payload,
        timeout=httpx.Timeout(_WEB_CONNECT_TIMEOUT, read=_WEB_READ_TIMEOUT),
    )

    if response.status_code != 200:
        raise Exception(
            f"Сайт ГИБДД вернул HTTP {response.status_code} "
            f"при генерации файла"
        )

    # Ответ: JSON с полем data (ID файла)
    try:
        result = response.json()
    except Exception:
        raise Exception(
            f"Сайт вернул не-JSON: {response.text[:200]}"
        )

    file_id = result.get("data") if isinstance(result, dict) else None

    if not file_id:
        # Пустой ответ = нет данных за период
        logger.info("Web fallback: сервер вернул пустой ответ (нет ДТП)")
        return ""

    return str(file_id)


async def _download_file(
    client: httpx.AsyncClient,
    base_url: str,
    file_id: str,
) -> bytes:
    """
    Скачивает сгенерированный файл по ID.

    Returns:
        Содержимое файла (XML bytes).
    """
    url = f"{base_url}/getFileById"
    params = {"data": file_id}

    await _web_throttle()

    response = await client.get(
        url,
        params=params,
        timeout=httpx.Timeout(_WEB_CONNECT_TIMEOUT, read=_WEB_READ_TIMEOUT),
    )

    if response.status_code != 200:
        raise Exception(
            f"Сайт ГИБДД вернул HTTP {response.status_code} "
            f"при скачивании файла (id={file_id})"
        )

    return response.content


def _parse_xml_cards(xml_bytes: bytes) -> list[dict[str, Any]]:
    """
    Парсит XML с карточками ДТП и конвертирует в формат API.

    XML структура (с сайта):
      <dtpCardList>
        <tab>
          <DTPV>Наезд на препятствие</DTPV>
          <date>30.06.2026</date>
          <time>20:50</time>
          <district>Череповецкий</district>
          <EMTP_NUMBER>190009147</EMTP_NUMBER>
          <infoDtp>
            <COORD_L>37.958488</COORD_L>
            <COORD_W>59.224894</COORD_W>
            <dor>А-114</dor>
            <dor_z>Федеральная</dor_z>
            <dor_k></dor_k>
            <k_ul>Вне НП</k_ul>
            <km>122</km>
            <m>480</m>
            <NP></NP>
            <street></street>
            <house></house>
            <s_dtp>940</s_dtp>
            <sdor>Перегон</sdor>
            <s_pch>Мокрое</s_pch>
            <osv>Сумерки</osv>
            <spog>Дождь</spog>
            <ndu>Не установлены</ndu>
            <OBJ_DTP>Перекрёсток</OBJ_DTP>
            <factor>Сведения отсутствуют</factor>
            <CHOM>Режим движения не изменялся</CHOM>
            <ts_info>
              <color>Синий</color>
              <marka_ts>FORD</marka_ts>
              <m_ts>Fusion</m_ts>
              <ts_uch>...</ts_uch>
            </ts_info>
          </infoDtp>
          <KTS>1</KTS>
          <KUCH>2</KUCH>
          <POG>0</POG>
          <RAN>1</RAN>
          <kartId>225722112</kartId>
        </tab>
        ...
      </dtpCardList>

    Конвертирует в формат API (для совместимости с gibdd_parser.py и
    concentration_points.py):
      {
        "date_dtp": "30.06.2026",
        "time": "20:50",
        "dtpv": "Наезд на препятствие",
        "coord_w": "59.224894",
        "coord_l": "37.958488",
        "district": "Череповецкий",
        "empt_number": "190009147",
        "dor": "А-114",
        "dor_z": "Федеральная",
        "dor_k": "",
        "k_ul": "Вне НП",
        "km": "122",
        "m": "480",
        "np": "",
        "street": "",
        "house": "",
        "s_dtp": "940",
        "k_ts": "1",
        "k_uch": "2",
        "pog": "0",
        "ran": "1",
        "dor_usl": {
            "sdor": ["Перегон"],
            "s_pch": "Мокрое",
            "osv": "Сумерки",
            "spog": ["Дождь"],
            "ndu": ["Не установлены"],
            "obj_dtp": ["Перекрёсток"],
            "factor": ["Сведения отсутствуют"],
            "chom": "Режим движения не изменялся",
        },
        "ts_info": [...],
      }
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        logger.error(f"Web fallback: ошибка парсинга XML: {e}")
        return []

    cards: list[dict[str, Any]] = []

    for tab in root.findall("tab"):
        info = tab.find("infoDtp")
        if info is None:
            info = ET.Element("infoDtp")  # пустой плейсхолдер

        def _text(parent, tag: str) -> str:
            """Безопасное извлечение текста элемента."""
            el = parent.find(tag) if parent is not None else None
            return (el.text or "").strip() if el is not None and el.text else ""

        # --- Поля верхнего уровня ---
        card: dict[str, Any] = {
            "date_dtp": _text(tab, "date"),
            "time": _text(tab, "time"),
            "dtpv": _text(tab, "DTPV"),
            "coord_w": _text(info, "COORD_W"),
            "coord_l": _text(info, "COORD_L"),
            "district": _text(tab, "district"),
            "empt_number": _text(tab, "EMTP_NUMBER"),
            "dor": _text(info, "dor"),
            "dor_z": _text(info, "dor_z"),
            "dor_k": _text(info, "dor_k"),
            "k_ul": _text(info, "k_ul"),
            "km": _text(info, "km"),
            "m": _text(info, "m"),
            "np": _text(info, "NP"),
            "street": _text(info, "street"),
            "house": _text(info, "house"),
            "s_dtp": _text(info, "s_dtp"),
            "k_ts": _text(tab, "KTS"),
            "k_uch": _text(tab, "KUCH"),
            "pog": _text(tab, "POG"),
            "ran": _text(tab, "RAN"),
        }

        # --- dor_usl (дорожные условия) ---
        # В XML — строки, в API — массивы. Оборачиваем в список.
        sdor_text = _text(info, "sdor")
        spog_text = _text(info, "spog")
        ndu_text = _text(info, "ndu")
        obj_dtp_text = _text(info, "OBJ_DTP")
        factor_text = _text(info, "factor")

        card["dor_usl"] = {
            "sdor": [sdor_text] if sdor_text else [],
            "s_pch": _text(info, "s_pch"),
            "osv": _text(info, "osv"),
            "spog": [spog_text] if spog_text else [],
            "ndu": [ndu_text] if ndu_text else [],
            "obj_dtp": [obj_dtp_text] if obj_dtp_text else [],
            "factor": [factor_text] if factor_text else [],
            "chom": _text(info, "CHOM"),
        }

        # --- ts_info (транспортные средства) ---
        ts_list = []
        for ts_info_el in info.findall("ts_info"):
            ts: dict[str, Any] = {
                "n_ts": _text(ts_info_el, "n_ts"),
                "ts_s": _text(ts_info_el, "ts_s"),
                "t_ts": _text(ts_info_el, "t_ts"),
                "m_ts": _text(ts_info_el, "m_ts"),
                "marka_ts": _text(ts_info_el, "marka_ts"),
                "color": _text(ts_info_el, "color"),
                "t_n": _text(ts_info_el, "t_n"),
                "r_rul": _text(ts_info_el, "r_rul"),
                "g_v": _text(ts_info_el, "g_v"),
                "m_pov": _text(ts_info_el, "m_pov"),
                "o_pf": _text(ts_info_el, "o_pf"),
            }

            # Участники внутри ТС
            ts_uch_list = []
            for uch_el in ts_info_el.findall("ts_uch"):
                # В XML поля NPDD и SOP_NPDD могут быть множественными
                npdd_vals = []
                sop_npdd_vals = []
                for npdd_el in uch_el.findall("NPDD"):
                    t = (npdd_el.text or "").strip()
                    if t:
                        npdd_vals.append(t)
                for sop_el in uch_el.findall("SOP_NPDD"):
                    t = (sop_el.text or "").strip()
                    if t:
                        sop_npdd_vals.append(t)

                uch: dict[str, Any] = {
                    "n_uch": _text(uch_el, "n_UCH"),
                    "kt_uch": _text(uch_el, "k_UCH"),
                    "npdd": npdd_vals if npdd_vals else [],
                    "sop_npdd": sop_npdd_vals if sop_npdd_vals else [],
                    "s_sm": _text(uch_el, "s_SM"),
                    "pol": _text(uch_el, "POL"),
                    "s_t": _text(uch_el, "s_T"),
                    "safety_belt": _text(uch_el, "SAFETY_BELT"),
                    "s_seat_group": _text(uch_el, "s_SEAT_GROUP"),
                    "alco": _text(uch_el, "ALCO"),
                    "v_st": _text(uch_el, "v_ST"),
                }
                ts_uch_list.append(uch)

            ts["ts_uch"] = ts_uch_list
            ts_list.append(ts)

        card["ts_info"] = ts_list
        cards.append(card)

    return cards


async def fetch_dtp_via_web(
    dat: str,
    reg_api: str,
    pok: str = "1",
) -> list[dict[str, Any]]:
    """
    Загружает карточки ДТП через сайт stat.gibdd.ru (fallback-метод).

    Формат входных параметров совместим с fetch_dtp_data() из api_client.py:
      dat: "m.YYYY" (например, "1.2026")
      reg_api: код региона в формате API (например, "1119")
      pok: показатель (используется только "1" — все ДТП)

    Returns:
        Список карточек ДТП в формате, совместимом с API.

    Raises:
        Exception: при ошибках сети или парсинга.
    """
    month = int(dat.split(".")[0])
    year = int(dat.split(".")[1])

    # Определяем границы месяца
    if month == 12:
        end_date = date(year, 12, 31)
    else:
        end_date = date(year, month + 1, 1) - timedelta(days=1)
    start_date = date(year, month, 1)

    reg_web = api_reg_to_web_reg(reg_api)

    # Разбиваем на интервалы ≤ 30 дней
    intervals = _split_period_to_intervals(start_date, end_date, max_days=30)

    all_cards: list[dict[str, Any]] = []

    async with httpx.AsyncClient() as client:
        for interval_start, interval_end in intervals:
            date_st = _date_to_web_format(interval_start)
            date_end = _date_to_web_format(interval_end)

            # Пробуем каждую базу по очереди, при ConnectTimeout — следующая
            base_error: Exception | None = None
            for base_url in WEB_FALLBACK_BASES:
                try:
                    # Шаг 1: генерация файла
                    file_id = await _request_file_generation(
                        client, base_url, reg_web, date_st, date_end
                    )

                    if not file_id:
                        # Нет данных за этот интервал — нормально
                        break

                    # Шаг 2: скачивание файла
                    xml_bytes = await _download_file(client, base_url, file_id)

                    # Шаг 3: парсинг XML
                    cards = _parse_xml_cards(xml_bytes)
                    all_cards.extend(cards)

                    logger.info(
                        f"Web fallback [{base_url}]: {date_st}-{date_end} -> "
                        f"{len(cards)} ДТП (file_id={file_id[:20]}...)"
                    )
                    base_error = None  # успех
                    break
                except (httpx.ConnectTimeout, httpx.ConnectError) as e:
                    base_error = e
                    logger.warning(
                        f"Web fallback: {base_url} недоступен "
                        f"({type(e).__name__}), пробую следующий..."
                    )
                    continue
                except Exception as e:
                    logger.error(
                        f"Web fallback: ошибка {date_st}-{date_end}: {e}"
                    )
                    raise

            if base_error is not None:
                logger.error(
                    f"Web fallback: все базы недоступны для {date_st}-{date_end}"
                )
                raise base_error

    logger.info(
        f"Web fallback: всего загружено {len(all_cards)} ДТП "
        f"за {dat}, рег={reg_api} (web_reg={reg_web})"
    )

    return all_cards


def _is_server_unreachable(e: Exception) -> bool:
    """Проверяет, указывает ли ошибка на полную недоступность сервера.

    Если сервер unreachable — нет смысла пробовать остальные месяцы.
    """
    import httpx
    # Непосредственная ошибка подключения
    if isinstance(e, (httpx.ConnectTimeout, httpx.ConnectError)):
        return True
    # Обёрнутая в ConnectionError (как в api_client._request_with_retries)
    cause = e.__cause__
    if cause is not None and isinstance(cause, (httpx.ConnectTimeout, httpx.ConnectError)):
        return True
    return False


def _is_server_error(e: Exception) -> bool:
    """Проверяет, является ли ошибка HTTP 5xx от сервера ГИБДД."""
    import httpx
    # Сайт вернул HTTP 500 и мы пробросили Exception с текстом
    if isinstance(e, Exception) and "HTTP 500" in str(e):
        return True
    if isinstance(e, httpx.HTTPStatusError) and e.response.status_code >= 500:
        return True
    return False


async def fetch_dtp_via_web_period(
    dat_list: list[str],
    reg_api: str,
    log_prefix: str = "Web fallback",
    progress_callback=None,
) -> tuple[list[dict], list[str]]:
    """
    Загружает карточки ДТП за список месяцев через сайт.

    Полный аналог _fetch_cards_for_period() из bot.py, но через сайт.

    Fail-fast: если первый запрос падает с ошибкой подключения
    (ConnectTimeout/ConnectError) или HTTP 500 — цикл прерывается,
    т.к. это означает полную недоступность сервера.
    """
    cards: list[dict] = []
    errors: list[str] = []
    consecutive_server_failures = 0
    _CONSECUTIVE_FAILURE_LIMIT = 2  # после N подряд идущих сбоев — стоп

    for i, dat in enumerate(dat_list, start=1):
        month_num = int(dat.split(".")[0])
        year = dat.split(".")[1]
        month_name = {
            1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
            5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
            9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
        }.get(month_num, dat)

        if progress_callback:
            await progress_callback(i, len(dat_list), month_name, year)

        try:
            extracted = await fetch_dtp_via_web(dat=dat, reg_api=reg_api)
            cards.extend(extracted)
            logger.info(f"  {log_prefix}: {dat} -> {len(extracted)} ДТП")
            # Успешный запрос — сбрасываем счётчик
            consecutive_server_failures = 0
        except Exception as e:
            from api_client import error_brief
            err_msg = f"{month_name} {year}: {error_brief(e)}"
            errors.append(err_msg)
            logger.error(
                f"  {log_prefix}: {dat} -> ОШИБКА [{type(e).__name__}] "
                f"{error_brief(e)}"
            )

            # Fail-fast: сервер полностью недоступен
            if _is_server_unreachable(e):
                logger.warning(
                    f"  {log_prefix}: сервер stat.gibdd.ru недоступен "
                    f"(ошибка подключения). Прерываю выгрузку остальных "
                    f"{len(dat_list) - i} месяцев."
                )
                break

            # Fail-fast: подряд идущие HTTP 500 (сервер падает)
            if _is_server_error(e):
                consecutive_server_failures += 1
                if consecutive_server_failures >= _CONSECUTIVE_FAILURE_LIMIT:
                    logger.warning(
                        f"  {log_prefix}: {consecutive_server_failures} "
                        f"подряд идущих HTTP-ошибок сервера. "
                        f"Прерываю выгрузку остальных "
                        f"{len(dat_list) - i} месяцев."
                    )
                    break
            else:
                # Другие ошибки (парсинг и т.д.) — тоже считаем как сбой сервера,
                # но только если первый запрос
                if i == 1 and len(cards) == 0:
                    logger.warning(
                        f"  {log_prefix}: ошибка при первом запросе. "
                        f"Прерываю выгрузку."
                    )
                    break

    return cards, errors
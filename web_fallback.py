"""
Запасной метод выгрузки данных ДТП через сайт stat.gibdd.ru.

Используется как fallback, когда API ГИБДД (opendataapi) недоступен (5xx).

Как работает:
  1. HTTP-запрос к главной странице для получения сессии (JSESSIONID)
  2. POST на /export/getCardsXML с параметрами выгрузки
  3. GET на /getFileById?data=<id> для скачивания ZIP-архива
  4. Распаковка ZIP и парсинг XML
  5. Конвертация в формат, совместимый с extract_accident_cards()

Требования:
  - httpx (уже есть в зависимостях)
  - Доступ к stat.gibdd.ru по HTTP

Ограничения:
  - Код региона короткий (19), а не API-формат (1119).
  - Максимум 31 день за один запрос (ограничение сервера).
  - Нужен User-Agent (WAF блокирует запросы без него).
"""

import io
import json
import logging
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Базовые URL для web-fallback (перебираются по порядку)
# Punycode-домен приоритетнее: он доступен с хостингов, блокирующих stat.gibdd.ru
_GIBDD_WEB_URLS = [
    "http://xn--80a7adb.xn--90adear.xn--p1ai",  # punycode (доступен с Amvera)
    "http://stat.gibdd.ru",                       # основное имя (может быть заблокировано хостингом)
]

# Таймауты
_WEB_CONNECT_TIMEOUT = 10  # секунд, подключение (короткий — быстро пробуем следующий домен)
_WEB_READ_TIMEOUT = 120    # секунд, чтение ответа / скачивание

# Заголовки, имитирующие браузер
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
}


def api_reg_to_web_reg(api_reg: str) -> str:
    """
    Конвертирует код региона из формата API в формат сайта.

    API использует 4-значные коды: 1119, 1122, 1101...
    Сайт использует короткие коды: 19, 22, 01...

    Правило: API код = "11" + двухзначный код сайта с ведущим нулём.

    Examples:
        "1119" -> "19"
        "1122" -> "22"
        "1101" -> "01"
        "1182" -> "82"
    """
    return api_reg[2:]  # убираем "11"


def _date_to_web_format(d: date) -> str:
    """Конвертирует дату в формат сайта: dd.mm.YYYY (4-значный год)."""
    return d.strftime("%d.%m.%Y")


def _split_period_to_intervals(
    start_date: date,
    end_date: date,
    max_days: int = 31,
) -> list[tuple[date, date]]:
    """
    Разбивает период на интервалы не более max_days.
    Сайт ГИБДД позволяет выгрузить до полного месяца (31 день).
    """
    intervals = []
    current = start_date
    while current <= end_date:
        interval_end = min(current + timedelta(days=max_days - 1), end_date)
        intervals.append((current, interval_end))
        current = interval_end + timedelta(days=1)
    return intervals


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
            ...
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
        </tab>
      </dtpCardList>

    Конвертирует в формат, совместимый с gibdd_parser.py и
    concentration_points.py.
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


def _extract_xml_from_zip(zip_bytes: bytes) -> bytes:
    """
    Извлекает XML из ZIP-архива, который возвращает сайт.

    Сайт всегда отдаёт ZIP с файлом «Карточки ДТП.xml» внутри.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        # Возможно, это голый XML (если формат ответа изменится)
        logger.warning("Web fallback: ответ не ZIP, пробуем как XML")
        return zip_bytes

    # Ищем XML-файл в архиве
    xml_names = [n for n in zf.namelist() if n.endswith(".xml")]
    if not xml_names:
        raise Exception(
            f"В ZIP-архиве нет XML-файлов: {zf.namelist()}"
        )

    xml_bytes = zf.read(xml_names[0])
    logger.debug(
        f"Web fallback: извлечён {xml_names[0]} "
        f"({len(xml_bytes)} байт) из ZIP"
    )
    return xml_bytes


# ---------------------------------------------------------------------------
# HTTP-based загрузка (без браузера, через httpx)
# ---------------------------------------------------------------------------

def _try_download_via_base(
    base_url: str,
    reg_web: str,
    date_st: str,
    date_end: str,
) -> list[dict[str, Any]]:
    """
    Пытается скачать карточки ДТП через один конкретный base_url.

    Raises:
        httpx.ConnectError / httpx.ConnectTimeout: если домен недоступен.
        Exception: при серверных ошибках (403, 500 и т.д.).
    """
    timeout = httpx.Timeout(
        connect=_WEB_CONNECT_TIMEOUT,
        read=_WEB_READ_TIMEOUT,
        write=30,
        pool=30,
    )

    with httpx.Client(base_url=base_url, timeout=timeout) as client:
        # Шаг 0: Получаем сессию (JSESSIONID)
        client.get("/", headers=_BROWSER_HEADERS)

        # Шаг 1: Запрашиваем генерацию файла
        inner = {
            "date_st": date_st,
            "date_end": date_end,
            "ParReg": "877",
            "order": {"type": 1, "fieldName": "dat"},
            "reg": [reg_web],
            "ind": "1",
            "exportType": 1,
        }

        resp = client.post(
            "/export/getCardsXML",
            headers={
                **_BROWSER_HEADERS,
                "Content-Type": "application/json; charset=utf-8",
                "Referer": f"{base_url}/",
            },
            json={"data": json.dumps(inner)},
        )

        if resp.status_code == 403:
            raise Exception("Доступ запрещён (403)")

        if resp.status_code != 200:
            raise Exception(f"HTTP {resp.status_code} при запросе выгрузки")

        try:
            file_id = resp.json().get("data", "")
        except Exception:
            raise Exception(
                f"Некорректный ответ сервера: {resp.text[:200]}"
            )

        if not file_id:
            logger.info(
                f"Web fallback: reg={reg_web}, "
                f"{date_st}-{date_end} -> нет данных (пустой ответ)"
            )
            return []

        # Шаг 2: Скачиваем файл
        logger.info(
            f"Web fallback: скачивание reg={reg_web}, "
            f"{date_st} - {date_end} (file_id={file_id})"
        )

        dl = client.get(
            f"/getFileById?data={file_id}",
            headers=_BROWSER_HEADERS,
        )

        if dl.status_code == 403:
            raise Exception("Доступ запрещён при скачивании (403)")

        if dl.status_code != 200:
            raise Exception(f"HTTP {dl.status_code} при скачивании файла")

        zip_bytes = dl.content
        logger.info(f"Web fallback: скачан файл ({len(zip_bytes)} байт)")

        # Шаг 3: Извлекаем XML из ZIP
        xml_bytes = _extract_xml_from_zip(zip_bytes)

        # Шаг 4: Парсим XML
        return _parse_xml_cards(xml_bytes)


def _download_cards_via_http(
    reg_web: str,
    date_st: str,
    date_end: str,
) -> list[dict[str, Any]]:
    """
    Скачивает карточки ДТП через HTTP к сайту ГИБДД.

    Перебирает несколько доменов по порядку (punycode → stat.gibdd.ru).
    Если первый домен недоступен (ConnectTimeout/ConnectError) —
    пробует следующий.

    Args:
        reg_web: код региона (например "19").
        date_st: начальная дата dd.mm.YYYY.
        date_end: конечная дата dd.mm.YYYY.

    Returns:
        Список карточек ДТП.

    Raises:
        Exception: если все домены недоступны или серверная ошибка.
    """
    last_exc: Exception | None = None

    for base_url in _GIBDD_WEB_URLS:
        try:
            cards = _try_download_via_base(
                base_url, reg_web, date_st, date_end,
            )
            if cards:
                logger.info(
                    f"Web fallback: успешно через {base_url}"
                )
            return cards
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            logger.warning(
                f"Web fallback: {base_url} недоступен, "
                f"пробую следующий домен..."
            )
            last_exc = e
            continue
        except Exception:
            # Серверная ошибка (403, 500 и т.д.) — не пробуем другой домен,
            # это одна и та же серверная инфраструктура
            raise

    # Ни один домен не доступен
    raise Exception(
        f"Все домены ГИБДД недоступны "
        f"(>{_WEB_CONNECT_TIMEOUT}с): "
        + ", ".join(_GIBDD_WEB_URLS)
    ) from last_exc


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
    import asyncio

    month = int(dat.split(".")[0])
    year = int(dat.split(".")[1])

    # Определяем границы месяца
    if month == 12:
        end_date = date(year, 12, 31)
    else:
        end_date = date(year, month + 1, 1) - timedelta(days=1)
    start_date = date(year, month, 1)

    reg_web = api_reg_to_web_reg(reg_api)

    # Разбиваем на интервалы (до 31 дня за запрос)
    intervals = _split_period_to_intervals(start_date, end_date)

    all_cards: list[dict[str, Any]] = []

    for interval_start, interval_end in intervals:
        date_st = _date_to_web_format(interval_start)
        date_end = _date_to_web_format(interval_end)

        # httpx работает синхронно — запускаем в потоке
        cards = await asyncio.to_thread(
            _download_cards_via_http,
            reg_web,
            date_st,
            date_end,
        )
        all_cards.extend(cards)

        logger.info(
            f"Web fallback: {date_st}-{date_end} -> "
            f"{len(cards)} ДТП"
        )

    logger.info(
        f"Web fallback: всего загружено {len(all_cards)} ДТП "
        f"за {dat}, рег={reg_api} (web_reg={reg_web})"
    )

    return all_cards



async def fetch_dtp_via_web_period(
    dat_list: list[str],
    reg_api: str,
    log_prefix: str = "Web fallback",
    progress_callback=None,
) -> tuple[list[dict], list[str]]:
    """
    Загружает карточки ДТП за список месяцев через сайт.

    Каждый месяц — до 3 попыток с задержкой 5/10/15с.
    Прерываем только при 3 подряд идущих неудачных месяцах
    (сервер точно лежит).
    """
    import asyncio as _asyncio

    cards: list[dict] = []
    errors: list[str] = []
    consecutive_failures = 0
    _CONSECUTIVE_FAILURE_LIMIT = 3
    _MONTH_RETRIES = 3
    _MONTH_RETRY_DELAYS = [5, 10, 15]  # секунд между попытками

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

        # Пробуем загрузить месяц с ретраями
        extracted = None
        last_err: Exception | None = None

        for attempt in range(1, _MONTH_RETRIES + 1):
            try:
                extracted = await fetch_dtp_via_web(dat=dat, reg_api=reg_api)
                break  # успех
            except Exception as e:
                last_err = e
                if attempt < _MONTH_RETRIES:
                    delay = _MONTH_RETRY_DELAYS[attempt - 1]
                    logger.warning(
                        f"  {log_prefix}: {dat} -> "
                        f"попытка {attempt}/{_MONTH_RETRIES} "
                        f"не удалась ({type(e).__name__}), "
                        f"повтор через {delay}с..."
                    )
                    await _asyncio.sleep(delay)

        if extracted is not None:
            cards.extend(extracted)
            logger.info(f"  {log_prefix}: {dat} -> {len(extracted)} ДТП")
            consecutive_failures = 0
        else:
            # Все попытки провалились
            try:
                from api_client import error_brief
                err_msg = f"{month_name} {year}: {error_brief(last_err)}"
            except ImportError:
                err_msg = f"{month_name} {year}: {last_err}"
            errors.append(err_msg)
            logger.error(
                f"  {log_prefix}: {dat} -> ОШИБКА после "
                f"{_MONTH_RETRIES} попыток: {err_msg}"
            )

            consecutive_failures += 1
            if consecutive_failures >= _CONSECUTIVE_FAILURE_LIMIT:
                logger.warning(
                    f"  {log_prefix}: {consecutive_failures} "
                    f"подряд идущих ошибок. "
                    f"Прерываю выгрузку остальных "
                    f"{len(dat_list) - i} месяцев."
                )
                break

    return cards, errors
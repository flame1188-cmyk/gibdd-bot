"""
Запасной метод выгрузки данных ДТП через сайт stat.gibdd.ru.

Используется как fallback, когда API ГИБДД (opendataapi) недоступен (5xx).

Как работает:
  1. Открывает сайт в headless-браузере (Playwright)
  2. Заполняет форму выгрузки (регион, даты, формат XML)
  3. Нажимает «Скачать» и перехватывает скачивание
  4. Распаковывает ZIP-архив и парсит XML
  5. Конвертирует в формат, совместимый с extract_accident_cards() из API

Важно:
  - Сайт защищён WAF, который блокирует прямые HTTP-запросы к /export/*.
  - Поэтому используется headless-браузер с антидетект-мерами.
  - Требуется установленный playwright (playwright install chromium).
  - Максимум 31 день за один запрос.

Ограничения:
  - Код региона короткий (19), а не API-формат (1119).
  - Работает только там, где доступен stat.gibdd.ru и установлен Chromium.
"""

import asyncio
import io
import json
import logging
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# Базовый URL для web-fallback
GIBDD_WEB_BASE = "http://stat.gibdd.ru"

# Таймауты
_PAGE_LOAD_TIMEOUT = 30_000   # мс, загрузка страницы
_FORM_INTERACT_TIMEOUT = 5   # секунд, ожидание между действиями
_DOWNLOAD_TIMEOUT = 120_000  # мс, ожидание скачивания файла


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
    """Конвертирует дату в формат сайта: dd.mm.yy."""
    return d.strftime("%d.%m.%y")


def _split_period_to_intervals(
    start_date: date,
    end_date: date,
    max_days: int = 30,
) -> list[tuple[date, date]]:
    """
    Разбивает период на интервалы не более max_days.
    Сайт ГИБДД ограничивает один запрос ~31 днем.
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
# Playwright-based загрузка (синхронная, вызывается из asyncio через to_thread)
# ---------------------------------------------------------------------------

def _download_cards_via_browser(
    reg_web: str,
    date_st: str,
    date_end: str,
) -> list[dict[str, Any]]:
    """
    Скачивает карточки ДТП через headless-браузер.

    Использует Playwright для обхода WAF сайта stat.gibdd.ru.
    Заполняет форму выгрузки и скачивает результат.

    Args:
        reg_web: код региона в формате сайта (строка, например "19").
        date_st: начальная дата в формате dd.mm.yy.
        date_end: конечная дата в формате dd.mm.yy.

    Returns:
        Список карточек ДТП.

    Raises:
        Exception: при ошибках браузера, формы или парсинга.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            accept_downloads=True,
        )
        # Антидетект: убираем маркеры headless-режима
        context.add_init_script(
            'Object.defineProperty(navigator, "webdriver", '
            '{get: () => undefined}); '
            'delete window.__playwright; '
            'delete window.__pw_manual;'
        )
        page = context.new_page()

        try:
            # 1. Загрузка главной страницы
            page.goto(
                GIBDD_WEB_BASE,
                wait_until="networkidle",
                timeout=_PAGE_LOAD_TIMEOUT,
            )
            time.sleep(_FORM_INTERACT_TIMEOUT)

            # 2. Переход на вкладку «Выгрузка»
            page.evaluate(
                '() => { document.getElementById("downloadAction").click(); }'
            )
            time.sleep(_FORM_INTERACT_TIMEOUT + 2)

            # 3. Открытие формы «Карточки ДТП»
            page.evaluate('''() => {
                document.querySelectorAll(".dui-links-list__title")
                    .forEach(e => {
                        if (e.textContent.trim() === "Карточки ДТП") e.click();
                    });
            }''')
            time.sleep(_FORM_INTERACT_TIMEOUT + 1)

            # 4. Заполнение формы
            _hide_loader_js = """\
                () => {\
                    const l = document.getElementById('jquery-loader-background');\
                    if (l) l.style.display = 'none';\
                }\
            """

            def _hl(page):
                page.evaluate(_hide_loader_js)

            # Тип: «Карточки ДТП» (type 41, а не «Список» type 31)
            _hl(page)
            page.evaluate('''() => {
                document.querySelectorAll(".dui-controls-btn")
                    .forEach(b => {
                        if (b.textContent.trim() === "Карточки ДТП") b.click();
                    });
            }''')
            _hl(page)
            time.sleep(1)

            # Формат: XML
            page.evaluate('''() => {
                document.querySelectorAll(".dui-controls-btn")
                    .forEach(b => {
                        if (b.textContent.trim() === "XML") b.click();
                    });
            }''')
            time.sleep(1)

            # Регион (строковый ID!)
            page.evaluate(
                '(reg) => { if (typeof regDDList !== "undefined") regDDList.setSelect([reg]); }',
                reg_web,
            )
            time.sleep(1)

            # Показатель: ДТП (value="1")
            page.evaluate('''() => {
                if (typeof pokList !== "undefined") pokList.setSelect(["1"]);
            }''')
            time.sleep(1)

            # Даты через jQuery datepicker (передаём как объект)
            d_st_parts = date_st.split(".")
            d_en_parts = date_end.split(".")
            page.evaluate(
                '''(d) => {
                    if (typeof duiDatePickerStart !== "undefined") {
                        $(duiDatePickerStart.datePickerInput).datepicker(
                            "setDate", new Date(d.sy, d.sm, d.sd)
                        );
                        duiDatePickerStart.datePickerInput.onchange();
                    }
                    if (typeof duiDatePickerEnd !== "undefined") {
                        $(duiDatePickerEnd.datePickerInput).datepicker(
                            "setDate", new Date(d.ey, d.em, d.ed)
                        );
                        duiDatePickerEnd.datePickerInput.onchange();
                    }
                }''',
                {
                    "sd": int(d_st_parts[0]),
                    "sm": int(d_st_parts[1]) - 1,
                    "sy": 2000 + int(d_st_parts[2]),
                    "ed": int(d_en_parts[0]),
                    "em": int(d_en_parts[1]) - 1,
                    "ey": 2000 + int(d_en_parts[2]),
                },
            )
            time.sleep(1)

            # Проверяем что кнопка активна
            btn_ok = page.evaluate('''() => {
                if (typeof downloadChecker === "function") downloadChecker(3);
                const btn = Array.from(document.querySelectorAll(".dui-controls-btn"))
                    .find(b => b.textContent.trim() === "Скачать");
                return btn && !btn.disabled;
            }''')

            if not btn_ok:
                raise Exception(
                    "Кнопка «Скачать» неактивна — форма не заполнена корректно"
                )

            # 5. Нажимаем «Скачать» и перехватываем скачивание
            logger.info(
                f"Web fallback: скачивание reg={reg_web}, "
                f"{date_st} - {date_end}"
            )

            with page.expect_download(timeout=_DOWNLOAD_TIMEOUT) as dl_info:
                page.evaluate('''() => {
                    const btn = Array.from(
                        document.querySelectorAll(".dui-controls-btn")
                    ).find(b => b.textContent.trim() === "Скачать");
                    if (btn) btn.click();
                }''')

            download = dl_info.value

            # 6. Сохраняем ZIP во временный файл и читаем
            import tempfile
            import os

            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                tmp_path = tmp.name

            try:
                download.save_as(tmp_path)
                with open(tmp_path, "rb") as f:
                    zip_bytes = f.read()
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            logger.info(
                f"Web fallback: скачан файл "
                f"{download.suggested_filename} ({len(zip_bytes)} байт)"
            )

            # 7. Извлекаем XML из ZIP
            xml_bytes = _extract_xml_from_zip(zip_bytes)

            # 8. Парсим XML
            cards = _parse_xml_cards(xml_bytes)
            return cards

        finally:
            browser.close()


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
        Exception: при ошибках браузера, сети или парсинга.
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

    for interval_start, interval_end in intervals:
        date_st = _date_to_web_format(interval_start)
        date_end = _date_to_web_format(interval_end)

        try:
            # Playwright работает синхронно — запускаем в потоке
            cards = await asyncio.to_thread(
                _download_cards_via_browser,
                reg_web,
                date_st,
                date_end,
            )
            all_cards.extend(cards)

            logger.info(
                f"Web fallback: {date_st}-{date_end} -> "
                f"{len(cards)} ДТП"
            )

        except Exception as e:
            logger.error(
                f"Web fallback: ошибка {date_st}-{date_end}: {e}"
            )
            raise

    logger.info(
        f"Web fallback: всего загружено {len(all_cards)} ДТП "
        f"за {dat}, рег={reg_api} (web_reg={reg_web})"
    )

    return all_cards


def _is_server_unreachable(e: Exception) -> bool:
    """Проверяет, указывает ли ошибка на полную недоступность.

    Для web-fallback через браузер это означает:
    - Ошибки Playwright (браузер не установлен, crash)
    - Ошибки подключения к сайту
    """
    msg = str(e).lower()
    # Playwright ошибки
    if "playwright" in msg or "chromium" in msg:
        return True
    # Таймауты
    if "timeout" in msg and ("navigate" in msg or "load" in msg):
        return True
    return False


def _is_server_error(e: Exception) -> bool:
    """Проверяет, является ли ошибка серверной (5xx)."""
    msg = str(e)
    if "HTTP 500" in msg or "HTTP 502" in msg or "HTTP 503" in msg:
        return True
    if "Ошибка на стороне сервера" in msg:
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
    или HTTP 500 — цикл прерывается.
    """
    cards: list[dict] = []
    errors: list[str] = []
    consecutive_server_failures = 0
    _CONSECUTIVE_FAILURE_LIMIT = 2

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
            consecutive_server_failures = 0
        except Exception as e:
            try:
                from api_client import error_brief
                err_msg = f"{month_name} {year}: {error_brief(e)}"
            except ImportError:
                err_msg = f"{month_name} {year}: {e}"
            errors.append(err_msg)
            logger.error(
                f"  {log_prefix}: {dat} -> ОШИБКА [{type(e).__name__}] "
                f"{err_msg}"
            )

            if _is_server_unreachable(e):
                logger.warning(
                    f"  {log_prefix}: сервер stat.gibdd.ru недоступен. "
                    f"Прерываю выгрузку остальных "
                    f"{len(dat_list) - i} месяцев."
                )
                break

            if _is_server_error(e):
                consecutive_server_failures += 1
                if consecutive_server_failures >= _CONSECUTIVE_FAILURE_LIMIT:
                    logger.warning(
                        f"  {log_prefix}: {consecutive_server_failures} "
                        f"подряд идущих ошибок сервера. "
                        f"Прерываю выгрузку."
                    )
                    break
            else:
                if i == 1 and len(cards) == 0:
                    logger.warning(
                        f"  {log_prefix}: ошибка при первом запросе. "
                        f"Прерываю выгрузку."
                    )
                    break

    return cards, errors
"""
Основной файл Telegram-бота для выгрузки данных ДТП с stat.gibdd.ru.

Поддерживает 3 способа ввода запроса:
  1. Inline-кнопки: /dtp → [Регион] → [Период]
  2. Естественный язык: "Вологодская область за 2025 год"
  3. Строгий формат: 2.2024 1101

Бот делает API-запросы к stat.gibdd.ru и возвращает 2 Excel-файла.

Запуск: python bot.py
"""

import asyncio
import html as html_mod
import logging
import os
import sys
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Conflict, NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import validate_config, ALLOWED_USER_IDS, LLM_API_KEY, ENABLE_NEWS_SEARCH

# ========================
# Утилита ретрая Telegram API
# ========================

_MAX_TG_RETRIES = 3
_TG_RETRY_DELAYS = [2, 5, 10]  # секунды между попытками


async def _tg_retry(coro_factory, description="Telegram API"):
    """Выполняет вызов Telegram API с ретраем при TimedOut/NetworkError.

    Args:
        coro_factory: Вызываемый объект (lambda/func), возвращающий корутину.
                      Создаёт новую корутину при каждой попытке.
        description: Описание вызова для логов.
    """
    last_exc = None
    for attempt in range(_MAX_TG_RETRIES):
        try:
            return await coro_factory()
        except (TimedOut, NetworkError) as exc:
            last_exc = exc
            if attempt < _MAX_TG_RETRIES - 1:
                delay = _TG_RETRY_DELAYS[attempt]
                logger.warning(
                    f"{description}: {exc.__class__.__name__}. "
                    f"Попытка {attempt + 1}/{_MAX_TG_RETRIES}, "
                    f"повтор через {delay} сек..."
                )
                await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


from api_client import fetch_dtp_data, fetch_regions, extract_accident_cards, error_brief, close_client
from llm_analyzer import close_llm_client
from gibdd_parser import build_file1_data, build_file2_data
from excel_generator import generate_both_files, generate_analytics_file, generate_concentration_file, generate_concentration_dynamics_file, generate_point_stats_file
from analytics import (
    calculate_metrics,
    compare_metrics,
    build_analytics_message,
    build_analytics_excel_data,
    get_analytics_column_names,
    extract_raw_supplement,
)
from llm_analyzer import get_ai_summary, get_ai_answer, format_clusters_for_prompt
from news_fetcher import fetch_news_context
from concentration_points import (
    calculate_concentration_points,
    calculate_concentration_dynamics,
    build_concentration_excel_data,
    build_concentration_detail_data,
    build_precluster_excel_data,
    build_dynamics_excel_data,
    build_dynamics_detail_data,
    build_dynamics_summary,
    get_concentration_column_names,
    get_detail_column_names,
    get_precluster_column_names,
    get_dynamics_column_names,
    get_dynamics_detail_column_names,
    enrich_clusters_with_cameras,
)
from point_statistics import (
    parse_coordinates,
    calculate_point_statistics,
    format_point_stats_message,
    build_point_stats_excel_data,
    get_point_stats_column_names,
    RADIUS_OPTIONS,
)
from user_request_parser import (
    parse_user_message,
    parse_period,
    find_region,
    ensure_regions_loaded,
    ParsedPeriod,
)

# ========================
# Кастомные фильтры
# ========================

class _IsDocument(filters.BaseFilter):
    """Фильтр для сообщений с прикреплённым документом (файлом).

    Используется вместо filters.Document, который в некоторых версиях
    python-telegram-bot разрешается в класс telegram.Document вместо фильтра.
    """
    def check_update(self, update):
        return bool(update.message and update.message.document is not None)


# ========================
# Настройка логирования
# ========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Rate-limit для Conflict-предупреждений (логируем не чаще 1 раза в 60с)
_conflict_last_log: float = 0.0
_CONFLICT_LOG_INTERVAL = 60.0

# Лимит символов в одном сообщении Telegram
TG_MSG_LIMIT = 4096


async def _send_long_message(
    bot,
    chat_id: int,
    text: str,
    parse_mode: str | None = None,
    reply_markup=None,
) -> None:
    """Отправляет текст, разбивая на части если он превышает TG_MSG_LIMIT.

    Разбивка происходит по границам абзацев (\\n\\n) для читаемости.
    reply_markup прикрепляется только к последнему сообщению.
    """
    if len(text) <= TG_MSG_LIMIT:
        await _tg_retry(lambda: bot.send_message(
            chat_id=chat_id, text=text,
            parse_mode=parse_mode, reply_markup=reply_markup,
        ), "send_message (короткое)")
        return

    # Разбиваем по двойным переносам строк
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""
    for p in paragraphs:
        candidate = current + ("\n\n" if current else "") + p
        if len(candidate) > TG_MSG_LIMIT and current:
            chunks.append(current)
            current = p
        else:
            current = candidate
    if current:
        chunks.append(current)

    for i, chunk in enumerate(chunks):
        is_last = (i == len(chunks) - 1)
        await _tg_retry(
            lambda c=chunk, m=parse_mode, r=reply_markup if is_last else None:
                bot.send_message(
                    chat_id=chat_id, text=c,
                    parse_mode=m, reply_markup=r,
                ),
            f"send_message (часть {i + 1}/{len(chunks)})",
        )


# ========================
# Константы
# ========================

REGIONS_PER_PAGE = 8  # Регионов на одной странице кнопок

MONTH_SHORT = {
    1: "Янв", 2: "Фев", 3: "Мар", 4: "Апр",
    5: "Май", 6: "Июн", 7: "Июл", 8: "Авг",
    9: "Сен", 10: "Окт", 11: "Ноя", 12: "Дек",
}

MONTH_FULL = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}

QUARTER_LABELS = {
    1: "I кв (Янв-Мар)", 2: "II кв (Апр-Июн)",
    3: "III кв (Июл-Сен)", 4: "IV кв (Окт-Дек)",
}


# ========================
# Вспомогательные функции
# ========================

def is_user_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


def _get_regions(context: ContextTypes.DEFAULT_TYPE) -> list[dict[str, str]]:
    """Возвращает список регионов из кэша в user_data."""
    return context.bot_data.get("regions", [])


async def _load_regions_if_needed(context: ContextTypes.DEFAULT_TYPE) -> list[dict[str, str]]:
    """Загружает справочник регионов, если ещё не загружен."""
    regions = _get_regions(context)
    if not regions:
        regions = await ensure_regions_loaded()
        context.bot_data["regions"] = regions
    return regions


async def _fetch_cards_for_period(
    dat_list: list[str],
    reg_code: str,
    log_prefix: str,
    progress_callback=None,
    notify_callback=None,
) -> tuple[list[dict], list[str]]:
    """Загружает карточки ДТП за список месяцев с GIBDD API.

    При получении 5xx от API автоматически переключается на запасной
    метод через сайт stat.gibdd.ru (web_fallback).

    Общая функция для аналитики, очагов и точечной статистики —
    устраняет дублирование одного и того же цикла в 3 местах.

    Args:
        dat_list: Список строк в формате "m.YYYY"
        reg_code: Код региона
        log_prefix: Префикс для логов (например "Аналитика", "Очаги")
        progress_callback: Опциональная async-функция(i, total, month_name, year)
                           для обновления статуса
        notify_callback: Опциональная async-функция(str) для одноразовых
                         уведомлений пользователю (например, о переключении
                         на запасной метод)

    Returns:
        (cards, errors) — список карточек ДТП и список строк-ошибок
    """
    import httpx as _httpx

    cards: list[dict] = []
    errors: list[str] = []
    use_web_fallback = False  # переключаемся после первой 5xx

    for i, dat in enumerate(dat_list, start=1):
        month_num = int(dat.split(".")[0])
        month_name = MONTH_FULL.get(month_num, dat)
        year = dat.split(".")[1]

        if progress_callback:
            await progress_callback(i, len(dat_list), month_name, year)

        if not use_web_fallback:
            # --- Основной метод: API ГИБДД ---
            try:
                api_response = await fetch_dtp_data(dat=dat, reg=reg_code, pok="1")
                extracted = extract_accident_cards(api_response)
                cards.extend(extracted)
                logger.info(f"  {log_prefix}: {dat} -> {len(extracted)} ДТП")
            except _httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status >= 500:
                    use_web_fallback = True
                    logger.warning(
                        f"  {log_prefix}: {dat} -> HTTP {status}, "
                        f"переключаюсь на запасной метод (сайт ГИБДД)"
                    )
                    if notify_callback:
                        try:
                            await notify_callback(
                                "\u26A0\uFE0F API ГИБДД недоступен (HTTP "
                                f"{status}).\n"
                                "Переключаюсь на запасной метод (сайт)..."
                            )
                        except Exception:
                            pass
                    remaining_dats = [dat] + dat_list[i:]
                    from web_fallback import fetch_dtp_via_web_period
                    fb_cards, fb_errors = await fetch_dtp_via_web_period(
                        remaining_dats, reg_code,
                        log_prefix=f"{log_prefix} [сайт]",
                        progress_callback=progress_callback,
                    )
                    cards.extend(fb_cards)
                    errors.extend(fb_errors)
                    break  # fallback обработал все оставшиеся месяцы
                else:
                    # Клиентская ошибка — не ретраим
                    err_msg = f"{month_name} {year}: {error_brief(e)}"
                    errors.append(err_msg)
                    logger.error(
                        f"  {log_prefix}: {dat} -> ОШИБКА "
                        f"[{type(e).__name__}] {error_brief(e)}"
                    )
            except ConnectionError as e:
                # Сетевая ошибка / таймаут — переключаемся на fallback
                use_web_fallback = True
                logger.warning(
                    f"  {log_prefix}: {dat} -> {error_brief(e)}, "
                    f"переключаюсь на запасной метод (сайт ГИБДД)"
                )
                if notify_callback:
                    try:
                        await notify_callback(
                            "\u26A0\uFE0F API ГИБДД недоступен "
                            f"({error_brief(e)}).\n"
                            "Переключаюсь на запасной метод (сайт)..."
                        )
                    except Exception:
                        pass
                remaining_dats = [dat] + dat_list[i:]
                from web_fallback import fetch_dtp_via_web_period
                fb_cards, fb_errors = await fetch_dtp_via_web_period(
                    remaining_dats, reg_code,
                    log_prefix=f"{log_prefix} [сайт]",
                    progress_callback=progress_callback,
                )
                cards.extend(fb_cards)
                errors.extend(fb_errors)
                break  # fallback обработал все оставшиеся месяцы
            except Exception as e:
                err_msg = f"{month_name} {year}: {error_brief(e)}"
                errors.append(err_msg)
                logger.error(
                    f"  {log_prefix}: {dat} -> ОШИБКА "
                    f"[{type(e).__name__}] {error_brief(e)}"
                )

    return cards, errors


# ========================
# Построение клавиатур
# ========================

def build_region_keyboard(
    regions: list[dict[str, str]],
    page: int = 0,
) -> InlineKeyboardMarkup:
    """Строит клавиатуру выбора региона с пагинацией."""
    total = len(regions)
    total_pages = max(1, (total + REGIONS_PER_PAGE - 1) // REGIONS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start = page * REGIONS_PER_PAGE
    end = min(start + REGIONS_PER_PAGE, total)
    page_regions = regions[start:end]

    buttons = []

    # Кнопки регионов
    for r in page_regions:
        # Короткая метка: название + код
        label = r["name"]
        if len(label) > 35:
            label = label[:33] + ".."
        buttons.append([InlineKeyboardButton(
            f"{label} ({r['code']})",
            callback_data=f"r:{r['code']}",
        )])

    # Навигация
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("<< Назад", callback_data=f"rp:{page - 1}"))
    nav_row.append(InlineKeyboardButton(
        f"{page + 1}/{total_pages}",
        callback_data="rp:noop",
    ))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Вперёд >>", callback_data=f"rp:{page + 1}"))
    buttons.append(nav_row)

    buttons.append([InlineKeyboardButton("Отмена", callback_data="cancel")])

    return InlineKeyboardMarkup(buttons)


def build_period_keyboard(year: int) -> InlineKeyboardMarkup:
    """Строит клавиатуру выбора периода."""
    buttons = []

    # Строка 1: годовые периоды
    buttons.append([
        InlineKeyboardButton(f"Весь {year} год", callback_data=f"py:{year}"),
        InlineKeyboardButton("Полугодие 1", callback_data=f"ph:1:{year}"),
        InlineKeyboardButton("Полугодие 2", callback_data=f"ph:2:{year}"),
    ])

    # Строка 2: кварталы
    buttons.append([
        InlineKeyboardButton(f"I кв", callback_data=f"pq:1:{year}"),
        InlineKeyboardButton(f"II кв", callback_data=f"pq:2:{year}"),
        InlineKeyboardButton(f"III кв", callback_data=f"pq:3:{year}"),
        InlineKeyboardButton(f"IV кв", callback_data=f"pq:4:{year}"),
    ])

    # Строка 3: произвольное количество месяцев
    buttons.append([
        InlineKeyboardButton(f"За 2 мес", callback_data=f"pn:2:{year}"),
        InlineKeyboardButton(f"За 4 мес", callback_data=f"pn:4:{year}"),
        InlineKeyboardButton(f"За 5 мес", callback_data=f"pn:5:{year}"),
        InlineKeyboardButton(f"За 7 мес", callback_data=f"pn:7:{year}"),
    ])
    buttons.append([
        InlineKeyboardButton(f"За 8 мес", callback_data=f"pn:8:{year}"),
        InlineKeyboardButton(f"За 9 мес", callback_data=f"pn:9:{year}"),
        InlineKeyboardButton(f"За 10 мес", callback_data=f"pn:10:{year}"),
        InlineKeyboardButton(f"За 11 мес", callback_data=f"pn:11:{year}"),
    ])

    # Строки 5-6: месяцы (по 6 в строке)
    for row_start in (1, 7):
        row = []
        for m in range(row_start, row_start + 6):
            row.append(InlineKeyboardButton(
                MONTH_SHORT[m], callback_data=f"pm:{m}:{year}",
            ))
        buttons.append(row)

    # Навигация по годам
    buttons.append([
        InlineKeyboardButton(f"<< {year - 1}", callback_data=f"yy:{year - 1}"),
        InlineKeyboardButton(str(year), callback_data="yy:noop"),
        InlineKeyboardButton(f"{year + 1} >>", callback_data=f"yy:{year + 1}"),
    ])

    # Кнопка «Назад»
    buttons.append([InlineKeyboardButton("<< Назад к регионам", callback_data="back")])
    buttons.append([InlineKeyboardButton("Отмена", callback_data="cancel")])

    return InlineKeyboardMarkup(buttons)


# ========================
# Обработчики команд
# ========================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_user_allowed(update.effective_user.id):
        await update.message.reply_text("У вас нет доступа к этому боту.")
        return

    await update.message.reply_text(
        "Привет! Я бот для выгрузки данных ДТП с stat.gibdd.ru.\n\n"
        "Способы запроса:\n\n"
        "1. Кнопки: /dtp — выберите регион и период\n\n"
        "2. Текстом (примеры):\n"
        "   Вологодская область за 2025 год\n"
        "   Вологодская за 3 месяца 2026\n"
        "   март 2025 Алтайский край\n"
        "   за I квартал 2025 Москва\n"
        "   2.2024 1101\n\n"
        "Результат: 2 Excel-файла\n"
        "  1. Карточки ДТП (1 строка = 1 ДТП)\n"
        "  2. Участники ДТП (1 строка = 1 участник)\n\n"
        "После выгрузки бот предложит:\n"
        "\U0001F4CA Анализ — сравнение с прошлым годом\n"
        "\U0001F916 Анализ с ИИ — анализ + резюме нейросети\n"
        "\U0001F525 Очаги ДТП — места концентрации аварийности\n\n"
        "Команды:\n"
        "/dtp — начать выгрузку через кнопки\n"
        "/help — справка\n"
        "/regions — список регионов"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_user_allowed(update.effective_user.id):
        return

    await update.message.reply_text(
        "Справка по использованию бота\n\n"
        "--- Способ 1: Кнопки ---\n"
        "/dtp → выберите регион → выберите период\n\n"
        "--- Способ 2: Текстом ---\n"
        "Напишите запрос на русском, например:\n"
        "  Вологодская область за 2025 год\n"
        "  Алтайский край за 3 месяца 2026\n"
        "  март 2025 Вологодская\n"
        "  за I квартал 2025 Татарстан\n"
        "  за полугодие 2025 Москва\n\n"
        "--- Способ 3: Строгий формат ---\n"
        "  2.2024 1101  (месяц.год код_региона)\n\n"
        "--- Аналитика ---\n"
        "После выгрузки данных бот предложит:\n\n"
        "\U0001F4CA <b>Анализ</b> — математическое сравнение\n"
        "текущего периода с аналогичным периодом\n"
        "прошлого года. Результат: текстовое резюме + Excel-файл.\n\n"
        "\U0001F916 <b>Анализ с ИИ</b> — то же самое +\n"
        "текстовое резюме от нейросети GLM\n"
        "и возможность задавать вопросы по данным.\n\n"
        "\U0001F525 <b>Очаги ДТП</b> — выявление мест\n"
        "концентрации аварийности (перекрёстки, участки дорог).\n"
        "Результат: Excel-файл с описанием очагов и подробностями.\n\n"
        "--- Команды ---\n"
        "/dtp — выгрузка через кнопки\n"
        "/regions — список регионов\n"
        "/help — эта справка\n\n"
        "--- Результат выгрузки ---\n"
        "Бот вернёт 2 Excel-файла:\n"
        "  1. dtp_cards.xlsx — карточки ДТП\n"
        "  2. dtp_uch.xlsx — участники ДТП\n\n"
        "--- Контакты ---\n"
        "Вопросы и предложения по работе бота:\n"
        "@flame1290 и @Julich_Vorobevich",
        parse_mode="HTML",
    )


async def cmd_dtp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /dtp — начало интерактивной выгрузки через кнопки."""
    if not is_user_allowed(update.effective_user.id):
        return

    await _show_region_keyboard(update, context, page=0)


async def _show_region_keyboard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int = 0,
    edit_message: bool = False,
) -> None:
    """Показывает клавиатуру выбора региона."""
    msg = await update.message.reply_text("Загружаю список регионов...") if not edit_message else None

    regions = await _load_regions_if_needed(context)
    if not regions:
        text = (
            "Не удалось загрузить список регионов.\n\n"
            "Сервер ГИБДД недоступен, а локальный кэш пуст.\n\n"
            "Возможные действия:\n"
            "• Подождите и попробуйте позже\n"
            "• Используйте текстовый формат:\n"
            "  <code>месяц.год код_региона</code>\n"
            "  Например: <code>6.2026 1119</code>"
        )
        if msg:
            await msg.edit_text(text, parse_mode="HTML")
        else:
            await update.callback_query.edit_message_text(text, parse_mode="HTML")
        return

    keyboard = build_region_keyboard(regions, page)
    text = "Выберите регион:"

    if edit_message and update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=keyboard,
            )
        except Exception:
            await update.callback_query.message.reply_text(text, reply_markup=keyboard)
    else:
        await msg.edit_text(text, reply_markup=keyboard)


async def cmd_regions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /regions — выводит список регионов текстом."""
    if not is_user_allowed(update.effective_user.id):
        return

    msg = await update.message.reply_text("Загружаю список регионов...")

    regions = await _load_regions_if_needed(context)
    if not regions:
        await msg.edit_text("Не удалось загрузить список регионов.")
        return

    lines = [f"<b>Код — Регион</b> ({len(regions)} всего)\n"]
    for r in regions:
        lines.append(f"<code>{r['code']}</code> — {r['name']}")

    # Отправляем частями
    chunk_size = 40
    for i in range(0, len(lines), chunk_size):
        chunk = lines[i:i + chunk_size]
        text = "\n".join(chunk)
        await update.message.reply_text(text, parse_mode="HTML")

    await msg.delete()


# ========================
# Обработчики callback (нажатия кнопок)
# ========================

async def on_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Главный диспетчер callback-запросов от inline-кнопок."""
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        await query.edit_message_text("У вас нет доступа к этому боту.")
        return

    data = query.data

    try:
        # --- Навигация по страницам регионов ---
        if data.startswith("rp:"):
            parts = data.split(":")
            if parts[1] != "noop":
                page = int(parts[1])
                regions = _get_regions(context)
                keyboard = build_region_keyboard(regions, page)
                await query.edit_message_text(
                    "Выберите регион:", reply_markup=keyboard,
                )
            return

        # --- Выбор региона ---
        if data.startswith("r:"):
            reg_code = data[2:]
            regions = _get_regions(context)
            reg_name = "Регион " + reg_code
            for r in regions:
                if r["code"] == reg_code:
                    reg_name = r["name"]
                    break

            context.user_data["reg_code"] = reg_code
            context.user_data["reg_name"] = reg_name

            # Показываем клавиатуру выбора периода
            current_year = datetime.now().year
            context.user_data["sel_year"] = current_year
            keyboard = build_period_keyboard(current_year)

            await query.edit_message_text(
                f"Регион: {reg_name}\n\n"
                f"Выберите период:",
                reply_markup=keyboard,
            )
            return

        # --- Выбор периода: Весь год ---
        if data.startswith("py:"):
            year = int(data[3:])
            period = ParsedPeriod(
                months=list(range(1, 13)),
                year=year,
                label=f"Весь {year} год",
            )
            await _start_fetching(query, context, period)
            return

        # --- Выбор периода: Квартал ---
        if data.startswith("pq:"):
            parts = data.split(":")
            q = int(parts[1])
            year = int(parts[2])
            start = (q - 1) * 3 + 1
            end = start + 2
            period = ParsedPeriod(
                months=list(range(start, end + 1)),
                year=year,
                label=f"{['I','II','III','IV'][q-1]} квартал {year} "
                      f"({MONTH_SHORT[start]}-{MONTH_SHORT[end]})",
            )
            await _start_fetching(query, context, period)
            return

        # --- Выбор периода: Полугодие ---
        if data.startswith("ph:"):
            parts = data.split(":")
            half = int(parts[1])
            year = int(parts[2])
            if half == 1:
                months = list(range(1, 7))
                label = f"Полугодие 1 {year} (Янв-Июн)"
            else:
                months = list(range(7, 13))
                label = f"Полугодие 2 {year} (Июл-Дек)"
            period = ParsedPeriod(months=months, year=year, label=label)
            await _start_fetching(query, context, period)
            return

        # --- Выбор периода: 9 месяцев ---
        if data.startswith("p9:"):
            year = int(data[3:])
            period = ParsedPeriod(
                months=list(range(1, 10)),
                year=year,
                label=f"9 месяцев {year} (Янв-Сен)",
            )
            await _start_fetching(query, context, period)
            return

        # --- Выбор периода: Произвольное количество месяцев ---
        if data.startswith("pn:"):
            parts = data.split(":")
            n = int(parts[1])
            year = int(parts[2])
            months = list(range(1, n + 1))
            label = f"За {n} мес. {year} ({MONTH_SHORT[1]}-{MONTH_SHORT[n]})"
            period = ParsedPeriod(months=months, year=year, label=label)
            await _start_fetching(query, context, period)
            return

        # --- Выбор периода: Конкретный месяц ---
        if data.startswith("pm:"):
            parts = data.split(":")
            month = int(parts[1])
            year = int(parts[2])
            period = ParsedPeriod(
                months=[month],
                year=year,
                label=f"{MONTH_FULL.get(month, '')} {year}",
            )
            await _start_fetching(query, context, period)
            return

        # --- Навигация по годам ---
        if data.startswith("yy:"):
            parts = data.split(":")
            if parts[1] != "noop":
                year = int(parts[1])
                context.user_data["sel_year"] = year
                keyboard = build_period_keyboard(year)
                reg_name = context.user_data.get("reg_name", "")
                await query.edit_message_text(
                    f"Регион: {reg_name}\n\n"
                    f"Выберите период:",
                    reply_markup=keyboard,
                )
            return

        # --- Назад к регионам ---
        if data == "back":
            context.user_data.pop("reg_code", None)
            context.user_data.pop("reg_name", None)
            regions = _get_regions(context)
            keyboard = build_region_keyboard(regions, page=0)
            await query.edit_message_text(
                "Выберите регион:", reply_markup=keyboard,
            )
            return

        # --- Запрос аналитики (без ИИ) ---
        if data == "do_analytics":
            await _run_analysis(update, context, use_llm=False)
            return

        # --- Запрос аналитики (с ИИ) ---
        if data == "do_analytics_ai":
            await _run_analysis(update, context, use_llm=True)
            return

        # --- Расчёт очагов ДТП ---
        if data == "do_concentration":
            # Проверяем, есть ли камеры в кэше для этого региона
            reg_code = (
                context.user_data.get("concentration_reg_code", "")
                or context.user_data.get("reg_code", "")
                or context.user_data.get("analytics_reg_code", "")
            )
            # Запоминаем код региона для последующей загрузки файла камер
            if reg_code:
                context.user_data["concentration_reg_code"] = reg_code
            from camera_cache import has_cached_cameras, load_cameras_from_cache

            cached_cameras = None
            if reg_code and has_cached_cameras(reg_code):
                cached_cameras = load_cameras_from_cache(reg_code)

            if cached_cameras:
                # Камеры в кэше — предлагаем использовать их или загрузить новые
                with_pk = sum(1 for c in cached_cameras if c["has_piket"])
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            f"\U0001F4F7 Использовать сохранённые ({len(cached_cameras)} камер)",
                            callback_data="cam_use_cached",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "\U0001F4E4 Загрузить новый файл",
                            callback_data="cam_ask_upload",
                        ),
                        InlineKeyboardButton(
                            "\u27A1 Без камер",
                            callback_data="cam_skip",
                        ),
                    ],
                ])
                await query.edit_message_text(
                    "\U0001F525 <b>Очаги ДТП</b>\n\n"
                    f"Для региона <b>{reg_code}</b> найден сохранённый файл камер:\n"
                    f"  \u2022 Всего: {len(cached_cameras)}\n"
                    f"  \u2022 С пикетажем: {with_pk}\n\n"
                    "Использовать его или загрузить новый?",
                    reply_markup=keyboard,
                    parse_mode="HTML",
                )
            else:
                # Камер в кэше нет — просим загрузить
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "\U0001F4F7 Загрузить камеры",
                            callback_data="cam_ask_upload",
                        ),
                        InlineKeyboardButton(
                            "\u27A1 Без камер",
                            callback_data="cam_skip",
                        ),
                    ],
                ])
                await query.edit_message_text(
                    "\U0001F525 <b>Очаги ДТП</b>\n\n"
                    "Загрузите файл с камерами фотовидеофиксации\n"
                    "(gibddrf_cameras_change_*.xls)\n"
                    "или продолжите без камер.",
                    reply_markup=keyboard,
                    parse_mode="HTML",
                )
            return

        # --- Камеры: пропустить ---
        if data == "cam_skip":
            context.user_data.pop("cameras_data", None)
            await query.edit_message_text(
                "\U0001F525 Запуск расчёта очагов (без камер)..."
            )
            await _run_concentration_points(update, context)
            return

        # --- Камеры: использовать сохранённые из кэша ---
        if data == "cam_use_cached":
            from camera_cache import load_cameras_from_cache
            reg_code = (
                context.user_data.get("concentration_reg_code", "")
                or context.user_data.get("reg_code", "")
                or context.user_data.get("analytics_reg_code", "")
            )
            cameras = load_cameras_from_cache(reg_code) if reg_code else None
            if cameras:
                context.user_data["cameras_data"] = cameras
                await query.edit_message_text(
                    f"\U0001F525 Запуск расчёта очагов "
                    f"(с сохранёнными камерами: {len(cameras)})..."
                )
                await _run_concentration_points(update, context)
            else:
                await query.edit_message_text(
                    "\u26A0\uFE0F Файл камер не найден. Загрузите заново."
                )
            return

        # --- Камеры: запрос загрузки ---
        if data == "cam_ask_upload":
            context.user_data["waiting_camera_file"] = True
            await query.edit_message_text(
                "\U0001F4F7 <b>Загрузка камер</b>\n\n"
                "Отправьте Excel-файл с камерами\n"
                "(gibddrf_cameras_change_*.xlsx)\n\n"
                "Или нажмите \u274C чтобы пропустить.",
                parse_mode="HTML",
            )
            return

        # --- Статистика по точке ---
        if data == "do_point_stats":
            await _start_point_stats(update, context)
            return

        # --- Смена радиуса статистики по точке ---
        if data.startswith("ps_radius:"):
            radius_m = int(data.split(":")[1])
            await _handle_point_stats_radius(update, context, radius_m)
            return

        # --- Выгрузка ДТП по точке в Excel ---
        if data == "ps_excel":
            await _send_point_stats_excel(update, context)
            return

        # --- Завершить режим вопросов ---
        if data == "end_qa":
            _clear_analytics_data(context.user_data)
            await query.edit_message_text(
                "Режим вопросов завершён.\n\nОтправьте /dtp для новой выгрузки."
            )
            return

        # --- Отмена ---
        if data == "cancel":
            context.user_data.clear()
            await query.edit_message_text(
                "Отменено. Отправьте /dtp чтобы начать заново."
            )
            return

    except Exception as e:
        logger.exception(f"Ошибка в callback handler: {e}")
        try:
            await query.edit_message_text(
                "Произошла ошибка при обработке запроса.\n\n"
            )
        except Exception:
            pass


# ========================
# Мультизапрос с прогрессом
# ========================

async def _start_fetching(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    period: ParsedPeriod,
) -> None:
    """
    Начинает выгрузку данных для выбранного региона и периода.
    Использует _fetch_cards_for_period, который при 5xx
    автоматически переключается на запасной метод через сайт ГИБДД.
    """
    reg_code = context.user_data.get("reg_code", "")
    reg_name = context.user_data.get("reg_name", "Регион " + reg_code)
    dat_list = period.get_dat_list()
    total_months = len(dat_list)

    try:
        await _tg_retry(lambda: query.edit_message_text(
            f"Выгрузка данных:\n\n"
            f"Регион: {reg_name}\n"
            f"Период: {period.label}\n"
            f"Запросов: {total_months}\n\n"
            f"Подготовка..."
        ), "edit_message_text (старт выгрузки)")
    except (TimedOut, NetworkError):
        logger.warning("Не удалось отправить стартовое сообщение выгрузки")

    # Прогресс-колбэк для обновления сообщения в Telegram
    async def _progress(i: int, total: int, month_name: str, year: str):
        progress_bar = _make_progress_bar(i, total)
        status_text = (
            f"Выгрузка данных:\n\n"
            f"Регион: {reg_name}\n"
            f"Период: {period.label}\n\n"
            f"{progress_bar} {i}/{total}\n"
            f"Запрос: {month_name} {year}..."
        )
        try:
            await query.edit_message_text(status_text)
        except Exception:
            pass  # Не критично

    # Уведомление о переключении на запасной метод
    async def _notify(text: str):
        try:
            await query.edit_message_text(
                f"Выгрузка данных:\n\n"
                f"Регион: {reg_name}\n"
                f"Период: {period.label}\n\n"
                f"{text}"
            )
        except Exception:
            pass

    # Загружаем данные (с автоматическим web-fallback при 5xx)
    all_cards, errors = await _fetch_cards_for_period(
        dat_list, reg_code,
        log_prefix="Выгрузка",
        progress_callback=_progress,
        notify_callback=_notify,
    )

    # Проверяем результат
    if not all_cards and errors:
        error_text = "\n".join(f"- {e}" for e in errors)
        try:
            await _tg_retry(lambda: query.edit_message_text(
                f"Не удалось получить данные.\n\nОшибки:\n{error_text}\n\n"
                f"Попробуйте позже или измените параметры."
            ), "edit_message_text (ошибки выгрузки)")
        except (TimedOut, NetworkError):
            logger.warning("Не удалось отправить сообщение об ошибках выгрузки")
        return

    # Предупреждение о пропущенных месяцах (частичная выгрузка)
    if errors:
        skipped_text = "\n".join(f"- {e}" for e in errors)
        warn_msg = (
            f"⚠ Не удалось скачать данные за следующие месяцы:\n"
            f"{skipped_text}\n\n"
            f"Выгрузка неполная — сравнение периодов "
            f"может быть некорректным.\n"
            f"Рекомендуется повторить запрос позже."
        )
        try:
            await _tg_retry(lambda: query.edit_message_text(
                f"Выгрузка данных:\n\n"
                f"Регион: {reg_name}\n"
                f"Период: {period.label}\n\n"
                f"{warn_msg}\n\n"
                f"Найдено ДТП: {len(all_cards)}\n"
                f"Генерация Excel-файлов..."
            ), "edit_message_text (предупреждение о пропущенных)")
        except (TimedOut, NetworkError):
            logger.warning("Не удалось отправить предупреждение о пропущенных месяцах")

    # Обработка и генерация Excel
    try:
        await _tg_retry(lambda: query.edit_message_text(
            f"Выгрузка данных:\n\n"
            f"Регион: {reg_name}\n"
            f"Период: {period.label}\n\n"
            f"Найдено ДТП: {len(all_cards)}\n"
            f"Генерация Excel-файлов..."
        ), "edit_message_text (статус генерации)")

        file1_data = build_file1_data(all_cards)
        file2_data = build_file2_data(all_cards)
        file1_bytes, file2_bytes = generate_both_files(file1_data, file2_data)

        # Отправляем файлы
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_reg = reg_name.replace(" ", "_")[:30]
        filename1 = f"dtp_cards_{safe_reg}_{period.year}_{timestamp}.xlsx"
        filename2 = f"dtp_uch_{safe_reg}_{period.year}_{timestamp}.xlsx"

        await _tg_retry(lambda: query.edit_message_text("Готово! Отправляю файлы..."),
                                 "edit_message_text (готово)")

        chat_id = query.message.chat_id

        from telegram import Bot
        bot: Bot = context.bot

        await _tg_retry(lambda: bot.send_document(
            chat_id=chat_id,
            document=file1_bytes,
            filename=filename1,
            caption=(
                f"Карточки ДТП\n"
                f"{reg_name} | {period.label}\n"
                f"ДТП: {len(all_cards)}"
            ),
        ), "send_document (карточки ДТП)")

        await _tg_retry(lambda: bot.send_document(
            chat_id=chat_id,
            document=file2_bytes,
            filename=filename2,
            caption=(
                f"Участники ДТП\n"
                f"{reg_name} | {period.label}\n"
                f"Участников: {len(file2_data)}"
            ),
        ), "send_document (участники ДТП)")

        # Удаляем сообщение о статусе
        try:
            await _tg_retry(lambda: query.message.delete(), "delete message")
        except Exception:
            pass

        logger.info(f"Файлы отправлены: {len(all_cards)} ДТП, {len(file2_data)} участников")

        # Предлагаем провести анализ
        await _offer_analysis(context, chat_id, reg_name, reg_code, period, all_cards)

    except Exception as e:
        logger.exception(f"Ошибка генерации/отправки файлов: {e}")
        try:
            await query.edit_message_text(f"Ошибка при генерации файлов: {e}")
        except Exception:
            pass

    finally:
        # НЕ очищаем user_data полностью, потому что _offer_analysis
        # сохранил данные аналитики (analytics_reg_code, analytics_cards и т.д.)
        # Удаляем только данные выгрузки, оставляем данные аналитики
        for key in ["reg_code", "reg_name", "sel_year"]:
            context.user_data.pop(key, None)


async def _offer_analysis(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    reg_name: str,
    reg_code: str,
    period: ParsedPeriod,
    current_cards: list[dict],
) -> None:
    """
    После выгрузки предлагает кнопки для проведения анализа
    (сравнение с аналогичным периодом прошлого года).
    Два режима: без ИИ и с ИИ (нейросеть GLM).
    """
    prev_year = period.year - 1
    prev_label = period.label.replace(str(period.year), str(prev_year))

    # Сохраняем данные для аналитики в user_data
    context.user_data["analytics_ready"] = True
    context.user_data["analytics_reg_code"] = reg_code
    context.user_data["analytics_reg_name"] = reg_name
    context.user_data["analytics_period"] = period
    context.user_data["analytics_cards"] = current_cards

    # Формируем кнопки
    buttons = []
    buttons.append([InlineKeyboardButton(
        f"\U0001F4CA Анализ ({prev_label})",
        callback_data="do_analytics",
    )])

    if LLM_API_KEY:
        buttons.append([InlineKeyboardButton(
            f"\U0001F916 Анализ с ИИ ({prev_label})",
            callback_data="do_analytics_ai",
        )])

    buttons.append([InlineKeyboardButton(
        "\U0001F525 Очаги ДТП",
        callback_data="do_concentration",
    )])
    buttons.append([InlineKeyboardButton(
        "\U0001F4CD Статистика по точке",
        callback_data="do_point_stats",
    )])

    keyboard = InlineKeyboardMarkup(buttons)

    if LLM_API_KEY:
        text = (
            f"\u2705 Выгрузка завершена: {len(current_cards)} ДТП.\n\n"
            f"Хотите провести сравнительный анализ с аналогичным периодом {prev_year} года?\n\n"
            f"\U0001F4CA <b>Без ИИ</b> — математический анализ (таблицы, проценты)\n"
            f"\U0001F916 <b>С ИИ</b> — анализ + резюме от нейросети\n"
            f"\U0001F525 <b>Очаги ДТП</b> — места концентрации аварийности\n"
            f"\U0001F4CD <b>По точке</b> — статистика ДТП по координатам"
        )
    else:
        text = (
            f"\u2705 Выгрузка завершена: {len(current_cards)} ДТП.\n\n"
            f"Хотите провести сравнительный анализ с аналогичным периодом {prev_year} года?\n\n"
            f"\U0001F525 <b>Очаги ДТП</b> — места концентрации аварийности\n"
            f"\U0001F4CD <b>По точке</b> — статистика ДТП по координатам"
        )

    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def _run_analysis(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    use_llm: bool = False,
) -> None:
    """
    Выполняет сравнительный анализ текущего периода с прошлым годом.

    Args:
        use_llm: Если True — после расчёта метрик запрашивает резюме у GLM
    """
    chat_id = update.effective_chat.id

    reg_code = context.user_data.get("analytics_reg_code", "")
    reg_name = context.user_data.get("analytics_reg_name", "")
    period = context.user_data.get("analytics_period")
    current_cards = context.user_data.get("analytics_cards", [])

    if not reg_code or not period or not current_cards:
        await update.callback_query.edit_message_text(
            "Данные для анализа не найдены. Пожалуйста, выполните выгрузку заново."
        )
        return

    # Период прошлого года
    prev_year = period.year - 1
    dat_list_prev = [f"{m}.{prev_year}" for m in period.months]
    prev_label = period.label.replace(str(period.year), str(prev_year))
    current_label = period.label

    mode_label = "\U0001F916 AI-анализ" if use_llm else "\U0001F4CA Анализ"

    status_msg = await _tg_retry(lambda: context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"{mode_label}: подготовка...\n\n"
            f"Регион: {reg_name}\n"
            f"Текущий период: {current_label}\n"
            f"Сравнение: {prev_label}\n\n"
            f"Загрузка данных за {prev_year} год..."
        ),
    ), "send_message (статус аналитики)")

    # Запрашиваем данные за прошлый год
    async def progress(i, total, month_name, year):
        bar = _make_progress_bar(i, total)
        try:
            await status_msg.edit_text(
                f"{mode_label}: загрузка...\n\n"
                f"{bar} {i}/{total}\n"
                f"Запрос: {month_name} {year}..."
            )
        except Exception:
            pass

    prev_cards, errors = await _fetch_cards_for_period(
        dat_list_prev, reg_code, "Аналитика", progress_callback=progress,
    )

    if not prev_cards:
        error_text = "\n".join(f"- {e}" for e in errors) if errors else "Нет данных"
        await status_msg.edit_text(
            f"\u26A0\uFE0F Не удалось загрузить данные за {prev_label}.\n\n"
            f"Ошибки:\n{error_text}\n\n"
            f"Возможно, данные за этот период ещё не опубликованы."
        )
        return

    # Предупреждение о неполных данных за прошлый год
    if errors:
        err_text = "\n".join(f"- {e}" for e in errors)
        try:
            await status_msg.edit_text(
                f"{mode_label}: данные за {prev_label} "
                f"загружены неполностью.\n\n"
                f"Не удалось скачать:\n{err_text}\n\n"
                f"Сравнение может быть некорректным."
            )
        except Exception:
            pass

    # Считаем метрики
    await status_msg.edit_text(f"{mode_label}: считаю метрики...")

    current_metrics = calculate_metrics(current_cards)
    previous_metrics = calculate_metrics(prev_cards)
    comparison = compare_metrics(current_metrics, previous_metrics)

    # Сохраняем comparison для возможных вопросов
    context.user_data["analytics_comparison"] = comparison
    context.user_data["analytics_current_label"] = current_label
    context.user_data["analytics_prev_label"] = prev_label

    # Сохраняем сырые карточки для детальных ответов LLM
    context.user_data["analytics_prev_cards"] = prev_cards

    # --- Генерируем контент ---
    llm_summary_text = None

    if use_llm and LLM_API_KEY:
        try:
            await status_msg.edit_text(
                f"{mode_label}: собираю данные и ищу новости..."
            )

            # Формируем дополнение из сырых карточек
            raw_sup = extract_raw_supplement(current_cards, current_label, max_cards=50)
            raw_sup += extract_raw_supplement(prev_cards, prev_label, max_cards=50)

            # Ищем новости из открытых источников (если включено)
            news_ctx = ""
            if ENABLE_NEWS_SEARCH:
                news_ctx = await fetch_news_context(reg_name, current_label, prev_label)
            # Сохраняем для вопросов
            context.user_data["analytics_news_context"] = news_ctx

            # Рассчитываем очаги ДТП для передачи в LLM
            clusters_ctx = ""
            try:
                await status_msg.edit_text(
                    f"{mode_label}: рассчитываю очаги ДТП..."
                )
                clusters, _preclusters = await calculate_concentration_points(
                    current_cards,
                )
                if clusters:
                    clusters_ctx = format_clusters_for_prompt(clusters, max_clusters=10)
                    context.user_data["analytics_clusters"] = clusters
                    logger.info(
                        f"LLM-анализ: рассчитано {len(clusters)} очагов "
                        f"для контекста ({len(clusters_ctx)} симв.)"
                    )
                else:
                    context.user_data["analytics_clusters"] = []
            except Exception as e:
                logger.warning(f"Не удалось рассчитать очаги для LLM-контекста: {e}")
                context.user_data["analytics_clusters"] = []

            await status_msg.edit_text(
                f"{mode_label}: нейросеть анализирует данные...\n"
                f"⏳ Обычно занимает 15-30 секунд."
            )

            llm_summary_text = await get_ai_summary(
                comparison=comparison,
                reg_name=reg_name,
                current_label=current_label,
                prev_label=prev_label,
                raw_supplement=raw_sup,
                news_context=news_ctx,
                clusters_context=clusters_ctx,
            )
        except Exception as e:
            logger.error(f"Ошибка LLM: {e}")
            llm_summary_text = None
            await status_msg.edit_text(
                f"\u26A0\uFE0F Не удалось получить ответ от нейросети.\n\n"
                f"Ошибка: {e}\n\n"
                f"Отправляю математический анализ без ИИ.\n"
                f"Попробуйте нажать кнопку ещё раз — обычно работает со 2-й попытки."
            )
            # Не удаляем status_msg — пользователь должен увидеть ошибку

    # Генерируем Excel
    analytics_data = build_analytics_excel_data(
        comparison=comparison,
        reg_name=reg_name,
        current_label=current_label,
        previous_label=prev_label,
    )
    column_names = get_analytics_column_names(current_label, prev_label)
    analytics_bytes = generate_analytics_file(analytics_data, column_names)

    # Удаляем сообщение о статусе (если не было ошибки LLM)
    if use_llm and not llm_summary_text:
        # status_msg уже содержит сообщение об ошибке LLM — не удаляем
        pass
    else:
        try:
            await status_msg.delete()
        except Exception:
            pass

    # Отправляем результаты
    if use_llm and llm_summary_text:
        # Экранируем спецсимволы HTML в LLM-ответе, чтобы теги от модели
        # (например <i>, <b>) не ломали Telegram HTML-парсер
        safe_llm = html_mod.escape(llm_summary_text)
        # Режим с ИИ: сначала LLM-резюме, потом таблица + Excel
        await _send_long_message(
            context.bot, chat_id,
            text=(
                f"\U0001F916 <b>Аналитика ИИ: {reg_name}</b>\n"
                f"{current_label} vs {prev_label}\n\n"
                f"<i>{safe_llm}</i>"
            ),
            parse_mode="HTML",
        )
        # Также отправляем математический анализ
        analytics_text = build_analytics_message(
            comparison=comparison,
            reg_name=reg_name,
            current_label=current_label,
            previous_label=prev_label,
        )
        # Отправляем как отдельное сообщение (математика)
        await _send_long_message(
            context.bot, chat_id,
            text=f"\U0001F4CA <b>Детальные данные:</b>\n\n{analytics_text}",
            parse_mode="HTML",
        )
    else:
        # Режим без ИИ: только математический анализ
        analytics_text = build_analytics_message(
            comparison=comparison,
            reg_name=reg_name,
            current_label=current_label,
            previous_label=prev_label,
        )
        await _send_long_message(
            context.bot, chat_id,
            text=analytics_text,
            parse_mode="HTML",
        )

    # Отправляем Excel-файл аналитики
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_reg = reg_name.replace(" ", "_")[:30]
    ai_suffix = "_ai" if use_llm else ""
    filename = f"dtp_analytics{ai_suffix}_{safe_reg}_{period.year}_vs_{prev_year}_{timestamp}.xlsx"

    await _tg_retry(lambda: context.bot.send_document(
        chat_id=chat_id,
        document=analytics_bytes,
        filename=filename,
        caption=(
            f"\U0001F4CA Аналитика: {reg_name}\n"
            f"{current_label} vs {prev_label}\n"
            f"Текущий: {len(current_cards)} ДТП | Прошлый: {len(prev_cards)} ДТП"
        ),
    ), "send_document (аналитика)")

    # Предлагаем задать вопросы (если есть LLM-ключ)
    if LLM_API_KEY:
        context.user_data["qa_mode"] = True
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "\u274C Завершить",
                callback_data="end_qa",
            )],
        ])
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "\u2753 Вы можете задавать вопросы по этим данным.\n"
                "Просто напишите вопрос текстом, например:\n"
                "\n"
                "\u2022 Почему выросла тяжкость аварий?\n"
                "\u2022 Какие рекомендации можно дать?\n"
                "\u2022 Что происходит с нетрезвыми водителями?\n\n"
                "Или нажмите /dtp для новой выгрузки."
            ),
            reply_markup=keyboard,
        )
    else:
        # Без ИИ — очищаем данные аналитики
        _clear_analytics_data(context.user_data)

    logger.info(
        f"Аналитика отправлена: {reg_name}, "
        f"{current_label} vs {prev_label}, "
        f"{len(current_cards)} vs {len(prev_cards)} ДТП, "
        f"LLM={'да' if (use_llm and llm_summary_text) else 'нет'}"
    )


def _clear_analytics_data(user_data: dict) -> None:
    """Очищает все данные аналитики из user_data (включая тяжёлые списки ДТП)."""
    for key in [
        "analytics_ready", "analytics_reg_code", "analytics_reg_name",
        "analytics_period", "analytics_cards", "analytics_comparison",
        "analytics_current_label", "analytics_prev_label",
        "analytics_prev_cards", "analytics_clusters",
        "analytics_news_context", "qa_mode",
        "point_stats_mode", "point_stats_lat", "point_stats_lon", "point_stats_radius",
        "cameras_data", "waiting_camera_file",
    ]:
        user_data.pop(key, None)


async def _run_concentration_points(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Рассчитывает очаги концентрации ДТП с исторической динамикой
    (сравнение с прошлым годом) и отправляет Excel-файл.
    """
    chat_id = update.effective_chat.id

    reg_name = context.user_data.get("analytics_reg_name", "")
    reg_code = context.user_data.get("analytics_reg_code", "")
    period = context.user_data.get("analytics_period")
    current_cards = context.user_data.get("analytics_cards", [])

    if not period or not current_cards:
        await update.callback_query.edit_message_text(
            "Данные для расчёта очагов не найдены. "
            "Пожалуйста, выполните выгрузку заново."
        )
        return

    current_label = period.label
    prev_year = period.year - 1
    dat_list_prev = [f"{m}.{prev_year}" for m in period.months]
    prev_label = period.label.replace(str(period.year), str(prev_year))

    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "\U0001F525 Очаги ДТП: подготовка...\n\n"
            f"Регион: {reg_name}\n"
            f"Период: {current_label}\n"
            f"ДТП: {len(current_cards)}\n\n"
            f"\u26A0\uFE0F Расчёт может занять 2-5 минут\n"
            f"(загрузка данных за прошлый год + OSM + 2 расчёта очагов)"
        ),
    )

    async def progress_callback(text: str) -> None:
        """Обновляет статусное сообщение."""
        try:
            await status_msg.edit_text(
                f"\U0001F525 Очаги ДТП (динамика)\n\n"
                f"Регион: {reg_name}\n"
                f"{current_label} vs {prev_label}\n\n"
                f"{text}"
            )
        except Exception:
            pass

    try:
        # --- Загрузка данных за прошлый год ---
        prev_cards = []
        errors = []

        if reg_code:
            async def fetch_progress(i, total, month_name, year):
                await progress_callback(
                    f"Загрузка данных за прошлый год...\n"
                    f"{i}/{total} — {month_name} {year}"
                )

            prev_cards, errors = await _fetch_cards_for_period(
                dat_list_prev, reg_code, "Очаги-динамика",
                progress_callback=fetch_progress,
            )

            if errors:
                logger.warning(
                    f"Ошибки загрузки прошлого года: {errors}"
                )
                # Предупреждаем пользователя о неполных данных
                err_text = "\n".join(f"- {e}" for e in errors)
                try:
                    await progress_callback(
                        f"Загрузка данных за прошлый год...\n"
                        f"⚠ Не удалось скачать:\n{err_text}\n\n"
                        f"Данные за эти месяцы отсутствуют, "
                        f"сравнение будет неполным."
                    )
                except Exception:
                    pass

        # --- Расчёт очагов с динамикой ---
        clusters = await calculate_concentration_dynamics(
            current_cards,
            prev_cards,
            progress_callback,
        )

        if not clusters:
            await status_msg.edit_text(
                "\U0001F525 Очаги ДТП\n\n"
                "Очаги концентрации ДТП не найдены.\n\n"
                "Возможные причины:\n"
                "\u2022 Мало ДТП за выбранный период\n"
                "\u2022 ДТП распределены равномерно (нет концентрации)\n"
                "\u2022 У большинства ДТП нет координат"
            )
            return

        # --- Сводная статистика ---
        dyn_stats = build_dynamics_summary(clusters)

        # --- Разделяем очаги: текущие vs исчезнувшие ---
        current_only_clusters = [
            c for c in clusters if not c.get("_is_lost", False)
        ]

        # --- Обогащение камерами (если загружен файл) ---
        cameras = context.user_data.get("cameras_data")
        if cameras:
            await progress_callback(
                f"Сопоставление с камерами фотовидеофиксации...\n"
                f"Камер: {len(cameras)}"
            )
            enrich_clusters_with_cameras(current_only_clusters, cameras)
            lost_clusters = [
                c for c in clusters if c.get("_is_lost", False)
            ]
            if lost_clusters:
                enrich_clusters_with_cameras(lost_clusters, cameras)

        # --- Генерируем Excel с 4 листами ---
        # Лист 1: очаги запрашиваемого года (стандартный формат)
        current_data = build_concentration_excel_data(current_only_clusters)
        current_columns = get_concentration_column_names()

        # Лист 2: динамика очагов (текущие + исчезнувшие)
        dyn_data = build_dynamics_excel_data(clusters)
        dyn_columns = get_dynamics_column_names()

        # Лист 3: детализация ДТП
        detail_data = build_dynamics_detail_data(
            clusters, current_label, prev_label,
        )
        detail_columns = get_dynamics_detail_column_names()

        # Лист 4: предочаги
        preclusters = clusters[0].get("_preclusters", []) if clusters else []
        precluster_data = None
        precluster_columns = None
        if preclusters:
            # Обогащаем предочаги камерами
            if cameras:
                enrich_clusters_with_cameras(preclusters, cameras)
            precluster_data = build_precluster_excel_data(preclusters)
            precluster_columns = get_precluster_column_names()

        conc_bytes = generate_concentration_dynamics_file(
            current_data, current_columns,
            dyn_data, dyn_columns,
            detail_data, detail_columns,
            precluster_data, precluster_columns,
        )

        # Удаляем статус
        try:
            await status_msg.delete()
        except Exception:
            pass

        # --- Статистика по очагам текущего года ---
        current_np_count = sum(
            1 for c in current_only_clusters
            if c["zone_type"].startswith("settlement")
        )
        current_nonp_count = sum(
            1 for c in current_only_clusters
            if c["zone_type"] == "nonsettlement"
        )
        current_total_clusters = len(current_only_clusters)
        current_total_dtp = sum(
            c["total_accidents"] for c in current_only_clusters
        )
        current_deaths = sum(
            c["deaths"] for c in current_only_clusters
        )
        current_injured = sum(
            c["injured"] for c in current_only_clusters
        )

        # --- Текстовое резюме ---
        # Блок 1: очаги запрашиваемого года
        summary_lines = [
            f"\U0001F525 <b>Очаги ДТП: {reg_name}</b>",
            f"Период: {current_label}",
            f"Всего ДТП: {len(current_cards)}",
            "",
            f"\U0001F4CA <b>Очагов за {current_label}:</b> <b>{current_total_clusters}</b>",
            f"  \u2022 В НП: {current_np_count}",
            f"  \u2022 Вне НП: {current_nonp_count}",
            "",
            f"ДТП в очагах: {current_total_dtp}",
            f"  \u2022 Погибло: {current_deaths}",
            f"  \u2022 Ранено: {current_injured}",
        ]

        # Блок камер (если загружены)
        if cameras:
            cam_closed = sum(
                1 for c in current_only_clusters
                if (c.get("camera_match") or {}).get("status") == "закрыт"
            )
            cam_open = current_total_clusters - cam_closed
            summary_lines.extend([
                "",
                f"\U0001F4F7 <b>Камеры фотовидеофиксации:</b>",
                f"  \u2022 Закрыто камерой: {cam_closed}/{current_total_clusters}",
                f"  \u2022 Открыто: {cam_open}",
            ])

        # Блок 2: динамика (только если есть данные за прошлый год)
        if prev_cards:
            summary_lines.extend([
                "",
                f"<b>\U0001F4C8 Динамика ({prev_label}):</b>",
                f"  \U0001F7E2 Новый: {dyn_stats['new']}",
                f"  \u2B06 Рост: {dyn_stats['growing']}",
                f"  \u2B07 Снижение: {dyn_stats['shrinking']}",
                f"  \u27A1 Стабильный: {dyn_stats['stable']}",
                f"  \u274C Исчезнувший: {dyn_stats['lost']}",
            ])

            if dyn_stats["prev_total_dtp"] > 0:
                delta_dtp = (
                    dyn_stats["current_total_dtp"]
                    - dyn_stats["prev_total_dtp"]
                )
                summary_lines.append("")
                summary_lines.append(
                    f"ДТП в очагах ({prev_label}): {dyn_stats['prev_total_dtp']} "
                    f"({delta_dtp:+d})"
                )

        # Блок 3: предочаги
        if preclusters:
            pre_np = sum(
                1 for p in preclusters
                if p["zone_type"].startswith("settlement")
            )
            pre_nonp = len(preclusters) - pre_np
            pre_dtp = sum(p["total_accidents"] for p in preclusters)
            summary_lines.extend([
                "",
                f"\u26A0\uFE0F <b>Предочаги:</b> <b>{len(preclusters)}</b>",
                f"  \u2022 В НП: {pre_np}",
                f"  \u2022 Вне НП: {pre_nonp}",
                f"  \u2022 ДТП в предочагах: {pre_dtp}",
            ])

        await _send_long_message(
            context.bot, chat_id,
            text="\n".join(summary_lines),
            parse_mode="HTML",
        )

        # Отправляем Excel
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_reg = reg_name.replace(" ", "_")[:30]
        filename = (
            f"dtp_ochagi_dynamics_{safe_reg}_"
            f"{period.year}_{timestamp}.xlsx"
        )

        await context.bot.send_document(
            chat_id=chat_id,
            document=conc_bytes,
            filename=filename,
            caption=(
                f"\U0001F525 Очаги ДТП: {reg_name}\n"
                f"{current_label}"
                + (f" | Динамика: {prev_label}" if prev_cards else "")
                + f"\n"
                f"Очагов: {current_total_clusters} | "
                f"ДТП в очагах: {current_total_dtp}"
                + (f" | Предочагов: {len(preclusters)}" if preclusters else "")
            ),
        )

        # Сохраняем очаги в сессию (для LLM и дальнейших вопросов)
        context.user_data["analytics_clusters"] = clusters

        # Освобождаем память: прошлый год больше не нужен
        context.user_data.pop("analytics_prev_cards", None)

        logger.info(
            f"Очаги отправлены: {reg_name}, "
            f"{current_label}, "
            f"{current_total_clusters} очагов из {len(current_cards)} ДТП"
            + (f", динамика: {prev_label}" if prev_cards else "")
        )

    except Exception as e:
        logger.exception(f"Ошибка расчёта очагов (динамика): {e}")
        try:
            await status_msg.edit_text(
                f"\u26A0\uFE0F Ошибка при расчёте очагов ДТП:\n\n{e}\n\n"
                f"Попробуйте позже или выберите другой период."
            )
        except Exception:
            pass


# ========================
# Статистика по точке
# ========================

async def _start_point_stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Начинает режим «Статистика по точке».
    Загружает данные за прошлый год (если ещё нет) и просит координаты.
    """
    chat_id = update.effective_chat.id

    reg_code = context.user_data.get("analytics_reg_code", "")
    reg_name = context.user_data.get("analytics_reg_name", "")
    period = context.user_data.get("analytics_period")
    current_cards = context.user_data.get("analytics_cards", [])

    if not period or not current_cards:
        await update.callback_query.edit_message_text(
            "Данные не найдены. Пожалуйста, выполните выгрузку заново."
        )
        return

    current_label = period.label
    prev_year = period.year - 1
    prev_label = period.label.replace(str(period.year), str(prev_year))

    # Проверяем, есть ли данные за прошлый год
    prev_cards = context.user_data.get("analytics_prev_cards", [])

    if not prev_cards and reg_code:
        # Загружаем данные за прошлый год
        status_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "\U0001F4CD Статистика по точке: подготовка...\n\n"
                f"Загрузка данных за {prev_label}..."
            ),
        )

        dat_list_prev = [f"{m}.{prev_year}" for m in period.months]

        async def pt_progress(i, total, month_name, year):
            await status_msg.edit_text(
                f"\U0001F4CD Загрузка данных за прошлый год...\n\n"
                f"{i}/{total} — {month_name} {year}"
            )

        prev_cards, pt_errors = await _fetch_cards_for_period(
            dat_list_prev, reg_code, "Точечная статистика",
            progress_callback=pt_progress,
        )

        # Предупреждение о неполных данных
        if pt_errors:
            err_text = "\n".join(f"- {e}" for e in pt_errors)
            try:
                await status_msg.edit_text(
                    f"\U0001F4CD Данные за {prev_label} "
                    f"загружены неполностью.\n\n"
                    f"Не удалось скачать:\n{err_text}\n\n"
                    f"Сравнение может быть некорректным."
                )
            except Exception:
                pass

        # Сохраняем для повторного использования
        if prev_cards:
            context.user_data["analytics_prev_cards"] = prev_cards
            context.user_data["analytics_prev_label"] = prev_label

        try:
            await status_msg.delete()
        except Exception:
            pass

    # Входим в режим ожидания координат
    context.user_data["point_stats_mode"] = True

    await update.callback_query.edit_message_text(
        "\U0001F4CD <b>Статистика по точке</b>\n\n"
        "Отправьте координаты одним из способов:\n\n"
        "\U0001F4CD <b>Прикрепить локацию</b> (скрепка \U0001F4CE → Местоположение)\n\n"
        "Или текстом:\n"
        "<code>55.1234, 38.5678</code>\n\n"
        f"Период: {current_label}"
        + (f" | {prev_label}" if prev_cards else ""),
        parse_mode="HTML",
    )


async def _handle_point_stats_radius(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    radius_m: int,
) -> None:
    """Пересчитывает статистику с новым радиусом (координаты те же)."""
    lat = context.user_data.get("point_stats_lat")
    lon = context.user_data.get("point_stats_lon")

    if lat is None or lon is None:
        await update.callback_query.edit_message_text(
            "Координаты потеряны. Попробуйте снова."
        )
        return

    await _process_point_stats(
        context, update.effective_chat.id, lat, lon, radius_m,
        edit_query=update.callback_query,
    )


async def _send_point_stats_excel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Генерирует и отправляет Excel-файл с ДТП в радиусе точки.
    Использует сохранённые координаты и радиус из user_data.
    """
    chat_id = update.effective_chat.id

    lat = context.user_data.get("point_stats_lat")
    lon = context.user_data.get("point_stats_lon")
    radius_m = context.user_data.get("point_stats_radius", 500)

    if lat is None or lon is None:
        await update.callback_query.answer(
            "Координаты не найдены. Отправьте координаты заново.",
            show_alert=True,
        )
        return

    # Подтверждение
    await update.callback_query.answer("Генерирую Excel-файл...")

    current_cards = context.user_data.get("analytics_cards", [])
    prev_cards = context.user_data.get("analytics_prev_cards", [])
    period = context.user_data.get("analytics_period")
    current_label = period.label if period else "Текущий период"
    prev_label = context.user_data.get("analytics_prev_label", "")

    # Фильтруем карточки по радиусу
    from point_statistics import (
        filter_cards_by_radius,
        build_point_stats_excel_data,
        get_point_stats_column_names,
    )

    current_filtered = filter_cards_by_radius(current_cards, lat, lon, radius_m)
    prev_filtered = filter_cards_by_radius(prev_cards, lat, lon, radius_m) if prev_cards else []

    total = len(current_filtered) + len(prev_filtered)
    if total == 0:
        await context.bot.send_message(
            chat_id=chat_id,
            text="\u26A0\uFE0F В указанном радиусе нет ДТП для выгрузки.",
        )
        return

    # Строим данные для Excel
    current_rows, prev_rows = build_point_stats_excel_data(
        current_filtered,
        prev_filtered if prev_filtered else None,
        current_label,
        prev_label,
    )

    column_names = get_point_stats_column_names()

    # Генерируем файл
    excel_bytes = generate_point_stats_file(
        current_rows=current_rows,
        prev_rows=prev_rows if prev_rows else None,
        column_names=column_names,
        current_label=current_label,
        prev_label=prev_label if prev_filtered else None,
    )

    # Имя файла
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if radius_m >= 1000:
        radius_str = f"{radius_m / 1000:.0f}km"
    else:
        radius_str = f"{radius_m}m"

    filename = f"dtp_point_{radius_str}_{timestamp}.xlsx"

    # Формируем подпись
    if radius_m >= 1000:
        radius_display = f"{radius_m / 1000:.0f} км"
    else:
        radius_display = f"{radius_m} м"

    caption_parts = [
        f"\U0001F4CD ДТП в радиусе {radius_display}",
        f"Координаты: {lat:.5f}, {lon:.5f}",
        f"Период: {current_label}",
    ]
    if prev_filtered:
        caption_parts.append(f"Сравнение: {prev_label}")
    caption_parts.append(f"ДТП: {total} ({len(current_filtered)} + {len(prev_filtered)})")

    await context.bot.send_document(
        chat_id=chat_id,
        document=excel_bytes,
        filename=filename,
        caption="\n".join(caption_parts),
    )


async def _process_point_stats(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    lat: float,
    lon: float,
    radius_m: int,
    edit_query=None,
) -> None:
    """
    Вычисляет и отправляет статистику по точке.

    Args:
        edit_query: Если передан — редактирует сообщение с кнопками.
            Иначе отправляет новое сообщение.
    """
    current_cards = context.user_data.get("analytics_cards", [])
    prev_cards = context.user_data.get("analytics_prev_cards", [])
    period = context.user_data.get("analytics_period")
    current_label = period.label if period else ""
    prev_label = context.user_data.get("analytics_prev_label", "")

    # Сохраняем координаты и радиус для переключения
    context.user_data["point_stats_lat"] = lat
    context.user_data["point_stats_lon"] = lon
    context.user_data["point_stats_radius"] = radius_m

    # Вычисляем статистику
    stats = calculate_point_statistics(
        lat, lon, radius_m, current_cards,
        prev_cards if prev_cards else None,
    )

    # Форматируем сообщение
    message_text = format_point_stats_message(
        stats, current_label,
        prev_label if prev_cards else None,
    )

    # Кнопки радиуса
    radius_buttons = []
    for r_m, r_label in RADIUS_OPTIONS:
        active = "\u2022 " if r_m == radius_m else ""
        radius_buttons.append(InlineKeyboardButton(
            f"{active}{r_label}",
            callback_data=f"ps_radius:{r_m}",
        ))

    # Кнопка выгрузки в Excel (если есть ДТП)
    total_dtp = stats["current"]["total"]
    prev_total = stats["prev"]["total"] if stats.get("prev") else 0
    buttons = [radius_buttons]

    if total_dtp > 0 or prev_total > 0:
        excel_label = f"\U0001F4E5 Выгрузить в Excel ({total_dtp + prev_total} ДТП)"
        buttons.append([InlineKeyboardButton(
            excel_label,
            callback_data="ps_excel",
        )])

    keyboard = InlineKeyboardMarkup(buttons)

    # Отправляем или редактируем
    if edit_query:
        try:
            await edit_query.edit_message_text(
                message_text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except Exception:
            # Если сообщение слишком длинное для редактирования — отправляем новое
            await _send_long_message(
                context.bot, chat_id,
                text=message_text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
    else:
        await _send_long_message(
            context.bot, chat_id,
            text=message_text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )


async def _handle_location_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Обрабатывает сообщение с локацией (пinned location)."""
    if not context.user_data.get("point_stats_mode"):
        return

    location = update.message.location
    if not location:
        return

    lat = location.latitude
    lon = location.longitude
    radius_m = context.user_data.get("point_stats_radius", 500)

    await _process_point_stats(
        context, update.effective_chat.id, lat, lon, radius_m,
    )


async def _handle_coordinate_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    """Обрабатывает текстовое сообщение с координатами."""
    if not context.user_data.get("point_stats_mode"):
        return

    coords = parse_coordinates(text)
    if coords is None:
        # Возможно, это вопрос для LLM — не обрабатываем здесь
        return

    lat, lon = coords
    radius_m = context.user_data.get("point_stats_radius", 500)

    # Удаляем сообщение с координатами (чистота чата)
    try:
        await update.message.delete()
    except Exception:
        pass

    await _process_point_stats(
        context, update.effective_chat.id, lat, lon, radius_m,
    )


async def _handle_analytics_question(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    question: str,
    comparison: dict,
    reg_name: str,
    current_label: str,
    prev_label: str,
) -> None:
    """
    Обрабатывает вопрос пользователя по данным аналитики.
    Отправляет вопрос в LLM и возвращает ответ.
    """
    chat_id = update.effective_chat.id

    # Индикатор набора
    wait_msg = await update.message.reply_text(
        "\U0001F916 Анализирую вопрос...\n"
        "⏳ Обычно занимает 15-30 секунд."
    )

    try:
        # Формируем дополнение из сырых карточек (если есть)
        # Для вопросов берём меньше карточек — только статистику + 15 самых тяжёлых
        raw_sup = ""
        current_cards = context.user_data.get("analytics_cards", [])
        prev_cards = context.user_data.get("analytics_prev_cards", [])
        if current_cards or prev_cards:
            raw_sup = extract_raw_supplement(current_cards, current_label, max_cards=15)
            raw_sup += extract_raw_supplement(prev_cards, prev_label, max_cards=15)

        answer = await get_ai_answer(
            question=question,
            comparison=comparison,
            reg_name=reg_name,
            current_label=current_label,
            prev_label=prev_label,
            raw_supplement=raw_sup,
            news_context=context.user_data.get("analytics_news_context", ""),
            clusters_context=format_clusters_for_prompt(
                context.user_data.get("analytics_clusters", [])
            ),
        )

        # Удаляем индикатор
        try:
            await wait_msg.delete()
        except Exception:
            pass

        # Отправляем ответ (экранируем и вопрос, и ответ LLM)
        await _send_long_message(
            context.bot, chat_id,
            text=(
                f"\U0001F916 <b>Вопрос:</b> {html_mod.escape(question)}\n\n"
                f"{html_mod.escape(answer)}"
            ),
            parse_mode="HTML",
        )

    except Exception as e:
        logger.error(f"Ошибка при ответе на вопрос: {e}")
        try:
            await wait_msg.edit_text(
                f"\u26A0\uFE0F Не удалось получить ответ от нейросети.\n\n"
                f"Ошибка: {e}\n\n"
                f"Попробуйте переформулировать вопрос или нажмите кнопку ниже."
            )
        except Exception:
            pass


def _make_progress_bar(current: int, total: int, width: int = 20) -> str:
    """Генерирует текстовую строку прогресса."""
    if total <= 1:
        return ""

    filled = int(width * current / total)
    empty = width - filled
    return f"[{'=' * filled}{' ' * empty}]"


# ========================
# Обработчик текстовых сообщений (NLP)
# ========================

async def _handle_document(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Обрабатывает загрузку файла (камеры фотовидеофиксации)."""
    if not context.user_data.get("waiting_camera_file"):
        # Не ожидаем файл — игнорируем
        return

    context.user_data.pop("waiting_camera_file", None)

    document = update.message.document
    if not document:
        return

    # Проверяем имя файла (.xls или .xlsx)
    filename = document.file_name or ""
    if not filename.startswith("gibddrf_cameras_change"):
        await update.message.reply_text(
            "\u26A0\uFE0F Неверный файл.\n\n"
            "Ожидается файл: gibddrf_cameras_change_*.xls"
        )
        return

    wait_msg = await update.message.reply_text(
        "\U0001F4F7 Обработка файла камер..."
    )

    try:
        file = await document.get_file()

        # Скачиваем файл
        import io
        import tempfile
        import os
        from camera_loader import parse_camera_file

        # Используем временный файл — самый надёжный способ
        tmp_path = os.path.join(tempfile.gettempdir(), f"cam_{document.file_id}.xls")
        try:
            await file.download_to_drive(custom_path=tmp_path)
            with open(tmp_path, "rb") as f:
                file_bytes = f.read()
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

        # Диагностика: логируем сигнатуру до вызова парсера
        sig_hex = file_bytes[:8].hex() if len(file_bytes) >= 8 else "<too short>"
        logger.info(
            f"Загружен файл: {document.file_name}, "
            f"{len(file_bytes)} байт, сигнатура: {sig_hex}"
        )

        cameras = parse_camera_file(file_bytes)

        if not cameras:
            await wait_msg.edit_text(
                "\u26A0\uFE0F В файле не найдено камер.\n"
                "Проверьте формат файла."
            )
            return

        # Сохраняем в сессию
        context.user_data["cameras_data"] = cameras

        # Сохраняем файл на диск (кэш по региону)
        # reg_code мог быть удалён после выгрузки ДТП, проверяем все источники
        reg_code = (
            context.user_data.get("concentration_reg_code", "")
            or context.user_data.get("reg_code", "")
            or context.user_data.get("analytics_reg_code", "")
        )
        if reg_code and file_bytes:
            try:
                from camera_cache import save_camera_file
                path = save_camera_file(reg_code, file_bytes)
                logger.info(f"Камеры сохранены в кэш: {path}")
                save_ok = True
            except Exception as save_err:
                logger.error(f"Ошибка сохранения камер в кэш: {save_err}", exc_info=True)
                save_ok = False
        else:
            logger.warning(
                f"Кэширование камер пропущено: "
                f"reg_code={reg_code!r}, file_bytes={len(file_bytes) if file_bytes else 0}, "
                f"user_data keys={list(context.user_data.keys())}"
            )
            save_ok = False

        with_pk = sum(1 for c in cameras if c["has_piket"])
        without_pk = len(cameras) - with_pk

        save_line = ""
        if reg_code and save_ok:
            save_line = f"  \u2022 Файл сохранён для региона {reg_code}\n"
        elif reg_code and not save_ok:
            save_line = f"  \u26A0\uFE0F Не удалось сохранить файл на сервере\n"

        await wait_msg.edit_text(
            f"\u2705 Загружено <b>{len(cameras)}</b> камер:\n"
            f"  \u2022 С пикетажем: {with_pk}\n"
            f"  \u2022 Городских: {without_pk}\n"
            f"{save_line}\n"
            f"Запускаю расчёт очагов...",
            parse_mode="HTML",
        )

        # Запускаем расчёт очагов
        await _run_concentration_points(update, context)

    except Exception as e:
        logger.error(f"Ошибка обработки файла камер: {e}")
        await wait_msg.edit_text(
            f"\u26A0\uFE0F Ошибка обработки файла:\n\n<code>{e}</code>\n\n"
            f"Попробуйте ещё раз или нажмите 'Без камер'.",
            parse_mode="HTML",
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает текстовые сообщения:
      - Пытается распознать запрос на естественном языке
      - Если распознал — начинает выгрузку
      - Если нет — предлагает помощь
    """
    user = update.effective_user
    user_text = update.message.text.strip()

    if not user_text:
        return

    # --- Режим статистики по точке: проверяем координаты ---
    if context.user_data.get("point_stats_mode"):
        coords = parse_coordinates(user_text)
        if coords is not None:
            lat, lon = coords
            radius_m = context.user_data.get("point_stats_radius", 500)
            try:
                await update.message.delete()
            except Exception:
                pass
            await _process_point_stats(
                context, update.effective_chat.id, lat, lon, radius_m,
            )
            return
        # Если координаты не распознаны — выходим из режима и падаем ниже
        context.user_data.pop("point_stats_mode", None)

    if not is_user_allowed(user.id):
        await update.message.reply_text("У вас нет доступа к этому боту.")
        return

    logger.info(f"Сообщение от user_id={user.id}: {user_text}")

    # Пытаемся распознать запрос
    parsed = await parse_user_message(user_text)

    if parsed is not None:
        # Полностью распознано — начинаем выгрузку
        reg_code = parsed.region_code
        reg_name = parsed.region_name
        period = parsed.period

        # Очищаем контекст аналитики от предыдущего запроса
        _clear_analytics_data(context.user_data)

        logger.info(
            f"Распознан запрос: регион={reg_name} ({reg_code}), "
            f"период={period.label}"
        )

        # Сохраняем в user_data для _start_fetching
        context.user_data["reg_code"] = reg_code
        context.user_data["reg_name"] = reg_name

        # Создаём сообщение и вызываем выгрузку
        processing_msg = await update.message.reply_text(
            f"Распознан запрос:\n\n"
            f"Регион: {reg_name}\n"
            f"Период: {period.label}\n\n"
            f"Начинаю выгрузку..."
        )

        # Создаём фейковый callback-объект для _start_fetching
        class FakeQuery:
            def __init__(self, message, bot):
                self.message = message
                self._bot = bot

            async def edit_message_text(self, text, reply_markup=None):
                try:
                    await self._bot.edit_message_text(
                        chat_id=self.message.chat_id,
                        message_id=self.message.message_id,
                        text=text,
                        reply_markup=reply_markup,
                    )
                except Exception:
                    pass

        fake_query = FakeQuery(processing_msg, context.bot)
        await _start_fetching(fake_query, context, period)
        return

    # Не удалось распознать полностью — пробуем частичный парсинг
    regions = await _load_regions_if_needed(context)

    # Попробуем найти хотя бы регион
    region = find_region(user_text, regions) if regions else None
    period = parse_period(user_text)

    if region is not None and period is None:
        # Регион найден, но период — нет → показываем выбор периода
        reg_code, reg_name = region
        context.user_data["reg_code"] = reg_code
        context.user_data["reg_name"] = reg_name
        context.user_data["sel_year"] = datetime.now().year

        keyboard = build_period_keyboard(datetime.now().year)
        await update.message.reply_text(
            f"Регион распознан: {reg_name}\n\n"
            f"Теперь выберите период:",
            reply_markup=keyboard,
        )
        return

    if region is None and period is not None:
        # Период найден, но регион — нет
        # Очищаем контекст аналитики при новой выгрузке
        _clear_analytics_data(context.user_data)

        await update.message.reply_text(
            f"Период распознан: {period.label}\n\n"
            f"Но не удалось определить регион.\n"
            f"Укажите название региона или его код.\n\n"
            f"Или используйте /dtp для выбора через кнопки."
        )
        return

    # --- Режим вопрос-ответ по данным аналитики ---
    if context.user_data.get("qa_mode") and LLM_API_KEY:
        comparison = context.user_data.get("analytics_comparison")
        reg_name = context.user_data.get("analytics_reg_name", "")
        current_label = context.user_data.get("analytics_current_label", "")
        prev_label = context.user_data.get("analytics_prev_label", "")

        if comparison:
            await _handle_analytics_question(
                update, context, user_text,
                comparison, reg_name, current_label, prev_label,
            )
            return

    # Ничего не распознано — подсказка
    await update.message.reply_text(
        "Не удалось распознать запрос.\n\n"
        "Попробуйте один из вариантов:\n\n"
        "1. Текстом:\n"
        "   Вологодская область за 2025 год\n"
        "   Алтайский край за март 2025\n"
        "   за I квартал 2025 Москва\n\n"
        "2. Строгий формат:\n"
        "   2.2024 1101\n\n"
        "3. Через кнопки:\n"
        "   /dtp\n\n"
        "Справка: /help\n"
        "Список регионов: /regions"
    )


# ========================
# Функция ошибки
# ========================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error

    if isinstance(error, Conflict):
        import time as _time
        global _conflict_last_log
        now = _time.monotonic()
        if now - _conflict_last_log >= _CONFLICT_LOG_INTERVAL:
            _conflict_last_log = now
            logger.warning(
                "Conflict: другой экземпляр бота (deploу). "
                "Автоматически разрешится. Следующее сообщение через 60с."
            )
        return

    if isinstance(error, NetworkError):
        logger.warning(f"Сетевая ошибка (временная): {error}")
        return

    logger.error(f"Ошибка: {error}", exc_info=error)


# ========================
# Точка входа
# ========================

async def _post_shutdown(app) -> None:
    """Корректно закрывает все HTTP-клиенты при остановке бота."""
    await close_client()
    await close_llm_client()
    logger.info("Все HTTP-клиенты закрыты (post_shutdown)")


def main() -> None:
    logger.info("=== GIBDD Telegram Bot запускается ===")

    errors = validate_config()
    if errors:
        print("\nОШИБКИ КОНФИГУРАЦИИ:")
        for err in errors:
            print(f"  x {err}")
        print("\nСоздайте файл .env на основе .env.example и заполните его.")
        sys.exit(1)

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")

    app = Application.builder().token(token).concurrent_updates(True).post_shutdown(_post_shutdown).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("dtp", cmd_dtp))
    app.add_handler(CommandHandler("regions", cmd_regions))

    # Callback-кнопки
    app.add_handler(CallbackQueryHandler(on_callback_query))

    # Текстовые сообщения
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Сообщения с локацией (для статистики по точке)
    app.add_handler(MessageHandler(filters.LOCATION, _handle_location_message))

    # Документы (загрузка камер фотовидеофиксации)
    app.add_handler(MessageHandler(_IsDocument(), _handle_document))

    # Глобальный обработчик ошибок
    app.add_error_handler(error_handler)

    logger.info("Бот запущен. Нажмите Ctrl+C для остановки.")
    print("\nGIBDD-бот запущен!")
    print("  /dtp — выгрузка через кнопки")
    print("  /help — справка")
    print("  Текст — 'Вологодская область за 2025 год'")
    print("  Нажмите Ctrl+C для остановки.\n")

    # Ретрай при запуске: Telegram API может быть временно недоступен
    # с хостинга (ConnectTimeout на get_me). Без ретрая бот крашится
    # и Amvera перезапускает его, создавая Conflict с ещё живым экземпляром.
    _STARTUP_RETRIES = 5
    _STARTUP_DELAYS = [5, 10, 15, 30, 60]

    for attempt in range(1, _STARTUP_RETRIES + 1):
        try:
            app.run_polling(allowed_updates=Update.ALL_TYPES)
            return  # нормальный выход (Ctrl+C или shutdown)
        except (TimedOut, NetworkError) as e:
            if attempt < _STARTUP_RETRIES:
                delay = _STARTUP_DELAYS[attempt - 1]
                logger.warning(
                    f"Telegram API недоступен при запуске ({type(e).__name__}). "
                    f"Попытка {attempt}/{_STARTUP_RETRIES}, повтор через {delay}с..."
                )
                import time as _time
                _time.sleep(delay)
            else:
                logger.error(
                    f"Telegram API недоступен после {_STARTUP_RETRIES} попыток. "
                    f"Останавливаюсь."
                )
                raise


if __name__ == "__main__":
    main()

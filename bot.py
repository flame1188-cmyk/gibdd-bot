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
import logging
import os
import sys
from datetime import datetime

# ============================================================
# SSL: otkluchaem proverku sertifikatov (dlya korporativnogo fayervola)
# Patentiruem httpx DO importa telegram
# ============================================================
import httpx
_orig_async_client_init = httpx.AsyncClient.__init__

def _patched_async_client_init(self, *args, **kwargs):
    kwargs.setdefault('verify', False)
    _orig_async_client_init(self, *args, **kwargs)

httpx.AsyncClient.__init__ = _patched_async_client_init

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import validate_config, ALLOWED_USER_IDS
from api_client import fetch_dtp_data, fetch_regions, extract_accident_cards
from gibdd_parser import build_file1_data, build_file2_data
from excel_generator import generate_both_files, generate_analytics_file
from analytics import (
    calculate_metrics,
    compare_metrics,
    build_analytics_message,
    build_analytics_excel_data,
    get_analytics_column_names,
)
from user_request_parser import (
    parse_user_message,
    parse_period,
    find_region,
    ensure_regions_loaded,
    ParsedPeriod,
)

# ========================
# Настройка логирования
# ========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ========================
# Константы
# ========================

REGIONS_PER_PAGE = 8  # Регионов на одной странице кнопок

MONTH_SHORT = {
    1: "Янв", 2: "Фев", 3: "Мар", 4: "Апр",
    5: "Май", 6: "Июн", 7: "Июл", 8: "Авг",
    9: "Сен", 10: "Окт", 11: "Ноя", 12: "Дек",
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

    # Строка 3: 9 месяцев
    buttons.append([
        InlineKeyboardButton(f"9 месяцев ({MONTH_SHORT[1]}-{MONTH_SHORT[9]})", callback_data=f"p9:{year}"),
    ])

    # Строки 4-5: месяцы (по 6 в строке)
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
        "После выгрузки бот предложит провести анализ —\n"
        "сравнение с аналогичным периодом прошлого года.\n\n"
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
        "После выгрузки данных бот предложит кнопку\n"
        "\U0001F4CA Провести анализ — сравнение текущего\n"
        "периода с аналогичным периодом прошлого года.\n"
        "Результат: текстовое резюме + Excel-файл\n\n"
        "--- Команды ---\n"
        "/dtp — выгрузка через кнопки\n"
        "/regions — список регионов\n"
        "/help — эта справка\n\n"
        "--- Результат ---\n"
        "Бот вернёт 2 Excel-файла:\n"
        "  1. dtp_cards.xlsx — карточки ДТП\n"
        "  2. dtp_uch.xlsx — участники ДТП\n"
        "И при запросе анализа:\n"
        "  3. dtp_analytics.xlsx — аналитика"
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
        text = "Не удалось загрузить список регионов. Попробуйте позже."
        if msg:
            await msg.edit_text(text)
        else:
            await update.callback_query.edit_message_text(text)
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

        # --- Выбор периода: Конкретный месяц ---
        if data.startswith("pm:"):
            parts = data.split(":")
            month = int(parts[1])
            year = int(parts[2])
            month_name = {
                1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
                5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
                9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
            }
            period = ParsedPeriod(
                months=[month],
                year=year,
                label=f"{month_name.get(month, '')} {year}",
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

        # --- Запрос аналитики ---
        if data == "do_analytics":
            await _run_analysis(update, context)
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
                f"Произошла ошибка: {e}\n\nОтправьте /dtp чтобы начать заново."
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
    Поддерживает несколько месяцев (последовательные запросы).
    """
    reg_code = context.user_data.get("reg_code", "")
    reg_name = context.user_data.get("reg_name", "Регион " + reg_code)
    dat_list = period.get_dat_list()
    total_months = len(dat_list)

    await query.edit_message_text(
        f"Выгрузка данных:\n\n"
        f"Регион: {reg_name}\n"
        f"Период: {period.label}\n"
        f"Запросов: {total_months}\n\n"
        f"Подготовка..."
    )

    # Выполняем запросы
    all_cards = []
    errors = []

    for i, dat in enumerate(dat_list, start=1):
        # Обновляем прогресс
        month_num = int(dat.split(".")[0])
        month_name = {
            1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
            5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
            9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
        }.get(month_num, dat)

        progress_bar = _make_progress_bar(i, total_months)
        status_text = (
            f"Выгрузка данных:\n\n"
            f"Регион: {reg_name}\n"
            f"Период: {period.label}\n\n"
            f"{progress_bar} {i}/{total_months}\n"
            f"Запрос: {month_name} {period.year}..."
        )

        try:
            await query.edit_message_text(status_text)
        except Exception:
            pass  # Не критично, если не удалось обновить

        # API-запрос
        try:
            api_response = await fetch_dtp_data(dat=dat, reg=reg_code, pok="1")
            cards = extract_accident_cards(api_response)
            all_cards.extend(cards)
            logger.info(f"  {dat}: {len(cards)} ДТП")
        except Exception as e:
            error_msg = f"{month_name} {period.year}: {e}"
            errors.append(error_msg)
            logger.error(f"  {dat}: ОШИБКА — {e}")

    # Проверяем результат
    if not all_cards and errors:
        error_text = "\n".join(f"- {e}" for e in errors)
        await query.edit_message_text(
            f"Не удалось получить данные.\n\nОшибки:\n{error_text}\n\n"
            f"Попробуйте позже или измените параметры."
        )
        return

    # Обработка и генерация Excel
    try:
        await query.edit_message_text(
            f"Выгрузка данных:\n\n"
            f"Регион: {reg_name}\n"
            f"Период: {period.label}\n\n"
            f"Найдено ДТП: {len(all_cards)}\n"
            f"Генерация Excel-файлов..."
        )

        file1_data = build_file1_data(all_cards)
        file2_data = build_file2_data(all_cards)
        file1_bytes, file2_bytes = generate_both_files(file1_data, file2_data)

        # Отправляем файлы
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_reg = reg_name.replace(" ", "_")[:30]
        filename1 = f"dtp_cards_{safe_reg}_{period.year}_{timestamp}.xlsx"
        filename2 = f"dtp_uch_{safe_reg}_{period.year}_{timestamp}.xlsx"

        await query.edit_message_text("Готово! Отправляю файлы...")

        chat_id = query.message.chat_id

        from telegram import Bot
        bot: Bot = context.bot

        await bot.send_document(
            chat_id=chat_id,
            document=file1_bytes,
            filename=filename1,
            caption=(
                f"Карточки ДТП\n"
                f"{reg_name} | {period.label}\n"
                f"ДТП: {len(all_cards)}"
            ),
        )

        await bot.send_document(
            chat_id=chat_id,
            document=file2_bytes,
            filename=filename2,
            caption=(
                f"Участники ДТП\n"
                f"{reg_name} | {period.label}\n"
                f"Участников: {len(file2_data)}"
            ),
        )

        # Удаляем сообщение о статусе
        try:
            await query.message.delete()
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
    После выгрузки предлагает кнопку для проведения анализа
    (сравнение с аналогичным периодом прошлого года).
    """
    prev_year = period.year - 1
    prev_label = period.label.replace(str(period.year), str(prev_year))

    # Сохраняем данные для аналитики в user_data
    context.user_data["analytics_ready"] = True
    context.user_data["analytics_reg_code"] = reg_code
    context.user_data["analytics_reg_name"] = reg_name
    context.user_data["analytics_period"] = period
    context.user_data["analytics_cards"] = current_cards

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"\U0001F4CA Провести анализ ({prev_label})",
            callback_data="do_analytics",
        )],
    ])

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"\u2705 Выгрузка завершена: {len(current_cards)} ДТП.\n\n"
            f"Хотите провести сравнительный анализ с аналогичным периодом {prev_year} года?"
        ),
        reply_markup=keyboard,
    )


async def _run_analysis(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Выполняет сравнительный анализ текущего периода с прошлым годом.

    1. Берёт карточки текущего периода из user_data
    2. Запрашивает карточки аналогичного периода прошлого года через API
    3. Считает метрики и сравнивает
    4. Отправляет текстовое резюме + Excel-файл аналитики
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

    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"\U0001F4CA Подготовка анализа...\n\n"
            f"Регион: {reg_name}\n"
            f"Текущий период: {current_label}\n"
            f"Сравнение: {prev_label}\n\n"
            f"Загрузка данных за {prev_year} год..."
        ),
    )

    # Запрашиваем данные за прошлый год
    prev_cards = []
    errors = []

    for i, dat in enumerate(dat_list_prev, start=1):
        month_num = int(dat.split(".")[0])
        month_name = {
            1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
            5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
            9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
        }.get(month_num, dat)

        progress = _make_progress_bar(i, len(dat_list_prev))
        await status_msg.edit_text(
            f"\U0001F4CA Загрузка данных за {prev_year} год...\n\n"
            f"{progress} {i}/{len(dat_list_prev)}\n"
            f"Запрос: {month_name} {prev_year}..."
        )

        try:
            api_response = await fetch_dtp_data(dat=dat, reg=reg_code, pok="1")
            cards = extract_accident_cards(api_response)
            prev_cards.extend(cards)
            logger.info(f"  Аналитика: {dat} -> {len(cards)} ДТП")
        except Exception as e:
            errors.append(f"{month_name} {prev_year}: {e}")
            logger.error(f"  Аналитика: {dat} -> ОШИБКА {e}")

    if not prev_cards:
        error_text = "\n".join(f"- {e}" for e in errors) if errors else "Нет данных"
        await status_msg.edit_text(
            f"\u26A0\uFE0F Не удалось загрузить данные за {prev_label}.\n\n"
            f"Ошибки:\n{error_text}\n\n"
            f"Возможно, данные за этот период ещё не опубликованы."
        )
        return

    # Считаем метрики
    await status_msg.edit_text("\U0001F4CA Считаю метрики...")

    current_metrics = calculate_metrics(current_cards)
    previous_metrics = calculate_metrics(prev_cards)
    comparison = compare_metrics(current_metrics, previous_metrics)

    # Формируем текстовое сообщение
    analytics_text = build_analytics_message(
        comparison=comparison,
        reg_name=reg_name,
        current_label=current_label,
        previous_label=prev_label,
    )

    # Генерируем Excel
    analytics_data = build_analytics_excel_data(
        comparison=comparison,
        reg_name=reg_name,
        current_label=current_label,
        previous_label=prev_label,
    )
    column_names = get_analytics_column_names(current_label, prev_label)
    analytics_bytes = generate_analytics_file(analytics_data, column_names)

    # Удаляем сообщение о статусе
    try:
        await status_msg.delete()
    except Exception:
        pass

    # Отправляем текстовое резюме
    await context.bot.send_message(
        chat_id=chat_id,
        text=analytics_text,
        parse_mode="HTML",
    )

    # Отправляем Excel-файл аналитики
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_reg = reg_name.replace(" ", "_")[:30]
    filename = f"dtp_analytics_{safe_reg}_{period.year}_vs_{prev_year}_{timestamp}.xlsx"

    await context.bot.send_document(
        chat_id=chat_id,
        document=analytics_bytes,
        filename=filename,
        caption=(
            f"\U0001F4CA Аналитика: {reg_name}\n"
            f"{current_label} vs {prev_label}\n"
            f"Текущий: {len(current_cards)} ДТП | Прошлый: {len(prev_cards)} ДТП"
        ),
    )

    # Очищаем данные аналитики
    context.user_data.pop("analytics_ready", None)
    context.user_data.pop("analytics_reg_code", None)
    context.user_data.pop("analytics_reg_name", None)
    context.user_data.pop("analytics_period", None)
    context.user_data.pop("analytics_cards", None)

    logger.info(
        f"Аналитика отправлена: {reg_name}, "
        f"{current_label} vs {prev_label}, "
        f"{len(current_cards)} vs {len(prev_cards)} ДТП"
    )


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
        await update.message.reply_text(
            f"Период распознан: {period.label}\n\n"
            f"Но не удалось определить регион.\n"
            f"Укажите название региона или его код.\n\n"
            f"Или используйте /dtp для выбора через кнопки."
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
    logger.error(f"Ошибка: {context.error}", exc_info=context.error)


# ========================
# Точка входа
# ========================

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

    app = Application.builder().token(token).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("dtp", cmd_dtp))
    app.add_handler(CommandHandler("regions", cmd_regions))

    # Callback-кнопки
    app.add_handler(CallbackQueryHandler(on_callback_query))

    # Текстовые сообщения
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Глобальный обработчик ошибок
    app.add_error_handler(error_handler)

    logger.info("Бот запущен. Нажмите Ctrl+C для остановки.")
    print("\nGIBDD-бот запущен!")
    print("  /dtp — выгрузка через кнопки")
    print("  /help — справка")
    print("  Текст — 'Вологодская область за 2025 год'")
    print("  Нажмите Ctrl+C для остановки.\n")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

"""
Генерация двух Excel-файлов на основе данных ДТП:

  1. dtp_cards.xlsx  — одна строка = одно ДТП (все поля карточки)
  2. dtp_uch.xlsx    — одна строка = один участник ДТП
"""

import io
import logging
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

logger = logging.getLogger(__name__)


# ========================
# Стили
# ========================

HEADER_FONT = Font(bold=True, color="FFFFFF", size=10)
HEADER_FILL = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
HEADER_ALIGNMENT = Alignment(horizontal="center", vertical="center", wrap_text=True)

CELL_ALIGNMENT = Alignment(vertical="center", wrap_text=True)
CELL_ALIGNMENT_CENTER = Alignment(horizontal="center", vertical="center")

THIN_BORDER = Border(
    left=Side(style="thin"),
    right=Side(style="thin"),
    top=Side(style="thin"),
    bottom=Side(style="thin"),
)

# ========================
# Вспомогательные функции
# ========================

def _apply_header_style(ws, col_count: int) -> None:
    """Применяет стили к строке заголовков."""
    for col_idx in range(1, col_count + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGNMENT
        cell.border = THIN_BORDER


def _apply_data_styles(ws, row_count: int, col_count: int) -> None:
    """Применяет стили к ячейкам с данными."""
    for row_idx in range(2, row_count + 2):
        for col_idx in range(1, col_count + 1):
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.alignment = CELL_ALIGNMENT
            cell.border = THIN_BORDER


def _auto_width(ws, col_count: int, max_width: int = 40) -> None:
    """Автоподбор ширины колонок с ограничением."""
    for col_idx in range(1, col_count + 1):
        column_letter = ws.cell(row=1, column=col_idx).column_letter
        max_len = len(str(ws.cell(row=1, column=col_idx).value or ""))

        # Проверяем первые 50 строк для определения ширины
        check_rows = min(ws.max_row, 51)
        for row_idx in range(2, check_rows):
            cell_val = ws.cell(row=row_idx, column=col_idx).value
            if cell_val:
                cell_len = len(str(cell_val))
                if cell_len > max_len:
                    max_len = cell_len

        adjusted_width = min(max_len + 3, max_width)
        ws.column_dimensions[column_letter].width = max(adjusted_width, 8)


def _create_workbook(
    column_names: list[str],
    data_rows: list[dict[str, str]],
) -> Workbook:
    """
    Создаёт объект Workbook с заголовками и данными.

    Args:
        column_names: Список названий колонок (порядок важен)
        data_rows: Список словарей {название_колонки: значение}
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Данные"

    # Заголовки
    for col_idx, col_name in enumerate(column_names, start=1):
        ws.cell(row=1, column=col_idx, value=col_name)

    # Данные
    for row_idx, row_data in enumerate(data_rows, start=2):
        for col_idx, col_name in enumerate(column_names, start=1):
            value = row_data.get(col_name, "")
            ws.cell(row=row_idx, column=col_idx, value=value)

    col_count = len(column_names)
    row_count = len(data_rows)

    # Стили заголовков
    _apply_header_style(ws, col_count)

    # Стили данных
    _apply_data_styles(ws, row_count, col_count)

    # Автоподбор ширины
    _auto_width(ws, col_count)

    # Заморозка заголовков (чтобы при прокрутке шапка оставалась видна)
    ws.freeze_panes = "A2"

    # Авторазмер листа по содержимому
    if row_count > 0:
        ws.auto_filter.ref = ws.dimensions

    return wb


def workbook_to_bytes(wb: Workbook) -> bytes:
    """Сериализует Workbook в байты xlsx-файла."""
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer.getvalue()


# ========================
# Публичные функции
# ========================

def generate_file1(file1_data: list[dict[str, str]]) -> bytes:
    """
    Генерирует Файл 1: одна строка = одно ДТП.

    Args:
        file1_data: Данные от gibdd_parser.build_file1_data()

    Returns:
        Байты xlsx-файла
    """
    from gibdd_parser import get_file1_column_names

    column_names = get_file1_column_names()
    wb = _create_workbook(column_names, file1_data)
    return workbook_to_bytes(wb)


def generate_file2(file2_data: list[dict[str, str]]) -> bytes:
    """
    Генерирует Файл 2: одна строка = один участник ДТП.

    Args:
        file2_data: Данные от gibdd_parser.build_file2_data()

    Returns:
        Байты xlsx-файла
    """
    from gibdd_parser import get_file2_column_names

    column_names = get_file2_column_names()
    wb = _create_workbook(column_names, file2_data)
    return workbook_to_bytes(wb)


def generate_both_files(
    file1_data: list[dict[str, str]],
    file2_data: list[dict[str, str]],
) -> tuple[bytes, bytes]:
    """
    Генерирует оба Excel-файла.

    Returns:
        (file1_bytes, file2_bytes)
    """
    logger.info(f"Генерация Excel: Файл 1 — {len(file1_data)} ДТП, Файл 2 — {len(file2_data)} участников")
    file1_bytes = generate_file1(file1_data)
    file2_bytes = generate_file2(file2_data)
    logger.info(f"Файл 1: {len(file1_bytes)} байт, Файл 2: {len(file2_bytes)} байт")
    return file1_bytes, file2_bytes

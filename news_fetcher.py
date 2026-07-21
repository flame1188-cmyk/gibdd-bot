"""
Модуль поиска новостей из открытых источников для контекста LLM.

Использует RSS-ленты Google News и DuckDuckGo для поиска
новостей о ДТП по указанному региону и периоду.

Работает через httpx без JavaScript и дополнительных зависимостей.
"""

import logging
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# Таймаут для запросов к новостным источникам
_NEWS_TIMEOUT = 15
# Быстрый таймаут для соединения (Google News заблокирован с Amvera)
_NEWS_CONNECT_TIMEOUT = 5

# Флаги: источник недоступен, не пробовать до перезапуска бота
_google_down = False
_ddg_down = False


def _build_search_query(reg_name: str, current_label: str, prev_label: str) -> str:
    """
    Строит поисковый запрос для новостей о ДТП.

    Args:
        reg_name: Название региона (например "Волгоградская область")
        current_label: Текущий период (например "I квартал 2026")
        prev_label: Предыдущий период (например "I квартал 2025")

    Returns:
        Строка поискового запроса
    """
    # Извлекаем год и период из labels
    years = set()
    for label in (current_label, prev_label):
        # Ищем год в формате XXXX
        match = re.search(r"\b(20\d{2})\b", label)
        if match:
            years.add(match.group(1))

    year_str = " ".join(sorted(years))

    # Формируем запрос
    query = f"{reg_name} ДТП {year_str}".strip()
    return query


async def _fetch_google_news_rss(query: str, max_results: int = 10) -> list[dict[str, str]]:
    """
    Получает новости через Google News RSS.

    URL: https://news.google.com/rss/search?q=...&hl=ru&gl=RU&ceid=RU:ru

    Returns:
        Список словарей с полями: title, source, date, url, snippet
    """
    global _google_down
    if _google_down:
        return []

    url = (
        f"https://news.google.com/rss/search?"
        f"q={quote(query)}&hl=ru&gl=RU&ceid=RU:ru"
    )

    logger.info(f"Google News RSS запрос: {query}")

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_NEWS_TIMEOUT, connect=_NEWS_CONNECT_TIMEOUT),
            follow_redirects=True,
        ) as client:
            response = await client.get(url)

        if response.status_code != 200:
            logger.warning(f"Google News вернул {response.status_code}")
            return []

        # Парсим RSS XML
        root = ET.fromstring(response.content)

        results = []
        for item in root.iter("item"):
            if len(results) >= max_results:
                break

            entry: dict[str, str] = {}

            # Заголовок
            title_el = item.find("title")
            if title_el is not None and title_el.text:
                entry["title"] = title_el.text.strip()
            else:
                continue

            # Ссылка
            link_el = item.find("link")
            if link_el is not None and link_el.text:
                entry["url"] = link_el.text.strip()

            # Источник и дата из <source>
            source_el = item.find("source")
            if source_el is not None:
                entry["source"] = source_el.text.strip() if source_el.text else ""
                # Дата может быть в атрибуте
                pub_date = source_el.get("data-timestamp") or ""
                if pub_date:
                    try:
                        from datetime import datetime
                        ts = int(pub_date) / 1000
                        entry["date"] = datetime.fromtimestamp(ts).strftime("%d.%m.%Y")
                    except (ValueError, OSError):
                        entry["date"] = ""

            # Дата публикации из <pubDate>
            if "date" not in entry:
                pub_date_el = item.find("pubDate")
                if pub_date_el is not None and pub_date_el.text:
                    entry["date"] = pub_date_el.text.strip()

            # Сниппет/описание
            desc_el = item.find("description")
            if desc_el is not None and desc_el.text:
                # Google News description часто содержит HTML теги — чистим
                text = desc_el.text
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"\s+", " ", text).strip()
                entry["snippet"] = text[:300]  # Ограничиваем длину
            else:
                entry["snippet"] = ""

            results.append(entry)

        logger.info(f"Google News: найдено {len(results)} новостей")
        return results

    except ET.ParseError as e:
        logger.warning(f"Ошибка парсинга RSS: {e}")
        return []
    except httpx.TimeoutException:
        logger.warning("Google News: таймаут запроса")
        return []
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        logger.warning(f"Google News: подключение недоступно: {e}")
        _google_down = True  # не пробовать до перезапуска
        return []
    except Exception as e:
        logger.warning(f"Google News: ошибка {type(e).__name__}: {e}")
        return []


async def _fetch_duckduckgo_html(query: str, max_results: int = 10) -> list[dict[str, str]]:
    """
    Получает результаты поиска через DuckDuckGo HTML.

    URL: https://html.duckduckgo.com/html/?q=...

    Returns:
        Список словарей с полями: title, source, snippet
    """
    global _ddg_down
    if _ddg_down:
        return []

    url = f"https://html.duckduckgo.com/html/?q={quote(query)}"

    logger.info(f"DuckDuckGo запрос: {query}")

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_NEWS_TIMEOUT, connect=_NEWS_CONNECT_TIMEOUT),
            follow_redirects=True,
        ) as client:
            response = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )

        if response.status_code == 202:
            # Bot detection — не пробовать до перезапуска
            logger.warning("DuckDuckGo: HTTP 202 (bot detection), отключаем источник")
            _ddg_down = True
            return []

        if response.status_code != 200:
            logger.warning(f"DuckDuckGo вернул {response.status_code}")
            return []

        html = response.text
        results = []

        # DuckDuckGo HTML результаты — простая структура
        # Заголовки: <a class="result__a" href="...">title</a>
        # Сниппеты: <a class="result__snippet" ...>snippet</a>
        # Источник: <span class="result__url">source</span>

        # Ищем блоки результатов
        blocks = re.split(r'<div class="result\s', html)[1:]  # пропускаем первый (до первого результата)

        for block in blocks[:max_results]:
            entry: dict[str, str] = {}

            # Заголовок
            title_match = re.search(r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', block, re.DOTALL)
            if title_match:
                entry["url"] = html.unescape(title_match.group(1))
                entry["title"] = re.sub(r"<[^>]+>", "", title_match.group(2)).strip()
            else:
                continue

            if not entry.get("title"):
                continue

            # Сниппет
            snippet_match = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.DOTALL)
            if snippet_match:
                entry["snippet"] = re.sub(r"<[^>]+>", "", snippet_match.group(1)).strip()
                entry["snippet"] = re.sub(r"\s+", " ", entry["snippet"]).strip()
            else:
                entry["snippet"] = ""

            # Источник
            source_match = re.search(r'class="result__url"[^>]*>(.*?)</span>', block, re.DOTALL)
            if source_match:
                entry["source"] = re.sub(r"<[^>]+>", "", source_match.group(1)).strip()
            else:
                entry["source"] = ""

            entry["date"] = ""
            results.append(entry)

        logger.info(f"DuckDuckGo: найдено {len(results)} результатов")
        return results

    except httpx.TimeoutException:
        logger.warning("DuckDuckGo: таймаут запроса")
        return []
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        logger.warning(f"DuckDuckGo: подключение недоступно: {e}")
        _ddg_down = True
        return []
    except Exception as e:
        logger.warning(f"DuckDuckGo: ошибка {type(e).__name__}: {e}")
        return []


async def fetch_news_context(
    reg_name: str,
    current_label: str,
    prev_label: str,
    max_results: int = 8,
) -> str:
    """
    Получает контекст из новостей о ДТП для указанного региона и периода.

    Пытается получить новости из нескольких источников.
    Возвращает форматированный текст для вставки в промпт LLM.

    Args:
        reg_name: Название региона
        current_label: Текущий период
        prev_label: Предыдущий период
        max_results: Максимум новостей на источник

    Returns:
        Текстовый блок с новостями или пустую строку
    """
    query = _build_search_query(reg_name, current_label, prev_label)
    if not query:
        return ""

    all_results: list[dict[str, str]] = []

    # 1. Google News RSS (лучшее качество для русского контента)
    google_results = await _fetch_google_news_rss(query, max_results)
    all_results.extend(google_results)

    # 2. DuckDuckGo как дополнение (если Google вернул мало)
    if len(all_results) < 5:
        ddg_query = f"{query} авария ДТП происшествие"
        ddg_results = await _fetch_duckduckgo_html(ddg_query, max_results)
        # Не дублируем по заголовку
        existing_titles = {r.get("title", "") for r in all_results}
        for r in ddg_results:
            if r.get("title", "") not in existing_titles:
                all_results.append(r)

    if not all_results:
        logger.info("Новости не найдены ни в одном источнике")
        return ""

    # Форматируем результат
    lines = []
    lines.append("КОНТЕКСТ ИЗ ОТКРЫТЫХ ИСТОЧНИКОВ:")
    lines.append(f"Поисковый запрос: \"{query}\"")
    lines.append("")

    for i, result in enumerate(all_results[:max_results], 1):
        title = result.get("title", "")
        source = result.get("source", "")
        date = result.get("date", "")
        snippet = result.get("snippet", "")

        line = f"{i}. {title}"
        if source or date:
            attrs = []
            if source:
                attrs.append(source)
            if date:
                attrs.append(date)
            line += f" — {', '.join(attrs)}"
        lines.append(line)

        if snippet:
            # Ограничиваем длину сниппета
            if len(snippet) > 200:
                snippet = snippet[:200] + "..."
            lines.append(f"   {snippet}")
        lines.append("")

    text = "\n".join(lines)
    logger.info(f"Новости для LLM: {len(all_results)} результатов, {len(text)} символов")
    return text

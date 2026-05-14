"""
HTTP-клиент для работы с Open Data API stat.gibdd.ru (ГИБДД).

Документация API:
  Данные ДТП:  /opendataapi/v1/kartdtp/rows
  Справочники: /opendataapi/v1/dictionary/rows
"""

import logging
from typing import Any

import httpx

from config import HTTP_PROXY, HTTPS_PROXY, TARGET_API_TIMEOUT

# Базовый URL API ГИБДД (кириллический домен через punycode)
GIBDD_BASE_URL = "http://xn--80a7adb.xn--90adear.xn--p1ai"

logger = logging.getLogger(__name__)


def _get_proxy_config() -> dict[str, str] | None:
    """Возвращает конфигурацию прокси, если он задан."""
    if HTTP_PROXY or HTTPS_PROXY:
        return {
            "http://": HTTP_PROXY,
            "https://": HTTPS_PROXY,
        }
    return None


async def fetch_dtp_data(
    dat: str,
    reg: str,
    pok: str = "1",
    dor: str | None = None,
) -> dict[str, Any]:
    """
    Получает данные ДТП с API stat.gibdd.ru.

    Args:
        dat: Дата в формате м.гггг (например, "2.2024")
        reg: Код региона (например, "1101"). Код "1100" не допустим.
        pok: Код показателя аварийности (по умолчанию "1" — все ДТП)
        dor: Код федеральной дороги (опционально)

    Returns:
        Словарь с ответом API

    Raises:
        httpx.HTTPStatusError: при ошибке HTTP
        ValueError: при неверных параметрах
    """
    # Валидация параметров
    if reg == "1100":
        raise ValueError('Код региона "1100" не допустим. Укажите конкретный регион.')

    params: dict[str, str] = {
        "pok": pok,
        "dat": dat,
        "reg": reg,
    }
    if dor:
        params["dor"] = dor

    url = f"{GIBDD_BASE_URL}/opendataapi/v1/kartdtp/rows"
    proxy = _get_proxy_config()

    logger.info(f"Запрос к API ГИБДД: {url} с параметрами {params}")

    async with httpx.AsyncClient(proxy=proxy, timeout=TARGET_API_TIMEOUT, verify=False) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()

    data = response.json()

    if data.get("status") != 200:
        raise ValueError(f"API вернул ошибку: status={data.get('status')}, {data}")

    return data


async def fetch_dictionary(code: int) -> dict[str, Any]:
    """
    Получает справочник с API stat.gibdd.ru.

    Args:
        code: Код справочника:
              1 — Регионы Российской Федерации
              2 — Показатели аварийности
              3 — Федеральные дороги

    Returns:
        Словарь с ответом API
    """
    url = f"{GIBDD_BASE_URL}/opendataapi/v1/dictionary/rows"
    proxy = _get_proxy_config()

    params = {"code": str(code)}
    logger.info(f"Запрос справочника: code={code}")

    async with httpx.AsyncClient(proxy=proxy, timeout=TARGET_API_TIMEOUT, verify=False) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()

    return response.json()


async def fetch_regions() -> list[dict[str, str]]:
    """Получает справочник регионов (code=1). Возвращает список {code, name}."""
    data = await fetch_dictionary(1)
    rows = data.get("results", [{}])[0].get("dict_rows", [])
    return [{"code": r["rows_code"], "name": r["rows_name"]} for r in rows]


async def fetch_indicators() -> list[dict[str, str]]:
    """Получает справочник показателей аварийности (code=2). Возвращает список {code, name}."""
    data = await fetch_dictionary(2)
    rows = data.get("results", [{}])[0].get("dict_rows", [])
    return [{"code": r["rows_code"], "name": r["rows_name"]} for r in rows]


async def fetch_federal_roads() -> list[dict[str, str]]:
    """Получает справочник федеральных дорог (code=3). Возвращает список {code, name}."""
    data = await fetch_dictionary(3)
    rows = data.get("results", [{}])[0].get("dict_rows", [])
    return [{"code": r["rows_code"], "name": r["rows_name"]} for r in rows]


def extract_accident_cards(api_response: dict) -> list[dict[str, Any]]:
    """
    Извлекает список карточек ДТП из ответа API.

    Реальная структура ответа API stat.gibdd.ru:
      response["results"]["region_list"][0]["pok_list"][0]["result"][0]["dtpcardlist"]["info_dtp"]

    Returns:
        Список словарей — карточек ДТП
    """
    cards: list[dict[str, Any]] = []

    try:
        # results — это DICT, не LIST!
        results = api_response.get("results", {})
        if isinstance(results, dict):
            region_list = results.get("region_list", [])
        elif isinstance(results, list):
            # Fallback: если вдруг API изменит формат
            region_list = results[0].get("region_list", []) if results else []
        else:
            region_list = []

        for region in region_list:
            pok_list = region.get("pok_list", [])
            for pok_item in pok_list:
                result_list = pok_item.get("result", [])
                for result in result_list:
                    card_list = result.get("dtpcardlist", {})
                    info_dtp = card_list.get("info_dtp", [])
                    cards.extend(info_dtp)
    except (KeyError, TypeError, AttributeError) as e:
        logger.error(f"Ошибка парсинга структуры ответа API: {e}")
        raise ValueError(f"Неожиданная структура ответа API: {e}")
    except httpx.HTTPStatusError as e:
    logger.error(f"Ошибка HTTP {e.response.status_code}: {e.response.text[:200]}")
    except httpx.RequestError as e:
    logger.error(f"Ошибка запроса: {type(e).__name__}: {e}")
    except Exception as e:
    logger.error(f"Ошибка: {type(e).__name__}: {e}")
    logger.info(f"Извлечено {len(cards)} карточек ДТП")
    return cards

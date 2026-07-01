"""
HTTP-клиент для работы с Open Data API stat.gibdd.ru (ГИБДД).

Документация API:
  Данные ДТП:  /opendataapi/v1/kartdtp/rows
  Справочники: /opendataapi/v1/dictionary/rows

Архитектура:
  - Персистентный httpx.AsyncClient с connection pooling и keep-alive.
    Вместо нового TCP-соединения на каждый запрос — переиспользуем
    установленное соединение. Это критично на Amvera, где новые
    TCP-подключения к stat.gibdd.ru часто таймаутятся.
  - Ретраи с экспоненциальной задержкой при сетевых ошибках.
  - Автоматическая пауза между запросами (min 1с) для защиты
    от rate-limiting на стороне GИБДД.
"""

import asyncio
import logging
import time as _time
from typing import Any

import httpx

from config import HTTP_PROXY, HTTPS_PROXY, TARGET_API_TIMEOUT

# Подавляем мусорные SSL-предупреждения от verify=False
logging.getLogger("httpx").setLevel(logging.WARNING)

# Базовый URL API ГИБДД (кириллический домен через punycode)
GIBDD_BASE_URL = "http://xn--80a7adb.xn--90adear.xn--p1ai"

# ========================
# Настройки ретраев
# ========================
MAX_RETRIES = 3              # для справочников (маленькие запросы, быстрые)
MAX_RETRIES_LARGE = 3        # для kartdtp (большой payload, таймауты на Amvera МСК_0)
RETRY_BACKOFF_BASE = 5       # секунды между ретрайами для справочников (5, 10, 20...)
RETRY_BACKOFF_BASE_LARGE = 10 # секунды между ретрайами для kartdtp (10, 20, 40...)

# ========================
# Защита от rate-limit
# ========================
MIN_REQUEST_INTERVAL = 1.0   # минимальная пауза между запросами (секунды)

logger = logging.getLogger(__name__)

# ========================
# Персистентный HTTP-клиент (connection pooling)
# ========================
_shared_client: httpx.AsyncClient | None = None
_last_request_time: float = 0.0


def _get_proxy_config() -> dict[str, str] | None:
    """Возвращает конфигурацию прокси, если он задан."""
    if HTTP_PROXY or HTTPS_PROXY:
        return {
            "http://": HTTP_PROXY,
            "https://": HTTPS_PROXY,
        }
    return None


async def get_client() -> httpx.AsyncClient:
    """Возвращает единственный экземпляр httpx.AsyncClient для всего приложения.
    TCP-соединение переиспользуется между запросами (keep-alive)."""
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        proxy = _get_proxy_config()
        _shared_client = httpx.AsyncClient(
            proxy=proxy,
            verify=False,
            timeout=httpx.Timeout(
                connect=60,
                read=TARGET_API_TIMEOUT,
                write=30,
                pool=30,
            ),
            limits=httpx.Limits(
                max_connections=5,
                max_keepalive_connections=3,
                keepalive_expiry=120,
            ),
            # HTTP/1.1 keep-alive (GIBDD не поддерживает HTTP/2)
            http2=False,
        )
        logger.info("Создан новый HTTP-клиент (connection pooling)")
    return _shared_client


async def close_client() -> None:
    """Закрывает HTTP-клиент. Вызывать при остановке бота."""
    global _shared_client
    if _shared_client is not None and not _shared_client.is_closed:
        await _shared_client.aclose()
        _shared_client = None
        logger.info("HTTP-клиент закрыт")


async def _throttle() -> None:
    """Добавляет паузу между запросами, чтобы не превысить rate-limit GИБДД."""
    global _last_request_time
    now = _time.monotonic()
    elapsed = now - _last_request_time
    if elapsed < MIN_REQUEST_INTERVAL:
        wait = MIN_REQUEST_INTERVAL - elapsed
        logger.debug(f"Rate-limit: пауза {wait:.1f}с")
        await asyncio.sleep(wait)
    _last_request_time = _time.monotonic()


def _classify_error(e: Exception) -> str:
    """Классифицирует ошибку для понятного логирования и отображения пользователю."""
    if isinstance(e, httpx.ReadTimeout):
        return "таймаут чтения (сервер не успел передать данные)"
    if isinstance(e, httpx.WriteTimeout):
        return "таймаут отправки запроса"
    if isinstance(e, httpx.ConnectTimeout):
        return "таймаут подключения (сервер недоступен)"
    if isinstance(e, httpx.PoolTimeout):
        return "таймаут ожидания свободного соединения"
    if isinstance(e, httpx.ConnectError):
        return "ошибка подключения (сервер недоступен или заблокирован)"
    if isinstance(e, httpx.RemoteProtocolError):
        return "ошибка протокола (сервер разорвал соединение)"
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    return f"{type(e).__name__}: {e}"


def error_brief(e: Exception) -> str:
    """Краткое описание ошибки для логов и сообщений пользователю.
    Вынесено в модульный уровень, чтобы bot.py мог переиспользовать."""
    cls = _classify_error(e)
    # Если исключение обёрнуто (ConnectionError от _request_with_retries),
    # раскрываем вложенную причину
    if e.__cause__ is not None:
        cls += f" ({_classify_error(e.__cause__)})"
    return cls


async def _request_with_retries(
    url: str,
    params: dict[str, str],
    description: str,
    connect_timeout: int | None = None,
    read_timeout: int | None = None,
    max_retries: int | None = None,
    backoff_base: int | None = None,
) -> httpx.Response:
    """
    Выполняет GET-запрос с ретраями и экспоненциальной задержкой.
    Использует общий пул соединений (keep-alive).

    Args:
        url: URL запроса
        params: Параметры запроса
        description: Описание запроса для логов
        connect_timeout: Таймаут подключения (секунды). По умолчанию 60.
        read_timeout: Таймаут чтения (секунды). По умолчанию TARGET_API_TIMEOUT.
        max_retries: Количество попыток. По умолчанию MAX_RETRIES (3).
        backoff_base: Базовая задержка между ретрайами.

    Returns:
        httpx.Response

    Raises:
        ConnectionError: после исчерпания всех ретраев
        httpx.HTTPStatusError: при HTTP-ошибке (без ретраев)
    """
    client = await get_client()
    retries = max_retries if max_retries is not None else MAX_RETRIES
    bo_base = backoff_base if backoff_base is not None else RETRY_BACKOFF_BASE

    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        await _throttle()

        try:
            logger.info(f"{description} | попытка {attempt}/{retries}")
            response = await client.get(url, params=params)
            response.raise_for_status()
            logger.info(
                f"{description} | успех на попытке {attempt} | "
                f"статус={response.status_code} | "
                f"размер={len(response.content)} байт"
            )
            return response

        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            error_desc = _classify_error(e)
            last_error = e
            logger.warning(f"{description} | попытка {attempt}/{retries} | {error_desc}")
            # НЕ закрываем клиент! httpx сам помечает нерабочие соединения
            # в пуле и создаст новое при следующем запросе.
            # close_client() здесь убивал connection pooling — каждый ретрай
            # открывал новый TCP-сокет, что на Amvera вызывало rate-limit.

        except httpx.HTTPStatusError as e:
            error_desc = _classify_error(e)
            body_preview = e.response.text[:300] if e.response.text else "пусто"
            logger.error(
                f"{description} | попытка {attempt} | {error_desc} | тело={body_preview}"
            )
            raise

        except Exception as e:
            last_error = e
            logger.error(
                f"{description} | попытка {attempt}/{retries} | "
                f"неожиданная ошибка: {type(e).__name__}: {e}"
            )

        # Задержка перед следующим ретраем (экспоненциальная)
        if attempt < retries:
            wait = bo_base * (2 ** (attempt - 1))
            logger.info(f"{description} | ожидание {wait}с перед повторной попыткой...")
            await asyncio.sleep(wait)

    # Все ретраи исчерпаны
    error_desc = _classify_error(last_error) if last_error else "неизвестная ошибка"
    raise ConnectionError(
        f"Не удалось выполнить запрос после {retries} попыток. "
        f"Последняя ошибка: {error_desc}"
    ) from last_error


async def fetch_dtp_data(
    dat: str,
    reg: str,
    pok: str = "1",
    dor: str | None = None,
) -> dict[str, Any]:
    """
    Получает данные ДТП с API stat.gibdd.ru с автоматическими ретраями.

    Args:
        dat: Дата в формате м.гггг (например, "2.2024")
        reg: Код региона (например, "1101"). Код "1100" не допустим.
        pok: Код показателя аварийности (по умолчанию "1" — все ДТП)
        dor: Код федеральной дороги (опционально)

    Returns:
        Словарь с ответом API

    Raises:
        httpx.HTTPStatusError: при ошибке HTTP (без ретраев)
        ConnectionError: при исчерпании всех ретраев (таймаут, подключение)
        ValueError: при неверных параметрах или ответе API
    """
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
    desc = f"Запрос ДТП dat={dat} reg={reg}"

    logger.info(f"Запрос к API ГИБДД: {url} с параметрами {params}")

    response = await _request_with_retries(
        url, params, desc,
        max_retries=MAX_RETRIES_LARGE,
        backoff_base=RETRY_BACKOFF_BASE_LARGE,
    )

    data = response.json()

    if data.get("status") != 200:
        raise ValueError(f"API вернул ошибку: status={data.get('status')}, {data}")

    return data


async def fetch_dictionary(code: int) -> dict[str, Any]:
    """
    Получает справочник с API stat.gibdd.ru с автоматическими ретраями.

    Args:
        code: Код справочника:
              1 — Регионы Российской Федерации
              2 — Показатели аварийности
              3 — Федеральные дороги

    Returns:
        Словарь с ответом API
    """
    url = f"{GIBDD_BASE_URL}/opendataapi/v1/dictionary/rows"
    params = {"code": str(code)}
    desc = f"Запрос справочника code={code}"

    logger.info(f"Запрос справочника: code={code}, url={url}, proxy={'да' if _get_proxy_config() else 'нет'}")

    response = await _request_with_retries(url, params, desc)

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


async def check_api_availability() -> tuple[bool, str]:
    """
    Проверяет доступность API ГИБДД быстрым запросом справочника.

    Returns:
        (доступен, описание_проблемы). Если доступен — описание пустое.
    """
    url = f"{GIBDD_BASE_URL}/opendataapi/v1/dictionary/rows"
    params = {"code": "1"}

    try:
        t0 = _time.monotonic()
        client = await get_client()
        response = await client.get(url, params=params)
        response.raise_for_status()
        elapsed = _time.monotonic() - t0
        logger.info(f"Проверка доступности API: ОК за {elapsed:.1f}с")
        return True, ""
    except httpx.ConnectError:
        msg = "API ГИБДД недоступен: не удаётся установить соединение."
        logger.error(f"Проверка доступности API: {msg}")
        return False, msg
    except httpx.ConnectTimeout:
        msg = "API ГИБДД недоступен: таймаут подключения."
        logger.error(f"Проверка доступности API: {msg}")
        return False, msg
    except httpx.ReadTimeout:
        msg = "API ГИБДД: соединение установлено, но сервер не отвечает."
        logger.error(f"Проверка доступности API: {msg}")
        return False, msg
    except httpx.HTTPStatusError as e:
        msg = f"API ГИБДД вернул HTTP {e.response.status_code}."
        logger.error(f"Проверка доступности API: {msg}")
        return False, msg
    except Exception as e:
        msg = f"API ГИБДД недоступен: {type(e).__name__}: {e}"
        logger.error(f"Проверка доступности API: {msg}")
        return False, msg


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
        results = api_response.get("results", {})
        if isinstance(results, dict):
            region_list = results.get("region_list", [])
        elif isinstance(results, list):
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

    logger.info(f"Извлечено {len(cards)} карточек ДТП")
    return cards
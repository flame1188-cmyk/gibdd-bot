# GIBDD Telegram Bot

Telegram-бот для выгрузки данных ДТП с сайта stat.gibdd.ru (Открытые данные ГИБДД).

Бот делает API-запрос к stat.gibdd.ru и возвращает **2 Excel-файла**:
1. **dtp_cards.xlsx** — карточки ДТП (1 строка = 1 ДТП)
2. **dtp_uch.xlsx** — участники ДТП (1 строка = 1 участник)

## Быстрый старт

### 1. Установка

```bash
# Создайте виртуальное окружение
python -m venv venv

# Активируйте его
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate

# Установите зависимости
pip install -r requirements.txt
```

Или на Windows просто запустите `install.bat`.

### 2. Настройка

```bash
cp .env.example .env
```

Заполните в `.env`:
- `TELEGRAM_BOT_TOKEN` — получите у [@BotFather](https://t.me/BotFather)
- При необходимости — `HTTP_PROXY` / `HTTPS_PROXY` для корпоративной сети

### 3. Запуск

```bash
python bot.py
```

Или на Windows — `run_bot.bat`.

## Формат запроса

Отправьте боту сообщение:

```
<дата> <регион>
```

| Параметр | Формат | Пример | Описание |
|----------|--------|--------|----------|
| `dat` | м.гггг | 2.2024 | Месяц и год |
| `reg` | 4 цифры | 1101 | Код региона РФ |

Примеры:
```
2.2024 1101
12.2023 1115
dat=1.2024 reg=77
```

## Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Приветственное сообщение |
| `/help` | Справка по использованию |
| `/regions` | Список регионов с кодами |

## Структура проекта

| Файл | Назначение |
|------|-----------|
| `bot.py` | Telegram-бот: обработка команд, приём/отправка сообщений |
| `config.py` | Конфигурация (.env, переменные окружения) |
| `api_client.py` | HTTP-запросы к API stat.gibdd.ru (ДТП + справочники) |
| `gibdd_parser.py` | Парсинг JSON-ответа → структура для 2 Excel-файлов |
| `excel_generator.py` | Генерация стилизованных .xlsx файлов |
| `.env` | Секретные ключи (не коммитить!) |
| `.env.example` | Шаблон конфигурации |

## API stat.gibdd.ru

| Эндпоинт | Назначение |
|----------|-----------|
| `/opendataapi/v1/kartdtp/rows` | Данные ДТП (параметры: dat, reg, pok) |
| `/opendataapi/v1/dictionary/rows?code=1` | Справочник регионов |
| `/opendataapi/v1/dictionary/rows?code=2` | Показатели аварийности |
| `/opendataapi/v1/dictionary/rows?code=3` | Федеральные дороги |

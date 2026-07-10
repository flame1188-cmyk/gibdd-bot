"""
Скрипт для парсинга регионов с сайта stat.gibdd.ru и генерации
хардкод-файла regions_builtin.py с полным справочником.

Запуск: python parse_regions.py
"""

import json
import re
import urllib.request

GIBDD_URL = "http://stat.gibdd.ru"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def fetch_page():
    req = urllib.request.Request(GIBDD_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def extract_regions(html):
    """Извлекает regions[] и regId2MiasId из HTML."""
    # Ищем var regions = [...];
    m = re.search(r'var regions = (\[.*?\]);', html, re.DOTALL)
    if not m:
        raise ValueError("Не удалось найти var regions в HTML")
    regions = json.loads(m.group(1))

    # Ищем var regId2MiasId = {...};
    m = re.search(r'var regId2MiasId = (\{.*?\});', html, re.DOTALL)
    if not m:
        raise ValueError("Не удалось найти var regId2MiasId в HTML")
    id_map = json.loads(m.group(1))

    return regions, id_map


def build_api_regions(regions, id_map):
    """
    Конвертирует в формат API: [{"code": "1119", "name": "..."}, ...]

    Правило: API-код = "11" + MiasId (с ведущим нулём до 2 цифр)
    Для MiasId > 99 (автономные округа, новые территории) —
    пропускаем, т.к. их API-коды неизвестны (отличаются от веб-кодов).
    Они будут доступны после восстановления API через файловый кэш.
    """
    api_regions = []
    skipped = []

    for r in regions:
        reg_id = str(r["regId"])
        name = r["name"]

        # Пропускаем "Российская Федерация" (877)
        if reg_id == "877":
            continue

        mias_id = id_map.get(reg_id)
        if mias_id is None:
            print(f"  ⚠ Нет miasId для {name} (regId={reg_id})")
            continue

        # Только стандартные коды (1-99) → API-код "11XX"
        if mias_id < 1 or mias_id > 99:
            skipped.append((mias_id, name))
            continue

        api_code = f"11{mias_id:02d}"
        api_regions.append({"code": api_code, "name": name})

    # Сортируем по коду
    api_regions.sort(key=lambda x: x["code"])

    if skipped:
        print(f"\n  Пропущено {len(skipped)} регионов (miasId вне 1-99):")
        for mid, name in skipped:
            print(f"    miasId={mid} — {name}")

    return api_regions


def generate_python_file(api_regions, output_path):
    """Генерирует Python-файл с хардкод-списком регионов."""
    lines = [
        '"""',
        'Встроенный (builtin) справочник регионов Российской Федерации.',
        '',
        'Извлечён из stat.gibdd.ru. Используется как fallback,',
        'когда API ГИБДД недоступен и файловый кэш пуст.',
        '',
        'Коды в формате API: "11" + двухзначный код региона.',
        '"""',
        '',
        'BUILTIN_REGIONS: list[dict[str, str]] = [',
    ]

    for r in api_regions:
        lines.append(f'    {{"code": "{r["code"]}", "name": "{r["name"]}"}},')

    lines.append(']')
    lines.append('')
    lines.append(f'# Всего регионов: {len(api_regions)}')
    lines.append('')

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"✓ Сгенерирован {output_path}: {len(api_regions)} регионов")


def main():
    print("Загрузка stat.gibdd.ru...")
    html = fetch_page()
    print(f"  HTML: {len(html)} символов")

    print("Извлечение данных...")
    regions, id_map = extract_regions(html)
    print(f"  regions: {len(regions)} записей")
    print(f"  regId2MiasId: {len(id_map)} записей")

    print("Конвертация в API-формат...")
    api_regions = build_api_regions(regions, id_map)
    print(f"  API регионов: {len(api_regions)}")

    # Выводим первые 10 для проверки
    print("\nПервые 10 регионов:")
    for r in api_regions[:10]:
        print(f"  {r['code']} — {r['name']}")

    print("\nАвтономные округа (miasId > 99):")
    for r in api_regions:
        code_num = r["code"][2:]
        if len(code_num) > 2:
            print(f"  {r['code']} — {r['name']}")

    output_path = "regions_builtin.py"
    generate_python_file(api_regions, output_path)

    # Также выводим JSON для проверки
    json_path = "regions_builtin.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(api_regions, f, ensure_ascii=False, indent=2)
    print(f"✓ Сохранён {json_path}")


if __name__ == "__main__":
    main()
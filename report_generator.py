"""
Генератор самодостаточных HTML-отчётов с картами и графиками.

Каждый отчёт — один .html файл без внешних зависимостей.
Библиотеки (Leaflet, ECharts) встраиваются в файл.

Типы отчётов:
  - dtp_map:       Карта всех ДТП с попапами (опционально + камеры)
  - analytics:     Аналитический отчёт с графиками ECharts
  - clusters:      Карта очагов с зонами, предочагами, камерами
  - point_stats:   Карта точки с радиусом, ДТП и камерами
"""

import hashlib
import io
import json
import logging
import os
from datetime import datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Директория для кэша библиотек (persistence на Amvera)
_DATA_DIR = os.environ.get("CAMERA_DATA_DIR", "data")
_LIB_DIR = os.path.join(_DATA_DIR, "report_libs")

# CDN-URL библиотек
_LIB_URLS = {
    "leaflet.css": "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
    "leaflet.js": "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
    "leaflet.markercluster.css": "https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css",
    "leaflet.markercluster.js": "https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js",
    "leaflet.measure.css": "https://cdnjs.cloudflare.com/ajax/libs/leaflet-measure/3.1.0/leaflet.measure.css",
    "leaflet.measure.js": "https://cdnjs.cloudflare.com/ajax/libs/leaflet-measure/3.1.0/leaflet.measure.js",
    "echarts.min.js": "https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js",
}

# Цвета тяжести ДТП
_COLOR_FATAL = "#d32f2f"      # погибло — красный
_COLOR_INJURED = "#f57c00"    # ранено — оранжевый
_COLOR_MATERIAL = "#4caf50"   # только материальный — зелёный
_COLOR_NO_COORDS = "#9e9e9e"  # без координат

# Цвета очагов по количеству ДТП
_CLUSTER_COLORS = {
    10: "#d32f2f",   # 10+ — красный
    6: "#f57c00",    # 6-9 — оранжевый
    3: "#fbc02d",    # 3-5 — жёлтый
}


# ========================
# Кэш библиотек
# ========================

def _ensure_lib(name: str, url: str) -> str:
    """
    Скачивает библиотеку с CDN при первом запуске,
    кэширует в _LIB_DIR. Возвращает содержимое как строку.

    Примечание: используется синхронный httpx.get, т.к. метод вызывается
    из синхронного кода генератора. На практике библиотеки кэшируются
    после первого вызова и повторные скачивания не нужны.
    """
    os.makedirs(_LIB_DIR, exist_ok=True)
    path = os.path.join(_LIB_DIR, name)

    if os.path.exists(path):
        logger.debug(f"report_generator: библиотека из кэша {name}")
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    logger.info(f"report_generator: скачивание {name}...")
    try:
        # Используем синхронный клиент с ограниченным таймаутом.
        # Потокоблокировка допустима: это происходит один раз при
        # первом запросе, далее — чтение из дискового кэша.
        with httpx.Client(follow_redirects=True, timeout=30) as client:
            resp = client.get(url)
        resp.raise_for_status()
        content = resp.text
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"report_generator: {name} закэширован ({len(content)} симв.)")
        return content
    except Exception as e:
        logger.error(f"report_generator: не удалось скачать {name}: {e}")
        return ""


def _get_embedded_libs() -> dict[str, str]:
    """Загружает и возвращает все библиотеки для встраивания."""
    libs = {}
    for name, url in _LIB_URLS.items():
        libs[name] = _ensure_lib(name, url)
    return libs


# ========================
# Утилиты
# ========================

def _safe_float(val, default=0.0) -> float:
    """Безопасное преобразование строки/числа в float."""
    if val is None:
        return default
    try:
        v = float(val)
        return v if v != 0 else default
    except (ValueError, TypeError):
        return default


def _parse_coords(card: dict) -> tuple[float, float] | None:
    """Извлекает (lat, lon) из карточки. Возвращает None если нет координат."""
    lat = _safe_float(card.get("coord_w"))
    lon = _safe_float(card.get("coord_l"))
    if lat == 0.0 and lon == 0.0:
        return None
    return (lat, lon)


def _severity_color(card: dict) -> str:
    """Цвет маркера по тяжести ДТП."""
    pog = int(card.get("pog", 0) or 0)
    ran = int(card.get("ran", 0) or 0)
    if pog > 0:
        return _COLOR_FATAL
    if ran > 0:
        return _COLOR_INJURED
    return _COLOR_MATERIAL


def _address_text(card: dict) -> str:
    """Формирует адрес из полей карточки."""
    parts = []
    dor = (card.get("dor") or "").strip()
    km = (card.get("km") or "").strip()
    m = (card.get("m") or "").strip()
    np = (card.get("np") or "").strip()
    street = (card.get("street") or "").strip()
    house = (card.get("house") or "").strip()
    district = (card.get("district") or "").strip()

    if np:
        parts.append(f"н.п. {np}")
    if street:
        parts.append(f"ул. {street}")
    if house:
        parts.append(f"д. {house}")
    if dor:
        road_part = dor
        if km:
            road_part += f", {km} км"
            if m:
                road_part += f" + {m} м"
        parts.append(road_part)
    if district and not np:
        parts.append(f"({district})")

    return ", ".join(parts) if parts else "Адрес не указан"


def _cluster_color(count: int) -> str:
    """Цвет очага по количеству ДТП."""
    for threshold, color in sorted(_CLUSTER_COLORS.items(), reverse=True):
        if count >= threshold:
            return color
    return "#4caf50"


def _card_popup_html(card: dict) -> str:
    """HTML попапа для карточки ДТП."""
    date_dtp = card.get("date_dtp", "")
    time_dtp = card.get("time", "")
    dtpv = card.get("dtpv", "")
    address = _address_text(card)
    pog = int(card.get("pog", 0) or 0)
    ran = int(card.get("ran", 0) or 0)
    k_ts = card.get("k_ts", "?")
    empt_number = card.get("empt_number", "")

    # Транспортные средства
    ts_parts = []
    for ts in (card.get("ts_info") or []):
        marka = ts.get("marka_ts", "")
        model = ts.get("m_ts", "")
        ts_parts.append(f"{marka} {model}".strip())
    ts_str = " + ".join(ts_parts) if ts_parts else ""

    lines = [
        f"<b>{dtpv}</b>" if dtpv else "",
        f"📅 {date_dtp} {time_dtp}",
        f"📍 {address}",
    ]
    if pog > 0:
        lines.append(f"💀 Погибло: {pog}")
    if ran > 0:
        lines.append(f"🏥 Ранено: {ran}")
    if ts_str:
        lines.append(f"🚗 {ts_str}")
    if empt_number:
        lines.append(f"📋 № {empt_number}")

    # Координаты
    lat_val = card.get("coord_w", "")
    lon_val = card.get("coord_l", "")
    if lat_val and lon_val:
        lines.append(f"🌐 {lat_val}, {lon_val}")

    # Погодные и дорожные условия
    dor_usl = card.get("dor_usl") or {}
    spog = dor_usl.get("spog", []) or []
    if spog:
        lines.append(f"🌤 Погода: {'; '.join(str(s) for s in spog)}")
    s_pch = dor_usl.get("s_pch", "")
    if s_pch:
        lines.append(f"🛣 Покрытие: {s_pch}")
    osv = dor_usl.get("osv", "")
    if osv:
        lines.append(f"💡 Освещение: {osv}")

    # Дорожные условия (перекрёсток, переход и т.д.)
    sdor = dor_usl.get("sdor", []) or []
    if sdor:
        lines.append(f"🚦 Дорожные условия: {'; '.join(str(s) for s in sdor)}")
    obj_dtp = dor_usl.get("obj_dtp", []) or []
    if obj_dtp:
        lines.append(f"🏗 Объекты УДС: {'; '.join(str(s) for s in obj_dtp)}")

    # Нарушения ПДД
    all_npdd = []
    all_sop_npdd = []
    for ts in (card.get("ts_info") or []):
        for uch in (ts.get("ts_uch", []) or []):
            npdd = uch.get("npdd", []) or []
            if npdd:
                all_npdd.extend(str(n) for n in npdd)
            sop = uch.get("sop_npdd", []) or []
            if sop:
                all_sop_npdd.extend(str(n) for n in sop)
    # Участники без ТС
    for uch in (card.get("uch_info") or []):
        npdd = uch.get("npdd", []) or []
        if npdd:
            all_npdd.extend(str(n) for n in npdd)
        sop = uch.get("sop_npdd", []) or []
        if sop:
            all_sop_npdd.extend(str(n) for n in sop)
    if all_npdd:
        lines.append(f"⚠️ Нарушения: {'; '.join(all_npdd)}")
    if all_sop_npdd:
        lines.append(f"📌 Сопутствующие: {'; '.join(all_sop_npdd)}")

    return "<br>".join(l for l in lines if l)


def _camera_popup_html(cam: dict) -> str:
    """HTML попапа для камеры фотовидеофиксации."""
    address = cam.get("address", "")
    model = cam.get("model", "")
    number = cam.get("number", "")
    road_num = cam.get("road_num", "")
    road_name = cam.get("road_name", "")
    piket = cam.get("piket")

    lines = [f"<b>📷 {model}</b>" if model else "<b>📷 Камера</b>"]
    if address:
        lines.append(f"📍 {address}")
    if number:
        lines.append(f"🔢 № {number}")
    road = ""
    if road_num:
        road = road_num
    if road_name:
        road = f"{road} «{road_name}»" if road else f"«{road_name}»"
    if road:
        lines.append(f"🛣 Маршрут: {road}")
    if piket is not None:
        lines.append(f"📏 Пикетаж: {piket:.3f} км")
    lat = cam.get("lat", 0)
    lon = cam.get("lon", 0)
    if lat and lon:
        lines.append(f"🌐 {lat:.6f}, {lon:.6f}")

    return "<br>".join(lines)


# ========================
# Главный класс
# ========================

class ReportGenerator:
    """
    Генератор самодостаточного HTML-файла с картой и/или графиками.

    Usage::

        gen = ReportGenerator(
            region_name="Вологодская область",
            period_label="Январь-Май 2026",
        )
        html = gen.generate_dtp_map(cards, cameras=cameras_list)
        # html — полная строка HTML, готовая к отправке
    """

    def __init__(
        self,
        region_name: str,
        period_label: str,
    ):
        self.region_name = region_name
        self.period_label = period_label
        self._libs: dict[str, str] | None = None

    def _libs_loaded(self) -> dict[str, str]:
        """Ленивая загрузка библиотек."""
        if self._libs is None:
            self._libs = _get_embedded_libs()
        return self._libs

    # --------------------------------------------------
    # Общий каркас HTML
    # --------------------------------------------------

    def _html_shell(
        self,
        body_content: str,
        use_map: bool = True,
        use_echarts: bool = False,
        custom_css: str = "",
    ) -> str:
        """Оборачивает содержимое в полный HTML-документ."""
        libs = self._libs_loaded()

        head_parts = []

        if use_map:
            leaflet_css = libs.get("leaflet.css", "")
            head_parts.append(f"<style>{leaflet_css}</style>")
            leaflet_js = libs.get("leaflet.js", "")
            head_parts.append(f"<script>{leaflet_js}</script>")
            # MarkerCluster
            mc_css = libs.get("leaflet.markercluster.css", "")
            if mc_css:
                head_parts.append(f"<style>{mc_css}</style>")
            mc_js = libs.get("leaflet.markercluster.js", "")
            if mc_js:
                head_parts.append(f"<script>{mc_js}</script>")
            # Measure
            ms_css = libs.get("leaflet.measure.css", "")
            if ms_css:
                head_parts.append(f"<style>{ms_css}</style>")
            ms_js = libs.get("leaflet.measure.js", "")
            if ms_js:
                head_parts.append(f"<script>{ms_js}</script>")

        if use_echarts:
            echarts_js = libs.get("echarts.min.js", "")
            head_parts.append(f"<script>{echarts_js}</script>")

        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        title = f"ДТП — {self.region_name} — {self.period_label}"

        return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
{"".join(head_parts)}
<style>
{self._base_css()}
{custom_css}
</style>
</head>
<body>
<div class="container">
  <header class="header">
    <h1> ДТП — {self._esc(self.region_name)}</h1>
    <p class="subtitle">{self._esc(self.period_label)}</p>
    <p class="generated">Сгенерировано: {now}</p>
  </header>
  {body_content}
</div>
</body>
</html>"""

    # --------------------------------------------------
    # CSS
    # --------------------------------------------------

    def _base_css(self) -> str:
        """Базовые стили отчёта."""
        return """
body {
  margin: 0;
  padding: 0;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
    'Helvetica Neue', Arial, sans-serif;
  background: #f5f5f5;
  color: #212121;
}
.container {
  max-width: 1200px;
  margin: 0 auto;
  padding: 16px;
}
.header {
  background: #1565c0;
  color: white;
  padding: 20px 24px;
  border-radius: 8px;
  margin-bottom: 16px;
}
.header h1 {
  margin: 0 0 4px 0;
  font-size: 22px;
  font-weight: 600;
}
.subtitle {
  margin: 0 0 4px 0;
  font-size: 15px;
  opacity: 0.9;
}
.generated {
  margin: 0;
  font-size: 12px;
  opacity: 0.7;
}
.map-container {
  background: white;
  border-radius: 8px;
  overflow: hidden;
  margin-bottom: 16px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.12);
}
.map-container .map-title {
  padding: 12px 16px;
  font-weight: 600;
  font-size: 15px;
  border-bottom: 1px solid #e0e0e0;
}
#map {
  height: 600px;
  width: 100%;
}
.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
.summary-card {
  background: white;
  border-radius: 8px;
  padding: 16px;
  text-align: center;
  box-shadow: 0 1px 3px rgba(0,0,0,0.12);
}
.summary-card .value {
  font-size: 28px;
  font-weight: 700;
  line-height: 1.2;
}
.summary-card .label {
  font-size: 13px;
  color: #757575;
  margin-top: 4px;
}
.summary-card.fatal .value { color: #d32f2f; }
.summary-card.injured .value { color: #f57c00; }
.summary-card.total .value { color: #1565c0; }
.summary-card.cameras .value { color: #2e7d32; }
.chart-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
  gap: 16px;
  margin-bottom: 16px;
}
.chart-box {
  background: white;
  border-radius: 8px;
  overflow: hidden;
  box-shadow: 0 1px 3px rgba(0,0,0,0.12);
}
.chart-box .chart-title {
  padding: 12px 16px;
  font-weight: 600;
  font-size: 14px;
  border-bottom: 1px solid #e0e0e0;
}
.legend {
  background: white;
  border-radius: 8px;
  padding: 12px 16px;
  margin-bottom: 16px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.12);
  font-size: 13px;
}
.legend-item {
  display: inline-block;
  margin-right: 16px;
  vertical-align: middle;
}
.legend-dot {
  display: inline-block;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  margin-right: 4px;
  vertical-align: middle;
}
.filter-panel {
  background: white;
  border-radius: 8px;
  padding: 12px 16px;
  margin-bottom: 12px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.12);
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
  font-size: 13px;
}
.filter-panel .filter-title {
  font-weight: 600;
  margin-right: 4px;
  font-size: 14px;
}
.filter-group {
  display: flex;
  align-items: center;
  gap: 4px;
}
.filter-group label {
  color: #616161;
  white-space: nowrap;
}
.filter-group select,
.filter-group input[type="date"] {
  padding: 5px 8px;
  border: 1px solid #bdbdbd;
  border-radius: 4px;
  font-size: 13px;
  background: #fafafa;
}
.filter-group select:focus,
.filter-group input[type="date"]:focus {
  outline: none;
  border-color: #1565c0;
}
.btn-filter {
  padding: 5px 14px;
  border: none;
  border-radius: 4px;
  font-size: 13px;
  cursor: pointer;
  font-weight: 500;
}
.btn-apply {
  background: #1565c0;
  color: white;
}
.btn-apply:hover { background: #0d47a1; }
.btn-reset {
  background: #e0e0e0;
  color: #424242;
}
.btn-reset:hover { background: #bdbdbd; }
.filter-count {
  color: #757575;
  font-size: 12px;
  margin-left: 4px;
}
.filter-divider {
  width: 1px;
  height: 24px;
  background: #e0e0e0;
  margin: 0 4px;
}
.multi-select {
  position: relative;
  display: inline-block;
}
.multi-select-trigger {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 5px 8px;
  border: 1px solid #bdbdbd;
  border-radius: 4px;
  font-size: 13px;
  background: #fafafa;
  cursor: pointer;
  min-width: 180px;
  user-select: none;
}
.multi-select-trigger:hover {
  border-color: #1565c0;
}
.multi-select-arrow {
  font-size: 10px;
  margin-left: 8px;
}
.multi-select-dropdown {
  display: none;
  position: absolute;
  top: 100%;
  left: 0;
  z-index: 1000;
  background: white;
  border: 1px solid #bdbdbd;
  border-radius: 4px;
  margin-top: 2px;
  max-height: 220px;
  overflow-y: auto;
  min-width: 220px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.15);
}
.multi-select-dropdown.open {
  display: block;
}
.multi-select-dropdown label {
  display: flex;
  align-items: center;
  padding: 4px 8px;
  cursor: pointer;
  font-size: 13px;
  white-space: nowrap;
}
.multi-select-dropdown label:hover {
  background: #f5f5f5;
}
.multi-select-dropdown input[type="checkbox"] {
  margin-right: 6px;
}
/* --- Панель поиска по координатам --- */
.coord-search {
  background: white;
  border-radius: 8px;
  padding: 10px 16px;
  margin-bottom: 12px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.12);
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  flex-wrap: wrap;
}
.coord-search .cs-label {
  font-weight: 600;
  font-size: 14px;
  white-space: nowrap;
}
.coord-search .cs-input {
  padding: 5px 10px;
  border: 1px solid #bdbdbd;
  border-radius: 4px;
  font-size: 13px;
  width: 260px;
  background: #fafafa;
}
.coord-search .cs-input:focus {
  outline: none;
  border-color: #1565c0;
}
.coord-search .btn-cs {
  padding: 5px 14px;
  border: none;
  border-radius: 4px;
  font-size: 13px;
  cursor: pointer;
  font-weight: 500;
  background: #1565c0;
  color: white;
}
.coord-search .btn-cs:hover { background: #0d47a1; }
.coord-search .btn-cs-clear {
  background: #e0e0e0;
  color: #424242;
}
.coord-search .btn-cs-clear:hover { background: #bdbdbd; }
.coord-search .cs-hint {
  color: #9e9e9e;
  font-size: 11px;
}
@media print {
  body { background: white; }
  .container { max-width: 100%; padding: 0; }
  .map-container { box-shadow: none; border: 1px solid #ccc; }
  #map { height: 500px; }
  .filter-panel { display: none; }
}
.camera-cluster-icon {
  background: rgba(46, 125, 50, 0.7);
  border-radius: 50%;
  width: 36px; height: 36px;
  display: flex; align-items: center; justify-content: center;
  color: white; font-weight: 700; font-size: 13px;
  box-shadow: 0 2px 6px rgba(0,0,0,0.3);
}
"""

    @staticmethod
    def _esc(text: str) -> str:
        """HTML-экранирование."""
        return (text
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))

    # --------------------------------------------------
    # 1. Карта ДТП (сценарий 1)
    # --------------------------------------------------

    def generate_dtp_map(
        self,
        cards: list[dict],
        cameras: list[dict] | None = None,
    ) -> str:
        """
        Генерирует HTML-файл с картой всех ДТП.
        Опционально с маркерами камер.
        """
        # Фильтруем карточки с координатами
        cards_with_coords = [
            c for c in cards if _parse_coords(c) is not None
        ]
        cards_no_coords = len(cards) - len(cards_with_coords)

        # Статистика
        total = len(cards)
        pog_total = sum(int(c.get("pog", 0) or 0) for c in cards)
        ran_total = sum(int(c.get("ran", 0) or 0) for c in cards)

        # Сводка
        summary_html = self._build_summary(
            total=total,
            deaths=pog_total,
            injured=ran_total,
            no_coords=cards_no_coords,
            cameras=len(cameras) if cameras else 0,
        )

        # Легенда
        legend_html = self._build_dtp_legend()

        # Уникальные виды ДТП для фильтра
        dtp_types = sorted(set(
            c.get("dtpv", "") for c in cards_with_coords if c.get("dtpv")
        ))
        # Уникальные модели камер для фильтра
        cam_models = sorted(set(
            c.get("model", "") for c in (cameras or []) if c.get("model")
        )) if cameras else []

        # Панель фильтров
        filter_html = self._build_filter_panel(
            dtp_types, cam_models,
            total_on_map=len(cards_with_coords),
        )

        # Данные для карты
        dtp_geojson = self._build_dtp_geojson(cards_with_coords)
        camera_markers = self._build_camera_markers_js(cameras) if cameras else "[]"
        center = self._calc_center(cards_with_coords)
        zoom = self._calc_zoom(cards_with_coords)

        # JS-код карты
        map_js = self._dtp_map_js(
            center, zoom, dtp_geojson, camera_markers,
            has_cameras=cameras is not None,
        )

        body = f"""
{summary_html}
{legend_html}
{filter_html}
{self._build_coord_search_html()}
<div class="map-container">
  <div class="map-title">Карта ДТП — {self._esc(self.region_name)}</div>
  <div id="map"></div>
</div>
<script>
{map_js}
</script>
"""

        return self._html_shell(body, use_map=True)

    def _build_summary(
        self,
        total: int,
        deaths: int,
        injured: int,
        no_coords: int = 0,
        cameras: int = 0,
    ) -> str:
        cards_html = f"""
<div class="summary-grid">
  <div class="summary-card total">
    <div class="value">{total}</div>
    <div class="label">Всего ДТП</div>
  </div>
  <div class="summary-card fatal">
    <div class="value">{deaths}</div>
    <div class="label">Погибло</div>
  </div>
  <div class="summary-card injured">
    <div class="value">{injured}</div>
    <div class="label">Ранено</div>
  </div>"""
        if cameras > 0:
            cards_html += f"""
  <div class="summary-card cameras">
    <div class="value">{cameras}</div>
    <div class="label">Камер на карте</div>
  </div>"""
        if no_coords > 0:
            cards_html += f"""
  <div class="summary-card">
    <div class="value">{no_coords}</div>
    <div class="label">Без координат</div>
  </div>"""
        cards_html += "\n</div>"
        return cards_html

    def _build_dtp_legend(self) -> str:
        return """
<div class="legend">
  <div style="font-weight:600;margin-bottom:6px;">Условные обозначения</div>
  <table style="border-collapse:collapse;font-size:13px;">
    <tr><td style="padding:2px 8px 2px 0;"><span class="legend-dot" style="background:#d32f2f"></span></td><td>ДТП с погибшими</td></tr>
    <tr><td style="padding:2px 8px 2px 0;"><span class="legend-dot" style="background:#f57c00"></span></td><td>ДТП с ранеными</td></tr>
    <tr><td style="padding:2px 8px 2px 0;font-size:16px;">📷</td><td>Камера фотовидеофиксации</td></tr>
  </table>
  <div style="margin-top:6px;color:#757575;font-size:12px;">Нажмите на маркер для подробностей. Колёсико мыши / +/- — масштаб.</div>
</div>"""

    def _build_dtp_geojson(self, cards: list[dict]) -> str:
        """Строит GeoJSON FeatureCollection из карточек ДТП.
        Каждая фича содержит свойства для фильтрации.
        """
        features = []
        for card in cards:
            coords = _parse_coords(card)
            if coords is None:
                continue
            popup = _card_popup_html(card)

            # Тяжесть для фильтра
            pog = int(card.get("pog", 0) or 0)
            ran = int(card.get("ran", 0) or 0)
            if pog > 0:
                severity = "fatal"
            elif ran > 0:
                severity = "injured"
            else:
                severity = "material"

            # Дата для фильтра (DD.MM.YYYY -> YYYYMMDD)
            date_sort = ""
            date_str = card.get("date_dtp", "")
            if "." in date_str:
                parts = date_str.split(".")
                if len(parts) == 3:
                    try:
                        date_sort = f"{parts[2]}{parts[1]}{parts[0]}"
                    except (ValueError, IndexError):
                        pass

            features.append({
                "type": "Feature",
                "properties": {
                    "popup": popup,
                    "color": _severity_color(card),
                    "dtpv": card.get("dtpv", ""),
                    "severity": severity,
                    "date_sort": date_sort,
                },
                "geometry": {
                    "type": "Point",
                    "coordinates": [coords[1], coords[0]],
                },
            })
        return json.dumps({
            "type": "FeatureCollection",
            "features": features,
        }, ensure_ascii=False)

    def _build_camera_markers_js(self, cameras: list[dict]) -> str:
        """Строит JSON-массив маркеров камер для JS."""
        markers = []
        for cam in cameras:
            lat = cam.get("lat", 0)
            lon = cam.get("lon", 0)
            if lat == 0 and lon == 0:
                continue
            popup = _camera_popup_html(cam)
            markers.append({
                "lat": lat,
                "lon": lon,
                "popup": popup,
                "model": cam.get("model", ""),
            })
        return json.dumps(markers, ensure_ascii=False)

    def _build_filter_panel(
        self,
        dtp_types: list[str],
        cam_models: list[str] | None = None,
        total_on_map: int = 0,
    ) -> str:
        """Строит HTML-панель фильтров для карты ДТП."""
        # Опции вида ДТП
        type_opts = '<option value="">Все виды</option>'
        for t in dtp_types:
            type_opts += f'<option value="{self._esc(t)}">{self._esc(t)}</option>'

        html = f"""
<div class="filter-panel">
  <span class="filter-title">🔍 Фильтр ДТП:</span>
  <div class="filter-group">
    <label>Вид:</label>
    <select id="filter_type">{type_opts}</select>
  </div>
  <div class="filter-group">
    <label>Тяжесть:</label>
    <select id="filter_severity">
      <option value="">Все</option>
      <option value="fatal">С погибшими</option>
      <option value="injured">С ранеными</option>
      <option value="material">Материальный ущерб</option>
    </select>
  </div>
  <div class="filter-group">
    <label>С:</label>
    <input type="date" id="filter_date_from">
  </div>
  <div class="filter-group">
    <label>По:</label>
    <input type="date" id="filter_date_to">
  </div>
  <button class="btn-filter btn-apply" id="filter_apply">Применить</button>
  <button class="btn-filter btn-reset" id="filter_reset">Сбросить</button>
  <span class="filter-count" id="filter_count">{total_on_map} ДТП</span>"""

        # Фильтр камер по модели
        if cam_models:
            cb_items = ""
            for m in cam_models:
                cb_items += f'<label><input type="checkbox" value="{self._esc(m)}" onchange="applyDtpCameraFilter()"> {self._esc(m)}</label>\n    '
            html += f"""
  <span class="filter-divider"></span>
  <span class="filter-title">📷 Камеры:</span>
  <div class="multi-select" id="camera_model_multi">
    <div class="multi-select-trigger" onclick="toggleMultiSelect('camera_model_multi')">
      <span class="multi-select-label">Все модели</span>
      <span class="multi-select-arrow">▼</span>
    </div>
    <div class="multi-select-dropdown">
    {cb_items}
    </div>
  </div>
  <span class="filter-count" id="camera_filter_count"></span>"""

        html += "\n</div>"
        return html

    @staticmethod
    def _build_coord_search_html() -> str:
        """Панель поиска по координатам (HTML + JS). Одна строка ввода."""
        return """
<div class="coord-search">
  <span class="cs-label">📍 Координаты:</span>
  <input type="text" id="cs_input" class="cs-input" placeholder="59.1234, 39.5678">
  <button class="btn-cs" onclick="goToCoords()">Перейти</button>
  <button class="btn-cs btn-cs-clear" onclick="clearCoordSearch()">Сбросить вид</button>
  <span class="cs-hint">Вставьте: широта, долгота — Enter для поиска</span>
</div>
<script>
(function() {
  document.getElementById('cs_input').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      goToCoords();
    }
  });
})();

function parseCoordInput(val) {
  // Поддерживаем разделители: запятая, пробел, табуляция, точка с запятой
  // Также обрабатываем копирование из Excel (часто табуляция между ячейками)
  var parts = val.trim().split(/[,\\s;]+/);
  if (parts.length >= 2) {
    return { lat: parseFloat(parts[0]), lon: parseFloat(parts[1]) };
  }
  return null;
}

function goToCoords() {
  var input = document.getElementById('cs_input');
  var parsed = parseCoordInput(input.value);
  if (!parsed || isNaN(parsed.lat) || isNaN(parsed.lon)) {
    input.style.borderColor = '#d32f2f';
    setTimeout(function() { input.style.borderColor = ''; }, 1500);
    return;
  }
  var lat = parsed.lat, lon = parsed.lon;
  if (lat < -90 || lat > 90 || lon < -180 || lon > 180) {
    input.style.borderColor = '#d32f2f';
    setTimeout(function() { input.style.borderColor = ''; }, 1500);
    return;
  }
  if (window._searchMarker) map.removeLayer(window._searchMarker);
  window._searchMarker = L.circleMarker([lat, lon], {
    radius: 10, fillColor: '#e91e63', color: '#880e4f',
    weight: 3, fillOpacity: 0.9
  }).bindPopup('<b>Поиск:</b><br>' + lat.toFixed(5) + ', ' + lon.toFixed(5)).addTo(map);
  map.setView([lat, lon], 16);
}

function clearCoordSearch() {
  if (window._searchMarker) {
    map.removeLayer(window._searchMarker);
    window._searchMarker = null;
  }
  document.getElementById('cs_input').value = '';
}
</script>
"""

    def _calc_center(
        self, cards: list[dict],
    ) -> tuple[float, float]:
        """Вычисляет центр карты по медиане координат."""
        if not cards:
            return (55.0, 40.0)
        lats = []
        lons = []
        for c in cards:
            coords = _parse_coords(c)
            if coords:
                lats.append(coords[0])
                lons.append(coords[1])
        if not lats:
            return (55.0, 40.0)
        lats.sort()
        lons.sort()
        mid = len(lats) // 2
        return (lats[mid], lons[mid])

    def _calc_zoom(self, cards: list[dict]) -> int:
        """Определяет начальный зум по разбросу координат."""
        if not cards:
            return 5
        lats = []
        lons = []
        for c in cards:
            coords = _parse_coords(c)
            if coords:
                lats.append(coords[0])
                lons.append(coords[1])
        if not lats:
            return 5
        lat_span = max(lats) - min(lats)
        lon_span = max(lons) - min(lons)
        span = max(lat_span, lon_span)
        if span > 20:
            return 5
        if span > 10:
            return 6
        if span > 5:
            return 7
        if span > 2:
            return 8
        if span > 1:
            return 9
        if span > 0.3:
            return 11
        return 12

    def _dtp_map_js(
        self,
        center: tuple[float, float],
        zoom: int,
        dtp_geojson: str,
        camera_markers_js: str,
        has_cameras: bool,
    ) -> str:
        """JavaScript-код карты ДТП с фильтрами."""
        return f"""
var map = L.map('map').setView([{center[0]}, {center[1]}], {zoom});

L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    attribution: '&copy; OpenStreetMap contributors',
    maxZoom: 19,
}}).addTo(map);

// --- Линейка ---
L.control.measure({{
    position: 'topleft',
    primaryLengthUnit: 'meters',
    secondaryLengthUnit: 'kilometers',
    localization: {{
    }}
}}).addTo(map);

// --- Данные ---
var dtpData = {dtp_geojson};
var cameraDataFull = {camera_markers_js};

// --- MarkerCluster для ДТП ---
var dtpCluster = L.markerClusterGroup({{
    maxClusterRadius: 40,
    spiderfyOnMaxZoom: true,
    showCoverageOnHover: false,
    iconCreateFunction: function(cluster) {{
        var count = cluster.getChildCount();
        var size = count < 10 ? 'small' : count < 100 ? 'medium' : 'large';
        return L.divIcon({{
            html: '<div><span>' + count + '</span></div>',
            className: 'marker-cluster marker-cluster-' + size,
            iconSize: L.point(40, 40)
        }});
    }}
}});

function renderDtp(data) {{
    dtpCluster.clearLayers();
    L.geoJSON(data, {{
        pointToLayer: function(feature, latlng) {{
            return L.circleMarker(latlng, {{
                radius: 6,
                fillColor: feature.properties.color,
                color: '#333',
                weight: 1,
                fillOpacity: 0.8,
            }});
        }},
        onEachFeature: function(feature, layer) {{
            layer.bindPopup(feature.properties.popup, {{maxWidth: 320}});
        }}
    }}).addTo(dtpCluster);
}}

// Первичная отрисовка всех ДТП
renderDtp(dtpData);
dtpCluster.addTo(map);

// --- Фильтр ДТП ---
function applyDtpFilter() {{
    var typeVal = document.getElementById('filter_type').value;
    var sevVal  = document.getElementById('filter_severity').value;
    var dFrom   = document.getElementById('filter_date_from').value;
    var dTo     = document.getElementById('filter_date_to').value;

    var from8 = dFrom ? dFrom.replace(/-/g, '') : '';
    var to8   = dTo   ? dTo.replace(/-/g, '')   : '';

    var filtered = {{
        "type": "FeatureCollection",
        "features": dtpData.features.filter(function(f) {{
            if (typeVal && f.properties.dtpv !== typeVal) return false;
            if (sevVal  && f.properties.severity !== sevVal) return false;
            if (from8  && f.properties.date_sort < from8) return false;
            if (to8    && f.properties.date_sort > to8)   return false;
            return true;
        }})
    }};
    renderDtp(filtered);
    document.getElementById('filter_count').textContent =
        filtered.features.length + ' ДТП';
}}

document.getElementById('filter_apply').addEventListener('click', applyDtpFilter);
document.getElementById('filter_reset').addEventListener('click', function() {{
    document.getElementById('filter_type').value = '';
    document.getElementById('filter_severity').value = '';
    document.getElementById('filter_date_from').value = '';
    document.getElementById('filter_date_to').value = '';
    renderDtp(dtpData);
    document.getElementById('filter_count').textContent =
        dtpData.features.length + ' ДТП';
}});

// --- Слой камер (кластеризация) ---
var cameraIcon = L.divIcon({{
    html: '<div style="font-size:18px;text-align:center;line-height:1;">📷</div>',
    iconSize: [24, 24],
    iconAnchor: [12, 12],
    className: ''
}});
var cameraCluster = L.markerClusterGroup({{
    maxClusterRadius: 40,
    spiderfyOnMaxZoom: true,
    showCoverageOnHover: false,
    iconCreateFunction: function(cluster) {{
        var count = cluster.getChildCount();
        return L.divIcon({{
            html: '<div class="camera-cluster-icon"><span>' + count + '</span></div>',
            className: '',
            iconSize: L.point(36, 36)
        }});
    }}
}});

function renderCameras(data) {{
    cameraCluster.clearLayers();
    data.forEach(function(c) {{
        L.marker([c.lat, c.lon], {{icon: cameraIcon}})
         .bindPopup(c.popup, {{maxWidth: 320}})
         .addTo(cameraCluster);
    }});
}}

renderCameras(cameraDataFull);
cameraCluster.addTo(map);

// --- Множественный выбор моделей камер ---
function toggleMultiSelect(id) {{
    var dd = document.querySelector('#' + id + ' .multi-select-dropdown');
    dd.classList.toggle('open');
}}
document.addEventListener('click', function(e) {{
    if (!e.target.closest('.multi-select')) {{
        document.querySelectorAll('.multi-select-dropdown.open').forEach(function(d) {{
            d.classList.remove('open');
        }});
    }}
}});
function getSelectedModels(id) {{
    var cbs = document.querySelectorAll('#' + id + ' .multi-select-dropdown input:checked');
    return Array.from(cbs).map(function(cb) {{ return cb.value; }});
}}
function updateMultiSelectLabel(id) {{
    var sel = getSelectedModels(id);
    var lbl = document.querySelector('#' + id + ' .multi-select-label');
    if (sel.length === 0) {{
        lbl.textContent = 'Все модели';
    }} else {{
        lbl.textContent = sel.length + ' выбрано';
    }}
}}
function applyDtpCameraFilter() {{
    updateMultiSelectLabel('camera_model_multi');
    var selected = getSelectedModels('camera_model_multi');
    var filtered = cameraDataFull.filter(function(c) {{
        return selected.length === 0 || selected.indexOf(c.model) !== -1;
    }});
    renderCameras(filtered);
    var cntEl = document.getElementById('camera_filter_count');
    if (cntEl) cntEl.textContent = filtered.length + ' из ' + cameraDataFull.length;
}}

// --- Управление слоями ---
var overlayLayers = {{"ДТП": dtpCluster}};
if ({str(has_cameras).lower()}) {{
    overlayLayers["Камеры"] = cameraCluster;
}}
L.control.layers({{}}, overlayLayers, {{collapsed: false}}).addTo(map);
"""

    # --------------------------------------------------
    # 2. Аналитический отчёт с графиками (сценарий 2)
    # --------------------------------------------------

    def generate_analytics_report(
        self,
        current_cards: list[dict],
        prev_cards: list[dict] | None = None,
        comparison: dict | None = None,
    ) -> str:
        """
        Генерирует HTML-отчёт с графиками ECharts.
        Без карты — только визуализации аналитики.
        """
        from analytics import calculate_metrics, compare_metrics

        current_metrics = calculate_metrics(current_cards)
        prev_metrics = None
        if prev_cards:
            prev_metrics = calculate_metrics(prev_cards)

        # Сводка
        summary_html = self._build_summary(
            total=current_metrics.get("total", 0),
            deaths=current_metrics.get("deaths", 0),
            injured=current_metrics.get("injured", 0),
        )

        # Графики
        charts_html = self._build_analytics_charts(
            current_cards, prev_cards, current_metrics, prev_metrics,
        )

        body = f"""
{summary_html}
<div class="chart-grid">
{charts_html}
</div>
"""

        return self._html_shell(
            body, use_map=False, use_echarts=True,
            custom_css=self._chart_css(),
        )

    def _chart_css(self) -> str:
        return """
.chart-box { min-height: 380px; }
.chart-box .chart-container { height: 350px; padding: 8px; }
"""

    def _build_analytics_charts(
        self,
        current_cards: list[dict],
        prev_cards: list[dict] | None,
        current_metrics: dict,
        prev_metrics: dict | None,
    ) -> str:
        """Генерирует блоки с графиками ECharts."""
        charts = []

        # 1. Динамика по месяцам
        charts.append(self._chart_monthly_dynamics(current_cards, prev_cards))

        # 2. Распределение по типам ДТП
        charts.append(self._chart_by_type(current_cards))

        # 3. По дням недели
        charts.append(self._chart_by_weekday(current_cards, prev_cards))

        # 4. По часам суток
        charts.append(self._chart_by_hour(current_cards))

        # 5. Сравнение с прошлым периодом (если есть)
        if prev_metrics:
            charts.append(self._chart_comparison(current_metrics, prev_metrics))

        return "\n".join(charts)

    def _chart_monthly_dynamics(
        self, current_cards: list[dict], prev_cards: list[dict] | None,
    ) -> str:
        """Линейный график: ДТП по месяцам."""
        from collections import Counter

        # Текущий период
        month_counts = Counter()
        month_order = [
            "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
            "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
        ]
        for c in current_cards:
            date_str = c.get("date_dtp", "")
            if "." in date_str:
                day, mon, year = date_str.split(".")
                try:
                    mi = int(mon) - 1
                    month_counts[month_order[mi]] += 1
                except (ValueError, IndexError):
                    pass

        labels = [m for m in month_order if m in month_counts]
        values = [month_counts[m] for m in labels]

        # Прошлый период
        prev_values = []
        if prev_cards:
            prev_counts = Counter()
            for c in prev_cards:
                date_str = c.get("date_dtp", "")
                if "." in date_str:
                    parts = date_str.split(".")
                    try:
                        mi = int(parts[1]) - 1
                        prev_counts[month_order[mi]] += 1
                    except (ValueError, IndexError):
                        pass
            prev_values = [prev_counts[m] for m in labels]

        series = [
            {"name": "Текущий период", "type": "line", "smooth": True,
             "data": values, "areaStyle": {"opacity": 0.15}},
        ]
        if prev_values:
            series.append(
                {"name": "Прошлый период", "type": "line", "smooth": True,
                 "data": prev_values, "lineStyle": {"type": "dashed"}},
            )

        return self._echarts_box(
            "chart_dynamics", "Динамика ДТП по месяцам",
            json.dumps(labels, ensure_ascii=False),
            series,
        )

    def _chart_by_type(self, cards: list[dict]) -> str:
        """Круговая диаграмма: распределение по видам ДТП."""
        from collections import Counter

        type_counts = Counter(
            c.get("dtpv", "Не указан") for c in cards if c.get("dtpv")
        )
        top = type_counts.most_common(8)
        other = sum(v for _, v in type_counts.most_common()[8:])

        pie_data = [{"name": n, "value": v} for n, v in top]
        if other > 0:
            pie_data.append({"name": "Прочие", "value": other})

        option = {
            "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
            "series": [{
                "type": "pie",
                "radius": ["35%", "65%"],
                "data": pie_data,
                "label": {"fontSize": 11},
            }],
        }

        return f"""
<div class="chart-box">
  <div class="chart-title">Распределение по видам ДТП</div>
  <div class="chart-container" id="chart_type"></div>
</div>
<script>
var chartType = echarts.init(document.getElementById('chart_type'));
chartType.setOption({json.dumps(option, ensure_ascii=False)});
</script>"""

    def _chart_by_weekday(
        self, current_cards: list[dict], prev_cards: list[dict] | None,
    ) -> str:
        """Столбчатый график: по дням недели."""
        from collections import Counter

        days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        counts = Counter()
        for c in current_cards:
            date_str = c.get("date_dtp", "")
            if "." in date_str:
                try:
                    day, mon, year = date_str.split(".")
                    from datetime import date as _date
                    d = _date(int(year), int(mon), int(day))
                    counts[days[d.weekday()]] += 1
                except (ValueError, IndexError):
                    pass

        values = [counts[d] for d in days]

        series = [{"name": "ДТП", "type": "bar", "data": values}]

        if prev_cards:
            prev_counts = Counter()
            for c in prev_cards:
                date_str = c.get("date_dtp", "")
                if "." in date_str:
                    try:
                        day, mon, year = date_str.split(".")
                        from datetime import date as _date
                        d = _date(int(year), int(mon), int(day))
                        prev_counts[days[d.weekday()]] += 1
                    except (ValueError, IndexError):
                        pass
            series.append({
                "name": "Прошлый период", "type": "bar",
                "data": [prev_counts[d] for d in days],
            })

        return self._echarts_box(
            "chart_weekday", "Распределение по дням недели",
            json.dumps(days, ensure_ascii=False),
            series,
        )

    def _chart_by_hour(self, cards: list[dict]) -> str:
        """Столбчатый график: по часам суток."""
        from collections import Counter

        hour_counts = Counter()
        for c in cards:
            time_str = c.get("time", "")
            if ":" in time_str:
                try:
                    h = int(time_str.split(":")[0])
                    hour_counts[h] += 1
                except (ValueError, IndexError):
                    pass

        labels = [f"{h:02d}:00" for h in range(24)]
        values = [hour_counts.get(h, 0) for h in range(24)]

        series = [{
            "name": "ДТП", "type": "bar",
            "data": values,
            "itemStyle": {"color": "#1565c0"},
        }]

        return self._echarts_box(
            "chart_hour", "Распределение по часам суток",
            json.dumps(labels, ensure_ascii=False),
            series,
        )

    def _chart_comparison(
        self, current_metrics: dict, prev_metrics: dict,
    ) -> str:
        """Сгруппированный столбчатый: сравнение периодов."""
        labels = ["Всего ДТП", "Погибло", "Ранено"]
        current_vals = [
            current_metrics.get("total", 0),
            current_metrics.get("deaths", 0),
            current_metrics.get("injured", 0),
        ]
        prev_vals = [
            prev_metrics.get("total", 0),
            prev_metrics.get("deaths", 0),
            prev_metrics.get("injured", 0),
        ]

        series = [
            {"name": "Текущий", "type": "bar", "data": current_vals},
            {"name": "Прошлый", "type": "bar", "data": prev_vals},
        ]

        return self._echarts_box(
            "chart_comparison", "Сравнение с прошлым периодом",
            json.dumps(labels, ensure_ascii=False),
            series,
        )

    def _echarts_box(
        self, chart_id: str, title: str,
        x_data_json: str, series: list[dict],
    ) -> str:
        """Общий шаблон блока с ECharts (осевой график)."""
        option = {
            "tooltip": {"trigger": "axis"},
            "legend": {"data": [s["name"] for s in series], "bottom": 0},
            "xAxis": {"type": "category", "data": json.loads(x_data_json)},
            "yAxis": {"type": "value"},
            "series": series,
            "grid": {"bottom": 50, "left": 50, "right": 20},
        }

        return f"""
<div class="chart-box">
  <div class="chart-title">{self._esc(title)}</div>
  <div class="chart-container" id="{chart_id}"></div>
</div>
<script>
var {chart_id} = echarts.init(document.getElementById('{chart_id}'));
{chart_id}.setOption({json.dumps(option, ensure_ascii=False)});
</script>"""

    # --------------------------------------------------
    # 3. Карта очагов (сценарий 3)
    # --------------------------------------------------

    def generate_cluster_map(
        self,
        clusters: list[dict],
        preclusters: list[dict] | None = None,
        cameras: list[dict] | None = None,
    ) -> str:
        """
        Генерирует HTML с картой очагов, предочагов и камер.
        """
        # Статистика
        total_dtp = sum(c.get("total_accidents", 0) for c in clusters)
        total_deaths = sum(c.get("deaths", 0) for c in clusters)
        total_injured = sum(c.get("injured", 0) for c in clusters)
        closed = sum(
            1 for c in clusters
            if (c.get("camera_match") or {}).get("status") == "закрыт"
        )
        has_pre = preclusters and len(preclusters) > 0

        summary_html = self._build_summary(
            total=total_dtp,
            deaths=total_deaths,
            injured=total_injured,
            cameras=len(cameras) if cameras else 0,
        )
        if closed > 0:
            summary_html += f"""
<div class="summary-grid" style="margin-top:-8px;">
  <div class="summary-card cameras">
    <div class="value">{closed}/{len(clusters)}</div>
    <div class="label">Очагов закрыто камерами</div>
  </div>
</div>"""

        # Собираем все точки для центра
        all_cards = []
        for cl in clusters:
            all_cards.extend(cl.get("cards", []))
        if preclusters:
            for pc in preclusters:
                all_cards.extend(pc.get("cards", []))
        center = self._calc_center(all_cards)
        zoom = self._calc_zoom(all_cards)

        # JS-данные
        clusters_js = self._build_clusters_js(clusters)
        preclusters_js = self._build_clusters_js(
            preclusters, is_precluster=True,
        ) if has_pre else "[]"
        camera_markers = self._build_camera_markers_js(
            cameras,
        ) if cameras else "[]"

        map_js = self._cluster_map_js(
            center, zoom, clusters_js, preclusters_js,
            camera_markers, cameras is not None, has_pre,
        )

        # Легенда для карты очагов
        legend_html = self._build_cluster_legend(has_pre)

        # Фильтр камер по модели (если есть камеры)
        cam_models = sorted(set(
            c.get("model", "") for c in (cameras or []) if c.get("model")
        )) if cameras else []
        filter_html = ""
        if cam_models:
            filter_html = self._build_camera_filter_panel(cam_models)

        body = f"""
{summary_html}
{legend_html}
{filter_html}
{self._build_coord_search_html()}
<div class="map-container">
  <div class="map-title">Карта очагов ДТП — {self._esc(self.region_name)}</div>
  <div id="map"></div>
</div>
<script>
{map_js}
</script>
"""

        return self._html_shell(body, use_map=True)

    def _build_clusters_js(
        self, clusters: list[dict], is_precluster: bool = False,
    ) -> str:
        """Строит JSON-массив очагов/предочагов для JS."""
        result = []
        for i, cl in enumerate(clusters, start=1):
            cards = cl.get("cards", [])
            points = []
            for c in cards:
                coords = _parse_coords(c)
                if coords:
                    popup = _card_popup_html(c)
                    points.append({
                        "lat": coords[0], "lon": coords[1],
                        "popup": popup,
                        "color": _severity_color(c),
                    })

            # Информация об очаге
            road = cl.get("road", "")
            count = cl.get("total_accidents", len(cards))
            deaths = cl.get("deaths", 0)
            injured = cl.get("injured", 0)
            types = cl.get("type_counter", {})
            center = cl.get("center")

            # Камера — все три состояния
            cam_match = cl.get("camera_match") or {}
            cam_info = None
            cam_status = cam_match.get("status", "открыт")
            if cam_status == "закрыт":
                cam = cam_match.get("in_cluster")
                cam_info = {
                    "status": "закрыт",
                    "popup": _camera_popup_html(cam) if cam else None,
                }
            else:
                # Отдельно проверяем ближайшую камеру
                nearest = cam_match.get("nearest")
                near_dist = cam_match.get("nearest_dist_m")
                if nearest:
                    cam_info = {
                        "status": "открыт_ближайшая",
                        "nearest_dist": near_dist,
                        "popup": _camera_popup_html(nearest),
                    }
                else:
                    cam_info = {"status": "открыт"}

            # Динамика
            dynamics = cl.get("dynamics")
            dyn_info = None
            if dynamics:
                dyn_info = {
                    "status": dynamics.get("status"),
                    "prev_total": dynamics.get("prev_total"),
                }

            # Предочаг — критерий
            pre_criterion = cl.get("precluster_criterion", "") if is_precluster else ""

            entry = {
                "id": i,
                "road": road,
                "count": count,
                "deaths": deaths,
                "injured": injured,
                "types": types,
                "points": points,
                "is_precluster": is_precluster,
                "pre_criterion": pre_criterion,
                "camera": cam_info,
                "dynamics": dyn_info,
                "piket_min": cl.get("dtp_pk_min"),
                "piket_max": cl.get("dtp_pk_max"),
            }
            if center:
                entry["center"] = list(center)

            result.append(entry)

        return json.dumps(result, ensure_ascii=False)

    def _build_cluster_legend(self, has_preclusters: bool) -> str:
        """Легенда для карты очагов."""
        pre_row = """
    <tr><td style="padding:2px 8px 2px 0;"><span class="legend-dot" style="background:#e0e0e0;border:2px dashed #9e9e9e;"></span></td><td>Зона предочага (пунктир)</td></tr>""" if has_preclusters else ""
        return f"""
<div class="legend">
  <div style="font-weight:600;margin-bottom:6px;">Условные обозначения</div>
  <table style="border-collapse:collapse;font-size:13px;">
    <tr><td style="padding:2px 8px 2px 0;"><span class="legend-dot" style="background:#d32f2f"></span></td><td>Очаг: 10+ ДТП</td></tr>
    <tr><td style="padding:2px 8px 2px 0;"><span class="legend-dot" style="background:#f57c00"></span></td><td>Очаг: 6–9 ДТП</td></tr>
    <tr><td style="padding:2px 8px 2px 0;"><span class="legend-dot" style="background:#fbc02d"></span></td><td>Очаг: 3–5 ДТП</td></tr>
{pre_row}
    <tr><td style="padding:2px 8px 2px 0;"><span class="legend-dot" style="background:#d32f2f"></span></td><td style="font-size:12px;">— точка ДТП с погибшими</td></tr>
    <tr><td style="padding:2px 8px 2px 0;"><span class="legend-dot" style="background:#f57c00"></span></td><td style="font-size:12px;">— точка ДТП с ранеными</td></tr>
    <tr><td style="padding:2px 8px 2px 0;font-size:16px;">📷</td><td>Камера фотовидеофиксации</td></tr>
  </table>
  <div style="margin-top:6px;color:#757575;font-size:12px;">Кликните на зону очага для подробностей. Включите слои через панель справа.</div>
</div>"""

    def _build_camera_filter_panel(self, cam_models: list[str]) -> str:
        """Панель фильтра камер по модели (множественный выбор, для карт очагов/точки)."""
        cb_items = ""
        for m in cam_models:
            cb_items += f'<label><input type="checkbox" value="{self._esc(m)}" onchange="applyClusterCameraFilter()"> {self._esc(m)}</label>\n    '
        return f"""
<div class="filter-panel">
  <span class="filter-title">📷 Камеры:</span>
  <div class="multi-select" id="camera_model_multi">
    <div class="multi-select-trigger" onclick="toggleMultiSelect('camera_model_multi')">
      <span class="multi-select-label">Все модели</span>
      <span class="multi-select-arrow">▼</span>
    </div>
    <div class="multi-select-dropdown">
    {cb_items}
    </div>
  </div>
  <span class="filter-count" id="camera_filter_count"></span>
</div>"""

    def _cluster_map_js(
        self,
        center: tuple[float, float],
        zoom: int,
        clusters_js: str,
        preclusters_js: str,
        camera_markers_js: str,
        has_cameras: bool,
        has_preclusters: bool,
    ) -> str:
        """JavaScript-код карты очагов.
        ДТП очагов и предочагов разделены по слоям.
        """
        return f"""
var map = L.map('map').setView([{center[0]}, {center[1]}], {zoom});

L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    attribution: '&copy; OpenStreetMap contributors',
    maxZoom: 19,
}}).addTo(map);

// --- Линейка ---
L.control.measure({{
    position: 'topleft',
    primaryLengthUnit: 'meters',
    secondaryLengthUnit: 'kilometers',
    localization: {{
    }}
}}).addTo(map);

// --- Алгоритм Грэхема (convex hull) ---
function convexHull(pts) {{
    if (pts.length < 3) return pts.map(function(p) {{ return [p.lon, p.lat]; }});
    var points = pts.slice().sort(function(a, b) {{
        return a.lat - b.lat || a.lon - b.lon;
    }});
    var cross = function(O, A, B) {{
        return (A[0] - O[0]) * (B[1] - O[1]) - (A[1] - O[1]) * (B[0] - O[0]);
    }};
    var lower = [];
    for (var i = 0; i < points.length; i++) {{
        while (lower.length >= 2 && cross(lower[lower.length-2], lower[lower.length-1], [points[i].lon, points[i].lat]) <= 0)
            lower.pop();
        lower.push([points[i].lon, points[i].lat]);
    }}
    var upper = [];
    for (var i = points.length - 1; i >= 0; i--) {{
        while (upper.length >= 2 && cross(upper[upper.length-2], upper[upper.length-1], [points[i].lon, points[i].lat]) <= 0)
            upper.pop();
        upper.push([points[i].lon, points[i].lat]);
    }}
    lower.pop();
    upper.pop();
    return lower.concat(upper);
}}

function clusterColor(count) {{
    if (count >= 10) return '#d32f2f';
    if (count >= 6) return '#f57c00';
    if (count >= 3) return '#fbc02d';
    return '#4caf50';
}}

// --- Слои: отдельные для ДТП очагов и предочагов ---
var dtpClusterLayer = L.layerGroup();    // ДТП внутри очагов
var dtpPreclusterLayer = L.layerGroup(); // ДТП внутри предочагов
var clusterLayer = L.layerGroup();      // зоны и маркеры очагов
var preclusterLayer = L.layerGroup();   // зоны и маркеры предочагов

// --- Камеры (кластеризация) ---
var cameraIcon = L.divIcon({{
    html: '<div style="font-size:18px;text-align:center;line-height:1;">📷</div>',
    iconSize: [24, 24], iconAnchor: [12, 12], className: ''
}});
var cameraCluster = L.markerClusterGroup({{
    maxClusterRadius: 40,
    spiderfyOnMaxZoom: true,
    showCoverageOnHover: false,
    iconCreateFunction: function(cluster) {{
        var count = cluster.getChildCount();
        return L.divIcon({{
            html: '<div class="camera-cluster-icon"><span>' + count + '</span></div>',
            className: '',
            iconSize: L.point(36, 36)
        }});
    }}
}});
var cameraDataFull = {camera_markers_js};
cameraDataFull.forEach(function(c) {{
    L.marker([c.lat, c.lon], {{icon: cameraIcon}})
     .bindPopup(c.popup, {{maxWidth: 320}})
     .addTo(cameraCluster);
}});
cameraCluster.addTo(map);

// --- Функция отрисовки очага/предочага ---
function drawClusterGroup(data, zoneLayer, dtpLayer, isPre) {{
    data.forEach(function(cl) {{
        if (cl.points.length === 0) return;
        var pts = cl.points;

        // Convex hull (зона)
        var hull = convexHull(pts);
        var color = clusterColor(cl.count);
        var zoneStyle = isPre ? {{
            color: '#9e9e9e', fillColor: '#e0e0e0', fillOpacity: 0.08,
            weight: 2, dashArray: '6,4'
        }} : {{
            color: color, fillColor: color, fillOpacity: 0.12, weight: 2
        }};

        if (hull.length >= 3) {{
            L.polygon(hull, zoneStyle).addTo(zoneLayer);
        }}

        // Точки ДТП — в соответствующий DTP-слой
        pts.forEach(function(p) {{
            L.circleMarker([p.lat, p.lon], {{
                radius: 5, fillColor: p.color, color: '#333',
                weight: 1, fillOpacity: 0.9
            }}).bindPopup(p.popup, {{maxWidth: 320}}).addTo(dtpLayer);
        }});

        // Линии между точками ДТП очага
        if (pts.length >= 2) {{
            var lineCoords = pts.map(function(p) {{ return [p.lat, p.lon]; }});
            var lineOpts = {{
                color: isPre ? '#757575' : color,
                weight: 3, opacity: 0.7
            }};
            if (isPre) lineOpts.dashArray = '6,4';
            L.polyline(lineCoords, lineOpts).addTo(zoneLayer);
        }}

        // Попап зоны
        var dynText = '';
        if (cl.dynamics) {{
            var statusMap = {{
                'new': '🆕 Новый', 'lost': '❌ Исчез',
                'growing': '📈 Растёт', 'shrinking': '📉 Снижается',
                'stable': '➡️ Стабильный'
            }};
            dynText = '<br>Динамика: ' + (statusMap[cl.dynamics.status] || cl.dynamics.status);
            if (cl.dynamics.prev_total !== null) {{
                dynText += ' (было ' + cl.dynamics.prev_total + ' ДТП)';
            }}
        }}
        var preText = isPre ? '<br><i>' + cl.pre_criterion + '</i>' : '';
        var camText = '';
        if (cl.camera) {{
            if (cl.camera.status === 'закрыт') {{
                camText = '<br>📷 <b>Закрыт камерой</b>';
            }} else if (cl.camera.status === 'открыт_ближайшая') {{
                var d = cl.camera.nearest_dist ? Math.round(cl.camera.nearest_dist) + ' м' : '';
                camText = '<br>📷 Не закрыт, ближайшая: ' + d;
            }} else {{
                camText = '<br>📷 Не закрыт камерой';
            }}
        }}

        // Пикетаж очага
        var piketText = '';
        if (cl.piket_min != null && cl.piket_max != null) {{
            piketText = '<br>📏 Пикетаж: ' + cl.piket_min.toFixed(3) + ' — ' + cl.piket_max.toFixed(3) + ' км';
        }} else if (cl.piket_min != null) {{
            piketText = '<br>📏 Пикетаж: ' + cl.piket_min.toFixed(3) + ' км';
        }}

        // Маркер центра с попапом очага
        if (cl.center) {{
            var marker = L.circleMarker(cl.center, {{
                radius: 10, fillColor: color, color: '#000',
                weight: 2, fillOpacity: 0.3
            }});
            var popupHtml = '<b>' + (isPre ? 'Предочаг' : 'Очаг') + ' №' + cl.id + '</b>' +
                '<br>' + cl.road +
                '<br>ДТП: ' + cl.count +
                (cl.deaths ? ' | 💀 ' + cl.deaths : '') +
                (cl.injured ? ' | 🏥 ' + cl.injured : '') +
                camText + dynText + preText + piketText;

            var typeEntries = Object.entries(cl.types).sort(function(a,b) {{ return b[1]-a[1]; }});
            if (typeEntries.length > 0) {{
                popupHtml += '<br><br><b>Типы ДТП:</b>';
                typeEntries.slice(0, 5).forEach(function(e) {{
                    popupHtml += '<br>&bull; ' + e[0] + ' — ' + e[1];
                }});
            }}

            // Информация о камере
            if (cl.camera && cl.camera.popup) {{
                popupHtml += '<br><br><b>Камера:</b><br>' + cl.camera.popup;
            }}

            marker.bindPopup(popupHtml, {{maxWidth: 360}}).addTo(zoneLayer);
        }}
    }});
}}

// --- Отрисовка ---
var clusterData = {clusters_js};
drawClusterGroup(clusterData, clusterLayer, dtpClusterLayer, false);

var preclusterData = {preclusters_js};
if (preclusterData.length > 0) {{
    drawClusterGroup(preclusterData, preclusterLayer, dtpPreclusterLayer, true);
}}

// Добавляем слои на карту
dtpClusterLayer.addTo(map);
clusterLayer.addTo(map);

// --- Управление слоями ---
var overlayLayers = {{
    "Очаги (зоны)": clusterLayer,
    "ДТП в очагах": dtpClusterLayer
}};
if (preclusterData.length > 0) {{
    overlayLayers["Предочаги (зоны)"] = preclusterLayer;
    overlayLayers["ДТП в предочагах"] = dtpPreclusterLayer;
}}
if ({str(has_cameras).lower()}) overlayLayers["Камеры"] = cameraCluster;

L.control.layers({{}}, overlayLayers, {{collapsed: false}}).addTo(map);

// --- Фильтр камер по модели (множественный выбор) ---
function renderCameras(data) {{
    cameraCluster.clearLayers();
    data.forEach(function(c) {{
        L.marker([c.lat, c.lon], {{icon: cameraIcon}})
         .bindPopup(c.popup, {{maxWidth: 320}})
         .addTo(cameraCluster);
    }});
}}
function toggleMultiSelect(id) {{
    var dd = document.querySelector('#' + id + ' .multi-select-dropdown');
    dd.classList.toggle('open');
}}
document.addEventListener('click', function(e) {{
    if (!e.target.closest('.multi-select')) {{
        document.querySelectorAll('.multi-select-dropdown.open').forEach(function(d) {{
            d.classList.remove('open');
        }});
    }}
}});
function getSelectedModels(id) {{
    var cbs = document.querySelectorAll('#' + id + ' .multi-select-dropdown input:checked');
    return Array.from(cbs).map(function(cb) {{ return cb.value; }});
}}
function updateMultiSelectLabel(id) {{
    var sel = getSelectedModels(id);
    var lbl = document.querySelector('#' + id + ' .multi-select-label');
    if (sel.length === 0) {{
        lbl.textContent = 'Все модели';
    }} else {{
        lbl.textContent = sel.length + ' выбрано';
    }}
}}
function applyClusterCameraFilter() {{
    updateMultiSelectLabel('camera_model_multi');
    var selected = getSelectedModels('camera_model_multi');
    var filtered = cameraDataFull.filter(function(c) {{
        return selected.length === 0 || selected.indexOf(c.model) !== -1;
    }});
    renderCameras(filtered);
    var cntEl = document.getElementById('camera_filter_count');
    if (cntEl) cntEl.textContent = filtered.length + ' из ' + cameraDataFull.length;
}}
"""

    # --------------------------------------------------
    # 4. Карта статистики по точке (сценарий 4)
    # --------------------------------------------------

    def generate_point_stats_map(
        self,
        lat: float,
        lon: float,
        radius_m: float,
        current_cards: list[dict],
        prev_cards: list[dict] | None = None,
        cameras: list[dict] | None = None,
        current_label: str = "",
        prev_label: str = "",
    ) -> str:
        """
        Генерирует HTML с картой: точка + радиус + ДТП + камеры.
        """
        from point_statistics import filter_cards_by_radius

        current_filtered = filter_cards_by_radius(
            current_cards, lat, lon, radius_m,
        )
        prev_filtered = (
            filter_cards_by_radius(prev_cards, lat, lon, radius_m)
            if prev_cards else []
        )

        # Статистика
        cur_pog = sum(int(c.get("pog", 0) or 0) for c in current_filtered)
        cur_ran = sum(int(c.get("ran", 0) or 0) for c in current_filtered)
        prev_pog = sum(int(c.get("pog", 0) or 0) for c in prev_filtered)
        prev_ran = sum(int(c.get("ran", 0) or 0) for c in prev_filtered)

        # Фильтруем камеры в радиусе
        cam_in_radius = []
        if cameras:
            from camera_matcher import haversine
            for cam in cameras:
                d = haversine(lat, lon, cam["lat"], cam["lon"])
                if d <= radius_m:
                    cam_in_radius.append({**cam, "distance_m": round(d, 0)})

        radius_str = f"{radius_m:.0f} м" if radius_m < 1000 else f"{radius_m/1000:.0f} км"

        summary_html = f"""
<div class="summary-grid">
  <div class="summary-card total">
    <div class="value">{len(current_filtered)}</div>
    <div class="label">ДТП ({self._esc(current_label)})</div>
  </div>
  <div class="summary-card fatal">
    <div class="value">{cur_pog}</div>
    <div class="label">Погибло</div>
  </div>
  <div class="summary-card injured">
    <div class="value">{cur_ran}</div>
    <div class="label">Ранено</div>
  </div>"""
        if prev_filtered:
            summary_html += f"""
  <div class="summary-card">
    <div class="value">{len(prev_filtered)}</div>
    <div class="label">ДТП ({self._esc(prev_label)})</div>
  </div>"""
        if cam_in_radius:
            summary_html += f"""
  <div class="summary-card cameras">
    <div class="value">{len(cam_in_radius)}</div>
    <div class="label">Камер в радиусе</div>
  </div>"""
        summary_html += "\n</div>"

        # Легенда
        legend_items = []
        if prev_filtered:
            legend_items.append("<tr><td style=\"padding:2px 8px 2px 0;\"><span class=\"legend-dot\" style=\"background:#1565c0;\"></span></td><td>Точка запроса</td></tr>")
            legend_items.append("<tr><td style=\"padding:2px 8px 2px 0;\"><span class=\"legend-dot\" style=\"background:#1565c0;opacity:0.4;\"></span></td><td>ДТП прошлого периода (полупрозрачные)</td></tr>")
        else:
            legend_items.append("<tr><td style=\"padding:2px 8px 2px 0;\"><span class=\"legend-dot\" style=\"background:#1565c0;\"></span></td><td>Точка запроса + радиус</td></tr>")
        legend_items.append("<tr><td style=\"padding:2px 8px 2px 0;\"><span class=\"legend-dot\" style=\"background:#d32f2f\"></span></td><td>ДТП с погибшими</td></tr>")
        legend_items.append("<tr><td style=\"padding:2px 8px 2px 0;\"><span class=\"legend-dot\" style=\"background:#f57c00\"></span></td><td>ДТП с ранеными</td></tr>")
        legend_items.append("<tr><td style=\"padding:2px 8px 2px 0;\"><span class=\"legend-dot\" style=\"background:#4caf50\"></span></td><td>Материальный ущерб</td></tr>")
        if cam_in_radius:
            legend_items.append("<tr><td style=\"padding:2px 8px 2px 0;font-size:16px;\">📷</td><td>Камера фотовидеофиксации</td></tr>")
        legend_html = f"""
<div class="legend">
  <div style="font-weight:600;margin-bottom:6px;">Условные обозначения</div>
  <table style="border-collapse:collapse;font-size:13px;">
    {''.join(legend_items)}
  </table>
</div>"""

        # Данные карты
        dtp_geojson = self._build_dtp_geojson(current_filtered)
        prev_geojson = self._build_dtp_geojson(prev_filtered) if prev_filtered else "[]"
        camera_markers = self._build_camera_markers_js(cam_in_radius) if cam_in_radius else "[]"

        map_js = self._point_map_js(
            lat, lon, radius_m, radius_str,
            dtp_geojson, prev_geojson, camera_markers,
            has_prev=bool(prev_filtered),
            has_cameras=bool(cam_in_radius),
        )

        body = f"""
{summary_html}
{legend_html}
{self._build_coord_search_html()}
<div class="map-container">
  <div class="map-title">
    Статистика по точке — радиус {self._esc(radius_str)}
  </div>
  <div id="map"></div>
</div>
<script>
{map_js}
</script>
"""

        return self._html_shell(body, use_map=True)

    def _point_map_js(
        self,
        lat: float,
        lon: float,
        radius_m: float,
        radius_str: str,
        dtp_geojson: str,
        prev_geojson: str,
        camera_markers_js: str,
        has_prev: bool,
        has_cameras: bool,
    ) -> str:
        """JavaScript-код карты точки."""
        return f"""
var map = L.map('map').setView([{lat}, {lon}], 15);

L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    attribution: '&copy; OpenStreetMap contributors',
    maxZoom: 19,
}}).addTo(map);

// --- Линейка ---
L.control.measure({{
    position: 'topleft',
    primaryLengthUnit: 'meters',
    secondaryLengthUnit: 'kilometers',
    localization: {{
    }}
}}).addTo(map);

// --- Точка пользователя ---
var userMarker = L.circleMarker([{lat}, {lon}], {{
    radius: 10, fillColor: '#1565c0', color: '#0d47a1',
    weight: 3, fillOpacity: 1
}}).bindPopup('<b>Точка запроса</b><br>{self._esc(radius_str)}').addTo(map);

// --- Круг радиуса ---
L.circle([{lat}, {lon}], {{
    radius: {radius_m},
    color: '#1565c0', fillColor: '#1565c0',
    fillOpacity: 0.06, weight: 2, dashArray: '8,4'
}}).addTo(map);

// --- ДТП текущего периода (кластеризация) ---
var curDtpCluster = L.markerClusterGroup({{
    maxClusterRadius: 40,
    spiderfyOnMaxZoom: true,
    showCoverageOnHover: false,
    iconCreateFunction: function(cluster) {{
        var count = cluster.getChildCount();
        var size = count < 10 ? 'small' : count < 100 ? 'medium' : 'large';
        return L.divIcon({{
            html: '<div><span>' + count + '</span></div>',
            className: 'marker-cluster marker-cluster-' + size,
            iconSize: L.point(40, 40)
        }});
    }}
}});
L.geoJSON({dtp_geojson}, {{
    pointToLayer: function(feature, latlng) {{
        return L.circleMarker(latlng, {{
            radius: 6, fillColor: feature.properties.color,
            color: '#333', weight: 1, fillOpacity: 0.8
        }});
    }},
    onEachFeature: function(feature, layer) {{
        layer.bindPopup(feature.properties.popup, {{maxWidth: 320}});
    }}
}}).addTo(curDtpCluster);
curDtpCluster.addTo(map);

// --- ДТП прошлого периода ---
var prevDtpLayer = L.layerGroup();
if ({str(has_prev).lower()}) {{
    L.geoJSON({prev_geojson}, {{
        pointToLayer: function(feature, latlng) {{
            return L.circleMarker(latlng, {{
                radius: 6, fillColor: feature.properties.color,
                color: '#333', weight: 1, fillOpacity: 0.4
            }});
        }},
        onEachFeature: function(feature, layer) {{
            layer.bindPopup(feature.properties.popup, {{maxWidth: 320}});
        }}
    }}).addTo(prevDtpLayer);
}}

// --- Камеры (кластеризация) ---
var cameraIcon = L.divIcon({{
    html: '<div style="font-size:18px;text-align:center;line-height:1;">📷</div>',
    iconSize: [24, 24], iconAnchor: [12, 12], className: ''
}});
var cameraCluster = L.markerClusterGroup({{
    maxClusterRadius: 40,
    spiderfyOnMaxZoom: true,
    showCoverageOnHover: false,
    iconCreateFunction: function(cluster) {{
        var count = cluster.getChildCount();
        return L.divIcon({{
            html: '<div class="camera-cluster-icon"><span>' + count + '</span></div>',
            className: '',
            iconSize: L.point(36, 36)
        }});
    }}
}});
var cameraData = {camera_markers_js};
cameraData.forEach(function(c) {{
    L.marker([c.lat, c.lon], {{icon: cameraIcon}})
     .bindPopup(c.popup + '<br>Расстояние: ' + c.distance_m + ' м', {{maxWidth: 320}})
     .addTo(cameraCluster);
}});
cameraCluster.addTo(map);

// --- Слои ---
var overlays = {{
    "ДТП (текущий период)": curDtpCluster
}};
if ({str(has_prev).lower()}) overlays["ДТП (прошлый период)"] = prevDtpLayer;
if ({str(has_cameras).lower()}) overlays["Камеры"] = cameraCluster;

L.control.layers({{}}, overlays, {{collapsed: false}}).addTo(map);

// Подгоняем карту под круг
map.fitBounds(L.circle([{lat}, {lon}], {{radius: {radius_m}}}).getBounds());
"""
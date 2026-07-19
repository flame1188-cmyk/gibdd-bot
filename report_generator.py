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
    """
    os.makedirs(_LIB_DIR, exist_ok=True)
    path = os.path.join(_LIB_DIR, name)

    if os.path.exists(path):
        logger.debug(f"report_generator: библиотека из кэша {name}")
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    logger.info(f"report_generator: скачивание {name}...")
    try:
        resp = httpx.get(url, follow_redirects=True, timeout=60)
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
@media print {
  body { background: white; }
  .container { max-width: 100%; padding: 0; }
  .map-container { box-shadow: none; border: 1px solid #ccc; }
  #map { height: 500px; }
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

        # Данные для карты
        dtp_geojson = self._build_dtp_geojson(cards_with_coords)
        camera_markers = self._build_camera_markers_js(cameras) if cameras else "[]"
        center = self._calc_center(cards_with_coords)
        zoom = self._calc_zoom(cards_with_coords)

        # JS-код карты
        map_js = self._dtp_map_js(center, zoom, dtp_geojson, camera_markers, cameras is not None)

        body = f"""
{summary_html}
{legend_html}
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
  <span class="legend-item"><span class="legend-dot" style="background:#d32f2f"></span> Погибло</span>
  <span class="legend-item"><span class="legend-dot" style="background:#f57c00"></span> Ранено</span>
  <span class="legend-item"><span class="legend-dot" style="background:#4caf50"></span> Материальный ущерб</span>
</div>"""

    def _build_dtp_geojson(self, cards: list[dict]) -> str:
        """Строит GeoJSON FeatureCollection из карточек ДТП."""
        features = []
        for card in cards:
            coords = _parse_coords(card)
            if coords is None:
                continue
            popup = _card_popup_html(card)
            features.append({
                "type": "Feature",
                "properties": {
                    "popup": popup,
                    "color": _severity_color(card),
                },
                "geometry": {
                    "type": "Point",
                    "coordinates": [coords[1], coords[0]],  # lon, lat
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
            })
        return json.dumps(markers, ensure_ascii=False)

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
        """JavaScript-код карты ДТП."""
        return f"""
var map = L.map('map').setView([{center[0]}, {center[1]}], {zoom});

// Тайлы OpenStreetMap
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    attribution: '&copy; OpenStreetMap contributors',
    maxZoom: 18,
}}).addTo(map);

// --- Слой ДТП ---
var dtpLayer = L.layerGroup().addTo(map);
var dtpData = {dtp_geojson};

L.geoJSON(dtpData, {{
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
}}).addTo(dtpLayer);

// --- Слой камер ---
var cameraLayer = L.layerGroup();
var cameraIcon = L.divIcon({{
    html: '<div style="font-size:18px;text-align:center;line-height:1;">📷</div>',
    iconSize: [24, 24],
    iconAnchor: [12, 12],
    className: ''
}});

var cameraData = {camera_markers_js};
cameraData.forEach(function(c) {{
    L.marker([c.lat, c.lon], {{icon: cameraIcon}})
     .bindPopup(c.popup, {{maxWidth: 320}})
     .addTo(cameraLayer);
}});

// --- Управление слоями ---
var baseLayers = {{}};
var overlayLayers = {{"ДТП": dtpLayer}};
if ({str(has_cameras).lower()}) {{
    overlayLayers["Камеры"] = cameraLayer;
    cameraLayer.addTo(map);
}}
L.control.layers(baseLayers, overlayLayers, {{collapsed: false}}).addTo(map);
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

        body = f"""
{summary_html}
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

            # Камера
            cam_match = cl.get("camera_match")
            cam_info = None
            if cam_match and cam_match.get("status") == "закрыт":
                cam = cam_match.get("camera", {})
                cam_info = {
                    "status": "закрыт",
                    "distance": cam_match.get("distance_m"),
                    "popup": _camera_popup_html(cam) if cam else None,
                }

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
            }
            if center:
                entry["center"] = list(center)

            result.append(entry)

        return json.dumps(result, ensure_ascii=False)

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
        """JavaScript-код карты очагов."""
        return f"""
var map = L.map('map').setView([{center[0]}, {center[1]}], {zoom});

L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    attribution: '&copy; OpenStreetMap contributors',
    maxZoom: 18,
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

// --- Слои ---
var dtpLayer = L.layerGroup();
var clusterLayer = L.layerGroup();
var preclusterLayer = L.layerGroup();
var cameraLayer = L.layerGroup();

// --- Камеры ---
var cameraIcon = L.divIcon({{
    html: '<div style="font-size:18px;text-align:center;line-height:1;">📷</div>',
    iconSize: [24, 24], iconAnchor: [12, 12], className: ''
}});
var cameraData = {camera_markers_js};
cameraData.forEach(function(c) {{
    L.marker([c.lat, c.lon], {{icon: cameraIcon}})
     .bindPopup(c.popup, {{maxWidth: 320}})
     .addTo(cameraLayer);
}});

// --- Функция отрисовки очага/предочага ---
function drawClusterGroup(data, layer, isPre) {{
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
            L.polygon(hull, zoneStyle).addTo(layer);
        }}

        // Точки ДТП внутри
        pts.forEach(function(p) {{
            L.circleMarker([p.lat, p.lon], {{
                radius: 5, fillColor: p.color, color: '#333',
                weight: 1, fillOpacity: 0.9
            }}).bindPopup(p.popup, {{maxWidth: 320}}).addTo(dtpLayer);
        }});

        // Попап зоны (при клике на hull)
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
        if (cl.camera && cl.camera.status === 'закрыт') {{
            camText = '<br>📷 <b>Закрыт камерой</b>' +
                (cl.camera.distance ? ' (' + Math.round(cl.camera.distance) + ' м)' : '');
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
                camText + dynText + preText;

            // Топ типы ДТП
            var typeEntries = Object.entries(cl.types).sort(function(a,b) {{ return b[1]-a[1]; }});
            if (typeEntries.length > 0) {{
                popupHtml += '<br><br><b>Типы ДТП:</b>';
                typeEntries.slice(0, 5).forEach(function(e) {{
                    popupHtml += '<br>&bull; ' + e[0] + ' — ' + e[1];
                }});
            }}

            marker.bindPopup(popupHtml, {{maxWidth: 360}}).addTo(layer);
        }}
    }});
}}

// --- Отрисовка ---
var clusterData = {clusters_js};
drawClusterGroup(clusterData, clusterLayer, false);

var preclusterData = {preclusters_js};
if (preclusterData.length > 0) {{
    drawClusterGroup(preclusterData, preclusterLayer, true);
}}

// DTP слой добавляем
dtpLayer.addTo(map);
clusterLayer.addTo(map);

// --- Управление слоями ---
var overlayLayers = {{
    "Очаги": clusterLayer,
    "ДТП (точки)": dtpLayer
}};
if (preclusterData.length > 0) overlayLayers["Предочаги"] = preclusterLayer;
if ({str(has_cameras).lower()}) overlayLayers["Камеры"] = cameraLayer;

L.control.layers({{}}, overlayLayers, {{collapsed: false}}).addTo(map);
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
    maxZoom: 18,
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

// --- ДТП текущего периода ---
var curDtpLayer = L.layerGroup();
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
}}).addTo(curDtpLayer);
curDtpLayer.addTo(map);

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

// --- Камеры ---
var cameraLayer = L.layerGroup();
var cameraIcon = L.divIcon({{
    html: '<div style="font-size:18px;text-align:center;line-height:1;">📷</div>',
    iconSize: [24, 24], iconAnchor: [12, 12], className: ''
}});
var cameraData = {camera_markers_js};
cameraData.forEach(function(c) {{
    L.marker([c.lat, c.lon], {{icon: cameraIcon}})
     .bindPopup(c.popup + '<br>Расстояние: ' + c.distance_m + ' м', {{maxWidth: 320}})
     .addTo(cameraLayer);
}});

// --- Слои ---
var overlays = {{
    "ДТП (текущий период)": curDtpLayer
}};
if ({str(has_prev).lower()}) overlays["ДТП (прошлый период)"] = prevDtpLayer;
if ({str(has_cameras).lower()}) overlays["Камеры"] = cameraLayer;

L.control.layers({{}}, overlays, {{collapsed: false}}).addTo(map);

// Подгоняем карту под круг
map.fitBounds(L.circle([{lat}, {lon}], {{radius: {radius_m}}}).getBounds());
"""
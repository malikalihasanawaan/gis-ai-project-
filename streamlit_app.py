"""
FloodSentry — Light-themed Streamlit + Folium viewer.

Launch flow: map appears immediately → search a location → click Evaluate → results.

Run: streamlit run streamlit_app.py
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import folium
import geopandas as gpd
import requests
import streamlit as st
from folium.plugins import Draw, Fullscreen, MeasureControl, MousePosition
from streamlit_folium import st_folium

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from flood_sentry.config import load_config
from flood_sentry.pipeline import get_evaluation_status, start_evaluation_job

# ─── Page config (light theme via .streamlit/config.toml) ────────────────
st.set_page_config(
    page_title="FloodSentry — Flood Risk Analysis",
    page_icon="🌊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Light-theme CSS ─────────────────────────────────────────────────────
st.markdown(
    """
<style>
    /* force light everywhere */
    .stApp { background-color: #ffffff; color: #1e293b; }
    section[data-testid="stSidebar"] {
        background-color: #f8fafc;
        border-right: 1px solid #e2e8f0;
        min-width: 340px; max-width: 400px;
    }
    .stApp header { background-color: #ffffff; }

    /* header bar */
    .app-header {
        background: linear-gradient(90deg, #2563eb 0%, #3b82f6 100%);
        color: white; padding: 14px 24px; border-radius: 0 0 12px 12px;
        margin-bottom: 10px; display: flex; align-items: center; gap: 14px;
    }
    .app-header h1 { margin: 0; font-size: 24px; font-weight: 700; }
    .app-header .sub { font-size: 13px; opacity: .85; }

    /* weather card */
    .weather-card {
        background: linear-gradient(135deg,#60a5fa,#2563eb);
        color:#fff; border-radius:12px; padding:14px 16px; margin:6px 0;
    }
    .weather-card .temp { font-size:28px; font-weight:700; }
    .weather-card .desc { font-size:14px; opacity:.9; }

    /* metric row */
    .metric-box {
        background:#f1f5f9; border-radius:8px; padding:12px 16px;
        border-left:4px solid #2563eb; margin:4px 0;
    }
    .metric-box .label { font-size:11px; text-transform:uppercase; color:#64748b; letter-spacing:.5px; }
    .metric-box .value { font-size:20px; font-weight:700; color:#1e293b; }

    /* legend */
    .legend-row { display:flex; align-items:center; gap:8px; font-size:13px; margin:3px 0; }
    .legend-swatch { width:16px; height:16px; border-radius:3px; border:1px solid #94a3b8; display:inline-block; }

    /* buttons */
    .stButton>button { border-radius:8px; font-weight:600; }

    /* status pill */
    .status-pill {
        display:inline-block; padding:3px 12px; border-radius:20px;
        font-size:12px; font-weight:600;
    }
    .status-pill.idle { background:#e2e8f0; color:#475569; }
    .status-pill.running { background:#dbeafe; color:#1d4ed8; }
    .status-pill.done { background:#d1fae5; color:#065f46; }
    .status-pill.error { background:#fee2e2; color:#991b1b; }
</style>
""",
    unsafe_allow_html=True,
)

# ─── Constants ───────────────────────────────────────────────────────────
RISK_COLORS = {
    "Very High": "#d73027",
    "High": "#f46d43",
    "Medium": "#fdae61",
    "Low": "#fee08b",
}
FLOOD_COLOR = "#abd9e9"

WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow",
    80: "Rain showers", 81: "Rain showers", 82: "Violent showers",
    95: "Thunderstorm", 96: "Thunderstorm + hail", 99: "Severe thunderstorm",
}

# ─── Session state ───────────────────────────────────────────────────────
defaults = {
    "flood_gdf": None,
    "risk_gdf": None,
    "analysis_bounds": None,
    "job_id": None,
    "job_status": None,
    "drawn_geometry": None,
    "search_result": None,
    "weather": None,
    "stats": None,
    "evaluated": False,
}
for k, v in defaults.items():
    st.session_state.setdefault(k, v)


# ─── Cached helpers ──────────────────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner=False)
def geocode(query: str) -> dict | None:
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1},
            headers={"User-Agent": "FloodSentry/1.0"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data:
            return {
                "lat": float(data[0]["lat"]),
                "lon": float(data[0]["lon"]),
                "name": data[0]["display_name"],
            }
    except Exception as e:
        logging.warning("geocode failed: %s", e)
    return None


@st.cache_data(ttl=600, show_spinner=False)
def fetch_weather(lat: float, lon: float) -> dict | None:
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "current_weather": "true",
                "hourly": "relative_humidity_2m",
                "timezone": "auto",
            },
            timeout=10,
        )
        r.raise_for_status()
        d = r.json()
        cw = d.get("current_weather", {})
        rh = d.get("hourly", {}).get("relative_humidity_2m", [None])
        return {
            "temp": cw.get("temperature"),
            "wind": cw.get("windspeed"),
            "dir": cw.get("winddirection"),
            "code": cw.get("weathercode"),
            "humidity": rh[0] if rh else None,
        }
    except Exception as e:
        logging.warning("weather failed: %s", e)
    return None


# ─── Map builders ────────────────────────────────────────────────────────
def build_map(center, zoom) -> folium.Map:
    m = folium.Map(location=center, zoom_start=zoom, tiles=None, control_scale=True)
    folium.TileLayer("OpenStreetMap", name="OpenStreetMap").add_to(m)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri", name="Satellite",
    ).add_to(m)
    folium.TileLayer(
        tiles="https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
        attr="OpenTopoMap", name="Terrain",
    ).add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    Fullscreen().add_to(m)
    MeasureControl(primary_length_unit="kilometers").add_to(m)
    MousePosition(position="bottomright", separator=" | ").add_to(m)
    Draw(
        export=False,
        draw_options={
            "polygon": True, "rectangle": True,
            "circle": False, "marker": False,
            "circlemarker": False, "polyline": False,
        },
        edit_options={"edit": True, "remove": True},
    ).add_to(m)
    return m


def risk_style(feat):
    cls = feat["properties"].get("class", "Low")
    return {
        "fillColor": RISK_COLORS.get(cls, "#fee08b"),
        "color": "#475569", "weight": 0.8, "fillOpacity": 0.55,
    }


def flood_style(_feat):
    return {"fillColor": FLOOD_COLOR, "color": "#1e3a8a", "weight": 1.2, "fillOpacity": 0.5}


def add_gdf(m, gdf, name, style, show=True):
    if gdf is None or gdf.empty:
        return
    if gdf.crs and gdf.crs != "EPSG:4326":
        gdf = gdf.to_crs("EPSG:4326")
    folium.GeoJson(
        json.loads(gdf.to_json()),
        name=name,
        style_function=style,
        tooltip=folium.GeoJsonTooltip(
            fields=[c for c in gdf.columns if c != "geometry"],
            aliases=[c.replace("_", " ").title() for c in gdf.columns if c != "geometry"],
        ),
        show=show,
    ).add_to(m)


def bbox_to_polygon(bbox: dict) -> dict:
    """Build a square polygon around the searched location (~0.15 deg)."""
    lat, lon = bbox["lat"], bbox["lon"]
    d = 0.15
    return {
        "type": "Polygon",
        "coordinates": [[
            [lon - d, lat - d], [lon + d, lat - d],
            [lon + d, lat + d], [lon - d, lat + d],
            [lon - d, lat - d],
        ]],
    }


# ─── Sidebar ─────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🔍 Search Location")
    query = st.text_input(
        "City, address, or lat,lon",
        placeholder="e.g. Mumbai  or  19.0760,72.8777",
        key="q",
    )
    col_a, col_b = st.columns([3, 1])
    search_clicked = col_a.button("Search", use_container_width=True)
    clear_clicked = col_b.button("Clear", use_container_width=True)

    if clear_clicked:
        for k in defaults:
            st.session_state[k] = defaults[k]
        st.rerun()

    if search_clicked and query.strip():
        with st.spinner("Searching…"):
            res = geocode(query.strip())
        if res:
            st.session_state.search_result = res
            st.session_state.weather = fetch_weather(res["lat"], res["lon"])
            st.success(f"📍 {res['name'][:60]}")
        else:
            st.error("Location not found. Try another query.")

    # Location card + weather
    if st.session_state.search_result:
        loc = st.session_state.search_result
        st.caption(f"📍 {loc['name'][:80]}")
        if st.session_state.weather:
            w = st.session_state.weather
            desc = WEATHER_CODES.get(w.get("code"), "—")
            st.markdown(
                f"""
<div class="weather-card">
    <div class="temp">{w.get('temp','—')}°C &nbsp; <span class="desc">{desc}</span></div>
    <div class="desc">💧 {w.get('humidity','—')}% humidity &nbsp;·&nbsp; 💨 {w.get('wind','—')} km/h</div>
</div>""",
                unsafe_allow_html=True,
            )

    st.markdown("---")
    st.markdown("## 🚀 Evaluate")

    st.caption(
        "Search a location above, or draw a polygon on the map, "
        "then click **Evaluate**."
    )
    evaluate = st.button(
        "⚡ Evaluate Flood Risk", type="primary", use_container_width=True
    )

    # Status pill
    if st.session_state.job_id:
        status = get_evaluation_status(st.session_state.job_id)
        step = status.get("progress", {}).get("step", "Working…")
        st.markdown(
            f'<span class="status-pill running">⏳ {step}</span>',
            unsafe_allow_html=True,
        )
    elif st.session_state.evaluated:
        st.markdown(
            '<span class="status-pill done">✅ Analysis complete</span>',
            unsafe_allow_html=True,
        )

    st.markdown("---")
    st.markdown("## 📤 Export")
    if st.session_state.flood_gdf is not None:
        st.download_button(
            "⬇️ Flood GeoJSON",
            data=st.session_state.flood_gdf.to_json(),
            file_name="flood_extent.geojson",
            mime="application/geo+json",
            use_container_width=True,
        )
    if st.session_state.risk_gdf is not None:
        st.download_button(
            "⬇️ Risk GeoJSON",
            data=st.session_state.risk_gdf.to_json(),
            file_name="risk_zones.geojson",
            mime="application/geo+json",
            use_container_width=True,
        )

    st.markdown("---")
    st.markdown("## 📋 Legend")
    for cls, color in RISK_COLORS.items():
        st.markdown(
            f'<div class="legend-row"><span class="legend-swatch" style="background:{color}"></span> {cls}</div>',
            unsafe_allow_html=True,
        )
    st.markdown(
        f'<div class="legend-row"><span class="legend-swatch" style="background:{FLOOD_COLOR}"></span> Flood Extent</div>',
        unsafe_allow_html=True,
    )

# ─── Header ──────────────────────────────────────────────────────────────
st.markdown(
    """
<div class="app-header">
    <div>
        <h1>🌊 FloodSentry</h1>
        <div class="sub">AI-based flood detection & risk-zone evaluation from Sentinel-1 SAR</div>
    </div>
</div>""",
    unsafe_allow_html=True,
)

# ─── Determine map centre ────────────────────────────────────────────────
if st.session_state.search_result:
    loc = st.session_state.search_result
    center, zoom = (loc["lat"], loc["lon"]), 11
elif st.session_state.analysis_bounds:
    b = st.session_state.analysis_bounds
    center = ((b[1] + b[3]) / 2, (b[0] + b[2]) / 2)
    zoom = 10
else:
    center, zoom = (20.0, 78.0), 4  # default to India

# ─── Build map + layers ──────────────────────────────────────────────────
m = build_map(center, zoom)

cfg = load_config()
demo_flood = Path(cfg["paths"]["predictions"]) / "flood_extent.geojson"
demo_risk = Path(cfg["paths"]["risk"]) / "risk_zones.geojson"

if demo_flood.exists():
    add_gdf(m, gpd.read_file(demo_flood), "Flood (demo)", flood_style)
if demo_risk.exists():
    add_gdf(m, gpd.read_file(demo_risk), "Risk (demo)", risk_style)
if st.session_state.flood_gdf is not None:
    add_gdf(m, st.session_state.flood_gdf, "Flood (result)", flood_style, True)
if st.session_state.risk_gdf is not None:
    add_gdf(m, st.session_state.risk_gdf, "Risk (result)", risk_style, True)

# Marker for searched location
if st.session_state.search_result:
    loc = st.session_state.search_result
    folium.Marker(
        [loc["lat"], loc["lon"]],
        tooltip=loc["name"],
        icon=folium.Icon(color="blue", icon="info-sign"),
    ).add_to(m)

# ─── Render map (captures drawings) ──────────────────────────────────────
map_data = st_folium(m, width=None, height=680,
                     returned_objects=["last_active_drawing", "all_drawings"])

# Persist any drawing
if map_data and map_data.get("last_active_drawing"):
    st.session_state.drawn_geometry = map_data["last_active_drawing"]["geometry"]

# ─── Evaluate handler (async job) ────────────────────────────────────────
if evaluate:
    geom = None
    if st.session_state.drawn_geometry:
        geom = st.session_state.drawn_geometry
    elif st.session_state.search_result:
        geom = bbox_to_polygon(st.session_state.search_result)

    if geom is None:
        st.warning("Search a location first, or draw a polygon on the map.")
    else:
        st.session_state.evaluated = False
        st.session_state.job_id = start_evaluation_job(geom)
        st.rerun()

# ─── Poll running job ────────────────────────────────────────────────────
if st.session_state.job_id:
    status = get_evaluation_status(st.session_state.job_id)

    if status["status"] in ("pending", "running"):
        prog = status.get("progress", {})
        st.progress(prog.get("pct", 0) / 100, text=prog.get("step", "Working…"))
        time.sleep(4)
        st.rerun()
    elif status["status"] == "done":
        result = status["result"]
        st.session_state.flood_gdf = gpd.GeoDataFrame.from_features(
            result["flood_geojson"]["features"], crs="EPSG:4326")
        st.session_state.risk_gdf = gpd.GeoDataFrame.from_features(
            result["risk_geojson"]["features"], crs="EPSG:4326")
        st.session_state.analysis_bounds = result["bounds"]
        st.session_state.stats = result["stats"]
        st.session_state.evaluated = True
        st.session_state.job_id = None
        st.rerun()
    elif status["status"] == "error":
        st.error(f"Analysis failed: {status.get('error', 'Unknown error')}")
        st.session_state.job_id = None

# ─── Results ─────────────────────────────────────────────────────────────
if st.session_state.evaluated and st.session_state.stats:
    st.markdown("---")
    st.subheader("📊 Evaluation Results")
    s = st.session_state.stats
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("🌊 Flood Area", f"{s['flood_area_ha']:,.1f} ha")
    c2.metric("🌊 Flood % of AOI", f"{s['flood_pct']:.1f}%")
    risk = s.get("risk_area_ha", {})
    c3.metric("🔴 High + Very High", f"{risk.get('High',0)+risk.get('Very High',0):,.1f} ha")
    c4.metric("🟡 Medium", f"{risk.get('Medium',0):,.1f} ha")

    # Risk breakdown table
    if risk:
        st.markdown("**Risk zone breakdown**")
        for cls in ["Very High", "High", "Medium", "Low"]:
            if cls in risk:
                st.markdown(
                    f'<div class="legend-row"><span class="legend-swatch" '
                    f'style="background:{RISK_COLORS[cls]}"></span> '
                    f'{cls}: <strong>{risk[cls]:,.1f} ha</strong></div>',
                    unsafe_allow_html=True,
                )

st.caption("FloodSentry · Sentinel-1 SAR · AI flood detection & risk zoning")
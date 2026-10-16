# FloodSentry — Architecture

AI-based flood detection & risk-zone mapping from satellite microwave (SAR) data.
Goal: deliver distortion-free flood/risk maps with minimal manual GIS effort, replacing
the repetitive parts of an ArcGIS workflow with an automated, reproducible pipeline.

---

## 1. Why microwave (SAR) first

Floods occur under cloud cover and at night — exactly when optical sensors fail.
SAR is all-weather, day/night, and the specular reflection of calm water off the
sensor direction produces a strong drop in backscatter, so water is physically
separable from land.

| Layer | Source | Role |
|---|---|---|
| Primary hazard | **Sentinel-1 GRD** (C-band, VV/VH, 10 m, ~6-day revisit) | Flood extent detection |
| Revisit gap-fill | ICEYE / Capella / TerraSAR-X (X-band), **NISAR** (L/S-band) | Daily monitoring, urban flood, vegetated floodplain |
| Supplementary | Sentinel-2, Landsat-9 | Validation, land-cover context (when cloud-free) |
| Terrain | Copernicus DEM 30 m / ALOS AW3D30 | Terrain correction, slope, **HAND** |
| Forcing | GPM/IMERG rainfall, river gauges, SMAP/S1 soil moisture | Predictive risk, nowcasting |
| Exposure / vulnerability | WorldPop, OSM, Microsoft building footprints, census, ESA WorldCover | Risk = hazard × exposure × vulnerability |

---

## 2. System pipeline

```
Copernicus Data Space / Microsoft Planetary Computer (STAC + S3)
        │
 [1] INGEST      AOI + date window → scene search, dedup, quality mask
        │
 [2] PREPROCESS  SNAP graph (containerised) or Python fallback:
                 thermal-noise removal → radiometric calibration →
                 speckle filter (Refined-Lee / NL-means) →
                 Range-Doppler terrain correction w/ DEM →
                 sub-pixel co-registration of pre-event / event pair
        │
 [3] FEATURES    σ°VV, σ°VH, VV/VH ratio, ΔdB(pre,event),
                 HAND, slope, TWI, distance-to-river, 7-day rainfall
        │
 [4] AI          A. Segmentation: dual-temporal U-Net/SegFormer → flood mask
                 B. Risk: susceptibility model + weighted overlay → 5 classes
                 C. Optional: LSTM discharge forecast (0–72 h)
        │
 [5] POST        mosaick → gap fill → vectorise → sliver cleanup
                 → Cloud-Optimised GeoTIFF + GeoJSON + STAC entry
        │
 [6] SERVE       FastAPI + TiTiler tiles + OGC API - Features (pygeoapi)
        │
 [7] CONSUME     MapLibre GL JS map, alerts (SMS/email/webhook), PDF/CSV export
```

Event-driven trigger: a new STAC item matching the AOI starts [1] automatically
(Prefect / Dagster flow), so a map is produced within minutes of scene delivery.

---

## 3. Removing map distortion

| Distortion | Cause | Fix |
|---|---|---|
| Salt-and-pepper noise | Speckle (multiplicative) | Refined-Lee or NL-means in preprocessing; learned despeckling (MSD) at scale |
| Geometric smear | Layover, foreshortening, shadow | **Range-Doppler terrain correction** with DEM — done once in the pipeline, never by hand in a GIS GUI |
| Pre/event misregistration | Orbit + terrain differences | Sub-pixel cross-correlation co-registration *before* differencing |
| Area / shape distortion | Web Mercator (EPSG:3857) stretching | Display in 3857, **compute every area and class in equal-area or local UTM**; publish native CRS in tile metadata |
| Class ambiguity | Ad-hoc symbology per map | Fixed deterministic class ramp, legend, scale bar, north arrow baked into export |
| Blurry zoom levels | Missing overviews | Internal COG overviews + vector tiles |

---

## 4. AI model design

**Flood extent (segmentation)**
- Architecture: dual-temporal U-Net / DeepLabV3+ / SegFormer.
- Inputs: pre-event and during-event σ° (VV, VH, VV/VH) → 6 channels.
- Loss: Dice + focal (flood class is a small fraction of pixels).
- Training data: **Sen1Floods11**, **WorldFloods** (350k pairs), **SEN12-FLOOD**,
  JRC Global Flood Database; fine-tune on your basin with Copernicus EMS final products.
- Target: IoU ≥ 0.70, boundary F-score reported per event.

**Risk zones**
- Don't train "risk" from scratch. Train a *susceptibility* surface
  (HAND, slope, rainfall, historical flood frequency, land use), then combine with
  exposure and vulnerability through a weighted overlay (AHP) or learned weights.
- Optional: surrogate a 2D hydraulic model (LISFLOOD-FP / HEC-RAS) with a GNN to
  return depth × velocity for each return period in seconds instead of hours.
- Output: 5-class raster (Very Low → Very High) + vectorised polygons + per-admin stats.

**Forecasting**
- LSTM/GRU on gauge + rainfall series → 0–72 h discharge/exceedance probability.

---

## 5. Replacing the ArcGIS effort

| ArcGIS task today | Replacement |
|---|---|
| Manual geoprocessing chains | SNAP graphs + Python — repeatable, one click, auditable |
| Reprojection / clip / mosaicking | GDAL + Rasterio inside the pipeline |
| Terrain analysis | SAGA/QGIS processing via script, `richdem`, `pysheds` |
| Manual delineation per event | Model runs automatically on a new scene → alert in minutes |
| Web map licences & credit limits | TiTiler + MapLibre GL JS (open source) |
| Server licences | Docker on any cloud; GPU only at inference time (ONNX Runtime / TensorRT) |
| Proprietary format lock-in | OGC-standard outputs (COG, GeoJSON, GeoPackage, OGC API) — ArcGIS and QGIS can both consume them |

Humans stay in the loop only for validation, weight selection, and sign-off.

---

## 6. Tech stack

| Concern | Choice |
|---|---|
| Orchestration | Prefect or Dagster (event-driven on new STAC item) |
| Language | Python 3.11 |
| SAR processing | ESA SNAP (`gpt`) in Docker, `PyroSAR`, `sarsen`, Rasterio |
| ML | PyTorch → exported to ONNX for deployment |
| Storage | S3/MinIO (COG), PostGIS (vectors), STAC catalog |
| API | FastAPI + pygeoapi (OGC API - Features/Processes) |
| Tiling | TiTiler (COG → XYZ tiles), PMTiles for offline AOI bundles |
| Frontend | React + MapLibre GL JS |
| Alerting | Rule engine on gauge + model probability → SMS/email/WhatsApp webhook |
| Ops | Docker Compose (dev) → Kubernetes (prod), GPU only for batch inference |

---

## 7. Deliverables / outputs

1. **Flood-extent map** — vector + raster, timestamped, change vs. previous pass.
2. **Risk-zone map** — 5 classes, stats per administrative unit (population, ha affected).
3. **0–72 h forecast** with probability contours.
4. **Exports** — GeoPackage, GeoJSON, PDF (legend + scale + north arrow),
   OGC API endpoint so existing ArcGIS/QGIS clients keep working.

---

## 8. Delivery phases

| Phase | Scope | Duration |
|---|---|---|
| **1 — PoC** (this repo) | Sentinel-1 ingest → preprocessing → U-Net inference → COG/GeoJSON map + web viewer | 3–4 weeks |
| **2 — MVP** | STAC event trigger, risk overlay, web map + API, alerts, per-admin stats | 6–8 weeks |
| **3 — Scale** | Multi-basin, multi-sensor revisit fusion, hydraulic surrogate, discharge forecasting | ongoing |

---

## 9. Risks & mitigations

| Risk | Mitigation |
|---|---|
| SAR-only misses shallow/vegetated water | Fuse L-band (NISAR/ALOS) + rainfall + hydraulic prior |
| Scarce local labels | Pre-train on Sen1Floods11/WorldFloods, few-shot fine-tune with Copernicus EMS finals |
| Terrain correction quality | Always run RTC; validate against ground control / optical reference |
| False positives (radar shadow, calm ponds) | HAND + slope constraint, temporal persistence across ≥2 passes |
| Data licensing | Copernicus data is free & open; commercial SAR only for gap-filling |

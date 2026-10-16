# FloodSentry

AI-based flood detection and risk-zone mapping from **satellite microwave (SAR) data** —
Sentinel-1 backscatter in, distortion-free flood + risk maps out, with the repetitive
ArcGIS geoprocessing replaced by a reproducible pipeline.

Design details: [ARCHITECTURE.md](ARCHITECTURE.md)

## Quick start (offline demo — no downloads)

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
pip install -e . --no-deps      # or: set PYTHONPATH=src

python -m flood_sentry.cli synth    # synthetic AOI + speckled SAR pair + risk layers
python -m flood_sentry.cli all      # preprocess → features → infer → vectorize → risk
python -m flood_sentry.cli train    # (optional) train the U-Net on the demo pair

python -m flood_sentry.api.main     # open http://127.0.0.1:8077
```

Without `train`, inference falls back to the physical backscatter-drop heuristic —
the pipeline always returns a map.

## Real Sentinel-1 data

```bash
python -m flood_sentry.cli ingest     # searches Planetary Computer, downloads VV/VH
python -m flood_sentry.cli all
```

Edit `config/config.yaml` to set your AOI bbox and pre-event / event date windows.
Ingest handles the two quirks of Planetary Computer Sentinel-1 COGs: georeferencing
is stored as GCPs (warped onto the AOI grid during download), and raw digital
numbers are auto-calibrated to σ⁰ dB. Use `config/config.smoke.yaml` for a tiny
AOI when smoke-testing real downloads:

```bash
python -m flood_sentry.cli ingest --config config\config.smoke.yaml
```

For production geometric fidelity set `preprocess.terrain_correction: snap`
(requires [ESA SNAP](https://step.esa.int/main/download/snap/) on PATH — it removes
layover/foreshortening/shadow via the graph in `graphs/rtc.xml`).

## Commands

| Command | What it does |
|---|---|
| `synth` | Generate a demo AOI: terrain, river, flood expansion, speckled VV/VH, risk layers |
| `ingest` | Search + download Sentinel-1 GRD scenes (VV/VH) for the AOI/date windows |
| `preprocess` | Linear→dB, Lee speckle filter, optional SNAP terrain correction, grid alignment |
| `features` | Build the change-detection stack: vv, vh, ratio, dvv, dvh (+ HAND, slope, rainfall, hist_freq, exposure) |
| `train` | Train the dual-temporal U-Net (Dice + BCE) on `data/features` + `data/labels` |
| `infer` | Flood extent map — U-Net if `data/models/unet.pt` exists, else physical heuristic (dB-drop threshold) |
| `vectorize` | Mask → GeoJSON polygons, areas measured in equal-area CRS (EPSG:6933) |
| `risk` | Normalised weighted overlay → 5-class risk zones (raster + GeoJSON) |
| `all` | Everything except `synth`/`ingest`/`train`, in order |

## API

| Endpoint | Description |
|---|---|
| `GET /` | MapLibre viewer (flood extent + risk zones, legend, stats) |
| `GET /api/flood` | Flood extent GeoJSON |
| `GET /api/risk` | 5-class risk zones GeoJSON |
| `GET /api/stats` | Flood fraction, polygon areas per class, method used |
| `POST /api/run` | Re-run the whole pipeline |
| `GET /api/health` | Status + available artifacts |

## Layout

```
flood-sentry/
├── ARCHITECTURE.md          # full system design
├── config/config.yaml       # AOI, dates, filter settings, risk weights, paths
├── graphs/rtc.xml           # SNAP Range-Doppler terrain correction graph
├── scripts/make_synthetic.py
├── web/index.html           # MapLibre GL JS viewer
└── src/flood_sentry/
    ├── ingest/stac.py       # Sentinel-1 search + windowed download
    ├── preprocess/sar.py    # dB, Lee filter, co-registration, SNAP hook
    ├── features/build.py    # feature stack (change detection + statics)
    ├── models/unet.py       # segmentation network
    ├── train.py / infer.py
    ├── post/vectorize.py    # raster → clean polygons (equal-area areas)
    ├── risk/overlay.py      # weighted risk overlay → 5 classes
    ├── api/main.py          # FastAPI + tile/viewer server
    ├── synthetic.py         # offline demo data
    └── cli.py
```

## Notes on correctness

- **No distorted areas:** geometries are measured in EPSG:6933 (equal-area) and
  published in EPSG:4326; Web Mercator display never affects reported numbers.
- **No false change:** pre/event rasters are phase-correlation co-registered before differencing.
- **Always answers:** if PyTorch or a checkpoint is missing, inference falls back to the
  physical backscatter-drop heuristic so a map is still produced.

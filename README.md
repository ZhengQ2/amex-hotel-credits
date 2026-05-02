# Amex Credit Map — Hilton · FHR · THC

An interactive map of hotels eligible for resort credits under three premium programs:

- **Hilton Honors** — resort credit-eligible properties
- **Fine Hotels + Resorts (FHR)** — AmEx Platinum / Centurion benefit
- **The Hotel Collection (THC)** — AmEx premium property collection

Hotel lists are scraped nightly, geocoded via Google Places, and published to a [Leaflet.js map](https://ZhengQ2.github.io/amex-hotel-credits/) with marker clustering.

---

## How it works

```
hilton.py / fhrthc.py        →   cache/*.csv          (scraped hotel lists)
google-convert.py            →   cache/*_geocoded.csv  (lat/lon + address)
generate_map_data.py         →   docs/hotels.json      (merged map data)
docs/index.html                                         (interactive map)
```

1. **Scrape** — `hilton.py` uses Playwright to scrape Hilton's resort credit page. `fhrthc.py` parses the AmEx Travel HTML directly.
2. **Geocode** — `google-convert.py` looks up each hotel via the Google Places API. Results are cached in `cache/geocode_cache_google_*.json` so only new or changed hotels hit the API.
3. **Generate** — `generate_map_data.py` merges the three geocoded CSVs into `docs/hotels.json`.
4. **Publish** — GitHub Actions commits the updated files and GitHub Pages serves the map.

---

## Local setup

**Requirements:** Python 3.11+

```bash
pip install playwright pandas requests tenacity python-dotenv gspread google-auth
python -m playwright install --with-deps chromium
mkdir -p cache
```

**Run the full pipeline:**

```bash
python hilton.py
python fhrthc.py
python google-convert.py --input cache/hilton_hotels.csv \
    --output cache/hilton_hotels_geocoded_google.csv \
    --cache cache/geocode_cache_google_hilton.json \
    --input-format hilton
python google-convert.py --input cache/fhr_hotels.csv \
    --output cache/fhr_hotels_geocoded_google.csv \
    --cache cache/geocode_cache_google_fhr.json \
    --input-format fhrthc
python google-convert.py --input cache/thc_hotels.csv \
    --output cache/thc_hotels_geocoded_google.csv \
    --cache cache/geocode_cache_google_thc.json \
    --input-format fhrthc
python generate_map_data.py
```

---

## Environment variables

| Variable | Used by | Purpose |
|---|---|---|
| `GOOGLE_MAPS_API_KEY` | `google-convert.py` | Google Places + Geocoding API |
| `GOOGLE_SERVICE_ACCOUNT_KEY` | CI | Google Sheets sync (JSON credentials) |
| `HILTON_SHEET_ID` | CI | Target Google Sheet for Hilton results |
| `FHR_SHEET_ID` | CI | Target Google Sheet for FHR results |
| `THC_SHEET_ID` | CI | Target Google Sheet for THC results |
| `SMTP_*`, `EMAIL_TO/FROM` | CI | Email alerts when lists change |

---

## Nightly CI

Two GitHub Actions workflows run on a daily schedule:

| Workflow | Schedule (UTC) | Covers |
|---|---|---|
| `hilton-nightly.yml` | 05:00 | Hilton resort credit hotels |
| `fhr-thc-nightly.yml` | 06:00 | FHR and THC hotels |

Each workflow:
1. Scrapes the latest hotel list
2. Compares against the previous run to detect additions, removals, or renames
3. Re-geocodes only if the list changed (to minimise API spend)
4. Syncs results to Google Sheets and sends an email summary
5. Commits updated cache files and `docs/hotels.json` back to the repo

Workflows can also be triggered manually via `workflow_dispatch`.

---

## Project structure

```
hilton.py                  # Scraper — Hilton (Playwright)
fhrthc.py                  # Scraper — FHR & THC (HTML parser)
google-convert.py          # Geocoder — Google Places API, with caching
generate_map_data.py       # Builds docs/hotels.json from geocoded CSVs
cache_utils.py             # Shared cache read/write + fuzzy matching helpers
cache/
  hilton_hotels.csv                    # Raw scraped hotel list
  fhr_hotels.csv
  thc_hotels.csv
  hilton_hotels_geocoded_google.csv    # Geocoded output (lat, lon, address, …)
  fhr_hotels_geocoded_google.csv
  thc_hotels_geocoded_google.csv
  geocode_cache_google_hilton.json     # API response cache
  geocode_cache_google_fhr.json
  geocode_cache_google_thc.json
docs/
  index.html               # Interactive Leaflet map
  hotels.json              # Compiled hotel data consumed by the map
```

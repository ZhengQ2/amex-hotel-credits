# Amex Hotel Credits Map

An automatically refreshed map of hotels participating in these programs:

- Hilton Honors resort-credit eligible hotels
- American Express Fine Hotels + Resorts (FHR)
- American Express The Hotel Collection (THC)

**Website:** [zhengq2.github.io/amex-hotel-credits](https://zhengq2.github.io/amex-hotel-credits/)

The published site is a static Google Map in [`docs/`](docs/) with searchable,
filterable hotel markers. Its data is generated from the geocoded CSVs in
[`cache/`](cache/).

## How it works

1. `hilton.py` uses Playwright to collect Hilton's resort-credit eligible
   properties, grouped by brand.
2. `fhrthc.py` fetches and parses the Amex Travel property-result pages, then
   separates FHR and THC properties.
3. `google-convert.py` geocodes each list with the Google Places API, falling
   back to the Google Geocoding API when needed. JSON caches avoid repeating
   successful lookups and preserve unresolved results.
4. `generate_map_data.py` combines the three geocoded CSVs into
   `docs/hotels.json`, which `docs/index.html` renders as the map.

GitHub Actions runs the Hilton and FHR/THC pipelines nightly. When the hotel
data changes, the workflows update the map data, commit the cache and map
outputs, and can update configured Google Sheets and email recipients.

## Run locally

Use Python 3.11 or newer. Create and activate a virtual environment, then
install the dependencies:

```bash
python -m pip install playwright pandas requests tenacity python-dotenv
python -m playwright install chromium
```

Add a Google Maps API key to a local `.env` file (which is ignored by Git):

```dotenv
GOOGLE_MAPS_API_KEY=your_key_here
```

Refresh all source lists, geocode them, and rebuild the map data:

```bash
python hilton.py
python fhrthc.py

python google-convert.py \
  --input cache/hilton_hotels.csv \
  --output cache/hilton_hotels_geocoded_google.csv \
  --cache cache/geocode_cache_google_hilton.json \
  --input-format hilton

python google-convert.py \
  --input cache/fhr_hotels.csv \
  --output cache/fhr_hotels_geocoded_google.csv \
  --cache cache/geocode_cache_google_fhr.json \
  --input-format fhrthc

python google-convert.py \
  --input cache/thc_hotels.csv \
  --output cache/thc_hotels_geocoded_google.csv \
  --cache cache/geocode_cache_google_thc.json \
  --input-format fhrthc

python generate_map_data.py
```

Run `python hilton.py --headed` to watch the Hilton browser session while
troubleshooting a scrape.

## Repository layout

| Path | Purpose |
| --- | --- |
| `hilton.py` | Scrapes Hilton's resort-credit hotel list and compares it with the cache. |
| `fhrthc.py` | Scrapes Amex Travel FHR/THC listings and compares them with their caches. |
| `google-convert.py` | Places-first geocoding command-line tool. |
| `cache_utils.py` | Shared cache normalization, fuzzy matching, and maintenance helpers. |
| `generate_map_data.py` | Produces the compact map dataset. |
| `cache/` | Geocode caches and generated geocoded CSVs. Raw scraper CSVs are intentionally ignored. |
| `docs/` | Static map page and generated `hotels.json` for GitHub Pages. |
| `.github/workflows/` | Scheduled refresh, optional Sheets/email notification, and automatic commit workflows. |

## Notes

- Geocoding requires a Google Maps API key with Places and Geocoding API
  access. It may incur Google Maps Platform charges.
- The map requires a separate browser key with the **Maps JavaScript API**
  enabled. Store it as the `GOOGLE_MAPS_BROWSER_API_KEY` GitHub Actions secret;
  the Pages workflow inserts it only into the deployment artifact. Restrict it
  by HTTP referrer to `https://zhengq2.github.io/amex-hotel-credits/*` and any
  custom-domain URL used by the site. Browser keys are intentionally public at
  runtime; never commit one or use the server-side geocoding key here.
- The map is informational: eligibility, credits, benefits, availability, and
  booking terms are determined by Hilton, American Express, and the applicable
  card terms at the time of booking.
- Generated files are committed so the static map can be served without a
  backend. Do not commit `.env`, service-account credentials, or other secrets.

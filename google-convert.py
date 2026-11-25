#!/usr/bin/env python3
"""
Geocode Hilton hotels using Google **Places API** (Text Search + Place Details),
with fallback to the classic Geocoding API only if Places yields nothing acceptable.

Inputs
- CSV with columns: hotel_name, group_label; optional: hotel_url

Outputs
- CSV with precise coordinates (favoring rooftop-quality Place Details),
  cached results, and status info per row.

Env
- GOOGLE_MAPS_API_KEY  (same as before)

Notes
- We now **keep** discriminators like "Hotel" / "Resort" in the query.
- We accept results only if they look like lodging (or a clear property premise).
- Results are cached by the query set to reduce API calls.
"""

import os
import json
import time
import re
import random
import argparse
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List
from urllib.parse import urlparse, unquote

import requests
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from dotenv import load_dotenv

from cache_utils import (
    address_location_compatible,
    find_fuzzy_cache_key,
    is_lodging_type,
    is_strict_lodging_type,
    normalize_name,
)

load_dotenv()

# -----------------------
# Config defaults (can be overridden via CLI)
# -----------------------
INPUT_PATH  = "cache/hilton_hotels.csv"  # hilton mode: hotel_name, group_label; fhrthc mode: hotel_name, hotel_location
OUTPUT_PATH = "cache/hilton_hotels_geocoded_google.csv"
CACHE_PATH  = "cache/geocode_cache_google_hilton.json"
INPUT_FORMAT = "auto"  # auto|hilton|fhrthc

REGION_BIAS = ""       # e.g., "US", "CA", "GB" or "" for none (used by Geocoding fallback)
BASE_DELAY  = 0.0       # seconds between requests (jittered)
ONLY_MISSING = False    # if True, skip rows already having lat/lon in OUTPUT_PATH
OFFSET = 0              # start after this many rows (post-filter)
MAX_ROWS = 0            # process at most this many (0 = all)

# --- Google endpoints ---
PLACES_TEXT_SEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
PLACES_DETAILS_URL     = "https://maps.googleapis.com/maps/api/place/details/json"
GEOCODE_URL            = "https://maps.googleapis.com/maps/api/geocode/json"  # fallback only
API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

CACHE_VERSION = 3  # bumped for Places pipeline

class GeocodeError(Exception):
    pass

# -----------------------
# Query building
# -----------------------

def clean_query(text: str) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    t = re.sub(r"[®™]", "", t)
    return t.strip(" ,-")

# Keep discriminators like hotel/resort; only drop true glue words
STOPWORDS = {"by","the","and","at","&"}


def _slug_words_from_url(url: str) -> List[str]:
    if not url:
        return []
    try:
        p = urlparse(url)
        segs = [s for s in p.path.split("/") if s]
        if not segs:
            return []
        # For hilton.com/en/hotels/PROPERTY-SLUG/... use PROPERTY-SLUG, not the
        # last segment which could be a room/category sub-page.
        try:
            hotels_idx = segs.index("hotels")
            if hotels_idx + 1 < len(segs):
                slug = unquote(segs[hotels_idx + 1])
            else:
                slug = unquote(segs[-1])
        except ValueError:
            slug = unquote(segs[-1])
        slug = re.sub(r"[^A-Za-z0-9\- ]+", " ", slug)
        words = re.split(r"[\s\-]+", slug)
        return [w for w in words if w]
    except Exception:
        return []


def _title_like_from_words(words: List[str]) -> str:
    if not words:
        return ""
    # Don't over-strip short slugs; keep most words to retain specificity
    keep = [w for w in words if (len(words) <= 3 or w.lower() not in STOPWORDS)]
    s = " ".join(keep) if keep else " ".join(words)
    s = re.sub(r"\bHilton\b", "Hilton", s, flags=re.I)
    return s.strip()


def build_queries(hotel_name: str, brand: str, hotel_url: str = "", hotel_location: str = "") -> List[str]:
    """Build strongest → weakest search strings for Places Text Search.
    We keep discriminators and try brand spellings.
    """
    queries: List[str] = []

    def add(q: str):
        q = clean_query(q)
        if q and q not in queries:
            queries.append(q)

    slug_title = _title_like_from_words(_slug_words_from_url(hotel_url))
    brand_tokens = [brand, "Hilton"] if brand and brand.lower() != "hilton" else (["Hilton"] if brand else [""])
    # Prefer explicit hotel_name over URL slug to avoid noisy slug artifacts
    # dominating the candidate set.
    name_candidates = [s for s in [hotel_name, slug_title] if s]
    location = clean_query(hotel_location)

    # Strong variants: include Hotel/Resort suffixes explicitly to skew toward lodging
    for name in name_candidates:
        for b in brand_tokens:
            add(f"{name} {b} Hotel")
            add(f"{name} {b} Resort")
            add(f"{name} {b}")
            if location:
                add(f"{name} {b} {location} Hotel")
                add(f"{name} {b} {location} Resort")
                add(f"{name} {b} {location}")

    # Fallbacks, progressively weaker
    if hotel_name:
        add(f"{hotel_name} Hotel")
        add(f"{hotel_name} Resort")
        add(hotel_name)
        if location:
            add(f"{hotel_name} {location} Hotel")
            add(f"{hotel_name} {location} Resort")
            add(f"{hotel_name} {location}")

    return queries


# -----------------------
# Google API helpers (Places-first)
# -----------------------

@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.8, min=1, max=6),
    retry=retry_if_exception_type((requests.RequestException, GeocodeError)),
)
def places_text_search(query: str, api_key: str) -> Dict[str, Any]:
    if not api_key:
        raise GeocodeError("Missing GOOGLE_MAPS_API_KEY")
    params = {"query": query, "key": api_key}
    r = requests.get(PLACES_TEXT_SEARCH_URL, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    status = data.get("status", "")
    if status == "OK":
        return data
    if status in ("ZERO_RESULTS",):
        return {"results": []}
    msg = data.get("error_message") or status or "UNKNOWN"
    raise GeocodeError(f"Places Text Search error: {msg}")


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.8, min=1, max=6),
    retry=retry_if_exception_type((requests.RequestException, GeocodeError)),
)
def places_details(place_id: str, api_key: str) -> Optional[Dict[str, Any]]:
    params = {
        "place_id": place_id,
        "key": api_key,
        "fields": "place_id,name,geometry,formatted_address,types,business_status,website"
    }
    r = requests.get(PLACES_DETAILS_URL, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    status = data.get("status", "")
    if status == "OK":
        return data.get("result")
    if status in ("NOT_FOUND", "ZERO_RESULTS"):
        return None
    msg = data.get("error_message") or status or "UNKNOWN"
    raise GeocodeError(f"Places Details error: {msg}")


# Optional, limited fallback to classic geocoder if Places fails
@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.8, min=1, max=6),
    retry=retry_if_exception_type((requests.RequestException, GeocodeError)),
)
def geocode_google(query: str, api_key: str, region_bias: str = "") -> Optional[Dict[str, Any]]:
    if not api_key:
        raise GeocodeError("Missing GOOGLE_MAPS_API_KEY")
    params = {"address": query, "key": api_key}
    if region_bias:
        params["region"] = region_bias
    resp = requests.get(GEOCODE_URL, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    status = data.get("status", "")
    if status == "OK":
        r0 = data["results"][0]
        loc = r0["geometry"]["location"]
        return {
            "lat": float(loc["lat"]),
            "lon": float(loc["lng"]),
            "formatted_address": r0.get("formatted_address"),
            "provider": "google_geocoding",
            "confidence": r0.get("geometry", {}).get("location_type"),
            "place_id": r0.get("place_id"),
            "types": r0.get("types", []),
            "partial_match": r0.get("partial_match", False),
            "raw": r0,
        }
    if status in ("ZERO_RESULTS",):
        return None
    if status in ("OVER_QUERY_LIMIT", "REQUEST_DENIED", "INVALID_REQUEST", "UNKNOWN_ERROR"):
        msg = data.get("error_message") or status
        raise GeocodeError(f"Google Geocoding error: {msg}")
    msg = data.get("error_message") or status or "UNKNOWN"
    raise GeocodeError(f"Google Geocoding error: {msg}")


# -----------------------
# Selection & acceptance rules
# -----------------------

BRAND_HINTS = [
    # minimal — adjust as needed
    "Hilton", "DoubleTree", "Curio", "Tapestry", "Conrad", "Waldorf",
    "Hilton Garden Inn", "Hampton", "Embassy Suites", "Homewood Suites",
    "Home2 Suites", "Motto", "Tempo", "Signia", "Canopy", "Tru"
]

GENERIC_NAME_TOKENS = {
    "hotel", "resort", "spa", "and", "the", "at", "by", "&",
    "hilton", "inn", "suites", "collection", "club", "property"
}


def _meaningful_tokens(text: str) -> set:
    return {
        tok
        for tok in normalize_name(text).split()
        if tok and tok not in GENERIC_NAME_TOKENS and len(tok) > 1
    }


def _name_similarity(place_name: str, hotel_name: str) -> float:
    p = _meaningful_tokens(place_name)
    h = _meaningful_tokens(hotel_name)
    if not p or not h:
        return 0.0

    overlap = len(p & h)
    if overlap == 0:
        return 0.0
    # F1-style: harmonic mean of precision and recall so that a short hotel
    # name cannot achieve a perfect score against a much longer candidate name.
    # e.g. "The Georgian" vs "Georgian Bay Hotel, Trademark Collection by Wyndham"
    # gives recall=1.0 but precision=0.25, so F1=0.40 — correctly rejected.
    precision = overlap / len(p)
    recall = overlap / len(h)
    return 2 * precision * recall / (precision + recall)


def _is_name_compatible(place_name: str, hotel_name: str) -> bool:
    score = _name_similarity(place_name, hotel_name)
    if score >= 0.6:
        return True

    # Fallback: allow near-subsets (e.g. "Park Kyoto" matching "Park Hyatt Kyoto")
    # but also require a minimum precision so a short hotel name cannot match a
    # much longer candidate on just one token (the Georgian / Georgian Bay case).
    p = _meaningful_tokens(place_name)
    h = _meaningful_tokens(hotel_name)
    if not p or not h:
        return False
    overlap = len(p & h)
    return overlap >= max(1, min(len(p), len(h)) - 1) and overlap / len(p) >= 0.3


_is_strict_lodging_type = is_strict_lodging_type
_is_lodging_type = is_lodging_type


def _brand_in_name(name: str, brand: str) -> bool:
    name_l = normalize_name(name or "")
    b = normalize_name(brand or "")
    if not name_l:
        return False
    # direct brand token
    if b and b in name_l:
        return True
    # any Hilton family hint
    return any(normalize_name(h) in name_l for h in BRAND_HINTS)


def choose_best_place(
    results: List[Dict[str, Any]],
    brand: str,
    hotel_name: str,
    hotel_location: str = "",
) -> Optional[Dict[str, Any]]:
    if not results:
        return None

    # Drop candidates in the wrong country before ranking. Text Search already
    # returns formatted_address, so this costs nothing and also avoids paying
    # for a Place Details lookup on a candidate we would reject afterwards.
    # Without it, ranking is driven by user_ratings_total, so a famous namesake
    # beats the actual property - "San Antonio" (a hotel in Imerovigli, Greece)
    # loses to San Antonio, Texas.
    if hotel_location:
        in_country = [
            r for r in results
            if _location_compatible(r.get("formatted_address", ""), hotel_location)
        ]
        if in_country:
            results = in_country
        else:
            # Nothing in the right country for this query; let the caller try
            # the next, weaker query rather than accept a known-wrong place.
            return None

    # Rank: (acceptable type, name compatibility, strict lodging, brand hit, rating, reviews)
    ranked = []
    for r in results:
        t = r.get("types", [])
        acceptable = 1 if _is_lodging_type(t) else 0
        strict_lodging = 1 if _is_strict_lodging_type(t) else 0
        brand_hit = 1 if _brand_in_name(r.get("name"), brand) else 0
        rating = 1 if r.get("rating") is not None else 0
        ur = int(r.get("user_ratings_total") or 0)
        name_hit = 1 if _is_name_compatible(r.get("name", ""), hotel_name) else 0
        score = (acceptable, name_hit, strict_lodging, brand_hit, rating, ur)
        ranked.append((score, r))
    ranked.sort(key=lambda x: x[0], reverse=True)
    best = ranked[0][1]
    # Hard guard against near-miss sibling properties and non-lodging noise (e.g. parking).
    if not _is_lodging_type(best.get("types", [])):
        return None
    if not _is_name_compatible(best.get("name", ""), hotel_name):
        return None
    return best


def acceptable_from_details(details: Dict[str, Any]) -> bool:
    if not details:
        return False
    if details.get("business_status") == "CLOSED_PERMANENTLY":
        # Still could be a real rooftop, but generally not desired for active lists
        pass
    if not details.get("geometry"):
        return False
    if not _is_lodging_type(details.get("types", [])):
        # Allow premises if clearly a property
        return False
    return True


# How long a confirmed location/type mismatch is trusted before we try again.
#
# Re-geocoding a geographically wrong or non-lodging cache entry only heals it
# if Google's answer has actually changed. When it hasn't, the entry is
# rejected, re-fetched, and rejected again on every run - a paid Places call
# per hotel per night, with no path to convergence. Re-check on a slow cadence
# instead, so a genuinely improved Google result is still picked up eventually.
LOCATION_MISMATCH_RECHECK_DAYS = 30

# Stored on a cached result whose location or place type we have re-verified and
# found still wrong. Leading underscore keeps it clear this is our bookkeeping,
# not part of the Places payload; output rows are built from named fields, so it
# is ignored everywhere downstream.
_MISMATCH_STAMP = "_location_mismatch_confirmed_at"
_MISMATCH_SELECTOR_V = "_location_mismatch_selector_v"

# Bumped whenever candidate selection changes in a way that could turn a
# previously unresolvable hotel into a resolvable one. A stamp recorded by an
# older selector is not evidence about the current one, so it is ignored and the
# hotel gets one fresh attempt instead of waiting out the recheck window.
#   1 - selection ignored hotel_location entirely
#   2 - candidates in the wrong country are filtered out before ranking
#   3 - reject non-lodging noise types, fold accents in name check, enforce US/CA state guard
SELECTOR_VERSION = 3


def _mismatch_recheck_due(entry: Dict[str, Any]) -> bool:
    """True when a known-wrong-location/type entry is due for another geocode attempt."""
    if not isinstance(entry, dict):
        return True
    stamp = entry.get(_MISMATCH_STAMP)
    if not isinstance(stamp, (int, float)):
        return True  # never re-verified before, so try once
    if entry.get(_MISMATCH_SELECTOR_V) != SELECTOR_VERSION:
        return True  # recorded under different selection logic; re-test once
    return (time.time() - stamp) / 86400.0 >= LOCATION_MISMATCH_RECHECK_DAYS


_location_compatible = address_location_compatible


# -----------------------
# Orchestration
# -----------------------

def geocode_places_first(
    queries: List[str],
    brand: str,
    hotel_name: str,
    base_delay: float,
    hotel_location: str = "",
) -> Tuple[Optional[Dict[str, Any]], str]:
    last = "NO_RESULT"
    last_err: Optional[str] = None
    had_clean_empty = False

    # Try location-qualified queries first. Each query is a billed Text Search,
    # and for an ambiguous name ("San Antonio", "Mason") the bare-name variants
    # reliably return a famous namesake that the location filter then discards -
    # pure spend for no result. Ordering costs nothing: the cache key stores the
    # query list as built, so reordering execution here does not change any key
    # or invalidate a single cached entry.
    if hotel_location:
        loc_needle = hotel_location.split(",")[0].strip().lower()
        queries = sorted(
            queries,
            key=lambda q: 0 if loc_needle and loc_needle in q.lower() else 1,
        )

    for q in queries:
        try:
            ts = places_text_search(q, API_KEY)
            cand = choose_best_place(ts.get("results", []), brand, hotel_name, hotel_location)
            if cand:
                det = places_details(cand["place_id"], API_KEY)
                if det and acceptable_from_details(det):
                    loc = det["geometry"]["location"]
                    return {
                        "lat": float(loc["lat"]),
                        "lon": float(loc["lng"]),
                        "formatted_address": det.get("formatted_address"),
                        "provider": "google_places",
                        "confidence": "PLACE_DETAILS",  # explicit
                        "place_id": det.get("place_id"),
                        "types": det.get("types", []),
                        "partial_match": False,
                        "raw": det,
                    }, f"PLACES:{q}"
            # limited fallback to classic geocoder for this query
            geo = geocode_google(q + " Hotel", API_KEY, REGION_BIAS)
            if (
                geo
                and geo.get("confidence", "").upper() == "ROOFTOP"
                and _is_lodging_type(geo.get("types", []))
                and _is_name_compatible(geo.get("raw", {}).get("name", "") or q, hotel_name)
                and _location_compatible(geo.get("formatted_address", ""), hotel_location)
            ):
                return geo, f"GEOCODE:{q}"
            last = f"WEAK_OR_EMPTY:{q}"
            had_clean_empty = True
        except (GeocodeError, requests.RequestException) as e:
            last = f"ERR:{e}"
            last_err = last
        finally:
            if base_delay > 0:
                time.sleep(base_delay + random.uniform(0, base_delay * 0.25))
    if last_err and had_clean_empty:
        return None, f"PARTIAL_ERR:{last_err}"
    return None, (last_err or last)


# -----------------------
# Main
# -----------------------

def load_cache(path: Path) -> Dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Geocode hotel CSV rows with Google Places-first flow.")
    p.add_argument("--input", default=INPUT_PATH, help="Input CSV path")
    p.add_argument("--output", default=OUTPUT_PATH, help="Output CSV path")
    p.add_argument("--cache", default=CACHE_PATH, help="Cache JSON path")
    p.add_argument(
        "--input-format",
        default=INPUT_FORMAT,
        choices=["auto", "hilton", "fhrthc"],
        help="Input schema mode. fhrthc expects optional brand_label + hotel_location.",
    )
    return p.parse_args()


def _clean_cell(value: Any) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def detect_input_format(df: pd.DataFrame, requested_format: str) -> str:
    if requested_format != "auto":
        return requested_format
    if "hotel_location" in df.columns and "brand_label" in df.columns:
        return "fhrthc"
    return "hilton"


def main():
    args = parse_args()

    if not API_KEY:
        raise SystemExit("Set GOOGLE_MAPS_API_KEY env var (or put it in .env).")

    # Load input
    input_path = args.input
    output_path = args.output
    cache_path = args.cache
    df = pd.read_csv(input_path)
    input_format = detect_input_format(df, args.input_format)

    required = {"hotel_name", "group_label"}
    if input_format == "fhrthc":
        required = {"hotel_name", "hotel_location"}
    if not required.issubset(df.columns):
        raise SystemExit(f"Input format '{input_format}' must contain columns: {', '.join(sorted(required))}")
    if "hotel_url" not in df.columns:
        df["hotel_url"] = None
    if "group_label" not in df.columns:
        df["group_label"] = None
    if input_format == "fhrthc" and "brand_label" not in df.columns:
        df["brand_label"] = None
    if input_format == "fhrthc" and "hotel_location" not in df.columns:
        df["hotel_location"] = None

    # Resume support
    already = set()
    if ONLY_MISSING and Path(output_path).exists():
        prev = pd.read_csv(output_path)
        if {"hotel_name", "group_label", "lat", "lon"}.issubset(prev.columns):
            for _, r in prev.dropna(subset=["lat", "lon"]).iterrows():
                already.add((str(r["hotel_name"]).strip().lower(), str(r["group_label"]).strip().lower()))

    cache_path_obj = Path(cache_path)
    cache = load_cache(cache_path_obj)

    work = df.copy()
    if ONLY_MISSING:
        mask = ~work.apply(lambda r: (str(r["hotel_name"]).strip().lower(),
                                      str(r["group_label"]).strip().lower()) in already, axis=1)
        work = work[mask]
    if OFFSET:
        work = work.iloc[OFFSET:]
    if MAX_ROWS and MAX_ROWS > 0:
        work = work.iloc[:MAX_ROWS]

    rows = []
    total = len(work)
    for idx, row in enumerate(work.itertuples(index=False), 1):
        hotel = _clean_cell(getattr(row, "hotel_name", ""))
        hotel_url = _clean_cell(getattr(row, "hotel_url", ""))
        hotel_location = _clean_cell(getattr(row, "hotel_location", ""))
        group_label = _clean_cell(getattr(row, "group_label", ""))
        group_type = _clean_cell(getattr(row, "group_type", "Brand")) or "Brand"

        if input_format == "fhrthc":
            brand = _clean_cell(getattr(row, "brand_label", ""))
            cache_scope = group_label or brand
        else:
            brand = group_label
            cache_scope = group_label

        queries = build_queries(hotel, brand, hotel_url, hotel_location)
        cache_key = json.dumps({
            "v": CACHE_VERSION,
            "provider": "google_places_first",
            "input_format": input_format,
            "hotel_name": hotel,
            "queries": queries,
            "brand": cache_scope,
        }, sort_keys=True)

        cached = cache.get(cache_key, None)
        res = None
        status = None
        needs_geocode = cached is None
        stale_cached: Optional[Dict[str, Any]] = None
        stale_reason = ""

        if cached is not None:
            is_no_result = isinstance(cached, dict) and cached.get("status") == "NO_RESULT"
            res = None if is_no_result else cached
            if is_no_result and _MISMATCH_STAMP in cached and _mismatch_recheck_due(cached):
                # Stamped partial-error NO_RESULT whose recheck window elapsed.
                needs_geocode = True
                status = "CACHED:NO_RESULT_RECHECK"
            else:
                bad_location = bool(
                    res and not _location_compatible(res.get("formatted_address", ""), hotel_location)
                )
                bad_type = bool(
                    res and res.get("types") and not _is_lodging_type(res.get("types", []))
                )
                if bad_location or bad_type:
                    if _mismatch_recheck_due(res):
                        # Cached result is geographically wrong or a non-lodging POI
                        # (e.g. parking lot / standalone restaurant); try fuzzy cache
                        # reuse or re-geocode while preserving the prior entry in case
                        # the retry errors or finds no lodging replacement.
                        stale_cached = res
                        stale_reason = "LOCATION_MISMATCH" if bad_location else "TYPE_MISMATCH"
                        res = None
                        needs_geocode = True
                        status = f"CACHED:{stale_reason}"
                    else:
                        # Already re-geocoded recently under the current selector and
                        # Google still returned an invalid result; avoid burning quota.
                        status = (
                            "CACHED:LOCATION_MISMATCH_CONFIRMED"
                            if bad_location
                            else "CACHED:TYPE_MISMATCH_CONFIRMED"
                        )
                else:
                    status = "CACHED" if res else "CACHED:NO_RESULT"

        if needs_geocode:
            # Fuzzy fallback: reuse cached geocoding when the hotel name or
            # location formatting changed slightly, or when an exact entry was
            # rejected for wrong state/non-lodging type and a valid older entry
            # already exists. Never reuse NO_RESULT across renamed keys so a
            # renamed hotel always gets a fresh lookup.
            fuzzy_key = find_fuzzy_cache_key(
                hotel,
                cache,
                input_format=input_format,
                hotel_location=hotel_location,
                scope_key=cache_scope,
            )
            if fuzzy_key is not None and cache[fuzzy_key] is not None:
                candidate = cache[fuzzy_key]
                candidate = (
                    None
                    if isinstance(candidate, dict) and candidate.get("status") == "NO_RESULT"
                    else candidate
                )
                candidate_valid = bool(
                    candidate
                    and _location_compatible(candidate.get("formatted_address", ""), hotel_location)
                    and (not candidate.get("types") or _is_lodging_type(candidate.get("types", [])))
                )
                if candidate_valid:
                    res = candidate
                    status = "CACHED:RENAMED"
                    # Persist under the new key so future runs hit it exactly.
                    cache[cache_key] = cache[fuzzy_key]
                    save_cache(cache_path_obj, cache)
                # else: fuzzy match is NO_RESULT or fails validation — fall through to geocode

            if res is None:
                res, status = geocode_places_first(queries, brand, hotel, BASE_DELAY, hotel_location)
                api_error = bool(status and status.startswith("ERR:"))
                partial_error = bool(status and status.startswith("PARTIAL_ERR:"))
                if res is None:
                    if api_error:
                        # Transient Google/network failure: never persist NO_RESULT
                        # and keep any prior cached pin un-stamped so the next run
                        # retries cleanly when the API recovers.
                        if stale_cached is not None:
                            res = stale_cached
                            status = f"CACHED:RETRY_ERR:{status}"
                    elif partial_error:
                        if stale_cached is not None:
                            res = stale_cached
                            status = f"CACHED:RETRY_ERR:{status}"
                        else:
                            # One query errored (e.g. INVALID_REQUEST) while other
                            # queries completed cleanly with no result: store a
                            # stamped NO_RESULT so the lookup is bounded instead of
                            # repeating every night, yet still retries after the window.
                            cache[cache_key] = {
                                "status": "NO_RESULT",
                                _MISMATCH_STAMP: time.time(),
                                _MISMATCH_SELECTOR_V: SELECTOR_VERSION,
                            }
                            save_cache(cache_path_obj, cache)
                    elif stale_cached is not None and stale_reason == "TYPE_MISMATCH":
                        # Plausibly located pin failed only on place type and the
                        # retry found no replacement: retain and stamp the prior
                        # entry instead of blanking the hotel permanently.
                        stamped = dict(stale_cached)
                        stamped[_MISMATCH_STAMP] = time.time()
                        stamped[_MISMATCH_SELECTOR_V] = SELECTOR_VERSION
                        res = stamped
                        status = f"{status}:TYPE_MISMATCH_KEPT"
                        cache[cache_key] = stamped
                        save_cache(cache_path_obj, cache)
                    else:
                        cache[cache_key] = {"status": "NO_RESULT"}
                        save_cache(cache_path_obj, cache)
                else:
                    to_store = res
                    fresh_bad_location = not _location_compatible(
                        res.get("formatted_address", ""), hotel_location
                    )
                    fresh_bad_type = bool(
                        res.get("types") and not _is_lodging_type(res.get("types", []))
                    )
                    if fresh_bad_location or fresh_bad_type:
                        # The fresh lookup is still invalid, so this query set cannot
                        # currently resolve this hotel cleanly. Stamp the entry so
                        # the next run doesn't repeat the same paid call.
                        to_store = dict(res)
                        to_store[_MISMATCH_STAMP] = time.time()
                        to_store[_MISMATCH_SELECTOR_V] = SELECTOR_VERSION
                        suffix = "LOCATION_MISMATCH" if fresh_bad_location else "TYPE_MISMATCH"
                        status = f"{status}:{suffix}"
                    cache[cache_key] = to_store
                    save_cache(cache_path_obj, cache)

        out = {
            "hotel_name": hotel,
            "hotel_url": hotel_url or None,
            "hotel_location": hotel_location or None,
            "group_label": group_label or None,
            "brand_label": brand or None,
            "group_type": group_type,
            "lat": None,
            "lon": None,
            "formatted_address": None,
            "provider": "google_places_first",
            "confidence": None,
            "place_id": None,
            "types": None,
            "partial_match": None,
            "status": status if res else f"NO_RESULT:{status}",
        }
        if res:
            types_out = res.get("types", [])
            out.update({
                "lat": res.get("lat"),
                "lon": res.get("lon"),
                "formatted_address": res.get("formatted_address"),
                "provider": res.get("provider", "google_places"),
                "confidence": res.get("confidence"),
                "place_id": res.get("place_id"),
                "types": ";".join(types_out) if isinstance(types_out, list) else types_out,
                "partial_match": bool(res.get("partial_match", False)),
            })

        rows.append(out)
        print(f"[{idx}/{total}] {hotel} -> {out['status']}")

    out_df = pd.DataFrame(rows, columns=[
        "hotel_name","hotel_url","hotel_location","group_label","brand_label","group_type",
        "lat","lon","formatted_address","provider","confidence",
        "place_id","types","partial_match","status"
    ])

    if ONLY_MISSING and Path(output_path).exists():
        prev = pd.read_csv(output_path)
        keycols = ["hotel_name","group_label"]
        merged = pd.concat([prev, out_df]).drop_duplicates(subset=keycols, keep="last")
        merged.to_csv(output_path, index=False)
        print(f"Wrote {len(merged)} rows -> {output_path}")
    else:
        out_df.to_csv(output_path, index=False)
        print(f"Wrote {len(out_df)} rows -> {output_path}")


if __name__ == "__main__":
    main()

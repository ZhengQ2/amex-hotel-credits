import csv
import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

import cache_utils
import fhrthc


def _make_mini_pandas():
    class _MiniDF:
        def __init__(self, rows, columns):
            self._rows = [dict(r) for r in rows]
            self.columns = list(columns)

        def copy(self):
            return _MiniDF(self._rows, self.columns)

        def __len__(self):
            return len(self._rows)

        def __setitem__(self, key, val):
            if key not in self.columns:
                self.columns.append(key)
            for r in self._rows:
                r[key] = val

        def itertuples(self, index=False):
            for r in self._rows:
                yield types.SimpleNamespace(**{c: r.get(c) for c in self.columns})

        def to_csv(self, path, index=False):
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=self.columns)
                w.writeheader()
                for r in self._rows:
                    w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in self.columns})

    def _read_csv(path):
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return _MiniDF(list(reader), reader.fieldnames or [])

    def _df_ctor(rows, columns=None):
        cols = list(columns) if columns is not None else (list(rows[0].keys()) if rows else [])
        return _MiniDF(rows, cols)

    pd_mod = types.ModuleType("pandas")
    pd_mod.DataFrame = _df_ctor
    pd_mod.read_csv = _read_csv
    pd_mod.isna = lambda v: v is None or v == "" or (isinstance(v, float) and math.isnan(v))
    return pd_mod


def _load_google_convert():
    path = Path(__file__).resolve().parent.parent / "google-convert.py"
    stubs = {}
    if "requests" not in sys.modules:
        req = types.ModuleType("requests")
        req.RequestException = Exception
        stubs["requests"] = req
    if "pandas" not in sys.modules:
        try:
            import pandas  # noqa: F401
        except ImportError:
            stubs["pandas"] = _make_mini_pandas()
    if "tenacity" not in sys.modules:
        ten = types.ModuleType("tenacity")
        ten.retry = lambda *a, **kw: (lambda fn: fn)
        ten.stop_after_attempt = lambda *a, **kw: None
        ten.wait_exponential = lambda *a, **kw: None
        ten.retry_if_exception_type = lambda *a, **kw: None
        stubs["tenacity"] = ten
    if "dotenv" not in sys.modules:
        dot = types.ModuleType("dotenv")
        dot.load_dotenv = lambda *a, **kw: None
        stubs["dotenv"] = dot

    with mock.patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location("google_convert", path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        return module


class CacheUtilsTests(unittest.TestCase):
    def test_names_match_handles_diacritics_and_apostrophes(self):
        self.assertTrue(cache_utils._names_match("Nobu Hotel San Sebastián", "Nobu Hotel San Sebastian"))
        self.assertTrue(cache_utils._names_match("Awasi Iguazú", "Awasi Iguazu"))
        self.assertTrue(cache_utils._names_match("Hotel Schweizerhof Zürich", "Hotel Schweizerhof Zurich"))
        self.assertTrue(cache_utils._names_match("The Ritz-Carlton, Xi'an", "The Ritz-Carlton, Xian"))
        self.assertTrue(cache_utils._names_match("Ho‘olei Villas at Grand Wailea", "Hoolei Villas at Grand Wailea"))
        self.assertTrue(cache_utils._names_match("Hôtel Ermitage", "Hotel Ermitage"))

    def test_names_match_handles_multiword_affiliation_taglines(self):
        self.assertTrue(
            cache_utils._names_match(
                "Carmel Valley Ranch",
                "Carmel Valley Ranch, in The Unbound Collection by Hyatt",
            )
        )
        self.assertTrue(
            cache_utils._names_match(
                "Akelarre - Relais & Châteaux",
                "Akelarre Relais & Chateaux, San Sebastian",
                hotel_location="San Sebastián, Spain",
            )
        )
        self.assertTrue(
            cache_utils._names_match(
                "45 Park Lane",
                "45 Park Lane - Dorchester Collection",
            )
        )

    def test_names_match_rejects_sibling_discriminators(self):
        self.assertFalse(
            cache_utils._names_match(
                "Four Seasons Hotel New York",
                "Four Seasons Hotel New York Downtown",
            )
        )
        self.assertFalse(
            cache_utils._names_match(
                "Zadun Los Cabos, a Ritz-Carlton Reserve",
                "Zadun Los Cabos, a Ritz-Carlton Reserve Residence",
            )
        )
        self.assertFalse(
            cache_utils._names_match(
                "Hilton Dubai Jumeirah",
                "Hilton Dubai Palm Jumeirah",
            )
        )
        self.assertFalse(
            cache_utils._names_match(
                "Hyatt Zilara Cap Cana",
                "Hyatt Ziva Cap Cana",
            )
        )

    def test_find_fuzzy_cache_key_skips_placeholders_and_checks_state(self):
        placeholder = cache_utils.make_placeholder_key("Awasi Iguazu", "FHR", "fhrthc")
        resolved = json.dumps(
            {
                "brand": "FINE HOTELS + RESORTS",
                "hotel_name": "Awasi Iguazu",
                "input_format": "fhrthc",
                "provider": "google_places_first",
                "queries": ["Awasi Iguazu Puerto Iguazu, Argentina Hotel"],
                "v": 3,
            },
            sort_keys=True,
        )
        cache = {
            placeholder: None,
            resolved: {
                "lat": -25.609,
                "lon": -54.573,
                "formatted_address": "Puerto Iguazú, Misiones, Argentina",
            },
        }
        hit = cache_utils.find_fuzzy_cache_key(
            "Awasi Iguazú",
            cache,
            input_format="fhrthc",
            hotel_location="Puerto Iguazú, Argentina",
            scope_key="FINE HOTELS + RESORTS",
        )
        self.assertEqual(hit, resolved)

    def test_find_fuzzy_cache_key_blocks_cross_state_us_collision(self):
        texan_key = json.dumps(
            {
                "brand": "THE HOTEL COLLECTION",
                "hotel_name": "Gaylord Texan Resort & Convention Center",
                "input_format": "fhrthc",
                "provider": "google_places_first",
                "queries": ["Gaylord Texan Resort & Convention Center Grapevine, Texas US Hotel"],
                "v": 3,
            },
            sort_keys=True,
        )
        cache = {
            texan_key: {
                "lat": 32.9545,
                "lon": -97.0645,
                "formatted_address": "1501 Gaylord Trail, Grapevine, TX 76051, USA",
            }
        }
        hit = cache_utils.find_fuzzy_cache_key(
            "Gaylord Palms Resort & Convention Center",
            cache,
            input_format="fhrthc",
            hotel_location="Kissimmee, Florida US",
            scope_key="THE HOTEL COLLECTION",
        )
        self.assertIsNone(hit)

    def test_find_fuzzy_cache_key_preserves_conflicting_city_qualifiers(self):
        # Regression test: subtracting requested city ('austin') must not turn
        # 'Four Seasons Hotel Austin' into a subset match of 'Four Seasons Hotel Houston'.
        houston_key = json.dumps(
            {
                "brand": "FINE HOTELS + RESORTS",
                "hotel_name": "Four Seasons Hotel Houston",
                "input_format": "fhrthc",
                "provider": "google_places_first",
                "queries": ["Four Seasons Hotel Houston Houston, Texas US Hotel"],
                "v": 3,
            },
            sort_keys=True,
        )
        bare_houston_key = json.dumps(
            {
                "brand": "FINE HOTELS + RESORTS",
                "hotel_name": "Four Seasons Hotel",
                "input_format": "fhrthc",
                "provider": "google_places_first",
                "queries": ["Four Seasons Hotel Houston, Texas US Hotel"],
                "v": 3,
            },
            sort_keys=True,
        )
        melbourne_key = json.dumps(
            {
                "brand": "FINE HOTELS + RESORTS",
                "hotel_name": "The Ritz-Carlton, Melbourne",
                "input_format": "fhrthc",
                "provider": "google_places_first",
                "queries": ["The Ritz-Carlton, Melbourne Melbourne, Australia Hotel"],
                "v": 3,
            },
            sort_keys=True,
        )
        quebec_key = json.dumps(
            {
                "brand": "FINE HOTELS + RESORTS",
                "hotel_name": "Fairmont Le Chateau Frontenac",
                "input_format": "fhrthc",
                "provider": "google_places_first",
                "queries": ["Fairmont Le Chateau Frontenac Quebec City, Quebec CA Hotel"],
                "v": 3,
            },
            sort_keys=True,
        )
        cache = {
            houston_key: {
                "lat": 29.7547,
                "lon": -95.3627,
                "formatted_address": "1300 Lamar St, Houston, TX 77010, USA",
            },
            bare_houston_key: {
                "lat": 29.7547,
                "lon": -95.3627,
                "formatted_address": "1300 Lamar St, Houston, TX 77010, USA",
            },
            melbourne_key: {
                "lat": -37.8136,
                "lon": 144.9506,
                "formatted_address": "650 Lonsdale St, Melbourne VIC 3000, Australia",
            },
            quebec_key: {
                "lat": 46.8121,
                "lon": -71.2053,
                "formatted_address": "1 Rue des Carrières, Québec, QC G1R 5J5, Canada",
            },
        }
        self.assertIsNone(
            cache_utils.find_fuzzy_cache_key(
                "Four Seasons Hotel Austin",
                cache,
                input_format="fhrthc",
                hotel_location="Austin, Texas US",
                scope_key="FINE HOTELS + RESORTS",
            )
        )
        self.assertIsNone(
            cache_utils.find_fuzzy_cache_key(
                "The Ritz-Carlton, Perth",
                cache,
                input_format="fhrthc",
                hotel_location="Perth, Australia",
                scope_key="FINE HOTELS + RESORTS",
            )
        )
        self.assertIsNone(
            cache_utils.find_fuzzy_cache_key(
                "Fairmont Chateau Whistler",
                cache,
                input_format="fhrthc",
                hotel_location="Whistler, British Columbia CA",
                scope_key="FINE HOTELS + RESORTS",
            )
        )
        # Canadian postal codes with spaces ("QC G1R 5J5") must still trigger province checks.
        self.assertFalse(
            cache_utils.address_location_compatible(
                "1 Rue des Carrières, Québec, QC G1R 5J5, Canada",
                "Whistler, British Columbia CA",
            )
        )

    def test_find_fuzzy_cache_key_does_not_cross_match_same_city_siblings_or_brands(self):
        lutetia_key = json.dumps(
            {
                "brand": "FINE HOTELS + RESORTS",
                "hotel_name": "Mandarin Oriental Lutetia, Paris",
                "input_format": "fhrthc",
                "provider": "google_places_first",
                "queries": ["Mandarin Oriental Lutetia, Paris Paris, France Hotel"],
                "v": 3,
            },
            sort_keys=True,
        )
        marriott_pl_key = json.dumps(
            {
                "brand": "THE HOTEL COLLECTION",
                "hotel_name": "London Marriott Hotel Park Lane",
                "input_format": "fhrthc",
                "provider": "google_places_first",
                "queries": ["London Marriott Hotel Park Lane London, England Hotel"],
                "v": 3,
            },
            sort_keys=True,
        )
        bh_key = json.dumps(
            {
                "brand": "FINE HOTELS + RESORTS",
                "hotel_name": "The Beverly Hills Hotel",
                "input_format": "fhrthc",
                "provider": "google_places_first",
                "queries": ["The Beverly Hills Hotel Beverly Hills, California US Hotel"],
                "v": 3,
            },
            sort_keys=True,
        )
        cache = {
            lutetia_key: {
                "lat": 48.8511,
                "lon": 2.3275,
                "formatted_address": "45 Bd Raspail, 75006 Paris, France",
            },
            marriott_pl_key: {
                "lat": 51.5129,
                "lon": -0.1586,
                "formatted_address": "140 Park Ln, London W1K 7AA, UK",
            },
            bh_key: {
                "lat": 34.0815,
                "lon": -118.4136,
                "formatted_address": "9641 Sunset Blvd, Beverly Hills, CA 90210, USA",
            },
        }
        self.assertIsNone(
            cache_utils.find_fuzzy_cache_key(
                "Mandarin Oriental, Paris",
                cache,
                input_format="fhrthc",
                hotel_location="Paris, France",
                scope_key="FINE HOTELS + RESORTS",
            )
        )
        self.assertIsNone(
            cache_utils.find_fuzzy_cache_key(
                "London Hilton on Park Lane",
                cache,
                input_format="fhrthc",
                hotel_location="London, England",
                scope_key="THE HOTEL COLLECTION",
            )
        )
        self.assertIsNone(
            cache_utils.find_fuzzy_cache_key(
                "Waldorf Astoria Beverly Hills",
                cache,
                input_format="fhrthc",
                hotel_location="Beverly Hills, California US",
                scope_key="FINE HOTELS + RESORTS",
            )
        )

    def test_is_cached_hotel_in_scraped_preserves_listed_single_core_hotels(self):
        scraped = {
            "fhr": {
                "hotel de rome berlin",
                "loden hotel",
                "baccarat hotel and residences new york",
                "universal's loews portofino bay hotel",
            },
            "thc": {
                "w hong kong",
                "omni austin hotel",
            },
        }
        self.assertTrue(cache_utils.is_cached_hotel_in_scraped("hotel de rome", "FHR", scraped))
        self.assertTrue(cache_utils.is_cached_hotel_in_scraped("loden boutique hotel vancouver", "FHR", scraped))
        self.assertTrue(cache_utils.is_cached_hotel_in_scraped("baccarat hotel new york", "FHR", scraped))
        self.assertTrue(cache_utils.is_cached_hotel_in_scraped("loews portofino bay hotel universal orlando", "FHR", scraped))
        self.assertTrue(cache_utils.is_cached_hotel_in_scraped("omni austin hotel downtown", "THC", scraped))
        # Single-char brand 'W Hong Kong' must NOT keep unrelated Hong Kong FHR hotels in THC.
        self.assertFalse(cache_utils.is_cached_hotel_in_scraped("rosewood hong kong", "THC", scraped))

    def test_find_fuzzy_cache_key_reuses_older_brand_scoped_and_hilton_rebrand_entries(self):
        old_brand_key = json.dumps(
            {
                "brand": "Grand Hyatt",
                "hotel_name": "Grand Hyatt Deer Valley",
                "input_format": "fhrthc",
                "provider": "google_places_first",
                "queries": ["Grand Hyatt Deer Valley Park City, Utah US Hotel"],
                "v": 3,
            },
            sort_keys=True,
        )
        thc_only_key = json.dumps(
            {
                "brand": "THE HOTEL COLLECTION",
                "hotel_name": "Grand Hyatt Deer Valley",
                "input_format": "fhrthc",
                "provider": "google_places_first",
                "queries": ["Grand Hyatt Deer Valley Park City, Utah US Hotel"],
                "v": 3,
            },
            sort_keys=True,
        )
        fhr_cache = {
            old_brand_key: {
                "lat": 40.6,
                "lon": -111.5,
                "formatted_address": "123 St, Park City, UT 84060, USA",
                "types": ["lodging", "establishment"],
            },
            thc_only_key: {
                "lat": 40.6,
                "lon": -111.5,
                "formatted_address": "123 St, Park City, UT 84060, USA",
                "types": ["lodging", "establishment"],
            },
        }
        # Older brand-scoped entry is reused for FHR, while conflicting THC bucket is ignored.
        hit = cache_utils.find_fuzzy_cache_key(
            "Grand Hyatt Deer Valley",
            fhr_cache,
            input_format="fhrthc",
            hotel_location="Park City, Utah US",
            scope_key="FINE HOTELS + RESORTS",
        )
        self.assertEqual(hit, old_brand_key)

        hilton_key = json.dumps(
            {
                "brand": "Curio Collection by Hilton",
                "hotel_name": "Casa Marina Key West",
                "input_format": "hilton",
                "provider": "google_places_first",
                "queries": ["Casa Marina Key West Curio Collection by Hilton Hotel"],
                "v": 3,
            },
            sort_keys=True,
        )
        hilton_cache = {
            hilton_key: {
                "lat": 24.5477,
                "lon": -81.7901,
                "formatted_address": "1500 Reynolds St, Key West, FL 33040, USA",
                "types": ["lodging", "establishment"],
            }
        }
        self.assertEqual(
            cache_utils.find_fuzzy_cache_key(
                "Casa Marina Key West",
                hilton_cache,
                input_format="hilton",
                scope_key="LXR Hotels & Resorts",
            ),
            hilton_key,
        )

    def test_find_fuzzy_cache_key_requires_city_agreement_for_exact_names(self):
        cache = {
            json.dumps(
                {
                    "brand": "FINE HOTELS + RESORTS",
                    "hotel_name": "The Grand Hotel",
                    "input_format": "fhrthc",
                    "provider": "google_places_first",
                    "queries": ["The Grand Hotel Houston, Texas US Hotel"],
                    "v": 3,
                },
                sort_keys=True,
            ): {
                "lat": 29.75,
                "lon": -95.36,
                "formatted_address": "1 Main St, Houston, TX 77002, USA",
                "types": ["lodging", "establishment"],
            },
            json.dumps(
                {
                    "brand": "FINE HOTELS + RESORTS",
                    "hotel_name": "The Grand Hotel",
                    "input_format": "fhrthc",
                    "provider": "google_places_first",
                    "queries": ["The Grand Hotel Nice, France Hotel"],
                    "v": 3,
                },
                sort_keys=True,
            ): {
                "lat": 43.70,
                "lon": 7.26,
                "formatted_address": "1 Promenade, 06000 Nice, France",
                "types": ["lodging", "establishment"],
            },
        }
        self.assertIsNone(
            cache_utils.find_fuzzy_cache_key(
                "The Grand Hotel",
                cache,
                input_format="fhrthc",
                hotel_location="Austin, Texas US",
                scope_key="FINE HOTELS + RESORTS",
            )
        )
        self.assertIsNone(
            cache_utils.find_fuzzy_cache_key(
                "Le Grand Hotel",
                cache,
                input_format="fhrthc",
                hotel_location="Paris, France",
                scope_key="FINE HOTELS + RESORTS",
            )
        )

    def test_mid_name_by_hilton_retains_brand_word(self):
        key = json.dumps(
            {
                "brand": "Signia by Hilton",
                "hotel_name": "Signia by Hilton Orlando Bonnet Creek",
                "input_format": "hilton",
                "provider": "google_places_first",
                "queries": ["Signia by Hilton Orlando Bonnet Creek Hotel"],
                "v": 3,
            },
            sort_keys=True,
        )
        cache = {
            key: {
                "lat": 28.35,
                "lon": -81.53,
                "formatted_address": "14100 Bonnet Creek Resort Ln, Orlando, FL 32821, USA",
                "types": ["lodging", "establishment"],
            }
        }
        self.assertEqual(
            cache_utils.find_fuzzy_cache_key(
                "Signia Hilton Orlando Bonnet Creek",
                cache,
                input_format="hilton",
                scope_key="Signia by Hilton",
            ),
            key,
        )

    def test_us_virgin_islands_location_compatibility(self):
        self.assertTrue(
            cache_utils.address_location_compatible(
                "6900 Great Bay, Nazareth, St Thomas 00802, USVI",
                "St. Thomas, U.S. Virgin Islands",
            )
        )
        self.assertTrue(
            cache_utils.address_location_compatible(
                "1, Lovango Cay, St. John, St John 00830, USVI",
                "St. John, U.S. Virgin Islands",
            )
        )

    def test_no_result_does_not_block_resolved_or_other_cities(self):
        oslo_no_res = json.dumps(
            {
                "brand": "FINE HOTELS + RESORTS",
                "hotel_name": "The Grand Hotel",
                "input_format": "fhrthc",
                "provider": "google_places_first",
                "queries": ["The Grand Hotel Oslo, Norway Hotel"],
                "v": 3,
            },
            sort_keys=True,
        )
        austin_no_res = json.dumps(
            {
                "brand": "FINE HOTELS + RESORTS",
                "hotel_name": "The Grand Hotel",
                "input_format": "fhrthc",
                "provider": "google_places_first",
                "queries": ["The Grand Hotel Austin, Texas US Hotel"],
                "v": 3,
            },
            sort_keys=True,
        )
        austin_resolved = json.dumps(
            {
                "brand": "FINE HOTELS + RESORTS",
                "hotel_name": "Grand Hotel, Autograph Collection",
                "input_format": "fhrthc",
                "provider": "google_places_first",
                "queries": ["Grand Hotel, Autograph Collection Austin, Texas US Hotel"],
                "v": 3,
            },
            sort_keys=True,
        )
        # Oslo NO_RESULT must not match Austin.
        self.assertIsNone(
            cache_utils.find_fuzzy_cache_key(
                "The Grand Hotel",
                {oslo_no_res: {"status": "NO_RESULT"}},
                input_format="fhrthc",
                hotel_location="Austin, Texas US",
                scope_key="FINE HOTELS + RESORTS",
            )
        )
        # Resolved Austin entry must beat an exact-name Austin NO_RESULT entry.
        hit = cache_utils.find_fuzzy_cache_key(
            "The Grand Hotel",
            {
                austin_no_res: {"status": "NO_RESULT"},
                austin_resolved: {
                    "lat": 30.26,
                    "lon": -97.74,
                    "formatted_address": "100 Main St, Austin, TX 78701, USA",
                    "types": ["lodging", "establishment"],
                },
            },
            input_format="fhrthc",
            hotel_location="Austin, Texas US",
            scope_key="FINE HOTELS + RESORTS",
        )
        self.assertEqual(hit, austin_resolved)

        # NO_RESULT entries are never reused fuzzily (Portland ME vs OR, and renamed hotels).
        portland_me_no_res = json.dumps(
            {
                "brand": "FINE HOTELS + RESORTS",
                "hotel_name": "The Grand Hotel",
                "input_format": "fhrthc",
                "provider": "google_places_first",
                "queries": ["The Grand Hotel Portland, Maine US Hotel"],
                "v": 3,
            },
            sort_keys=True,
        )
        eagles_no_res = json.dumps(
            {
                "brand": "THE HOTEL COLLECTION",
                "hotel_name": "Eagles Villas",
                "input_format": "fhrthc",
                "provider": "google_places_first",
                "queries": ["Eagles Villas Ouranoupolis, Greece Hotel"],
                "v": 3,
            },
            sort_keys=True,
        )
        self.assertIsNone(
            cache_utils.find_fuzzy_cache_key(
                "The Grand Hotel",
                {portland_me_no_res: {"status": "NO_RESULT"}},
                input_format="fhrthc",
                hotel_location="Portland, Oregon US",
                scope_key="FINE HOTELS + RESORTS",
            )
        )
        self.assertIsNone(
            cache_utils.find_fuzzy_cache_key(
                "Eagles Villas - Small Luxury Hotels of The World",
                {eagles_no_res: {"status": "NO_RESULT"}},
                input_format="fhrthc",
                hotel_location="Ouranoupolis, Greece",
                scope_key="THE HOTEL COLLECTION",
            )
        )

    def test_address_locality_checked_before_queries_and_excludes_streets(self):
        # Conflicting returned address (Los Angeles) vetoes queries mentioning San Francisco.
        fs_la_key = json.dumps(
            {
                "brand": "FINE HOTELS + RESORTS",
                "hotel_name": "Four Seasons Hotel San Francisco",
                "input_format": "fhrthc",
                "provider": "google_places_first",
                "queries": ["Four Seasons Hotel San Francisco San Francisco, California US"],
                "v": 3,
            },
            sort_keys=True,
        )
        buffalo_key = json.dumps(
            {
                "brand": "FINE HOTELS + RESORTS",
                "hotel_name": "The Grand Hotel",
                "input_format": "fhrthc",
                "provider": "google_places_first",
                "queries": ["The Grand Hotel Buffalo, New York US"],
                "v": 3,
            },
            sort_keys=True,
        )
        milano_key = json.dumps(
            {
                "brand": "FINE HOTELS + RESORTS",
                "hotel_name": "The Grand Hotel",
                "input_format": "fhrthc",
                "provider": "google_places_first",
                "queries": ["The Grand Hotel"],
                "v": 3,
            },
            sort_keys=True,
        )
        sorrento_key = json.dumps(
            {
                "brand": "FINE HOTELS + RESORTS",
                "hotel_name": "The Grand Hotel",
                "input_format": "fhrthc",
                "provider": "google_places_first",
                "queries": ["The Grand Hotel Sorrento"],
                "v": 3,
            },
            sort_keys=True,
        )
        cache = {
            fs_la_key: {
                "lat": 34.073,
                "lon": -118.389,
                "formatted_address": "300 S Doheny Dr, Los Angeles, CA 90048, USA",
                "types": ["lodging", "establishment"],
            },
            buffalo_key: {
                "lat": 42.886,
                "lon": -78.878,
                "formatted_address": "100 Main St, Buffalo, NY 14202, USA",
                "types": ["lodging", "establishment"],
            },
            milano_key: {
                "lat": 45.47,
                "lon": 9.20,
                "formatted_address": "Corso Venezia, 1, 20121 Milano MI, Italy",
                "types": ["lodging", "establishment"],
            },
            sorrento_key: {
                "lat": 40.62,
                "lon": 14.37,
                "formatted_address": "Via Roma, 1, 80067 Sorrento NA, Italy",
                "types": ["lodging", "establishment"],
            },
        }
        self.assertIsNone(
            cache_utils.find_fuzzy_cache_key(
                "Four Seasons Hotel San Francisco",
                cache,
                input_format="fhrthc",
                hotel_location="San Francisco, CA US",
                scope_key="FINE HOTELS + RESORTS",
            )
        )
        self.assertIsNone(
            cache_utils.find_fuzzy_cache_key(
                "The Grand Hotel",
                cache,
                input_format="fhrthc",
                hotel_location="New York, New York US",
                scope_key="FINE HOTELS + RESORTS",
            )
        )
        self.assertIsNone(
            cache_utils.find_fuzzy_cache_key(
                "The Grand Hotel",
                cache,
                input_format="fhrthc",
                hotel_location="Venice, Italy",
                scope_key="FINE HOTELS + RESORTS",
            )
        )
        self.assertIsNone(
            cache_utils.find_fuzzy_cache_key(
                "The Grand Hotel",
                cache,
                input_format="fhrthc",
                hotel_location="Rome, Italy",
                scope_key="FINE HOTELS + RESORTS",
            )
        )
        # Valid endonyms and city-states match on formatted_address alone (no queries).
        self.assertTrue(
            cache_utils._candidate_city_compatible(
                "St. Julian's, Malta", "Triq Dragunara, San Ġiljan, Malta", []
            )
        )
        self.assertTrue(
            cache_utils._candidate_city_compatible(
                "Mexico City, Mexico City MX",
                "Durango 280, Roma Nte., Cuauhtémoc, 06700 Ciudad de México, CDMX, Mexico",
                [],
            )
        )
        self.assertTrue(
            cache_utils._candidate_city_compatible(
                "Hong Kong, China", "555 Shanghai St, Mong Kok, Hong Kong", []
            )
        )
        self.assertTrue(
            cache_utils._candidate_city_compatible(
                "Xi'an, China",
                "50 Ke Ji Er Lu, Yan Ta Qu, Xi An Shi, Shan Xi Sheng, China, 710065",
                [],
            )
        )

    def test_city_suffix_not_stripped_to_state_country_or_neighbor(self):
        self.assertFalse(
            cache_utils._candidate_city_compatible(
                "Mexico City, Mexico", "Blvd. Kukulcan Km 10, Cancún, Q.R., Mexico", []
            )
        )
        self.assertFalse(
            cache_utils._candidate_city_compatible(
                "Virginia Beach, Virginia US", "1200 N Courthouse Rd, Arlington, VA 22201, USA", []
            )
        )
        self.assertFalse(
            cache_utils._candidate_city_compatible(
                "Miami Beach, Florida US", "1109 Brickell Ave, Miami, FL 33131, USA", []
            )
        )
        for loc1, loc2 in (
            ("San Diego, California US", "San Francisco, California US"),
            ("Santa Monica, California US", "Santa Barbara, California US"),
            ("Palm Beach, Florida US", "Miami Beach, Florida US"),
            ("Fort Lauderdale, Florida US", "Fort Myers, Florida US"),
            ("Manchester, England", "Winchester, England"),
        ):
            self.assertFalse(cache_utils.locations_compatible(loc1, loc2), f"{loc1} vs {loc2}")

    def test_exact_name_establishment_outranks_core_only_lodging(self):
        exact_est_key = json.dumps(
            {
                "brand": "FINE HOTELS + RESORTS",
                "hotel_name": "The Grand Hotel",
                "input_format": "fhrthc",
                "provider": "google_places_first",
                "queries": ["The Grand Hotel Austin, Texas US"],
                "v": 3,
            },
            sort_keys=True,
        )
        loose_lodging_key = json.dumps(
            {
                "brand": "FINE HOTELS + RESORTS",
                "hotel_name": "Grand Resort & Spa, Autograph Collection",
                "input_format": "fhrthc",
                "provider": "google_places_first",
                "queries": ["Grand Resort & Spa Austin, Texas US"],
                "v": 3,
            },
            sort_keys=True,
        )
        cache = {
            exact_est_key: {
                "lat": 30.27,
                "lon": -97.74,
                "formatted_address": "100 Congress Ave, Austin, TX 78701, USA",
                "types": ["establishment"],
            },
            loose_lodging_key: {
                "lat": 30.28,
                "lon": -97.75,
                "formatted_address": "200 Congress Ave, Austin, TX 78701, USA",
                "types": ["lodging", "establishment"],
            },
        }
        hit = cache_utils.find_fuzzy_cache_key(
            "The Grand Hotel",
            cache,
            input_format="fhrthc",
            hotel_location="Austin, Texas US",
            scope_key="FINE HOTELS + RESORTS",
        )
        self.assertEqual(hit, exact_est_key)

    def test_pruning_preserves_hilton_rebrands_and_edge_cases_without_retaining_removed_chains(self):
        # Hilton rebrand: Curio entry preserved while property is listed under LXR.
        hilton_scraped = {
            "lxr hotels & resorts": {"casa marina key west, curio collection by hilton"},
            "curio collection by hilton": {"some other curio resort"},
        }
        self.assertTrue(
            cache_utils.is_cached_hotel_in_scraped(
                "casa marina key west, curio collection by hilton",
                "curio collection by hilton",
                hilton_scraped,
            )
        )

        # Edge-case listed hotels remain preserved across pruning.
        fhr_scraped = {
            "fhr": {
                "hotel d'angleterre geneva",
                "ka la'i waikiki beach, lxr hotels & resorts",
                "the hay-adams",
                "inn by the sea",
                "four seasons hotel houston",
                "the carlton",
                "nobis hotel stockholm, a member of design hotels",
                "the marquette hotel, curio collection by hilton",
                "the dixon, autograph collection",
                "the alida, savannah, a tribute portfolio hotel",
                "the randolph hotel oxford, a graduate by hilton",
                "magma resort santorini, part of the unbound collection by hyatt",
            }
        }
        self.assertTrue(cache_utils.is_cached_hotel_in_scraped("hotel angleterre geneva", "FHR", fhr_scraped))
        self.assertTrue(cache_utils.is_cached_hotel_in_scraped("ka la i waikiki beach, lxr resorts", "FHR", fhr_scraped))
        self.assertTrue(cache_utils.is_cached_hotel_in_scraped("hay adams washington dc", "FHR", fhr_scraped))
        self.assertTrue(cache_utils.is_cached_hotel_in_scraped("inn sea hilton", "FHR", fhr_scraped))
        self.assertTrue(cache_utils.is_cached_hotel_in_scraped("marquette curio collection hilton hilton", "FHR", fhr_scraped))
        self.assertTrue(cache_utils.is_cached_hotel_in_scraped("the dixon, tower bridge, autograph collection", "FHR", fhr_scraped))
        self.assertTrue(cache_utils.is_cached_hotel_in_scraped("the alida hotel, a tribute portfolio", "FHR", fhr_scraped))
        self.assertTrue(cache_utils.is_cached_hotel_in_scraped("randolph hotel graduate hotels hilton", "FHR", fhr_scraped))
        self.assertTrue(
            cache_utils.is_cached_hotel_in_scraped(
                "magma resort santorini part of unbound collection hyatt hilton", "FHR", fhr_scraped
            )
        )

        # Removed chain hotels in other cities are NOT falsely retained.
        self.assertFalse(cache_utils.is_cached_hotel_in_scraped("four seasons hotel austin", "FHR", fhr_scraped))
        self.assertFalse(cache_utils.is_cached_hotel_in_scraped("the ritz-carlton, tokyo", "FHR", fhr_scraped))
        self.assertFalse(
            cache_utils.is_cached_hotel_in_scraped(
                "nobis hotel copenhagen, a member of design hotels", "FHR", fhr_scraped
            )
        )

    def test_same_normalized_name_and_location_preserves_neighboring_town_address(self):
        cabo_key = json.dumps(
            {
                "brand": "THE HOTEL COLLECTION",
                "hotel_name": "Hilton Los Cabos",
                "input_format": "fhrthc",
                "provider": "google_places_first",
                "queries": [
                    "Hilton Los Cabos San Jose del Cabo, Baja California Sur MX Hotel",
                    "Hilton Los Cabos San Jose del Cabo, Baja California Sur MX",
                ],
                "v": 3,
            },
            sort_keys=True,
        )
        buffalo_key = json.dumps(
            {
                "brand": "FINE HOTELS + RESORTS",
                "hotel_name": "The Grand Hotel",
                "input_format": "fhrthc",
                "provider": "google_places_first",
                "queries": ["The Grand Hotel Buffalo, New York US"],
                "v": 3,
            },
            sort_keys=True,
        )
        cache = {
            cabo_key: {
                "lat": 22.97,
                "lon": -109.79,
                "formatted_address": "Carr. Transpeninsular, Tourist Corridor, 23400 Cabo San Lucas, B.C.S., Mexico",
                "types": ["lodging", "establishment"],
            },
            buffalo_key: {
                "lat": 42.886,
                "lon": -78.878,
                "formatted_address": "100 Main St, Buffalo, NY 14202, USA",
                "types": ["lodging", "establishment"],
            },
        }
        # Accent-only location change on the same hotel reuses its Cabo San Lucas entry.
        self.assertEqual(
            cache_utils.find_fuzzy_cache_key(
                "Hilton Los Cabos",
                cache,
                input_format="fhrthc",
                hotel_location="San José del Cabo, Baja California Sur MX",
                scope_key="THE HOTEL COLLECTION",
            ),
            cabo_key,
        )
        # Substring location ("New York, US") must NOT bypass via "Buffalo, New York US".
        self.assertIsNone(
            cache_utils.find_fuzzy_cache_key(
                "The Grand Hotel",
                cache,
                input_format="fhrthc",
                hotel_location="New York, US",
                scope_key="FINE HOTELS + RESORTS",
            )
        )

    def test_possessive_collapse_preserves_parkers_and_retains_universals_fallback(self):
        self.assertEqual(
            cache_utils.normalize_name("Grand Hotel Parker's"),
            cache_utils.normalize_name("Grand Hotel Parkers"),
        )
        self.assertTrue(
            cache_utils._names_match(
                "Universal's Loews Portofino Bay Hotel",
                "Loews Portofino Bay Hotel Universal",
            )
        )

    def test_address_locality_preserves_first_segment_cities_and_filters_streets_and_xian_county(self):
        # First-segment cities, mixed separators, Italian LU province, Polish/Icelandic postals, Arabic comma.
        for loc, addr in (
            ("Adare, Ireland", "Adare, Co. Limerick, V94 W8WR, Ireland"),
            ("St. Helena, California US", "St. Helena, CA 94574, USA"),
            (
                "Dubai, United Arab Emirates",
                "GV09 Level GF, Gate Village - Building 9, Unit - Sheikh Zayed Rd - Zaa'beel Second - DIFC - Dubai - United Arab Emirates",
            ),
            ("Forte dei Marmi, Italy", "Viale Ammiraglio Morin, 67, 55042 Forte dei Marmi LU, Italy"),
            ("Warsaw, Poland", "Krakowskie Przedmieście 13, 00-071 Warszawa, Poland"),
            ("Reykjavik, Iceland", "Austurbakki 2, 101 Reykjavík, Iceland"),
            ("Marrakech, Morocco", "Rue Abou Abbas El Sebti، Marrakech 40000, Morocco"),
            ("St. James, Barbados", "Sandy Lane Hotel, 30 South Rd, Holetown, Saint James BB24024Barbados"),
        ):
            self.assertTrue(
                cache_utils._candidate_city_compatible(loc, addr, []),
                f"Expected locality match for {loc!r} in {addr!r}",
            )

        # Mid-address street names, Spanish "C." streets, and Chinese county "Xian" must NOT match.
        for loc, addr in (
            ("Oxford, England", "Kimpton Clocktower Hotel, Oxford St, Manchester M60 7HA, UK"),
            ("Seville, Spain", "C. de Sevilla, 3, Centro, 28014 Madrid, Spain"),
            ("Xi'an, China", "Jiu Zhai Gou Xian, A Ba Zang Zu Qiang Zu Zi Zhi Zhou, Si Chuan Sheng, China"),
        ):
            self.assertFalse(
                cache_utils._candidate_city_compatible(loc, addr, []),
                f"Expected locality mismatch for {loc!r} in {addr!r}",
            )


class UpstreamFixTests(unittest.TestCase):
    def test_canonicalize_amex_url_normalizes_property_prefix(self):
        short = "https://www.americanexpress.com/property/Florida-US/Boca-Raton/Tower-At-The-Boca-Raton"
        canonical = "https://www.americanexpress.com/en-us/travel/discover/property/Florida-US/Boca-Raton/Tower-At-The-Boca-Raton"
        self.assertEqual(fhrthc._canonicalize_amex_url(short), canonical)
        self.assertEqual(fhrthc._canonicalize_amex_url(canonical), canonical)

    def test_google_convert_rejects_parking_and_prefers_lodging(self):
        gc = _load_google_convert()
        self.assertGreaterEqual(gc.SELECTOR_VERSION, 3)
        self.assertFalse(gc._is_lodging_type(["establishment", "parking", "point_of_interest"]))
        self.assertFalse(gc._is_lodging_type(["establishment", "restaurant", "food"]))
        self.assertFalse(gc._is_lodging_type(["establishment", "store"]))
        self.assertFalse(gc._is_lodging_type(["natural_feature"]))
        # A real hotel that also lists parking/restaurant still carries "lodging".
        self.assertTrue(gc._is_lodging_type(["establishment", "lodging", "parking", "restaurant"]))
        # Fallback hotel without "lodging" tag (e.g. spa resort) remains accepted.
        self.assertTrue(gc._is_lodging_type(["establishment", "health", "point_of_interest", "spa"]))

        # Accent-folded name matching and restaurant rejection.
        self.assertTrue(gc._is_name_compatible("Hôtel Métropole", "Hotel Metropole"))
        self.assertTrue(gc._is_name_compatible("Hotel Zürich", "Hotel Zurich"))
        self.assertTrue(gc._is_name_compatible("Hotel Xi'an", "Hotel Xian"))
        metropole_choice = gc.choose_best_place(
            [
                {
                    "name": "Metropole Restaurant",
                    "place_id": "pid_rest",
                    "types": ["restaurant", "food", "establishment"],
                    "formatted_address": "Brussels, Belgium",
                    "rating": 4.9,
                    "user_ratings_total": 999,
                },
                {
                    "name": "Hôtel Métropole",
                    "place_id": "pid_hotel",
                    "types": ["lodging", "establishment"],
                    "formatted_address": "Brussels, Belgium",
                    "rating": 4.5,
                    "user_ratings_total": 100,
                },
            ],
            "",
            "Hotel Metropole",
            "Brussels, Belgium",
        )
        self.assertIsNotNone(metropole_choice)
        self.assertEqual(metropole_choice["place_id"], "pid_hotel")

        # Shared location helper enforces state guard and handles USVI identically.
        self.assertIs(gc._location_compatible, cache_utils.address_location_compatible)
        self.assertFalse(
            gc._location_compatible(
                "926 J St, Sacramento, CA 95814, USA",
                "Alexandria, Virginia US",
            )
        )
        self.assertTrue(
            gc._location_compatible(
                "6900 Great Bay, Nazareth, St Thomas 00802, USVI",
                "St. Thomas, U.S. Virgin Islands",
            )
        )

        results = [
            {
                "name": "The Ritz-Carlton, Xi'an Parking",
                "place_id": "pid_parking",
                "types": ["establishment", "parking", "point_of_interest"],
                "formatted_address": "50 Ke Ji Er Lu, Xi'an, China",
                "rating": 4.9,
                "user_ratings_total": 500,
            },
            {
                "name": "The Ritz-Carlton, Xi'an",
                "place_id": "pid_hotel",
                "types": ["establishment", "lodging", "point_of_interest"],
                "formatted_address": "50 Ke Ji Er Lu, Xi'an, China",
                "rating": 4.7,
                "user_ratings_total": 300,
            },
        ]
        chosen = gc.choose_best_place(results, "The Ritz-Carlton", "The Ritz-Carlton, Xi'an", "Xi'an, China")
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["place_id"], "pid_hotel")

        # Parking-only result set must return None rather than accept a parking lot.
        only_parking = [results[0]]
        self.assertIsNone(
            gc.choose_best_place(only_parking, "The Ritz-Carlton", "The Ritz-Carlton, Xi'an", "Xi'an, China")
        )

    def test_fuzzy_cache_key_skips_parking_entry_and_reuses_valid_lodging(self):
        parking_key = json.dumps(
            {
                "brand": "FINE HOTELS + RESORTS",
                "hotel_name": "The Ritz-Carlton, Xi'an",
                "input_format": "fhrthc",
                "provider": "google_places_first",
                "queries": ["The Ritz-Carlton, Xi'an Xi'an, China Hotel"],
                "v": 3,
            },
            sort_keys=True,
        )
        lodging_key = json.dumps(
            {
                "brand": "FINE HOTELS + RESORTS",
                "hotel_name": "The Ritz-Carlton, Xi'an",
                "input_format": "fhrthc",
                "provider": "google_places_first",
                "queries": ["The Ritz-Carlton, Xi'an Xian, China Hotel"],
                "v": 3,
            },
            sort_keys=True,
        )
        cache = {
            parking_key: {
                "lat": 34.2237654,
                "lon": 108.8980399,
                "formatted_address": "50 Ke Ji Er Lu, 50, Yan Ta Qu, Xi An Shi, Shan Xi Sheng, China, 710065",
                "place_id": "pid_parking",
                "types": ["establishment", "parking", "point_of_interest"],
            },
            lodging_key: {
                "lat": 34.2232364,
                "lon": 108.8985118,
                "formatted_address": "50 Ke Ji Er Lu, Yan Ta Qu, Xi An Shi, Shan Xi Sheng, China, 710065",
                "place_id": "pid_lodging",
                "types": ["establishment", "lodging", "point_of_interest"],
            },
        }
        hit = cache_utils.find_fuzzy_cache_key(
            "The Ritz-Carlton, Xi'an",
            cache,
            input_format="fhrthc",
            hotel_location="Xi'an, China",
            scope_key="FINE HOTELS + RESORTS",
        )
        self.assertEqual(hit, lodging_key)

    def test_google_convert_retry_preserves_pins_on_error_and_type_mismatch_and_retries_renamed_no_result(self):
        gc = _load_google_convert()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            inp = tmp_dir / "in.csv"
            out1 = tmp_dir / "out1.csv"
            out2 = tmp_dir / "out2.csv"
            out3 = tmp_dir / "out3.csv"
            cache_p = tmp_dir / "cache.json"

            with open(inp, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(
                    f,
                    fieldnames=["hotel_name", "hotel_url", "hotel_location", "group_label", "brand_label", "group_type"],
                )
                w.writeheader()
                w.writerow({
                    "hotel_name": "The Satire",
                    "hotel_url": "",
                    "hotel_location": "Alexandria, Virginia US",
                    "group_label": "THE HOTEL COLLECTION",
                    "brand_label": "",
                    "group_type": "Program",
                })
                w.writerow({
                    "hotel_name": "The St. Regis Chengdu",
                    "hotel_url": "",
                    "hotel_location": "Chengdu, China",
                    "group_label": "FINE HOTELS + RESORTS",
                    "brand_label": "",
                    "group_type": "Program",
                })
                w.writerow({
                    "hotel_name": "Eagles Villas - Small Luxury Hotels of The World",
                    "hotel_url": "",
                    "hotel_location": "Ouranoupolis, Greece",
                    "group_label": "THE HOTEL COLLECTION",
                    "brand_label": "",
                    "group_type": "Program",
                })

            k_satire = json.dumps(
                {
                    "v": gc.CACHE_VERSION,
                    "provider": "google_places_first",
                    "input_format": "fhrthc",
                    "hotel_name": "The Satire",
                    "queries": gc.build_queries("The Satire", "", "", "Alexandria, Virginia US"),
                    "brand": "THE HOTEL COLLECTION",
                },
                sort_keys=True,
            )
            k_chengdu = json.dumps(
                {
                    "v": gc.CACHE_VERSION,
                    "provider": "google_places_first",
                    "input_format": "fhrthc",
                    "hotel_name": "The St. Regis Chengdu",
                    "queries": gc.build_queries("The St. Regis Chengdu", "", "", "Chengdu, China"),
                    "brand": "FINE HOTELS + RESORTS",
                },
                sort_keys=True,
            )
            k_eagles_old = json.dumps(
                {
                    "v": gc.CACHE_VERSION,
                    "provider": "google_places_first",
                    "input_format": "fhrthc",
                    "hotel_name": "Eagles Villas",
                    "queries": ["Eagles Villas Ouranoupolis, Greece"],
                    "brand": "THE HOTEL COLLECTION",
                },
                sort_keys=True,
            )
            cache_p.write_text(
                json.dumps({
                    k_satire: {
                        "lat": 38.58,
                        "lon": -121.49,
                        "formatted_address": "100 Capitol Mall, Sacramento, CA 95814, USA",
                        "types": ["lodging", "establishment"],
                        "place_id": "pid_sac",
                    },
                    k_chengdu: {
                        "lat": 30.66,
                        "lon": 104.07,
                        "formatted_address": "88 Taisheng S Rd, Chengdu, Sichuan, China",
                        "types": ["restaurant", "food", "establishment"],
                        "place_id": "pid_cd",
                    },
                    k_eagles_old: {"status": "NO_RESULT"},
                }),
                encoding="utf-8",
            )

            # Night 1: Google API errors on all retries -> old pins stay intact and unstamped, no NO_RESULT written.
            n1_calls = []

            def fake_err(queries, brand, hotel, delay, loc=""):
                n1_calls.append(hotel)
                return None, "ERR:OVER_QUERY_LIMIT"

            with mock.patch.object(gc, "API_KEY", "test-key"), mock.patch.object(
                gc, "BASE_DELAY", 0
            ), mock.patch.object(gc, "geocode_places_first", side_effect=fake_err), mock.patch(
                "sys.argv",
                ["google-convert.py", "--input", str(inp), "--output", str(out1), "--cache", str(cache_p), "--input-format", "fhrthc"],
            ):
                gc.main()

            with open(out1, encoding="utf-8") as f:
                rows1 = {r["hotel_name"]: r for r in csv.DictReader(f)}
            self.assertEqual(len(n1_calls), 3)
            self.assertEqual(rows1["The Satire"]["lat"], "38.58")
            self.assertEqual(rows1["The St. Regis Chengdu"]["lat"], "30.66")
            c1 = json.loads(cache_p.read_text(encoding="utf-8"))
            self.assertNotIn(gc._MISMATCH_STAMP, c1[k_satire])
            self.assertNotIn(gc._MISMATCH_STAMP, c1[k_chengdu])
            self.assertEqual(len(c1), 3)

            # Night 2: Satire and renamed Eagles Villas resolve; Chengdu empty retry keeps & stamps old pin.
            n2_calls = []

            def fake_recover(queries, brand, hotel, delay, loc=""):
                n2_calls.append(hotel)
                if hotel == "The Satire":
                    return {
                        "lat": 38.81,
                        "lon": -77.04,
                        "formatted_address": "625 First St, Alexandria, VA 22314, USA",
                        "types": ["lodging", "establishment"],
                        "place_id": "pid_va",
                    }, "PLACES:ok"
                if hotel == "The St. Regis Chengdu":
                    return None, "WEAK_OR_EMPTY:q"
                return {
                    "lat": 40.35,
                    "lon": 23.95,
                    "formatted_address": "Ouranoupolis 630 75, Greece",
                    "types": ["lodging", "establishment"],
                    "place_id": "pid_eagles",
                }, "PLACES:ok"

            with mock.patch.object(gc, "API_KEY", "test-key"), mock.patch.object(
                gc, "BASE_DELAY", 0
            ), mock.patch.object(gc, "geocode_places_first", side_effect=fake_recover), mock.patch(
                "sys.argv",
                ["google-convert.py", "--input", str(inp), "--output", str(out2), "--cache", str(cache_p), "--input-format", "fhrthc"],
            ):
                gc.main()

            with open(out2, encoding="utf-8") as f:
                rows2 = {r["hotel_name"]: r for r in csv.DictReader(f)}
            self.assertEqual(len(n2_calls), 3)
            self.assertEqual(rows2["The Satire"]["lat"], "38.81")
            self.assertEqual(rows2["The St. Regis Chengdu"]["lat"], "30.66")
            self.assertEqual(rows2["Eagles Villas - Small Luxury Hotels of The World"]["lat"], "40.35")
            c2 = json.loads(cache_p.read_text(encoding="utf-8"))
            self.assertIn(gc._MISMATCH_STAMP, c2[k_chengdu])
            self.assertEqual(c2[k_chengdu][gc._MISMATCH_SELECTOR_V], gc.SELECTOR_VERSION)

            # Night 3: 0 API calls (stamped Chengdu + resolved Satire & Eagles Villas).
            n3_calls = []
            with mock.patch.object(gc, "API_KEY", "test-key"), mock.patch.object(
                gc, "BASE_DELAY", 0
            ), mock.patch.object(
                gc, "geocode_places_first", side_effect=lambda *a, **kw: (n3_calls.append(a[2]) or (None, "ERR"))
            ), mock.patch(
                "sys.argv",
                ["google-convert.py", "--input", str(inp), "--output", str(out3), "--cache", str(cache_p), "--input-format", "fhrthc"],
            ):
                gc.main()
            self.assertEqual(n3_calls, [])

    def test_google_convert_partial_error_stores_stamped_no_result_and_bounds_retries(self):
        gc = _load_google_convert()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            inp = tmp_dir / "in.csv"
            out1 = tmp_dir / "out1.csv"
            out2 = tmp_dir / "out2.csv"
            out3 = tmp_dir / "out3.csv"
            cache_p = tmp_dir / "cache.json"

            with open(inp, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(
                    f,
                    fieldnames=["hotel_name", "hotel_url", "hotel_location", "group_label", "brand_label", "group_type"],
                )
                w.writeheader()
                w.writerow({
                    "hotel_name": "Phantom Hotel",
                    "hotel_url": "",
                    "hotel_location": "Nowhere, France",
                    "group_label": "FINE HOTELS + RESORTS",
                    "brand_label": "",
                    "group_type": "Program",
                })
            cache_p.write_text("{}", encoding="utf-8")

            calls = []

            def fake_ts(q, key):
                calls.append(q)
                if len(calls) == 1:
                    raise gc.GeocodeError("INVALID_REQUEST")
                return {"results": []}

            with mock.patch.object(gc, "API_KEY", "test-key"), mock.patch.object(
                gc, "BASE_DELAY", 0
            ), mock.patch.object(gc, "places_text_search", side_effect=fake_ts), mock.patch.object(
                gc, "geocode_google", return_value=None
            ):
                # Night 1: query 1 errors with INVALID_REQUEST, remaining queries return clean empty.
                with mock.patch(
                    "sys.argv",
                    ["google-convert.py", "--input", str(inp), "--output", str(out1), "--cache", str(cache_p), "--input-format", "fhrthc"],
                ):
                    gc.main()
                n1_count = len(calls)
                self.assertGreater(n1_count, 0)

                saved = json.loads(cache_p.read_text(encoding="utf-8"))
                entry = next(iter(saved.values()))
                self.assertEqual(entry.get("status"), "NO_RESULT")
                self.assertIn(gc._MISMATCH_STAMP, entry)

                # Night 2: stamped NO_RESULT bounds retries (0 API calls).
                with mock.patch(
                    "sys.argv",
                    ["google-convert.py", "--input", str(inp), "--output", str(out2), "--cache", str(cache_p), "--input-format", "fhrthc"],
                ):
                    gc.main()
                self.assertEqual(len(calls), n1_count)

                # After the recheck window elapses, it retries again cleanly.
                key = next(iter(saved.keys()))
                saved[key][gc._MISMATCH_STAMP] = 0.0
                cache_p.write_text(json.dumps(saved), encoding="utf-8")
                with mock.patch(
                    "sys.argv",
                    ["google-convert.py", "--input", str(inp), "--output", str(out3), "--cache", str(cache_p), "--input-format", "fhrthc"],
                ):
                    gc.main()
                self.assertGreater(len(calls), n1_count)


if __name__ == "__main__":
    unittest.main()

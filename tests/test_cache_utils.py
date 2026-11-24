import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

import cache_utils
import fhrthc


def _load_google_convert():
    path = Path(__file__).resolve().parent.parent / "google-convert.py"
    stubs = {}
    if "requests" not in sys.modules:
        req = types.ModuleType("requests")
        req.RequestException = Exception
        stubs["requests"] = req
    if "pandas" not in sys.modules:
        pd = types.ModuleType("pandas")
        pd.DataFrame = object
        stubs["pandas"] = pd
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


class UpstreamFixTests(unittest.TestCase):
    def test_canonicalize_amex_url_normalizes_property_prefix(self):
        short = "https://www.americanexpress.com/property/Florida-US/Boca-Raton/Tower-At-The-Boca-Raton"
        canonical = "https://www.americanexpress.com/en-us/travel/discover/property/Florida-US/Boca-Raton/Tower-At-The-Boca-Raton"
        self.assertEqual(fhrthc._canonicalize_amex_url(short), canonical)
        self.assertEqual(fhrthc._canonicalize_amex_url(canonical), canonical)

    def test_google_convert_rejects_parking_and_prefers_lodging(self):
        gc = _load_google_convert()
        self.assertFalse(gc._is_lodging_type(["establishment", "parking", "point_of_interest"]))
        self.assertFalse(gc._is_lodging_type(["natural_feature"]))
        # A real hotel that also lists parking still carries "lodging".
        self.assertTrue(gc._is_lodging_type(["establishment", "lodging", "parking"]))
        # Fallback hotel without "lodging" tag (e.g. spa resort) remains accepted.
        self.assertTrue(gc._is_lodging_type(["establishment", "health", "point_of_interest", "spa"]))

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


if __name__ == "__main__":
    unittest.main()

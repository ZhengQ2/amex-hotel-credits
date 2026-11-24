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


if __name__ == "__main__":
    unittest.main()

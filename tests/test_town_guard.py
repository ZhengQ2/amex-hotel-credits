"""Fresh Places candidates in another town must not be accepted as a different hotel.

Addresses are Google formatted_address values taken from the committed caches.
"""

import unittest
from unittest import mock

import cache_utils
from test_cache_utils import _load_google_convert

conflicts = cache_utils.fresh_candidate_conflicts

HILTON_LAKE_COMO = ("Hilton Lake Como", "Via Borgo Vico, 241, 22100 Como CO, Italy")
# Google lists the renamed hotel under its new name at Il Sereno's address.
JUMEIRAH = ("Jumeirah Lake Como", "Via Torrazza, 10, 22020 Torno CO, Italy")


class FreshCandidateConflictTests(unittest.TestCase):
    def test_rejects_other_hotel_sharing_only_place_words(self):
        # The 25 Nov 2026 nightly pinned Jumeirah Lake Como (the renamed
        # Il Sereno in Torno) at Hilton Lake Como, 6 km away in Como.
        self.assertTrue(conflicts(*HILTON_LAKE_COMO, "Jumeirah Lake Como", "Torno, Italy"))
        self.assertTrue(conflicts(
            "Hilton Helsinki Airport", "Lentäjänkuja 1, 01530 Vantaa, Finland",
            "Waldorf Astoria Helsinki", "Helsinki, Finland",
        ))

    def test_rejects_candidate_that_drops_the_hotels_town(self):
        self.assertTrue(conflicts(
            "The St. Regis Bal Harbour Resort",
            "9703 Collins Avenue Bal Harbour, Miami Beach, FL 33154, USA",
            "The St. Regis Longboat Key Resort", "Longboat Key, Florida US",
        ))

    def test_rejects_candidate_named_for_another_town(self):
        self.assertTrue(conflicts(
            "Four Seasons Hotel Los Angeles At Beverly Hills",
            "300 S Doheny Dr, Los Angeles, CA 90048, USA",
            "Four Seasons Hotel San Francisco", "San Francisco, California US",
        ))
        self.assertTrue(conflicts(
            "Shade Hotel Redondo Beach", "655 N Harbor Dr, Redondo Beach, CA 90277, USA",
            "Shade Hotel", "Manhattan Beach, California US",
        ))

    def test_accepts_the_hotel_in_its_own_town(self):
        self.assertEqual(conflicts(*JUMEIRAH, "Jumeirah Lake Como", "Torno, Italy"), [])

    def test_accepts_correct_pins_whose_address_names_a_neighbouring_place(self):
        # AMEX often lists a nearby town, island or the city a suburb belongs to.
        cases = [
            ("Crown Towers Melbourne", "8 Whiteman St, Southbank VIC 3006, Australia",
             "Crown Towers Melbourne", "Melbourne, Australia"),
            ("Mr. C Coconut Grove", "2988 McFarlane Rd, Coconut Grove, FL 33133, USA",
             "Mr. C Miami Coconut Grove", "Miami, Florida US"),
            ("Mashpi Lodge", "Reserva Privada Mashpi, 150150 Mashpi, Ecuador",
             "Mashpi Lodge", "Pacto, Ecuador"),
            ("Sundance Mountain Resort", "8841 Alpine Loop Scenic Byway, Sundance, UT 84604, USA",
             "Sundance Mountain Resort", "Provo, Utah US"),
            ("Peter Island Resort", "Sprat Bay, British Virgin Islands",
             "Peter Island Resort", "Peter Island, British Virgin Islands"),
            ("Hayfield Manor Hotel",
             "Hayfield Manor Hotel, Perrott Ave, College Rd, Centre, Cork, T12 HT97, Ireland",
             "Hayfield Manor", "Cork City, Ireland"),
            ("Waldhotel by Bürgenstock",
             "Resort Lake Lucerne, Bürgenstock 30, 6363 Obbürgen, Switzerland",
             "Waldhotel by Buergenstock", "Ennetbuergen, Switzerland"),
            ("The St. Regis. Shenzhen BAO AN",
             "1088 Xing Ye Lu, Bao An Qu, No, Guang Dong Sheng, China, 518",
             "The St. Regis Shenzhen Bao'an", "Shenzhen, China"),
            ("La Sultana", "403 Rue de La Kasbah, 63-67 rue Boutouille, Marrakesh 40000, Morocco",
             "La Sultana Marrakech", "Marrakech, Morocco"),
            ("Amrit Ocean Resort", "3100 N Ocean Dr, Riviera Beach, FL 33404, USA",
             "Amrit Ocean Resort and Residences - Singer Island", "Singer Island, Florida US"),
        ]
        for place_name, address, hotel_name, location in cases:
            with self.subTest(hotel_name):
                self.assertEqual(conflicts(place_name, address, hotel_name, location), [])

    def test_no_location_means_no_town_check(self):
        # Hilton rows carry no hotel_location.
        self.assertEqual(conflicts(*HILTON_LAKE_COMO, "Jumeirah Lake Como", ""), [])


class ChooseBestPlaceTownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gc = _load_google_convert()

    @staticmethod
    def _place(name, address, place_id, reviews):
        return {
            "name": name,
            "formatted_address": address,
            "place_id": place_id,
            "types": ["establishment", "lodging", "point_of_interest"],
            "rating": 4.6,
            "user_ratings_total": reviews,
        }

    def test_prefers_the_hotel_over_a_busier_namesake_in_another_town(self):
        results = [
            self._place(*HILTON_LAKE_COMO, "pid_hilton", 4000),
            self._place(*JUMEIRAH, "pid_jumeirah", 300),
        ]
        chosen = self.gc.choose_best_place(results, "", "Jumeirah Lake Como", "Torno, Italy")
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["place_id"], "pid_jumeirah")

    def test_returns_none_when_only_the_namesake_is_found(self):
        results = [self._place(*HILTON_LAKE_COMO, "pid_hilton", 4000)]
        self.assertIsNone(
            self.gc.choose_best_place(results, "", "Jumeirah Lake Como", "Torno, Italy")
        )


class GeocodingFallbackIdentityTests(unittest.TestCase):
    """The Geocoding fallback returns no place name, so it must not bypass the guard."""

    @classmethod
    def setUpClass(cls):
        cls.gc = _load_google_convert()

    @staticmethod
    def _geocode(address, place_id):
        # Shaped like geocode_google(): the raw Geocoding result has no "name".
        return {
            "lat": 45.8, "lon": 9.1, "formatted_address": address,
            "provider": "google_geocoding", "confidence": "ROOFTOP",
            "place_id": place_id, "types": ["establishment", "lodging", "point_of_interest"],
            "partial_match": False,
            "raw": {"formatted_address": address, "place_id": place_id},
        }

    def _lookup(self, text_results, geocoded, details, hotel_name, location):
        gc = self.gc
        with mock.patch.object(gc, "API_KEY", "test-key"), mock.patch.object(
            gc, "places_text_search", return_value={"results": text_results}
        ), mock.patch.object(gc, "geocode_google", return_value=geocoded), mock.patch.object(
            gc, "places_details", side_effect=details
        ) as places_details:
            result, status = gc.geocode_places_first(
                [f"{hotel_name} {location} Hotel"], "", hotel_name, 0, location
            )
        return result, status, places_details

    def test_rejects_the_namesake_the_text_search_guard_rejected(self):
        hilton = {
            "name": HILTON_LAKE_COMO[0], "formatted_address": HILTON_LAKE_COMO[1],
            "place_id": "pid_hilton", "types": ["establishment", "lodging"],
            "rating": 4.6, "user_ratings_total": 4000,
        }
        result, _, places_details = self._lookup(
            [hilton],
            self._geocode(HILTON_LAKE_COMO[1], "pid_hilton"),
            lambda place_id, key: {"name": HILTON_LAKE_COMO[0], "place_id": place_id},
            "Jumeirah Lake Como", "Torno, Italy",
        )
        self.assertIsNone(result)
        places_details.assert_called_once_with("pid_hilton", "test-key")

    def test_rejects_an_out_of_town_result_whose_name_cannot_be_found(self):
        result, _, _ = self._lookup(
            [], self._geocode(HILTON_LAKE_COMO[1], "pid_hilton"),
            lambda place_id, key: None, "Jumeirah Lake Como", "Torno, Italy",
        )
        self.assertIsNone(result)

    def test_accepts_an_out_of_town_result_named_for_the_hotel(self):
        address = "Reserva Privada Mashpi, 150150 Mashpi, Ecuador"
        result, status, places_details = self._lookup(
            [], self._geocode(address, "pid_mashpi"),
            lambda place_id, key: {"name": "Mashpi Lodge", "place_id": place_id},
            "Mashpi Lodge", "Pacto, Ecuador",
        )
        self.assertIsNotNone(result)
        self.assertTrue(status.startswith("GEOCODE:"))
        places_details.assert_called_once()

    def test_in_town_result_needs_no_extra_lookup(self):
        result, status, places_details = self._lookup(
            [], self._geocode(JUMEIRAH[1], "pid_jumeirah"),
            lambda place_id, key: self.fail("no Place Details call expected in the hotel's town"),
            "Jumeirah Lake Como", "Torno, Italy",
        )
        self.assertIsNotNone(result)
        self.assertTrue(status.startswith("GEOCODE:"))
        places_details.assert_not_called()


if __name__ == "__main__":
    unittest.main()

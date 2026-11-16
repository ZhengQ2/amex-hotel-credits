import io
import json
from datetime import date, datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import fetch_hotel_prices as fhp


NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
START, END = fhp.window(NOW)
HOTEL = {"id": "place-a", "n": "Conrad Test Resort", "a": "1 Beach Rd, Testville", "lat": 10.0, "lon": 20.0, "p": "Hilton"}
KEY = "secret-key-123"


def prop(name, token, lat=10.0, lon=20.0, **extra):
    return {"name": name, "property_token": token, "gps_coordinates": {"latitude": lat, "longitude": lon}, **extra}


def calendar(days, start=START, **fields):
    fields = fields or {"extracted_before_taxes_fees": 200, "extracted_display_price": 200}
    return [{"date": (start + timedelta(days=offset)).isoformat(), "currency": "USD", **fields} for offset in range(days)]


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FakeOpener:
    def __init__(self, responses):
        self.responses, self.urls = list(responses), []

    def open(self, request, timeout=None):
        self.urls.append(request.full_url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(json.dumps(item).encode())


def http_error(code, body=None):
    return HTTPError("https://serpapi.com/search.json", code, "error", Message(), io.BytesIO(json.dumps(body or {}).encode()))


class FakeClient:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def search(self, params):
        self.calls.append(params)
        return self.responses.pop(0)


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def client(self, responses, **budget):
        ledger = fhp.Budget(self.dir / "usage.json", initialize=True, now=NOW, **budget)
        return fhp.SerpApi(KEY, ledger, min_interval=0, opener=FakeOpener(responses), sleep=lambda seconds: None), ledger


class CandidateTests(unittest.TestCase):
    def test_properties_list_skips_vacation_rentals_and_incomplete_items(self):
        payload = {"properties": [prop("Conrad Test Resort", "tok-a"), prop("Beach Villa", "tok-b", type="vacation rental"),
                                  {"name": "No token", "gps_coordinates": {"latitude": 1, "longitude": 2}},
                                  {"name": "No coordinates", "property_token": "tok-c"}]}
        self.assertEqual([c["id"] for c in fhp.candidates_from(payload)], ["tok-a"])

    def test_single_property_page_and_empty_results(self):
        payload = prop("Conrad Test Resort", "tok-a", address="1 Beach Rd", type="hotel")
        self.assertEqual(fhp.candidates_from(payload)[0]["address"], "1 Beach Rd")
        self.assertEqual(fhp.candidates_from(None), [])
        with self.assertRaises(fhp.SchemaError):
            fhp.candidates_from({"search_information": {}})


class MatchTests(unittest.TestCase):
    def test_close_identical_name_matches(self):
        result = fhp.match_hotel([HOTEL], fhp.candidates_from({"properties": [prop("Conrad Test Resort", "tok-a", lat=10.001)]}))
        self.assertEqual((result["status"], result["provider_id"]), ("matched", "tok-a"))

    def test_near_duplicates_are_ambiguous_and_distant_hotels_unmatched(self):
        twins = {"properties": [prop("Conrad Test Resort", "tok-a"), prop("Conrad Test Resort", "tok-b", lat=10.0005)]}
        self.assertEqual(fhp.match_hotel([HOTEL], fhp.candidates_from(twins))["status"], "ambiguous")
        far = {"properties": [prop("Conrad Test Resort", "tok-a", lat=10.5)]}
        self.assertEqual(fhp.match_hotel([HOTEL], fhp.candidates_from(far))["status"], "unmatched")

    def test_different_hotel_next_door_is_not_matched(self):
        result = fhp.match_hotel([HOTEL], fhp.candidates_from({"properties": [prop("Seaside Inn", "tok-z")]}))
        self.assertEqual(result["status"], "ambiguous")

    def test_shortened_or_suffixed_google_names_match_on_the_same_spot(self):
        # Pilot cases: Google drops the city or appends a brand collection.
        for ours, theirs in (("Hotel Locarno Roma", "Hotel Locarno"), ("Fontaine Hotel", "The Fontaine"),
                             ("Humaniti Hotel Montreal", "Humaniti Hotel Montreal Autograph Collection"),
                             ("Waldorf Astoria Orlando", "Waldorf Astoria Orlando - An Official Walt Disney World Hotel")):
            hotel = {**HOTEL, "n": ours}
            payload = {"properties": [prop(theirs, "tok-a", lat=10.0001), prop("Pestana Carlton", "tok-b", lat=10.001)]}
            self.assertEqual(fhp.match_hotel([hotel], fhp.candidates_from(payload)).get("provider_id"), "tok-a", ours)

    def test_contained_names_need_the_same_spot_and_not_a_residences_listing(self):
        hotel = {**HOTEL, "n": "Waldorf Astoria Orlando"}
        nearby = {"properties": [prop("Waldorf Astoria Orlando - Official Disney Hotel", "tok-a", lat=10.002)]}  # ~220 m
        self.assertEqual(fhp.match_hotel([hotel], fhp.candidates_from(nearby))["status"], "ambiguous")
        sister = {"properties": [prop("Conrad Test Resort Residences", "tok-r")]}
        self.assertEqual(fhp.match_hotel([HOTEL], fhp.candidates_from(sister))["status"], "ambiguous")


class AggregateTests(unittest.TestCase):
    def test_prefers_before_tax_prices_and_ignores_out_of_window_and_foreign_currency(self):
        prices = calendar(91) + [{"date": (START - timedelta(days=1)).isoformat(), "currency": "USD", "extracted_before_taxes_fees": 9999},
                                 {"date": (END + timedelta(days=1)).isoformat(), "currency": "USD", "extracted_before_taxes_fees": 9999}]
        prices[0] = {**prices[0], "currency": "EUR", "extracted_before_taxes_fees": 9999}
        result = fhp.aggregate_calendar(prices, START, END, NOW)
        self.assertEqual((result["status"], result["tax_basis"], result["sample_count"]), ("available", "excluded", 90))
        self.assertEqual((result["estimate"], result["low"], result["high"]), (200, 200, 200))
        self.assertEqual(result["expires_at"], "2027-06-04T12:00:00Z")  # 200 days: a 6-month cycle plus slack

    def test_falls_back_to_display_price_with_unknown_tax_basis(self):
        result = fhp.aggregate_calendar(calendar(30, extracted_display_price=150), START, END, NOW)
        self.assertEqual((result["status"], result["tax_basis"], result["estimate"]), ("available", "unknown", 150))

    def test_sparse_or_duplicated_dates_are_unavailable(self):
        self.assertEqual(fhp.aggregate_calendar(calendar(20), START, END, NOW)["status"], "unavailable")
        doubled = calendar(25) + calendar(25)
        result = fhp.aggregate_calendar(doubled, START, END, NOW)
        self.assertEqual((result["status"], result["sample_count"]), ("unavailable", 0))
        with self.assertRaises(fhp.SchemaError):
            fhp.aggregate_calendar([{"price": 1}], START, END, NOW)


class ClientTests(TempDirTest):
    def test_no_results_error_returns_none_and_is_still_counted(self):
        client, ledger = self.client([{"error": "Google Hotels hasn't returned any results for this query."}])
        self.assertIsNone(client.search({"engine": "google_hotels", "q": "x"}))
        self.assertEqual(json.loads((self.dir / "usage.json").read_text())["months"]["2026-11"]["search"], 1)

    def test_rate_limit_stops_without_retrying(self):
        client, ledger = self.client([http_error(429)])
        with self.assertRaises(fhp.BudgetError):
            client.search({"engine": "google_hotels", "q": "x"})
        self.assertEqual(ledger.used, 1)

    def test_server_errors_retry_three_times_and_reserve_each_attempt(self):
        client, ledger = self.client([http_error(503)] * 3)
        with self.assertRaises(fhp.PriceError):
            client.search({"engine": "google_hotels", "q": "x"})
        self.assertEqual(ledger.used, 3)

    def test_budget_is_enforced_before_the_request(self):
        client, ledger = self.client([{"properties": []}], max_requests=1)
        client.search({"engine": "google_hotels", "q": "x"})
        with self.assertRaises(fhp.BudgetError):
            client.search({"engine": "google_hotels", "q": "y"})
        self.assertEqual(len(client.opener.urls), 1)

    def test_errors_never_reveal_the_api_key(self):
        client, _ = self.client([{"error": f"Invalid API key {KEY}"}, http_error(400, {"error": f"bad key {KEY}"})])
        for _ in range(2):
            with self.assertRaises(fhp.PriceError) as caught:
                client.search({"engine": "google_hotels", "q": "x"})
            self.assertNotIn(KEY, str(caught.exception))
            self.assertIn("[redacted]", str(caught.exception))

    def test_account_check_reports_quota_without_counting_a_search(self):
        client, ledger = self.client([{"account_email": "private", "api_key": KEY, "total_searches_left": 240,
                                       "account_rate_limit_per_hour": 50}])
        self.assertEqual(client.account(), {"searches_left": 240, "hourly_limit": 50})
        self.assertEqual(ledger.used, 0)

    def test_missing_ledger_refuses_to_run(self):
        with self.assertRaises(fhp.BudgetError):
            fhp.Budget(self.dir / "usage.json", now=NOW)

    def test_hourly_allowance_follows_the_clock_across_a_long_run(self):
        times = [NOW, NOW, NOW + timedelta(hours=1)]
        ledger = fhp.Budget(self.dir / "usage.json", initialize=True, now=NOW, max_requests=10,
                            clock=lambda: times[0])
        ledger.hourly_limit = 2
        ledger.reserve()
        ledger.reserve()
        with self.assertRaises(fhp.BudgetError):
            ledger.reserve()
        times[0] = NOW + timedelta(hours=1)
        ledger.reserve()
        self.assertEqual(json.loads((self.dir / "usage.json").read_text())["hours"],
                         {"2026-11-16T12": 2, "2026-11-16T13": 1})

    def test_run_reservation_spends_only_its_allowance(self):
        fhp.Budget(self.dir / "usage.json", initialize=True, now=NOW, monthly_limit=10).reserve_run("ci-1", 3)
        with self.assertRaises(fhp.BudgetError):
            fhp.Budget(self.dir / "usage.json", now=NOW, monthly_limit=10).reserve_run("ci-1", 1)
        ledger = fhp.Budget(self.dir / "usage.json", now=NOW, monthly_limit=10, max_requests=10, reservation_id="ci-1")
        for _ in range(3):
            ledger.reserve()
        with self.assertRaises(fhp.BudgetError):
            ledger.reserve()
        state = json.loads((self.dir / "usage.json").read_text())
        self.assertEqual((state["months"]["2026-11"]["search"], state["reservations"]["ci-1"]["used"]), (3, 3))


    def test_month_end_reservation_takes_what_is_left_and_returns_the_unused_part(self):
        ledger = fhp.Budget(self.dir / "usage.json", initialize=True, now=NOW, monthly_limit=1000)
        ledger.state["months"]["2026-11"] = {"search": 980}
        self.assertEqual(ledger.reserve_run("ci-1", 40), 20)
        run = fhp.Budget(self.dir / "usage.json", now=NOW, monthly_limit=1000, max_requests=40, reservation_id="ci-1")
        self.assertEqual(run.max_requests, 20)
        for _ in range(5):
            run.reserve()
        run.settle()
        state = json.loads((self.dir / "usage.json").read_text())
        self.assertEqual((state["months"]["2026-11"]["search"], state["reservations"]["ci-1"]["allowance"]), (985, 5))

    def test_quota_pacing_counts_days_until_renewal(self):
        self.assertEqual(fhp.days_until_reset(NOW, 1), 15)  # Nov 16 through Nov 30
        self.assertEqual(fhp.days_until_reset(NOW, 20), 4)
        self.assertEqual(fhp.days_until_reset(NOW, 16), 30)  # Renewed today
        self.assertEqual(fhp.days_until_reset(datetime(2026, 12, 31, tzinfo=timezone.utc), 1), 1)


class RotationTests(TempDirTest):
    def hotel(self, key, name, lat):
        return {**HOTEL, "id": key, "n": name, "lat": lat}

    def test_overdue_prices_then_new_hotels_then_oldest_prices(self):
        names = {"place-a": "Alpha Resort", "place-b": "Bravo Resort", "place-c": "Charlie Resort", "place-d": "Delta Resort"}
        groups = {key: [self.hotel(key, name, 1.0 + index)] for index, (key, name) in enumerate(names.items())}
        mappings, estimates = {}, {}
        for key, token, age in (("place-a", "tok-a", 160), ("place-b", "tok-b", 40)):
            mappings[key] = {"status": "matched", "provider_id": token, "fingerprint": fhp.fingerprint(groups[key]),
                             "checked_at": fhp.timestamp(NOW - timedelta(days=400))}  # Old matches stay valid.
            estimates[key] = {"status": "available", "last_attempt_at": fhp.timestamp(NOW - timedelta(days=age))}
        client, ledger = self.client([{"prices": calendar(91)}, {"properties": [prop("Charlie Resort", "tok-c", lat=3.0)]},
                                      {"prices": calendar(91)}, {"prices": calendar(91)}], max_requests=4)
        fhp.rotate(client, groups, mappings, estimates, {}, NOW, lambda: None, lambda: None)
        sent = [(params.get("engine", [""])[0], (params.get("property_token") or params.get("q"))[0])
                for params in (parse_qs(urlsplit(url).query) for url in client.opener.urls)]
        self.assertEqual(sent, [("google_hotels_price_calendar", "tok-a"), ("google_hotels", "Charlie Resort, 1 Beach Rd, Testville"),
                                ("google_hotels_price_calendar", "tok-c"), ("google_hotels_price_calendar", "tok-b")])
        self.assertEqual({key: estimates.get(key, {}).get("status") for key in groups},
                         {"place-a": "available", "place-b": "available", "place-c": "available", "place-d": None})
        self.assertNotIn("place-d", mappings)  # Waits for tomorrow's allowance.

    def test_unmatched_hotels_are_retried_twice_a_year(self):
        rows, query = [HOTEL], fhp.search_query([HOTEL])
        prior = {"status": "unmatched", "fingerprint": fhp.fingerprint(rows), "query": query,
                 "checked_at": fhp.timestamp(NOW - timedelta(days=61))}
        self.assertFalse(fhp.mapping_due(prior, rows, query, NOW))
        self.assertTrue(fhp.mapping_due(prior, rows, query, NOW + timedelta(days=120)))
        self.assertTrue(fhp.mapping_due({**prior, "status": "matched", "fingerprint": "changed"}, rows, query, NOW))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.groups = {"place-a": [HOTEL]}
        self.mappings = {"place-a": {"status": "matched", "provider_id": "tok-a", "fingerprint": fhp.fingerprint([HOTEL]),
                                     "checked_at": fhp.timestamp(NOW)}}

    def test_mapping_searches_by_name_and_address(self):
        client, mappings = FakeClient([{"properties": [prop("Conrad Test Resort", "tok-a")]}]), {}
        fhp.resolve_mappings(client, self.groups, mappings, {}, NOW, lambda: None)
        self.assertEqual(client.calls[0]["q"], "Conrad Test Resort, 1 Beach Rd, Testville")
        self.assertEqual((client.calls[0]["check_in_date"], client.calls[0]["check_out_date"]), ("2026-11-30", "2026-12-01"))
        self.assertEqual(mappings["place-a"]["provider_id"], "tok-a")
        fhp.resolve_mappings(client, self.groups, mappings, {}, NOW + timedelta(days=1), lambda: None)
        self.assertEqual(len(client.calls), 1)  # Cached matches are not searched again.

    def test_queries_use_broad_location_and_retry_by_name_when_google_finds_nothing(self):
        hotel = {**HOTEL, "n": "Tierra Patagonia", "a": "On the edge of the park, Torres de Paine, Magallanes, Chile, 6160000"}
        self.assertEqual(fhp.search_query([hotel]), "Tierra Patagonia, Magallanes, Chile")
        self.assertEqual(fhp.search_query([{**HOTEL, "a": "Corniche St - Al Bateen - Abu Dhabi - UAE"}]),
                         "Conrad Test Resort, Abu Dhabi, UAE")
        client, mappings = FakeClient([None, {"properties": [prop("Tierra Patagonia", "tok-t")]}]), {}
        fhp.resolve_mappings(client, {"place-a": [hotel]}, mappings, {}, NOW, lambda: None)
        self.assertEqual([call["q"] for call in client.calls], ["Tierra Patagonia, Magallanes, Chile", "Tierra Patagonia"])
        self.assertEqual(mappings["place-a"]["provider_id"], "tok-t")

    def test_a_changed_query_rechecks_unmatched_hotels_but_not_matched_ones(self):
        identity = fhp.fingerprint([HOTEL])
        mappings = {"place-a": {"status": "unmatched", "fingerprint": identity, "checked_at": fhp.timestamp(NOW)},
                    "place-b": {**self.mappings["place-a"]}}
        client = FakeClient([{"properties": [prop("Conrad Test Resort", "tok-a")]}])
        fhp.resolve_mappings(client, {"place-a": [HOTEL], "place-b": [HOTEL]}, mappings, {}, NOW, lambda: None)
        self.assertEqual((len(client.calls), mappings["place-a"]["status"]), (1, "matched"))

    def test_rule_changes_redecide_stored_candidates_without_searching(self):
        hotel = {**HOTEL, "n": "Hotel Locarno Roma"}
        evidence = [{"provider_id": "tok-l", "name": "Hotel Locarno", "address": "", "distance_m": 0, "name_score": 0.839}]
        mappings = {"place-a": {"status": "ambiguous", "candidates": evidence, "fingerprint": fhp.fingerprint([hotel]),
                                "query": "Hotel Locarno Roma, Italy", "checked_at": fhp.timestamp(NOW)}}
        self.assertEqual(fhp.redecide_mappings({"place-a": [hotel]}, mappings), 1)
        self.assertEqual((mappings["place-a"]["status"], mappings["place-a"]["provider_id"]), ("matched", "tok-l"))
        self.assertEqual(mappings["place-a"]["checked_at"], fhp.timestamp(NOW))
        self.assertEqual(fhp.redecide_mappings({"place-a": [hotel]}, mappings), 0)  # Already on the current rules.

    def test_two_map_entries_on_one_google_property_are_held_for_review(self):
        other = {**HOTEL, "id": "place-b", "n": "Conrad Test Resort Annex"}
        groups = {"place-a": [HOTEL], "place-b": [other]}
        mappings = {**self.mappings, "place-b": {**self.mappings["place-a"], "fingerprint": fhp.fingerprint([other])}}
        fhp.reject_provider_collisions(groups, mappings, NOW)
        self.assertEqual({mappings[key]["status"] for key in groups}, {"provider_conflict"})

    def test_weekend_means_friday_or_saturday_check_in(self):
        weekdays_only = [day for day in (START + timedelta(days=offset) for offset in range(60)) if day.weekday() < 4]
        self.assertFalse(fhp.sufficient(weekdays_only))
        first_friday = START + timedelta(days=(4 - START.weekday()) % 7)
        self.assertTrue(fhp.sufficient(weekdays_only + [first_friday]))

    def test_refresh_keeps_last_good_estimate_when_google_has_no_prices(self):
        estimates = {}
        fhp.refresh_prices(FakeClient([{"prices": calendar(91)}]), self.groups, self.mappings, estimates, NOW, lambda: None)
        observed = estimates["place-a"]["observed_at"]
        later = NOW + timedelta(days=7)
        fhp.refresh_prices(FakeClient([None]), self.groups, self.mappings, estimates, later, lambda: None)
        self.assertEqual((estimates["place-a"]["status"], estimates["place-a"]["observed_at"]), ("available", observed))
        self.assertEqual(estimates["place-a"]["last_attempt_status"], "unavailable")

    def test_recently_refreshed_hotels_are_skipped(self):
        estimates, client = {}, FakeClient([{"prices": calendar(91)}])
        fhp.refresh_prices(client, self.groups, self.mappings, estimates, NOW, lambda: None)
        fhp.refresh_prices(client, self.groups, self.mappings, estimates, NOW + timedelta(days=2), lambda: None)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["start_date"], START.isoformat())

    def test_public_dataset_omits_internal_fields_and_expired_estimates(self):
        estimates = {}
        fhp.refresh_prices(FakeClient([{"prices": calendar(91)}]), self.groups, self.mappings, estimates, NOW, lambda: None)
        public = fhp.public_prices(self.groups, self.mappings, estimates, NOW)
        self.assertEqual(public["source"], "Google Hotels via SerpApi")
        self.assertEqual(set(public["hotels"]["place-a"]) & {"provider_id", "fingerprint", "last_attempt_at"}, set())
        self.assertEqual(fhp.public_prices(self.groups, self.mappings, estimates, NOW + timedelta(days=201))["hotels"], {})


if __name__ == "__main__":
    unittest.main()

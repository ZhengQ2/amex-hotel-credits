import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import hotel_history
from hotel_history import ProgramDiff, merge_events, run_events

HEADER = "hotel_name,hotel_url,hotel_location,group_label,brand_label,lat,lon,formatted_address,place_id,types,status\n"
FHR = "Fine Hotels + Resorts"
THC = "The Hotel Collection"
HILTON = "Hilton Portfolio"


def row(name, location="Paris, France", group="FINE HOTELS + RESORTS", brand="", lat="48.8600", lon="2.3300",
        place_id=None, url=None):
    place_id = place_id if place_id is not None else "pid_" + name.replace(" ", "_").lower()
    url = url or "https://www.americanexpress.com/en-us/travel/discover/property/x?intlink=US-card"
    fields = [name, url, location, group, brand, lat, lon, f"1 Rue {name}, {location}", place_id,
              "establishment;lodging", "CACHED"]
    return ",".join(f'"{f}"' for f in fields) + "\n"


def csv_doc(*rows):
    return HEADER + "".join(rows)


def filler(count, group="FINE HOTELS + RESORTS"):
    # Unrelated hotels far apart, so a list is big enough for the bulk limit.
    return [row(f"Filler {i}", location=f"Town{i}, Spain", group=group, lat=f"{40 + i * 0.1:.4f}",
                lon=f"{-3 - i * 0.1:.4f}") for i in range(count)]


class RunEventsTests(unittest.TestCase):
    def test_added_and_removed_hotels_become_events(self):
        base = filler(10)
        old = csv_doc(*base, row("Hotel Lutetia", brand="The Leading Hotels of the World"))
        new = csv_doc(*base, row("Aman Venice", location="Venice, Italy", lat="45.4400", lon="12.3300"))
        events = run_events([ProgramDiff(FHR, old, new)], "2026-11-20", "https://example.com/run/1")
        by_change = {event["change"]: event for event in events}
        self.assertEqual(set(by_change), {"added", "removed"})
        added, removed = by_change["added"], by_change["removed"]
        self.assertEqual(added["name"], "Aman Venice")
        self.assertEqual(added["date"], "2026-11-20")
        self.assertEqual(added["program"], FHR)
        self.assertEqual(added["source"], "https://example.com/run/1")
        self.assertEqual(added["location"], "Venice, Italy")
        self.assertEqual(added["address"], "1 Rue Aman Venice, Venice, Italy")
        self.assertEqual(added["lat"], 45.44)
        # The program's own name is not a brand, and the click-tracking parameter is dropped.
        self.assertNotIn("brand", added)
        self.assertNotIn("intlink", added["url"])
        self.assertEqual(removed["name"], "Hotel Lutetia")
        self.assertEqual(removed["brand"], "The Leading Hotels of the World")

    def test_first_load_is_not_news(self):
        events = run_events([ProgramDiff(FHR, "", csv_doc(*filler(10)))], "2026-11-20")
        self.assertEqual(events, [])

    def test_bulk_change_is_skipped(self):
        old = csv_doc(*filler(80))
        new = csv_doc(*filler(80)[:20])  # 60 of 80 hotels vanished: a scraper outage
        self.assertEqual(run_events([ProgramDiff(FHR, old, new)], "2026-11-20"), [])

    def test_matcher_renames_are_kept_with_previous_name(self):
        old = csv_doc(*filler(5), row("Armani Dubai", location="Dubai, United Arab Emirates", place_id="pid_armani"))
        new = csv_doc(*filler(5), row("Armani Hotel Dubai, Burj Khalifa", location="Dubai, United Arab Emirates",
                                      place_id="pid_armani"))
        events = run_events([ProgramDiff(FHR, old, new)], "2026-11-20")
        self.assertEqual([(e["change"], e["name"], e["previous_name"]) for e in events],
                         [("renamed", "Armani Hotel Dubai, Burj Khalifa", "Armani Dubai")])

    def test_shared_place_relisting_is_a_rename(self):
        # Two listings share one Google place, so the email's matcher cannot use it to pair them;
        # each new name must still go to the right old one (Residences is a sibling word).
        def nekajui(name):
            return row(name, location="Liberia, Costa Rica", brand="Ritz-Carlton", lat="10.6300", lon="-85.7000",
                       place_id="pid_nekajui")
        old = csv_doc(*filler(5), nekajui("Nekajui, a Ritz-Carlton Reserve"),
                      nekajui("Nekajui, a Ritz-Carlton Reserve Residences"))
        new = csv_doc(*filler(5), nekajui("Nekajui Peninsula Papagayo, a Ritz-Carlton Reserve"),
                      nekajui("Nekajui Peninsula Papagayo, a Ritz-Carlton Reserve Residence"))
        events = run_events([ProgramDiff(FHR, old, new)], "2026-11-21")
        self.assertEqual(sorted((e["change"], e["previous_name"], e["name"]) for e in events), [
            ("renamed", "Nekajui, a Ritz-Carlton Reserve", "Nekajui Peninsula Papagayo, a Ritz-Carlton Reserve"),
            ("renamed", "Nekajui, a Ritz-Carlton Reserve Residences",
             "Nekajui Peninsula Papagayo, a Ritz-Carlton Reserve Residence"),
        ])

    def test_nearby_relisting_without_shared_place_stays_visible(self):
        # 30 m apart but different Google places: possibly a rename, possibly a neighbor, so
        # keep the addition and removal (renames are hidden by default) and point them at each other.
        old = csv_doc(*filler(5), row("W Fort Lauderdale", location="Fort Lauderdale, Florida US",
                                      lat="26.1300", lon="-80.1000"))
        new = csv_doc(*filler(5), row("W Fort Lauderdale Beach", location="Fort Lauderdale, Florida US",
                                      lat="26.1302", lon="-80.1001"))
        events = {e["change"]: e for e in run_events([ProgramDiff(THC, old, new)], "2026-11-21")}
        self.assertEqual(set(events), {"added", "removed"})
        self.assertEqual(events["added"]["maybe_renamed_from"], "W Fort Lauderdale")
        self.assertEqual(events["removed"]["maybe_renamed_to"], "W Fort Lauderdale Beach")

    def test_sibling_swaps_stay_added_and_removed(self):
        # The sibling pairs the email's matcher keeps apart (tests/test_hotel_changes.py), even
        # when they share a Google place: one leaving and the other joining is two changes.
        pairs = [
            (dict(name="Four Seasons Hotel New York", location="New York, New York US", lat="40.7623", lon="-73.9713"),
             dict(name="Four Seasons Hotel New York Downtown", location="New York, New York US", lat="40.7126",
                  lon="-74.0091")),
            (dict(name="Zadun Los Cabos, a Ritz-Carlton Reserve", location="San José del Cabo, Baja California Sur MX",
                  lat="23.0630", lon="-109.6562"),
             dict(name="Zadun Los Cabos, a Ritz-Carlton Reserve Residence",
                  location="San José del Cabo, Baja California Sur MX", lat="23.0631", lon="-109.6563")),
            (dict(name="Hyatt Zilara Cap Cana", location="Punta Cana, Dominican Republic", lat="18.4970", lon="-68.3820"),
             dict(name="Hyatt Ziva Cap Cana", location="Punta Cana, Dominican Republic", lat="18.4960", lon="-68.3810")),
            (dict(name="Hilton Dubai Jumeirah", location="", group="Hilton Hotels & Resorts", lat="25.0772", lon="55.1342"),
             dict(name="Hilton Dubai Palm Jumeirah", location="", group="Hilton Hotels & Resorts", lat="25.1095",
                  lon="55.1489")),
            (dict(name="Mangrove Beach Corendon Curacao All-Inclusive Resort, Curio by Hilton",
                  location="Willemstad, Curacao", lat="12.1110", lon="-68.9480"),
             dict(name="The Rif at Mangrove Beach Corendon Curacao All-Inclusive, Curio by Hilton",
                  location="Willemstad, Curacao", lat="12.1100", lon="-68.9475")),
            # Different Hilton property codes are different hotels.
            (dict(name="Conrad Algarve", location="", group="Conrad Hotels & Resorts",
                  url="https://www.hilton.com/en/hotels/faoapci-conrad-algarve/", lat="37.0632", lon="-8.0290"),
             dict(name="Conrad Algarve Resort", location="", group="Conrad Hotels & Resorts",
                  url="https://www.hilton.com/en/hotels/faoqlci-conrad-algarve-resort/", lat="37.0633", lon="-8.0291")),
        ]
        cases = [(old, new, shared) for old, new in pairs for shared in (False, True)
                 # On one shared place the email's matcher itself calls this pair a rename.
                 if not (shared and new["name"].startswith("The Rif at"))]
        for old_fields, new_fields, shared_place in cases:
            with self.subTest(old=old_fields["name"], new=new_fields["name"], shared_place=shared_place):
                others = []
                if shared_place:
                    # A third listing on the same Google place, so the email's matcher cannot
                    # use it as an ID and leaves the pair to this module.
                    place = {"place_id": "pid_shared"}
                    others = [row("Neighbor Lodge", location=old_fields["location"], place_id="pid_shared",
                                  lat=old_fields["lat"], lon=old_fields["lon"])]
                else:
                    place = {}
                old = csv_doc(*filler(5), *others, row(**old_fields, **place))
                new = csv_doc(*filler(5), *others, row(**new_fields, **place))
                program = HILTON if "hilton.com" in old_fields.get("url", "") else FHR
                events = run_events([ProgramDiff(program, old, new)], "2026-11-26")
                self.assertEqual(sorted(e["change"] for e in events), ["added", "removed"])
                self.assertFalse([e for e in events if any(key.startswith("maybe_") for key in e)])

    def test_same_hotel_moving_between_programs_points_both_ways(self):
        sanderling = row("The Sanderling", location="Duck, North Carolina US", lat="36.2300", lon="-75.7700",
                         place_id="pid_sanderling", group="{}")
        fhr_old = csv_doc(*filler(5))
        fhr_new = csv_doc(*filler(5), sanderling.replace("{}", "FINE HOTELS + RESORTS"))
        thc_old = csv_doc(*filler(5, "THE HOTEL COLLECTION"), sanderling.replace("{}", "THE HOTEL COLLECTION"))
        thc_new = csv_doc(*filler(5, "THE HOTEL COLLECTION"))
        events = run_events([ProgramDiff(FHR, fhr_old, fhr_new), ProgramDiff(THC, thc_old, thc_new)], "2026-04-03")
        moves = {(e["change"], e["program"], e.get("from_program"), e.get("to_program")) for e in events}
        self.assertEqual(moves, {("added", FHR, THC, None), ("removed", THC, None, FHR)})

    def test_far_apart_listing_in_same_town_is_a_possible_rename(self):
        # A wrong geocode put the new name 2 km away: keep both, but point them at each other.
        old = csv_doc(*filler(5), row("Hotel Wailea, Relais & Chateaux", location="Wailea, Hawaii US",
                                      lat="20.6900", lon="-156.4400"))
        new = csv_doc(*filler(5), row("Hotel Wailea Maui", location="Wailea, Hawaii US",
                                      lat="20.6750", lon="-156.4450"))
        events = {e["change"]: e for e in run_events([ProgramDiff(FHR, old, new)], "2026-11-21")}
        self.assertEqual(events["added"]["maybe_renamed_from"], "Hotel Wailea, Relais & Chateaux")
        self.assertEqual(events["removed"]["maybe_renamed_to"], "Hotel Wailea Maui")

    def test_different_hotels_in_one_town_stay_unpaired(self):
        old = csv_doc(*filler(5), row("Il Sereno Lago di Como", location="Torno, Italy", lat="45.8500", lon="9.1100"))
        new = csv_doc(*filler(5), row("Jumeirah Lake Como", location="Torno, Italy", lat="45.8200", lon="9.0800"))
        events = run_events([ProgramDiff(FHR, old, new)], "2026-11-25")
        self.assertEqual(sorted(e["change"] for e in events), ["added", "removed"])
        self.assertTrue(all(not any(k.startswith("maybe_") for k in e) for e in events))


class LogFileTests(unittest.TestCase):
    def test_merge_skips_duplicates_and_sorts_newest_first(self):
        log = hotel_history.empty_log()
        older = {"date": "2026-11-01", "program": THC, "change": "added", "name": "B"}
        newer = {"date": "2026-11-02", "program": FHR, "change": "removed", "name": "A"}
        hilton = {"date": "2026-11-02", "program": HILTON, "change": "added", "name": "Z"}
        self.assertEqual(merge_events(log, [older, newer]), 2)
        self.assertEqual(merge_events(log, [dict(newer), hilton]), 1)
        self.assertEqual([e["name"] for e in log["events"]], ["Z", "A", "B"])

    def test_write_and_load_round_trip_one_event_per_line(self):
        log = hotel_history.empty_log()
        log["tracking_since"] = {THC: "2026-03-30", HILTON: "2026-03-01"}
        merge_events(log, [{"date": "2026-11-01", "program": THC, "change": "added", "name": "Café Ünïcode"},
                           {"date": "2026-11-02", "program": THC, "change": "added", "name": "Other"}])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "changes.json")
            hotel_history.write_log(path, log)
            text = Path(path).read_text(encoding="utf-8")
            self.assertIn("Café Ünïcode", text)
            self.assertEqual(len(text.splitlines()), 4)
            self.assertEqual(hotel_history.load_log(path), log)
            self.assertLess(text.index(HILTON), text.index(THC))  # programs in map order

    def test_empty_log_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "changes.json")
            hotel_history.write_log(path, hotel_history.empty_log())
            self.assertEqual(hotel_history.load_log(path)["events"], [])


class GitRepoTests(unittest.TestCase):
    """update and backfill against a throwaway repository laid out like this one."""

    CSV = "cache/fhr_hotels_geocoded_google.csv"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for key in ("GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_SERVER_URL"):
            os.environ.pop(key, None)
        self.git("init", "-q")
        os.makedirs("cache")

    def tearDown(self):
        os.chdir(self.cwd)
        self.env.stop()
        self.tmp.cleanup()

    def git(self, *args, author="Someone"):
        env = {**os.environ, "GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": "a@example.com",
               "GIT_COMMITTER_NAME": author, "GIT_COMMITTER_EMAIL": "a@example.com"}
        subprocess.run(["git", *args], check=True, capture_output=True, env=env)

    def commit_csv(self, text, author, date):
        Path(self.CSV).write_text(text, encoding="utf-8")
        self.git("add", self.CSV, author=author)
        os.environ["GIT_COMMITTER_DATE"] = os.environ["GIT_AUTHOR_DATE"] = f"{date}T06:00:00+00:00"
        self.git("commit", "-q", "-m", "update", author=author)
        del os.environ["GIT_COMMITTER_DATE"], os.environ["GIT_AUTHOR_DATE"]

    def test_backfill_records_ci_commits_only(self):
        base = filler(10)
        self.commit_csv(csv_doc(), "Someone", "2026-03-01")
        self.commit_csv(csv_doc(*base), hotel_history.CI_AUTHOR, "2026-03-30")  # first load
        self.commit_csv(csv_doc(*base, row("Aman Venice")), hotel_history.CI_AUTHOR, "2026-04-02")
        self.commit_csv(csv_doc(*base, row("Aman Venice"), row("Manual Fix")), "Someone", "2026-04-03")
        hotel_history.backfill([self.CSV], "changes.json", "HEAD", hotel_history.CI_AUTHOR)
        log = hotel_history.load_log("changes.json")
        self.assertEqual(log["tracking_since"], {FHR: "2026-03-30"})
        self.assertEqual([(e["date"], e["change"], e["name"]) for e in log["events"]],
                         [("2026-04-02", "added", "Aman Venice")])

    def test_update_appends_working_tree_changes_once(self):
        base = filler(10)
        self.commit_csv(csv_doc(*base, row("Old Hotel")), hotel_history.CI_AUTHOR, "2026-03-30")
        hotel_history.write_log("changes.json", {**hotel_history.empty_log(), "tracking_since": {FHR: "2026-03-30"}})
        Path(self.CSV).write_text(csv_doc(*base, row("New Hotel", location="Lyon, France", lat="45.7600", lon="4.8300")),
                                  encoding="utf-8")
        self.assertEqual(hotel_history.update([self.CSV], "changes.json", "2026-11-26", "https://example.com/run"), 2)
        log = hotel_history.load_log("changes.json")
        self.assertEqual(sorted((e["change"], e["name"]) for e in log["events"]),
                         [("added", "New Hotel"), ("removed", "Old Hotel")])
        self.assertEqual(log["tracking_since"], {FHR: "2026-03-30"})
        # A rerun of the same night adds nothing and leaves the file alone.
        before = Path("changes.json").stat().st_mtime_ns
        self.assertEqual(hotel_history.update([self.CSV], "changes.json", "2026-11-26", "https://example.com/run"), 0)
        self.assertEqual(Path("changes.json").stat().st_mtime_ns, before)

    def test_update_starts_tracking_on_first_load(self):
        self.commit_csv(csv_doc(), "Someone", "2026-03-01")
        Path(self.CSV).write_text(csv_doc(*filler(10)), encoding="utf-8")
        self.assertEqual(hotel_history.update([self.CSV], "changes.json", "2026-11-26", ""), 0)
        log = hotel_history.load_log("changes.json")
        self.assertEqual(log, {"version": 1, "tracking_since": {FHR: "2026-11-26"}, "events": []})


if __name__ == "__main__":
    unittest.main()

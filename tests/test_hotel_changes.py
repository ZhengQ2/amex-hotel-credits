import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import hotel_changes
from hotel_changes import compare, report_section


HEADER = "hotel_name,hotel_url,hotel_location,group_label,brand_label,lat,lon,place_id,types,status\n"
FIRST = "Hotel A,https://example.com/a,Paris,FHR,,48.1,2.3,pid_a,establishment;lodging,PLACES\n"
SECOND = "Hotel B,https://example.com/b,London,FHR,,51.5,-0.1,pid_b,establishment;lodging,CACHED\n"


def csv_doc(*rows: str) -> str:
    return HEADER + "".join(r if r.endswith("\n") else r + "\n" for r in rows)


class HotelChangeTests(unittest.TestCase):
    def test_status_and_row_order_do_not_trigger_updates(self):
        old = csv_doc(FIRST, SECOND)
        new = csv_doc(SECOND, FIRST.replace("PLACES", "CACHED"))
        diff = compare(old, new)
        self.assertFalse(diff.sheet_changed)
        self.assertFalse(diff.has_notifiable_changes)
        self.assertEqual((diff.added, diff.removed, diff.renamed, diff.moved), ([], [], [], []))

    def test_url_only_change_updates_sheet_without_email(self):
        old = csv_doc(
            FIRST.replace("https://example.com/a", "https://www.americanexpress.com/property/Hotel-A")
        )
        new = old.replace(
            "americanexpress.com/property/",
            "americanexpress.com/en-us/travel/discover/property/",
        )
        diff = compare(old, new)
        self.assertTrue(diff.sheet_changed)
        self.assertFalse(diff.has_notifiable_changes)
        self.assertEqual((diff.added, diff.removed, diff.renamed, diff.moved), ([], [], [], []))

    def test_coordinate_change_triggers_email(self):
        old = csv_doc(FIRST)
        new = csv_doc(FIRST.replace("48.1,2.3", "48.2,2.3"))
        diff = compare(old, new)
        self.assertTrue(diff.sheet_changed)
        self.assertTrue(diff.has_notifiable_changes)
        self.assertEqual((diff.added, diff.removed, diff.renamed), ([], [], []))
        self.assertEqual([m.new.identity for m in diff.moved], [("Hotel A", "FHR", "Paris")])
        self.assertGreater(diff.moved[0].distance_m, 10_000.0)

    def test_small_coordinate_jitter_does_not_trigger_email(self):
        # ~73m shift that keeps lodging status is treated as benign pin jitter
        old = csv_doc(
            '"The Ritz-Carlton, Xi\'an",https://example.com/rc,"Xian, China",FINE HOTELS + RESORTS,The Ritz-Carlton,34.2232364,108.8985118,pid_1,establishment;lodging,CACHED'
        )
        new = csv_doc(
            '"The Ritz-Carlton, Xi\'an",https://example.com/rc,"Xi\'an, China",FINE HOTELS + RESORTS,The Ritz-Carlton,34.2237654,108.8980399,pid_1,establishment;lodging,PLACES'
        )
        diff = compare(old, new)
        self.assertFalse(diff.has_notifiable_changes)
        self.assertEqual(len(diff.silent_updates), 1)
        self.assertEqual(diff.moved, [])

    def test_small_shift_with_place_id_change_and_lost_lodging_is_reported(self):
        # Regression: a ~73m shift to a different place_id that loses "lodging"
        # (e.g. lodging -> parking lot) must still trigger a coordinate alert.
        old = csv_doc(
            '"The Ritz-Carlton, Xi\'an",https://example.com/rc,"Xian, China",FINE HOTELS + RESORTS,The Ritz-Carlton,34.2232364,108.8985118,pid_lodging,establishment;lodging;point_of_interest,CACHED'
        )
        new = csv_doc(
            '"The Ritz-Carlton, Xi\'an",https://example.com/rc,"Xi\'an, China",FINE HOTELS + RESORTS,The Ritz-Carlton,34.2237654,108.8980399,pid_parking,establishment;parking;point_of_interest,PLACES'
        )
        diff = compare(old, new)
        self.assertTrue(diff.has_notifiable_changes)
        self.assertEqual(len(diff.moved), 1)
        self.assertEqual(diff.moved[0].reason, "lost lodging type")
        report = report_section("cache/fhr_hotels_geocoded_google.csv", diff)
        self.assertIn("lost lodging type", report)

    def test_new_and_removed_hotels_trigger_email(self):
        diff = compare(csv_doc(FIRST), csv_doc(SECOND))
        self.assertTrue(diff.sheet_changed)
        self.assertTrue(diff.has_notifiable_changes)
        self.assertEqual([r.identity for r in diff.added], [("Hotel B", "FHR", "London")])
        self.assertEqual([r.identity for r in diff.removed], [("Hotel A", "FHR", "Paris")])
        self.assertEqual((diff.renamed, diff.moved), ([], []))

    def test_coordinate_formatting_is_not_a_change(self):
        old = csv_doc(FIRST)
        new = csv_doc(FIRST.replace("48.1,2.3", "48.1000,2.300"))
        diff = compare(old, new)
        self.assertTrue(diff.sheet_changed)
        self.assertFalse(diff.has_notifiable_changes)
        self.assertEqual((diff.added, diff.removed, diff.renamed, diff.moved), ([], [], [], []))

    def test_location_diacritics_and_granularity_do_not_produce_add_remove(self):
        # Regression test from GitHub Actions run 45447124482 / commit 2adac60
        old = csv_doc(
            'Akelarre - Relais & Châteaux,https://example.com/1,"San Sebastian, Spain",FINE HOTELS + RESORTS,Relais & Châteaux,43.3075,-2.0439,pid_ak,establishment;lodging,CACHED',
            '"Carmel Valley Ranch, in The Unbound Collection by Hyatt",https://example.com/2,"Carmel-By-The-Sea, California US",FINE HOTELS + RESORTS,,36.5220,-121.8058,pid_cvr,establishment;lodging,CACHED',
            'Four Seasons Resort Seychelles,https://example.com/3,"Mahe Island, Seychelles",FINE HOTELS + RESORTS,Four Seasons,-4.7495,55.4757,pid_fs,establishment;lodging,CACHED',
            '"Villa Sant\'Andrea, A Belmond Hotel, Taormina Mare",https://example.com/4,"Taormina Mare, Italy",FINE HOTELS + RESORTS,Belmond,37.8551,15.3005,pid_vsa,establishment;lodging,CACHED',
            '"ME Malta by Meliá",https://example.com/5,"St Julian\'s, Malta",FINE HOTELS + RESORTS,ME,35.9220,14.4898,pid_me,establishment;lodging,CACHED',
        )
        new = csv_doc(
            'Akelarre - Relais & Châteaux,https://example.com/1,"San Sebastián, Spain",FINE HOTELS + RESORTS,Relais & Châteaux,43.3075,-2.0439,pid_ak,establishment;lodging,CACHED',
            '"Carmel Valley Ranch, in The Unbound Collection by Hyatt",https://example.com/2,"Carmel, California US",FINE HOTELS + RESORTS,,36.5220,-121.8058,pid_cvr,establishment;lodging,CACHED',
            'Four Seasons Resort Seychelles,https://example.com/3,"Mahé, Seychelles",FINE HOTELS + RESORTS,Four Seasons,-4.7495,55.4757,pid_fs,establishment;lodging,CACHED',
            '"Villa Sant\'Andrea, A Belmond Hotel, Taormina Mare",https://example.com/4,"Taormina, Italy",FINE HOTELS + RESORTS,Belmond,37.8551,15.3005,pid_vsa,establishment;lodging,CACHED',
            '"ME Malta by Meliá",https://example.com/5,"St. Julian\'s, Malta",FINE HOTELS + RESORTS,ME,35.9220,14.4898,pid_me,establishment;lodging,CACHED',
        )
        diff = compare(old, new)
        self.assertTrue(diff.sheet_changed)
        self.assertFalse(diff.has_notifiable_changes)
        self.assertEqual((diff.added, diff.removed, diff.renamed, diff.moved), ([], [], [], []))
        self.assertEqual(len(diff.silent_updates), 5)

    def test_cosmetic_affiliation_tagline_rename_is_silenced(self):
        # Regression test from commit 35d79f1
        old = csv_doc(
            '45 Park Lane,https://example.com/45,"London, England",FINE HOTELS + RESORTS,,51.5064,-0.1503,pid_45,establishment;lodging,CACHED',
            'Carmel Valley Ranch,https://example.com/cvr,"Carmel, California US",FINE HOTELS + RESORTS,,36.5220,-121.8058,pid_cvr,establishment;lodging,CACHED',
            'Hoolei Villas at Grand Wailea,https://example.com/h,"Wailea, Hawaii US",FINE HOTELS + RESORTS,,20.6843,-156.4410,pid_h,establishment;lodging,CACHED',
        )
        new = csv_doc(
            '45 Park Lane - Dorchester Collection,https://example.com/45,"London, England",FINE HOTELS + RESORTS,,51.5064,-0.1503,pid_45,establishment;lodging,CACHED',
            '"Carmel Valley Ranch, in The Unbound Collection by Hyatt",https://example.com/cvr,"Carmel, California US",FINE HOTELS + RESORTS,,36.5220,-121.8058,pid_cvr,establishment;lodging,CACHED',
            'Ho\u2018olei Villas at Grand Wailea,https://example.com/h,"Wailea, Hawaii US",FINE HOTELS + RESORTS,,20.6843,-156.4410,pid_h,establishment;lodging,CACHED',
        )
        diff = compare(old, new)
        self.assertFalse(diff.has_notifiable_changes)
        self.assertEqual((diff.added, diff.removed, diff.renamed), ([], [], []))
        self.assertEqual(len(diff.silent_updates), 3)

    def test_substantive_rename_surfaces_in_renamed_not_add_remove(self):
        old = csv_doc(
            'Omni Austin Hotel Downtown,https://example.com/omni,"Austin, Texas US",THE HOTEL COLLECTION,Omni,30.2689,-97.7405,pid_omni,establishment;lodging,CACHED',
        )
        new = csv_doc(
            'Omni Austin Hotel,https://example.com/omni,"Austin, Texas US",THE HOTEL COLLECTION,Omni,30.2689,-97.7405,pid_omni,establishment;lodging,CACHED',
        )
        diff = compare(old, new)
        self.assertTrue(diff.has_notifiable_changes)
        self.assertEqual((diff.added, diff.removed), ([], []))
        self.assertEqual(len(diff.renamed), 1)
        report = report_section("cache/thc_hotels_geocoded_google.csv", diff)
        self.assertIn("~ Renamed: Omni Austin Hotel Downtown -> Omni Austin Hotel", report)

    def test_hilton_property_code_detects_rename(self):
        old = csv_doc(
            'Casa Marina Key West,https://www.hilton.com/en/hotels/eywcmqq-casa-marina-key-west/,,Curio Collection by Hilton,Curio Collection by Hilton,24.5477,-81.7901,pid_cm,establishment;lodging,CACHED',
        )
        new = csv_doc(
            '"Casa Marina Key West, Curio Collection",https://www.hilton.com/en/hotels/eywcmqq-casa-marina-key-west/,,Curio Collection by Hilton,Curio Collection by Hilton,24.5477,-81.7901,pid_cm,establishment;lodging,CACHED',
        )
        diff = compare(old, new)
        self.assertEqual((diff.added, diff.removed), ([], []))
        self.assertEqual(len(diff.renamed), 1)
        self.assertEqual(diff.renamed[0].stage, "hilton_code")

    def test_hilton_brand_change_with_same_property_code_is_reported(self):
        old = csv_doc(
            'Casa Marina Key West,https://www.hilton.com/en/hotels/eywcmqq-casa-marina-key-west/,,Curio Collection by Hilton,Curio Collection by Hilton,24.5477,-81.7901,pid_cm,establishment;lodging,CACHED',
        )
        new = csv_doc(
            'Casa Marina Key West,https://www.hilton.com/en/hotels/eywcmqq-casa-marina-key-west/,,LXR Hotels & Resorts,LXR Hotels & Resorts,24.5477,-81.7901,pid_cm,establishment;lodging,CACHED',
        )
        diff = compare(old, new)
        self.assertTrue(diff.has_notifiable_changes)
        self.assertEqual((diff.added, diff.removed, diff.silent_updates), ([], [], []))
        self.assertEqual(len(diff.renamed), 1)
        report = report_section("cache/hilton_hotels_geocoded_google.csv", diff)
        self.assertIn("Curio Collection by Hilton", report)
        self.assertIn("LXR Hotels & Resorts", report)

    def test_sibling_removal_when_other_sibling_stays_is_not_masked(self):
        ny = 'Four Seasons Hotel New York,https://example.com/ny,"New York, New York US",FINE HOTELS + RESORTS,Four Seasons,40.7623,-73.9713,pid_fs_shared,establishment;lodging,CACHED'
        ny_dt = 'Four Seasons Hotel New York Downtown,https://example.com/nydt,"New York, New York US",FINE HOTELS + RESORTS,Four Seasons,40.7623,-73.9713,pid_fs_shared,establishment;lodging,CACHED'
        diff = compare(csv_doc(ny, ny_dt), csv_doc(ny_dt))
        self.assertEqual([r.hotel_name for r in diff.removed], ["Four Seasons Hotel New York"])
        self.assertEqual((diff.added, diff.renamed), ([], []))

    def test_simultaneous_sibling_swap_is_never_silently_swallowed(self):
        pairs = [
            (
                'Four Seasons Hotel New York,https://example.com/1,"New York, New York US",FINE HOTELS + RESORTS,Four Seasons,40.7623,-73.9713,pid_fs1,establishment;lodging,CACHED',
                'Four Seasons Hotel New York Downtown,https://example.com/2,"New York, New York US",FINE HOTELS + RESORTS,Four Seasons,40.7126,-74.0091,pid_fs2,establishment;lodging,CACHED',
            ),
            (
                '"Zadun Los Cabos, a Ritz-Carlton Reserve",https://example.com/1,"San José del Cabo, Baja California Sur MX",FINE HOTELS + RESORTS,Ritz-Carlton,23.0630,-109.6562,pid_z1,establishment;lodging,CACHED',
                '"Zadun Los Cabos, a Ritz-Carlton Reserve Residence",https://example.com/2,"San José del Cabo, Baja California Sur MX",FINE HOTELS + RESORTS,Ritz-Carlton,23.0631,-109.6563,pid_z2,establishment;lodging,CACHED',
            ),
            (
                'Hyatt Zilara Cap Cana,https://example.com/1,"Punta Cana, Dominican Republic",THE HOTEL COLLECTION,Hyatt,18.4970,-68.3820,pid_hz1,establishment;lodging,CACHED',
                'Hyatt Ziva Cap Cana,https://example.com/2,"Punta Cana, Dominican Republic",THE HOTEL COLLECTION,Hyatt,18.4960,-68.3810,pid_hz2,establishment;lodging,CACHED',
            ),
            (
                'Hilton Dubai Jumeirah,https://example.com/1,,Hilton Hotels & Resorts,Hilton,25.0772,55.1342,pid_hd1,establishment;lodging,CACHED',
                'Hilton Dubai Palm Jumeirah,https://example.com/2,,Hilton Hotels & Resorts,Hilton,25.1095,55.1489,pid_hd2,establishment;lodging,CACHED',
            ),
            (
                '"Mangrove Beach Corendon Curacao All-Inclusive Resort, Curio by Hilton",https://example.com/1,"Willemstad, Curacao",THE HOTEL COLLECTION,,12.1110,-68.9480,pid_mb,establishment;lodging,CACHED',
                '"The Rif at Mangrove Beach Corendon Curacao All-Inclusive, Curio by Hilton",https://example.com/2,"Willemstad, Curacao",THE HOTEL COLLECTION,,12.1100,-68.9475,pid_rif,establishment;lodging,CACHED',
            ),
        ]
        for old_row, new_row in pairs:
            diff = compare(csv_doc(old_row), csv_doc(new_row))
            self.assertEqual(diff.silent_updates, [], f"Silently merged distinct siblings: {old_row} vs {new_row}")
            self.assertEqual(diff.renamed, [], f"Falsely renamed distinct siblings: {old_row} vs {new_row}")
            self.assertTrue(diff.has_notifiable_changes)
            self.assertEqual(len(diff.added), 1)
            self.assertEqual(len(diff.removed), 1)

    def test_different_hilton_property_codes_and_missing_locations_do_not_merge(self):
        # Different Hilton property codes (even with missing coords) must never merge.
        diff_codes = compare(
            csv_doc(
                'Hilton Lake Las Vegas Resort & Spa,https://www.hilton.com/en/hotels/laslkhf-hilton-lake-las-vegas/,,Hilton Hotels & Resorts,Hilton Hotels & Resorts,,,,establishment;lodging,CACHED'
            ),
            csv_doc(
                'Las Vegas Hilton at Resorts World,https://www.hilton.com/en/hotels/rslvhvh-las-vegas-hilton-at-resorts-world/,,Hilton Hotels & Resorts,Hilton Hotels & Resorts,,,,establishment;lodging,CACHED'
            ),
        )
        self.assertEqual(len(diff_codes.added), 1)
        self.assertEqual(len(diff_codes.removed), 1)
        self.assertEqual((diff_codes.renamed, diff_codes.silent_updates), ([], []))

        # Two Hilton Grand Vacations Club properties 3.5 km apart with empty hotel_location.
        diff_hgv = compare(
            csv_doc(
                'Hilton Grand Vacations Club SeaWorld Orlando,https://www.hilton.com/en/hotels/orlsggv-hgvc-seaworld/,,Hilton Grand Vacation Club,Hilton Grand Vacation Club,28.4070,-81.4660,pid_sw,establishment;lodging,CACHED'
            ),
            csv_doc(
                'Hilton Grand Vacations Club Tuscany Village Orlando,https://www.hilton.com/en/hotels/orlingv-hgvc-tuscany/,,Hilton Grand Vacation Club,Hilton Grand Vacation Club,28.3860,-81.4900,pid_tv,establishment;lodging,CACHED'
            ),
        )
        self.assertEqual(len(diff_hgv.added), 1)
        self.assertEqual(len(diff_hgv.removed), 1)
        self.assertEqual((diff_hgv.renamed, diff_hgv.silent_updates), ([], []))

    def test_nearby_unrelated_hotels_in_same_city_do_not_merge(self):
        pairs = [
            (
                'Aman New York,https://example.com/1,"New York, New York US",FINE HOTELS + RESORTS,Aman,40.7630,-73.9735,pid_aman,establishment;lodging,CACHED',
                'The Peninsula New York,https://example.com/2,"New York, New York US",FINE HOTELS + RESORTS,Peninsula,40.7618,-73.9752,pid_pen,establishment;lodging,CACHED',
            ),
            (
                'W Rome,https://example.com/1,"Rome, Italy",FINE HOTELS + RESORTS,W Hotels,41.9068,12.4875,pid_w,establishment;lodging,CACHED',
                '"Baglioni Hotel Regina, Rome",https://example.com/2,"Rome, Italy",FINE HOTELS + RESORTS,Baglioni,41.9072,12.4890,pid_bag,establishment;lodging,CACHED',
            ),
            (
                'Four Seasons Hotel Mexico City,https://example.com/1,"Mexico City, Mexico",FINE HOTELS + RESORTS,Four Seasons,19.4243,-99.1745,pid_fsmx,establishment;lodging,CACHED',
                '"The Ritz-Carlton, Mexico City",https://example.com/2,"Mexico City, Mexico",FINE HOTELS + RESORTS,Ritz-Carlton,19.4254,-99.1733,pid_rcmx,establishment;lodging,CACHED',
            ),
        ]
        for old_row, new_row in pairs:
            diff = compare(csv_doc(old_row), csv_doc(new_row))
            self.assertEqual(len(diff.added), 1)
            self.assertEqual(len(diff.removed), 1)
            self.assertEqual((diff.renamed, diff.silent_updates), ([], []))

    def test_same_name_in_different_cities_does_not_merge(self):
        # Different states
        diff_states = compare(
            csv_doc('The Grand Hotel,https://example.com/1,"Miami, Florida US",FHR,,25.7617,-80.1918,pid_mia,establishment;lodging,CACHED'),
            csv_doc('The Grand Hotel,https://example.com/2,"Seattle, Washington US",FHR,,47.6062,-122.3321,pid_sea,establishment;lodging,CACHED'),
        )
        self.assertEqual((len(diff_states.added), len(diff_states.removed)), (1, 1))

        # Same state, different cities (with missing coords AND with real coords + distinct place_ids)
        diff_same_state_nocoords = compare(
            csv_doc('The Grand Hotel,https://example.com/1,"Austin, Texas US",FHR,,,,,establishment;lodging,CACHED'),
            csv_doc('The Grand Hotel,https://example.com/2,"Houston, Texas US",FHR,,,,,establishment;lodging,CACHED'),
        )
        self.assertEqual((len(diff_same_state_nocoords.added), len(diff_same_state_nocoords.removed)), (1, 1))
        self.assertTrue(diff_same_state_nocoords.has_notifiable_changes)

        diff_same_state_coords = compare(
            csv_doc('The Grand Hotel,https://example.com/1,"Austin, Texas US",FHR,,30.2672,-97.7431,pid_aus,establishment;lodging,CACHED'),
            csv_doc('The Grand Hotel,https://example.com/2,"Houston, Texas US",FHR,,29.7604,-95.3698,pid_hou,establishment;lodging,CACHED'),
        )
        self.assertEqual((len(diff_same_state_coords.added), len(diff_same_state_coords.removed)), (1, 1))
        self.assertEqual(diff_same_state_coords.moved, [])

    def test_same_city_distinct_st_regis_resorts_do_not_merge(self):
        old = csv_doc(
            'The St. Regis Bal Harbour Resort,https://example.com/1,"Bal Harbour, Florida US",FINE HOTELS + RESORTS,St. Regis,25.8886,-80.1232,pid_same,establishment;lodging,CACHED'
        )
        new = csv_doc(
            'The St. Regis Longboat Key Resort,https://example.com/2,"Longboat Key, Florida US",FINE HOTELS + RESORTS,St. Regis,27.3621,-82.6177,pid_same,establishment;lodging,CACHED'
        )
        diff = compare(old, new)
        self.assertEqual(diff.silent_updates, [])
        self.assertTrue(diff.has_notifiable_changes)

    def test_report_preserves_rename_and_move_details_with_many_adds_and_removes(self):
        old_rows = [
            f'Old Hotel {i},https://example.com/o{i},"Paris, France",FHR,,48.{10 + i},2.30,pid_o{i},establishment;lodging,CACHED'
            for i in range(20)
        ] + [
            'Omni Austin Hotel Downtown,https://example.com/omni,"Austin, Texas US",FHR,Omni,30.2689,-97.7405,pid_omni,establishment;lodging,CACHED',
            'Moved Hotel,https://example.com/mv,"London, England",FHR,,51.5000,-0.1000,pid_mv,establishment;lodging,CACHED',
        ]
        new_rows = [
            f'New Hotel {i},https://example.com/n{i},"Rome, Italy",FHR,,41.{10 + i},12.50,pid_n{i},establishment;lodging,CACHED'
            for i in range(20)
        ] + [
            'Omni Austin Hotel,https://example.com/omni,"Austin, Texas US",FHR,Omni,30.2689,-97.7405,pid_omni,establishment;lodging,CACHED',
            'Moved Hotel,https://example.com/mv,"London, England",FHR,,51.6000,-0.1000,pid_mv,establishment;lodging,CACHED',
        ]
        diff = compare(csv_doc(*old_rows), csv_doc(*new_rows))
        report = report_section("cache/fhr_hotels_geocoded_google.csv", diff)
        self.assertIn("... and 5 more added", report)
        self.assertIn("... and 5 more removed", report)
        self.assertIn("~ Renamed: Omni Austin Hotel Downtown -> Omni Austin Hotel", report)
        self.assertIn("~ Moved (", report)

    def test_cli_main_writes_github_output_and_report(self):
        old = csv_doc(
            'Akelarre - Relais & Châteaux,https://example.com/1,"San Sebastian, Spain",FINE HOTELS + RESORTS,Relais & Châteaux,43.3075,-2.0439,pid_ak,establishment;lodging,CACHED',
        )
        new = csv_doc(
            'Akelarre - Relais & Châteaux,https://example.com/1,"San Sebastián, Spain",FINE HOTELS + RESORTS,Relais & Châteaux,43.3075,-2.0439,pid_ak,establishment;lodging,CACHED',
        )
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "fhr.csv"
            report_path = Path(tmp) / "report.txt"
            gh_out = Path(tmp) / "gh_output.txt"
            csv_path.write_text(new, encoding="utf-8")

            argv = ["hotel_changes.py", "--csv", f"fhr_out:{csv_path}", "--report", str(report_path)]
            with mock.patch("sys.argv", argv), mock.patch.dict(
                os.environ, {"GITHUB_OUTPUT": str(gh_out)}
            ), mock.patch.object(hotel_changes, "committed", return_value=old):
                hotel_changes.main()

            self.assertEqual(gh_out.read_text(encoding="utf-8"), "fhr_out=true\nnotify=false\n")
            self.assertEqual(
                report_path.read_text(encoding="utf-8"),
                "No hotel list or coordinate changes.\n",
            )


if __name__ == "__main__":
    unittest.main()

import unittest

from hotel_changes import compare


HEADER = "hotel_name,hotel_url,hotel_location,group_label,lat,lon,status\n"
FIRST = "Hotel A,https://example.com/a,Paris,FHR,48.1,2.3,PLACES\n"
SECOND = "Hotel B,https://example.com/b,London,FHR,51.5,-0.1,CACHED\n"


class HotelChangeTests(unittest.TestCase):
    def test_status_and_row_order_do_not_trigger_updates(self):
        old = HEADER + FIRST + SECOND
        new = HEADER + SECOND + FIRST.replace("PLACES", "CACHED")
        sheet_changed, added, removed, moved, _, _ = compare(old, new)
        self.assertFalse(sheet_changed)
        self.assertEqual((added, removed, moved), ([], [], []))

    def test_url_only_change_updates_sheet_without_email(self):
        old = HEADER + FIRST.replace(
            "https://example.com/a", "https://www.americanexpress.com/property/Hotel-A"
        )
        new = old.replace(
            "americanexpress.com/property/", "americanexpress.com/en-us/travel/discover/property/"
        )
        sheet_changed, added, removed, moved, _, _ = compare(old, new)
        self.assertTrue(sheet_changed)
        self.assertEqual((added, removed, moved), ([], [], []))

    def test_coordinate_change_triggers_email(self):
        old = HEADER + FIRST
        new = HEADER + FIRST.replace("48.1,2.3", "48.2,2.3")
        sheet_changed, added, removed, moved, _, _ = compare(old, new)
        self.assertTrue(sheet_changed)
        self.assertEqual((added, removed), ([], []))
        self.assertEqual(moved, [("Hotel A", "FHR", "Paris")])

    def test_new_and_removed_hotels_trigger_email(self):
        sheet_changed, added, removed, moved, _, _ = compare(
            HEADER + FIRST, HEADER + SECOND
        )
        self.assertTrue(sheet_changed)
        self.assertEqual(added, [("Hotel B", "FHR", "London")])
        self.assertEqual(removed, [("Hotel A", "FHR", "Paris")])
        self.assertEqual(moved, [])

    def test_coordinate_formatting_is_not_a_change(self):
        old = HEADER + FIRST
        new = HEADER + FIRST.replace("48.1,2.3", "48.1000,2.300")
        sheet_changed, added, removed, moved, _, _ = compare(old, new)
        self.assertTrue(sheet_changed)
        self.assertEqual((added, removed, moved), ([], [], []))


if __name__ == "__main__":
    unittest.main()

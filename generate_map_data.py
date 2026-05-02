#!/usr/bin/env python3
"""Read geocoded CSVs and write docs/hotels.json for the interactive map."""

import csv
import json
import os

SOURCES = [
    ("cache/hilton_hotels_geocoded_google.csv", "Hilton Portfolio"),
    ("cache/fhr_hotels_geocoded_google.csv", "Fine Hotels + Resorts"),
    ("cache/thc_hotels_geocoded_google.csv", "The Hotel Collection"),
]

OUTPUT = "docs/hotels.json"


def load_csv(path, program):
    hotels = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                lat = float(row["lat"])
                lon = float(row["lon"])
            except (ValueError, KeyError):
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            hotels.append({
                "n": row["hotel_name"].strip(),
                "u": row.get("hotel_url", "").strip(),
                "a": (row.get("formatted_address") or row.get("hotel_location", "")).strip(),
                "g": row.get("group_label", "").strip(),
                "b": row.get("brand_label", "").strip(),
                "p": program,
                "lat": lat,
                "lon": lon,
            })
    return hotels


def main():
    os.makedirs("docs", exist_ok=True)
    all_hotels = []
    for path, program in SOURCES:
        if not os.path.exists(path):
            print(f"Warning: {path} not found, skipping")
            continue
        hotels = load_csv(path, program)
        all_hotels.extend(hotels)
        print(f"  {len(hotels):>5} hotels from {path}")

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(all_hotels, f, separators=(",", ":"))
    print(f"\nWrote {len(all_hotels)} total hotels → {OUTPUT}")


if __name__ == "__main__":
    main()

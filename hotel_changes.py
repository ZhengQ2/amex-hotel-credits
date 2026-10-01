#!/usr/bin/env python3
"""Decide whether nightly geocoded CSV changes need a sheet update or email."""

import argparse
import csv
import io
import os
import subprocess
from collections import Counter
from decimal import Decimal
from pathlib import Path


def read_rows(text):
    reader = csv.DictReader(io.StringIO(text))
    if not text.strip():
        return []
    required = {"hotel_name", "lat", "lon"}
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        raise ValueError(f"Geocoded CSV is missing columns: {sorted(required - set(reader.fieldnames or []))}")
    return list(reader)


def sheet_rows(rows):
    """Ignore output order and the per-run cache/Places status."""
    return Counter(
        tuple(sorted((key, value) for key, value in row.items() if key != "status"))
        for row in rows
    )


def hotel_index(rows):
    hotels = {}
    for row in rows:
        identity = tuple((row.get(key) or "").strip() for key in
                         ("hotel_name", "group_label", "hotel_location"))
        if not identity[0]:
            raise ValueError("Geocoded CSV has a row without a hotel name")
        if identity in hotels:
            raise ValueError(f"Duplicate hotel identity in geocoded CSV: {identity}")
        hotels[identity] = tuple(
            Decimal(value.strip()) if (value := row.get(key) or "").strip() else None
            for key in ("lat", "lon")
        )
    return hotels


def compare(old_text, new_text):
    old_rows, new_rows = read_rows(old_text), read_rows(new_text)
    old, new = hotel_index(old_rows), hotel_index(new_rows)
    added = sorted(new.keys() - old.keys())
    removed = sorted(old.keys() - new.keys())
    moved = sorted(key for key in old.keys() & new.keys() if old[key] != new[key])
    return sheet_rows(old_rows) != sheet_rows(new_rows), added, removed, moved, old, new


def committed(path):
    result = subprocess.run(["git", "show", f"HEAD:{path}"], capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout
    existing = subprocess.run(
        ["git", "ls-tree", "--name-only", "HEAD", "--", path],
        capture_output=True, text=True, check=True,
    )
    if not existing.stdout.strip():
        return ""
    raise RuntimeError(f"Could not read committed CSV {path}: {result.stderr}")


def describe(identity):
    name, group, location = identity
    details = ", ".join(part for part in (location, group) if part)
    return f"{name} ({details})" if details else name


def report_section(label, added, removed, moved, old, new):
    lines = [f"{label}: {len(added)} added, {len(removed)} removed, "
             f"{len(moved)} coordinate changes"]
    details = (
        [f"  + {describe(key)}" for key in added]
        + [f"  - {describe(key)}" for key in removed]
        + [f"  ~ {describe(key)}: {old[key][0]}, {old[key][1]} -> "
           f"{new[key][0]}, {new[key][1]}" for key in moved]
    )
    lines.extend(details[:25])
    if len(details) > 25:
        lines.append(f"  ... and {len(details) - 25} more; see attached CSVs")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", action="append", required=True, metavar="OUTPUT:PATH")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()

    outputs = {}
    sections = []
    notify = False
    for spec in args.csv:
        key, path = spec.split(":", 1)
        current = Path(path).read_text(encoding="utf-8")
        if not current.strip():
            raise ValueError(f"Geocoded CSV is empty: {path}")
        sheet_changed, added, removed, moved, old, new = compare(committed(path), current)
        outputs[key] = "true" if sheet_changed else "false"
        if added or removed or moved:
            notify = True
            sections.append(report_section(path, added, removed, moved, old, new))

    outputs["notify"] = "true" if notify else "false"
    Path(args.report).write_text(
        "Hotel list and coordinate changes\n\n" + "\n\n".join(sections)
        if notify else "No hotel list or coordinate changes.\n",
        encoding="utf-8",
    )
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        for key, value in outputs.items():
            output.write(f"{key}={value}\n")
    print(f"Sheet changes and email decision: {outputs}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Restore only known JSON files from a price-state GitHub Actions artifact."""

import argparse
import json
from pathlib import Path
import sys
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fetch_hotel_prices import atomic_json  # noqa: E402


def restore(archive, directory):
    allowed = {"usage.json", "mappings.json", "estimates.json", "report.json"}
    restored = {}
    with ZipFile(archive) as source:
        for item in source.infolist():
            if item.filename not in allowed:
                continue
            if item.file_size > 30_000_000:
                raise ValueError("Price state file exceeds the size limit")
            value = json.loads(source.read(item))
            if not isinstance(value, dict) or item.filename in restored:
                raise ValueError("Invalid or duplicate state file")
            restored[item.filename] = value
    usage = restored.get("usage.json", {})
    if usage.get("version") != 1 or not isinstance(usage.get("months"), dict):
        raise ValueError("Artifact is missing a valid usage ledger")
    for name, value in restored.items():
        atomic_json(Path(directory) / name, value)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive")
    parser.add_argument("--directory", default=".price-cache")
    args = parser.parse_args()
    restore(args.archive, args.directory)

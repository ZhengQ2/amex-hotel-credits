#!/usr/bin/env python3
"""Keep docs/changes.json: the dated log of hotels that joined or left each program.

`update` runs in the nightly workflows just before they commit. It compares each
geocoded CSV in the working tree with its committed version and appends what
changed. `backfill` rebuilds the log from the CI commits already in git history.

Both use hotel_changes.compare, the matcher behind the nightly email, and then
clean the result up for readers:

- A night that adds or drops far too many hotels (a scraper outage or a reload)
  is left out.
- An added and a removed hotel on the same night can be one hotel. Their names
  must agree: one name holds all of the other's words, and they are not told
  apart by a sibling word (Zilara/Ziva, Residences, Downtown), by different
  Hilton property codes, or by an "X at Y" name for a property inside another.
  Then they are:
  - renamed, when Google gives both the same place in one program;
  - moved between programs (THC -> FHR) at the same place or spot, kept as a
    removal and an addition that point at each other;
  - otherwise, in one program and town, kept as an addition and a removal
    flagged as a possible rename: the geocoder can put a hotel's old and new
    names kilometers apart, and only a shared place is sure enough to file the
    pair under renames, which the page hides by default.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from cache_utils import (
    DISCRIMINATOR_TOKENS,
    align_compound_tokens,
    core_name_tokens,
    locations_compatible,
    normalize_name,
)
from generate_map_data import SOURCES
from hotel_changes import HotelRecord, _haversine_m, _hilton_codes_conflict, committed, compare, read_rows

CHANGES_PATH = "docs/changes.json"
LOG_VERSION = 1
PROGRAMS: Dict[str, str] = dict(SOURCES)  # geocoded CSV path -> program name on the map
PROGRAM_ORDER = {program: i for i, (_, program) in enumerate(SOURCES)}
CHANGE_ORDER = {"added": 0, "removed": 1, "renamed": 2}
CI_AUTHOR = "github-actions[bot]"

# A night that adds or drops more hotels than this did not change the program:
# the scraper lost most of the list, or a reload rebuilt it.
BULK_MIN = 50
BULK_FRACTION = 0.25

# A hotel removed from one program and added to another this close, with
# agreeing names, moved between programs.
SAME_SPOT_M = 300.0
# Hilton rows have no scraped town, so "same town" falls back to this distance.
SAME_TOWN_M = 25_000.0

Rows = Dict[Tuple[str, str, str], Dict[str, str]]


def _fold_plural(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


# Words that tell sibling properties apart, in singular form like the name words.
SIBLING_WORDS = frozenset(_fold_plural(token) for token in DISCRIMINATOR_TOKENS)


@dataclass
class ProgramDiff:
    """One program's CSV before and after a run."""

    program: str
    old_text: str
    new_text: str


@dataclass
class _Side:
    program: str
    record: HotelRecord
    row: Dict[str, str]

    @property
    def place_words(self) -> Set[str]:
        place = self.record.hotel_location or (self.row.get("formatted_address") or "")
        return set(normalize_name(place).split())


def _rows_by_identity(text: str) -> Rows:
    rows: Rows = {}
    for row in read_rows(text):
        identity = tuple((row.get(key) or "").strip() for key in ("hotel_name", "group_label", "hotel_location"))
        rows[identity] = row
    return rows


def _name_words(old: _Side, new: _Side) -> Tuple[Set[str], Set[str]]:
    """Each name's identity words, without the town both are in: 'atlas' in 'The Atlas Hotel Boston'."""
    seq_a, seq_b = (
        [_fold_plural(word) for word in core_name_tokens(side.record.hotel_name)] for side in (old, new)
    )
    a, b = align_compound_tokens(seq_a, seq_b)
    shared_place = old.place_words & new.place_words
    distinctive = (a - shared_place, b - shared_place)
    # A name that is only its town ("The Beverly Hills Hotel") keeps the town.
    return distinctive if all(distinctive) else (a, b)


def _inside_other(old_name: str, new_name: str) -> bool:
    """'The Rif at Mangrove Beach' is a property inside 'Mangrove Beach', not its new name."""
    for inner, outer in ((new_name, old_name), (old_name, new_name)):
        head, at, tail = normalize_name(inner).rpartition(" at ")
        if at and core_name_tokens(head) and set(core_name_tokens(outer)) <= set(core_name_tokens(tail)):
            return True
    return False


def _pairing(old: _Side, new: _Side) -> Optional[Tuple[float, str]]:
    """Score a removed and an added hotel as one hotel: (score, 'same' or 'possible'), or None."""
    o, n = old.record, new.record
    a, b = _name_words(old, new)
    # Names that each have a word the other lacks are two hotels, and so are
    # siblings the email's matcher keeps apart.
    if not (a & b) or not (a <= b or b <= a):
        return None
    if (a ^ b) & SIBLING_WORDS or _hilton_codes_conflict(o, n) or _inside_other(o.hotel_name, n.hotel_name):
        return None
    distance = _haversine_m(o.float_coords, n.float_coords) if o.float_coords and n.float_coords else None
    tiebreak = len(a & b) / len(a | b) - (distance or 0.0) / 1e8
    same_place = bool(o.place_id and o.place_id == n.place_id)
    if old.program != new.program:
        if same_place or (distance is not None and distance <= SAME_SPOT_M):
            return 20.0 + tiebreak, "same"
        return None
    if same_place:
        return 30.0 + tiebreak, "same"
    if o.hotel_location or n.hotel_location:
        if not locations_compatible(o.hotel_location, n.hotel_location):
            return None
    elif distance is None or distance > SAME_TOWN_M:
        return None
    return 10.0 + tiebreak, "possible"


def _is_bulk(old_count: int, added: int, removed: int) -> bool:
    limit = max(BULK_MIN, BULK_FRACTION * old_count)
    return added > limit or removed > limit


def _display_brand(record: HotelRecord, program: str) -> str:
    brand = record.brand_label or record.group_label
    return "" if brand.casefold() == program.casefold() else brand


def _clean_url(url: str) -> str:
    """Drop the Amex listing's click-tracking parameter; the link goes to the same page."""
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "intlink"]
    return urlunsplit(parts._replace(query=urlencode(query)))


def _entry(change: str, side: _Side, date: str, source: str) -> Dict:
    record, row = side.record, side.row
    entry = {
        "date": date,
        "program": side.program,
        "change": change,
        "name": record.hotel_name,
        "brand": _display_brand(record, side.program),
        "location": record.hotel_location,
        "address": (row.get("formatted_address") or "").strip(),
        "url": _clean_url(record.hotel_url),
        "place_id": record.place_id,
        "lat": float(record.lat) if record.lat is not None else None,
        "lon": float(record.lon) if record.lon is not None else None,
        "source": source,
    }
    return {key: value for key, value in entry.items() if value not in ("", None)}


def run_events(diffs: Sequence[ProgramDiff], date: str, source: str = "") -> List[Dict]:
    """Turn one run's CSV diffs, across programs, into change-log events."""
    added: List[_Side] = []
    removed: List[_Side] = []
    renamed: List[Tuple[_Side, _Side]] = []

    for diff in diffs:
        if not read_rows(diff.old_text):
            print(f"{diff.program}: no earlier list to compare with; recording none of it as new")
            continue
        result = compare(diff.old_text, diff.new_text)
        old_rows, new_rows = _rows_by_identity(diff.old_text), _rows_by_identity(diff.new_text)
        if _is_bulk(len(old_rows), len(result.added), len(result.removed)):
            print(f"{diff.program}: skipped {len(result.added)} added and {len(result.removed)} "
                  f"removed hotels as a bulk change (scraper outage or reload), not program news")
            continue

        def side(record: HotelRecord, rows: Rows) -> _Side:
            return _Side(diff.program, record, rows.get(record.identity, {}))

        added.extend(side(r, new_rows) for r in result.added)
        removed.extend(side(r, old_rows) for r in result.removed)
        renamed.extend((side(m.old, old_rows), side(m.new, new_rows)) for m in result.renamed)

    # Pair each removed hotel with at most one added hotel, surest pairs first.
    candidates = sorted(
        ((pairing[0], i, j, pairing[1]) for i, old in enumerate(removed) for j, new in enumerate(added)
         if (pairing := _pairing(old, new)) is not None),
        reverse=True,
    )
    paired_old: Dict[int, int] = {}
    paired_new: Set[int] = set()
    events: List[Dict] = []
    for _, i, j, kind in candidates:
        if i in paired_old or j in paired_new:
            continue
        paired_old[i] = j
        paired_new.add(j)
        old, new = removed[i], added[j]
        if kind == "possible":
            events.append({**_entry("removed", old, date, source), "maybe_renamed_to": new.record.hotel_name})
            events.append({**_entry("added", new, date, source), "maybe_renamed_from": old.record.hotel_name})
        elif old.program == new.program:
            renamed.append((old, new))
        else:
            # Moved between programs (THC -> FHR): still a removal and an addition.
            events.append({**_entry("removed", old, date, source), "to_program": new.program})
            events.append({**_entry("added", new, date, source), "from_program": old.program})
    events.extend(_entry("added", new, date, source) for j, new in enumerate(added) if j not in paired_new)
    events.extend(_entry("removed", old, date, source) for i, old in enumerate(removed) if i not in paired_old)
    for old, new in renamed:
        event = _entry("renamed", new, date, source)
        event["previous_name"] = old.record.hotel_name
        previous_brand = _display_brand(old.record, old.program)
        if previous_brand != event.get("brand", ""):
            event["previous_brand"] = previous_brand
        events.append(event)
    return events


def _event_key(event: Dict) -> Tuple:
    return tuple(event.get(key, "") for key in ("date", "program", "change", "name", "location", "brand"))


def _sort_key(event: Dict) -> Tuple:
    # Newest day first; within a day, map order, then added/removed/renamed, then name.
    return (
        -int(event["date"].replace("-", "")),
        PROGRAM_ORDER.get(event["program"], len(PROGRAM_ORDER)),
        CHANGE_ORDER.get(event["change"], len(CHANGE_ORDER)),
        event["name"].casefold(),
        event.get("location", ""),
    )


def empty_log() -> Dict:
    return {"version": LOG_VERSION, "tracking_since": {}, "events": []}


def load_log(path: str) -> Dict:
    if not os.path.exists(path):
        return empty_log()
    with open(path, encoding="utf-8") as f:
        log = json.load(f)
    if log.get("version") != LOG_VERSION or not isinstance(log.get("events"), list):
        raise ValueError(f"{path} is not a version {LOG_VERSION} change log")
    log.setdefault("tracking_since", {})
    return log


def merge_events(log: Dict, events: Iterable[Dict]) -> int:
    """Add events not already in the log; return how many were new."""
    seen = {_event_key(event) for event in log["events"]}
    added = 0
    for event in events:
        key = _event_key(event)
        if key not in seen:
            seen.add(key)
            log["events"].append(event)
            added += 1
    log["events"].sort(key=_sort_key)
    return added


def write_log(path: str, log: Dict) -> None:
    """One event per line, so each nightly commit's diff is readable."""
    header = {key: value for key, value in log.items() if key != "events"}
    header["tracking_since"] = dict(sorted(
        header.get("tracking_since", {}).items(), key=lambda item: PROGRAM_ORDER.get(item[0], 99)))
    lines = [json.dumps(event, ensure_ascii=False, separators=(",", ":")) for event in log["events"]]
    body = ",\n".join(lines)
    text = json.dumps(header, ensure_ascii=False, separators=(",", ":"))[:-1]
    text += ',"events":[\n' + body + "\n]}\n" if lines else ',"events":[]}\n'
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text, encoding="utf-8")


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def _show(rev: str, path: str) -> Optional[str]:
    result = _git("show", f"{rev}:{path}")
    return result.stdout if result.returncode == 0 else None


def repo_url() -> str:
    if os.environ.get("GITHUB_REPOSITORY"):
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        return f"{server}/{os.environ['GITHUB_REPOSITORY']}"
    remote = _git("remote", "get-url", "origin").stdout.strip()
    match = re.match(r"(?:https://|git@)github\.com[/:](.+?)(?:\.git)?$", remote)
    return f"https://github.com/{match.group(1)}" if match else ""


def run_url() -> str:
    if os.environ.get("GITHUB_RUN_ID") and repo_url():
        return f"{repo_url()}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    return ""


def _has_rows(text: Optional[str]) -> bool:
    try:
        return bool(text and read_rows(text))
    except ValueError:
        return False


def update(paths: Sequence[str], log_path: str, date: str, source: str) -> int:
    log = load_log(log_path)
    before = json.dumps(log["tracking_since"], sort_keys=True)
    diffs = []
    for path in paths:
        program = PROGRAMS[path]
        new_text = Path(path).read_text(encoding="utf-8")
        if not _has_rows(new_text):
            raise ValueError(f"Geocoded CSV is empty: {path}")
        old_text = committed(path)
        if _has_rows(old_text):
            log["tracking_since"].setdefault(program, date)
        else:
            log["tracking_since"][program] = date  # a first load starts the record
        diffs.append(ProgramDiff(program, old_text, new_text))
    new = merge_events(log, run_events(diffs, date, source))
    if new or before != json.dumps(log["tracking_since"], sort_keys=True) or not os.path.exists(log_path):
        write_log(log_path, log)
    print(f"Recorded {new} hotel change(s) in {log_path}")
    return new


def backfill(paths: Sequence[str], log_path: str, rev: str, author: str) -> int:
    """Rebuild the log from every commit by the CI author that touched the CSVs."""
    base = repo_url()
    fmt = "--format=%H%x09%cI%x09%an"
    order = _git("log", "--first-parent", "--reverse", fmt, rev, "--", *paths).stdout.splitlines()
    log = empty_log()
    events: List[Dict] = []
    for line in order:
        sha, when, name = line.split("\t")
        date = datetime.fromisoformat(when).astimezone(timezone.utc).date().isoformat()
        touched = set(_git("diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha,
                           "--", *paths).stdout.split())
        diffs = []
        for path in paths:
            if path not in touched:
                continue
            program = PROGRAMS[path]
            old_text, new_text = _show(f"{sha}^", path), _show(sha, path)
            if not _has_rows(new_text):
                continue
            if not _has_rows(old_text):
                log["tracking_since"][program] = date  # a first load starts the record
            elif name == author:  # people's fixes to the data are not program news
                diffs.append(ProgramDiff(program, old_text, new_text))
        if diffs:
            events.extend(run_events(diffs, date, f"{base}/commit/{sha}" if base else ""))
    merge_events(log, events)
    write_log(log_path, log)
    print(f"Wrote {len(log['events'])} hotel change(s) to {log_path}")
    return len(log["events"])


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("update", "backfill"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--csv", action="append", choices=list(PROGRAMS), metavar="PATH",
                         help="geocoded CSV to track (repeatable; default: all of them)")
        cmd.add_argument("--output", default=CHANGES_PATH)
    sub.choices["update"].add_argument("--date", help="UTC date to record (default: today)")
    sub.choices["update"].add_argument("--source", help="link for these changes (default: this Actions run)")
    sub.choices["backfill"].add_argument("--rev", default="HEAD")
    sub.choices["backfill"].add_argument("--author", default=CI_AUTHOR)
    args = parser.parse_args(argv)

    paths = args.csv or list(PROGRAMS)
    if args.command == "update":
        date = args.date or datetime.now(timezone.utc).date().isoformat()
        update(paths, args.output, date, args.source if args.source is not None else run_url())
    else:
        backfill(paths, args.output, args.rev, args.author)


if __name__ == "__main__":
    sys.exit(main())

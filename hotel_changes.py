#!/usr/bin/env python3
"""Decide whether nightly geocoded CSV changes need a sheet update or email.

Uses cascading 1-to-1 entity resolution so minor metadata changes (diacritics,
punctuation, locality granularity, chain/collection taglines) do not produce
paired false-positive remove+add diffs, while keeping false negatives near zero
by locking exact matches first, guarding sibling discriminators, conflicting
property codes, and distinct Google place_ids, and surfacing substantive
renames/brand changes in a dedicated `~ Renamed` section.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
from decimal import Decimal
import io
import math
import os
from pathlib import Path
import re
import subprocess
from typing import Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from cache_utils import (
    DISCRIMINATOR_TOKENS,
    align_compound_tokens,
    core_name_tokens,
    is_lodging_type,
    locations_compatible,
    normalize_location,
    normalize_name,
)

# Coordinate shifts smaller than this distance (e.g. entrance vs building pin
# jitter) are ignored unless the place_id also changed and lost lodging status;
# larger shifts and None <-> valid transitions are always reported.
DEFAULT_MOVE_THRESHOLD_M = 250.0
MAX_DETAILS_PER_CATEGORY = 15

_HILTON_CODE_RE = re.compile(r"/hotels/([a-z0-9]{7})-", re.IGNORECASE)


@dataclass(frozen=True)
class HotelRecord:
    hotel_name: str
    group_label: str
    hotel_location: str
    brand_label: str
    hotel_url: str
    place_id: str
    lat: Optional[Decimal]
    lon: Optional[Decimal]
    types: FrozenSet[str] = frozenset()

    @property
    def identity(self) -> Tuple[str, str, str]:
        return (self.hotel_name, self.group_label, self.hotel_location)

    @property
    def coords(self) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        return (self.lat, self.lon)

    @property
    def float_coords(self) -> Optional[Tuple[float, float]]:
        if self.lat is None or self.lon is None:
            return None
        return (float(self.lat), float(self.lon))


@dataclass(frozen=True)
class HotelMatch:
    old: HotelRecord
    new: HotelRecord
    stage: str


@dataclass(frozen=True)
class MovedHotel:
    old: HotelRecord
    new: HotelRecord
    distance_m: Optional[float]
    reason: str = ""


@dataclass(frozen=True)
class DiffResult:
    sheet_changed: bool
    added: List[HotelRecord]
    removed: List[HotelRecord]
    renamed: List[HotelMatch]
    moved: List[MovedHotel]
    silent_updates: List[HotelMatch]

    @property
    def has_notifiable_changes(self) -> bool:
        return bool(self.added or self.removed or self.renamed or self.moved)


def read_rows(text: str) -> List[Dict[str, str]]:
    if not text.strip():
        return []
    reader = csv.DictReader(io.StringIO(text))
    required = {"hotel_name", "lat", "lon"}
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        missing = sorted(required - set(reader.fieldnames or []))
        raise ValueError(f"Geocoded CSV is missing columns: {missing}")
    return list(reader)


def sheet_rows(rows: Sequence[Dict[str, str]]) -> Counter:
    """Ignore output order and the per-run cache/Places status."""
    return Counter(
        tuple(sorted((key, value) for key, value in row.items() if key != "status"))
        for row in rows
    )


def _parse_types(raw: str) -> FrozenSet[str]:
    if not raw:
        return frozenset()
    return frozenset(t.strip().lower() for t in raw.split(";") if t.strip())


def parse_records(rows: Sequence[Dict[str, str]]) -> List[HotelRecord]:
    """Validate and parse CSV rows into HotelRecords, rejecting duplicate raw identities."""
    records: List[HotelRecord] = []
    seen: Set[Tuple[str, str, str]] = set()
    for row in rows:
        name = (row.get("hotel_name") or "").strip()
        if not name:
            raise ValueError("Geocoded CSV has a row without a hotel name")
        group = (row.get("group_label") or "").strip()
        location = (row.get("hotel_location") or "").strip()
        identity = (name, group, location)
        if identity in seen:
            raise ValueError(f"Duplicate hotel identity in geocoded CSV: {identity}")
        seen.add(identity)

        lat_str = (row.get("lat") or "").strip()
        lon_str = (row.get("lon") or "").strip()
        records.append(
            HotelRecord(
                hotel_name=name,
                group_label=group,
                hotel_location=location,
                brand_label=(row.get("brand_label") or "").strip(),
                hotel_url=(row.get("hotel_url") or "").strip(),
                place_id=(row.get("place_id") or "").strip(),
                lat=Decimal(lat_str) if lat_str else None,
                lon=Decimal(lon_str) if lon_str else None,
                types=_parse_types(row.get("types") or ""),
            )
        )
    return records


def _haversine_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    radius_m = 6_371_000.0
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    hav = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2.0 * radius_m * math.asin(math.sqrt(min(1.0, hav)))


def _distance_between(old: HotelRecord, new: HotelRecord) -> Optional[float]:
    oc, nc = old.float_coords, new.float_coords
    if oc is None or nc is None:
        return None
    return _haversine_m(oc, nc)


def _hilton_property_code(url: str) -> str:
    match = _HILTON_CODE_RE.search(url or "")
    return match.group(1).lower() if match else ""


def _hilton_codes_conflict(old: HotelRecord, new: HotelRecord) -> bool:
    o_code = _hilton_property_code(old.hotel_url)
    n_code = _hilton_property_code(new.hotel_url)
    return bool(o_code and n_code and o_code != n_code)


def _lost_lodging_place(old: HotelRecord, new: HotelRecord) -> bool:
    """True when place_id changed and the geocoded place degraded out of lodging."""
    if not (old.place_id and new.place_id and old.place_id != new.place_id):
        return False
    if not (old.types and new.types):
        return False
    if "lodging" in old.types and "lodging" not in new.types:
        return True
    return is_lodging_type(old.types) and not is_lodging_type(new.types)


def _build_idf(records: Sequence[HotelRecord]) -> Dict[str, float]:
    doc_freq: Counter = Counter()
    n_docs = max(1, len(records))
    for rec in records:
        for tok in set(core_name_tokens(rec.hotel_name)):
            doc_freq[tok] += 1
    return {
        tok: math.log((n_docs + 1.0) / (count + 1.0)) + 1.0
        for tok, count in doc_freq.items()
    }


@dataclass(frozen=True)
class _PreparedRecord:
    record: HotelRecord
    norm_group: str
    norm_brand: str
    hilton_code: str
    city: str
    country: str
    toks_seq: Tuple[str, ...]
    joined_core: str
    loc_tokens: FrozenSet[str]


def _prepare(rec: HotelRecord) -> _PreparedRecord:
    city, country = normalize_location(rec.hotel_location)
    toks = tuple(core_name_tokens(rec.hotel_name))
    loc_toks = frozenset(normalize_name(rec.hotel_location).split())
    return _PreparedRecord(
        record=rec,
        norm_group=normalize_name(rec.group_label),
        norm_brand=normalize_name(rec.brand_label),
        hilton_code=_hilton_property_code(rec.hotel_url),
        city=city,
        country=country,
        toks_seq=toks,
        joined_core="".join(toks),
        loc_tokens=loc_toks,
    )


def _name_similarity(
    op: _PreparedRecord,
    np: _PreparedRecord,
    idf: Dict[str, float],
) -> Tuple[float, bool, Set[str], bool]:
    """Compare identity tokens after removing shared location words.

    Returns (idf_f1, exact_core, extra_discriminators, mutually_exclusive).
    Subtracting only the *intersection* of location tokens avoids giving false
    overlap credit for a shared city ('Aman New York' vs 'Peninsula New York')
    without erasing distinct city qualifiers ('Bal Harbour' vs 'Longboat Key').
    """
    t1, t2 = align_compound_tokens(list(op.toks_seq), list(np.toks_seq))
    if not t1 or not t2:
        return 0.0, False, set(), False

    shared_loc = op.loc_tokens & np.loc_tokens
    id1 = t1 - shared_loc
    id2 = t2 - shared_loc
    used_geo_fallback = not id1 or not id2
    if used_geo_fallback:
        id1, id2 = t1, t2

    exact_core = (id1 == id2 and bool(id1)) or (
        bool(op.joined_core) and op.joined_core == np.joined_core
    )
    inter = id1 & id2
    diff = id1 ^ id2

    def weight(tok: str) -> float:
        return idf.get(tok, 5.0)

    w_inter = sum(weight(t) for t in inter)
    w_1 = sum(weight(t) for t in id1)
    w_2 = sum(weight(t) for t in id2)
    prec = w_inter / w_1 if w_1 > 0 else 0.0
    rec = w_inter / w_2 if w_2 > 0 else 0.0
    idf_f1 = (2.0 * prec * rec / (prec + rec)) if (prec + rec) > 0 else 0.0
    if used_geo_fallback and t1 != t2:
        idf_f1 *= 0.5

    mutually_exclusive = bool((id1 - id2) and (id2 - id1))
    extra_disc = diff & DISCRIMINATOR_TOKENS
    return idf_f1, exact_core, extra_disc, mutually_exclusive


def compare(
    old_text: str,
    new_text: str,
    *,
    move_threshold_m: float = DEFAULT_MOVE_THRESHOLD_M,
) -> DiffResult:
    """Compare old and new geocoded CSV texts using 4-stage 1-to-1 matching."""
    old_rows, new_rows = read_rows(old_text), read_rows(new_text)
    old_records, new_records = parse_records(old_rows), parse_records(new_rows)
    sheet_changed = sheet_rows(old_rows) != sheet_rows(new_rows)

    idf = _build_idf(old_records + new_records)

    # Full-snapshot ID uniqueness counts prevent colliding place_ids (where
    # multiple distinct hotels were previously geocoded to the same place_id)
    # from being treated as unique physical identifiers in Stage 4.
    old_pid_counts = Counter(r.place_id for r in old_records if r.place_id)
    new_pid_counts = Counter(r.place_id for r in new_records if r.place_id)
    old_hcode_counts = Counter(
        code for r in old_records if (code := _hilton_property_code(r.hotel_url))
    )
    new_hcode_counts = Counter(
        code for r in new_records if (code := _hilton_property_code(r.hotel_url))
    )

    u_old: Set[int] = set(range(len(old_records)))
    u_new: Set[int] = set(range(len(new_records)))
    # (old_idx, new_idx, tier, stage_reason)
    matched: List[Tuple[int, int, str, str]] = []

    # --- Stage 1: Exact raw (hotel_name, group_label, hotel_location) ---
    new_by_exact: Dict[Tuple[str, str, str], int] = {
        new_records[ni].identity: ni for ni in u_new
    }
    for oi in list(u_old):
        ni = new_by_exact.get(old_records[oi].identity)
        if ni is not None and ni in u_new:
            matched.append((oi, ni, "tier1_silent", "exact"))
            u_old.remove(oi)
            u_new.remove(ni)

    # --- Stage 2: Canonical normalized (name, group) + city/geo agreement ---
    new_by_norm: Dict[Tuple[str, str], List[int]] = defaultdict(list)
    for ni in u_new:
        rec = new_records[ni]
        new_by_norm[(normalize_name(rec.hotel_name), normalize_name(rec.group_label))].append(ni)

    stage2_candidates: List[Tuple[Tuple[int, int, float], int, int]] = []
    for oi in u_old:
        o = old_records[oi]
        key = (normalize_name(o.hotel_name), normalize_name(o.group_label))
        for ni in new_by_norm.get(key, []):
            n = new_records[ni]
            if _hilton_codes_conflict(o, n):
                continue
            dist = _distance_between(o, n)
            same_pid = bool(o.place_id and o.place_id == n.place_id)
            distinct_pids = bool(o.place_id and n.place_id and o.place_id != n.place_id)
            very_close = dist is not None and dist <= DEFAULT_MOVE_THRESHOLD_M
            if o.hotel_location or n.hotel_location:
                loc_ok = locations_compatible(o.hotel_location, n.hotel_location)
                geo_ok = (
                    same_pid
                    or very_close
                    or (not distinct_pids and dist is not None and dist <= 25_000.0)
                )
                if not (loc_ok or geo_ok):
                    continue
            else:
                # Both rows omit hotel_location (e.g. Hilton): require matching
                # place_id, very close coords (<=250 m), or non-conflicting
                # place_ids with nearby/missing coords.
                loc_ok = False
                if not (
                    same_pid
                    or very_close
                    or (not distinct_pids and (dist is None or dist <= 25_000.0))
                ):
                    continue
            rank = (1 if same_pid else 0, 1 if loc_ok else 0, -(dist if dist is not None else 1e9))
            stage2_candidates.append((rank, oi, ni))

    stage2_candidates.sort(key=lambda item: item[0], reverse=True)
    for _, oi, ni in stage2_candidates:
        if oi in u_old and ni in u_new:
            matched.append((oi, ni, "tier1_silent", "norm_name"))
            u_old.remove(oi)
            u_new.remove(ni)

    # --- Stage 3: Unique stable Hilton 7-char property code ---
    new_by_hcode: Dict[str, int] = {}
    for ni in u_new:
        code = _hilton_property_code(new_records[ni].hotel_url)
        if code and new_hcode_counts[code] == 1:
            new_by_hcode[code] = ni

    for oi in list(u_old):
        code = _hilton_property_code(old_records[oi].hotel_url)
        if code and old_hcode_counts[code] == 1 and code in new_by_hcode:
            ni = new_by_hcode[code]
            if ni in u_new:
                same_name = (
                    normalize_name(old_records[oi].hotel_name)
                    == normalize_name(new_records[ni].hotel_name)
                )
                same_group = (
                    normalize_name(old_records[oi].group_label)
                    == normalize_name(new_records[ni].group_label)
                )
                # Report brand/group changes (e.g. Curio -> LXR) as notifiable renames.
                tier = "tier1_silent" if (same_name and same_group) else "tier2_rename"
                matched.append((oi, ni, tier, "hilton_code"))
                u_old.remove(oi)
                u_new.remove(ni)

    # --- Stage 4: Multi-signal entity resolution on residuals ---
    if u_old and u_new:
        old_prep = {oi: _prepare(old_records[oi]) for oi in u_old}
        new_prep = {ni: _prepare(new_records[ni]) for ni in u_new}

        new_by_block: Dict[Tuple[str, str], List[int]] = defaultdict(list)
        for ni, np in new_prep.items():
            new_by_block[(np.norm_group, np.country)].append(ni)
            if not np.country:
                new_by_block[(np.norm_group, "")].append(ni)

        stage4_candidates: List[Tuple[float, int, int, str, str]] = []
        for oi, op in old_prep.items():
            cand_nis: Set[int] = set(new_by_block.get((op.norm_group, op.country), []))
            if not op.country:
                for (bg, _), lst in new_by_block.items():
                    if bg == op.norm_group:
                        cand_nis.update(lst)
            else:
                cand_nis.update(new_by_block.get((op.norm_group, ""), []))

            o = op.record
            opid_unique = bool(o.place_id and old_pid_counts[o.place_id] == 1)

            for ni in cand_nis:
                np = new_prep[ni]
                # Hard veto: conflicting Hilton property codes.
                if op.hilton_code and np.hilton_code and op.hilton_code != np.hilton_code:
                    continue

                n = np.record
                dist = _distance_between(o, n)
                same_unique_pid = bool(
                    opid_unique and n.place_id and o.place_id == n.place_id
                    and new_pid_counts[n.place_id] == 1
                )
                # Veto brand mismatches unless both rows share the exact same
                # unique Google place_id (e.g. Bulgari -> Bvlgari brand rename).
                if not same_unique_pid and op.norm_brand and np.norm_brand and op.norm_brand != np.norm_brand:
                    continue
                distinct_pids = bool(o.place_id and n.place_id and o.place_id != n.place_id)
                same_city = locations_compatible(o.hotel_location, n.hotel_location)
                if not same_unique_pid and not same_city and (dist is None or dist > 50_000.0):
                    continue

                idf_f1, exact_core, extra_disc, mutually_exclusive = _name_similarity(op, np, idf)

                tier: Optional[str] = None
                reason = ""

                # Tier 1 (silent cosmetic update): identical core identity after
                # stripping affiliation taglines and shared location tokens.
                if (
                    exact_core
                    and not extra_disc
                    and (same_city or same_unique_pid)
                    and (same_unique_pid or dist is None or dist <= 5_000.0)
                ):
                    tier = "tier1_silent"
                    reason = "cosmetic_rename"
                # Tier 2 (surfaced as `~ Renamed` for human review): strong physical
                # match with substantive name change. When both records carry
                # distinct non-empty place_ids and non-identical cores, keep them
                # separate so adjacent sibling properties do not merge.
                elif same_unique_pid and not extra_disc and not mutually_exclusive and idf_f1 >= 0.65:
                    tier = "tier2_rename"
                    reason = "unique_place_id+subset_name"
                elif same_unique_pid and (dist is None or dist <= DEFAULT_MOVE_THRESHOLD_M):
                    tier = "tier2_rename"
                    reason = "unique_place_id+rename"
                elif (
                    not distinct_pids
                    and dist is not None
                    and dist <= 250.0
                    and not extra_disc
                    and not mutually_exclusive
                    and idf_f1 >= 0.78
                ):
                    tier = "tier2_rename"
                    reason = "tight_geo+high_idf"
                elif (
                    not distinct_pids
                    and same_city
                    and not extra_disc
                    and not mutually_exclusive
                    and idf_f1 >= 0.80
                    and (dist is None or dist <= 15_000.0)
                ):
                    tier = "tier2_rename"
                    reason = "same_city+high_idf"

                if tier is not None:
                    tier_bonus = 2.0 if tier == "tier1_silent" else 1.0
                    geo_bonus = 1.5 if same_unique_pid else (0.8 if (dist is not None and dist <= 250.0) else 0.3)
                    dist_penalty = 0.00001 * dist if dist is not None else 0.0
                    score = tier_bonus + geo_bonus + idf_f1 - dist_penalty
                    stage4_candidates.append((score, oi, ni, tier, reason))

        stage4_candidates.sort(key=lambda item: item[0], reverse=True)
        for _, oi, ni, tier, reason in stage4_candidates:
            if oi in u_old and ni in u_new:
                matched.append((oi, ni, tier, reason))
                u_old.remove(oi)
                u_new.remove(ni)

    # --- Build final diff buckets ---
    renamed: List[HotelMatch] = []
    silent_updates: List[HotelMatch] = []
    moved: List[MovedHotel] = []

    for oi, ni, tier, stage in matched:
        o, n = old_records[oi], new_records[ni]
        if tier == "tier2_rename":
            renamed.append(HotelMatch(old=o, new=n, stage=stage))
        elif stage != "exact":
            silent_updates.append(HotelMatch(old=o, new=n, stage=stage))

        dist = _distance_between(o, n)
        lost_lodging = _lost_lodging_place(o, n)
        coords_moved = o.coords != n.coords and (dist is None or dist >= move_threshold_m)
        if coords_moved or lost_lodging:
            moved.append(
                MovedHotel(
                    old=o,
                    new=n,
                    distance_m=dist,
                    reason="lost lodging type" if lost_lodging else "",
                )
            )

    added = sorted((new_records[ni] for ni in u_new), key=lambda r: r.identity)
    removed = sorted((old_records[oi] for oi in u_old), key=lambda r: r.identity)
    renamed.sort(key=lambda m: (m.new.identity, m.old.identity))
    silent_updates.sort(key=lambda m: (m.new.identity, m.old.identity))
    moved.sort(key=lambda m: m.new.identity)

    return DiffResult(
        sheet_changed=sheet_changed,
        added=added,
        removed=removed,
        renamed=renamed,
        moved=moved,
        silent_updates=silent_updates,
    )


def committed(path: str) -> str:
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


def describe(record: HotelRecord) -> str:
    details = ", ".join(part for part in (record.hotel_location, record.group_label) if part)
    return f"{record.hotel_name} ({details})" if details else record.hotel_name


def _format_distance(distance_m: Optional[float], reason: str = "") -> str:
    if distance_m is None:
        base = "coords added/removed"
    elif distance_m >= 1000.0:
        base = f"{distance_m / 1000.0:.1f} km"
    else:
        base = f"{distance_m:.0f} m"
    return f"{base}, {reason}" if reason else base


def _format_rename(match: HotelMatch) -> str:
    if match.old.group_label != match.new.group_label:
        return f"  ~ Renamed: {describe(match.old)} -> {describe(match.new)}"
    return f"  ~ Renamed: {match.old.hotel_name} -> {describe(match.new)}"


def _append_capped(
    lines: List[str],
    items: List[str],
    noun: str,
    limit: Optional[int],
    *,
    footer_note: str = "see attached CSVs",
) -> None:
    if limit is None:
        lines.extend(items)
        return
    lines.extend(items[:limit])
    if len(items) > limit:
        lines.append(f"  ... and {len(items) - limit} more {noun}; {footer_note}")


def report_section(
    label: str,
    diff: DiffResult,
    *,
    max_per_category: int = MAX_DETAILS_PER_CATEGORY,
    max_renames: Optional[int] = None,
) -> str:
    lines = [
        f"{label}: {len(diff.added)} added, {len(diff.removed)} removed, "
        f"{len(diff.renamed)} renamed, {len(diff.moved)} coordinate changes"
    ]
    _append_capped(
        lines,
        [f"  + {describe(rec)}" for rec in diff.added],
        "added",
        max_per_category,
    )
    _append_capped(
        lines,
        [f"  - {describe(rec)}" for rec in diff.removed],
        "removed",
        max_per_category,
    )
    # List all renames by default because old names are not in the attached CSVs.
    _append_capped(
        lines,
        [_format_rename(m) for m in diff.renamed],
        "renamed",
        max_renames,
        footer_note="see workflow logs for full rename list",
    )
    _append_capped(
        lines,
        [
            f"  ~ Moved ({_format_distance(m.distance_m, m.reason)}): {describe(m.new)}: "
            f"{m.old.lat}, {m.old.lon} -> {m.new.lat}, {m.new.lon}"
            for m in diff.moved
        ],
        "coordinate changes",
        max_per_category,
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", action="append", required=True, metavar="OUTPUT:PATH")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()

    outputs: Dict[str, str] = {}
    sections: List[str] = []
    notify = False
    for spec in args.csv:
        key, path = spec.split(":", 1)
        current = Path(path).read_text(encoding="utf-8")
        if not current.strip():
            raise ValueError(f"Geocoded CSV is empty: {path}")
        diff = compare(committed(path), current)
        outputs[key] = "true" if diff.sheet_changed else "false"
        if diff.silent_updates:
            print(f"{path}: ignored {len(diff.silent_updates)} location/cosmetic metadata update(s)")
        if diff.has_notifiable_changes:
            notify = True
            sections.append(report_section(path, diff))

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

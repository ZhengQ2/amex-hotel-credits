#!/usr/bin/env python3
"""Decide whether nightly geocoded CSV changes need a sheet update or email."""

import argparse
import csv
import html
import io
import os
import subprocess
from collections import Counter
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

SITE_URL = "https://amex.zhengqiu.net/"
DETAIL_LIMIT = 25

# Friendly program names and accent colors, keyed by geocoded CSV filename prefix.
PROGRAMS = {
    "hilton": ("Hilton Resort Credit", "#2563eb"),
    "fhr": ("Fine Hotels + Resorts", "#b45309"),
    "thc": ("The Hotel Collection", "#15803d"),
}


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
    lines.extend(details[:DETAIL_LIMIT])
    if len(details) > DETAIL_LIMIT:
        lines.append(f"  ... and {len(details) - DETAIL_LIMIT} more; see attached CSVs")
    return "\n".join(lines)


def program_for(path):
    prefix = Path(path).name.split("_", 1)[0].lower()
    return PROGRAMS.get(prefix, (Path(path).name, "#475569"))


def hotel_urls(text):
    return {
        tuple((row.get(key) or "").strip() for key in ("hotel_name", "group_label", "hotel_location")):
            (row.get("hotel_url") or "").strip()
        for row in read_rows(text)
    }


def _hotel_cell(identity, url):
    name, group, location = (html.escape(part) for part in identity)
    if url.startswith(("https://", "http://")):
        name = (f'<a href="{html.escape(url)}" style="color:#0f172a;'
                f'text-decoration:none;font-weight:600">{name}</a>')
    else:
        name = f'<span style="font-weight:600">{name}</span>'
    meta = " &middot; ".join(part for part in (location, group.title() if group.isupper() else group) if part)
    meta = f'<div style="font-size:12px;color:#64748b;margin-top:2px">{meta}</div>' if meta else ""
    return name + meta


def _maps_link(coords):
    lat, lon = coords
    if lat is None or lon is None:
        return '<span style="color:#94a3b8">none</span>'
    return (f'<a href="https://www.google.com/maps/search/?api=1&amp;query={quote(f"{lat},{lon}")}" '
            f'style="color:#2563eb;text-decoration:none">{lat:.4f}, {lon:.4f}</a>')


def _pill(text, fg, bg):
    return (f'<span style="display:inline-block;padding:3px 10px;border-radius:999px;'
            f'background:{bg};color:{fg};font-size:12px;font-weight:600;margin:0 6px 6px 0">{text}</span>')


KINDS = {
    "added": ("Added", "#15803d", "#dcfce7"),
    "removed": ("Removed", "#b91c1c", "#fee2e2"),
    "moved": ("Moved", "#a16207", "#fef9c3"),
}


def html_section(path, added, removed, moved, old, new, urls):
    label, accent = program_for(path)
    rows = (
        [("added", key, "") for key in added]
        + [("removed", key, "") for key in removed]
        + [("moved", key, f"{_maps_link(old[key])}&nbsp;&rarr;&nbsp;{_maps_link(new[key])}")
           for key in moved]
    )
    pills = "".join(
        _pill(f"{count} {KINDS[kind][0].lower()}", *KINDS[kind][1:])
        for kind, count in (("added", len(added)), ("removed", len(removed)), ("moved", len(moved)))
        if count
    )
    body = []
    for kind, key, extra in rows[:DETAIL_LIMIT]:
        tag, fg, bg = KINDS[kind]
        extra = f'<div style="font-size:12px;margin-top:4px">{extra}</div>' if extra else ""
        body.append(
            '<tr><td style="padding:10px 12px;border-top:1px solid #f1f5f9;vertical-align:top;width:72px">'
            f'<span style="display:inline-block;padding:2px 8px;border-radius:6px;background:{bg};'
            f'color:{fg};font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.03em">{tag}</span></td>'
            f'<td style="padding:10px 12px 10px 0;border-top:1px solid #f1f5f9;font-size:14px;color:#0f172a">'
            f'{_hotel_cell(key, urls.get(key, ""))}{extra}</td></tr>'
        )
    if len(rows) > DETAIL_LIMIT:
        body.append(
            '<tr><td colspan="2" style="padding:10px 12px;border-top:1px solid #f1f5f9;'
            f'font-size:13px;color:#64748b">&hellip;and {len(rows) - DETAIL_LIMIT} more; '
            'see the attached CSV.</td></tr>'
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#ffffff;border:1px solid #e2e8f0;border-radius:12px;'
        f'border-top:4px solid {accent};margin:0 0 20px;border-collapse:separate">'
        f'<tr><td colspan="2" style="padding:16px 12px 6px"><div style="font-size:17px;font-weight:700;'
        f'color:#0f172a;margin-bottom:8px">{html.escape(label)}</div>{pills}</td></tr>'
        + "".join(body) + "</table>"
    )


def html_report(sections):
    if sections:
        intro = "Here's what changed in tonight's refresh."
        content = "".join(sections)
    else:
        intro = "No hotel list or coordinate changes tonight."
        content = ""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hotel list updates</title></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px">
<tr><td style="background:#0f172a;border-radius:12px;padding:24px;color:#f8fafc">
<div style="font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:#94a3b8;font-weight:600">Amex Hotel Credits Map</div>
<div style="font-size:22px;font-weight:700;margin-top:6px">Hotel list updates</div>
<div style="font-size:14px;color:#cbd5e1;margin-top:6px">{intro}</div>
</td></tr>
<tr><td style="height:20px;line-height:20px">&nbsp;</td></tr>
<tr><td>{content}</td></tr>
<tr><td align="center" style="padding:4px 0 8px">
<a href="{SITE_URL}" style="display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;font-weight:600;font-size:14px;padding:11px 22px;border-radius:8px">Open the map &rarr;</a>
</td></tr>
<tr><td align="center" style="padding:16px 0 0;font-size:12px;color:#94a3b8">
Updated geocoded CSVs are attached. Sent automatically by the nightly GitHub Actions refresh.
</td></tr>
</table></td></tr></table></body></html>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", action="append", required=True, metavar="OUTPUT:PATH")
    parser.add_argument("--report", required=True)
    parser.add_argument("--html-report", help="also write an HTML version of the report")
    args = parser.parse_args()

    outputs = {}
    sections = []
    html_sections = []
    notify = False
    for spec in args.csv:
        key, path = spec.split(":", 1)
        current = Path(path).read_text(encoding="utf-8")
        if not current.strip():
            raise ValueError(f"Geocoded CSV is empty: {path}")
        previous = committed(path)
        sheet_changed, added, removed, moved, old, new = compare(previous, current)
        outputs[key] = "true" if sheet_changed else "false"
        if added or removed or moved:
            notify = True
            sections.append(report_section(path, added, removed, moved, old, new))
            urls = {**hotel_urls(previous), **hotel_urls(current)}
            html_sections.append(html_section(path, added, removed, moved, old, new, urls))

    outputs["notify"] = "true" if notify else "false"
    Path(args.report).write_text(
        "Hotel list and coordinate changes\n\n" + "\n\n".join(sections)
        if notify else "No hotel list or coordinate changes.\n",
        encoding="utf-8",
    )
    if args.html_report:
        Path(args.html_report).write_text(html_report(html_sections), encoding="utf-8")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        for key, value in outputs.items():
            output.write(f"{key}={value}\n")
    print(f"Sheet changes and email decision: {outputs}")


if __name__ == "__main__":
    main()

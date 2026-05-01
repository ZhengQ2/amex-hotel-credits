import csv
import re
from html.parser import HTMLParser
from typing import Dict, List
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import HTTPCookieProcessor, Request, build_opener

from cache_utils import (
    _clean_text as _clean_text_shared,
    load_geocode_cache_index,
    find_cache_match,
    is_cached_hotel_in_scraped,
    update_cache_file,
    remove_hotels_from_cache,
    make_placeholder_key,
)

URL_TEMPLATE = "https://www.americanexpress.com/en-us/travel/discover/property-results/r/{page}"
OUT_FHR = "cache/fhr_hotels.csv"
OUT_THC = "cache/thc_hotels.csv"
FHR_CACHE_FILE = "cache/geocode_cache_google_fhr.json"
THC_CACHE_FILE = "cache/geocode_cache_google_thc.json"


def _clean_text(s: str) -> str:
    return _clean_text_shared(s)


def _program_bucket(label: str) -> str:
    raw = _clean_text(label).upper()
    compact = re.sub(r"[^A-Z0-9]+", "", raw)
    if "FHR" in compact or "FINEHOTELS" in compact:
        return "FHR"
    if "THC" in compact or "HOTELCOLLECTION" in compact:
        return "THC"
    return ""


class AmexCardHTMLParser(HTMLParser):
    """Parse AMEX property results cards from server-rendered HTML."""

    def __init__(self):
        super().__init__()
        self.rows = []

        self._stack = []
        self._capture_program = False
        self._capture_brand = False
        self._capture_location = False
        self._capture_supplier = False

        self._program_buf = []
        self._brand_buf = []
        self._location_buf = []
        self._supplier_buf = []

        self._current_href = ""
        self._last_program = ""
        self._last_brand = ""
        self._last_location = ""
        self._pending_row = None

    @staticmethod
    def _class_has(attrs, needle):
        classes = (attrs.get("class", "") or "").split()
        for cls in classes:
            if cls == needle or cls.startswith(f"{needle}-") or cls.startswith(f"{needle}__"):
                return True
        return False

    def _pop_until_matching_tag(self, tag):
        """Pop stack entries until we find a matching opening tag.

        HTML from upstream can be imperfect. Using strict LIFO matching can
        desynchronize parsing state and drop rows.
        """
        while self._stack:
            started_tag, started_attrs = self._stack.pop()
            if started_tag == tag:
                return started_tag, started_attrs
        return None, None

    @staticmethod
    def _effective_text(is_capturing, buf, last_value):
        """Prefer in-flight buffer text when a field hasn't closed yet."""
        if is_capturing:
            candidate = _clean_text("".join(buf))
            if candidate:
                return candidate
        return last_value

    def _build_row_from_current_state(self, hotel_name, href):
        program = self._effective_text(self._capture_program, self._program_buf, self._last_program)
        brand = self._effective_text(self._capture_brand, self._brand_buf, self._last_brand)
        location = self._effective_text(self._capture_location, self._location_buf, self._last_location)
        if hotel_name and href and program:
            return {
                "program": program,
                "brand": brand,
                "location": location,
                "hotel_name": hotel_name,
                "hotel_url": href,
            }
        return None

    def _flush_pending_row(self):
        if not self._pending_row:
            return

        # Preserve values captured when supplier anchor closed. Only backfill
        # missing fields from in-flight buffers; never overwrite from last seen
        # state because that can already belong to the next card.
        if not self._pending_row.get("program"):
            self._pending_row["program"] = self._effective_text(self._capture_program, self._program_buf, "")
        if not self._pending_row.get("brand"):
            self._pending_row["brand"] = self._effective_text(self._capture_brand, self._brand_buf, "")
        if not self._pending_row.get("location"):
            # Location often appears after supplierName; when that happens the
            # pending row is created before location is parsed. Prefer current
            # buffer text, then current-card finalized location.
            location_text = _clean_text("".join(self._location_buf))
            if location_text:
                self._pending_row["location"] = location_text
            elif self._last_location:
                self._pending_row["location"] = self._last_location

        self.rows.append(self._pending_row)
        self._pending_row = None

    def handle_starttag(self, tag, attrs_list):
        attrs = dict(attrs_list)
        self._stack.append((tag, attrs))

        if tag == "div" and self._class_has(attrs, "card-program"):
            # New card boundary: clear stale card-scoped fields so cards that
            # omit them do not inherit values from a previous hotel.
            self._last_brand = ""
            self._last_location = ""
            self._capture_program = True
            self._program_buf = []

        if tag == "div" and self._class_has(attrs, "card-brand"):
            self._capture_brand = True
            self._brand_buf = []

        if tag == "div" and self._class_has(attrs, "card-location"):
            self._capture_location = True
            self._location_buf = []

        if tag == "div" and self._class_has(attrs, "wst-footer"):
            # Footer marks end of card content; flush any parsed supplier row.
            self._flush_pending_row()
            # Also stop any in-flight supplier-name capture to avoid swallowing
            # CTA text like "View Hotel".
            self._capture_supplier = False
            self._supplier_buf = []
            self._current_href = ""

        if tag == "a":
            href = attrs.get("href", "")
            # Only capture the explicit supplier-name anchor. Broad href-based
            # matching also captures CTA links (e.g., "View Hotel").
            if self._class_has(attrs, "card-supplierName"):
                # If previous card had no footer marker, finalize it before
                # starting a new supplier capture.
                self._flush_pending_row()
                self._capture_supplier = True
                self._supplier_buf = []
                self._current_href = href

    def handle_endtag(self, tag):
        started_tag, started_attrs = self._pop_until_matching_tag(tag)
        if not started_tag:
            return
        if tag == "div" and started_tag == "div":
            if self._class_has(started_attrs, "card-program"):
                self._capture_program = False
                self._last_program = _clean_text("".join(self._program_buf))
            if self._class_has(started_attrs, "card-brand"):
                self._capture_brand = False
                self._last_brand = _clean_text("".join(self._brand_buf))
            if self._class_has(started_attrs, "card-location"):
                self._capture_location = False
                self._last_location = _clean_text("".join(self._location_buf))

        if tag == "a" and started_tag == "a" and self._capture_supplier:
            self._capture_supplier = False
            hotel_name = _clean_text("".join(self._supplier_buf))
            href = (self._current_href or "").strip()

            self._pending_row = self._build_row_from_current_state(hotel_name, href)
            self._supplier_buf = []
            self._current_href = ""

    def handle_data(self, data):
        if self._capture_program:
            self._program_buf.append(data)
        if self._capture_brand:
            self._brand_buf.append(data)
        if self._capture_location:
            self._location_buf.append(data)
        if self._capture_supplier:
            self._supplier_buf.append(data)

    def close(self):
        self._flush_pending_row()
        super().close()


def _dumb_fetch_html(url, opener, timeout=45):
    req = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://www.americanexpress.com/en-us/travel/",
            "Upgrade-Insecure-Requests": "1",
        },
    )
    with opener.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _normalize_rows(rows, base_url):
    cleaned = []
    for row in rows:
        program = _clean_text(row.get("program", ""))
        brand = _clean_text(row.get("brand", ""))
        location = _clean_text(row.get("location", ""))
        hotel_name = _clean_text(row.get("hotel_name", ""))
        hotel_url = (row.get("hotel_url", "") or "").strip()

        if not program or not hotel_name or not hotel_url:
            continue

        cleaned.append(
            {
                "program_label": program,
                "brand_label": brand,
                "hotel_location": location,
                "hotel_name": hotel_name,
                "hotel_url": urljoin(base_url, hotel_url),
                "group_label": program,
                "group_type": "Program",
            }
        )
    return cleaned


def scrape_all_pages(max_pages=50):
    opener = build_opener(HTTPCookieProcessor())
    all_rows = []
    empty_pages = 0

    try:
        _dumb_fetch_html("https://www.americanexpress.com/", opener, timeout=30)
    except Exception:
        pass

    for n in range(1, max_pages + 1):
        url = URL_TEMPLATE.format(page=n)
        try:
            html = _dumb_fetch_html(url, opener)
        except (HTTPError, URLError, TimeoutError):
            empty_pages += 1
            if empty_pages >= 2:
                break
            continue

        blocked = "Access Denied" in html or "Request unsuccessful" in html
        if blocked or len(html) < 1500:
            empty_pages += 1
            if empty_pages >= 2:
                break
            continue

        parser = AmexCardHTMLParser()
        parser.feed(html)
        rows = _normalize_rows(parser.rows, url)

        if not rows:
            empty_pages += 1
            if empty_pages >= 2:
                break
            continue

        empty_pages = 0
        all_rows.extend(rows)

    dedup = {}
    for r in all_rows:
        key = (
            r["hotel_name"].lower(),
            r["brand_label"].lower(),
            r["program_label"].lower(),
            r["hotel_location"].lower(),
        )
        dedup.setdefault(key, r)

    return list(dedup.values())


def _write_rows(rows, out_path):
    cols = [
        "program_label",
        "brand_label",
        "hotel_location",
        "hotel_name",
        "hotel_url",
        "group_label",
        "group_type",
    ]

    sorted_rows = sorted(
        rows,
        key=lambda r: (
            (r.get("program_label") or "").lower(),
            (r.get("brand_label") or "").lower(),
            (r.get("hotel_location") or "").lower(),
            (r.get("hotel_name") or "").lower(),
        ),
    )

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        for row in sorted_rows:
            writer.writerow({c: row.get(c, "") for c in cols})

    print(f"Wrote {len(sorted_rows)} rows -> {out_path} (source=dumb-fetch)")


def write_output(rows):
    fhr_rows = []
    thc_rows = []
    unknown_rows = 0

    for row in rows:
        bucket = _program_bucket(row.get("program_label", ""))
        if bucket == "FHR":
            fhr_rows.append(row)
        elif bucket == "THC":
            thc_rows.append(row)
        else:
            unknown_rows += 1

    if unknown_rows:
        print(f"Skipped {unknown_rows} rows with unknown program labels.")

    _write_rows(fhr_rows, OUT_FHR)
    _write_rows(thc_rows, OUT_THC)


def main():
    rows = scrape_all_pages()
    if not rows:
        raise RuntimeError("Failed to fetch AMEX pages or no rows were parsed.")
    write_output(rows)

    cache_index, cache_entries = load_geocode_cache_index(FHR_CACHE_FILE, THC_CACHE_FILE)
    if not cache_entries and not cache_index.get("__official__"):
        print(
            f"No cache index built (files missing or empty: "
            f"{FHR_CACHE_FILE}, {THC_CACHE_FILE})."
        )
        return

    # scope_key for FHR/THC is the normalised program ("FHR" or "THC"), lowercased
    # to match what load_geocode_cache_index stores from the cache's brand field.
    scraped_index: Dict[str, set] = {}
    for row in rows:
        program = _program_bucket(row["program_label"])
        if not program:
            continue
        scraped_index.setdefault(program.lower(), set()).add(_clean_text(row["hotel_name"]).lower())

    # 1) Classify each scraped hotel against the cache
    new_hotels = []
    renamed_hotels = []  # (scraped_name, cached_name, program)
    for row in rows:
        match = find_cache_match(row["hotel_name"], cache_entries)
        if match is None:
            new_hotels.append(row)
        else:
            cached_canonical = match[0]
            if _clean_text(row["hotel_name"]).lower() != cached_canonical:
                renamed_hotels.append((
                    row["hotel_name"],
                    cached_canonical,
                    _program_bucket(row["program_label"]),
                ))

    if not new_hotels:
        print("All scraped FHR/THC hotels appear to be present in geocode cache.")
    else:
        print("FHR/THC hotels NOT found in geocode cache (new hotels):")
        for r in new_hotels:
            print(f"- {r['hotel_name']}  [program: {_program_bucket(r['program_label'])}]  -> {r['hotel_url']}")

    if renamed_hotels:
        print("FHR/THC hotels with possible name changes (already geocoded, no re-geocoding needed):")
        for scraped, cached, program in sorted(renamed_hotels, key=lambda x: (x[2], x[0])):
            print(f"- [{program}] cached: {cached!r}  ->  scraped: {scraped!r}")

    # entry[1] is scope_key from the cache (brand.lower()). Pass it through
    # _program_bucket so that old google-convert entries with brand=brand_label
    # (not "FHR"/"THC") get normalised to "" and fall back to the full scraped set.
    removed_entries = [
        entry for entry in cache_entries
        if not is_cached_hotel_in_scraped(
            entry[0], _program_bucket(entry[1]), scraped_index
        )
    ]

    if not removed_entries:
        print("No cached FHR/THC hotels appear to have been removed from the current list.")
    else:
        print("FHR/THC hotels in geocode cache but NOT in current scraped list (removed):")
        for name, scope, _, _path in sorted(removed_entries, key=lambda x: (x[1], x[0])):
            print(f"- {name}  [program: {_program_bucket(scope) or 'UNKNOWN'}]")

    if new_hotels:
        print(f"\nUpdating cache with {len(new_hotels)} new FHR/THC hotels...")
        new_keys_by_file: Dict[str, list] = {}
        for h in new_hotels:
            program = _program_bucket(h["program_label"])
            path = THC_CACHE_FILE if program == "THC" else FHR_CACHE_FILE
            key = make_placeholder_key(_clean_text(h["hotel_name"]), program, "fhrthc")
            new_keys_by_file.setdefault(path, []).append((key, None))

        total_added = sum(
            update_cache_file(path, keys)
            for path, keys in new_keys_by_file.items()
        )
        if total_added > 0:
            print(f"✓ Added {total_added} new hotel(s) to cache")
        else:
            print("⚠ Failed to add hotels to cache (check file permissions)")

    if removed_entries:
        print(f"\nRemoving {len(removed_entries)} FHR/THC hotels from cache...")
        removed = remove_hotels_from_cache(removed_entries)
        if removed > 0:
            print(f"✓ Removed {removed} hotel(s) from cache")
        else:
            print("⚠ Failed to remove hotels from cache (check file permissions)")


if __name__ == "__main__":
    main()

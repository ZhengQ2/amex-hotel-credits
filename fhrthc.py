import csv
import difflib
import json
import os
import re
import unicodedata
from html.parser import HTMLParser
from typing import Any, Dict, List, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse, unquote
from urllib.request import HTTPCookieProcessor, Request, build_opener

URL_TEMPLATE = "https://www.americanexpress.com/en-us/travel/discover/property-results/r/{page}"
OUT_FHR = "cache/fhr_hotels.csv"
OUT_THC = "cache/thc_hotels.csv"
CACHE_FILE = "cache/geocode_cache_google_fhr_thc.json"


def _clean_text(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("®", "").replace("™", "")
    s = s.replace("–", "-").replace("—", "-").replace("’", "'")
    s = re.sub(r"\s+", " ", s).strip()
    return s


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


def _names_match(name1: str, name2: str) -> bool:
    n1 = _clean_text(name1).lower()
    n2 = _clean_text(name2).lower()

    if not n1 or not n2:
        return False
    if n1 == n2:
        return True

    # Many AMEX renames append neighborhood/location qualifiers while
    # keeping the original base name intact (e.g., "... Downtown" ->
    # "... Downtown Beale Street"). Treat long, explicit containment as
    # a match to avoid "new + removed" churn for the same hotel.
    shorter, longer = (n1, n2) if len(n1) <= len(n2) else (n2, n1)
    if len(shorter) >= 18 and (f" {shorter} " in f" {longer} "):
        return True

    generic_tokens = {
        "hotel", "resort", "spa", "and", "the", "at", "by", "&",
        "collection", "club", "property"
    }

    def tokens(s: str) -> set:
        return {
            t
            for t in re.findall(r"[a-z0-9]+", s)
            if t and t not in generic_tokens
        }

    t1 = tokens(n1)
    t2 = tokens(n2)

    if not t1 or not t2:
        return False
    if t1 == t2:
        return True

    overlap = t1 & t2
    if not overlap:
        return False

    smaller_set, larger_set = (t1, t2) if len(t1) <= len(t2) else (t2, t1)
    if smaller_set.issubset(larger_set):
        return (len(larger_set) - len(smaller_set)) <= 1

    overlap_ratio_1 = len(overlap) / len(t1)
    overlap_ratio_2 = len(overlap) / len(t2)
    if overlap_ratio_1 >= 0.8 and overlap_ratio_2 >= 0.8:
        return True

    # Final fallback for near-identical renames with punctuation/word-order
    # differences after upstream copy updates.
    return difflib.SequenceMatcher(a=n1, b=n2).ratio() >= 0.92


def _likely_same_hotel_rename(name1: str, name2: str) -> bool:
    """Looser matcher used only to reconcile new-vs-removed rename churn."""
    if _names_match(name1, name2):
        return True

    noise = {
        "hotel", "resort", "spa", "and", "the", "at", "by", "collection",
        "autograph", "luxury", "curio", "hilton", "marriott", "hyatt",
        "jw", "canopy", "graduate", "a", "an", "of"
    }

    def sig_tokens(s: str) -> set:
        return {
            t
            for t in re.findall(r"[a-z0-9]+", _clean_text(s).lower())
            if t and t not in noise
        }

    t1 = sig_tokens(name1)
    t2 = sig_tokens(name2)
    if not t1 or not t2:
        return False
    overlap = t1 & t2
    if len(overlap) == 1:
        # Some known rename pairs only preserve one distinctive token
        # (e.g., "Hotel AKA Brickell" -> "The Brickell Arch ...").
        token = next(iter(overlap))
        return len(token) >= 7

    smaller, larger = (t1, t2) if len(t1) <= len(t2) else (t2, t1)
    if smaller.issubset(larger):
        return True

    return (len(overlap) / len(smaller)) >= 0.8


def _load_geocode_cache_index(cache_path: str = CACHE_FILE):
    if not os.path.exists(cache_path):
        return {"__any__": set()}, {}

    with open(cache_path, "r", encoding="utf-8") as f:
        cache = json.load(f)

    index = {"__any__": set()}
    cached_hotels = {}

    for key in cache.keys():
        try:
            meta = json.loads(key)
        except Exception:
            continue

        program = _program_bucket(meta.get("program", "") or meta.get("brand", ""))
        queries_raw = meta.get("queries", []) or []
        queries_clean = [_clean_text(q).lower() for q in queries_raw if q]
        if not queries_clean:
            continue

        index["__any__"].update(queries_clean)
        if program:
            index.setdefault(program, set()).update(queries_clean)

        # Query lists are ordered strongest -> weakest and often end with
        # location-expanded variants. Picking the last query as canonical can
        # create false "removed" matches because those entries include city/
        # country tokens that are absent from scraped hotel names.
        # Prefer the shortest query as the canonical cache name.
        canonical_name = min(queries_clean, key=len)
        cached_hotels[key] = (canonical_name, program)

    return index, cached_hotels


def _slug_title_from_url(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        segs = [s for s in parsed.path.split("/") if s]
        if not segs:
            return ""
        slug = unquote(segs[-1])
        slug = re.sub(r"[^A-Za-z0-9 -]+", " ", slug)
        slug = re.sub(r"[-_]+", " ", slug)
        return _clean_text(slug)
    except Exception:
        return ""


def _is_hotel_in_cache(hotel_name: str, program: str, cache_index, hotel_url: str = "") -> bool:
    name_candidates = [_clean_text(hotel_name).lower()]
    slug_title = _slug_title_from_url(hotel_url).lower()
    if slug_title:
        name_candidates.append(slug_title)
    program_key = _program_bucket(program)

    scoped_queries = set(cache_index.get("__any__", set()))
    if program_key:
        scoped_queries.update(cache_index.get(program_key, set()))

    for candidate in name_candidates:
        for query in scoped_queries:
            if _names_match(candidate, query):
                return True
    return False


def _is_cached_hotel_in_scraped(cached_name: str, cached_program: str, scraped_index) -> bool:
    name = _clean_text(cached_name).lower()
    program = _program_bucket(cached_program)

    if program:
        candidates = scraped_index.get(program, [])
    else:
        candidates = [n for names in scraped_index.values() for n in names]

    for scraped_name in candidates:
        if _names_match(name, scraped_name):
            return True
    return False


def _update_cache_with_new_hotels(
    cache_path: str,
    new_hotels: List[Dict[str, Any]],
    cache_index: Dict[str, set],
) -> int:
    if not new_hotels:
        return 0

    try:
        if os.path.exists(cache_path):
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
        else:
            cache = {}

        added_count = 0
        for hotel_data in new_hotels:
            hotel_name = _clean_text(hotel_data["hotel_name"])
            program = _program_bucket(hotel_data["program_label"])
            if _is_hotel_in_cache(hotel_name, program, cache_index, hotel_data.get("hotel_url", "")):
                continue

            cache_key = json.dumps(
                {
                    "brand": program,
                    "input_format": "fhrthc",
                    "provider": "google_places_first",
                    "queries": [hotel_name],
                    "v": 3,
                },
                sort_keys=True,
            )
            cache[cache_key] = None
            added_count += 1

        if added_count > 0:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)

        return added_count

    except PermissionError:
        print(f"ERROR: Permission denied when trying to write to {cache_path}")
        return 0
    except Exception as e:
        print(f"ERROR: Failed to update cache: {e}")
        return 0


def _remove_hotels_from_cache(
    cache_path: str,
    keys_to_remove: List[str],
) -> int:
    if not keys_to_remove:
        return 0

    try:
        if not os.path.exists(cache_path):
            return 0

        with open(cache_path, "r", encoding="utf-8") as f:
            cache = json.load(f)

        removed_count = 0
        for key in keys_to_remove:
            if key in cache:
                del cache[key]
                removed_count += 1

        if removed_count > 0:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)

        return removed_count

    except PermissionError:
        print(f"ERROR: Permission denied when trying to write to {cache_path}")
        return 0
    except Exception as e:
        print(f"ERROR: Failed to remove hotels from cache: {e}")
        return 0


def main():
    rows = scrape_all_pages()
    if not rows:
        raise RuntimeError("Failed to fetch AMEX pages or no rows were parsed.")
    write_output(rows)

    cache_index, cached_hotels = _load_geocode_cache_index()
    if not cache_index.get("__any__"):
        print(f"No cache index built (file missing or empty: {CACHE_FILE}).")
        return

    scraped_index = {}
    for row in rows:
        program = _program_bucket(row["program_label"])
        if not program:
            continue
        name = _clean_text(row["hotel_name"]).lower()
        scraped_index.setdefault(program, []).append(name)

    new_hotels = [
        row
        for row in rows
        if not _is_hotel_in_cache(
            row["hotel_name"],
            row["program_label"],
            cache_index,
            row.get("hotel_url", ""),
        )
    ]

    if not new_hotels:
        print("All scraped FHR/THC hotels appear to be present in geocode cache.")
    else:
        print("FHR/THC hotels NOT found in geocode cache (new hotels):")
        for r in new_hotels:
            print(f"- {r['hotel_name']}  [program: {_program_bucket(r['program_label'])}]  -> {r['hotel_url']}")

    removed_hotels: List[Tuple[str, str]] = []
    removed_cache_keys: List[str] = []
    seen_removed = set()
    for cache_key, (cached_name, cached_program) in cached_hotels.items():
        if _is_cached_hotel_in_scraped(cached_name, cached_program, scraped_index):
            continue
        removed_cache_keys.append(cache_key)
        marker = (cached_name, cached_program)
        if marker not in seen_removed:
            seen_removed.add(marker)
            removed_hotels.append(marker)

    # Reconcile obvious rename pairs so they do not show up as both
    # "new hotel" and "removed hotel" in the same run.
    if new_hotels and removed_hotels:
        consumed_removed = set()
        reconciled_new = []
        for row in new_hotels:
            new_name = row["hotel_name"]
            matched_idx = None
            for idx, (removed_name, _removed_program) in enumerate(removed_hotels):
                if idx in consumed_removed:
                    continue
                if _likely_same_hotel_rename(new_name, removed_name):
                    matched_idx = idx
                    break
            if matched_idx is None:
                reconciled_new.append(row)
            else:
                consumed_removed.add(matched_idx)

        if consumed_removed:
            keep_pairs = set()
            for idx, pair in enumerate(removed_hotels):
                if idx not in consumed_removed:
                    keep_pairs.add(pair)
            removed_hotels = [p for p in removed_hotels if p in keep_pairs]
            removed_cache_keys = [
                k for k in removed_cache_keys
                if cached_hotels.get(k) in keep_pairs
            ]
            new_hotels = reconciled_new

    if not removed_hotels:
        print("No cached FHR/THC hotels appear to have been removed from the current list.")
    else:
        print("FHR/THC hotels in geocode cache but NOT in current scraped list (removed):")
        for name, program in sorted(removed_hotels, key=lambda x: (x[1], x[0])):
            print(f"- {name}  [program: {program or 'UNKNOWN'}]")

    if new_hotels:
        print(f"\nUpdating cache with {len(new_hotels)} new FHR/THC hotels...")
        added = _update_cache_with_new_hotels(CACHE_FILE, new_hotels, cache_index)
        if added > 0:
            print(f"✓ Added {added} new hotel(s) to cache")
        elif added == 0:
            print("⚠ Failed to add hotels to cache (check file permissions)")

    if removed_hotels:
        print(
            f"\nRemoving {len(removed_hotels)} FHR/THC hotels "
            f"({len(removed_cache_keys)} cache entries) from cache..."
        )
        removed = _remove_hotels_from_cache(CACHE_FILE, removed_cache_keys)
        if removed > 0:
            print(f"✓ Removed {removed} cache entr{'y' if removed == 1 else 'ies'}")
        elif removed == 0:
            print("⚠ Failed to remove hotels from cache (check file permissions)")


if __name__ == "__main__":
    main()

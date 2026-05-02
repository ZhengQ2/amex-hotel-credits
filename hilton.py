# scrape_hilton_hotels_by_brand_playwright.py
from playwright.sync_api import sync_playwright, TimeoutError
import pandas as pd
import json
import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode
from typing import Dict, List, Any, Tuple

from cache_utils import (
    _clean_text as _clean_text_shared,
    load_geocode_cache_index,
    is_hotel_in_cache,
    find_cache_match,
    is_cached_hotel_in_scraped,
    update_cache_file,
    remove_hotels_from_cache,
    make_placeholder_key,
)

URL = "https://www.hilton.com/en/p/hilton-honors/resort-credit-eligible-hotels/"
OUT = "cache/hilton_hotels.csv"
CACHE_FILE = "cache/geocode_cache_google_hilton.json"

_LOCATION_WORKERS = 10
_LOCATION_TIMEOUT = 15  # seconds per request
_LOCATION_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Some hotels are listed with external-brand domains or non-English Hilton locales.
# Map them to their canonical hilton.com/en/ property page so we can extract JSON-LD.
_EXTERNAL_URL_MAP: Dict[str, str] = {
    "https://romecavalieri.com/":               "https://www.hilton.com/en/hotels/romhiwa-rome-cavalieri/",
    "https://www.grandwailea.com/":             "https://www.hilton.com/en/hotels/jhmgwwa-grand-wailea/",
    "https://www.waldorfastoriamonarchbeach.com/": "https://www.hilton.com/en/hotels/snamowa-waldorf-astoria-monarch-beach/",
}


def _normalize_fetch_url(url: str) -> str:
    """Return the canonical Hilton /en/ URL to use for address extraction.

    - Maps known external-brand domains to their hilton.com property page.
    - Normalises non-English locale prefixes (/de/, /ja/, /fr/, …) to /en/.
    """
    if not url:
        return url
    if url in _EXTERNAL_URL_MAP:
        return _EXTERNAL_URL_MAP[url]
    # /XX/ or /XX-YY/ locale prefix → /en/  e.g. /de/hotels/ → /en/hotels/
    return re.sub(
        r'(https://(?:www\.)?hilton\.com)/[a-z]{2}(?:-[a-z]{2})?(/hotels/)',
        r'\1/en\2',
        url,
    )


def _extract_address_from_span(html: str) -> tuple:
    """Extract (hotel_address, hotel_location) from Hilton's address span element.

    Hilton renders the property address inside:
        <span class="... whitespace-pre-line">STREET\\nCITY, ZIP COUNTRY<svg…></svg></span>

    Returns a (hotel_address, hotel_location) tuple where hotel_location is a rough
    city/country hint derived from the trailing segment of the address.
    """
    m = re.search(
        r'<span[^>]+class=["\'][^"\']*\bwhitespace-pre-line\b[^"\']*["\'][^>]*>(.*?)</span>',
        html, re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return "", ""
    # Strip inner HTML (e.g. the SVG "open in new window" icon)
    text = re.sub(r'<[^>]+>', '', m.group(1))
    # Newlines separate street from city/country — normalise to comma-space
    text = re.sub(r'\s*\n\s*', ', ', text.strip())
    text = re.sub(r',\s*,', ',', text)
    text = re.sub(r'\s{2,}', ' ', text).strip().strip(',').strip()
    if not text:
        return "", ""

    # Derive a rough location hint (used by name-based geocoding fallback).
    # Typical tail segment: "78000 France" → "France", "CA 90210 United States" → "United States".
    parts = [p.strip() for p in text.split(',') if p.strip()]
    loc = ""
    if parts:
        last = re.sub(r'^\d[\d\s]*', '', parts[-1]).strip()   # strip leading zip/number
        loc = last if len(last) > 2 else (parts[-2].strip() if len(parts) >= 2 else "")
    return text, loc


def _extract_location_from_html(html: str) -> tuple:
    """Return (hotel_address, hotel_location) parsed from a Hilton property page.

    hotel_address  – full postal address (street, city, region, country)
    hotel_location – city + country only, used for location-compatibility validation

    Strategy:
    1. JSON-LD structured data (most precise, structured fields).
    2. Fallback: address <span class="...whitespace-pre-line"> in the page body.
    """
    # --- JSON-LD path ---
    for raw in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue

        items: list = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("@graph", [data])

        for item in items:
            if not isinstance(item, dict):
                continue
            type_val = item.get("@type", "")
            if isinstance(type_val, list):
                type_val = " ".join(str(t) for t in type_val)
            if not any(t in type_val.lower() for t in ("hotel", "lodging", "accommodation", "resort")):
                continue
            addr = item.get("address")
            if not isinstance(addr, dict):
                continue
            street  = (addr.get("streetAddress")  or "").strip()
            city    = (addr.get("addressLocality") or "").strip()
            region  = (addr.get("addressRegion")   or "").strip()
            country = (addr.get("addressCountry")  or "").strip()
            if city or street:
                hotel_address  = ", ".join(p for p in [street, city, region, country] if p)
                hotel_location = ", ".join(p for p in [city, country] if p)
                return hotel_address, hotel_location

    # --- Fallback: address span in page body ---
    return _extract_address_from_span(html)


def fetch_hotel_address(url: str) -> tuple:
    """GET a Hilton property page and return (hotel_address, hotel_location).

    The URL is normalised to a canonical hilton.com/en/ address before fetching
    so that external-domain URLs and non-English locale variants are handled.
    """
    if not url:
        return "", ""
    fetch_url = _normalize_fetch_url(url)
    try:
        resp = requests.get(fetch_url, timeout=_LOCATION_TIMEOUT, headers=_LOCATION_HEADERS)
    except Exception:
        return "", ""
    if not resp.ok:
        return "", ""
    return _extract_location_from_html(resp.text)


def backfill_addresses(df: pd.DataFrame, context=None) -> pd.DataFrame:
    """Fetch hotel_address (and hotel_location) for rows that are missing an address.

    When a Playwright BrowserContext is supplied the function uses a dedicated
    browser page so that JS-rendered content (JSON-LD, address spans) is
    available.  This is required because Hilton's pages are React-rendered and
    a plain HTTP GET returns a skeleton without address data.

    When no context is provided the function falls back to concurrent requests,
    which may yield empty results for JS-heavy pages.
    """
    mask = df["hotel_address"].str.strip() == ""
    missing_urls = df.loc[mask, "hotel_url"].tolist()
    if not missing_urls:
        return df

    url_to_result: Dict[str, tuple] = {}

    if context is not None:
        # ---- Playwright path (preferred) ----
        # Playwright sync API is single-threaded; we iterate sequentially.
        # In practice this only runs for new/uncached hotels so the cost is
        # amortised over subsequent runs.
        print(f"Fetching addresses for {len(missing_urls)} hotels via Playwright...")
        addr_page = context.new_page()
        addr_page.set_default_navigation_timeout(_LOCATION_TIMEOUT * 1000)
        for i, url in enumerate(missing_urls, 1):
            fetch_url = _normalize_fetch_url(url)
            try:
                addr_page.goto(fetch_url, wait_until="domcontentloaded")
                html = addr_page.content()
                url_to_result[url] = _extract_location_from_html(html)
            except Exception:
                url_to_result[url] = ("", "")
            if i % 10 == 0 or i == len(missing_urls):
                print(f"  {i}/{len(missing_urls)} fetched")
        addr_page.close()
    else:
        # ---- requests fallback (concurrent, but may miss JS-rendered data) ----
        print(f"Fetching addresses for {len(missing_urls)} hotels via requests "
              f"(up to {_LOCATION_WORKERS} parallel)...")
        with ThreadPoolExecutor(max_workers=_LOCATION_WORKERS) as pool:
            futures = {pool.submit(fetch_hotel_address, url): url for url in missing_urls}
            done = 0
            for future in as_completed(futures):
                url = futures[future]
                try:
                    url_to_result[url] = future.result()
                except Exception:
                    url_to_result[url] = ("", "")
                done += 1
                if done % 20 == 0 or done == len(missing_urls):
                    print(f"  {done}/{len(missing_urls)} fetched")

    fetched_count = sum(1 for (a, _) in url_to_result.values() if a)
    print(f"  Address resolved for {fetched_count}/{len(missing_urls)} hotels")

    df = df.copy()
    df.loc[mask, "hotel_address"]  = df.loc[mask, "hotel_url"].map(
        lambda u: url_to_result.get(u, ("", ""))[0]).fillna("")
    df.loc[mask, "hotel_location"] = df.loc[mask, "hotel_url"].map(
        lambda u: url_to_result.get(u, ("", ""))[1]).fillna("")
    return df


def _clean_text(s: str) -> str:
    return _clean_text_shared(s)


def _clean_url(u: str) -> str:
    if not u:
        return ""
    u = u.strip()
    if u.startswith(("javascript:", "#")):
        return ""
    # Normalize & drop obvious tracking params (keep it light)
    try:
        p = urlparse(u)
        if not p.scheme:
            # leave as-is; caller may join against page URL later
            pass
        # remove some common trackers
        qs = [
            (k, v)
            for k, v in parse_qsl(p.query)
            if k.lower()
            not in {
                "utm_source",
                "utm_medium",
                "utm_campaign",
                "utm_term",
                "utm_content",
                "gclid",
                "mc_cid",
                "mc_eid",
            }
        ]
        p = p._replace(query=urlencode(qs))
        return urlunparse(p)
    except Exception:
        return u


def _visible_text(el):
    return el.evaluate("n => (n.innerText || '').replace(/\\s+/g,' ').trim()")


def _click_all_show_more(panel):
    more_selectors = [
        "button:has-text('Show more')",
        "button:has-text('Show More')",
        "button:has-text('VIEW MORE')",
        "button:has-text('View more')",
        "a:has-text('Show more')",
        "a:has-text('View more')",
    ]
    changed = True
    attempts = 0
    while changed and attempts < 8:
        changed = False
        attempts += 1
        for sel in more_selectors:
            for b in panel.locator(sel).all():
                try:
                    if b.is_visible():
                        b.click(timeout=1000)
                        changed = True
                except Exception:
                    pass
        if changed:
            panel.page.wait_for_timeout(400)


def _force_lazy_render(panel):
    # Some brand panels are virtualized; keep scrolling until content
    # growth stabilizes instead of using a fixed number of attempts.
    stable_rounds = 0
    last_height = -1
    last_anchor_count = -1

    try:
        panel.evaluate("(el) => { if (el) el.scrollTop = 0; }")
    except Exception:
        return

    for _ in range(40):
        try:
            height = panel.evaluate("(el) => el ? el.scrollHeight : 0")
            anchor_count = panel.evaluate("(el) => el ? el.querySelectorAll('a').length : 0")
            panel.evaluate("(el) => { if (el) el.scrollTop = el.scrollHeight; }")
            panel.page.wait_for_timeout(180)
        except Exception:
            break

        if height == last_height and anchor_count == last_anchor_count:
            stable_rounds += 1
        else:
            stable_rounds = 0
        last_height = height
        last_anchor_count = anchor_count

        if stable_rounds >= 3:
            break


def _collect_hotel_links(panel, base_url):
    """
    Return list of dicts: [{name, href}], preferring Hilton property pages.
    """
    js = r"""
    (root) => {
      const rows = [];
      const clean = s => (s || "").replace(/\s+/g, " ").trim().replace(/[®™]/g, "");
      const isJunkText = t => /^(view|book|details?|rates?)\b/i.test(t);
      const abs = (href) => {
        try { return new URL(href, location.href).href; } catch { return href || ""; }
      };
      const titleFromHref = (href) => {
        try {
          const u = new URL(href, location.href);
          const parts = u.pathname.split("/").filter(Boolean);
          // For hilton.com/en/hotels/PROPERTY-SLUG/... use PROPERTY-SLUG, not the
          // last segment which may be a room/category sub-page.
          const hotelsIdx = parts.indexOf("hotels");
          const slug = (hotelsIdx !== -1 && hotelsIdx + 1 < parts.length)
            ? parts[hotelsIdx + 1]
            : (parts[parts.length - 1] || "");
          if (!slug) return "";
          return slug
            .replace(/[-_]+/g, " ")
            .replace(/\b\w/g, c => c.toUpperCase());
        } catch {
          return "";
        }
      };

      const push = (name, href) => {
        name = clean(name);
        href = (href || "").trim();
        if (!name || isJunkText(name)) return;
        if (!href || href.startsWith("#") || href.startsWith("javascript:")) return;
        rows.push({ name, href: abs(href) });
      };

      // Primary: anchors that look like property links
      root.querySelectorAll("a").forEach(a => {
        let t = clean(
          a.textContent ||
          a.getAttribute("aria-label") ||
          a.getAttribute("title") ||
          a.getAttribute("data-track-label") ||
          ""
        );
        const href = a.getAttribute("href") || "";
        if (!t && href) t = clean(titleFromHref(href));
        if (!t || t.length > 160) return;
        if (isJunkText(t)) return;
        push(t, href);
      });

      // Fallback: role=link (sometimes divs), try nearest/inner anchor
      root.querySelectorAll("[role='link']").forEach(el => {
        const t = clean(el.textContent || el.getAttribute("aria-label") || "");
        if (!t || t.length > 160 || isJunkText(t)) return;
        const a = el.closest("a") || el.querySelector("a");
        const href = a ? (a.getAttribute("href") || "") : "";
        if (href) push(t, href);
      });

      // Final fallback: cards with one obvious link
      root.querySelectorAll("[data-testid*='card'], [class*='card']").forEach(card => {
        const a = card.querySelector("a");
        if (!a) return;
        const t = clean(a.textContent || a.getAttribute("aria-label") || "");
        const href = a.getAttribute("href") || "";
        if (!t || t.length > 160 || isJunkText(t)) return;
        if (href) push(t, href);
      });

      return rows;
    }
    """
    try:
        items = panel.evaluate(js) or []
    except Exception:
        items = []

    # Clean and normalize
    cleaned = []
    for it in items:
        name = _clean_text(it.get("name") or "")
        href = _clean_url(it.get("href") or "")
        if not name or not href:
            continue
        if not urlparse(href).scheme:
            href = urljoin(base_url, href)
        cleaned.append({"name": name, "href": href})

    # Dedup per name, preferring Hilton property pages when multiple URLs exist
    def score(u: str) -> int:
        u = u.lower()
        # Prefer direct property pages on hilton.com/en/hotels/
        if "hilton.com" in u and "/en/hotels/" in u:
            return 3
        if "hilton.com" in u:
            return 2
        return 1

    by_name = {}
    for row in cleaned:
        key = row["name"].lower()
        best = by_name.get(key)
        if not best or score(row["href"]) > score(best["href"]):
            by_name[key] = row

    return list(by_name.values())


def _grab_from_panel(panel, brand_label, base_url):
    _click_all_show_more(panel)
    _force_lazy_render(panel)
    items = _collect_hotel_links(panel, base_url)
    rows = []
    for it in items:
        rows.append(
            {
                "hotel_name": it["name"],
                "hotel_url": it["href"],
                "group_label": brand_label,
                "group_type": "Brand",
            }
        )
    return rows


def scrape_desktop(page):
    rows = []
    container = page.locator("#HotelsByBrand")
    tablist = container.locator("[role='tablist'] button[role='tab']")
    if tablist.count() == 0:
        return rows
    for i in range(tablist.count()):
        btn = tablist.nth(i)
        label = _clean_text(btn.inner_text())
        panel_id = btn.get_attribute("aria-controls")
        btn.click()
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except TimeoutError:
            pass
        panel = (
            container.locator(f"#{panel_id}")
            if panel_id
            else container.locator("[role='tabpanel']").nth(i)
        )
        try:
            panel.wait_for(state="visible", timeout=7000)
        except TimeoutError:
            pass
        rows.extend(_grab_from_panel(panel, label, page.url))
    return rows


def scrape_mobile(page):
    rows = []
    container = page.locator("#HotelsByBrand")
    triggers = container.locator("[aria-controls^='radix-']")
    if triggers.count() == 0:
        return rows
    for i in range(triggers.count()):
        trig = triggers.nth(i)
        label = _clean_text(trig.inner_text())
        panel_id = trig.get_attribute("aria-controls")
        if not panel_id:
            continue
        panel = container.locator(f"#{panel_id}")
        if (trig.get_attribute("aria-expanded") or "").lower() != "true":
            trig.click()
        try:
            panel.wait_for(state="visible", timeout=7000)
        except TimeoutError:
            pass
        page.wait_for_timeout(300)
        rows.extend(_grab_from_panel(panel, label, page.url))
    return rows


# ----------------------------------------


def main(headless=True):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_navigation_timeout(60000)
        page.goto(URL, wait_until="domcontentloaded")

        rows = scrape_desktop(page)
        if not rows:
            rows = scrape_mobile(page)

        # Dedup by (hotel, brand) while preferring the "best" URL picked above
        dedup = {}
        for r in rows:
            key = (r["hotel_name"].lower(), r["group_label"].lower())
            dedup.setdefault(key, r)

        df = pd.DataFrame(
            dedup.values(), columns=["hotel_name", "hotel_url", "group_label", "group_type"]
        )

        # Preserve hotel_address and hotel_location already fetched in previous runs
        df["hotel_address"] = ""
        df["hotel_location"] = ""
        if Path(OUT).exists():
            try:
                existing = pd.read_csv(OUT)
                for col in ("hotel_address", "hotel_location"):
                    if col in existing.columns:
                        col_map = (
                            existing[existing[col].notna() & (existing[col] != "")]
                            .set_index("hotel_name")[col]
                            .to_dict()
                        )
                        df[col] = df["hotel_name"].map(col_map).fillna("")
            except Exception:
                pass

        df = backfill_addresses(df, context=context)
        df = df.sort_values(by=["group_label", "hotel_name"])
        df.to_csv(OUT, index=False)
        print(f"Wrote {len(df)} rows -> {OUT}")

        # -------- Compare against geocode cache --------
        cache_index, cache_entries = load_geocode_cache_index(CACHE_FILE)
        if not cache_entries and not cache_index.get("__official__"):
            print(f"No cache index built (file missing or empty: {CACHE_FILE}).")
            browser.close()
            return

        # Index scraped hotels by brand (scope_key) for reverse lookup
        scraped_index: Dict[str, set] = {}
        for _, row in df.iterrows():
            scope = _clean_text(row["group_label"]).lower()
            name = _clean_text(row["hotel_name"]).lower()
            scraped_index.setdefault(scope, set()).add(name)

        # 1) Classify each scraped hotel against the cache
        new_hotels: List[Any] = []
        renamed_hotels: List[Any] = []  # (scraped_name, cached_name, group_label)
        for _, row in df.iterrows():
            if not is_hotel_in_cache(row["hotel_name"], row["group_label"], cache_index):
                new_hotels.append(row)
                continue

            match = find_cache_match(row["hotel_name"], cache_entries, row["group_label"])
            if match is not None:
                cached_canonical = match[0]
                if _clean_text(row["hotel_name"]).lower() != cached_canonical:
                    renamed_hotels.append((row["hotel_name"], cached_canonical, row["group_label"]))

        if not new_hotels:
            print("All scraped hotels appear to be present in geocode cache.")
        else:
            print("Hotels NOT found in geocode cache (new hotels):")
            for r in new_hotels:
                print(f"- {r['hotel_name']}  [brand: {r['group_label']}]  -> {r['hotel_url']}")

        if renamed_hotels:
            print("Hotels with possible name changes (already geocoded, no re-geocoding needed):")
            for scraped, cached, brand in sorted(renamed_hotels, key=lambda x: (x[2], x[0])):
                print(f"- [{brand}] cached: {cached!r}  ->  scraped: {scraped!r}")

        # 2) Cached hotels NOT in scraped list (removed)
        removed_entries = [
            entry for entry in cache_entries
            if not is_cached_hotel_in_scraped(entry[0], entry[1], scraped_index)
        ]
        removed_hotels: List[Tuple[str, str]] = []
        seen_removed = set()
        for name, scope, _, _path in sorted(removed_entries, key=lambda x: (x[1], x[0])):
            key = (scope, name)
            if key in seen_removed:
                continue
            seen_removed.add(key)
            removed_hotels.append((name, scope))

        if not removed_entries:
            print("No cached hotels appear to have been removed from the current list.")
        else:
            print("Hotels in geocode cache but NOT in current scraped list (removed):")
            for name, scope in removed_hotels:
                print(f"- {name}  [brand: {scope}]")

        # 3) Update cache with new hotels
        if new_hotels:
            print(f"\nUpdating cache with {len(new_hotels)} new hotels...")
            new_keys = [
                (make_placeholder_key(_clean_text(h["hotel_name"]), _clean_text(h["group_label"]), "hilton"), None)
                for h in new_hotels
            ]
            added = update_cache_file(CACHE_FILE, new_keys)
            if added > 0:
                print(f"✓ Added {added} new hotel(s) to cache")
            else:
                print("⚠ Failed to add hotels to cache (check file permissions)")

        # 4) Remove hotels from cache that are no longer scraped
        if removed_entries:
            print(
                f"\nRemoving {len(removed_entries)} cache entries "
                f"for {len(removed_hotels)} removed hotels..."
            )
            removed = remove_hotels_from_cache(removed_entries)
            if removed > 0:
                print(f"✓ Removed {removed} hotel(s) from cache")
            else:
                print("⚠ Failed to remove hotels from cache (check file permissions)")

        browser.close()


if __name__ == "__main__":
    import sys

    headed = "--headed" in sys.argv
    main(headless=not headed)

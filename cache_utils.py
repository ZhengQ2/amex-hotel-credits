"""Shared geocode-cache utilities used by hilton.py and fhrthc.py.

Cache key format (JSON, keys sorted):
  {
    "brand":        <str>  # scope identifier (brand name for Hilton, program for FHR/THC)
    "hotel_name":   <str>  # official scraped name (new-style entries only)
    "input_format": <str>  # "hilton" or "fhrthc"
    "provider":     "google_places_first"
    "queries":      [<str>, ...]
    "v":            3
  }

Cache lookup uses similarity/fuzzy matching so that minor hotel name changes
(punctuation, minor additions, typos) do not trigger unnecessary re-geocoding.
The stored hotel_name is compared separately — an exact mismatch on that field
signals a potential rebrand for human review, without blocking cache reuse.

Old-style entries (without hotel_name) fall back to fuzzy query-string matching.
"""

import json
import os
import re
import unicodedata
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

CACHE_VERSION = 3

DEFAULT_GENERIC_TOKENS: FrozenSet[str] = frozenset({
    "and", "the", "at", "by", "&", "of",
})
MIN_FUZZY_MATCH_TOKENS = 2


def _clean_text(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("®", "").replace("™", "")
    s = s.replace("\u2013", "-").replace("\u2014", "-").replace("\u2019", "'")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _names_match(
    name1: str,
    name2: str,
    generic_tokens: FrozenSet[str] = DEFAULT_GENERIC_TOKENS,
) -> bool:
    """Fuzzy similarity check between two hotel names.

    Returns True when the names are plausibly the same physical hotel —
    allowing for minor name changes, punctuation differences, and benign
    suffix/prefix additions (e.g. adding a city qualifier).

    This is intentionally conservative: a genuine rebrand (e.g. a hotel
    switching loyalty programmes and acquiring a completely different set
    of meaningful tokens) will correctly return False, causing the hotel
    to appear as both "new" and "removed" for human review.
    """
    n1 = _clean_text(name1).lower()
    n2 = _clean_text(name2).lower()
    if not n1 or not n2:
        return False
    if n1 == n2:
        return True

    def tokens(s: str) -> Set[str]:
        return {
            t for t in re.findall(r"[a-z0-9]+", s)
            if t not in generic_tokens and len(t) > 1
        }

    t1 = tokens(n1)
    t2 = tokens(n2)
    if not t1 or not t2:
        return False
    if t1 == t2:
        return True

    overlap = t1 & t2
    if len(overlap) < MIN_FUZZY_MATCH_TOKENS:
        return False

    smaller_set, larger_set = (t1, t2) if len(t1) <= len(t2) else (t2, t1)
    if smaller_set.issubset(larger_set):
        return (len(larger_set) - len(smaller_set)) <= 2

    return len(overlap) / len(t1) >= 0.8 and len(overlap) / len(t2) >= 0.8


def _match_quality(
    left: str,
    right: str,
    generic_tokens: FrozenSet[str] = DEFAULT_GENERIC_TOKENS,
) -> Optional[Tuple[int, int, int, int, int]]:
    """Return a sortable quality tuple for a fuzzy name match, or None."""
    l = _clean_text(left).lower()
    r = _clean_text(right).lower()
    if not l or not r:
        return None
    if l == r:
        return (1, 999, 999, 0, 0)

    def tokens(s: str) -> Set[str]:
        return {
            t for t in re.findall(r"[a-z0-9]+", s)
            if t not in generic_tokens and len(t) > 1
        }

    lt = tokens(l)
    rt = tokens(r)
    overlap = lt & rt
    if not _names_match(left, right, generic_tokens=generic_tokens):
        return None
    smaller = min(len(lt), len(rt))
    larger = max(len(lt), len(rt))
    return (
        0,
        len(overlap),
        1 if (lt.issubset(rt) or rt.issubset(lt)) else 0,
        -abs(larger - smaller),
        -abs(len(l) - len(r)),
    )


def load_geocode_cache_index(
    *cache_paths: str,
) -> Tuple[Dict[str, Any], List[Tuple[str, str, str, str]]]:
    """Load one or more geocode cache files and return (index, entries).

    index layout:
      "__official__": {scope_key: set(name_lower), "__any__": set(name_lower)}
          — fuzzy matching pool for new-style entries (have hotel_name field)
      "__queries__": {scope_key: set(query_lower), "__any__": set(query_lower)}
          — fuzzy fallback pool for old-style entries that only store queries

    entries: [(canonical_name, scope_key, cache_key_str, cache_file_path), ...]
        canonical_name  — lowercased official name (new-style) or shortest query (old-style)
        scope_key       — meta["brand"].lower() from the cache key JSON
        cache_key_str   — original JSON string used as the dict key in the cache file
        cache_file_path — which file this entry lives in (for targeted removal/update)
    """
    official_index: Dict[str, Set[str]] = {}
    query_index: Dict[str, Set[str]] = {"__any__": set()}
    entries: List[Tuple[str, str, str, str]] = []

    for cache_path in cache_paths:
        if not os.path.exists(cache_path):
            continue
        with open(cache_path, "r", encoding="utf-8") as f:
            cache = json.load(f)

        for key in cache.keys():
            try:
                meta = json.loads(key)
            except Exception:
                continue

            scope_key = _clean_text(meta.get("brand", "")).lower()
            hotel_name_field = meta.get("hotel_name", "")

            if hotel_name_field:
                name = _clean_text(hotel_name_field).lower()
                official_index.setdefault("__any__", set()).add(name)
                if scope_key:
                    official_index.setdefault(scope_key, set()).add(name)
                canonical = name
            else:
                queries_raw = meta.get("queries", []) or []
                queries_clean = [_clean_text(q).lower() for q in queries_raw if q]
                if not queries_clean:
                    continue
                query_index["__any__"].update(queries_clean)
                if scope_key:
                    query_index.setdefault(scope_key, set()).update(queries_clean)
                canonical = min(queries_clean, key=len)

            entries.append((canonical, scope_key, key, cache_path))

    return {
        "__official__": official_index,
        "__queries__": query_index,
    }, entries


def _scoped_candidates(pool: Dict[str, Set[str]], scope_key: str) -> Set[str]:
    scoped = set(pool.get(scope_key, set())) if scope_key else set()
    if scoped:
        return scoped
    return set(pool.get("__any__", set()))


def is_hotel_in_cache(hotel_name: str, scope_key: str, cache_index: Dict) -> bool:
    """Return True if hotel_name has a fuzzy match in the cache.

    Uses similarity matching against stored official hotel names so that minor
    name changes do not trigger unnecessary re-geocoding. Falls back to
    query-string matching for old-style entries that predate hotel_name storage.
    """
    name = _clean_text(hotel_name).lower()
    sk = _clean_text(scope_key).lower()

    official = cache_index.get("__official__", {})
    official_names = _scoped_candidates(official, sk)
    if any(_names_match(name, n) for n in official_names):
        return True

    queries = cache_index.get("__queries__", {})
    scoped_queries = _scoped_candidates(queries, sk)
    return any(_names_match(name, q) for q in scoped_queries)


def find_cache_match(
    hotel_name: str,
    cache_entries: List[Tuple[str, str, str, str]],
    scope_key: str = "",
) -> Optional[Tuple[str, str, str, str]]:
    """Return the first cache entry that fuzzily matches hotel_name, or None.

    Used by scrapers to distinguish three cases:
      - None returned          → genuinely new hotel, needs geocoding
      - entry returned, canonical == cleaned scraped name → unchanged
      - entry returned, canonical != cleaned scraped name → possible rename/rebrand
    """
    target_scope = _clean_text(scope_key).lower()
    best_entry: Optional[Tuple[str, str, str, str]] = None
    best_quality: Optional[Tuple[int, int, int, int, int]] = None

    for entry in cache_entries:
        canonical, entry_scope, cache_key, cache_path = entry
        try:
            meta = json.loads(cache_key)
        except Exception:
            continue

        cached_hotel_name = _clean_text(meta.get("hotel_name", "")).lower()
        if not cached_hotel_name:
            continue
        if target_scope and entry_scope and entry_scope != target_scope:
            continue

        quality = _match_quality(hotel_name, cached_hotel_name)
        if quality is None:
            continue
        if best_quality is None or quality > best_quality:
            best_quality = quality
            best_entry = (cached_hotel_name, entry_scope, cache_key, cache_path)

    return best_entry


def find_fuzzy_cache_key(
    hotel_name: str,
    cache: Dict[str, Any],
    *,
    input_format: str = "",
) -> Optional[str]:
    """Search a raw cache dict for a key whose hotel_name fuzzily matches.

    Used by google-convert.py to reuse already-geocoded coordinates when the
    hotel's name has changed slightly, avoiding an unnecessary API call.
    Falls back to old-style query-array matching because most existing cache
    entries still predate the hotel_name field.
    Returns the matching key string, or None.
    """
    best_key: Optional[str] = None
    best_quality: Optional[Tuple[int, int, int, int, int]] = None

    for key in cache:
        try:
            meta = json.loads(key)
        except Exception:
            continue
        cached_format = _clean_text(meta.get("input_format", "")).lower()
        if input_format and cached_format and cached_format != _clean_text(input_format).lower():
            continue

        candidates: Iterable[str]
        cached_hotel_name = meta.get("hotel_name", "")
        if cached_hotel_name:
            candidates = [cached_hotel_name]
        else:
            candidates = meta.get("queries", []) or []

        for candidate in candidates:
            quality = _match_quality(hotel_name, candidate)
            if quality is None:
                continue
            if best_quality is None or quality > best_quality:
                best_quality = quality
                best_key = key

    return best_key


def is_cached_hotel_in_scraped(
    cached_name: str,
    scope_key: str,
    scraped_index: Dict[str, Set[str]],
) -> bool:
    """Return True if cached_name appears in the current scraped list for scope_key.

    scraped_index maps scope_key -> set of lowercased hotel names.
    Falls back to all scopes when scope_key is empty.
    Exact match first (fast path), then fuzzy fallback.
    """
    name = _clean_text(cached_name).lower()
    sk = _clean_text(scope_key).lower()

    candidates: Set[str] = (
        scraped_index.get(sk, set()) if sk
        else set().union(*scraped_index.values()) if scraped_index
        else set()
    )
    if name in candidates:
        return True
    return any(_names_match(name, s) for s in candidates)


def update_cache_file(cache_path: str, new_keys: List[Tuple[str, Any]]) -> int:
    """Append (key, value) pairs to a single cache file. Skips keys already present.

    Returns the number of new entries actually written.
    """
    if not new_keys:
        return 0
    try:
        if os.path.exists(cache_path):
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
        else:
            cache = {}

        added = 0
        for key, value in new_keys:
            if key not in cache:
                cache[key] = value
                added += 1

        if added > 0:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        return added
    except PermissionError:
        print(f"ERROR: Permission denied writing to {cache_path}")
        return 0
    except Exception as e:
        print(f"ERROR: Failed to update cache {cache_path}: {e}")
        return 0


def remove_hotels_from_cache(
    removed_entries: List[Tuple[str, str, str, str]],
) -> int:
    """Remove cache entries identified by (name, scope, key, path) 4-tuples.

    Groups removals by file path and removes from each file independently.
    Returns the total number of entries removed across all files.
    """
    if not removed_entries:
        return 0

    by_file: Dict[str, Set[str]] = {}
    for _, _, key, cache_path in removed_entries:
        by_file.setdefault(cache_path, set()).add(key)

    total_removed = 0
    for cache_path, keys_to_remove in by_file.items():
        try:
            if not os.path.exists(cache_path):
                continue
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
            before = len(cache)
            cache = {k: v for k, v in cache.items() if k not in keys_to_remove}
            removed = before - len(cache)
            if removed > 0:
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(cache, f, ensure_ascii=False, indent=2)
            total_removed += removed
        except PermissionError:
            print(f"ERROR: Permission denied writing to {cache_path}")
        except Exception as e:
            print(f"ERROR: Failed to remove from cache {cache_path}: {e}")

    return total_removed


def make_placeholder_key(hotel_name: str, brand: str, input_format: str) -> str:
    """Build the cache key JSON string for a new hotel placeholder entry.

    The placeholder stores the official hotel name up front so later geocoding
    runs can reuse it through fuzzy lookup even before the full query list is
    known.
    """
    return json.dumps(
        {
            "brand": brand,
            "hotel_name": hotel_name,
            "input_format": input_format,
            "provider": "google_places_first",
            "queries": [hotel_name],
            "v": CACHE_VERSION,
        },
        sort_keys=True,
    )

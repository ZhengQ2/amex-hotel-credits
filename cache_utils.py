"""Shared geocode-cache and hotel-matching utilities used across the pipeline.

Cache key format (JSON, keys sorted):
  {
    "brand":        <str>  # scope identifier (brand name for Hilton, program for FHR/THC)
    "hotel_name":   <str>  # official scraped name (new-style entries only)
    "input_format": <str>  # "hilton" or "fhrthc"
    "provider":     "google_places_first"
    "queries":      [<str>, ...]
    "v":            3
  }

Cache lookup uses diacritic-folded, affiliation-aware similarity matching so
minor hotel name edits (accents, punctuation, chain/collection taglines) reuse
existing geocodes without collapsing distinct sibling properties or
cross-city chains.
"""

from difflib import SequenceMatcher
from functools import lru_cache
import json
import os
import re
import unicodedata
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

CACHE_VERSION = 3

DEFAULT_GENERIC_TOKENS: FrozenSet[str] = frozenset({
    "a", "an", "and", "the", "at", "by", "&", "of", "in", "on", "to", "for",
    "de", "du", "la", "le", "les", "del", "da", "di", "el", "en",
})

GENERIC_HOSPITALITY_TOKENS: FrozenSet[str] = frozenset({
    "hotel", "hotels", "resort", "resorts", "spa", "inn",
    "luxury", "boutique", "collection", "club", "property", "brand",
    "member", "part", "world", "wellness", "experience", "convention", "center",
})

# Tokens too generic to anchor a 1-core-token subset match on their own.
_WEAK_SINGLE_CORE_TOKENS: FrozenSet[str] = frozenset({
    "new", "one", "two", "san", "las", "los", "the", "grand", "park",
    "west", "east", "north", "south", "city", "beach", "island", "bay",
    "house", "palace", "plaza", "royal", "tower", "club", "lodge", "villa",
    "manor", "inn", "garden", "lake", "river", "ocean", "sea", "view",
})

# Sub-property, wing, and directional qualifiers that distinguish distinct
# sibling properties when one name has them and the other does not (e.g.
# "Four Seasons Hotel New York" vs "Four Seasons Hotel New York Downtown",
# "Zadun ... Reserve" vs "Zadun ... Reserve Residence").
DISCRIMINATOR_TOKENS: FrozenSet[str] = frozenset({
    "downtown", "midtown", "uptown", "north", "south", "east", "west",
    "airport", "residence", "residences", "villa", "villas", "tower", "cloister",
    "yacht", "mare", "reserve", "annex", "estates", "palm", "walk",
    "galleria", "marunouchi", "otemachi", "beverly", "westlake", "dolphin", "swan",
    "ocean", "harbor", "harbour", "bay", "marina", "coral", "royal",
    "zilara", "ziva", "suites", "suite",
})

# Google Places types that represent non-lodging POIs when "lodging" is absent.
# Real hotels that also have an on-site restaurant/parking/bar carry "lodging"
# and remain accepted because "lodging" is checked first.
NON_LODGING_NOISE_TYPES: FrozenSet[str] = frozenset({
    "parking",
    "bus_station",
    "transit_station",
    "subway_station",
    "train_station",
    "atm",
    "gas_station",
    "natural_feature",
    "restaurant",
    "food",
    "store",
    "cafe",
    "bar",
})

MIN_FUZZY_MATCH_TOKENS = 2

_CHAR_TRANSLATIONS = str.maketrans({
    "ø": "o", "Ø": "o",
    "ł": "l", "Ł": "l",
    "đ": "d", "Đ": "d",
    "ß": "ss",
    "æ": "ae", "Æ": "ae",
    "œ": "oe", "Œ": "oe",
    "\u2018": "'", "\u2019": "'", "\u02bc": "'", "`": "'",
    "\u2013": "-", "\u2014": "-",
})

_NUM_WORDS: Dict[str, str] = {
    "1": "one", "2": "two", "3": "three", "4": "four", "5": "five",
    "6": "six", "7": "seven", "8": "eight", "9": "nine", "10": "ten",
    "11": "eleven", "12": "twelve",
}

# Common chain/collection/award taglines that AMEX frequently appends or drops
# on otherwise unchanged properties. Trailing "by <brand>" is anchored to the
# end of the string so mid-name brand phrases like "Signia by Hilton Orlando"
# retain "hilton" and still match "Signia Hilton Orlando".
_AFFILIATION_PATTERNS = [
    r"\b(?:a\s+)?member\s+of\s+design\s+hotels\b",
    r"\b(?:in\s+)?(?:the\s+)?unbound\s+collection\s+by\s+hyatt\b",
    r"\b(?:part\s+of\s+)?destination\s+by\s+hyatt\b",
    r"\b(?:a\s+)?luxury\s+collection\s+(?:desert\s+)?(?:hotel|resort)(?:\s*(?:&|and)\s*spa)?\b",
    r"\b(?:a\s+|the\s+)?leading\s+hotels?\s+of\s+the\s+world\b",
    r"\b(?:a\s+member\s+of\s+)?small\s+luxury\s+hotels?(?:\s+of\s+the\s+world)?\b",
    r"\brelais\s*(?:&|and)\s*chateaux\b",
    r"\bdorchester\s+collection\b",
    r"\b(?:a\s+)?rosewood\s+(?:hotel|resort)\b",
    r"\b(?:a\s+)?belmond\s+hotel\b",
    r"\b(?:a\s+)?tribute\s+portfolio\s+hotel\b",
    r"\bautograph\s+collection\b",
    r"\bcurio\s+(?:collection\s+)?by\s+hilton\b",
    r"\btapestry\s+collection\s+by\s+hilton\b",
    r"\bvignette\s+collection\s+by\s+ihg\b",
    r"\blxr\s+hotels?\s*(?:&|and)\s*resorts?\b",
    r"\b(?:a\s+)?paris\s+society\s+collection\s+hotel\b",
    r"\b(?:the\s+|member\s+of\s+)?melia\s+collection\b",
    r"\bmgallery\s+collection\b",
    r"\bpartner\s+of\s+all\s+accor\b",
    r"\bmichelin\s+key\s+award\s+hotel\b",
    r"\ba\s+marriott\s+hotel\b",
    r"\ba\s+hyatt\s+(?:luxury\s+)?(?:hotel|resort|brand)\b",
    r"\ba\s+noble\s+house\s+resort\b",
    r"\ba\s+shangri\s*la\s+hotel\b",
    r"\ba\s+hilton\s+resort\b",
    r"(?:[,\-\s]+by\s+(?:ihg|hyatt|hilton|marriott|melia|palisociety|library\s+hotel\s+collection))\s*$",
    r"\badults?\s*(?:-|\s)?\s*(?:only|friendly)\b",
    r"\ball\s*(?:-|\s)?\s*inclusive(?:\s+experience)?\b",
    r"\ball\s+parks\s*/?\s*all\s+fun\s+inclusive\b",
    r"\bworldhotels\s+luxury\b",
    r"\bpreferred\s+hotels?\s*(?:&|and)\s*resorts?\b",
    r"\bsavoy\s+signature\b",
    r"\bforbes\s+five\s+star\b",
]
_AFFILIATION_RE = re.compile("|".join(_AFFILIATION_PATTERNS), re.IGNORECASE)

# Canonical country names for common spellings/aliases/constituent countries.
_COUNTRY_ALIASES: Dict[str, str] = {
    "us": "united states", "usa": "united states", "america": "united states",
    "united states of america": "united states",
    "uk": "united kingdom", "britain": "united kingdom", "great britain": "united kingdom",
    "england": "united kingdom", "scotland": "united kingdom",
    "wales": "united kingdom", "northern ireland": "united kingdom",
    "uae": "united arab emirates",
    "turkiye": "turkey", "turkiy": "turkey",
    "czechia": "czech republic",
    "holland": "netherlands",
    "south korea": "korea", "republic of korea": "korea",
    "macau": "macao",
    "ca": "canada", "mx": "mexico",
}

_TEXT_COUNTRY_EXPANSIONS: Dict[str, str] = {
    k: v for k, v in _COUNTRY_ALIASES.items() if k not in ("ca", "mx")
}

# Common English exonym <-> local endonym city equivalents seen between AMEX
# scraped locations and Google formatted_address values.
_CITY_ALIASES: Dict[str, Tuple[str, ...]] = {
    "florence": ("firenze",),
    "athens": ("athina",),
    "brussels": ("bruxelles", "brussel"),
    "rome": ("roma",),
    "venice": ("venezia",),
    "milan": ("milano",),
    "naples": ("napoli",),
    "turin": ("torino",),
    "genoa": ("genova",),
    "vienna": ("wien",),
    "munich": ("munchen",),
    "cologne": ("koln",),
    "nuremberg": ("nurnberg",),
    "prague": ("praha",),
    "warsaw": ("warszawa",),
    "krakow": ("cracow",),
    "lisbon": ("lisboa",),
    "seville": ("sevilla",),
    "copenhagen": ("kobenhavn",),
    "gothenburg": ("goteborg",),
    "bangkok": ("krung thep",),
    "lucerne": ("luzern",),
    "geneva": ("geneve",),
    "basel": ("bale",),
    "antwerp": ("antwerpen",),
    "ghent": ("gent",),
    "bruges": ("brugge",),
    "mykonos": ("mikonos",),
    "santorini": ("thira", "oia", "fira", "imerovigli"),
    "saint julians": ("st julians", "san giljan"),
    "st julians": ("saint julians", "san giljan"),
}

_CITY_SUFFIX_RE = re.compile(
    r"\s+(?:island|city|beach|mare|bay|by the sea|metropolitan area)$"
)

# US state and Canadian province full-name -> postal code mapping used to guard
# against cross-state/province cache hits (e.g. Florida vs Texas within "US").
_SUBREGION_CODES: Dict[str, str] = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "district of columbia": "dc", "florida": "fl", "georgia": "ga", "hawaii": "hi",
    "idaho": "id", "illinois": "il", "indiana": "in", "iowa": "ia",
    "kansas": "ks", "kentucky": "ky", "louisiana": "la", "maine": "me",
    "maryland": "md", "massachusetts": "ma", "michigan": "mi", "minnesota": "mn",
    "mississippi": "ms", "missouri": "mo", "montana": "mt", "nebraska": "ne",
    "nevada": "nv", "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm",
    "new york": "ny", "north carolina": "nc", "north dakota": "nd", "ohio": "oh",
    "oklahoma": "ok", "oregon": "or", "pennsylvania": "pa", "rhode island": "ri",
    "south carolina": "sc", "south dakota": "sd", "tennessee": "tn", "texas": "tx",
    "utah": "ut", "vermont": "vt", "virginia": "va", "washington": "wa",
    "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy",
    "alberta": "ab", "british columbia": "bc", "manitoba": "mb",
    "new brunswick": "nb", "newfoundland and labrador": "nl", "nova scotia": "ns",
    "ontario": "on", "prince edward island": "pe", "quebec": "qc",
    "saskatchewan": "sk",
}
_VALID_SUBREGION_CODES: FrozenSet[str] = frozenset(_SUBREGION_CODES.values())

# The FHR/THC scope field is written under two different conventions:
# fhrthc.py records placeholders for newly-seen hotels using the program bucket
# ("FHR"/"THC"), while google-convert.py records the real geocoded entry using
# the scraped programme label ("FINE HOTELS + RESORTS"/"THE HOTEL COLLECTION").
# Fold them together so scoped lookups see both.
_SCOPE_ALIASES = {
    "fine hotels + resorts": "fhr",
    "fine hotels and resorts": "fhr",
    "fine hotels & resorts": "fhr",
    "fhr": "fhr",
    "the hotel collection": "thc",
    "hotel collection": "thc",
    "thc": "thc",
}
_PROGRAM_SCOPES: FrozenSet[str] = frozenset({"fhr", "thc"})


def is_strict_lodging_type(types: Iterable[str]) -> bool:
    """Return True when Google Places explicitly classifies the place as lodging."""
    return "lodging" in {str(x).strip().lower() for x in (types or []) if x}


def is_lodging_type(types: Iterable[str]) -> bool:
    """Return True for lodging places or non-noise premises/establishments."""
    t = {str(x).strip().lower() for x in (types or []) if x}
    if "lodging" in t:
        return True
    if t & NON_LODGING_NOISE_TYPES:
        return False
    return "premise" in t or "establishment" in t


def _normalize_scope(scope: str) -> str:
    """Canonical scope key, folding the FHR/THC programme-label variants."""
    s = _clean_text(scope).lower()
    return _SCOPE_ALIASES.get(s, s)


def _clean_text(s: str) -> str:
    """Light display-preserving cleanup (retains accents for official names)."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("®", "").replace("™", "")
    s = s.replace("\u2013", "-").replace("\u2014", "-").replace("\u2019", "'")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def fold_ascii(s: str) -> str:
    """Strip diacritics and fold special Latin letters/quotes to ASCII."""
    if not s:
        return ""
    s = s.translate(_CHAR_TRANSLATIONS)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.replace("®", "").replace("™", "")


@lru_cache(maxsize=16384)
def normalize_name(s: str) -> str:
    """Canonical ASCII, punctuation- and accent-insensitive name form.

    Strips English possessive "'s" (Universal's == Universal), collapses other
    apostrophes (Xi'an == Xian, Ho'olei == Hoolei), normalizes & -> and and
    St. -> saint, and preserves single-character brand tokens such as 'w'
    ('W Rome') and '1' ('1 Hotel').
    """
    s = fold_ascii(s).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"'s\b", "", s)
    s = re.sub(r"'", "", s)
    s = re.sub(r"\bst\.?\b", "saint", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _normalize_geo_text(s: str) -> str:
    """Normalize geographic text (countries/territories/addresses) consistently.

    Shared by both scraped hotel_location country parsing and Google
    formatted_address matching so U.S. Virgin Islands == USVI, St. == Saint,
    USA/UK/UAE expand consistently, and diacritics are stripped identically.
    """
    s = fold_ascii(s).replace("&", " and ").lower()
    s = re.sub(r"[^a-z\s]", " ", s)
    s = re.sub(r"\bu\s+s\b", "us", s)
    s = re.sub(r"\busvi\b", "us virgin islands", s)
    s = re.sub(r"\bst\b", "saint", s)
    for alias, canonical in _TEXT_COUNTRY_EXPANSIONS.items():
        s = re.sub(rf"\b{re.escape(alias)}\b", canonical, s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_location_parts(loc: str) -> Tuple[str, str, str]:
    """Parse scraped hotel_location into (city, region, canonical_country).

    Separates city from state/province so a shared state (e.g. 'Austin, Texas US'
    vs 'Houston, Texas US') never satisfies city-level compatibility.
    """
    if not loc:
        return "", "", ""
    parts = [p.strip() for p in loc.split(",") if p.strip()]
    if not parts:
        return "", "", ""

    last = parts[-1]
    # Domestic locations append an ALL-CAPS 2-3 letter country code directly
    # after the state/province without a comma (e.g. "Austin, Texas US",
    # "San José del Cabo, Baja California Sur MX").
    trailing_code = re.search(r"\b([A-Za-z]{2,3})\s*$", last)
    if trailing_code and trailing_code.group(1).isupper():
        code = trailing_code.group(1).lower()
        if code in _COUNTRY_ALIASES:
            country = _COUNTRY_ALIASES[code]
            region_head = last[:trailing_code.start()].strip()
            if len(parts) >= 2:
                city = normalize_name(parts[0])
                mid = [p for p in (*parts[1:-1], region_head) if p]
                region = normalize_name(" ".join(mid))
            else:
                city = normalize_name(region_head)
                region = ""
            return city, region, country

    country_norm = _normalize_geo_text(last)
    country = _COUNTRY_ALIASES.get(country_norm, country_norm)
    if len(parts) >= 3:
        city = normalize_name(parts[0])
        region = normalize_name(" ".join(parts[1:-1]))
    elif len(parts) == 2:
        city = normalize_name(parts[0])
        region = ""
    else:
        city = ""
        region = ""
    return city, region, country


def normalize_location(loc: str) -> Tuple[str, str]:
    """Return (normalized_city, canonical_country) for a scraped hotel_location."""
    city, _, country = normalize_location_parts(loc)
    return city, country


def _cities_compatible(city1: str, city2: str) -> bool:
    """Return True when two normalized city names refer to the same city/locality."""
    if not city1 or not city2:
        return False
    if city1 == city2 or city1 in city2 or city2 in city1:
        return True
    if city2 in _CITY_ALIASES.get(city1, ()) or city1 in _CITY_ALIASES.get(city2, ()):
        return True
    s1 = _CITY_SUFFIX_RE.sub("", city1).strip()
    s2 = _CITY_SUFFIX_RE.sub("", city2).strip()
    if s1 and s2 and (s1 == s2 or s1 in s2 or s2 in s1):
        return True
    t1, t2 = set(city1.split()), set(city2.split())
    if t1 & t2 and len(t1 & t2) / min(len(t1), len(t2)) >= 0.5:
        return True
    return SequenceMatcher(None, city1, city2).ratio() >= 0.80


def locations_compatible(loc1: str, loc2: str) -> bool:
    """Return True when two non-empty scraped hotel_locations match at city level.

    Requires both locations to be non-empty so missing locations (e.g. Hilton
    CSVs) never pass as a city match, and compares city separately from state/
    region so different cities in the same state (Austin vs Houston) return False.
    """
    if not (loc1 or "").strip() or not (loc2 or "").strip():
        return False
    city1, reg1, co1 = normalize_location_parts(loc1)
    city2, reg2, co2 = normalize_location_parts(loc2)
    if co1 and co2 and co1 != co2:
        return False
    if reg1 and reg2 and reg1 != reg2:
        return False
    if city1 and city2:
        return _cities_compatible(city1, city2)
    # Country/region-only locations (no city component on either side)
    return bool(co1 and co1 == co2 and (not city1 and not city2))


def _extract_subregion_from_location(loc: str) -> str:
    """Extract US state / Canadian province 2-letter code from scraped location."""
    if not loc:
        return ""
    _, region, country = normalize_location_parts(loc)
    if country not in ("united states", "canada"):
        return ""
    if region in _SUBREGION_CODES:
        return _SUBREGION_CODES[region]
    if region in _VALID_SUBREGION_CODES:
        return region
    return ""


def _extract_subregion_from_address(formatted_address: str) -> str:
    """Extract US state / Canadian province 2-letter code from Google formatted_address."""
    if not formatted_address:
        return ""
    # Matches ", FL 33040, USA", ", CA, USA", ", QC G1R 5J5, Canada", etc.
    for match in re.finditer(r",\s*([A-Z]{2})(?:\s+[A-Z0-9][A-Z0-9 -]*)?(?=,|$)", formatted_address):
        code = match.group(1).lower()
        if code in _VALID_SUBREGION_CODES:
            return code
    return ""


def address_location_compatible(formatted_address: str, hotel_location: str) -> bool:
    """Return False when a formatted_address disagrees on country or US/CA state/province."""
    if not formatted_address or not hotel_location:
        return True
    _, _, loc_country = normalize_location_parts(hotel_location)
    if not loc_country:
        return True
    addr_norm = _normalize_geo_text(formatted_address)
    country_ok = loc_country in addr_norm or (
        loc_country == "china" and ("hong kong" in addr_norm or "macao" in addr_norm)
    )
    if not country_ok:
        return False

    # Within US/Canada, also guard against cross-state/province collisions
    # (e.g. Florida vs Texas, British Columbia vs Quebec).
    loc_sub = _extract_subregion_from_location(hotel_location)
    addr_sub = _extract_subregion_from_address(formatted_address)
    if loc_sub and addr_sub and loc_sub != addr_sub:
        return False
    return True


def _city_needles(city: str) -> List[str]:
    """Return normalized city strings (including stem and endonyms) to search for."""
    if not city:
        return []
    needles: List[str] = [city]
    stem = _CITY_SUFFIX_RE.sub("", city).strip()
    if stem and len(stem) >= 3 and stem != city:
        needles.append(stem)
    for alias in _CITY_ALIASES.get(city, ()):
        if alias not in needles:
            needles.append(alias)
    if stem:
        for alias in _CITY_ALIASES.get(stem, ()):
            if alias not in needles:
                needles.append(alias)
    return needles


def _haystack_has_phrase(haystack: str, phrase: str) -> bool:
    if not haystack or not phrase:
        return False
    return re.search(rf"(?:^| ){re.escape(phrase)}(?: |$)", haystack) is not None


def _candidate_city_compatible(
    hotel_location: str,
    formatted_address: str,
    queries: Iterable[str],
) -> bool:
    """Verify that a cached entry's address or queries agree with hotel_location's city."""
    if not hotel_location:
        return True
    city, _, _ = normalize_location_parts(hotel_location)
    if not city:
        return True
    haystack = normalize_name(formatted_address + " " + " ".join(queries or ()))
    if not haystack:
        return False
    for needle in _city_needles(city):
        if _haystack_has_phrase(haystack, needle):
            return True
    return False


def _no_result_location_compatible(hotel_location: str, queries: Iterable[str]) -> bool:
    """Verify a cached NO_RESULT entry belongs to the same country and city."""
    if not hotel_location:
        return True
    queries_list = list(queries or ())
    if not queries_list:
        return False
    joined = " ".join(queries_list)
    _, _, loc_country = normalize_location_parts(hotel_location)
    if loc_country:
        geo_hay = _normalize_geo_text(joined)
        country_ok = loc_country in geo_hay or (
            loc_country == "china" and ("hong kong" in geo_hay or "macao" in geo_hay)
        )
        if not country_ok:
            return False
    return _candidate_city_compatible(hotel_location, "", queries_list)


@lru_cache(maxsize=16384)
def _core_name_tokens_cached(
    name: str,
    generic_tokens: FrozenSet[str],
) -> Tuple[str, ...]:
    s = fold_ascii(name).lower().replace("&", " and ")
    s = re.sub(r"'s\b", "", s)
    s = re.sub(r"'", "", s)
    s = _AFFILIATION_RE.sub(" ", s)
    s = re.sub(r"\bst\.?\b", "saint", s)
    toks: List[str] = []
    for tok in re.findall(r"[a-z0-9]+", s):
        if len(tok) > 6 and tok.endswith("hotel"):
            tok = tok[:-5]
        elif len(tok) > 7 and tok.endswith("resort"):
            tok = tok[:-6]
        tok = _NUM_WORDS.get(tok, tok)
        if tok in generic_tokens or tok in GENERIC_HOSPITALITY_TOKENS:
            continue
        toks.append(tok)
    if not toks:
        toks = [
            t for t in re.findall(r"[a-z0-9]+", normalize_name(name))
            if t not in generic_tokens
        ]
    return tuple(toks)


def core_name_tokens(
    name: str,
    generic_tokens: FrozenSet[str] = DEFAULT_GENERIC_TOKENS,
) -> List[str]:
    """Extract ordered identity tokens after stripping diacritics and affiliation taglines."""
    return list(_core_name_tokens_cached(name, generic_tokens))


def align_compound_tokens(
    seq1: List[str],
    seq2: List[str],
) -> Tuple[Set[str], Set[str]]:
    """Align adjacent two-token splits against joined spellings (e.g. 'de'+'witt' <-> 'dewitt')."""
    t1, t2 = set(seq1), set(seq2)
    for seq, dst, src in ((seq1, t1, t2), (seq2, t2, t1)):
        for i in range(len(seq) - 1):
            comb = seq[i] + seq[i + 1]
            if comb in src:
                dst.discard(seq[i])
                dst.discard(seq[i + 1])
                dst.add(comb)
    return t1, t2


def _names_match(
    name1: str,
    name2: str,
    generic_tokens: FrozenSet[str] = DEFAULT_GENERIC_TOKENS,
    hotel_location: str = "",
    location_confirmed: bool = True,
) -> bool:
    """Fuzzy similarity check between two hotel names.

    Returns True when the names plausibly identify the same physical hotel —
    handling diacritics, punctuation, apostrophes, and benign affiliation
    taglines, while rejecting sibling properties separated by sub-property or
    directional qualifiers (e.g. Downtown, Residence, Villas, East/West).
    """
    return _match_quality(
        name1,
        name2,
        generic_tokens=generic_tokens,
        hotel_location=hotel_location,
        location_confirmed=location_confirmed,
    ) is not None


def _match_quality(
    left: str,
    right: str,
    generic_tokens: FrozenSet[str] = DEFAULT_GENERIC_TOKENS,
    hotel_location: str = "",
    location_confirmed: bool = True,
    allow_subset: bool = True,
    strict_discriminators: bool = True,
) -> Optional[Tuple[int, int, int, int, int]]:
    """Return a sortable quality tuple for a hotel name match, or None.

    Tier precedence (first element):
      3 - exact normalized match (case/accent/punctuation/apostrophe insensitive)
      2 - exact core identity match after stripping affiliation taglines
          (and optional benign location qualifiers when no conflicting tokens remain)
      1 - conservative subset / high-overlap match on unstripped core tokens
          with optional discriminator guard (disabled when allow_subset=False)
    """
    l_norm = normalize_name(left)
    r_norm = normalize_name(right)
    if not l_norm or not r_norm:
        return None
    if l_norm == r_norm:
        return (3, 999, 999, 0, -abs(len(left) - len(right)))

    l_seq = core_name_tokens(left, generic_tokens=generic_tokens)
    r_seq = core_name_tokens(right, generic_tokens=generic_tokens)
    if not l_seq or not r_seq:
        return None

    lt, rt = align_compound_tokens(l_seq, r_seq)
    if not lt or not rt:
        return None

    # Exact core match after stripping affiliation taglines (no location stripping needed).
    if lt == rt:
        return (2, len(lt), 1, 0, -abs(len(l_norm) - len(r_norm)))

    # Allow ignoring tokens from hotel_location ONLY when EVERY differing token
    # between lt and rt is explained by hotel_location (no conflicting city/word
    # remains on either side, e.g. "Akelarre" vs "Akelarre San Sebastian" in
    # "San Sebastián, Spain"), and never before subset/overlap matching —
    # otherwise stripping "austin" from "Four Seasons Hotel Austin" would turn
    # it into a subset of "Four Seasons Hotel Houston".
    loc_toks = set(normalize_name(hotel_location).split()) if hotel_location else set()
    sym_diff = lt ^ rt
    if loc_toks and sym_diff.issubset(loc_toks):
        shared_core = lt & rt
        # Only bridge a location-only difference when at least one core token is
        # shared AND either the candidate itself names the requested location
        # (rt - lt non-empty) or the candidate's cached address/queries agree on city.
        candidate_confirms_city = bool(rt - lt) or location_confirmed
        if shared_core and candidate_confirms_city:
            return (2, len(shared_core), 1, -len(sym_diff), -abs(len(l_norm) - len(r_norm)))

    if not allow_subset:
        return None

    # Never collapse two properties separated by a sub-property/directional qualifier
    # (Downtown, Residence, Villas, Mare, Palm, East/West, Suites, Zilara/Ziva, ...)
    # when strict_discriminators is enabled.
    if strict_discriminators and (sym_diff & DISCRIMINATOR_TOKENS):
        return None

    overlap = lt & rt
    if not overlap:
        return None
    # Require at least one non-location overlapping token so two hotels cannot
    # match solely on their shared city name (e.g. "Beverly Hills").
    if loc_toks and not (overlap - loc_toks):
        return None

    smaller_set, larger_set = (lt, rt) if len(lt) <= len(rt) else (rt, lt)
    if smaller_set.issubset(larger_set):
        diff = len(larger_set) - len(smaller_set)
        if diff <= 2:
            if len(overlap) >= MIN_FUZZY_MATCH_TOKENS:
                return (1, len(overlap), 1, -diff, -abs(len(l_norm) - len(r_norm)))
            # Support 1-core-token hotels (e.g. "Hotel De Rome" vs "Hotel De Rome Berlin",
            # "Loden Hotel" vs "Loden Boutique Hotel Vancouver", "Le Sereno" vs
            # "Le Sereno St. Barths") so cache pruning preserves their entries without
            # letting weak single tokens like "new" or "w" match unrelated hotels.
            single_tok = next(iter(overlap))
            if len(single_tok) >= 3 and single_tok not in _WEAK_SINGLE_CORE_TOKENS:
                raw_l = {t for t in l_norm.split() if t not in generic_tokens}
                raw_r = {t for t in r_norm.split() if t not in generic_tokens}
                if len(raw_l & raw_r) >= 2 or len(raw_l) == 1 or len(raw_r) == 1:
                    return (1, 1, 1, -diff, -abs(len(l_norm) - len(r_norm)))
        return None

    if len(overlap) < MIN_FUZZY_MATCH_TOKENS:
        return None

    if len(overlap) / len(lt) >= 0.8 and len(overlap) / len(rt) >= 0.8:
        return (
            1,
            len(overlap),
            0,
            -abs(len(larger_set) - len(smaller_set)),
            -abs(len(l_norm) - len(r_norm)),
        )
    return None


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
        scope_key       — normalized meta["brand"] from the cache key JSON
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

            scope_key = _normalize_scope(meta.get("brand", ""))
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
    """Return True if hotel_name has a fuzzy match in the cache."""
    name = _clean_text(hotel_name).lower()
    sk = _normalize_scope(scope_key)

    official = cache_index.get("__official__", {})
    official_names = _scoped_candidates(official, sk)
    if name in official_names or any(_names_match(name, n) for n in official_names):
        return True

    queries = cache_index.get("__queries__", {})
    scoped_queries = _scoped_candidates(queries, sk)
    return name in scoped_queries or any(_names_match(name, q) for q in scoped_queries)


def find_cache_match(
    hotel_name: str,
    cache_entries: List[Tuple[str, str, str, str]],
    scope_key: str = "",
) -> Optional[Tuple[str, str, str, str]]:
    """Return the best cache entry that fuzzily matches hotel_name, or None."""
    target_scope = _normalize_scope(scope_key)
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
        if (
            target_scope in _PROGRAM_SCOPES
            and entry_scope in _PROGRAM_SCOPES
            and entry_scope != target_scope
        ):
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
    hotel_location: str = "",
    scope_key: str = "",
) -> Optional[str]:
    """Search a raw cache dict for a key whose hotel_name fuzzily matches.

    Used by google-convert.py to reuse already-geocoded coordinates when the
    hotel's name or location formatting changed slightly, avoiding an
    unnecessary paid API call. Skips placeholder (None) entries and non-lodging
    noise results (e.g. parking lots), prefers resolved entries and matching
    scopes without blocking older brand-scoped FHR/THC entries or Hilton brand
    rebrands, and enforces country, US/CA state/province, and city agreement
    when hotel_location is provided.
    """
    target_scope = _normalize_scope(scope_key)
    target_format = _clean_text(input_format).lower()
    best_key: Optional[str] = None
    best_rank: Optional[Tuple[int, int, int, int, int, int, int, int]] = None

    for key, value in cache.items():
        if value is None:
            continue
        try:
            meta = json.loads(key)
        except Exception:
            continue

        cached_format = _clean_text(meta.get("input_format", "")).lower()
        if target_format and cached_format and cached_format != target_format:
            continue

        entry_scope = _normalize_scope(meta.get("brand", ""))
        # Only hard-reject conflicting AMEX programme buckets (FHR vs THC).
        # Older FHR/THC entries store brand_label in meta["brand"], and Hilton
        # hotels may change brands while keeping the same physical location, so
        # treat non-matching scopes as a lower-ranked fallback rather than a miss.
        if (
            target_scope in _PROGRAM_SCOPES
            and entry_scope in _PROGRAM_SCOPES
            and entry_scope != target_scope
        ):
            continue
        scope_match = 1 if (target_scope and entry_scope == target_scope) else 0

        queries = meta.get("queries", []) or []
        formatted_address = ""
        is_resolved = 0
        strict_lodging = 0

        if isinstance(value, dict) and value != {"status": "NO_RESULT"}:
            types = value.get("types", [])
            if types and not is_lodging_type(types):
                continue
            formatted_address = value.get("formatted_address") or ""
            if hotel_location:
                if not address_location_compatible(formatted_address, hotel_location):
                    continue
                if not _candidate_city_compatible(hotel_location, formatted_address, queries):
                    continue
            is_resolved = 1
            strict_lodging = 1 if is_strict_lodging_type(types) else 0
        else:
            # Never reuse a NO_RESULT entry from another city/country.
            if hotel_location and not _no_result_location_compatible(hotel_location, queries):
                continue

        cached_hotel_name = meta.get("hotel_name", "")
        candidates: Iterable[str] = [cached_hotel_name] if cached_hotel_name else queries
        city_ok = _candidate_city_compatible(hotel_location, formatted_address, queries) if hotel_location else True

        for candidate in candidates:
            # Disallow loose subset matching when reusing cached coordinates:
            # copying coordinates from a sibling property (e.g. Mandarin Oriental
            # Paris -> Mandarin Oriental Lutetia, Paris) permanently poisons the
            # target hotel's exact cache key. Only reuse on exact normalized or
            # affiliation/location-bridged core matches.
            quality = _match_quality(
                hotel_name,
                candidate,
                hotel_location=hotel_location,
                location_confirmed=city_ok,
                allow_subset=False,
            )
            if quality is None:
                continue
            # Always rank resolved lodging entries above NO_RESULT entries so a
            # failed lookup cannot mask an already-geocoded entry.
            rank = (
                is_resolved,
                strict_lodging,
                quality[0],
                scope_match,
                quality[1],
                quality[2],
                quality[3],
                quality[4],
            )
            if best_rank is None or rank > best_rank:
                best_rank = rank
                best_key = key

    return best_key


def _retention_match(cached_name: str, scraped_name: str) -> bool:
    """Conservative-against-deletion match used only when pruning cache entries.

    Prevents deleting valid cached geocodes for hotels that are still listed
    under minor name edits (e.g. adding/removing Downtown, Residences, Villas,
    city qualifiers, or legacy query spellings), while still pruning genuinely
    removed or wrong-program entries without letting single-char brands like
    'W Hong Kong' match unrelated properties.
    """
    if _match_quality(cached_name, scraped_name, strict_discriminators=False) is not None:
        return True
    # Raw normalized-token fallback (preserving 1-char brand tokens like 'w'
    # and '1', and rejecting weak city-only matches) for legacy query-only
    # entries and minor typo/endonym renames.
    l_norm = normalize_name(cached_name)
    r_norm = normalize_name(scraped_name)
    if not l_norm or not r_norm:
        return False
    seq1 = [t for t in l_norm.split() if t not in DEFAULT_GENERIC_TOKENS]
    seq2 = [t for t in r_norm.split() if t not in DEFAULT_GENERIC_TOKENS]
    t1, t2 = align_compound_tokens(seq1, seq2)
    if not t1 or not t2:
        return False
    ov = t1 & t2
    if len(ov) < MIN_FUZZY_MATCH_TOKENS:
        return False
    # Never match solely on generic hospitality words like {"hotel", "resort"}
    if not (ov - GENERIC_HOSPITALITY_TOKENS - _WEAK_SINGLE_CORE_TOKENS):
        return False
    smaller, larger = (t1, t2) if len(t1) <= len(t2) else (t2, t1)
    if smaller.issubset(larger) and (len(larger) - len(smaller)) <= 2:
        return True
    return len(ov) / len(t1) >= 0.75 and len(ov) / len(t2) >= 0.75


def is_cached_hotel_in_scraped(
    cached_name: str,
    scope_key: str,
    scraped_index: Dict[str, Set[str]],
) -> bool:
    """Return True if cached_name appears in the current scraped list for scope_key."""
    name = _clean_text(cached_name).lower()
    sk = _normalize_scope(scope_key)

    candidates: Set[str] = (
        scraped_index.get(sk, set()) if sk
        else set().union(*scraped_index.values()) if scraped_index
        else set()
    )
    if name in candidates:
        return True
    return any(_retention_match(name, s) for s in candidates)


def update_cache_file(cache_path: str, new_keys: List[Tuple[str, Any]]) -> int:
    """Append (key, value) pairs to a single cache file. Skips keys already present."""
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
    """Remove cache entries identified by (name, scope, key, path) 4-tuples."""
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
    """Build the cache key JSON string for a new hotel placeholder entry."""
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

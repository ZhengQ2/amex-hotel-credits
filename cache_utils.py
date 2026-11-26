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
    # Letters NFKD leaves alone (Turkish dotless i, Maltese h-bar, Icelandic).
    "ı": "i", "ħ": "h", "Ħ": "h", "þ": "th", "Þ": "th", "ð": "d", "Ð": "d",
    # Apostrophe look-alikes, including the Hawaiian okina.
    "\u2018": "'", "\u2019": "'", "\u02bc": "'", "`": "'",
    "\u02bb": "'", "\u02b9": "'", "\u2032": "'", "\u00b4": "'", "\u201b": "'",
    "\u2013": "-", "\u2014": "-",
    # Must go before NFKD, which would expand them to "TM" / "SM".
    "®": None, "™": None, "\u2120": None,
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
    r"\b(?:part\s+of\s+)?(?:in\s+)?(?:the\s+)?unbound\s+collection(?:\s+by)?\s+hyatt(?:\s+hilton)?\b",
    r"\b(?:part\s+of\s+)?destination\s+by\s+hyatt\b",
    r"\b(?:a\s+)?luxury\s+collection\s+(?:desert\s+)?(?:hotel|resort)(?:\s*(?:&|and)\s*spa)?\b",
    r"\b(?:a\s+|the\s+)?leading\s+hotels?\s+of\s+the\s+world\b",
    r"\b(?:a\s+member\s+of\s+)?small\s+luxury\s+hotels?(?:\s+of\s+the\s+world)?\b",
    r"\brelais\s*(?:&|and)\s*chateaux\b",
    r"\bdorchester\s+collection\b",
    r"\b(?:a\s+)?rosewood\s+(?:hotel|resort)\b",
    r"\b(?:a\s+)?belmond\s+hotel\b",
    r"\b(?:a\s+)?tribute\s+portfolio(?:\s+hotel)?\b",
    r"\bautograph\s+collection\b",
    r"\bcurio(?:\s+collection)?(?:\s+by)?(?:\s+hilton)*\b",
    r"\btapestry(?:\s+collection)?(?:\s+by)?(?:\s+hilton)*\b",
    r"\b(?:a\s+)?graduate(?:\s+hotels?|\s+by\s+hilton)(?:\s+hilton)?\b",
    r"\bvignette\s+collection\s+by\s+ihg\b",
    r"\blxr\s+(?:hotels?\s*(?:&|and)\s*)?resorts?\b",
    r"\b(?:a\s+)?paris\s+society\s+collection\s+hotel\b",
    r"\b(?:the\s+|member\s+of\s+)?melia\s+collection\b",
    r"\bmgallery\s+collection\b",
    r"\bpartner\s+of\s+all\s+accor\b",
    r"\bmichelin\s+key\s+award\s+hotel\b",
    r"\ba\s+marriott\s+hotel\b",
    r"\(\s*marriott\s+international\s*\)",
    r"\ba\s+hyatt\s+(?:luxury\s+)?(?:hotel|resort|brand)\b",
    r"\b(?:a\s+)?concept\s+by\s+hyatt\b",
    r"\ba\s+registry\s+collection\s+hotel\b",
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

# Consortium labels and descriptors that never identify a hotel, so they can be
# dropped even when they come first ("Relais & Chateaux Villa Crespi").
_CONSORTIUM_RE = re.compile(
    r"relais|leading|small\s+luxury|design\s+hotels|preferred|worldhotels|"
    r"adults?|inclusive|forbes|michelin|accor|savoy",
    re.IGNORECASE,
)


def _strip_affiliations(s: str) -> str:
    """Drop affiliation taglines, keeping a brand that starts the name.

    "The Randolph Hotel Oxford, a Graduate by Hilton" loses its tagline, but
    "Graduate by Hilton Princeton" and "Rosewood Hotel Georgia" keep theirs:
    there the brand is the identity and only a place name would be left.
    """
    def replace(match: "re.Match[str]") -> str:
        if _CONSORTIUM_RE.search(match.group(0)):
            return " "
        head = re.findall(r"[a-z0-9]+", s[:match.start()].lower())
        if any(
            t not in DEFAULT_GENERIC_TOKENS and t not in GENERIC_HOSPITALITY_TOKENS
            for t in head
        ):
            return " "
        return match.group(0)

    return _AFFILIATION_RE.sub(replace, s)

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

# Common English exonym <-> local endonym / spacing city equivalents seen
# between AMEX scraped locations and Google formatted_address values.
# Keys match normalize_name() output (apostrophes collapsed, St. -> saint).
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
    "saint julians": ("san giljan", "saint julian", "st julians", "st julian"),
    "saint julian": ("san giljan", "saint julians", "st julians", "st julian"),
    "mexico city": ("ciudad de mexico", "cdmx"),
    "xian": ("xi an",),
    "shanghai": ("shang hai",),
    "beijing": ("bei jing",),
    "chengdu": ("cheng du",),
    "suzhou": ("su zhou",),
    "tianjin": ("tian jin",),
    "shenzhen": ("shen zhen",),
    "hangzhou": ("hang zhou",),
    "nanjing": ("nan jing",),
    "sanya": ("san ya",),
    "qingdao": ("qing dao",),
    "lijiang": ("li jiang",),
    "guangzhou": ("guang zhou",),
    "xiamen": ("xia men",),
    "zhuhai": ("zhu hai",),
}

# Only strip benign locality qualifiers that never collapse a distinct city
# into a state/country or separate neighbor (deliberately excludes "city",
# "beach", and "bay" so Mexico City, Virginia Beach, and Miami Beach remain
# distinct from Mexico, Virginia, and Miami).
_CITY_SUFFIX_RE = re.compile(
    r"\s+(?:island|mare|by the sea|metropolitan area)$"
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

# City-states and SARs whose trailing address segment is also the city itself.
_CITY_STATE_NAMES: FrozenSet[str] = frozenset({
    "hong kong", "macao", "macau", "singapore", "monaco",
})

_STATE_POSTAL_SEGMENT_RE = re.compile(
    r"^\s*([A-Z]{2})(?:\s+[A-Z0-9][A-Z0-9 -]*)?\s*$"
)
# Short leading house numbers (1-3 digits or hyphenated building numbers like
# "108-10"), applied only to the first segment and excluding Polish XX-XXX
# postal codes and 4-6 digit European/Asian postal codes (e.g. "20121 Milano").
_LEADING_HOUSE_NUM_RE = re.compile(r"^\s*\d{1,3}(?:-\d{1,2})?[a-z]?\s+[a-z]", re.IGNORECASE)
_POSTAL_HYPHEN_PREFIX_RE = re.compile(r"^\s*\d{2}-\d{3}\s+")
# Street-prefix tokens appearing at the start of a segment (or right after a
# house number); includes Spanish "C." ("C. de Sevilla") and intentionally
# excludes "st." so "St. Helena" / "St. Thomas" are never dropped.
_STREET_PREFIX_RE = re.compile(
    r"^(?:\d+[a-z]?(?:-\d+)?\s+)?(?:c\.|via|viale|corso|piazza|calle|carrer|avenida|"
    r"av\.?|rue|boulevard|bd\.?|chem\.?|chemin|paseo|rte\.?|route|carr\.?|carretera|"
    r"strasse|str\.?|platz|gasse|allee|jalan|jl\.?|thanon|soi|moo|khasra|triq|pl\.?)(?:\s|$)",
    re.IGNORECASE,
)
# Street-suffix tokens: full road words anywhere in a segment, or abbreviated
# road types ("st", "rd", "ave", "dr", ...) ONLY at the end of a segment (with
# optional directional/suite tail) so "Oxford St" is dropped while "St. Helena"
# is preserved. Chinese pinyin road words ("Lu", "Jie", "Dao") are checked
# case-sensitively so Italian province code "LU" (Lucca) is never matched.
_STREET_SUFFIX_RE = re.compile(
    r"(?:\b(?:street|road|avenue|boulevard|drive|lane|parkway|highway|court|"
    r"circle|terrace|trail|ibilbidea|pasealekua|strasse|chome|dori)\b|"
    r"\b(?:st|rd|ave|blvd|dr|ln|wy|way|pl|pkwy|hwy|ct|cir|ter|trl|ro|gil)\.?"
    r"(?:\s+(?:n|s|e|w|ne|nw|se|sw|suite|ste|unit|#)\b.*)?$)",
    re.IGNORECASE,
)
# Anchored to the end so "Qing Dao Shi" (a city) is kept while "Shi Ji Da Dao" goes.
_CN_STREET_WORD_RE = re.compile(r"\b(?:Lu|Jie|Dao)\s*$")
_CN_ADMIN_SUFFIX_RE = re.compile(r"\s+(?:shi|qu)$")
# "95131 Catania CT": postal code, town, province. Provinces such as CT and RO
# would otherwise read as "Court" and the Korean "-ro".
_IT_TOWN_SEG_RE = re.compile(r"^\d{5}\s+\S.*\s[A-Z]{2}$")
_CN_PROVINCE_SEG_RE = re.compile(r"\bsheng\s*$", re.IGNORECASE)
_CN_COUNTY_XIAN_RE = re.compile(r"(?<=[A-Za-z]\s)Xian\b")


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
    # Before NFKC, which would expand the trademark sign to "TM".
    s = s.replace("®", "").replace("™", "").replace("\u2120", "")
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\u2013", "-").replace("\u2014", "-").replace("\u2019", "'")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def fold_ascii(s: str) -> str:
    """Strip diacritics and fold special Latin letters/quotes to ASCII."""
    if not s:
        return ""
    s = s.translate(_CHAR_TRANSLATIONS)
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


@lru_cache(maxsize=16384)
def normalize_name(s: str) -> str:
    """Canonical ASCII, punctuation- and accent-insensitive name form.

    Collapses apostrophes ("Parker's" == "Parkers", "Xi'an" == "Xian",
    "Ho'olei" == "Hoolei"), strips French/Italian elided "d'"/"l'" prefixes
    ("Hotel D'Angleterre" == "Hotel Angleterre"), normalizes & -> and,
    St. -> saint, and D.C. -> dc, while preserving single-character brand
    tokens such as 'w' ('W Rome') and '1' ('1 Hotel').
    """
    s = fold_ascii(s).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"\bd\.\s*c\.?", "dc", s)
    s = re.sub(r"\b[dl]'(?=[a-z]{3,})", "", s)
    s = re.sub(r"'", "", s)
    s = re.sub(r"\bst\.?\b", "saint", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _strip_possessive(s: str) -> str:
    """Strip English possessive "'s" for secondary fallback comparisons only."""
    return re.sub(r"'s\b", "", fold_ascii(s), flags=re.IGNORECASE)


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
    # Keep the Australian state from expanding to "new south united kingdom".
    s = re.sub(r"\bnew south wales\b", "nsw", s)
    for alias, canonical in _TEXT_COUNTRY_EXPANSIONS.items():
        s = re.sub(rf"\b{re.escape(alias)}\b", canonical, s)
    return re.sub(r"\s+", " ", s).strip()


@lru_cache(maxsize=16384)
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


def _strip_safe_city_suffix(city: str, region: str = "", country: str = "") -> str:
    """Strip benign locality suffixes without collapsing a city into a state or country."""
    stem = _CITY_SUFFIX_RE.sub("", city).strip()
    if not stem or stem == city or len(stem) < 3:
        return ""
    if stem in _SUBREGION_CODES or stem in _COUNTRY_ALIASES or stem in _COUNTRY_ALIASES.values():
        return ""
    if (region and stem == region) or (country and stem == country):
        return ""
    return stem


def _cities_compatible(
    city1: str,
    city2: str,
    region: str = "",
    country: str = "",
) -> bool:
    """Return True only for exact city matches, known aliases, or safe suffix variants.

    Never merges distinct cities that merely share a prefix/word (e.g.
    San Diego vs San Francisco, Santa Monica vs Santa Barbara, Palm Beach vs
    Miami Beach, Fort Lauderdale vs Fort Myers, Manchester vs Winchester).
    """
    if not city1 or not city2:
        return False
    if city1 == city2:
        return True
    if city2 in _CITY_ALIASES.get(city1, ()) or city1 in _CITY_ALIASES.get(city2, ()):
        return True
    s1 = _strip_safe_city_suffix(city1, region=region, country=country) or city1
    s2 = _strip_safe_city_suffix(city2, region=region, country=country) or city2
    if s1 == s2:
        return True
    return s2 in _CITY_ALIASES.get(s1, ()) or s1 in _CITY_ALIASES.get(s2, ())


def locations_compatible(loc1: str, loc2: str) -> bool:
    """Return True when two non-empty scraped hotel_locations match at city level.

    Requires both locations to be non-empty so missing locations (e.g. Hilton
    CSVs) never pass as a city match, and compares city separately from state/
    region so different cities in the same state (Austin vs Houston, San Diego
    vs San Francisco) return False.
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
        return _cities_compatible(city1, city2, region=reg1 or reg2, country=co1 or co2)
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
    # Matches ", FL 33040, USA", ", CA, USA", ", QC G1R 5J5, Canada", etc. The
    # state comes last, after any direction such as ", NE," that looks like one.
    found = ""
    for match in re.finditer(r",\s*([A-Z]{2})(?:\s+[A-Z0-9][A-Z0-9 -]*)?(?=,|$)", formatted_address):
        code = match.group(1).lower()
        if code in _VALID_SUBREGION_CODES:
            found = code
    return found


def address_location_compatible(formatted_address: str, hotel_location: str) -> bool:
    """Return False when a formatted_address disagrees on country or US/CA state/province."""
    if not formatted_address or not hotel_location:
        return True
    _, _, loc_country = normalize_location_parts(hotel_location)
    if not loc_country:
        return True
    addr_norm = _normalize_geo_text(formatted_address)
    # Whole words only: Dominica is not the Dominican Republic, nor Oman Romania.
    country_ok = _haystack_has_phrase(addr_norm, loc_country) or (
        loc_country == "china"
        and (_haystack_has_phrase(addr_norm, "hong kong") or _haystack_has_phrase(addr_norm, "macao"))
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


def _city_needles(city: str, region: str = "", country: str = "") -> List[str]:
    """Return normalized city strings (including safe stems and endonyms) to match."""
    if not city:
        return []
    needles: List[str] = [city]
    stem = _strip_safe_city_suffix(city, region=region, country=country)
    if stem and stem not in needles:
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


def _street_kind(seg: str, *, is_first: bool = False, country: str = "") -> str:
    """Classify a segment as a street ("road"), a bare house number, or neither ("")."""
    folded = fold_ascii(seg).strip()
    if not folded or _POSTAL_HYPHEN_PREFIX_RE.search(folded):
        return ""
    if country == "italy" and _IT_TOWN_SEG_RE.match(folded):
        return ""
    if _STREET_PREFIX_RE.search(folded) or _STREET_SUFFIX_RE.search(folded):
        return "road"
    if country == "china" and _CN_STREET_WORD_RE.search(folded):
        return "road"
    if is_first and _LEADING_HOUSE_NUM_RE.search(folded):
        return "house_number"
    return ""


def _segment_name(seg: str, country: str) -> str:
    """Normalize one address segment to the bare place name it contains."""
    name = " ".join(t for t in normalize_name(seg).split() if not any(c.isdigit() for c in t))
    if country == "italy" and _IT_TOWN_SEG_RE.match(fold_ascii(seg).strip()):
        name = name.rsplit(" ", 1)[0]
    if country == "china":
        name = _CN_ADMIN_SUFFIX_RE.sub("", name)
    return name


@lru_cache(maxsize=32768)
def _parse_address_locality(formatted_address: str) -> Tuple[str, FrozenSet[str]]:
    """Return (locality text, exact segment names) for a Google formatted_address.

    The text drops street segments anywhere in the address (e.g. 'Corso Venezia',
    'Via Roma', 'C. de Sevilla', 'Oxford St', '300 S Doheny Dr'), trailing
    country/postal-only segments, Chinese province segments ('... Sheng'), and
    US/CA state+zip segments (e.g. 'NY 14202'), while preserving first-segment
    cities such as 'Adare, Co. Limerick, V94 W8WR, Ireland' and
    'St. Helena, CA 94574, USA'. The names let a town whose own name reads like a
    street ('Federal Way', 'Terrace') match when it is the whole segment.
    """
    if not formatted_address:
        return "", frozenset()
    parts = [p.strip() for p in re.split(r"\s*[,،]\s*|\s+-\s+", formatted_address) if p.strip()]
    if not parts:
        return "", frozenset()

    # Strip trailing pure-numeric postal segments (e.g. "... China, 710065").
    while len(parts) > 1 and re.fullmatch(r"[\d\s-]+", parts[-1]):
        parts.pop()

    # Drop the trailing country segment when multiple segments exist, unless
    # the trailing segment is a city-state / SAR (e.g. "Hong Kong", "Singapore")
    # or contains a glued postal code + locality (e.g. "Saint James BB24024Barbados").
    country = _normalize_geo_text(parts[-1])
    if (
        len(parts) >= 2
        and not re.search(r"\d", parts[-1])
        and country not in _CITY_STATE_NAMES
    ):
        parts = parts[:-1]

    # Filter out bare US/CA state+postal segments, pure house-number segments,
    # and Chinese province segments.
    non_meta: List[str] = []
    for seg in parts:
        if re.fullmatch(r"[\d\s-]+", seg):
            continue
        m = _STATE_POSTAL_SEGMENT_RE.match(seg)
        if m and m.group(1).lower() in _VALID_SUBREGION_CODES:
            continue
        if _CN_PROVINCE_SEG_RE.search(fold_ascii(seg)):
            continue
        non_meta.append(seg)

    kinds = [
        _street_kind(seg, is_first=(idx == 0), country=country)
        for idx, seg in enumerate(non_meta)
    ]
    locality_parts = [seg for seg, kind in zip(non_meta, kinds) if not kind]
    if not locality_parts:
        # A house number on the only segment is no reason to lose the city
        # ("279 Tambon Nongtalay ... Krabi 81180, Thailand").
        locality_parts = [seg for seg, kind in zip(non_meta, kinds) if kind == "house_number"]
    # Strip trailing Chinese administrative county word "Xian" (e.g.
    # "Jiu Zhai Gou Xian" in Sichuan) so it never collides with Xi'an city.
    cleaned = [_CN_COUNTY_XIAN_RE.sub("", seg).strip() for seg in locality_parts]
    text = normalize_name(" ".join(c for c in cleaned if c))
    # A numbered street is never a town: "22-28 Broadway" is not Broadway.
    names = frozenset(
        name for seg, kind in zip(non_meta, kinds)
        if not (kind and any(c.isdigit() for c in seg)) and (name := _segment_name(seg, country))
    )
    return text, names


def _address_locality_text(formatted_address: str) -> str:
    """Return only the locality text of a Google formatted_address."""
    return _parse_address_locality(formatted_address)[0]


def _same_normalized_hotel_and_location(
    hotel_name: str,
    cached_hotel_name: str,
    hotel_location: str,
    queries: Iterable[str],
) -> bool:
    """Return True when both hotel_name and hotel_location are unchanged after normalization.

    Matches exact normalized "{hotel_name} {hotel_location}" (with optional
    trailing Hotel/Resort) against the cached key's queries so an existing entry
    survives accent, punctuation, URL, or brand-metadata tweaks even when AMEX's
    location names an island, county, or neighboring town absent from Google's
    formatted_address, while never allowing a different city or state spelling
    (e.g. 'Buffalo, New York US' vs 'New York, US', or 'San Francisco, CA US'
    vs 'San Francisco, California US') to bypass the returned-address check.
    """
    if not hotel_name or not cached_hotel_name or not hotel_location or not queries:
        return False
    name_norm = normalize_name(hotel_name)
    if not name_norm or name_norm != normalize_name(cached_hotel_name):
        return False
    loc_norm = normalize_name(hotel_location)
    if not loc_norm:
        return False
    targets = {
        f"{name_norm} {loc_norm}",
        f"{name_norm} {loc_norm} hotel",
        f"{name_norm} {loc_norm} resort",
    }
    return any(normalize_name(q) in targets for q in queries if q)


def _candidate_city_compatible(
    hotel_location: str,
    formatted_address: str,
    queries: Iterable[str],
) -> bool:
    """Verify city agreement using the returned address locality first.

    When a resolved formatted_address is present, only its locality segments
    are checked (never letting search queries override a conflicting returned
    address, and never matching street names like 'Corso Venezia' or 'Via Roma').
    Falls back to queries only when formatted_address is absent.
    """
    if not hotel_location:
        return True
    city, region, country = normalize_location_parts(hotel_location)
    if not city:
        return True

    needles = _city_needles(city, region=region, country=country)
    if formatted_address:
        if city == country:
            # The place is its own country (Saint Barthelemy, Monaco), which the
            # caller's country check already covers.
            return True
        locality, segment_names = _parse_address_locality(formatted_address)
        return any(
            needle in segment_names or _haystack_has_phrase(locality, needle)
            for needle in needles
        )

    # No formatted_address available: fall back to cached queries.
    query_text = normalize_name(" ".join(queries or ()))
    if not query_text:
        return False
    return any(_haystack_has_phrase(query_text, needle) for needle in needles)


@lru_cache(maxsize=16384)
def _core_name_tokens_cached(
    name: str,
    generic_tokens: FrozenSet[str],
) -> Tuple[str, ...]:
    s = fold_ascii(name).lower().replace("&", " and ")
    s = re.sub(r"\bd\.\s*c\.?", "dc", s)
    s = re.sub(r"\b[dl]'(?=[a-z]{3,})", "", s)
    s = re.sub(r"'", "", s)
    s = _strip_affiliations(s)
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


def identity_words_agree(left: str, right: str) -> bool:
    """Return False when two names each have a word the other lacks.

    Used when the core tokens are only a place name, where "hotel" or "inn" is
    the only thing telling "Hotel Chicago" from "The Chicago Inn". A word on one
    side alone is fine ("Lizard Island" and "Lizard Island Resort"). Articles,
    taglines and plurals are ignored.
    """
    def words(name: str) -> Set[str]:
        return {
            t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t
            for t in _strip_affiliations_norm(name).split()
            if t not in DEFAULT_GENERIC_TOKENS
        }

    left_words, right_words = words(left), words(right)
    return left_words <= right_words or right_words <= left_words


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


# Words that describe a hotel's setting rather than which hotel it is, so two
# names agreeing only on them ("Jumeirah Lake Como" / "Hilton Lake Como") do
# not identify the same property.
_SETTING_TOKENS: FrozenSet[str] = _WEAK_SINGLE_CORE_TOKENS | frozenset({
    "lago", "lac", "isla", "ile", "isle", "islands", "mount", "mountain",
    "mountains", "valley", "coast", "shore", "cove", "harbor", "harbour",
    "marina", "lagoon", "springs", "peninsula", "cay", "atoll", "hills", "hill",
    "canyon", "desert", "forest", "point", "sands", "reef", "gulf", "old",
    "town", "village", "downtown", "saint", "slh", "lhw",
})


def _fold_umlaut_spelling(s: str) -> str:
    """Fold German-style umlaut spellings so 'Buergenstock' equals 'Burgenstock'."""
    return s.replace("ue", "u").replace("oe", "o").replace("ae", "a")


def _place_words(hotel_location: str, formatted_address: str) -> Set[str]:
    """Words naming a place: the scraped location, plus the address locality and country."""
    words = set(normalize_name(hotel_location).split())
    text, names = _parse_address_locality(formatted_address)
    words.update(text.split())
    for name in names:
        words.update(name.split())
    parts = [p for p in re.split(r"\s*[,،]\s*|\s+-\s+", formatted_address or "") if p.strip()]
    if parts:
        words.update(normalize_name(parts[-1]).split())
    return words


def fresh_candidate_conflicts(
    place_name: str,
    formatted_address: str,
    hotel_name: str,
    hotel_location: str,
) -> List[str]:
    """Why a freshly geocoded place in another town is probably a different hotel.

    Returns an empty list when the place is plausible. Only applies when the
    place's address does not name the hotel's own town: AMEX often lists a
    neighbouring town or island (Coconut Grove for Miami, Southbank for
    Melbourne), so the town alone is never a reason to reject. In another town,
    the candidate is rejected when
      1. its name agrees with the hotel's only on place or setting words, and
         each name has its own identifying word ("Hilton Lake Como" for
         "Jumeirah Lake Como" in Torno);
      2. the hotel's name contains its own town, the candidate's does not, and
         the candidate adds a word of its own ("The St. Regis Bal Harbour
         Resort" for "The St. Regis Longboat Key Resort");
      3. the candidate's name carries a town from its own address that the
         hotel's name and location lack ("Four Seasons Hotel Los Angeles At
         Beverly Hills" for "Four Seasons Hotel San Francisco").
    """
    if not place_name or not formatted_address or not hotel_name or not hotel_location:
        return []
    city, region, country = normalize_location_parts(hotel_location)
    if not city or city == country:
        return []
    if _candidate_city_compatible(hotel_location, formatted_address, ()):
        return []

    place_words = _place_words(hotel_location, formatted_address)
    hotel_tokens, place_tokens = align_compound_tokens(
        core_name_tokens(hotel_name), core_name_tokens(place_name)
    )
    ignorable = place_words | _SETTING_TOKENS
    hotel_only = hotel_tokens - place_tokens - ignorable
    place_only = place_tokens - hotel_tokens - ignorable
    conflicts: List[str] = []

    if not (hotel_tokens & place_tokens) - ignorable and hotel_only and place_only:
        conflicts.append("names share only place words")

    hotel_norm = normalize_name(hotel_name)
    place_norm = normalize_name(place_name)
    own_town = [
        needle for needle in _city_needles(city, region=region, country=country)
        if _haystack_has_phrase(hotel_norm, needle)
    ]
    if own_town and place_only and not any(
        _haystack_has_phrase(place_norm, needle) for needle in own_town
    ):
        conflicts.append("candidate drops the hotel's town")

    text, names = _parse_address_locality(formatted_address)
    hotel_words = {
        _fold_umlaut_spelling(w)
        for w in (hotel_norm + " " + normalize_name(hotel_location)).split()
    }
    hotel_joined = _fold_umlaut_spelling(
        (hotel_norm + normalize_name(hotel_location)).replace(" ", "")
    )
    for town in sorted(names | ({text} if text else set())):
        # Setting words do not tie a town to the hotel: Redondo Beach is not
        # Manhattan Beach.
        words = {_fold_umlaut_spelling(w) for w in town.split()} - _SETTING_TOKENS
        if (
            len(town) >= 3
            and words
            and _haystack_has_phrase(place_norm, town)
            and not words & hotel_words
            and _fold_umlaut_spelling(town.replace(" ", "")) not in hotel_joined
        ):
            conflicts.append(f"candidate is named for {town}")
            break
    return conflicts


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
    res = _match_quality_once(
        left,
        right,
        generic_tokens=generic_tokens,
        hotel_location=hotel_location,
        location_confirmed=location_confirmed,
        allow_subset=allow_subset,
        strict_discriminators=strict_discriminators,
    )
    if res is not None:
        return res
    # Secondary fallback for English possessive "'s" vs non-possessive
    # (e.g. "Universal's Loews Portofino Bay" vs "Loews Portofino Bay Universal")
    # without breaking "Parker's" == "Parkers" in the primary apostrophe-collapsed form.
    if "'s" in fold_ascii(left).lower() or "'s" in fold_ascii(right).lower():
        return _match_quality_once(
            _strip_possessive(left),
            _strip_possessive(right),
            generic_tokens=generic_tokens,
            hotel_location=hotel_location,
            location_confirmed=location_confirmed,
            allow_subset=allow_subset,
            strict_discriminators=strict_discriminators,
        )
    return None


def _match_quality_once(
    left: str,
    right: str,
    generic_tokens: FrozenSet[str] = DEFAULT_GENERIC_TOKENS,
    hotel_location: str = "",
    location_confirmed: bool = True,
    allow_subset: bool = True,
    strict_discriminators: bool = True,
) -> Optional[Tuple[int, int, int, int, int]]:
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

    loc_toks = set(normalize_name(hotel_location).split()) if hotel_location else set()

    # Exact core match after stripping affiliation taglines (no location stripping
    # needed). A core made only of location words does not identify a hotel.
    if lt == rt and (not loc_toks or lt - loc_toks or identity_words_agree(left, right)):
        return (2, len(lt), 1, 0, -abs(len(l_norm) - len(r_norm)))

    # Allow ignoring tokens from hotel_location ONLY when EVERY differing token
    # between lt and rt is explained by hotel_location (no conflicting city/word
    # remains on either side, e.g. "Akelarre" vs "Akelarre San Sebastian" in
    # "San Sebastián, Spain"), and never before subset/overlap matching —
    # otherwise stripping "austin" from "Four Seasons Hotel Austin" would turn
    # it into a subset of "Four Seasons Hotel Houston".
    sym_diff = lt ^ rt
    if loc_toks and sym_diff and sym_diff.issubset(loc_toks):
        shared_core = lt & rt
        candidate_confirms_city = bool(rt - lt) or location_confirmed
        identifies = bool(shared_core - loc_toks) or identity_words_agree(left, right)
        if shared_core and identifies and candidate_confirms_city:
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
            # letting weak single tokens like "new" or "w" — or a trailing word like
            # "The Carlton" matching every "The Ritz-Carlton, <City>" — match unrelated hotels.
            single_tok = next(iter(overlap))
            if (
                len(single_tok) >= 3
                and single_tok not in _WEAK_SINGLE_CORE_TOKENS
                and l_seq[0] == r_seq[0]
            ):
                raw_l = {t for t in l_norm.split() if t not in generic_tokens}
                raw_r = {t for t in r_norm.split() if t not in generic_tokens}
                ls = _strip_affiliations_norm(left)
                rs = _strip_affiliations_norm(right)
                prefix_ok = (
                    l_norm.startswith(r_norm + " ")
                    or r_norm.startswith(l_norm + " ")
                    or (bool(ls) and bool(rs) and (ls.startswith(rs + " ") or rs.startswith(ls + " ")))
                )
                if (diff == 1 or prefix_ok) and (
                    len(raw_l & raw_r) >= 2 or len(raw_l) == 1 or len(raw_r) == 1
                ):
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
    """Load one or more geocode cache files and return (index, entries)."""
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
    """Search a raw cache dict for a resolved key whose hotel_name matches.

    Used by google-convert.py to reuse already-geocoded coordinates when the
    hotel's name or location formatting changed slightly, avoiding an
    unnecessary paid API call.
      - Skips placeholder (None), NO_RESULT (so renamed hotels always get a
        fresh lookup), and non-lodging noise entries (e.g. parking lots).
      - Prefers exact name matches ahead of strict_lodging and scope match,
        while allowing older brand-scoped FHR/THC entries and Hilton brand
        rebrands as lower-ranked fallbacks.
      - Verifies country, US/CA state/province, and returned-address locality
        when hotel_location is provided.
    """
    target_scope = _normalize_scope(scope_key)
    target_format = _clean_text(input_format).lower()
    best_key: Optional[str] = None
    best_rank: Optional[Tuple[int, int, int, int, int, int, int, int]] = None

    for key, value in cache.items():
        if not isinstance(value, dict) or value.get("status") == "NO_RESULT":
            continue
        try:
            meta = json.loads(key)
        except Exception:
            continue

        cached_format = _clean_text(meta.get("input_format", "")).lower()
        if target_format and cached_format and cached_format != target_format:
            continue

        entry_scope = _normalize_scope(meta.get("brand", ""))
        if (
            target_scope in _PROGRAM_SCOPES
            and entry_scope in _PROGRAM_SCOPES
            and entry_scope != target_scope
        ):
            continue
        scope_match = 1 if (target_scope and entry_scope == target_scope) else 0

        types = value.get("types", [])
        if types and not is_lodging_type(types):
            continue

        queries = meta.get("queries", []) or []
        formatted_address = value.get("formatted_address") or ""
        cached_hotel_name = meta.get("hotel_name", "")
        city_addr_ok = 1
        if hotel_location:
            if not address_location_compatible(formatted_address, hotel_location):
                continue
            addr_city_match = _candidate_city_compatible(hotel_location, formatted_address, queries)
            if not addr_city_match:
                # Keep a hotel's own existing entry when both hotel_name and
                # hotel_location are unchanged after normalization (e.g. an
                # accent/punctuation/URL tweak on a resort whose AMEX location
                # names a region, island, or neighboring town absent from the
                # Google address), while applying the strict address check
                # whenever the name or location changed.
                if not _same_normalized_hotel_and_location(
                    hotel_name, cached_hotel_name, hotel_location, queries
                ):
                    continue
                city_addr_ok = 0

        strict_lodging = 1 if is_strict_lodging_type(types) else 0
        candidates: Iterable[str] = [cached_hotel_name] if cached_hotel_name else queries

        for candidate in candidates:
            quality = _match_quality(
                hotel_name,
                candidate,
                hotel_location=hotel_location,
                location_confirmed=True,
                allow_subset=False,
            )
            if quality is None:
                continue
            # Rank name match tier ahead of strict_lodging, preferring entries
            # whose returned address locality also matches hotel_location.
            rank = (
                quality[0],
                city_addr_ok,
                strict_lodging,
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


def _strip_affiliations_norm(name: str) -> str:
    """Normalize a hotel name after stripping affiliation taglines (keeps articles)."""
    s = fold_ascii(name).lower().replace("&", " and ")
    s = re.sub(r"\bd\.\s*c\.?", "dc", s)
    s = re.sub(r"\b[dl]'(?=[a-z]{3,})", "", s)
    s = re.sub(r"'", "", s)
    s = _strip_affiliations(s)
    s = re.sub(r"\bst\.?\b", "saint", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _retention_match(cached_name: str, scraped_name: str) -> bool:
    """Match predicate used only when pruning cache entries in scrapers.

    Preserves valid cached geocodes for still-listed hotels across minor edits
    (taglines, discriminators like Downtown/Residences/Villas, spacing/apostrophe
    splits like "Ka La'i" vs "Ka La i", city qualifiers, legacy query spellings)
    while avoiding false retention of removed same-chain hotels in other cities.
    """
    if _match_quality(cached_name, scraped_name, strict_discriminators=False) is not None:
        return True
    l_stripped = _strip_affiliations_norm(cached_name)
    r_stripped = _strip_affiliations_norm(scraped_name)
    if not l_stripped or not r_stripped:
        return False
    # Exact match ignoring whitespace catches apostrophe-split spellings like
    # "ka la i waikiki beach, lxr resorts" vs "Ka La'i Waikiki Beach, LXR Hotels & Resorts"
    # even when a split fragment ("la") happens to be a generic article token.
    l_compact = l_stripped.replace(" ", "")
    r_compact = r_stripped.replace(" ", "")
    if len(l_compact) >= 6 and l_compact == r_compact:
        return True

    # Strip affiliation taglines before tokenizing so shared collection phrases
    # (e.g. "The Leading Hotels of the World", "a Member of Design Hotels")
    # cannot inflate token overlap between distinct properties.
    seq1 = [t for t in l_stripped.split() if t not in DEFAULT_GENERIC_TOKENS]
    seq2 = [t for t in r_stripped.split() if t not in DEFAULT_GENERIC_TOKENS]
    t1, t2 = align_compound_tokens(seq1, seq2)
    if not t1 or not t2:
        return False
    ov = t1 & t2
    smaller, larger = (t1, t2) if len(t1) <= len(t2) else (t2, t1)
    if smaller.issubset(larger):
        diff = len(larger) - len(smaller)
        # Legacy query-only entry with appended "hilton" (e.g. "inn sea hilton"
        # vs "Inn by the Sea", or "marquette hilton" vs "The Marquette Hotel").
        if diff == 1 and (larger - smaller) == {"hilton"} and ov:
            return True
        if len(ov) >= MIN_FUZZY_MATCH_TOKENS and diff <= 2 and (
            ov - GENERIC_HOSPITALITY_TOKENS - _WEAK_SINGLE_CORE_TOKENS
        ):
            return True
        return False
    if len(ov) < MIN_FUZZY_MATCH_TOKENS:
        return False
    # Require >= 0.80 bidirectional overlap (matching main) on non-tagline
    # tokens so removed chain hotels in 1-word cities are not falsely retained.
    if not (ov - GENERIC_HOSPITALITY_TOKENS - _WEAK_SINGLE_CORE_TOKENS):
        return False
    return len(ov) / len(t1) >= 0.80 and len(ov) / len(t2) >= 0.80


def is_cached_hotel_in_scraped(
    cached_name: str,
    scope_key: str,
    scraped_index: Dict[str, Set[str]],
) -> bool:
    """Return True if cached_name appears in the current scraped list for scope_key.

    For non-programme scopes (Hilton brands), also checks exact normalized and
    exact core-identity matches across other brands in scraped_index so a hotel
    switching Hilton brands (e.g. Curio -> LXR) keeps its old cache entry for
    geocoding to reuse. The entry stays for as long as the hotel is listed.
    """
    name = _clean_text(cached_name).lower()
    sk = _normalize_scope(scope_key)

    candidates: Set[str] = (
        scraped_index.get(sk, set()) if sk
        else set().union(*scraped_index.values()) if scraped_index
        else set()
    )
    if name in candidates:
        return True
    if any(_retention_match(name, s) for s in candidates):
        return True

    # Hilton brand rebrands: keep the old brand's entry when the same hotel
    # (exact normalized name or exact tagline-stripped core identity) is still
    # listed under another brand in this scraper run.
    if sk and sk not in _PROGRAM_SCOPES and scraped_index:
        for other_scope, other_names in scraped_index.items():
            if other_scope == sk:
                continue
            if name in other_names or any(
                _match_quality(name, s, allow_subset=False) is not None
                for s in other_names
            ):
                return True
    return False


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

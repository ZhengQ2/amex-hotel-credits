# scrape_hilton_hotels_by_brand_playwright.py
from playwright.sync_api import sync_playwright, TimeoutError
import pandas as pd
import re
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

# Some hotels are listed with external-brand domains or non-English Hilton locales.
# _normalize_fetch_url uses this to resolve the canonical slug for city-hint matching.
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


# ---------------------------------------------------------------------------
# Property-code prefix → location (Tier 1 lookup)
#
# Hilton property codes follow the pattern {CITY:3}{PROPERTY:2}{BRAND:2}.
# The first 3 characters are an IATA airport code or a Hilton-internal city
# abbreviation.  This table maps those 3-char codes directly to location
# strings, which is more reliable than token-matching the slug.
#
# Sources: IATA airport codes + known Hilton-internal overrides.
# CC = ISO 3166-1 alpha-2 country code  (CA = Canada, not California).
# ---------------------------------------------------------------------------
_CODE_TO_LOCATION: Dict[str, str] = {
    # ── United States — Hawaii ───────────────────────────────────────────────
    "hnl": "Honolulu, US",      # Honolulu International
    "ogg": "Maui, US",          # Kahului (Maui)
    "jhm": "Maui, US",          # Kapalua (Maui)
    "lih": "Kauai, US",         # Lihue (Kauai)
    "koa": "Big Island, US",    # Kailua-Kona
    "ito": "Hilo, US",          # Hilo
    "mkk": "Molokai, US",
    # ── United States — Nevada / Mountain West ───────────────────────────────
    "las": "Las Vegas, US",
    "jac": "Jackson Hole, US",
    "ase": "Aspen, US",
    "ege": "Vail, US",          # Eagle/Vail
    "hdn": "Steamboat Springs, US",
    "tex": "Telluride, US",
    "snm": "Snowmass, US",
    "slc": "Salt Lake City, US",
    "prc": "Prescott, US",
    # ── United States — California ───────────────────────────────────────────
    "lax": "Los Angeles, US",
    "bur": "Burbank, US",
    "sfo": "San Francisco, US",
    "oak": "Oakland, US",
    "sjc": "San Jose, US",
    "san": "San Diego, US",
    "sna": "Newport Beach, US", # John Wayne / Orange County
    "lgb": "Long Beach, US",
    "sba": "Santa Barbara, US",
    "smx": "Santa Maria, US",
    "mry": "Monterey, US",
    "psp": "Palm Springs, US",
    "trk": "Truckee, US",       # Lake Tahoe area
    "mmh": "Mammoth Lakes, US",
    "sts": "Santa Rosa, US",    # Sonoma/Wine Country
    # ── United States — Arizona ──────────────────────────────────────────────
    "phx": "Phoenix, US",
    "sco": "Scottsdale, US",    # Hilton-internal (SCO = Scottsdale)
    "sdl": "Scottsdale, US",    # Scottsdale airport
    "tus": "Tucson, US",
    "sed": "Sedona, US",        # Hilton-internal
    # ── United States — Pacific Northwest ────────────────────────────────────
    "sea": "Seattle, US",
    "bfi": "Seattle, US",
    "pdx": "Portland, US",
    "geg": "Spokane, US",
    "rdm": "Bend, US",          # Redmond (Bend)
    "anc": "Anchorage, US",
    # ── United States — Florida ──────────────────────────────────────────────
    "mco": "Orlando, US",
    "mia": "Miami, US",
    "fll": "Fort Lauderdale, US",
    "pbi": "Palm Beach, US",
    "rsw": "Fort Myers, US",
    "nap": "Naples, US",        # Hilton-internal (NAP = Naples FL)
    "tpa": "Tampa, US",
    "pie": "St. Petersburg, US",
    "sfb": "Orlando, US",       # Sanford (Orlando area)
    "jax": "Jacksonville, US",
    "eyw": "Key West, US",
    "dab": "Daytona Beach, US",
    "srq": "Sarasota, US",
    "vps": "Destin, US",        # Destin / Fort Walton
    "pns": "Pensacola, US",
    # ── United States — Southeast ────────────────────────────────────────────
    "atl": "Atlanta, US",
    "sav": "Savannah, US",
    "chs": "Charleston, US",
    "hxd": "Hilton Head, US",   # Hilton Head airport
    "myr": "Myrtle Beach, US",
    "hhh": "Hilton Head, US",
    "avl": "Asheville, US",
    "clt": "Charlotte, US",
    "rdu": "Raleigh, US",
    "orf": "Virginia Beach, US",
    "ofk": "Norfolk, US",
    "phf": "Newport News, US",
    "ric": "Richmond, US",
    "bna": "Nashville, US",
    "mem": "Memphis, US",
    "sdf": "Louisville, US",
    "bhm": "Birmingham, US",
    "msy": "New Orleans, US",
    "btr": "Baton Rouge, US",
    # ── United States — Mid-Atlantic / Northeast ─────────────────────────────
    "dca": "Washington DC, US",
    "iad": "Washington DC, US",
    "bwi": "Baltimore, US",
    "phl": "Philadelphia, US",
    "pit": "Pittsburgh, US",
    "jfk": "New York, US",
    "lga": "New York, US",
    "ewr": "New York, US",
    "nyc": "New York, US",      # Hilton-internal
    "isp": "Long Island, US",
    "hpn": "Westchester, US",
    "hvn": "New Haven, US",
    "bos": "Boston, US",
    "pvd": "Providence, US",
    "ack": "Nantucket, US",
    "mvy": "Martha's Vineyard, US",
    "hya": "Cape Cod, US",
    "bed": "Boston, US",
    # ── United States — Midwest ──────────────────────────────────────────────
    "ord": "Chicago, US",
    "mdw": "Chicago, US",
    "dtw": "Detroit, US",
    "cle": "Cleveland, US",
    "cmh": "Columbus, US",
    "cvg": "Cincinnati, US",
    "ind": "Indianapolis, US",
    "mke": "Milwaukee, US",
    "msp": "Minneapolis, US",
    "stl": "St. Louis, US",
    "mci": "Kansas City, US",
    "oma": "Omaha, US",
    "dsm": "Des Moines, US",
    "grr": "Grand Rapids, US",
    "tvc": "Traverse City, US",
    # ── United States — Texas ────────────────────────────────────────────────
    "dfw": "Dallas, US",
    "dal": "Dallas, US",
    "iah": "Houston, US",
    "hou": "Houston, US",
    "aus": "Austin, US",
    "sat": "San Antonio, US",
    "gls": "Galveston, US",
    "elp": "El Paso, US",
    # ── Canada ───────────────────────────────────────────────────────────────
    "wst": "Whistler, CA",      # Hilton-internal (Whistler has no public airport)
    "yvr": "Vancouver, CA",
    "yyj": "Victoria, CA",
    "ylw": "Kelowna, CA",
    "yyz": "Toronto, CA",
    "yow": "Ottawa, CA",
    "yul": "Montreal, CA",
    "yqb": "Quebec City, CA",
    "yyc": "Calgary, CA",
    "yeg": "Edmonton, CA",
    "yhz": "Halifax, CA",
    "ywg": "Winnipeg, CA",
    "yxe": "Saskatoon, CA",
    "yfc": "Fredericton, CA",
    # ── Mexico ───────────────────────────────────────────────────────────────
    "cun": "Cancun, MX",
    "sjd": "Los Cabos, MX",
    "pvr": "Puerto Vallarta, MX",
    "zih": "Ixtapa/Zihuatanejo, MX",
    "hux": "Huatulco, MX",
    "aca": "Acapulco, MX",
    "zlo": "Manzanillo, MX",
    "mzt": "Mazatlán, MX",
    "mty": "Monterrey, MX",
    "gdl": "Guadalajara, MX",
    "mex": "Mexico City, MX",
    "oax": "Oaxaca, MX",
    "mid": "Mérida, MX",
    "cjs": "Ciudad Juárez, MX",
    # ── Caribbean ────────────────────────────────────────────────────────────
    "nas": "Nassau, BS",
    "pls": "Turks and Caicos, TC",
    "puj": "Punta Cana, DO",
    "sdq": "Santo Domingo, DO",
    "sju": "San Juan, PR",
    "stt": "St. Thomas, VI",
    "stx": "St. Croix, VI",
    "aua": "Aruba, AW",
    "cur": "Curaçao, CW",
    "sxm": "Sint Maarten, SX",
    "uvf": "St. Lucia, LC",
    "bgi": "Barbados, BB",
    "mbj": "Montego Bay, JM",
    "kin": "Kingston, JM",
    "gcm": "Grand Cayman, KY",
    "bze": "Belize City, BZ",
    "spr": "Ambergris Caye, BZ",
    "ptp": "Guadeloupe, GP",
    "fdf": "Martinique, MQ",
    "bdm": "Bermuda, BM",       # BDA is the IATA for Bermuda
    "bda": "Bermuda, BM",
    "pos": "Trinidad, TT",
    # ── Central & South America ──────────────────────────────────────────────
    "sjo": "San José, CR",
    "lir": "Guanacaste, CR",
    "pty": "Panama City, PA",
    "bog": "Bogotá, CO",
    "mde": "Medellín, CO",
    "ctg": "Cartagena, CO",
    "lim": "Lima, PE",
    "cuz": "Cusco, PE",
    "uio": "Quito, EC",
    "gye": "Guayaquil, EC",
    "gru": "São Paulo, BR",
    "cgh": "São Paulo, BR",
    "gig": "Rio de Janeiro, BR",
    "sdu": "Rio de Janeiro, BR",
    "ssz": "Santos, BR",
    "eze": "Buenos Aires, AR",
    "aep": "Buenos Aires, AR",
    "mdz": "Mendoza, AR",
    "brc": "Bariloche, AR",
    "scl": "Santiago, CL",
    "mvd": "Montevideo, UY",
    "pdp": "Punta del Este, UY",
    "ccs": "Caracas, VE",
    # ── United Kingdom & Ireland ─────────────────────────────────────────────
    "lhr": "London, GB",
    "lgw": "London, GB",
    "lcy": "London, GB",
    "stn": "London, GB",
    "edi": "Edinburgh, GB",
    "gla": "Glasgow, GB",
    "man": "Manchester, GB",
    "bhx": "Birmingham, GB",
    "brs": "Bristol, GB",
    "cwl": "Cardiff, GB",
    "dub": "Dublin, IE",
    "noc": "Knock, IE",         # Ireland West
    "snn": "Shannon, IE",
    "cfn": "Donegal, IE",
    # ── France ───────────────────────────────────────────────────────────────
    "cdg": "Paris, FR",
    "ory": "Paris, FR",
    "nce": "Nice, FR",
    "mrq": "Cannes, FR",        # Mandelieu La Napoule (Cannes area)
    "mnl": "Nice, FR",          # actually Manila; "mnl" won't appear for Nice
    "mrs": "Marseille, FR",
    "lys": "Lyon, FR",
    "bod": "Bordeaux, FR",
    "bas": "Biarritz, FR",      # Biarritz airport: BIQ
    "biq": "Biarritz, FR",
    "sxb": "Strasbourg, FR",
    "cot": "Courchevel, FR",    # Courchevel airport: CVF
    "cvf": "Courchevel, FR",
    "cmf": "Chambéry, FR",      # near ski resorts
    "gva": "Geneva, CH",        # also serves French Alps
    # ── Italy ────────────────────────────────────────────────────────────────
    "fco": "Rome, IT",
    "cia": "Rome, IT",
    "rom": "Rome, IT",          # Hilton-internal city code
    "lin": "Milan, IT",
    "mxp": "Milan, IT",
    "fir": "Florence, IT",
    "flr": "Florence, IT",
    "vce": "Venice, IT",
    "trs": "Trieste, IT",
    "nap": "Naples, IT",        # NOTE: conflicts with Naples FL above; Italy context
    "pmo": "Palermo, IT",
    "cag": "Sardinia, IT",
    "olb": "Sardinia, IT",      # Olbia (Costa Smeralda)
    "tor": "Turin, IT",
    "blq": "Bologna, IT",
    "bri": "Bari, IT",
    # ── Spain ────────────────────────────────────────────────────────────────
    "mad": "Madrid, ES",
    "bcn": "Barcelona, ES",
    "svq": "Seville, ES",
    "grx": "Granada, ES",
    "bao": "Bilbao, ES",        # BIO is Bilbao
    "bio": "Bilbao, ES",
    "vle": "Valencia, ES",      # VLC is Valencia
    "vlc": "Valencia, ES",
    "agp": "Málaga, ES",
    "ibz": "Ibiza, ES",
    "pmi": "Mallorca, ES",
    "mah": "Menorca, ES",
    "tfs": "Tenerife, ES",
    "tfn": "Tenerife, ES",
    "lpa": "Gran Canaria, ES",
    "ace": "Lanzarote, ES",
    "fue": "Fuerteventura, ES",
    "eas": "San Sebastián, ES",
    # ── Portugal ─────────────────────────────────────────────────────────────
    "lis": "Lisbon, PT",
    "opo": "Porto, PT",
    "fao": "Algarve, PT",
    "fnc": "Madeira, PT",
    "pdl": "Azores, PT",
    # ── Germany ──────────────────────────────────────────────────────────────
    "ber": "Berlin, DE",
    "txl": "Berlin, DE",
    "fra": "Frankfurt, DE",
    "muc": "Munich, DE",
    "ham": "Hamburg, DE",
    "cgn": "Cologne, DE",
    "dus": "Düsseldorf, DE",
    "str": "Stuttgart, DE",
    "drs": "Dresden, DE",
    "nue": "Nuremberg, DE",
    "haj": "Hannover, DE",
    "lem": "Leipzig, DE",
    "lei": "Leipzig, DE",
    # ── Austria / Switzerland ────────────────────────────────────────────────
    "vie": "Vienna, AT",
    "szg": "Salzburg, AT",
    "inn": "Innsbruck, AT",
    "lzs": "St. Moritz, CH",    # Samedan (St. Moritz area)
    "smv": "St. Moritz, CH",
    "zrh": "Zurich, CH",
    "gva": "Geneva, CH",
    "bsl": "Basel, CH",
    "brn": "Bern, CH",
    # ── Netherlands / Belgium ────────────────────────────────────────────────
    "ams": "Amsterdam, NL",
    "bru": "Brussels, BE",
    "crl": "Brussels, BE",
    # ── Scandinavia & Baltics ────────────────────────────────────────────────
    "arn": "Stockholm, SE",
    "bma": "Stockholm, SE",
    "got": "Gothenburg, SE",
    "cph": "Copenhagen, DK",
    "osl": "Oslo, NO",
    "bgo": "Bergen, NO",
    "hel": "Helsinki, FI",
    "kef": "Reykjavik, IS",
    "tll": "Tallinn, EE",
    "rix": "Riga, LV",
    "vno": "Vilnius, LT",
    # ── Eastern Europe ───────────────────────────────────────────────────────
    "waw": "Warsaw, PL",
    "krk": "Kraków, PL",
    "prg": "Prague, CZ",
    "bts": "Bratislava, SK",
    "bud": "Budapest, HU",
    "otp": "Bucharest, RO",
    "sof": "Sofia, BG",
    "dbv": "Dubrovnik, HR",
    "spu": "Split, HR",
    "zag": "Zagreb, HR",
    "lju": "Ljubljana, SI",
    "beg": "Belgrade, RS",
    "say": "Sarajevo, BA",
    "tia": "Tirana, AL",
    "ath": "Athens, GR",
    "jmk": "Mykonos, GR",
    "jtr": "Santorini, GR",
    "her": "Crete, GR",
    "rho": "Rhodes, GR",
    "cfu": "Corfu, GR",
    "skg": "Thessaloniki, GR",
    "iev": "Kyiv, UA",
    "svo": "Moscow, RU",
    "led": "St. Petersburg, RU",
    "tbs": "Tbilisi, GE",
    "gyd": "Baku, AZ",
    "evn": "Yerevan, AM",
    "ala": "Almaty, KZ",
    "tse": "Astana, KZ",
    "tas": "Tashkent, UZ",
    # ── Turkey ───────────────────────────────────────────────────────────────
    "ist": "Istanbul, TR",
    "saw": "Istanbul, TR",
    "esb": "Ankara, TR",
    "ayt": "Antalya, TR",
    "bjv": "Bodrum, TR",
    "izm": "İzmir, TR",
    "adm": "Adıyaman, TR",
    # ── Middle East ──────────────────────────────────────────────────────────
    "dxb": "Dubai, AE",
    "dwc": "Dubai, AE",
    "auh": "Abu Dhabi, AE",
    "shj": "Sharjah, AE",
    "rkt": "Ras Al Khaimah, AE",
    "fjr": "Fujairah, AE",
    "doh": "Doha, QA",
    "ruh": "Riyadh, SA",
    "jed": "Jeddah, SA",
    "mct": "Muscat, OM",
    "sll": "Salalah, OM",
    "kwi": "Kuwait City, KW",
    "bah": "Manama, BH",
    "amm": "Amman, JO",
    "bey": "Beirut, LB",
    "tlv": "Tel Aviv, IL",
    # ── Africa ───────────────────────────────────────────────────────────────
    "cai": "Cairo, EG",
    "ssh": "Sharm el-Sheikh, EG",
    "hrg": "Hurghada, EG",
    "lxr": "Luxor, EG",
    "asw": "Aswan, EG",
    "cmn": "Casablanca, MA",
    "rak": "Marrakech, MA",
    "tng": "Tangier, MA",
    "aga": "Agadir, MA",
    "tun": "Tunis, TN",
    "dji": "Djibouti, DJ",
    "cpt": "Cape Town, ZA",
    "jnb": "Johannesburg, ZA",
    "dur": "Durban, ZA",
    "nbo": "Nairobi, KE",       # NOTE: Hilton may also use "nbo" for Ningbo;
                                # if so, Tier-2 slug search catches "ningbo"
    "ngb": "Ningbo, CN",        # Ningbo Lishe Intl (actual IATA)
    "mba": "Mombasa, KE",
    "jro": "Kilimanjaro, TZ",   # near Serengeti
    "dar": "Dar es Salaam, TZ",
    "znz": "Zanzibar, TZ",
    "kgl": "Kigali, RW",
    "add": "Addis Ababa, ET",
    "los": "Lagos, NG",
    "abv": "Abuja, NG",
    "acc": "Accra, GH",
    "dkr": "Dakar, SN",
    "sex": "Seychelles, SC",    # SEZ is Seychelles
    "sez": "Seychelles, SC",
    "mru": "Mauritius, MU",
    # ── India & South Asia ───────────────────────────────────────────────────
    "bom": "Mumbai, IN",
    "del": "New Delhi, IN",
    "blr": "Bengaluru, IN",
    "maa": "Chennai, IN",
    "ccu": "Kolkata, IN",
    "hyd": "Hyderabad, IN",
    "goi": "Goa, IN",
    "jai": "Jaipur, IN",
    "udr": "Udaipur, IN",
    "jdh": "Jodhpur, IN",
    "agr": "Agra, IN",
    "vns": "Varanasi, IN",
    "atr": "Amritsar, IN",
    "cok": "Kochi, IN",
    "trv": "Thiruvananthapuram, IN",
    "pnq": "Pune, IN",
    "amd": "Ahmedabad, IN",
    "ixc": "Chandigarh, IN",
    "lko": "Lucknow, IN",
    "cmb": "Colombo, LK",
    "ktm": "Kathmandu, NP",
    "dac": "Dhaka, BD",
    "khi": "Karachi, PK",
    "lhe": "Lahore, PK",
    "isb": "Islamabad, PK",
    # ── East Asia ────────────────────────────────────────────────────────────
    "hkg": "Hong Kong, HK",
    "mfm": "Macau, MO",
    "nrt": "Tokyo, JP",
    "hnd": "Tokyo, JP",
    "kix": "Osaka, JP",
    "itm": "Osaka, JP",
    "cts": "Sapporo, JP",
    "oka": "Okinawa, JP",
    "fuk": "Fukuoka, JP",
    "nak": "Nakhon Ratchasima, TH",
    "ump": "Umpang, TH",
    "kij": "Niigata, JP",
    "sdj": "Sendai, JP",
    "icn": "Seoul, KR",
    "gmp": "Seoul, KR",
    "pus": "Busan, KR",
    "cju": "Jeju, KR",
    "pek": "Beijing, CN",
    "pkx": "Beijing, CN",
    "pvg": "Shanghai, CN",
    "sha": "Shanghai, CN",
    "can": "Guangzhou, CN",
    "szx": "Shenzhen, CN",
    "ctu": "Chengdu, CN",
    "ckg": "Chongqing, CN",
    "xiy": "Xi'an, CN",
    "hgh": "Hangzhou, CN",
    "nkg": "Nanjing, CN",
    "wuh": "Wuhan, CN",
    "kmg": "Kunming, CN",
    "kwl": "Guilin, CN",
    "syx": "Sanya, CN",
    "hrb": "Harbin, CN",
    "tao": "Qingdao, CN",
    "dlc": "Dalian, CN",
    "tsn": "Tianjin, CN",
    "ngb": "Ningbo, CN",
    "tpe": "Taipei, TW",
    "tsa": "Taipei, TW",
    "khh": "Kaohsiung, TW",
    "rmq": "Taichung, TW",
    # ── Southeast Asia ───────────────────────────────────────────────────────
    "sin": "Singapore, SG",
    "kul": "Kuala Lumpur, MY",
    "pen": "Penang, MY",
    "bki": "Kota Kinabalu, MY",
    "lgk": "Langkawi, MY",
    "jhu": "Johor Bahru, MY",
    "cgk": "Jakarta, ID",
    "dps": "Bali, ID",
    "lop": "Lombok, ID",
    "jog": "Yogyakarta, ID",
    "sub": "Surabaya, ID",
    "bkk": "Bangkok, TH",
    "dmk": "Bangkok, TH",
    "hkt": "Phuket, TH",
    "usm": "Koh Samui, TH",
    "cnx": "Chiang Mai, TH",
    "krb": "Krabi, TH",
    "kbv": "Krabi, TH",
    "hdx": "Hua Hin, TH",
    "hdy": "Hat Yai, TH",
    "sgn": "Ho Chi Minh City, VN",
    "han": "Hanoi, VN",
    "dad": "Da Nang, VN",
    "vca": "Can Tho, VN",
    "mnl": "Manila, PH",
    "ceb": "Cebu, PH",
    "mpu": "Boracay, PH",       # Caticlan (Boracay)
    "ilo": "Iloilo, PH",
    "pnh": "Phnom Penh, KH",
    "rep": "Siem Reap, KH",
    "lpq": "Luang Prabang, LA",
    "vie": "Vientiane, LA",     # VTE is Vientiane
    "vte": "Vientiane, LA",
    "rgn": "Yangon, MM",
    "mdl": "Mandalay, MM",
    # ── Pacific ──────────────────────────────────────────────────────────────
    "syd": "Sydney, AU",
    "mel": "Melbourne, AU",
    "bne": "Brisbane, AU",
    "per": "Perth, AU",
    "adl": "Adelaide, AU",
    "ool": "Gold Coast, AU",
    "cns": "Cairns, AU",
    "drw": "Darwin, AU",
    "akl": "Auckland, NZ",
    "wlg": "Wellington, NZ",
    "chc": "Christchurch, NZ",
    "zqn": "Queenstown, NZ",
    "rot": "Rotorua, NZ",
    "nan": "Nadi, FJ",
    "suv": "Suva, FJ",
    "ppt": "Papeete, PF",
    "bob": "Bora Bora, PF",
    "moo": "Moorea, PF",
    "gum": "Tumon, GU",
    "spn": "Saipan, MP",
    "mle": "Malé, MV",
}

# ---------------------------------------------------------------------------
# City-hint table: (search_token, "City, CC")
#
# Searched against the combined lowercase URL slug + hotel name.
# Rules:
#   • Multi-word tokens must appear BEFORE any of their component words.
#   • Tokens are plain substrings — no regex.
#   • CC = ISO 3166-1 alpha-2 country code (CA = Canada, not California).
# ---------------------------------------------------------------------------
_CITY_HINTS: list = [
    # ── United States — Hawaii ───────────────────────────────────────────────
    ("ko olina",          "Honolulu, US"),
    ("turtle bay",        "Oahu, US"),
    ("waikiki",           "Honolulu, US"),
    ("honolulu",          "Honolulu, US"),
    ("kapalua",           "Maui, US"),
    ("kaanapali",         "Maui, US"),
    ("wailea",            "Maui, US"),
    ("lahaina",           "Maui, US"),
    ("grand wailea",      "Maui, US"),
    ("maui",              "Maui, US"),
    ("princeville",       "Kauai, US"),
    ("poipu",             "Kauai, US"),
    ("kauai",             "Kauai, US"),
    ("waikoloa",          "Big Island, US"),
    ("kohala",            "Big Island, US"),
    ("kona",              "Kona, US"),
    ("hilo",              "Hilo, US"),
    # ── United States — Nevada / Mountain West ───────────────────────────────
    ("las vegas",         "Las Vegas, US"),
    ("henderson",         "Henderson, US"),
    ("park city",         "Park City, US"),
    ("jackson hole",      "Jackson Hole, US"),
    ("sun valley",        "Sun Valley, US"),
    ("aspen",             "Aspen, US"),
    ("vail",              "Vail, US"),
    ("breckenridge",      "Breckenridge, US"),
    ("steamboat",         "Steamboat Springs, US"),
    ("telluride",         "Telluride, US"),
    ("snowmass",          "Snowmass, US"),
    ("lake tahoe",        "Lake Tahoe, US"),
    ("mammoth",           "Mammoth Lakes, US"),
    ("santa fe",          "Santa Fe, US"),
    ("taos",              "Taos, US"),
    ("albuquerque",       "Albuquerque, US"),
    ("salt lake",         "Salt Lake City, US"),
    ("denver",            "Denver, US"),
    ("boulder",           "Boulder, US"),
    ("bozeman",           "Bozeman, US"),
    ("whitefish",         "Whitefish, US"),
    # ── United States — California ───────────────────────────────────────────
    ("san francisco",     "San Francisco, US"),
    ("half moon bay",     "Half Moon Bay, US"),
    ("napa",              "Napa, US"),
    ("sonoma",            "Sonoma, US"),
    ("healdsburg",        "Healdsburg, US"),
    ("los angeles",       "Los Angeles, US"),
    ("beverly hills",     "Beverly Hills, US"),
    ("west hollywood",    "West Hollywood, US"),
    ("santa monica",      "Santa Monica, US"),
    ("long beach",        "Long Beach, US"),
    ("huntington beach",  "Huntington Beach, US"),
    ("newport beach",     "Newport Beach, US"),
    ("laguna beach",      "Laguna Beach, US"),
    ("dana point",        "Dana Point, US"),
    ("monarch beach",     "Dana Point, US"),
    ("san diego",         "San Diego, US"),
    ("del coronado",      "Coronado, US"),
    ("shore house",       "Coronado, US"),
    ("coronado",          "Coronado, US"),
    ("pebble beach",      "Pebble Beach, US"),
    ("carmel",            "Carmel, US"),
    ("monterey",          "Monterey, US"),
    ("santa barbara",     "Santa Barbara, US"),
    ("palm springs",      "Palm Springs, US"),
    ("palm desert",       "Palm Desert, US"),
    ("la quinta",         "La Quinta, US"),
    ("indian wells",      "Indian Wells, US"),
    ("rancho mirage",     "Rancho Mirage, US"),
    ("sacramento",        "Sacramento, US"),
    # ── United States — Arizona ──────────────────────────────────────────────
    ("scottsdale",        "Scottsdale, US"),
    ("phoenix",           "Phoenix, US"),
    ("sedona",            "Sedona, US"),
    ("tucson",            "Tucson, US"),
    # ── United States — Pacific Northwest ────────────────────────────────────
    ("seattle",           "Seattle, US"),
    ("bellevue",          "Bellevue, US"),
    ("spokane",           "Spokane, US"),
    ("portland",          "Portland, US"),
    ("bend",              "Bend, US"),
    ("anchorage",         "Anchorage, US"),
    # ── United States — Florida ──────────────────────────────────────────────
    ("miami beach",       "Miami Beach, US"),
    ("miami",             "Miami, US"),
    ("fort lauderdale",   "Fort Lauderdale, US"),
    ("boca raton",        "Boca Raton, US"),
    ("west palm beach",   "West Palm Beach, US"),
    ("palm beach",        "Palm Beach, US"),
    ("naples",            "Naples, US"),
    ("marco island",      "Marco Island, US"),
    ("bonnet creek",      "Orlando, US"),
    ("orlando",           "Orlando, US"),
    ("tampa",             "Tampa, US"),
    ("clearwater",        "Clearwater, US"),
    ("st augustine",      "St. Augustine, US"),
    ("jacksonville",      "Jacksonville, US"),
    ("key west",          "Key West, US"),
    ("destin",            "Destin, US"),
    ("gulf shores",       "Gulf Shores, US"),
    ("pensacola",         "Pensacola, US"),
    ("sarasota",          "Sarasota, US"),
    ("daytona",           "Daytona Beach, US"),
    # ── United States — Southeast ────────────────────────────────────────────
    ("atlanta",           "Atlanta, US"),
    ("savannah",          "Savannah, US"),
    ("hilton head",       "Hilton Head, US"),
    ("myrtle beach",      "Myrtle Beach, US"),
    ("kiawah",            "Kiawah Island, US"),
    ("charleston",        "Charleston, US"),
    ("asheville",         "Asheville, US"),
    ("charlotte",         "Charlotte, US"),
    ("outer banks",       "Outer Banks, US"),
    ("virginia beach",    "Virginia Beach, US"),
    ("williamsburg",      "Williamsburg, US"),
    ("richmond",          "Richmond, US"),
    ("new orleans",       "New Orleans, US"),
    ("baton rouge",       "Baton Rouge, US"),
    ("nashville",         "Nashville, US"),
    ("memphis",           "Memphis, US"),
    ("louisville",        "Louisville, US"),
    ("birmingham",        "Birmingham, US"),
    # ── United States — Mid-Atlantic / Northeast ─────────────────────────────
    ("washington dc",     "Washington DC, US"),
    ("national harbor",   "National Harbor, US"),
    ("mclean",            "McLean, US"),
    ("bethesda",          "Bethesda, US"),
    ("baltimore",         "Baltimore, US"),
    ("annapolis",         "Annapolis, US"),
    ("ocean city",        "Ocean City, US"),
    ("atlantic city",     "Atlantic City, US"),
    ("cape may",          "Cape May, US"),
    ("philadelphia",      "Philadelphia, US"),
    ("pittsburgh",        "Pittsburgh, US"),
    ("new york",          "New York, US"),
    ("manhattan",         "New York, US"),
    ("brooklyn",          "Brooklyn, US"),
    ("the hamptons",      "Hamptons, US"),
    ("hamptons",          "Hamptons, US"),
    ("niagara falls",     "Niagara Falls, US"),
    ("lake placid",       "Lake Placid, US"),
    ("long island",       "Long Island, US"),
    ("boston",            "Boston, US"),
    ("cape cod",          "Cape Cod, US"),
    ("nantucket",         "Nantucket, US"),
    ("martha",            "Martha's Vineyard, US"),
    ("newport",           "Newport, US"),
    ("bar harbor",        "Bar Harbor, US"),
    ("stowe",             "Stowe, US"),
    # ── United States — Midwest ──────────────────────────────────────────────
    ("chicago",           "Chicago, US"),
    ("detroit",           "Detroit, US"),
    ("cleveland",         "Cleveland, US"),
    ("columbus",          "Columbus, US"),
    ("cincinnati",        "Cincinnati, US"),
    ("indianapolis",      "Indianapolis, US"),
    ("milwaukee",         "Milwaukee, US"),
    ("minneapolis",       "Minneapolis, US"),
    ("st louis",          "St. Louis, US"),
    ("saint louis",       "St. Louis, US"),
    ("kansas city",       "Kansas City, US"),
    ("omaha",             "Omaha, US"),
    ("traverse city",     "Traverse City, US"),
    ("mackinac",          "Mackinac Island, US"),
    # ── United States — Texas ────────────────────────────────────────────────
    ("san antonio",       "San Antonio, US"),
    ("fort worth",        "Fort Worth, US"),
    ("dallas",            "Dallas, US"),
    ("houston",           "Houston, US"),
    ("austin",            "Austin, US"),
    ("galveston",         "Galveston, US"),
    ("el paso",           "El Paso, US"),
    # ── Canada ───────────────────────────────────────────────────────────────
    ("whistler",          "Whistler, CA"),
    ("banff",             "Banff, CA"),
    ("jasper",            "Jasper, CA"),
    ("kelowna",           "Kelowna, CA"),
    ("victoria",          "Victoria, CA"),
    ("vancouver",         "Vancouver, CA"),
    ("toronto",           "Toronto, CA"),
    ("ottawa",            "Ottawa, CA"),
    ("montreal",          "Montreal, CA"),
    ("québec",            "Quebec City, CA"),
    ("quebec",            "Quebec City, CA"),
    ("niagara",           "Niagara Falls, CA"),
    ("edmonton",          "Edmonton, CA"),
    ("calgary",           "Calgary, CA"),
    ("halifax",           "Halifax, CA"),
    # ── Mexico ───────────────────────────────────────────────────────────────
    ("cabo san lucas",    "Los Cabos, MX"),
    ("san jose del cabo", "Los Cabos, MX"),
    ("los cabos",         "Los Cabos, MX"),
    ("cancún",            "Cancun, MX"),
    ("cancun",            "Cancun, MX"),
    ("playa del carmen",  "Playa del Carmen, MX"),
    ("riviera maya",      "Riviera Maya, MX"),
    ("tulum",             "Tulum, MX"),
    ("cozumel",           "Cozumel, MX"),
    ("puerto vallarta",   "Puerto Vallarta, MX"),
    ("nuevo vallarta",    "Puerto Vallarta, MX"),
    ("punta mita",        "Punta Mita, MX"),
    ("huatulco",          "Huatulco, MX"),
    ("acapulco",          "Acapulco, MX"),
    ("puerto peñasco",    "Puerto Peñasco, MX"),
    ("puerto penasco",    "Puerto Peñasco, MX"),
    ("mazatlan",          "Mazatlán, MX"),
    ("ixtapa",            "Ixtapa, MX"),
    ("zihuatanejo",       "Zihuatanejo, MX"),
    ("manzanillo",        "Manzanillo, MX"),
    ("monterrey",         "Monterrey, MX"),
    ("guadalajara",       "Guadalajara, MX"),
    ("mexico city",       "Mexico City, MX"),
    ("ciudad de mexico",  "Mexico City, MX"),
    ("oaxaca",            "Oaxaca, MX"),
    ("mérida",            "Merida, MX"),
    ("merida",            "Merida, MX"),
    # ── Caribbean ────────────────────────────────────────────────────────────
    ("nassau",            "Nassau, BS"),
    ("paradise island",   "Nassau, BS"),
    ("turks and caicos",  "Turks and Caicos, TC"),
    ("providenciales",    "Turks and Caicos, TC"),
    ("punta cana",        "Punta Cana, DO"),
    ("santo domingo",     "Santo Domingo, DO"),
    ("san juan",          "San Juan, PR"),
    ("puerto rico",       "San Juan, PR"),
    ("st thomas",         "St. Thomas, VI"),
    ("saint thomas",      "St. Thomas, VI"),
    ("st croix",          "St. Croix, VI"),
    ("saint croix",       "St. Croix, VI"),
    ("aruba",             "Aruba, AW"),
    ("curaçao",           "Curaçao, CW"),
    ("curacao",           "Curaçao, CW"),
    ("sint maarten",      "Sint Maarten, SX"),
    ("st maarten",        "Sint Maarten, SX"),
    ("saint maarten",     "Sint Maarten, SX"),
    ("st martin",         "St. Martin, MF"),
    ("saint martin",      "St. Martin, MF"),
    ("st lucia",          "St. Lucia, LC"),
    ("saint lucia",       "St. Lucia, LC"),
    ("barbados",          "Barbados, BB"),
    ("bridgetown",        "Bridgetown, BB"),
    ("montego bay",       "Montego Bay, JM"),
    ("ocho rios",         "Ocho Rios, JM"),
    ("negril",            "Negril, JM"),
    ("jamaica",           "Jamaica, JM"),
    ("antigua",           "Antigua, AG"),
    ("st kitts",          "St. Kitts, KN"),
    ("nevis",             "Nevis, KN"),
    ("bermuda",           "Bermuda, BM"),
    ("grand cayman",      "Grand Cayman, KY"),
    ("cayman",            "Cayman Islands, KY"),
    ("belize",            "Belize City, BZ"),
    ("ambergris",         "Belize, BZ"),
    ("guadeloupe",        "Guadeloupe, GP"),
    ("martinique",        "Martinique, MQ"),
    ("havana",            "Havana, CU"),
    ("trinidad",          "Trinidad, TT"),
    ("tobago",            "Tobago, TT"),
    # ── Central & South America ──────────────────────────────────────────────
    ("guanacaste",        "Guanacaste, CR"),
    ("papagayo",          "Guanacaste, CR"),
    ("manuel antonio",    "Manuel Antonio, CR"),
    ("tamarindo",         "Tamarindo, CR"),
    ("costa rica",        "San José, CR"),
    ("panama city",       "Panama City, PA"),
    ("cartagena",         "Cartagena, CO"),
    ("bogotá",            "Bogotá, CO"),
    ("bogota",            "Bogotá, CO"),
    ("medellín",          "Medellín, CO"),
    ("medellin",          "Medellín, CO"),
    ("machu picchu",      "Cusco, PE"),
    ("cusco",             "Cusco, PE"),
    ("lima",              "Lima, PE"),
    ("quito",             "Quito, EC"),
    ("galapagos",         "Galápagos, EC"),
    ("galápagos",         "Galápagos, EC"),
    ("rio de janeiro",    "Rio de Janeiro, BR"),
    ("são paulo",         "São Paulo, BR"),
    ("sao paulo",         "São Paulo, BR"),
    ("salvador",          "Salvador, BR"),
    ("fortaleza",         "Fortaleza, BR"),
    ("buenos aires",      "Buenos Aires, AR"),
    ("mendoza",           "Mendoza, AR"),
    ("bariloche",         "Bariloche, AR"),
    ("punta del este",    "Punta del Este, UY"),
    ("montevideo",        "Montevideo, UY"),
    ("santiago",          "Santiago, CL"),
    # ── United Kingdom & Ireland ─────────────────────────────────────────────
    ("london",            "London, GB"),
    ("edinburgh",         "Edinburgh, GB"),
    ("glasgow",           "Glasgow, GB"),
    ("manchester",        "Manchester, GB"),
    ("bath",              "Bath, GB"),
    ("oxford",            "Oxford, GB"),
    ("liverpool",         "Liverpool, GB"),
    ("bristol",           "Bristol, GB"),
    ("cardiff",           "Cardiff, GB"),
    ("dublin",            "Dublin, IE"),
    ("galway",            "Galway, IE"),
    ("cork",              "Cork, IE"),
    # ── France ───────────────────────────────────────────────────────────────
    ("monte carlo",       "Monaco, MC"),
    ("monaco",            "Monaco, MC"),
    ("saint tropez",      "Saint-Tropez, FR"),
    ("st tropez",         "Saint-Tropez, FR"),
    ("courchevel",        "Courchevel, FR"),
    ("chamonix",          "Chamonix, FR"),
    ("cannes",            "Cannes, FR"),
    ("antibes",           "Antibes, FR"),
    ("biarritz",          "Biarritz, FR"),
    ("bordeaux",          "Bordeaux, FR"),
    ("marseille",         "Marseille, FR"),
    ("strasbourg",        "Strasbourg, FR"),
    ("lyon",              "Lyon, FR"),
    ("paris",             "Paris, FR"),
    ("nice",              "Nice, FR"),
    # ── Italy ────────────────────────────────────────────────────────────────
    ("lake como",         "Lake Como, IT"),
    ("amalfi",            "Amalfi Coast, IT"),
    ("positano",          "Positano, IT"),
    ("sorrento",          "Sorrento, IT"),
    ("capri",             "Capri, IT"),
    ("rome cavalieri",    "Rome, IT"),
    ("rome",              "Rome, IT"),
    ("milan",             "Milan, IT"),
    ("florence",          "Florence, IT"),
    ("venice",            "Venice, IT"),
    ("tuscany",           "Tuscany, IT"),
    ("sardinia",          "Sardinia, IT"),
    ("sicily",            "Sicily, IT"),
    ("palermo",           "Palermo, IT"),
    ("verona",            "Verona, IT"),
    ("bologna",           "Bologna, IT"),
    ("turin",             "Turin, IT"),
    # ── Spain ────────────────────────────────────────────────────────────────
    ("san sebastian",     "San Sebastián, ES"),
    ("gran canaria",      "Gran Canaria, ES"),
    ("las palmas",        "Las Palmas, ES"),
    ("marbella",          "Marbella, ES"),
    ("ibiza",             "Ibiza, ES"),
    ("mallorca",          "Mallorca, ES"),
    ("menorca",           "Menorca, ES"),
    ("tenerife",          "Tenerife, ES"),
    ("lanzarote",         "Lanzarote, ES"),
    ("fuerteventura",     "Fuerteventura, ES"),
    ("madrid",            "Madrid, ES"),
    ("barcelona",         "Barcelona, ES"),
    ("seville",           "Seville, ES"),
    ("sevilla",           "Seville, ES"),
    ("granada",           "Granada, ES"),
    ("bilbao",            "Bilbao, ES"),
    ("valencia",          "Valencia, ES"),
    ("málaga",            "Málaga, ES"),
    ("malaga",            "Málaga, ES"),
    ("palma",             "Palma, ES"),
    ("córdoba",           "Córdoba, ES"),
    ("cordoba",           "Córdoba, ES"),
    # ── Portugal ─────────────────────────────────────────────────────────────
    ("algarve",           "Algarve, PT"),
    ("madeira",           "Madeira, PT"),
    ("azores",            "Azores, PT"),
    ("cascais",           "Cascais, PT"),
    ("sintra",            "Sintra, PT"),
    ("lisbon",            "Lisbon, PT"),
    ("lisboa",            "Lisbon, PT"),
    ("porto",             "Porto, PT"),
    # ── Germany ──────────────────────────────────────────────────────────────
    ("berlin",            "Berlin, DE"),
    ("münchen",           "Munich, DE"),
    ("munich",            "Munich, DE"),
    ("frankfurt",         "Frankfurt, DE"),
    ("hamburg",           "Hamburg, DE"),
    ("cologne",           "Cologne, DE"),
    ("köln",              "Cologne, DE"),
    ("düsseldorf",        "Düsseldorf, DE"),
    ("dusseldorf",        "Düsseldorf, DE"),
    ("stuttgart",         "Stuttgart, DE"),
    ("dresden",           "Dresden, DE"),
    ("heidelberg",        "Heidelberg, DE"),
    ("nuremberg",         "Nuremberg, DE"),
    ("nürnberg",          "Nuremberg, DE"),
    # ── Austria / Switzerland ────────────────────────────────────────────────
    ("vienna",            "Vienna, AT"),
    ("wien",              "Vienna, AT"),
    ("salzburg",          "Salzburg, AT"),
    ("innsbruck",         "Innsbruck, AT"),
    ("kitzbühel",         "Kitzbühel, AT"),
    ("st moritz",         "St. Moritz, CH"),
    ("zermatt",           "Zermatt, CH"),
    ("interlaken",        "Interlaken, CH"),
    ("davos",             "Davos, CH"),
    ("montreux",          "Montreux, CH"),
    ("lausanne",          "Lausanne, CH"),
    ("zurich",            "Zurich, CH"),
    ("zürich",            "Zurich, CH"),
    ("geneva",            "Geneva, CH"),
    ("genève",            "Geneva, CH"),
    ("lucerne",           "Lucerne, CH"),
    ("luzern",            "Lucerne, CH"),
    ("basel",             "Basel, CH"),
    # ── Netherlands / Belgium ────────────────────────────────────────────────
    ("amsterdam",         "Amsterdam, NL"),
    ("rotterdam",         "Rotterdam, NL"),
    ("the hague",         "The Hague, NL"),
    ("brussels",          "Brussels, BE"),
    ("bruxelles",         "Brussels, BE"),
    ("bruges",            "Bruges, BE"),
    ("antwerp",           "Antwerp, BE"),
    ("ghent",             "Ghent, BE"),
    # ── Scandinavia & Baltics ────────────────────────────────────────────────
    ("stockholm",         "Stockholm, SE"),
    ("gothenburg",        "Gothenburg, SE"),
    ("göteborg",          "Gothenburg, SE"),
    ("malmö",             "Malmö, SE"),
    ("malmo",             "Malmö, SE"),
    ("copenhagen",        "Copenhagen, DK"),
    ("oslo",              "Oslo, NO"),
    ("bergen",            "Bergen, NO"),
    ("helsinki",          "Helsinki, FI"),
    ("reykjavík",         "Reykjavik, IS"),
    ("reykjavik",         "Reykjavik, IS"),
    ("tallinn",           "Tallinn, EE"),
    ("riga",              "Riga, LV"),
    ("vilnius",           "Vilnius, LT"),
    # ── Eastern Europe ───────────────────────────────────────────────────────
    ("warsaw",            "Warsaw, PL"),
    ("kraków",            "Kraków, PL"),
    ("krakow",            "Kraków, PL"),
    ("prague",            "Prague, CZ"),
    ("budapest",          "Budapest, HU"),
    ("bucharest",         "Bucharest, RO"),
    ("sofia",             "Sofia, BG"),
    ("dubrovnik",         "Dubrovnik, HR"),
    ("split",             "Split, HR"),
    ("zagreb",            "Zagreb, HR"),
    ("hvar",              "Hvar, HR"),
    ("ljubljana",         "Ljubljana, SI"),
    ("belgrade",          "Belgrade, RS"),
    ("sarajevo",          "Sarajevo, BA"),
    ("athens",            "Athens, GR"),
    ("mykonos",           "Mykonos, GR"),
    ("santorini",         "Santorini, GR"),
    ("crete",             "Crete, GR"),
    ("rhodes",            "Rhodes, GR"),
    ("corfu",             "Corfu, GR"),
    ("thessaloniki",      "Thessaloniki, GR"),
    ("kyiv",              "Kyiv, UA"),
    ("moscow",            "Moscow, RU"),
    ("st petersburg",     "St. Petersburg, RU"),
    ("saint petersburg",  "St. Petersburg, RU"),
    ("tbilisi",           "Tbilisi, GE"),
    ("baku",              "Baku, AZ"),
    ("yerevan",           "Yerevan, AM"),
    ("almaty",            "Almaty, KZ"),
    ("astana",            "Astana, KZ"),
    ("tashkent",          "Tashkent, UZ"),
    # ── Turkey ───────────────────────────────────────────────────────────────
    ("istanbul",          "Istanbul, TR"),
    ("ankara",            "Ankara, TR"),
    ("antalya",           "Antalya, TR"),
    ("bodrum",            "Bodrum, TR"),
    ("cappadocia",        "Cappadocia, TR"),
    ("kapadokya",         "Cappadocia, TR"),
    ("alanya",            "Alanya, TR"),
    ("fethiye",           "Fethiye, TR"),
    ("izmir",             "İzmir, TR"),
    # ── Middle East ──────────────────────────────────────────────────────────
    ("abu dhabi",         "Abu Dhabi, AE"),
    ("dubai",             "Dubai, AE"),
    ("sharjah",           "Sharjah, AE"),
    ("ras al khaimah",    "Ras Al Khaimah, AE"),
    ("fujairah",          "Fujairah, AE"),
    ("doha",              "Doha, QA"),
    ("riyadh",            "Riyadh, SA"),
    ("jeddah",            "Jeddah, SA"),
    ("makkah",            "Mecca, SA"),
    ("mecca",             "Mecca, SA"),
    ("medina",            "Medina, SA"),
    ("al khobar",         "Al Khobar, SA"),
    ("dammam",            "Dammam, SA"),
    ("neom",              "NEOM, SA"),
    ("muscat",            "Muscat, OM"),
    ("salalah",           "Salalah, OM"),
    ("kuwait",            "Kuwait City, KW"),
    ("manama",            "Manama, BH"),
    ("bahrain",           "Manama, BH"),
    ("amman",             "Amman, JO"),
    ("petra",             "Petra, JO"),
    ("aqaba",             "Aqaba, JO"),
    ("dead sea",          "Dead Sea, JO"),
    ("beirut",            "Beirut, LB"),
    ("tel aviv",          "Tel Aviv, IL"),
    ("jerusalem",         "Jerusalem, IL"),
    ("eilat",             "Eilat, IL"),
    # ── Africa ───────────────────────────────────────────────────────────────
    ("sharm el sheikh",   "Sharm el-Sheikh, EG"),
    ("sharm",             "Sharm el-Sheikh, EG"),
    ("hurghada",          "Hurghada, EG"),
    ("luxor",             "Luxor, EG"),
    ("aswan",             "Aswan, EG"),
    ("cairo",             "Cairo, EG"),
    ("alexandria",        "Alexandria, EG"),
    ("marrakech",         "Marrakech, MA"),
    ("marrakesh",         "Marrakech, MA"),
    ("casablanca",        "Casablanca, MA"),
    ("tangier",           "Tangier, MA"),
    ("agadir",            "Agadir, MA"),
    ("tunis",             "Tunis, TN"),
    ("djerba",            "Djerba, TN"),
    ("cape town",         "Cape Town, ZA"),
    ("johannesburg",      "Johannesburg, ZA"),
    ("durban",            "Durban, ZA"),
    ("stellenbosch",      "Stellenbosch, ZA"),
    ("nairobi",           "Nairobi, KE"),
    ("mombasa",           "Mombasa, KE"),
    ("masai mara",        "Masai Mara, KE"),
    ("zanzibar",          "Zanzibar, TZ"),
    ("dar es salaam",     "Dar es Salaam, TZ"),
    ("serengeti",         "Serengeti, TZ"),
    ("kigali",            "Kigali, RW"),
    ("addis ababa",       "Addis Ababa, ET"),
    ("lagos",             "Lagos, NG"),
    ("abuja",             "Abuja, NG"),
    ("accra",             "Accra, GH"),
    ("dakar",             "Dakar, SN"),
    ("seychelles",        "Seychelles, SC"),
    ("mahé",              "Seychelles, SC"),
    ("mahe",              "Seychelles, SC"),
    ("mauritius",         "Mauritius, MU"),
    ("maldives",          "Maldives, MV"),
    # ── India & South Asia ───────────────────────────────────────────────────
    ("new delhi",         "New Delhi, IN"),
    ("mumbai",            "Mumbai, IN"),
    ("bombay",            "Mumbai, IN"),
    ("bengaluru",         "Bengaluru, IN"),
    ("bangalore",         "Bengaluru, IN"),
    ("chennai",           "Chennai, IN"),
    ("kolkata",           "Kolkata, IN"),
    ("hyderabad",         "Hyderabad, IN"),
    ("jaipur",            "Jaipur, IN"),
    ("udaipur",           "Udaipur, IN"),
    ("jodhpur",           "Jodhpur, IN"),
    ("agra",              "Agra, IN"),
    ("varanasi",          "Varanasi, IN"),
    ("amritsar",          "Amritsar, IN"),
    ("goa",               "Goa, IN"),
    ("kochi",             "Kochi, IN"),
    ("kerala",            "Kerala, IN"),
    ("pune",              "Pune, IN"),
    ("ahmedabad",         "Ahmedabad, IN"),
    ("chandigarh",        "Chandigarh, IN"),
    ("delhi",             "New Delhi, IN"),
    ("colombo",           "Colombo, LK"),
    ("kandy",             "Kandy, LK"),
    ("galle",             "Galle, LK"),
    ("kathmandu",         "Kathmandu, NP"),
    ("dhaka",             "Dhaka, BD"),
    ("islamabad",         "Islamabad, PK"),
    ("lahore",            "Lahore, PK"),
    ("karachi",           "Karachi, PK"),
    # ── East & Southeast Asia ────────────────────────────────────────────────
    ("hong kong",         "Hong Kong, HK"),
    ("macau",             "Macau, MO"),
    ("macao",             "Macau, MO"),
    ("tokyo",             "Tokyo, JP"),
    ("osaka",             "Osaka, JP"),
    ("kyoto",             "Kyoto, JP"),
    ("hiroshima",         "Hiroshima, JP"),
    ("sapporo",           "Sapporo, JP"),
    ("okinawa",           "Okinawa, JP"),
    ("fukuoka",           "Fukuoka, JP"),
    ("yokohama",          "Yokohama, JP"),
    ("nagoya",            "Nagoya, JP"),
    ("nara",              "Nara, JP"),
    ("kobe",              "Kobe, JP"),
    ("seoul",             "Seoul, KR"),
    ("busan",             "Busan, KR"),
    ("jeju",              "Jeju, KR"),
    ("beijing",           "Beijing, CN"),
    ("shanghai",          "Shanghai, CN"),
    ("guangzhou",         "Guangzhou, CN"),
    ("shenzhen",          "Shenzhen, CN"),
    ("chengdu",           "Chengdu, CN"),
    ("chongqing",         "Chongqing, CN"),
    ("xi'an",             "Xi'an, CN"),
    ("xian",              "Xi'an, CN"),
    ("hangzhou",          "Hangzhou, CN"),
    ("nanjing",           "Nanjing, CN"),
    ("wuhan",             "Wuhan, CN"),
    ("kunming",           "Kunming, CN"),
    ("guilin",            "Guilin, CN"),
    ("sanya",             "Sanya, CN"),
    ("hainan",            "Hainan, CN"),
    ("harbin",            "Harbin, CN"),
    ("qingdao",           "Qingdao, CN"),
    ("dalian",            "Dalian, CN"),
    ("tianjin",           "Tianjin, CN"),
    ("suzhou",            "Suzhou, CN"),
    ("ningbo",            "Ningbo, CN"),
    ("taipei",            "Taipei, TW"),
    ("kaohsiung",         "Kaohsiung, TW"),
    ("singapore",         "Singapore, SG"),
    ("kuala lumpur",      "Kuala Lumpur, MY"),
    ("penang",            "Penang, MY"),
    ("kota kinabalu",     "Kota Kinabalu, MY"),
    ("langkawi",          "Langkawi, MY"),
    ("jakarta",           "Jakarta, ID"),
    ("nusa dua",          "Nusa Dua, ID"),
    ("seminyak",          "Seminyak, ID"),
    ("jimbaran",          "Jimbaran, ID"),
    ("ubud",              "Ubud, ID"),
    ("kuta",              "Kuta, ID"),
    ("lombok",            "Lombok, ID"),
    ("bali",              "Bali, ID"),
    ("yogyakarta",        "Yogyakarta, ID"),
    ("surabaya",          "Surabaya, ID"),
    ("chiang mai",        "Chiang Mai, TH"),
    ("koh samui",         "Koh Samui, TH"),
    ("hua hin",           "Hua Hin, TH"),
    ("phuket",            "Phuket, TH"),
    ("krabi",             "Krabi, TH"),
    ("pattaya",           "Pattaya, TH"),
    ("bangkok",           "Bangkok, TH"),
    ("ho chi minh",       "Ho Chi Minh City, VN"),
    ("saigon",            "Ho Chi Minh City, VN"),
    ("da nang",           "Da Nang, VN"),
    ("hoi an",            "Hoi An, VN"),
    ("nha trang",         "Nha Trang, VN"),
    ("halong",            "Ha Long, VN"),
    ("hanoi",             "Hanoi, VN"),
    ("siem reap",         "Siem Reap, KH"),
    ("phnom penh",        "Phnom Penh, KH"),
    ("luang prabang",     "Luang Prabang, LA"),
    ("yangon",            "Yangon, MM"),
    ("mandalay",          "Mandalay, MM"),
    ("manila",            "Manila, PH"),
    ("boracay",           "Boracay, PH"),
    ("cebu",              "Cebu, PH"),
    ("palawan",           "Palawan, PH"),
    ("makati",            "Makati, PH"),
    ("bgc",               "Bonifacio Global City, PH"),
    # ── Pacific ──────────────────────────────────────────────────────────────
    ("bora bora",         "Bora Bora, PF"),
    ("moorea",            "Moorea, PF"),
    ("tahiti",            "Papeete, PF"),
    ("gold coast",        "Gold Coast, AU"),
    ("sydney",            "Sydney, AU"),
    ("melbourne",         "Melbourne, AU"),
    ("brisbane",          "Brisbane, AU"),
    ("perth",             "Perth, AU"),
    ("adelaide",          "Adelaide, AU"),
    ("cairns",            "Cairns, AU"),
    ("darwin",            "Darwin, AU"),
    ("queenstown",        "Queenstown, NZ"),
    ("auckland",          "Auckland, NZ"),
    ("wellington",        "Wellington, NZ"),
    ("christchurch",      "Christchurch, NZ"),
    ("rotorua",           "Rotorua, NZ"),
    ("denarau",           "Denarau, FJ"),
    ("fiji",              "Nadi, FJ"),
    ("nadi",              "Nadi, FJ"),
    ("guam",              "Tumon, GU"),
    ("tumon",             "Tumon, GU"),
    ("saipan",            "Saipan, MP"),
]


def _location_from_slug(url: str, hotel_name: str = "") -> str:
    """Derive hotel_location from the Hilton property URL and hotel name.

    Two-tier lookup:

    Tier 1 — 3-char property-code prefix (most precise):
        Hilton codes follow {CITY:3}{PROPERTY:2}{BRAND:2}.  The first three
        characters are usually an IATA airport code or a Hilton-internal city
        abbreviation, looked up in _CODE_TO_LOCATION.

    Tier 2 — token search in URL slug + hotel name:
        Falls back to scanning the combined lowercase text for entries in
        _CITY_HINTS when the 3-char code is unknown or ambiguous.

    Returns a "City, CC" string, or "" when neither tier matches.
    """
    canonical = _normalize_fetch_url(url)

    # ── Tier 1: property-code prefix ─────────────────────────────────────────
    m = re.search(r'/hotels/([a-z]{3})[a-z]{4}-', canonical)
    if m:
        loc = _CODE_TO_LOCATION.get(m.group(1))
        if loc:
            return loc

    # ── Tier 2: token search in slug + hotel name ─────────────────────────────
    slug = ""
    m2 = re.search(r'/hotels/[^/]+-(.+?)/?$', canonical)
    if m2:
        slug = m2.group(1).replace('-', ' ')
    combined = (slug + " " + hotel_name).lower()
    for token, location in _CITY_HINTS:
        if token in combined:
            return location

    return ""


def backfill_addresses(df: pd.DataFrame) -> pd.DataFrame:
    """Fill hotel_location for rows where it is missing, using URL/name city-hint matching.

    Hilton's CDN blocks automated HTTP fetches, so full street addresses cannot
    be reliably retrieved.  hotel_address is left empty for new hotels; the
    geocoder (google-convert.py) falls back to hotel_name + hotel_location
    when hotel_address is absent.
    """
    mask = df["hotel_location"].str.strip() == ""
    if not mask.any():
        return df

    n = int(mask.sum())
    print(f"Deriving location from URL/name for {n} hotels...")
    df = df.copy()
    df.loc[mask, "hotel_location"] = df.loc[mask].apply(
        lambda r: _location_from_slug(r["hotel_url"], r["hotel_name"]), axis=1
    )
    resolved = int((df.loc[mask, "hotel_location"].str.strip() != "").sum())
    unresolved = n - resolved
    print(f"  Resolved {resolved}/{n} hotels"
          + (f" ({unresolved} unmatched — extend _CITY_HINTS if needed)" if unresolved else ""))
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
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
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

        df = backfill_addresses(df)
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

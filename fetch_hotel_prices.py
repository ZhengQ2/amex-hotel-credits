#!/usr/bin/env python3
"""Match map hotels to Google Hotels through SerpApi and publish approximate USD nightly prices.

Uses only the Python standard library. API documentation:
https://serpapi.com/google-hotels-api
https://serpapi.com/google-hotels-price-calendar-api
https://serpapi.com/account-api
"""

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import shlex
import statistics
import tempfile
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


API_BASE = "https://serpapi.com"
SCHEMA_VERSION = 1
SOURCE = "Google Hotels via SerpApi"
# Prices for 2 adults in USD, as Google Hotels shows them to a US visitor.
COMMON_PARAMS = {"adults": 2, "currency": "USD", "gl": "us", "hl": "en"}
WINDOW_START_DAYS, WINDOW_END_DAYS = 14, 104
MIN_NIGHTS = 21
MATCHER_VERSION = 2  # Bump when match rules change: stored candidates are re-decided for free.
GENERIC_WORDS = frozenset({"a", "an", "and", "at", "by", "hotel", "hotels", "of", "the"})
SEPARATE_LISTING_WORDS = frozenset({"apartment", "apartments", "condo", "condos", "residence", "residences",
                                    "suite", "suites", "villa", "villas"})
CONTAINED_MAX_M = 150
# Each hotel's price is re-checked at least every REFRESH_DAYS. The daily rotation puts prices older than
# DUE_DAYS ahead of new hotels, and the map hides a price EXPIRE_DAYS after it was observed.
REFRESH_DAYS, DUE_DAYS, EXPIRE_DAYS = 180, 150, 200
RECHECK_UNMATCHED_DAYS = 180
NO_RESULTS = "returned any results"  # "Google Hotels hasn't returned any results for this query."
# Before-tax prices compare most consistently across countries, so prefer them.
PRICE_FIELDS = (("excluded", "extracted_before_taxes_fees"),
                ("included", "extracted_after_taxes_fees"),
                ("unknown", "extracted_display_price"))


class PriceError(Exception):
    pass


class BudgetError(PriceError):
    pass


class RunBudgetReached(BudgetError):
    """This run's allowance is spent: a normal end for a paced daily run."""


class SchemaError(PriceError):
    pass


def utcnow():
    return datetime.now(timezone.utc)


def timestamp(value):
    return value.isoformat().replace("+00:00", "Z")


def parse_timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return parsed


def window(now):
    return now.date() + timedelta(days=WINDOW_START_DAYS), now.date() + timedelta(days=WINDOW_END_DAYS)


def read_json(path, default=None):
    if not Path(path).exists():
        return default
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def state_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / ".lock", "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PriceError("Another price job is using this state directory") from error
        yield


def api_key(env_file=".env"):
    key = os.environ.get("SERPAPI_API_KEY", "").strip()
    if key:
        return key
    path = Path(env_file)
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            name, separator, value = line.strip().removeprefix("export ").partition("=")
            if separator and name.strip() == "SERPAPI_API_KEY":
                parts = shlex.split(value, comments=True)
                if len(parts) == 1 and parts[0]:
                    return parts[0]
    raise PriceError("Add SERPAPI_API_KEY to the local .env file or environment; never commit it")


def normalized(text):
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().lower()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def similarity(left, right):
    a, b = normalized(left), normalized(right)
    if not a or not b:
        return 0.0
    return max(SequenceMatcher(None, a, b).ratio(),
               SequenceMatcher(None, " ".join(sorted(a.split())),
                               " ".join(sorted(b.split()))).ratio())


def distance_m(lat1, lon1, lat2, lon2):
    a, b = math.radians(lat1), math.radians(lat2)
    h = math.sin((b - a) / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371000 * 2 * math.asin(min(1, math.sqrt(h)))


def fingerprint(rows):
    identity = sorted((row["n"], row.get("a", ""), row["lat"], row["lon"]) for row in rows)
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def hotel_groups(hotels):
    groups = defaultdict(list)
    for hotel in hotels:
        if hotel.get("id"):
            groups[hotel["id"]].append(hotel)
    return dict(groups)


def conflicting_identity(rows):
    first = rows[0]
    return any(similarity(first["n"], row["n"]) < 0.75 or
               distance_m(first["lat"], first["lon"], row["lat"], row["lon"]) > 100
               for row in rows[1:])


def select_pilot(groups, limit):
    # Round-robin programs and geographic cells to avoid a pilot of one brand/city.
    programs = {}
    for hotel_id, rows in sorted(groups.items()):
        hotel = rows[0]
        cells = programs.setdefault(hotel["p"], {})
        cells.setdefault((int(hotel["lat"] // 10), int(hotel["lon"] // 10)), []).append(hotel_id)
    selected = []
    while programs and len(selected) < limit:
        for program in list(sorted(programs)):
            cells = programs[program]
            cell = next(iter(cells))
            candidates = cells.pop(cell)
            selected.append(candidates.pop(0))
            if candidates:
                cells[cell] = candidates
            if not cells:
                del programs[program]
            if len(selected) == limit:
                break
    return {hotel_id: groups[hotel_id] for hotel_id in selected}


def search_query(rows):
    """Hotel name plus the broad end of its address: long street descriptions confuse Google Hotels."""
    hotel = rows[0]
    address = hotel.get("a", "").strip()
    parts = [part.strip() for part in address.split(",")]
    if len(parts) == 1:
        parts = [part.strip() for part in address.split(" - ")]
    parts = [part for part in parts if part and not part.isdigit()]
    return ", ".join([hotel["n"].strip()] + parts[-2:])


def candidates_from(payload):
    """Normalize a Google Hotels response into candidate properties."""
    if payload is None:
        return []
    if isinstance(payload.get("properties"), list):
        items = payload["properties"]
    elif "properties" in payload:
        raise SchemaError("Unexpected Google Hotels properties field")
    elif payload.get("property_token"):
        items = [payload]  # Google resolved the query straight to one property's page.
    else:
        raise SchemaError("Google Hotels response had neither properties nor a property")
    candidates = []
    for item in items:
        if not isinstance(item, dict) or str(item.get("type", "hotel")).lower() == "vacation rental":
            continue
        token, name, coordinates = item.get("property_token"), item.get("name"), item.get("gps_coordinates")
        try:
            lat, lon = float(coordinates["latitude"]), float(coordinates["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (isinstance(token, str) and token and isinstance(name, str) and name.strip()):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        address = item.get("address") if isinstance(item.get("address"), str) else ""
        candidates.append({"id": token, "name": name, "address": address, "latitude": lat, "longitude": lon})
    return candidates


def contained(hotel_name, candidate_name):
    """True when one name's distinctive words all appear in the other, as with brand suffixes or a dropped city."""
    ours = set(normalized(hotel_name).split()) - GENERIC_WORDS
    theirs = set(normalized(candidate_name).split()) - GENERIC_WORDS
    if not ours or not theirs:
        return False
    if ours <= theirs:
        return not (theirs - ours) & SEPARATE_LISTING_WORDS  # "X Residences" is usually its own listing.
    return theirs <= ours


def name_score(rows, candidate_name, distance):
    score = max(similarity(row["n"], candidate_name) for row in rows)
    # "Hotel Locarno" for "Hotel Locarno Roma", "Thompson Palm Springs, by Hyatt": trusted only on the same spot.
    if distance <= CONTAINED_MAX_M and any(contained(row["n"], candidate_name) for row in rows):
        score = max(score, 0.9)
    return score


def decide(rows, evidence):
    """Pick a property from candidates within 1 km; stored evidence lets rule changes re-decide without searching."""
    ranked = []
    for item in evidence:
        distance = item["distance_m"]
        score = name_score(rows, item["name"], distance)
        # Places and Google Hotels share Google's coordinates, so a true match is close by.
        eligible = (distance <= 300 and score >= 0.85) or (distance <= 1000 and score >= 0.95)
        ranked.append((0.9 * score + 0.1 * max(0, 1 - distance / 1000), eligible, {**item, "name_score": round(score, 3)}))
    ranked.sort(key=lambda item: item[0], reverse=True)
    kept = [item[2] for item in ranked[:5]]
    if not ranked:
        return {"status": "unmatched", "candidates": []}
    best = ranked[0]
    if not best[1] or (len(ranked) > 1 and best[0] - ranked[1][0] < 0.08):
        return {"status": "ambiguous", "candidates": kept}
    return {"status": "matched", "provider_id": best[2]["provider_id"], "candidates": kept}


def match_hotel(rows, candidates):
    if conflicting_identity(rows):
        return {"status": "identity_conflict"}
    hotel, evidence, seen = rows[0], [], set()
    for candidate in candidates:
        if candidate["id"] in seen:
            continue
        seen.add(candidate["id"])
        distance = distance_m(hotel["lat"], hotel["lon"], candidate["latitude"], candidate["longitude"])
        if distance <= 1000:
            evidence.append({"provider_id": candidate["id"], "name": candidate["name"],
                             "address": candidate["address"], "distance_m": round(distance)})
    if not evidence:
        return {"status": "unmatched", "candidates": [c["name"] for c in candidates[:3]]}
    return decide(rows, evidence)


class Budget:
    """Persist each reservation BEFORE its request, including failures and retries."""

    def __init__(self, path, initialize=False, monthly_limit=250, max_requests=25, now=None, reservation_id=None,
                 clock=utcnow):
        self.path, self.monthly_limit, self.max_requests, self.used = Path(path), monthly_limit, max_requests, 0
        self.month = (now or utcnow()).strftime("%Y-%m")
        # The hour is read at each reservation: a slow run can span several hours.
        self.clock, self.hourly_limit, self.reservation_id = clock, 50, reservation_id
        self.state = read_json(path)
        if self.state is None:
            if not initialize:
                raise BudgetError("Usage ledger missing. Use --initialize-budget only for a genuinely new budget; restore it after lost state")
            self.state = {"version": 1, "provider": "serpapi", "months": {}, "hours": {}}
        if self.state.get("version") != 1 or not isinstance(self.state.get("months"), dict):
            raise BudgetError("Invalid usage ledger; restore the last good state")
        for counts in self.state["months"].values():
            if not isinstance(counts, dict) or type(counts.get("search")) is not int or counts["search"] < 0:
                raise BudgetError("Invalid usage counters; restore the last good state")
        reservation = self.state.get("reservations", {}).get(reservation_id) if reservation_id else None
        if reservation:  # A month-end reservation may hold less than was asked for.
            self.max_requests = min(self.max_requests, reservation["allowance"] - reservation["used"])
        atomic_json(self.path, self.state)

    def reserve_run(self, reservation_id, requests):
        reservations = self.state.setdefault("reservations", {})
        counts = self.state["months"].setdefault(self.month, {"search": 0})
        if not reservation_id or reservation_id in reservations or requests < 1:
            raise BudgetError("A run reservation needs a new ID and positive allowance")
        requests = min(requests, self.monthly_limit - counts["search"])
        if requests < 1:
            raise BudgetError("No monthly allowance left to reserve")
        counts["search"] += requests
        reservations[reservation_id] = {"month": self.month, "allowance": requests, "used": 0}
        atomic_json(self.path, self.state)
        return requests

    def settle(self):
        """Release a finished run's unused allowance, so daily reservations do not use up the month."""
        reservation = self.state.get("reservations", {}).get(self.reservation_id) if self.reservation_id else None
        if not reservation or reservation["used"] >= reservation["allowance"]:
            return
        self.state["months"][reservation["month"]]["search"] -= reservation["allowance"] - reservation["used"]
        reservation["allowance"] = reservation["used"]
        atomic_json(self.path, self.state)

    @property
    def remaining(self):
        return max(0, self.max_requests - self.used)

    def reserve(self):
        counts = self.state["months"].setdefault(self.month, {"search": 0})
        if self.used >= self.max_requests:
            raise RunBudgetReached("This run's search allowance is used up; the rest waits for the next run")
        current = self.clock()
        hour, oldest = current.strftime("%Y-%m-%dT%H"), (current - timedelta(days=2)).strftime("%Y-%m-%dT%H")
        hours = {key: value for key, value in self.state.get("hours", {}).items() if key >= oldest}
        self.state["hours"] = hours
        if hours.get(hour, 0) >= self.hourly_limit:
            raise BudgetError("Hourly search allowance reached; resume in a later hour")
        if self.reservation_id:
            reservation = self.state.get("reservations", {}).get(self.reservation_id)
            if not reservation or reservation["month"] != self.month or reservation["used"] >= reservation["allowance"]:
                raise BudgetError("Run reservation missing, expired or exhausted")
            reservation["used"] += 1
        else:
            if counts["search"] >= self.monthly_limit:
                raise BudgetError("Monthly search allowance reached")
            counts["search"] += 1
        hours[hour] = hours.get(hour, 0) + 1
        atomic_json(self.path, self.state)
        self.used += 1


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Never forward an API key to another host.


def retry_after(value, now=None):
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0, (parsedate_to_datetime(value) - (now or utcnow())).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


class SerpApi:
    def __init__(self, key, budget, min_interval=1.25, opener=None, sleep=time.sleep, monotonic=time.monotonic):
        self.key, self.budget, self.min_interval = key, budget, min_interval
        self.opener = opener or build_opener(NoRedirect())
        self.sleep, self.monotonic, self.last_request = sleep, monotonic, None

    def _open(self, path, params):
        # The key travels only in this URL; never log requests, URLs or exceptions that carry them.
        request = Request(f"{API_BASE}{path}?{urlencode({**params, 'api_key': self.key})}",
                          headers={"Accept": "application/json"})
        with self.opener.open(request, timeout=90) as response:
            return json.load(response)

    def _safe(self, text):
        return str(text).replace(self.key, "[redacted]")[:200]

    def _http_message(self, error):
        try:
            detail = json.loads(error.read().decode("utf-8", "replace")).get("error")
        except (AttributeError, OSError, ValueError, TypeError):
            detail = None
        return f"SerpApi returned HTTP {error.code}" + (f": {self._safe(detail)}" if isinstance(detail, str) else "")

    def account(self):
        """Free: the Account API does not count toward the monthly quota."""
        try:
            payload = self._open("/account.json", {})
        except HTTPError as error:
            raise PriceError(self._http_message(error) + "; check SERPAPI_API_KEY") from None
        except (URLError, TimeoutError, OSError, HTTPException):
            raise PriceError("SerpApi account check failed") from None
        except (ValueError, UnicodeDecodeError):
            raise SchemaError("SerpApi account check returned invalid JSON") from None
        if not isinstance(payload, dict) or payload.get("error"):
            raise PriceError("SerpApi account check failed; check SERPAPI_API_KEY")

        def number(name):
            value = payload.get(name)
            return value if type(value) is int else None

        left = number("total_searches_left")
        return {"searches_left": left if left is not None else number("plan_searches_left"),
                "hourly_limit": number("account_rate_limit_per_hour")}

    def search(self, params):
        """Return the parsed response, or None when Google Hotels has no results for it."""
        for attempt in range(3):
            if self.last_request is not None:
                self.sleep(max(0, self.min_interval - (self.monotonic() - self.last_request)))
            self.budget.reserve()
            self.last_request = self.monotonic()
            try:
                payload = self._open("/search.json", params)
            except HTTPError as error:
                if error.code == 429:
                    raise BudgetError("SerpApi reported a plan or hourly search limit; retained existing data") from None
                if error.code not in (500, 502, 503, 504) or attempt == 2:
                    raise PriceError(self._http_message(error)) from None
                delay = max(2 ** (attempt + 1), retry_after((error.headers or {}).get("Retry-After")))
                if delay > 60:
                    raise PriceError("SerpApi asked to retry after more than 60 seconds; retry this job later") from None
                self.sleep(delay)
                continue
            except (URLError, TimeoutError, OSError, HTTPException):
                if attempt == 2:
                    raise PriceError("SerpApi request failed after three attempts") from None
                self.sleep(2 ** (attempt + 1))
                continue
            except (ValueError, UnicodeDecodeError):
                raise SchemaError("SerpApi returned invalid JSON") from None
            if not isinstance(payload, dict):
                raise SchemaError("Unexpected SerpApi response")
            error = payload.get("error")
            if error:
                if isinstance(error, str) and NO_RESULTS in error:
                    return None
                raise PriceError("SerpApi search failed: " + self._safe(error))
            status = (payload.get("search_metadata") or {}).get("status")
            if status not in (None, "Success"):
                raise PriceError("SerpApi search status: " + self._safe(status))
            return payload
        raise PriceError("SerpApi request failed after three attempts")


def save_raw(directory, kind, key, payload):
    """Keep a sanitized response for pilot review; never published."""
    if directory is None:
        return
    clean = {k: v for k, v in (payload or {}).items() if k not in ("search_metadata", "search_parameters")}
    name = hashlib.sha256(key.encode()).hexdigest()[:16]
    atomic_json(Path(directory) / f"{kind}-{name}.json", {"key": key, "response": clean})


def percentile(values, fraction):
    position = (len(values) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return round(values[low] + (values[high] - values[low]) * (position - low), 2)


def sufficient(days):
    weekdays = {day.weekday() for day in days}
    weeks = {day.isocalendar()[:2] for day in days}
    # These are check-in dates of one-night stays, so Friday and Saturday are the weekend nights.
    return (len(days) >= MIN_NIGHTS and bool(weekdays & {4, 5}) and bool(weekdays - {4, 5})
            and len(weeks) >= 3)


def aggregate_calendar(prices, start, end, now, expire_days=EXPIRE_DAYS):
    """Summarize Google's lowest one-night price for each check-in date in the window."""
    if not isinstance(prices, list):
        raise SchemaError("Expected a prices array")
    series = {basis: {} for basis, _ in PRICE_FIELDS}
    seen, duplicates = set(), set()
    for entry in prices:
        if not isinstance(entry, dict) or "date" not in entry:
            raise SchemaError("Price calendar schema changed")
        try:
            day = date.fromisoformat(entry["date"])
        except (TypeError, ValueError):
            continue
        if day in seen:
            duplicates.add(day)
        seen.add(day)
        # USD was requested; reject anything explicitly in another currency.
        if entry.get("currency", "USD") != "USD" or not start <= day <= end:
            continue
        for basis, field in PRICE_FIELDS:
            value = entry.get(field)
            if type(value) in (int, float) and math.isfinite(value) and 0 < value <= 100000:
                series[basis][day] = float(value)
    for days in series.values():
        for day in duplicates:
            days.pop(day, None)
    common = {"currency": "USD", "observed_at": timestamp(now), "window_start": start.isoformat(),
              "window_end": end.isoformat()}
    for basis, _ in PRICE_FIELDS:
        days = series[basis]
        if sufficient(days):
            values = sorted(days.values())
            return {**common, "status": "available", "tax_basis": basis, "sample_count": len(days),
                    "estimate": round(statistics.median(values), 2),
                    "low": percentile(values, 0.25), "high": percentile(values, 0.75),
                    "expires_at": timestamp(now + timedelta(days=expire_days))}
    return {**common, "status": "unavailable", "reason": "insufficient_dates", "tax_basis": "unknown",
            "sample_count": max(len(days) for days in series.values())}


def mapping_due(prior, rows, query, now):
    """Whether a hotel needs a Google Hotels search. Matches are kept until the hotel's map data changes."""
    try:
        age = (now - parse_timestamp(prior["checked_at"])).total_seconds() / 86400
    except (KeyError, ValueError, TypeError, AttributeError):
        return True
    if prior.get("manual") or prior.get("fingerprint") != fingerprint(rows):
        return True
    if prior.get("status") == "matched":
        return False
    # Unmatched hotels are retried twice a year, or sooner when their query changes.
    return prior.get("query") != query or not 0 <= age < RECHECK_UNMATCHED_DAYS


def resolve_mappings(client, groups, mappings, overrides, now, save, raw_dir=None):
    check_in = now.date() + timedelta(days=WINDOW_START_DAYS)
    for hotel_id, rows in groups.items():
        identity = fingerprint(rows)
        prior = mappings.get(hotel_id, {})
        if hotel_id in overrides:
            provider_id = overrides[hotel_id]
            if provider_id is not None and (not isinstance(provider_id, str) or not provider_id.strip()):
                raise PriceError("Overrides must contain nonempty property tokens or null to exclude a hotel")
            mappings[hotel_id] = {"status": "matched" if provider_id else "excluded", "provider_id": provider_id,
                                  "fingerprint": identity, "checked_at": timestamp(now), "manual": True}
        else:
            query = search_query(rows)
            if not mapping_due(prior, rows, query, now):
                continue
            if conflicting_identity(rows):
                result = {"status": "identity_conflict"}
            else:
                params = {"engine": "google_hotels", "check_in_date": check_in.isoformat(),
                          "check_out_date": (check_in + timedelta(days=1)).isoformat(), **COMMON_PARAMS}
                payload = client.search({**params, "q": query})
                name_only = rows[0]["n"].strip()
                if payload is None and name_only != query:
                    payload = client.search({**params, "q": name_only})  # One retry when Google finds nothing.
                save_raw(raw_dir, "search", hotel_id, payload)
                result = match_hotel(rows, candidates_from(payload))
            mappings[hotel_id] = {**result, "fingerprint": identity, "query": query, "checked_at": timestamp(now),
                                  "matcher": MATCHER_VERSION}
        save()


def redecide_mappings(groups, mappings):
    """Apply changed match rules to stored candidates without spending searches."""
    redecided = 0
    for hotel_id, rows in groups.items():
        prior = mappings.get(hotel_id, {})
        evidence = prior.get("candidates")
        if (prior.get("matcher", 1) == MATCHER_VERSION or prior.get("manual") or not evidence
                or prior.get("fingerprint") != fingerprint(rows) or not all(isinstance(item, dict) for item in evidence)):
            continue
        kept = {key: prior[key] for key in ("fingerprint", "query", "checked_at") if key in prior}
        mappings[hotel_id] = {**decide(rows, evidence), **kept, "matcher": MATCHER_VERSION}
        redecided += 1
    return redecided


def current_mapping(hotel_id, rows, mappings, now):
    mapping = mappings.get(hotel_id, {})
    if (mapping.get("status") == "matched" and mapping.get("fingerprint") == fingerprint(rows)
            and isinstance(mapping.get("provider_id"), str) and mapping["provider_id"]):
        return mapping["provider_id"]
    return None


def reject_provider_collisions(groups, mappings, now):
    tokens = defaultdict(list)
    for key, rows in groups.items():
        token = current_mapping(key, rows, mappings, now)
        if token:
            tokens[token].append(key)
    for ids in tokens.values():
        if len(ids) > 1 and not all(mappings[key].get("manual") for key in ids):
            for key in ids:
                mappings[key]["status"] = "provider_conflict"


def refresh_prices(client, groups, mappings, estimates, now, save, raw_dir=None, min_refresh_days=6,
                   expire_days=EXPIRE_DAYS):
    start, end = window(now)
    by_token = defaultdict(list)
    for hotel_id, rows in groups.items():
        token = current_mapping(hotel_id, rows, mappings, now)
        if token:
            by_token[token].append(hotel_id)
    # Distinct Google identities resolving to one property require review.
    for token in list(by_token):
        ids = by_token[token]
        if len(ids) > 1 and conflicting_identity([groups[hotel_id][0] for hotel_id in ids]):
            del by_token[token]
            for hotel_id in ids:
                estimates.pop(hotel_id, None)

    def last_attempt(token):
        return min(estimates.get(hotel_id, {}).get("last_attempt_at") or "" for hotel_id in by_token[token])

    # Oldest first, so a run that stops at its budget rotates through every hotel over time.
    for token in sorted(by_token, key=lambda item: (last_attempt(item), item)):
        try:
            if 0 <= (now - parse_timestamp(last_attempt(token))).total_seconds() < min_refresh_days * 86400:
                continue
        except ValueError:
            pass
        payload = client.search({"engine": "google_hotels_price_calendar", "property_token": token,
                                 "start_date": start.isoformat(), "end_date": end.isoformat(), **COMMON_PARAMS})
        save_raw(raw_dir, "calendar", token, payload)
        prices = None if payload is None else payload.get("prices")
        if prices is None:
            result = {"status": "unavailable", "reason": "no_data", "sample_count": 0}
        else:
            result = aggregate_calendar(prices, start, end, now, expire_days)
        for hotel_id in by_token[token]:
            previous = estimates.get(hotel_id, {})
            identity = fingerprint(groups[hotel_id])
            same = previous.get("provider_id") == token and previous.get("fingerprint") == identity
            # Keep the last good estimate (and its observation time) when a refresh comes back empty.
            if result["status"] == "available" or previous.get("status") != "available" or not same:
                estimates[hotel_id] = {**result, "provider_id": token, "fingerprint": identity}
            estimates[hotel_id].update({"last_attempt_at": timestamp(now), "last_attempt_status": result["status"]})
        save()


def days_until_reset(now, reset_day):
    """Days left before SerpApi renews the monthly quota on reset_day (1-28), counting today."""
    today = now.date()
    if today.day < reset_day:
        return (today.replace(day=reset_day) - today).days
    following = (today.replace(day=1) + timedelta(days=32)).replace(day=reset_day)
    return (following - today).days


def rotate(client, groups, mappings, estimates, overrides, now, save_mappings, save_estimates, raw_dir=None,
           due_days=DUE_DAYS, min_refresh_days=6, expire_days=EXPIRE_DAYS):
    """One day's share: prices nearing the six-month limit, then new hotels, then the oldest other prices."""
    # Overdue and never-checked prices come first; they sort oldest first.
    refresh_prices(client, groups, mappings, estimates, now, save_estimates, raw_dir, due_days, expire_days)
    # A new hotel costs about two searches (match, then first price). Spread them across programs and regions.
    waiting = [key for key, rows in select_pilot(groups, len(groups)).items()
               if key not in overrides and mapping_due(mappings.get(key, {}), rows, search_query(rows), now)]
    batch = waiting[:client.budget.remaining // 2]
    resolve_mappings(client, {key: groups[key] for key in batch}, mappings, overrides, now, save_mappings, raw_dir)
    reject_provider_collisions(groups, mappings, now)
    save_mappings()
    # Price the new matches, then spend what is left on the oldest prices.
    refresh_prices(client, groups, mappings, estimates, now, save_estimates, raw_dir, min_refresh_days, expire_days)


def coverage(groups, mappings, overrides, payload, now):
    ages = [(now - parse_timestamp(entry["observed_at"])).total_seconds() / 86400 for entry in payload["hotels"].values()]
    return {"hotels": len(groups),
            "matched": sum(bool(current_mapping(key, rows, mappings, now)) for key, rows in groups.items()),
            "awaiting_search": sum(key not in overrides and mapping_due(mappings.get(key, {}), rows, search_query(rows), now)
                                   for key, rows in groups.items()),
            "priced": len(ages),
            "oldest_price_days": round(max(ages), 1) if ages else None,
            "prices_older_than_refresh_days": sum(age > REFRESH_DAYS for age in ages)}


def public_prices(groups, mappings, estimates, now):
    public = {}
    for hotel_id, entry in estimates.items():
        rows = groups.get(hotel_id)
        if not rows or entry.get("status") != "available":
            continue
        if current_mapping(hotel_id, rows, mappings, now) != entry.get("provider_id") or fingerprint(rows) != entry.get("fingerprint"):
            continue
        try:
            if not 0 <= (now - parse_timestamp(entry["observed_at"])).total_seconds() or parse_timestamp(entry["expires_at"]) <= now:
                continue
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        public[hotel_id] = {key: value for key, value in entry.items()
                            if key not in {"provider_id", "fingerprint", "last_attempt_at", "last_attempt_status"}}
    return {"version": SCHEMA_VERSION, "source": SOURCE, "generated_at": timestamp(now), "hotels": public}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["pilot", "map", "refresh", "rotate", "export", "reserve"],
                        help="rotate: the daily job; it paces the month's searches and works through every hotel")
    parser.add_argument("--hotels", default="docs/hotels.json")
    parser.add_argument("--state-dir", default=".price-cache/serpapi")
    parser.add_argument("--overrides", default="config/price-overrides.json")
    parser.add_argument("--output", default="docs/prices.json")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--max-hotels", type=int, default=10, help="0 selects all hotels; pilot selects at most 50")
    parser.add_argument("--max-requests", type=int, default=25, help="searches this run may use, retries included")
    parser.add_argument("--monthly-limit", type=int, default=250, help="searches per UTC month; SerpApi's free plan allows 250")
    parser.add_argument("--min-interval", type=float, default=1.25, help="seconds between searches; persisted hourly counters limit throughput")
    parser.add_argument("--min-refresh-days", type=float, default=6, help="skip hotels whose prices were fetched more recently")
    parser.add_argument("--expire-days", type=int, default=EXPIRE_DAYS, help="hide an estimate this many days after it was observed")
    parser.add_argument("--due-days", type=float, default=DUE_DAYS, help="rotate: prices this old come before new hotels")
    parser.add_argument("--quota-reset-day", type=int, default=1, help="rotate: day of the month SerpApi renews the plan's searches")
    parser.add_argument("--initialize-budget", action="store_true")
    parser.add_argument("--reservation-id", help="Use an allowance persisted before this CI run")
    parser.add_argument("--save-raw", action="store_true", help="keep sanitized responses in STATE_DIR/raw (always on for pilot)")
    args = parser.parse_args(argv)
    if args.max_hotels < 0 or min(args.max_requests, args.monthly_limit) < 1 or args.min_interval < 0 or args.min_refresh_days < 0:
        parser.error("Limits must be positive; --max-hotels may also be 0")
    if not 1 <= args.expire_days <= EXPIRE_DAYS:
        parser.error(f"--expire-days must be between 1 and {EXPIRE_DAYS}; the map hides older estimates anyway")
    if not 1 <= args.quota_reset_day <= 28 or not 0 < args.due_days < REFRESH_DAYS:
        parser.error(f"--quota-reset-day must be 1-28 and --due-days below {REFRESH_DAYS}")
    directory, now = Path(args.state_dir), utcnow()
    with state_lock(directory):
        if args.command == "reserve":
            budget = Budget(directory / "usage.json", args.initialize_budget, args.monthly_limit, args.max_requests, now)
            reserved = budget.reserve_run(args.reservation_id, args.max_requests)
            print(f"Reserved {reserved} searches for this run; persist this ledger before making API calls")
            return 0
        groups = hotel_groups(read_json(args.hotels, []))
        if not groups:
            raise PriceError("No stable hotel IDs; run generate_map_data.py first")
        mappings = read_json(directory / "mappings.json", {})
        estimates = read_json(directory / "estimates.json", {})
        override_file = read_json(args.overrides, {"version": 1, "hotels": {}})
        if override_file.get("version") != 1 or not isinstance(override_file.get("hotels"), dict):
            raise PriceError("Invalid mapping override file")
        overrides = override_file["hotels"]
        for hotel_id, mapping in mappings.items():
            if mapping.get("manual") and hotel_id not in overrides:
                mapping["status"] = "pending"
        save_mappings = lambda: atomic_json(directory / "mappings.json", mappings)
        save_estimates = lambda: atomic_json(directory / "estimates.json", estimates)
        if redecide_mappings(groups, mappings):
            reject_provider_collisions(groups, mappings, now)
            save_mappings()
        if args.command == "pilot":
            selected = select_pilot(groups, min(args.max_hotels or 50, 50))
        elif args.command == "rotate":
            selected = groups
        else:
            selected = groups if args.max_hotels == 0 else select_pilot(groups, args.max_hotels)
        raw_dir = directory / "raw" if args.save_raw or args.command == "pilot" else None
        error, stopped, account, used = None, None, None, 0
        if args.command != "export":
            key = api_key(args.env_file)
            budget = Budget(directory / "usage.json", args.initialize_budget, args.monthly_limit, args.max_requests, now, args.reservation_id)
            client = SerpApi(key, budget, args.min_interval)
            try:
                account = client.account()
                if account["searches_left"] is not None:
                    if account["searches_left"] < 1:
                        raise BudgetError("The SerpApi account has no searches left; retained existing data")
                    budget.max_requests = min(budget.max_requests, account["searches_left"])
                    if args.command == "rotate":
                        # Spread what is left evenly over the days until the quota renews.
                        daily = math.ceil(account["searches_left"] / days_until_reset(now, args.quota_reset_day))
                        budget.max_requests = min(budget.max_requests, daily)
                if account["hourly_limit"]:
                    budget.hourly_limit = account["hourly_limit"]
                # Apply reviewed overrides even when only refreshing existing mappings.
                resolve_mappings(client, {key: rows for key, rows in groups.items() if key in overrides},
                                 mappings, overrides, now, save_mappings)
                if args.command in ("pilot", "map"):
                    resolve_mappings(client, selected, mappings, overrides, now, save_mappings, raw_dir)
                reject_provider_collisions(groups, mappings, now)
                save_mappings()
                if args.command in ("pilot", "refresh"):
                    refresh_prices(client, selected, mappings, estimates, now, save_estimates, raw_dir,
                                   args.min_refresh_days, args.expire_days)
                if args.command == "rotate":
                    rotate(client, groups, mappings, estimates, overrides, now, save_mappings, save_estimates,
                           raw_dir, args.due_days, args.min_refresh_days, args.expire_days)
            except RunBudgetReached as caught:
                stopped = str(caught)
            except PriceError as caught:
                error = str(caught)
            # Whatever stopped the run, never publish two map entries on one Google property.
            reject_provider_collisions(groups, mappings, now)
            save_mappings()
            budget.settle()
            used = budget.used
        payload = public_prices(groups, mappings, estimates, now)
        atomic_json(args.output, payload)
        matched = sum(bool(current_mapping(key, rows, mappings, now)) for key, rows in selected.items())
        priced = sum(key in payload["hotels"] for key in selected)
        report = {"version": 1, "generated_at": timestamp(now), "command": args.command,
                  "selected": len(selected), "matched": matched, "priced": priced,
                  "mapping_statuses": dict(Counter(mappings.get(key, {}).get("status", "pending") for key in selected)),
                  "tax_basis": dict(Counter(payload["hotels"][key]["tax_basis"] for key in selected if key in payload["hotels"])),
                  "coverage_pass": matched >= 0.9 * len(selected) and priced >= 0.8 * len(selected),
                  "coverage": coverage(groups, mappings, overrides, payload, now),
                  "searches_used": used, "searches_left_before": account and account["searches_left"],
                  "stopped": stopped, "error": error}
        atomic_json(directory / "report.json", report)
        print(json.dumps(report, indent=2))
        if error:
            return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PriceError, OSError, ValueError, TypeError) as error:
        # Do not dump request objects, URLs (they carry the key), or provider response bodies.
        print(f"Price refresh stopped: {error}")
        raise SystemExit(1)

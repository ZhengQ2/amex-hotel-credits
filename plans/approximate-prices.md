# Approximate hotel prices

Planning date: November 16, 2026. Revised the same day to switch providers: LiteAPI only issues production keys to registered travel businesses (legal entity, registered address, commercial model, then verification and payouts), and this personal map makes no bookings.

## Decision

Use **SerpApi's Google Hotels Price Calendar** on the $25/month plan (1,000 searches), with a daily job that re-checks every hotel at least every 6 months. The 50-hotel pilot on the free plan passed.

1. **Match once.** Search `engine=google_hotels` for each hotel's name and address. Accept a result only if Google's coordinates are close and the name matches (see `match_hotel`). Store its `property_token`.
2. **Price each hotel.** Call `engine=google_hotels_price_calendar` with that token for check-in dates 14–104 days out. It returns Google's lowest price for a one-night stay starting on each date. [Calendar API](https://serpapi.com/google-hotels-price-calendar-api), [Hotels API](https://serpapi.com/google-hotels-api)
3. **Summarize.** Take the median of the daily prices, with the 25th–75th percentile as the "typical range". Requests use 2 adults, USD, `gl=us` and `hl=en`.

Why SerpApi:

- **Anyone can sign up.** No business registration.
- **Clearer numbers.** Each price is for a specific stay: 2 adults, one night, a stated check-in date. Before- and after-tax prices come back when Google shows them.
- **Same Google identity as the map.** Hotels on the map come from Google Places, so names and coordinates match Google Hotels closely.

Trade-off: it costs at least one search per hotel per refresh. Covering the whole map needs a paid plan (see Cost).

## Options considered

| Option | Verdict |
| --- | --- |
| **SerpApi Google Hotels Price Calendar** | **Selected.** |
| LiteAPI Hotel Price Index | Not available. Production keys require a registered business. A sandbox trial in this repo (`.price-cache/`, 10 hotels) matched only 3. |
| Google Hotel Prices data feeds | Rejected. Hotels and OTAs use them to *send* rates to Google; there's no read access. |
| Google Places (already used) | Keep for identity and coordinates only. It has no room prices. |
| Amadeus Self-Service Hotel APIs | Rejected. Self-service keys were disabled on July 17, 2026. [PhocusWire](https://www.phocuswire.com/amadeus-shut-down-self-service-apis-portal-developers) |
| Booking.com Demand API / Expedia Rapid | Rejected. Both need affiliate approval for booking flows. |
| Makcorps | Not chosen. No public per-request pricing. |
| Xotelo | Not chosen. Unofficial, TripAdvisor-derived, unverified terms. |
| Apify / DataForSEO scrapers | Not chosen. Mostly one query per hotel per date, of varying reliability. |

## User-facing behavior

- **Popup line:** "Approx. US$320 / night", rounded to US$10. It also shows the date checked. An "About this estimate" section gives the typical range, the number of check-in dates, the tax basis and the source. The property link stays the main action.
- **Tax basis:** prefer Google's before-tax price. Fall back to the after-tax price, then to the displayed price, labeled "Tax and fee treatment is unconfirmed." One basis is used per hotel, never mixed.
- **Minimum data:** at least 21 valid check-in dates, including Friday or Saturday check-ins and other days, in at least 3 different weeks. Otherwise the popup shows "Price unavailable".
- **Freshness:** every hotel is re-checked at least every 6 months (about every 110 days once all are mapped). The popup always shows the date checked, shows "Update overdue" after 180 days, and hides the price after 200 days. A missing price is never shown as zero.
- **Filters:** "Max US$ / night" uses unrounded values. "Include hotels without prices" is on by default.
- **Disclaimer:** eligible Hilton, FHR and THC rates may differ from Google's lowest public price.

## Status in this repository (uncommitted)

**Built:**

- `generate_map_data.py` adds `id` (the Google Place ID) to map entries: 3,757 entries and 3,605 distinct IDs.
- `fetch_hotel_prices.py` talks to SerpApi using only the standard library. `rotate` is the daily job; `pilot`, `map`, `refresh` and `export` are for manual runs.
  - It checks the free Account API before spending searches. It limits the run to the searches left and slows down to fit the account's hourly limit; the free plan allows 50/hour, so the default spacing is 75 s.
  - A persisted ledger reserves every search before it's sent, retries included. A CI run reserves its allowance up front and releases what it didn't use.
  - A 429 response stops the run. Server errors retry at most 3 times.
  - The API key travels only in the request URL, and URLs are never logged. Error messages are scrubbed of the key, and redirects are never followed.
  - Hotels are refreshed oldest first, and any priced in the last 6 days are skipped, so a run that hits its budget picks up where it stopped.
  - A match is kept until the hotel's map data changes. Unmatched hotels are searched again after 180 days, or sooner if their search query changes.
  - The last good estimate is kept when a refresh comes back empty.
  - The pilot saves sanitized responses to `.price-cache/serpapi/raw/` for review.
- `docs/prices.js` and the `docs/index.html` changes: popup, price filters, optional `prices.json` loading.
- Tests: `tests/test_fetch_hotel_prices.py` (Python, no network) and `tests/test_prices.js`.

- `.github/workflows/hotel-prices.yml` runs `rotate` daily. It keeps state in an Actions artifact, reserves the run's searches before calling SerpApi, then deploys Pages, which exports `prices.json` from that state (`.github/workflows/pages.yml`, `scripts/restore_price_state.py`). Another tool started these for LiteAPI; they now target SerpApi.

## Pilot results (November 16, 2026)

50 hotels, chosen across programs and regions. 95 searches in total, and SerpApi's count matched the ledger exactly. State is in `.price-cache/serpapi/` (not published).

| Check | Target | Result |
| --- | --- | --- |
| Matched confidently | at least 90% | **96%** (48 of 50) |
| Enough check-in dates | at least 80% | **84%** (42 of 50) |
| Wrong-hotel matches | none | **None.** All 48 checked by hand; every one is within 65 m and is the same property. |
| Tax basis recorded | all | **All 42 before tax.** |

- **Matching tuned once.** The first pass held back 9 correct hotels because Google shortens or extends names ("Hotel Locarno" for Hotel Locarno Roma, "Humaniti Hotel Montreal Autograph Collection"). Now a name whose distinctive words all appear in the other counts as a match within 150 m, unless Google's extra words mark a separate listing ("Residences", "Villas", "Suites"). Stored candidates are re-decided when the rules change (`MATCHER_VERSION`), so this cost no searches.
- **Unmatched (2):** Rocco Forte Private Villas (Google has no listing) and Hilton Marsa Alam Nubian Resort (only other hotels came back). Both show "Price unavailable", which is correct.
- **Matched but unpriced (6):** Mykonos Blanc, Jumeirah Capri Palace, Hilton Dalaman and Hotel RioMar close for the winter; Cheval Blanc Courchevel and Kisawa Sanctuary show no prices on Google. Their calendars were empty, and they will be priced when Google has rates.
- **Prices look right.** Each calendar call returned all 91 dates and counted as one search. Conrad New York Downtown on Jan 2–3: US$434 before tax and US$504 after (NYC tax is about 16%). Google's own page showed CA$621 with fees, about US$445.
- **Known bias:** sold-out dates come back without a price and are skipped, so estimates lean slightly low around holidays.

## Daily schedule

The budget is at most $25/month, and the hard requirement is a re-check of every hotel's price within 6 months. On the $25 plan the job uses the full 1,000 searches each month, since unused searches don't roll over.

Each day's `rotate` run:

1. Spreads the searches left evenly over the days until SerpApi renews the quota (about 33 a day).
2. Refreshes prices older than 150 days first, and prices new matches.
3. Matches new hotels (about 2 searches each: one to match, one for the first price), spread across programs and regions.
4. Spends any searches left on the oldest remaining prices.

Simulated over two years on the real hotel list, with the pilot's match and price rates and the free plan's 146 searches left this month:

| | Result |
| --- | --- |
| Every hotel searched and priced once | about September 2027 (10 months) |
| Oldest price ever shown | 150 days (the limit is 180) |
| Refresh interval after the first pass | about 110 days |
| Hotels with a price | about 2,990 of 3,605 (83%); 95% match |

Until a hotel's turn comes, its popup shows "Price unavailable". The 30 days between 150 and 180 cover missed runs. With prices due at 170 days instead, the first pass ends three weeks sooner but leaves only 10 days of buffer.

The free plan can't meet the 6-month requirement: steady state alone needs about 610 searches a month.

## Remaining work

1. **Subscribe** to SerpApi's $25 plan, and note the day of the month it renews.
2. **Configure GitHub:**
   - Secret `SERPAPI_API_KEY`.
   - Variables:
     - `PRICE_QUOTA_RESET_DAY`: the renewal day.
     - `PRICE_MONTHLY_LIMIT`: 1000, the default.
     - `PRICE_REFRESH_ENABLED`: `true` turns on the schedule and publishing.
3. **First run:** start "Refresh approximate hotel prices" manually with `rotate` and "initialize budget" checked. CI starts without the local pilot state, so it re-matches the pilot hotels: about 100 searches, once. If an artifact is lost (they expire after 90 days without runs), the job stops rather than resetting the budget.
4. **Ship.** Check popups and filters on desktop and mobile, and with `prices.json` missing.

## Cost

From SerpApi's current list prices ([pricing](https://serpapi.com/pricing)): Free gives 250 searches/month at 50/hour. Paid plans are $25 for 1,000, $75 for 5,000, $150 for 15,000 and $275 for 30,000. Only the $150 plan and up include the U.S. Legal Shield. Cached and errored searches aren't counted.

Each hotel needs one search to match it (repeated only when the hotel changes, or every 180 days if unmatched) and one calendar search per refresh. Map hotels: 3,605 in total: 1,885 FHR, 1,402 THC and 318 Hilton resort-credit hotels, counted by first program.

The pilot confirmed the assumptions: one calendar call covers the 91-day window and counts as one search, and matching plus the first price took about 2 searches per hotel. Re-checking all hotels every 6 months needs about 610 searches a month in steady state, so the $25 plan (1,000) is the cheapest that fits. The first pass needs about 7,300 searches.

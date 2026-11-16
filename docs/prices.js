(function (root) {
  "use strict";
  const DAY = 86400000;
  // Every hotel is re-checked at least every 6 months: flag after 180 days, never show anything older than 200.
  const STALE_DAYS = 180, MAX_AGE_DAYS = 200;

  function validEntry(entry) {
    if (!entry || entry.status !== "available" || entry.currency !== "USD") return false;
    if (![entry.estimate, entry.low, entry.high].every(value => typeof value === "number" && Number.isFinite(value) && value > 0)) return false;
    if (!(entry.low <= entry.estimate && entry.estimate <= entry.high) || !Number.isInteger(entry.sample_count) || entry.sample_count < 21) return false;
    const times = [entry.observed_at, entry.expires_at, entry.window_start, entry.window_end].map(Date.parse);
    return times.every(Number.isFinite) && times[1] > times[0] && times[3] >= times[2];
  }

  function parseDataset(data) {
    if (!data || data.version !== 1 || !data.hotels || typeof data.hotels !== "object" || Array.isArray(data.hotels)) {
      throw new Error("Unsupported price dataset");
    }
    return Object.fromEntries(Object.entries(data.hotels).filter(([, entry]) => validEntry(entry)));
  }

  function currentPrice(hotel, prices, now = Date.now()) {
    const entry = hotel.id && prices[hotel.id];
    if (!validEntry(entry)) return null;
    const age = now - Date.parse(entry.observed_at);
    if (age < 0 || age >= MAX_AGE_DAYS * DAY || now >= Date.parse(entry.expires_at)) return null;
    return { ...entry, stale: age > STALE_DAYS * DAY };
  }

  function matchesFilters(hotel, query, maximum, includeUnknown, prices, now = Date.now()) {
    const textMatch = !query || [hotel.n, hotel.a, hotel.b, hotel.g].some(value => (value || "").toLowerCase().includes(query));
    if (!textMatch) return false;
    const price = currentPrice(hotel, prices, now);
    if (!price) return includeUnknown;
    return maximum === null || price.estimate <= maximum;
  }

  const money = amount => `US$${Math.max(10, Math.round(amount / 10) * 10).toLocaleString("en-US")}`;
  const shortDate = value => new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" }).format(new Date(value));

  function popupHtml(hotel, prices, now = Date.now()) {
    const price = currentPrice(hotel, prices, now);
    if (!price) return '<div class="hp-price hp-price-unknown">Price unavailable</div>';
    const tax = price.tax_basis === "included" ? "Includes taxes and fees." : price.tax_basis === "excluded" ? "Before taxes and fees." : "Tax and fee treatment is unconfirmed.";
    return `<div class="hp-price">Approx. ${money(price.estimate)} / night</div>
      <div class="hp-price-meta">Checked ${shortDate(price.observed_at)}${price.stale ? " · Update overdue" : ""}</div>
      <details class="hp-price-details"><summary>About this estimate</summary>
        <p>Typical range: ${money(price.low)}–${money(price.high)} per night.</p>
        <p>Median of the lowest listed price for a one-night stay for 2 adults, across ${price.sample_count} check-in dates from ${shortDate(price.window_start)} to ${shortDate(price.window_end)}. Room types vary. ${tax}</p>
        <p>Source: Google Hotels, via SerpApi. Eligible Hilton, FHR and THC booking prices may differ.</p>
      </details>`;
  }

  const api = { parseDataset, currentPrice, matchesFilters, popupHtml };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.HotelPrices = api;
})(typeof window !== "undefined" ? window : globalThis);

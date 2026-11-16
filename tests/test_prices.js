const { test } = require("node:test");
const assert = require("node:assert/strict");
const prices = require("../docs/prices.js");

const now = Date.parse("2026-11-16T12:00:00Z");
const hotel = { id: "hotel-a", n: "Hilton Test", a: "Paris", b: "Hilton", g: "Resort" };
const entry = {
  status: "available", currency: "USD", estimate: 305.5, low: 270, high: 360,
  observed_at: "2026-11-15T12:00:00Z", expires_at: "2026-12-15T12:00:00Z",
  window_start: "2026-11-30", window_end: "2027-02-28", sample_count: 65, tax_basis: "unknown",
};

test("combined filters use unrounded prices and preserve unknown hotels by default", () => {
  const dataset = { "hotel-a": entry };
  assert.equal(prices.matchesFilters(hotel, "paris", 305, true, dataset, now), false);
  assert.equal(prices.matchesFilters(hotel, "paris", 306, false, dataset, now), true);
  assert.equal(prices.matchesFilters(hotel, "london", 306, true, dataset, now), false);
  assert.equal(prices.matchesFilters({ ...hotel, id: "missing" }, "", 100, true, dataset, now), true);
  assert.equal(prices.matchesFilters({ ...hotel, id: "missing" }, "", null, false, dataset, now), false);
});

test("prices go stale after 180 days and disappear at the provider expiry or 200 days", () => {
  const DAY = 86400000;
  const long = { ...entry, expires_at: "2027-12-01T00:00:00Z" };
  assert.equal(prices.currentPrice(hotel, { "hotel-a": entry }, now).stale, false);
  assert.equal(prices.currentPrice(hotel, { "hotel-a": long }, now + 179 * DAY).stale, false);
  assert.equal(prices.currentPrice(hotel, { "hotel-a": long }, now + 180 * DAY).stale, true);
  assert.equal(prices.currentPrice(hotel, { "hotel-a": long }, now + 199 * DAY), null);
  assert.equal(prices.currentPrice(hotel, { "hotel-a": entry }, now + 29 * DAY), null);
  assert.equal(prices.currentPrice(hotel, { "hotel-a": { ...entry, expires_at: "2026-11-16T11:00:00Z" } }, now), null);
  assert.equal(prices.currentPrice(hotel, { "hotel-a": { ...entry, observed_at: "2026-11-17T12:00:00Z" } }, now), null);
});

test("malformed, non-USD and insufficient samples cannot produce displayed prices", () => {
  for (const changes of [{ estimate: "300" }, { estimate: Infinity }, { currency: "CAD" }, { sample_count: 20 }, { low: 400 }, { observed_at: "bad" }, { status: "unavailable" }]) {
    const dataset = prices.parseDataset({ version: 1, hotels: { "hotel-a": { ...entry, ...changes } } });
    assert.deepEqual(dataset, {});
  }
  assert.throws(() => prices.parseDataset({ version: 2, hotels: {} }));
  assert.throws(() => prices.parseDataset({ version: 1, hotels: [] }));
});

test("popups show explicit USD and never interpolate untrusted provider text", () => {
  const html = prices.popupHtml(hotel, { "hotel-a": { ...entry, tax_basis: '<img src=x onerror="alert(1)">' } }, now);
  assert.match(html, /Approx\. US\$310 \/ night/);
  assert.match(html, /unconfirmed/);
  assert.match(html, /Google Hotels, via SerpApi/);
  assert.match(html, /65 check-in dates/);
  assert.doesNotMatch(html, /<img/);
  assert.match(prices.popupHtml(hotel, {}, now), /Price unavailable/);
});

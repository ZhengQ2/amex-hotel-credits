// Run with: node --test tests/test_changes.js
const test = require("node:test");
const assert = require("node:assert/strict");
const HotelChanges = require("../docs/changes.js");

const FHR = "Fine Hotels + Resorts", THC = "The Hotel Collection", HILTON = "Hilton Portfolio";
const event = (date, program, change, name, extra = {}) => ({ date, program, change, name, ...extra });

test("parseLog rejects other versions and drops malformed events", () => {
  assert.throws(() => HotelChanges.parseLog({ version: 2, events: [] }));
  const { events, trackingSince } = HotelChanges.parseLog({
    version: 1,
    tracking_since: { [FHR]: "2026-03-30" },
    events: [event("2026-11-01", FHR, "added", "A"), event("bad", FHR, "added", "B"),
      event("2026-11-02", "Other", "added", "C"), event("2026-11-03", FHR, "moved", "D")],
  });
  assert.deepEqual(events.map(e => e.name), ["A"]);
  assert.deepEqual(trackingSince, { [FHR]: "2026-03-30" });
});

test("a quick return is a blip; a long absence is not", () => {
  const { events } = HotelChanges.parseLog({
    version: 1,
    events: [
      event("2026-04-02", FHR, "removed", "Gstaad Palace"),
      event("2026-04-04", FHR, "added", "Gstaad Palace", { returns_from: { date: "2026-04-02", name: "Gstaad Palace" } }),
      event("2026-04-14", FHR, "removed", "W Florence"),
      event("2026-07-02", FHR, "added", "W Florence", { returns_from: { date: "2026-04-14", name: "W Florence" } }),
      event("2026-05-06", THC, "added", "Liora Estate"),
      event("2026-07-15", THC, "removed", "Liora Estate", { added_on: { date: "2026-05-06", name: "Liora Estate" } }),
    ],
  });
  const find = (name, change) => events.find(e => e.name === name && e.change === change);
  assert.equal(find("Gstaad Palace", "removed").returnedOn, "2026-04-04");
  assert.equal(find("Gstaad Palace", "added").previouslyRemovedOn, "2026-04-02");
  assert.equal(find("Gstaad Palace", "added").gapDays, 2);
  assert.ok(find("Gstaad Palace", "added").blip && find("Gstaad Palace", "removed").blip);
  assert.ok(!find("W Florence", "added").blip);
  assert.equal(find("W Florence", "added").gapDays, 79);
  assert.equal(find("Liora Estate", "added").removedOn, "2026-07-15");
});

test("a return under a new listing name is a blip and names the other listing", () => {
  const { events } = HotelChanges.parseLog({
    version: 1,
    events: [
      event("2026-04-09", FHR, "removed", "Bulgari Hotel Shanghai"),
      event("2026-04-10", FHR, "added", "Bvlgari Hotel Shanghai",
        { returns_from: { date: "2026-04-09", name: "Bulgari Hotel Shanghai" } }),
    ],
  });
  const [added, removed] = events;
  assert.ok(added.blip && removed.blip);
  assert.equal(added.removedAs, "Bulgari Hotel Shanghai");
  assert.equal(removed.returnedAs, "Bvlgari Hotel Shanghai");
  assert.equal(HotelChanges.filterEvents(events, { hideBlips: true }).length, 0);
});

test("only the log's links tie events together, not matching names", () => {
  // hotel_history.py decides identity; equal names it did not link stay apart.
  const { events } = HotelChanges.parseLog({
    version: 1,
    events: [event("2026-04-02", THC, "removed", "The Sanderling"), event("2026-04-03", THC, "added", "The Sanderling"),
      event("2026-04-05", FHR, "added", "Ghost", { returns_from: { date: "2026-04-01", name: "Missing" } })],
  });
  assert.ok(events.every(e => !e.blip && !e.returnedOn && !e.previouslyRemovedOn));
});

test("filterEvents applies programs, changes, period, search and blips", () => {
  const { events } = HotelChanges.parseLog({
    version: 1,
    events: [
      event("2026-11-20", FHR, "added", "Aman Venice", { location: "Venice, Italy" }),
      event("2026-11-20", HILTON, "added", "Conrad Algarve", { brand: "Conrad Hotels & Resorts" }),
      event("2026-11-21", THC, "renamed", "Treville Positano", { previous_name: "Villa Treville" }),
      event("2026-06-01", FHR, "removed", "Old Hotel"),
      event("2026-11-22", FHR, "removed", "Gstaad Palace"),
      event("2026-11-23", FHR, "added", "Gstaad Palace", { returns_from: { date: "2026-11-22", name: "Gstaad Palace" } }),
    ],
  });
  const names = filters => HotelChanges.filterEvents(events, filters).map(e => e.name);
  assert.deepEqual(names({ programs: new Set([HILTON]) }), ["Conrad Algarve"]);
  assert.deepEqual(names({ changes: new Set(["renamed"]) }), ["Treville Positano"]);
  assert.deepEqual(names({ query: "villa" }), ["Treville Positano"]);  // matches the previous name
  assert.deepEqual(names({ query: "VENICE" }), ["Aman Venice"]);
  assert.deepEqual(names({ query: "conrad hotels" }), ["Conrad Algarve"]);
  assert.ok(!names({ sinceDay: HotelChanges.dayNumber("2026-11-01") }).includes("Old Hotel"));
  assert.ok(!names({ hideBlips: true }).includes("Gstaad Palace"));
  assert.equal(names({ hideBlips: false }).filter(n => n === "Gstaad Palace").length, 2);
});

test("groupByDay orders days newest first and programs in map order", () => {
  const { events } = HotelChanges.parseLog({
    version: 1,
    events: [
      event("2026-11-20", THC, "removed", "B", { source: "https://example.com/1" }),
      event("2026-11-20", THC, "added", "C", { source: "https://example.com/1" }),
      event("2026-11-20", HILTON, "added", "A", { source: "https://example.com/2" }),
      event("2026-11-21", FHR, "added", "D"),
    ],
  });
  const days = HotelChanges.groupByDay(events);
  assert.deepEqual(days.map(d => d.date), ["2026-11-21", "2026-11-20"]);
  assert.deepEqual(days[1].groups.map(g => g.program), [HILTON, THC]);
  assert.deepEqual(days[1].groups[1].events.map(e => e.name), ["C", "B"]);  // added before removed
  assert.deepEqual(days[1].groups[1].counts, { added: 1, removed: 1, renamed: 0 });
  assert.deepEqual(days[1].groups[1].sources, ["https://example.com/1"]);
  assert.deepEqual(days[0].groups[0].sources, []);
});

test("the committed log parses and every link finds its event", () => {
  const data = JSON.parse(require("node:fs").readFileSync(require("node:path").join(__dirname, "../docs/changes.json"), "utf8"));
  const { events } = HotelChanges.parseLog(data);
  assert.equal(events.length, data.events.length);
  assert.equal(events.filter(e => e.returns_from).length, events.filter(e => e.previouslyRemovedOn).length);
  const bvlgari = events.find(e => e.name === "Bvlgari Hotel Shanghai" && e.change === "added");
  assert.ok(bvlgari.blip, "the April Bulgari -> Bvlgari return is hidden as a drop-out");
});

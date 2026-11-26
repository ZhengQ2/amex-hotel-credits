// Logic for changes.html: reads changes.json (written by hotel_history.py) and
// follows its links between a removal and the hotel's return, so brief
// drop-outs can be hidden.
(function (root) {
  "use strict";
  const DAY = 86400000;
  // A hotel back on its list within this many days dropped off by mistake (a
  // glitch on the source site), so the removal and the return are not news.
  const BLIP_DAYS = 14;
  const PROGRAMS = ["Hilton Portfolio", "Fine Hotels + Resorts", "The Hotel Collection"];
  const CHANGES = ["added", "removed", "renamed"];

  const dayNumber = date => Date.UTC(+date.slice(0, 4), +date.slice(5, 7) - 1, +date.slice(8, 10)) / DAY;
  const today = (now = Date.now()) => Math.floor(now / DAY);
  const fold = value => String(value || "").normalize("NFKD").replace(/[\u0300-\u036f]/g, "")
    .toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

  // hotel_history.py decides which events are the same hotel (names, Google place,
  // sibling safeguards) and links them: an addition that undoes a removal carries
  // returns_from, a removal that undoes an addition carries added_on.
  function annotate(events) {
    const key = (program, change, date, name) => `${program}|${change}|${date}|${name}`;
    const index = new Map(events.map(event => [key(event.program, event.change, event.date, event.name), event]));
    events.forEach(event => {
      if (event.change === "added" && event.returns_from) {
        const removed = index.get(key(event.program, "removed", event.returns_from.date, event.returns_from.name));
        if (!removed) return;
        const gap = event.day - removed.day;
        removed.returnedOn = event.date;
        event.previouslyRemovedOn = removed.date;
        event.gapDays = removed.gapDays = gap;
        event.blip = removed.blip = gap <= BLIP_DAYS;
        if (removed.name !== event.name) {
          removed.returnedAs = event.name;
          event.removedAs = removed.name;
        }
      } else if (event.change === "removed" && event.added_on) {
        const added = index.get(key(event.program, "added", event.added_on.date, event.added_on.name));
        if (added) added.removedOn = event.date;
      }
    });
    return events;
  }

  function parseLog(data) {
    if (!data || data.version !== 1 || !Array.isArray(data.events)) throw new Error("Unsupported change log");
    const events = data.events
      .filter(event => event && /^\d{4}-\d{2}-\d{2}$/.test(event.date) && PROGRAMS.includes(event.program)
        && CHANGES.includes(event.change) && event.name)
      .map((event, index) => ({ ...event, index, day: dayNumber(event.date) }));
    events.sort((a, b) => b.day - a.day || a.index - b.index);
    return { trackingSince: data.tracking_since || {}, events: annotate(events) };
  }

  function matchesQuery(event, query) {
    if (!query) return true;
    return [event.name, event.previous_name, event.maybe_renamed_from, event.maybe_renamed_to,
      event.brand, event.location, event.address].some(value => fold(value).includes(query));
  }

  // filters: { programs: Set, changes: Set, sinceDay: number|null, query: string, hideBlips: boolean }
  function filterEvents(events, filters) {
    const query = fold(filters.query);
    return events.filter(event =>
      (!filters.programs || filters.programs.has(event.program))
      && (!filters.changes || filters.changes.has(event.change))
      && (filters.sinceDay == null || event.day >= filters.sinceDay)
      && !(filters.hideBlips && event.blip)
      && matchesQuery(event, query));
  }

  function countChanges(events) {
    const counts = { added: 0, removed: 0, renamed: 0 };
    events.forEach(event => { counts[event.change] += 1; });
    return counts;
  }

  function summarize(events) {
    return Object.fromEntries(PROGRAMS.map(program => [program, countChanges(events.filter(event => event.program === program))]));
  }

  // Newest day first; within a day, one group per program in map order.
  function groupByDay(events) {
    const days = new Map();
    events.forEach(event => {
      if (!days.has(event.date)) days.set(event.date, { date: event.date, day: event.day, byProgram: new Map() });
      const byProgram = days.get(event.date).byProgram;
      if (!byProgram.has(event.program)) byProgram.set(event.program, []);
      byProgram.get(event.program).push(event);
    });
    return [...days.values()].sort((a, b) => b.day - a.day).map(({ date, day, byProgram }) => ({
      date, day,
      groups: PROGRAMS.filter(program => byProgram.has(program)).map(program => {
        const list = byProgram.get(program);
        list.sort((a, b) => CHANGES.indexOf(a.change) - CHANGES.indexOf(b.change)
          || a.name.localeCompare(b.name, "en", { sensitivity: "base" }));
        return { program, events: list, counts: countChanges(list), sources: [...new Set(list.map(event => event.source).filter(Boolean))] };
      }),
    }));
  }

  const api = { BLIP_DAYS, PROGRAMS, CHANGES, dayNumber, today, fold, annotate, parseLog, filterEvents, summarize, countChanges, groupByDay };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.HotelChanges = api;
})(typeof window !== "undefined" ? window : globalThis);

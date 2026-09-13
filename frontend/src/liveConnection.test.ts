import { describe, expect, it } from "vitest";
import { acceptSnapshot, updatesFresh } from "./liveConnection";

const now = Date.parse("2026-09-13T06:00:00Z");
const snap = (offset = 0) => ({ ts: new Date(now + offset).toISOString() });
describe("live display truth", () => {
  it("accepts complete timestamped snapshots, never malformed or future frames", () => {
    expect(acceptSnapshot(snap(), undefined, now)).toBe(true);
    for (const bad of [null, [], {}, { ts: "bad" }, snap(60_000)]) {
      expect(acceptSnapshot(bad, snap(), now)).toBe(false);
    }
  });
  it("cannot regress REST state with an older stream frame", () => {
    expect(acceptSnapshot(snap(-1000), snap(), now)).toBe(false);
    expect(acceptSnapshot(snap(), snap(), now)).toBe(true);
  });
  it("does not call a stale server snapshot live just because it was received now", () => {
    expect(updatesFresh(snap(-30_000), now, now)).toBe(false);
    expect(updatesFresh(snap(), now - 30_000, now)).toBe(false);
    expect(updatesFresh(snap(), now, now)).toBe(true);
    expect(updatesFresh(snap(60_000), now, now)).toBe(false);
  });
});

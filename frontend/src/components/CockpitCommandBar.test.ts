import { describe, expect, it } from "vitest";
import { firstBlocker } from "./CockpitCommandBar";

describe("readiness blocker priority", () => {
  it("does not hide another lane's data failure behind live locks", () => {
    const rows = [
      { symbol: "BTC", observation_class: "shadow_observe", runtime_readiness: { live_blockers: ["capital_path_locked"] } },
      { symbol: "ETH", observation_class: "shadow_observe", runtime_readiness: { data_blockers: ["candle_gap"] } },
    ] as unknown as Parameters<typeof firstBlocker>[0];
    expect(firstBlocker(rows)).toBe("Data · ETH · candle gap");
  });
});

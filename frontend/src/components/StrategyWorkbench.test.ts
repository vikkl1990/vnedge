import { describe, expect, it } from "vitest";
import { regimeView, structureView } from "./StrategyWorkbench";

describe("strategy workbench evaluation projection", () => {
  it("reads the runtime regime fields from the feature envelope", () => {
    expect(regimeView({
      mreg_ready: true,
      features: {
        regime_state: "mean_revert",
        ema200_ready: true,
        daily_observations: 800,
        regime_ema_state: "range",
        regime_macd_impulse: "off",
        regime_rsi_zone: "mid",
      },
    })).toEqual({
      ready: true,
      state: "mean_revert",
      ema200Ready: true,
      dailyObservations: 800,
      emaState: "range",
      macdImpulse: "off",
      rsiZone: "mid",
    });
  });

  it("keeps top-level canonical fields ahead of compatibility fallbacks", () => {
    expect(regimeView({
      mreg_state: "continuation",
      features: { regime_state: "flat" },
    }).state).toBe("continuation");
  });

  it("reads structure diagnostics without inventing missing fields", () => {
    expect(structureView({
      features: {
        bos15_structure_ready: true,
        structure_1h: "up",
        htf_4h: "up",
        bos15_parent_identity_ok: true,
      },
    })).toMatchObject({
      ready: true,
      oneHourTrend: "up",
      fourHourTrend: "up",
      parentIdentity: true,
      avwap: undefined,
    });
  });
});

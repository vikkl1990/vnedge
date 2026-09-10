import { describe, expect, it } from "vitest";
import { regimeView, structureView } from "./StrategyWorkbench";

describe("strategy workbench evaluation projection", () => {
  it("shows loaded daily history without inventing EMA readiness at startup", () => {
    const result = regimeView({}, { evaluation_status: "awaiting_first_evaluation", daily_bars: 800, ema200_ready: null });
    expect(result.dailyObservations).toBe(800);
    expect(result.ema200Ready).toBeUndefined();
  });

  it("preserves zero confirmed lows and the quality reset evidence", () => {
    const result = structureView({ features: {
      bos15_structure_health_reason: "confirmed_swing_pair_not_ready",
      bos15_confirmed_high_count: 1,
      bos15_confirmed_low_count: 0,
      bos15_last_quality_reset_at: "2026-09-10T00:00:00+00:00",
      bos15_eligible_bars_since_reset: 14,
    } });
    expect(result.highCount).toBe(1);
    expect(result.lowCount).toBe(0);
    expect(result.reason).toBe("confirmed_swing_pair_not_ready");
    expect(result.lastReset).toBe("2026-09-10T00:00:00+00:00");
    expect(result.barsSinceReset).toBe(14);
  });
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

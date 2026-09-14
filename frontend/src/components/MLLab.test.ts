import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, it, expect } from "vitest";
import { MLLabView, mlAge, mlNumber, type MLLabPayload } from "./MLLab";

describe("ML Lab evidence semantics", () => {
  it("preserves unknown vs zero", () => {
    expect(mlNumber(null)).toBe("—");
    expect(mlNumber(0)).toBe("0");
    expect(mlAge(null)).toBe("unknown");
  });
  it("does not invent models, probability or a training action", () => {
    const html = renderToStaticMarkup(createElement(MLLabView, {}));
    expect(html).toContain("ML Lab");
    expect(html).toContain("NO TRADING AUTHORITY");
    expect(html).not.toContain("69%");
    expect(html).not.toContain("VALID EDGE");
    expect(html).not.toContain("Train Top");
  });
  it("quarantines the legacy status instead of showing its sample count", () => {
    const data: MLLabPayload = { artifact_state: "LEGACY_UNVERIFIED", audit: null,
      worker_generated_at: null, source_as_of: null, worker_age_s: null, source_age_s: null };
    const html = renderToStaticMarkup(createElement(MLLabView, { data }));
    expect(html).toContain("labels and validation claims are not admitted");
    expect(html).toContain("Latest source record");
  });
  it("shows zero real labels and real pipeline counts without a promotion action", () => {
    const data: MLLabPayload = { artifact_state: "CURRENT", audit: null,
      worker_generated_at: null, source_as_of: null, worker_age_s: 0, source_age_s: null,
      pipeline: { schema: "ml_lab_pipeline_v1", plans_total: 2, datasets_total: 1,
        runs_total: 0, predictions_total: 0, ledger_bound_paper_labels: 0,
        ledger_coverage_complete: false, failed_attempts: 1, incomplete_attempts: 0,
        datasets: [], runs: [], predictions: [], errors: [] } };
    const html = renderToStaticMarkup(createElement(MLLabView, { data }));
    expect(html).toContain("Ledger-bound paper labels");
    expect(html).toContain("1 failed");
    expect(html).toContain("incomplete");
    expect(html).not.toContain("VALID EDGE");
    expect(html).not.toContain("Train Top");
  });
  it("shows owned next actions and per-lane blockers without a start-training button", () => {
    const data: MLLabPayload = { artifact_state: "CURRENT", audit: null,
      worker_generated_at: null, source_as_of: null, worker_age_s: 0, source_age_s: null,
      pipeline: { schema: "ml_lab_pipeline_v1", plans_total: 0, datasets_total: 0,
        runs_total: 0, predictions_total: 0, failed_attempts: 0, incomplete_attempts: 0,
        datasets: [], runs: [], predictions: [], errors: [],
        readiness_worklist: [{id: "funding", title: "Settled funding evidence", status: "PENDING", owner: "external_source", action: "Rate candles are insufficient."}],
        ledger_sources: [{lane: "fixture_lane", state: "BLOCKED", labels: 0, rejections: {broken_or_legacy_chain: 1}}],
        non_label_sources: [{file: "delta_product_specs.journal.jsonl", reason: "venue_product_specifications"}] } };
    const html = renderToStaticMarkup(createElement(MLLabView, { data }));
    expect(html).toContain("What still needs evidence");
    expect(html).toContain("Rate candles are insufficient.");
    expect(html).toContain("fixture_lane");
    expect(html).toContain("broken or legacy chain: 1");
    expect(html).toContain("delta_product_specs.journal.jsonl");
    expect(html).not.toContain("Train Top");
    expect(html).not.toContain("VALID EDGE");
  });
});

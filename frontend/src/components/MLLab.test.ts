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
});

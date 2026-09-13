import { describe, it, expect } from "vitest";
import { queueOutcome, queuePrice, queueTime } from "./SignalQueue";
import { SignalQueue } from "./SignalQueue";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

describe("signal queue presentation truth", () => {
  it("never renders missing price as zero", () => {
    expect(queuePrice(null)).toBe("—");
    expect(queuePrice(NaN)).toBe("—");
    expect(queuePrice(0)).toBe("0");
  });
  it("uses explicit UTC timestamps", () => {
    expect(queueTime("2026-09-13T12:30:00+05:30")).toBe("2026-09-13 07:00:00 UTC");
    expect(queueTime("garbage")).toBe("—");
    expect(queueTime(null)).toBe("—");
  });
  it("labels simulated results and never presents them as a fill", () => {
    expect(queueOutcome({ outcome_basis: "research_observation", research_net_usd: 1.2, has_fill: false })).toBe("Research sim. +$1.20");
    expect(queueOutcome({ outcome_basis: "order_journal", research_net_usd: 1.2, has_fill: false })).toBe("No recorded fill");
    expect(queueOutcome({ outcome_basis: "order_journal", research_net_usd: null, has_fill: true })).toBe("Fill recorded · PnL in Book");
  });
  it("renders the real empty state with read-only navigation, not example trades", () => {
    const client = new QueryClient();
    client.setQueryData(["signal-queue", "limit=25&population=decisions"], {
      generated_at: new Date().toISOString(), last_event_at: null, revision: "test",
      rows: [], next_cursor: null, sources: [{ lane: "lane", state: "ok", caught_up: true, invalid_records: 0 }],
      facets: {}, summary: { total: 0, decisions: 0, with_fills: 0, rejected: 0, identity_gaps: 0 },
    });
    const html = renderToStaticMarkup(createElement(QueryClientProvider, { client }, createElement(SignalQueue)));
    expect(html).toContain("No armed decisions in this window");
    expect(html).toContain("Research observations");
    expect(html).not.toContain("VALID EDGE");
    expect(html).not.toContain("Place order");
    expect(html).not.toContain("69%");
    client.clear();
  });
});

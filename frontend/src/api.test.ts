import { describe, expect, it, vi } from "vitest";
import { apiGet, urlWithoutCredentials } from "./api";

it("bounds read requests so stalled connections cannot disable polling", async () => {
  const timeout = vi.spyOn(AbortSignal, "timeout");
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true })));
  vi.stubGlobal("fetch", fetcher);
  try {
    expect(await apiGet("/api/services")).toEqual({ ok: true });
    expect(timeout).toHaveBeenCalledWith(20_000);
    expect(fetcher.mock.calls[0][1].signal).toBeInstanceOf(AbortSignal);
  } finally {
    vi.unstubAllGlobals();
    timeout.mockRestore();
  }
});

describe("urlWithoutCredentials", () => {
  it("removes legacy query credentials and preserves the selected tab", () => {
    expect(urlWithoutCredentials("https://desk.example/app/?token=secret#patterns"))
      .toBe("/app/#patterns");
  });

  it("preserves unrelated query parameters", () => {
    expect(urlWithoutCredentials("https://desk.example/app/?mode=compact&token=secret#desk"))
      .toBe("/app/?mode=compact#desk");
  });
});

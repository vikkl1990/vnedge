import { describe, expect, it } from "vitest";
import { urlWithoutCredentials } from "./api";

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

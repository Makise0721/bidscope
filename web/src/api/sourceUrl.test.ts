import { describe, expect, it } from "vitest";
import { isAllowedSourceUrl } from "./sourceUrl";

describe("source URL display policy", () => {
  it("allows only exact HTTPS origins without query or fragment data", () => {
    expect(isAllowedSourceUrl("ccgp", "https://www.ccgp.gov.cn/tender/1")).toBe(true);
    expect(isAllowedSourceUrl("ccgp", "https://www.ccgp.gov.cn/tender/1?token=secret")).toBe(false);
    expect(isAllowedSourceUrl("ccgp", "https://www.ccgp.gov.cn/tender/1#details")).toBe(false);
    expect(isAllowedSourceUrl("ccgp", "https://user:password@www.ccgp.gov.cn/tender/1")).toBe(false);
  });
});

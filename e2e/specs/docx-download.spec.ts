import { test, expect } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

/**
 * Flow: Complete a run, click the DOCX download button, verify the downloaded
 * file is a valid .docx (check magic bytes PK\x03\x04).
 */
test.describe("DOCX download", () => {
  test("downloads a valid DOCX file for a completed run", async ({ page }) => {
    await page.goto("/");

    // Create a run and let it complete.
    const input = page.locator("#query-input");
    await input.fill("四川服务器招标");
    await page.getByRole("button", { name: "Search" }).click();

    // Approve confirmation and wait for the report page.
    const confirmation = page.getByRole("region", { name: "confirm intent" });
    await confirmation
      .waitFor({ state: "visible", timeout: 15_000 })
      .then(() => confirmation.getByRole("button", { name: "Approve" }).click())
      .catch(() => {
        // Confirmation may not appear for auto-confirmed runs.
      });
    await page.waitForURL(/#\/runs\/[0-9a-f-]+$/, { timeout: 30_000 });

    // Click the Download DOCX button.
    const downloadButton = page.getByRole("button", { name: /Download DOCX/ });
    // Fallback: the download link may render as an <a> with aria-label.
    const downloadLink = page.getByRole("link", { name: /Download DOCX/ });
    const downloadTarget =
      (await downloadButton.count()) > 0 ? downloadButton : downloadLink;
    await expect(downloadTarget).toBeVisible();

    const downloadPromise = page.waitForEvent("download");
    await downloadTarget.click();
    const download = await downloadPromise;

    // Save the download to a temp file and verify the magic bytes.
    const tempPath = join(tmpdir(), `bidscope-test-${Date.now()}.docx`);
    await download.saveAs(tempPath);

    const buffer = await readFile(tempPath);
    // DOCX files are ZIP archives: first two bytes are PK, next two are \x03\x04.
    expect(buffer.length).toBeGreaterThan(4);
    expect(buffer[0]).toBe(0x50); // P
    expect(buffer[1]).toBe(0x4b); // K
    expect(buffer[2]).toBe(0x03);
    expect(buffer[3]).toBe(0x04);
  });
});

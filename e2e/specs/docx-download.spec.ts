import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

/**
 * Flow: complete an unscheduled run, click the DOCX download link, verify the
 * downloaded file is a valid Office Open XML (.docx) archive.
 *
 * The representative query auto-confirms, so no Approve click is needed. The
 * DOCX asset is produced by the delivery node and fetched via an ``<a>`` link;
 * we assert the report is present before triggering the download.
 */
test.describe("DOCX download", () => {
  test("downloads a valid DOCX file for a completed run", async ({ page }) => {
    await page.goto("/");

    const input = page.getByLabel("Enter your request");
    await input.fill("四川服务器招标");
    await page.getByRole("button", { name: "Search" }).click();

    // The run completes and the report renders inline with its download link.
    const report = page.getByRole("region", { name: "report" });
    await expect(report).toBeVisible({ timeout: 30_000 });

    const downloadLink = page.getByRole("link", { name: "Download DOCX" });
    await expect(downloadLink).toBeVisible();

    const downloadPromise = page.waitForEvent("download");
    await downloadLink.click();
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

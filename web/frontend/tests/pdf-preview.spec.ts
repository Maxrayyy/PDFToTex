import { test, expect } from "@playwright/test";
import path from "node:path";

test("XeLaTeX Chinese glyphs actually appear in the PDF canvas", async ({ page }) => {
  const warnings: string[] = [];
  page.on("console", message => {
    if (/Warning:|Error during font/.test(message.text())) warnings.push(message.text());
  });
  await page.route("**/api/artifacts/chinese", route => route.fulfill({json: {
    id: "chinese", name: "chinese.pdf", kind: "pdf", attempt: 1,
    created_at: 0, job: "chinese-job",
  }}));
  await page.route("**/api/jobs/chinese-job", route => route.fulfill({json: {
    id: "chinese-job", kind: "tex_compile", attempt: 1, status: "succeeded",
  }}));
  await page.route("**/api/artifacts/chinese/file", route => route.fulfill({
    path: path.resolve("tests/fixtures/chinese.pdf"), contentType: "application/pdf",
  }));
  await page.goto("/artifacts/chinese/preview");
  await expect(page.getByLabel("PDF 页码")).toHaveAttribute("max", "1");
  // The first line is exclusively Chinese; successful PDF loading is insufficient.
  await expect.poll(async () => page.locator("canvas").evaluate(canvas => {
    const c = canvas as HTMLCanvasElement;
    const data = c.getContext("2d")!.getImageData(
      Math.floor(c.width * .2), Math.floor(c.height * .145),
      Math.floor(c.width * .6), Math.floor(c.height * .025),
    ).data;
    let ink = 0;
    for (let i = 0; i < data.length; i += 4)
      if (data[i + 3] > 0 && data[i] < 100) ink++;
    return ink;
  })).toBeGreaterThan(100);
  expect(warnings).toEqual([]);
});

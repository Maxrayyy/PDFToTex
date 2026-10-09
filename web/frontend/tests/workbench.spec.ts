import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
const audit = path.resolve("../../../data/audits/web-v1");
test("real TeX upload, compilation, durable preview and dark theme", async ({
  page,
}) => {
  const errors: string[] = [];
  const filename = "browser-test-" + Date.now() + ".tex";
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/compile");
  await expect(
    page.getByRole("heading", { name: "编译 TeX", exact: true }),
  ).toBeVisible();
  await page
    .locator("input[type=file]")
    .first()
    .setInputFiles({
      name: filename,
      mimeType: "text/plain",
      buffer: Buffer.from(
        "\\documentclass{article}\n\\begin{document}\nWeb compilation. $6.0\\times10^{7}$, $x\\geq 3$.\\newpage Second page.\\end{document}",
      ),
    });
  await expect(page.getByLabel("主 TeX 文件")).toHaveValue(filename);
  await page.getByRole("button", { name: "上传并加入队列" }).click();
  const row = page
    .getByRole("row")
    .filter({
      has: page.getByRole("button", { name: filename, exact: true }),
    })
    .last();
  await expect(row.getByText("已完成", { exact: true })).toBeVisible({
    timeout: 45000,
  });
  await row.getByRole("button", { name: "查看 PDF" }).click();
  await expect(page).toHaveURL(/\/artifacts\/.+\/preview/);
  await expect(page.locator("canvas")).toBeVisible();
  await expect(page.getByLabel("PDF 页码")).toHaveAttribute("max", "2");
  await page.getByRole("button", { name: "下一页", exact: true }).click();
  await expect(page.getByLabel("PDF 页码")).toHaveValue("2");
  await page.reload();
  await expect(page.getByLabel("PDF 页码")).toHaveAttribute("max", "2");
  await page.screenshot({
    path: path.join(audit, "preview.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "深色外观" }).click();
  await page.screenshot({
    path: path.join(audit, "preview-dark.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "概览", exact: true }).click();
  await page.screenshot({
    path: path.join(audit, "overview-dark.png"),
    fullPage: true,
  });
  expect(errors).toEqual([]);
});
test("directory selection lists nested PDFs and ignores other files", async ({
  page,
}) => {
  const folder = path.join(audit, "directory-fixture");
  fs.mkdirSync(path.join(folder, "nested"), { recursive: true });
  fs.writeFileSync(path.join(folder, "10.pdf"), "fake fixture, list only");
  fs.writeFileSync(path.join(folder, "2.PDF"), "fake fixture, list only");
  fs.writeFileSync(path.join(folder, "nested", "inside.pdf"), "fake fixture");
  fs.writeFileSync(path.join(folder, "notes.txt"), "ignored");
  await page.goto("/queue");
  await page.locator("input[webkitdirectory]").setInputFiles(folder);
  await expect(page.getByText("文件清单 · 3 个")).toBeVisible();
  await expect(page.locator(".upload-file")).toHaveCount(3);
  expect(await page.locator(".upload-file").first().innerText()).toContain(
    "2.PDF",
  );
  await expect(
    page.locator(".upload-file").filter({ hasText: "notes.txt" }),
  ).toHaveCount(0);
  await expect(page.getByLabel("识别页并发")).toHaveValue("1");
  await page.screenshot({
    path: path.join(audit, "queue.png"),
    fullPage: true,
  });
});

test("task detail exposes TeX, quality checks and live logs", async ({
  page,
  request,
}) => {
  const overview = await (await request.get("/api/overview")).json();
  const job = overview.jobs
    .filter(
      (j: { name: string; status: string }) =>
        j.name.startsWith("browser-test") && j.status === "succeeded",
    )
    .at(-1);
  expect(job).toBeTruthy();
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/jobs/" + job.id);
  await page.getByRole("button", { name: "检查结果", exact: true }).click();
  await expect(page.getByText("缺失字符", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "TeX", exact: true }).click();
  await expect(page.locator(".tex-source")).toContainText("documentclass");
  await page.getByRole("button", { name: "日志", exact: true }).click();
  await expect(page.locator(".log")).toContainText("XeLaTeX pass 2/2");
  await page.getByRole("button", { name: "配置", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "新增到 Overleaf" }),
  ).toBeDisabled();
  await page.screenshot({
    path: path.join(audit, "detail.png"),
    fullPage: true,
  });
  expect(errors).toEqual([]);
});

test("provider key is not echoed, templates persist, operations and mobile render", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/settings");
  await page
    .getByLabel("供应商", { exact: true })
    .selectOption("自定义兼容服务");
  await page.getByLabel("Base URL").fill("http://127.0.0.1:9876/v1");
  await page.getByLabel("API Key").fill("browser-fixture-key");
  await page.getByRole("button", { name: "保存供应商", exact: true }).click();
  await expect(page.getByLabel("API Key")).toHaveValue("");
  await expect(page.getByRole("status")).toContainText("供应商已保存");
  expect(await page.locator("body").innerText()).not.toContain(
    "browser-fixture-key",
  );
  await page.goto("/queue");
  await page
    .locator("input[type=file]")
    .first()
    .setInputFiles({
      name: "fixture.pdf",
      mimeType: "application/pdf",
      buffer: Buffer.from("list only"),
    });
  const provider = await page
    .getByLabel("识别供应商")
    .locator("option")
    .last()
    .getAttribute("value");
  await page.getByLabel("识别供应商").selectOption(provider!);
  await page.getByLabel("识别模型").fill("gpt-fixture");
  await page.getByPlaceholder("模板名称").fill("浏览器测试模板");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByLabel("配置模板").locator("option").last()).toHaveText(
    "浏览器测试模板",
  );
  await page.getByRole("button", { name: "运行记录", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Web 任务日报" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "批次与产物", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "历史 TeX · 只读索引" }),
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "概览", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "文档处理，一目了然" }),
  ).toBeVisible();
  expect(
    await page.evaluate(() => document.body.scrollWidth <= window.innerWidth),
  ).toBeTruthy();
  await page.screenshot({
    path: path.join(audit, "mobile.png"),
    fullPage: true,
  });
  expect(errors).toEqual([]);
});

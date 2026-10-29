// Walks the M2 pages against a fresh, disposable service started with the demo executor:
//   maestro --config <tmp>/config.toml init        (prints the setup token)
//   maestro --config <tmp>/config.toml serve --port 8431 --demo-executor
//   MAESTRO_SETUP_TOKEN=<token> npm run test:ui
// Never point this at a real team state: it creates an admin, runs, PATs and invitations.
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";

const token = process.env.MAESTRO_SETUP_TOKEN;
const password = "smoke-password-123";
const shots = process.env.MAESTRO_SHOTS ?? "/tmp";

/** A link in the sidebar (pages also have their own links with the same names). */
const nav = (page: Page, name: string) =>
  page.getByRole("navigation", { name: "主导航" }).getByRole("link", { name, exact: true });
/** The new-run form, from the button on the run list. */
const newRun = async (page: Page) => {
  await nav(page, "运行").click();
  await page.getByRole("link", { name: "新建运行" }).click();
};
/** A link in the administration sections, under "管理" in the sidebar. */
const adminNav = (page: Page, name: string) =>
  page.getByRole("navigation", { name: "管理" }).getByRole("link", { name, exact: true });



test("setup, run, PAT, admin and invitation flow", async ({ page, browser }) => {
  test.skip(!token, "MAESTRO_SETUP_TOKEN not set");
  await page.goto("/");
  await expect(page).toHaveTitle("Maestro");
  await page.getByLabel("初始化令牌").fill(token!);
  await page.getByLabel("管理员用户名").fill("admin");
  await page.getByLabel("密码").fill(password);
  await page.getByRole("button", { name: "创建管理员并登录" }).click();
  await expect(page.getByRole("heading", { name: "工作台" })).toBeVisible();
  await expect(page.getByText("可受理运行").first()).toBeVisible();

  // Datasets: add a version from the (fake) source; first preparation admits its tasks.
  await nav(page, "数据集").click();
  await page.getByRole("button", { name: "添加数据集" }).click();
  await page.getByLabel("作者 / 名称").fill("demo");
  await page.getByRole("button", { name: "搜索" }).click();
  await page.getByRole("button", { name: "demo/arith" }).click();
  await page.getByRole("row", { name: /^v1 / }).getByRole("button", { name: "添加为新版本…" }).click();
  await page.getByRole("button", { name: "添加为新版本", exact: true }).click();
  await expect(page.getByText("add-01")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("缓存就绪").first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("已准入 3 · 可提交 3")).toBeVisible({ timeout: 30_000 });
  await page.screenshot({ path: `${shots}/datasets.png`, fullPage: true });
  // Evicting a cache keeps the task submittable; the next run restores it.
  await page.getByRole("row", { name: /add-01/ }).getByRole("button", { name: "回收缓存" }).click();
  await expect(page.getByText("运行时恢复")).toBeVisible({ timeout: 30_000 });
  await page.getByRole("button", { name: "add-01" }).click();
  await expect(page.getByText("作者参数")).toBeVisible();
  await page.screenshot({ path: `${shots}/datasets-cold.png`, fullPage: true });
  await page.getByRole("button", { name: "收起", exact: true }).click();

  // A new version from the dataset's card: upstream v2 is not imported yet; its preview
  // compares tasks with v1. Added without first preparation, so v1 stays the default.
  await page.getByRole("button", { name: "检查上游更新" }).click();
  await expect(page.getByRole("row", { name: /^v1 / }).getByText("已导入（v1）")).toBeVisible();
  await page.getByRole("row", { name: /^v2 / }).getByRole("button", { name: "添加为新版本…" }).click();
  await expect(page.getByText("新增 1 · 删除 0 · 内容变化 3 · 未变 0")).toBeVisible();
  await page.getByLabel("添加后立即进行首次准备").uncheck();
  await page.getByRole("button", { name: "添加为新版本", exact: true }).click();
  await expect(page.getByRole("row", { name: /^v2 / }).getByText("已导入（v2）")).toBeVisible({
    timeout: 30_000,
  });
  // The selected version is in the address and survives a reload.
  await expect(page).toHaveURL(/[?&]version=/);
  await page.reload();
  await expect(page.getByText("demo/arith@v2")).toBeVisible();

  // New run: dataset, then version; the default is the latest one with submittable tasks.
  await newRun(page);
  await expect(page.locator("#run-dataset option:checked")).toHaveText("demo/arith");
  await expect(page.locator("#run-version option:checked")).toContainText("v1");
  await expect(page.locator("#run-version option", { hasText: "v2" })).toBeDisabled();
  await page.getByLabel("运行名称").fill("smoke run");
  await page.getByLabel("手动选择").check();
  await page.getByLabel("选择 add-01").check();
  await page.getByLabel("选择 mul-01").check();
  // The demo executor has no model: Oracle. Check the configuration, then submit it.
  await page.getByLabel(/^Oracle/).check();
  await page.getByRole("button", { name: "检查配置" }).click();
  await page.getByRole("button", { name: "按此配置提交运行" }).click();
  await expect(page).toHaveURL(/\/runs\/(?!new$)[^/]+$/);
  const runUrl = page.url();
  await expect(page.getByText("每 2 秒自动刷新")).toBeVisible();
  await expect(page.getByText("已结束")).toBeVisible({ timeout: 60_000 });
  for (const title of ["生命周期", "评分覆盖", "产物保存", "环境清理"]) {
    await expect(page.getByText(title, { exact: true })).toBeVisible();
  }
  // The execution page (U-07): trials on the left, the selected trial's steps on the right;
  // a step opens its log. The selected trial is kept in the URL.
  const trials = page.getByTestId("trial-row");
  await expect(trials).toHaveCount(2);
  await trials.nth(1).click();
  await expect(trials.nth(1)).toHaveAttribute("aria-current", "true");
  await expect(page).toHaveURL(/[?&]trial=/);
  const panel = page.getByTestId("trial-panel");
  await expect(panel.getByText("第 1 次尝试")).toBeVisible();
  await panel.getByRole("button", { name: /Agent/ }).first().click();
  await expect(panel.getByTestId("step-log").first()).toBeVisible();
  await page.screenshot({ path: `${shots}/run-execution.png`, fullPage: true });
  const selectedUrl = page.url();
  await page.reload();
  await expect(page.getByTestId("trial-row").nth(1)).toHaveAttribute("aria-current", "true");
  expect(page.url()).toBe(selectedUrl);

  // Deep link reload; the configuration snapshot is its own tab.
  await page.goto(runUrl);
  await page.getByRole("tab", { name: "配置快照" }).click();
  await expect(page.getByText("生效配置（effective）")).toBeVisible();
  // The dataset version links to its page on the datasets page.
  await page.getByRole("link", { name: /^dsv_/ }).click();
  await expect(page.getByText("demo/arith@v1")).toBeVisible();
  await page.goto(runUrl);

  // M6 results: summary card, per-task rows, artifact retention.
  await page.getByRole("tab", { name: "结果汇总" }).click();
  const summary = page.getByTestId("result-summary");
  await expect(summary).toBeVisible();
  await expect(summary.getByText("平台默认")).toBeVisible();
  await expect(page.getByTestId("task-summary-row")).toHaveCount(2);
  await page.screenshot({ path: `${shots}/run-summary.png`, fullPage: true });

  // N-05: artifacts as a tree; the run and each task, trial and attempt download as a zip.
  await page.getByRole("tab", { name: /^产物/ }).click();
  const wholeRun = page.getByRole("link", { name: "下载整个运行的产物（zip）" });
  await expect(wholeRun).toBeVisible();
  await page.getByRole("button", { name: /mul-01/ }).click();
  await page.getByRole("button", { name: /试次 #1/ }).first().click();
  await expect(page.getByRole("link", { name: /^下载尝试 1 的产物/ }).first()).toBeVisible();
  for (const link of [wholeRun, page.getByRole("link", { name: /^下载试次 #1 的产物/ }).first()]) {
    const zip = await page.request.get(new URL((await link.getAttribute("href"))!, page.url()).href);
    expect(zip.status()).toBe(200);
    expect(zip.headers()["content-type"]).toBe("application/zip");
    expect((await zip.body()).subarray(0, 2).toString()).toBe("PK");
  }
  await page.screenshot({ path: `${shots}/run-artifacts.png`, fullPage: true });

  // The workbench: my recent runs with their score coverage, pending items, system status.
  await nav(page, "工作台").click();
  await expect(page.getByRole("heading", { name: "工作台" })).toBeVisible();
  await expect(page.getByRole("link", { name: /smoke run/ }).first()).toBeVisible();
  await expect(page.getByTestId("pending")).toBeVisible();
  await page.screenshot({ path: `${shots}/workbench.png`, fullPage: true });

  // Run list score column: a mean always comes with its coverage.
  await nav(page, "运行").click();
  await expect(page.getByTestId("score-coverage").first()).toContainText("已评分 ");

  // Export the run, wait for the archive, download it.
  await page.getByLabel("选择 smoke run").check();
  await page.getByRole("button", { name: /导出所选/ }).click();
  await expect(page).toHaveURL(/\/exports$/);
  await expect(page.getByRole("link", { name: "下载" })).toBeVisible({ timeout: 60_000 });
  const download = page.waitForEvent("download");
  await page.getByRole("link", { name: "下载" }).click();
  expect((await download).suggestedFilename()).toMatch(/\.zip$/);
  await page.screenshot({ path: `${shots}/exports.png`, fullPage: true });


  // PAT shown once; the SDK card tells how to use it.
  await nav(page, "设置").click();
  await expect(page.getByTestId("sdk-card")).toContainText("MaestroClient");
  await page.getByLabel("名称").fill("smoke pat");
  await page.getByRole("button", { name: "创建令牌" }).click();
  await expect(page.getByText("只显示一次")).toBeVisible();
  await expect(page.getByTestId("one-time-secret")).toBeVisible();
  await page.getByRole("button", { name: "我已保存，关闭" }).click();
  await expect(page.getByText("smoke pat")).toBeVisible();
  await page.getByTestId("sdk-card").screenshot({ path: `${shots}/settings-sdk.png` });

  // Admin pages: a sub-navigation, one URL per section.
  await nav(page, "管理").click();
  await expect(page).toHaveURL(/\/admin\/members$/);
  await expect(page.getByRole("cell", { name: "admin", exact: true }).first()).toBeVisible();
  await adminNav(page, "凭据与主密钥").click();
  await page.getByLabel("名称").fill("smoke-key");
  await page.getByLabel("提供商").fill("fake");
  await page.getByLabel("值", { exact: true }).fill("not-a-real-secret");
  await page.getByRole("button", { name: "添加凭据" }).click();
  await expect(page.getByRole("cell", { name: "smoke-key" })).toBeVisible();
  await expect(page.getByText("代（generation）")).toBeVisible();
  await adminNav(page, "维护").click();
  await expect(page.getByTestId("maintenance")).toContainText("进程存活");
  await expect(page.getByTestId("maintenance").getByText("alive")).toBeVisible();
  await page.screenshot({ path: `${shots}/admin-maintenance.png`, fullPage: true });
  await adminNav(page, "数据集导入").click();
  await expect(page.getByText("首次准备").first()).toBeVisible();
  await adminNav(page, "审计").click();
  await expect(page.getByText("run.cancel").or(page.getByText("credential.create")).first()).toBeVisible();
  await adminNav(page, "成员与邀请").click();
  await expect(page.getByRole("heading", { name: "管理 · 成员与邀请" })).toBeVisible();
  await expect(page.getByRole("button", { name: "生成邀请链接" })).toBeVisible();
  await page.screenshot({ path: `${shots}/admin-members.png`, fullPage: true });
  await page.getByRole("button", { name: "生成邀请链接" }).click();
  const link = (await page.getByTestId("one-time-secret").textContent())!;
  expect(link).toContain("/invite#");

  // Accept the invitation in a fresh browser context: the member has no admin nav.
  const other = await browser.newContext();
  const member = await other.newPage();
  await member.goto(link);
  await expect(member.getByRole("heading", { name: "接受邀请" })).toBeVisible();
  await member.getByLabel("用户名").fill("alice");
  await member.getByLabel("密码").fill(password);
  await member.getByRole("button", { name: "创建账户并登录" }).click();
  await expect(member.getByRole("heading", { name: "工作台" })).toBeVisible();
  await expect(member.getByRole("link", { name: "管理" })).toHaveCount(0);
  await expect(member.getByText("smoke run").first()).toBeVisible();
  await member.getByRole("button", { name: "退出" }).click();
  await expect(member.getByRole("heading", { name: "登录" })).toBeVisible();
  await other.close();

  // Deletion: PAT revoke then delete; the run goes to the recycle bin, is restored,
  // deleted again and purged (the prompt asks for the object id).
  const runId = runUrl.split("/").pop()!;
  page.on("dialog", (dialog) => void dialog.accept(dialog.type() === "prompt" ? runId : undefined));
  await nav(page, "设置").click();
  const patRow = page.getByRole("row").filter({ hasText: "smoke pat" });
  await patRow.getByRole("button", { name: "撤销" }).click();
  await expect(patRow.getByText("已撤销")).toBeVisible();
  await patRow.getByRole("button", { name: "删除", exact: true }).click();
  await expect(page.getByText("smoke pat")).toHaveCount(0);
  await page.goto(runUrl);
  await page.getByRole("button", { name: "删除", exact: true }).click();
  await expect(page.getByText("该运行已删除，在回收站中")).toBeVisible();
  await page.goto("/admin/recycle-bin");
  const runEntry = page.getByTestId("recycle-row").filter({ hasText: "smoke run" });
  await runEntry.getByRole("button", { name: "恢复" }).click();
  await expect(runEntry).toHaveCount(0);
  await page.goto(runUrl);
  await expect(page.getByText("该运行已删除，在回收站中")).toHaveCount(0);
  await page.getByRole("button", { name: "删除", exact: true }).click();
  await expect(page.getByText("该运行已删除，在回收站中")).toBeVisible();
  await page.goto("/admin/recycle-bin");
  await runEntry.getByRole("button", { name: "彻底删除" }).click();
  await expect(runEntry).toHaveCount(0);
  await page.getByRole("button", { name: "删除回执" }).click();
  await expect(runEntry).toBeVisible();
  await page.screenshot({ path: `${shots}/recycle-receipts.png`, fullPage: true });
});

// Fixtures for the tests below that intercept API responses (no backend state touched).
const meta = { request_id: "req_test" };
const me = {
  id: "usr_1", username: "u", display_name: "U", role: "member", status: "active",
  revision: 1, created_at: "2026-10-22T00:00:00Z", disabled_at: null,
  auth_channel: "web_session", pat_id: null, pat_scope: null,
};
const run = {
  id: "run_x", name: "fixture run", owner_id: "usr_2",
  owner: { username: "gone", display_name: "Gone", deleted: true },
  scoring: null, deletion: null, queue_reason: null, heartbeat_at: null,
  dataset_version_deletion: { state: "recycled", deleted_at: "2026-10-22T01:00:00Z", purged_at: null },
  pat_id: null, relation: "initial",
  source_run_id: null, dataset_version_id: "dsv_1", operation_id: "op_1",
  client_request_id: null, state: "finished", result_completeness: "partial",
  artifact_state: "failed", cleanup_state: "failed", stop_requested: false, stop_reason: null,
  counts: { planned: 3, terminal: 3, scored: 1, errors: 2, not_started: 0, cancelled: 0,
    interrupted: 0, active: 0, queued: 0 },
  accepted_at: "2026-10-22T00:00:00Z", started_at: null, finished_at: null,
  deadline_at: "2026-10-25T00:00:00Z", allowed_actions: ["view"], links: {},
  snapshot: { schema_version: 1, effective: {}, provenance: {}, policy: {},
    config_digest: "c", snapshot_digest: "s", comparison_digest: "d" },
};
const trial = (key: string, state: string, result: unknown, reason: string | null) => ({
  id: `trl_${key}`, task_id: `tsk_${key}`, task_key: key, repetition_index: 1, state,
  phase: null, reason_code: reason, artifact_state: "complete", cleanup_state: "complete",
  artifact_counts: { available: 1, failed: 0, expired: 0 },
  result, attempts: [{ id: `att_${key}`, attempt_index: 1, state: "failed", phase: "executing",
    steps: [], executor_ref: null,
    started_at: "2026-10-22T00:00:00Z", ended_at: "2026-10-22T00:00:01Z",
    error: { code: "AGENT_TIMEOUT", message: "agent timed out" } }],
});
const result = (score: number | null, cause: string | null) => ({
  raw_score: score, reward_valid: score !== null, score, failure_cause: cause,
  exception_type: null, verifier_status: cause === "verifier_failed" ? "no_reward" : "ok",
  policy_version: "p", attempt_id: "att",
});
/** A member session on a service that cannot accept runs. */
const sessionRoutes: Record<string, unknown> = {
  "/api/v1/setup": { needs_setup: false },
  "/api/v1/session": { user: me, csrf_token: "csrf" },
  // Fixtures follow the contract (SystemStatus, Capabilities in src/lib/schema.d.ts).
  "/api/v1/capabilities": { api_version: "v1", service_version: "t", schema_revision: "t",
    contract_digest: "sha256:t", team_policy_revision: 0, features: {}, limits: {},
    auth: { pat_header: "Authorization: Bearer <token>", session_cookie: true,
      csrf_header: "X-CSRF-Token" },
    actions: {},
    execution: { executor: null, modes: ["agent", "oracle"], harness_adapters: [],
      policy_version: "t", capacity: { slots: 1, cpus: 1, memory_mb: 1024 } } },
  "/api/v1/system-status": { status: "ready", accepting_runs: false,
    detail: { master_key_available: false, executor: null, worker_running: false,
      needs_setup: false, schema: { compatible: true, current: "t", head: "t" },
      storage: { level: "ok", free_bytes: 1, total_bytes: 1,
        accept_min_free: { gib: 0, percent: 0 }, dispatch_min_free: { gib: 0, percent: 0 } } } },
};

/** Answer API calls from ``routes`` (a path to its data); anything else is a 404. */
async function serveFixtures(page: Page, routes: Record<string, unknown>) {
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    const data = routes[path];
    return data === undefined
      ? route.fulfill({ status: 404, json: { error: { code: "NOT_FOUND", message: path,
          field_errors: [], retryable: false, suggested_actions: [] }, meta } })
      : route.fulfill({ json: { data, meta } });
  });
}

// Score rendering rules with intercepted API responses (no backend state touched):
// a valid 0, a missing score and an execution failure stay distinct; four run dimensions
// stay separate.
test("score and dimension rendering", async ({ page }) => {
  const routes: Record<string, unknown> = {
    ...sessionRoutes,
    "/api/v1/runs/run_x": run,
    "/api/v1/runs/run_x/trials": [
      trial("zero", "scored", result(0, null), null),
      trial("unscored", "execution_error", result(null, "agent_timeout"), "AGENT_TIMEOUT"),
      trial("noresult", "execution_error", null, "SETUP_FAILED"),
      trial("noreward", "execution_error", result(null, "verifier_failed"), null),
    ],
    "/api/v1/runs/run_x/artifacts": [],
  };
  await serveFixtures(page, routes);
  await page.goto("/runs/run_x");
  await expect(page.getByText("当前无法受理新运行")).toBeVisible();
  await expect(page.getByRole("link", { name: "管理" })).toHaveCount(0);
  const scores = page.getByTestId("trial-score");
  await expect(scores.nth(0)).toHaveText("0有效零分");
  await expect(scores.nth(1)).toContainText("执行失败");
  await expect(scores.nth(1)).toContainText("AGENT_TIMEOUT");
  await expect(scores.nth(2)).toContainText("执行失败");
  await expect(scores.nth(2)).toContainText("SETUP_FAILED");
  await expect(scores.nth(3)).toContainText("缺评分");
  const rows = page.getByTestId("trial-row");
  await expect(page.getByText("（已删除）")).toBeVisible();
  await expect(page.getByText("所用数据集版本已删除，在回收站中")).toBeVisible();
  await expect(page.getByRole("button", { name: "删除", exact: true })).toHaveCount(0);
  await expect(page.getByText("保存失败")).toBeVisible();
  await expect(page.getByText("清理失败")).toBeVisible();
  await expect(page.getByText("部分评分")).toBeVisible();
  await expect(page.getByRole("button", { name: "取消运行" })).toHaveCount(0);
  await rows.nth(1).click();
  await expect(page.getByText("AGENT_TIMEOUT：agent timed out")).toBeVisible();
});

// Long names and long lines stay inside their boxes (U-45): no page is wider than the window,
// on a desktop or a phone.
test("long content does not widen the page", async ({ page }) => {
  const prefix = "swebench-verified-django__django-";
  const keys = [`${prefix}11099`, `${prefix}11133`, `${prefix}${"x".repeat(300)}`];
  const longTrial = (key: string) => {
    const t = trial(key, "scored", result(1, null), null);
    t.attempts[0].steps = [
      { step: "agent", at: "2026-10-22T00:00:00Z" },
      { step: "verifying", at: "2026-10-22T00:00:01Z" },
    ] as never;
    return t;
  };
  const log = {
    id: "art_log", run_id: "run_x", trial_id: `trl_${keys[0]}`, attempt_id: `att_${keys[0]}`,
    kind: "log", relative_path: "agent/opencode", size_bytes: 6000, status: "available",
    status_reason: null, expires_at: null, digest: null, created_at: "2026-10-22T00:00:00Z",
  };
  const deep = { ...log, id: "art_deep", kind: "agent",
    relative_path: `agent/${"very-long-directory-name/".repeat(12)}trajectory.json` };
  await serveFixtures(page, {
    ...sessionRoutes,
    "/api/v1/runs/run_x": { ...run, name: `fixture run ${"with a long name ".repeat(12)}` },
    "/api/v1/runs/run_x/trials": keys.map(longTrial),
    "/api/v1/runs/run_x/artifacts": [log, deep],
  });
  await page.route("**/api/v1/artifacts/art_log/download*", (route) =>
    route.fulfill({ contentType: "text/plain", body: `$ echo ${"y".repeat(6000)}\n` }),
  );
  const fits = async () =>
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth),
    ).toBeLessThanOrEqual(0);

  await page.goto(`/runs/run_x?trial=trl_${keys[0]}`);
  // Names sharing a prefix lose its middle; the full name is on hover.
  await expect(page.getByTestId("trial-row").nth(1)).toContainText("swebench…-11133");
  await page.getByTestId("trial-panel").getByRole("button", { name: /Agent/ }).click();
  await expect(page.getByTestId("step-log")).toContainText("yyyy");
  await fits();
  // The execution view links to the trial's artifacts instead of listing them.
  await page.getByRole("button", { name: /产物 2 个/ }).click();
  await expect(page).toHaveURL(/tab=artifacts/);
  await expect(page.getByText("very-long-directory-name", { exact: false })).toBeVisible();
  await fits();
  await page.screenshot({ path: `${shots}/long-artifacts.png`, fullPage: true });
  // Folding the sidebar is remembered by the browser.
  await page.getByRole("button", { name: "收起侧边栏" }).click();
  await page.reload();
  await expect(page.getByRole("button", { name: "展开侧边栏" })).toBeVisible();
  await fits();
  await page.getByRole("button", { name: "展开侧边栏" }).click();

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/runs/run_x?trial=trl_${keys[2]}`);
  await expect(page.getByTestId("trial-panel")).toBeVisible();
  await fits();
  await page.getByRole("button", { name: "打开导航" }).click();
  await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();
  await page.screenshot({ path: `${shots}/phone-drawer.png` });
  await nav(page, "设置").click();
  await expect(page.getByRole("navigation", { name: "主导航" })).toHaveCount(0);
});

// M5: rerun a run, save and use an experiment plan, and submit a batch.
test("rerun, plan and batch", async ({ page }) => {
  test.skip(!token, "MAESTRO_SETUP_TOKEN not set");
  page.on("dialog", (dialog) => void dialog.accept());
  await page.goto("/");
  await page.getByLabel("用户名").fill("admin");
  await page.getByLabel("密码").fill(password);
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByRole("heading", { name: "工作台" })).toBeVisible();

  await newRun(page);
  await page.getByLabel("运行名称").fill("m5 source");
  await page.getByLabel(/^Oracle/).check();
  await page.getByLabel("把当前表单另存为方案").fill("smoke plan");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByText("已保存方案")).toBeVisible();
  await page.getByRole("button", { name: "检查配置" }).click();
  await page.getByRole("button", { name: "按此配置提交运行" }).click();
  await expect(page).toHaveURL(/\/runs\/(?!new$)[^/]+$/);
  const source = page.url();
  await expect(page.getByText("已结束")).toBeVisible({ timeout: 60_000 });

  // Rerun: a new run from the same snapshot, linked back to its source.
  await page.getByRole("button", { name: "重跑", exact: true }).click();
  await expect(page).not.toHaveURL(source);
  await expect(page.getByText("m5 source (rerun)")).toBeVisible();
  await expect(page.getByText("重跑范围")).toBeVisible();

  // Modified run: the form comes filled from the source.
  await page.goto(source);
  await page.getByRole("link", { name: "修改后运行" }).click();
  await expect(page.getByText("基于运行")).toBeVisible();
  await expect(page.getByLabel("运行名称")).toHaveValue("m5 source（修改）");

  // A plan, then two runs from it in one batch.
  await nav(page, "方案").click();
  await expect(page.getByText("smoke plan")).toBeVisible();
  await page.getByRole("link", { name: "用它新建运行" }).click();
  await expect(page.getByLabel("选用方案")).not.toHaveValue("");
  await page.getByLabel("运行名称").fill("batch one");
  await page.getByRole("button", { name: "加入批次草稿" }).click();
  await page.getByLabel("运行名称").fill("batch two");
  await page.getByRole("button", { name: "加入批次草稿" }).click();
  await page.getByLabel("批次名称").fill("smoke batch");
  await page.getByRole("button", { name: "预览" }).click();
  await expect(page.getByText("可以提交")).toBeVisible();
  await page.getByRole("button", { name: "提交批次" }).click();
  await expect(page).toHaveURL(/\/batches\/[^/]+$/);
  await expect(page.getByRole("heading", { name: "批次：smoke batch" })).toBeVisible();
  await expect(page.getByRole("link", { name: "batch one" })).toBeVisible();
  await expect(page.getByText("2 个运行：已结束 2")).toBeVisible({ timeout: 60_000 });
  await page.screenshot({ path: `${shots}/batch.png`, fullPage: true });
});

// N-06: publish a folder, then a changed copy based on it; only the changed file uploads.
test("attachment snapshots", async ({ page }) => {
  test.skip(!token, "MAESTRO_SETUP_TOKEN not set");
  const folder = join(mkdtempSync(join(tmpdir(), "maestro-smoke-")), "smoke-tools");
  mkdirSync(join(folder, "bin"), { recursive: true });
  const manifest = {
    schema_version: 1,
    name: "smoke-tools",
    effects: ["agent_tools"],
    compatible_harnesses: ["*"],
  };
  writeFileSync(join(folder, "manifest.json"), JSON.stringify(manifest));
  writeFileSync(join(folder, "bootstrap.sh"), "#!/bin/bash\necho installed\n");
  writeFileSync(join(folder, "bin", "run.sh"), "#!/bin/sh\necho one\n");

  await page.goto("/");
  await page.getByLabel("用户名").fill("admin");
  await page.getByLabel("密码").fill(password);
  await page.getByRole("button", { name: "登录" }).click();
  await nav(page, "附件").click();

  const choose = page.locator('input[type="file"]');
  await choose.setInputFiles(folder);
  await expect(page.getByText("附件 smoke-tools · 3 个文件")).toBeVisible();
  // A shebang makes a file executable by default.
  await expect(page.getByRole("checkbox", { name: /bootstrap\.sh/ })).toBeChecked();
  await expect(page.getByRole("checkbox", { name: /manifest\.json/ })).not.toBeChecked();
  await page.getByLabel("标签（可选）").fill("v1");
  await page.getByRole("button", { name: "上传并发布" }).click();
  await expect(page.getByText("已发布 smoke-tools v1（上传 3 个文件）")).toBeVisible();

  writeFileSync(join(folder, "bin", "run.sh"), "#!/bin/sh\necho two\n");
  await page.getByRole("button", { name: "基于此发布" }).click();
  await expect(page.getByText("将基于 smoke-tools v1 发布")).toBeVisible();
  await choose.setInputFiles(folder);
  await expect(page.getByLabel("父快照")).not.toHaveValue("");
  await page.getByLabel("标签（可选）").fill("v2");
  await page.getByRole("button", { name: "上传并发布" }).click();
  await expect(page.getByText("已发布 smoke-tools v2（上传 1 个文件）")).toBeVisible();
  await expect(page.getByText("基于 v1")).toBeVisible();

  // The new snapshot against its parent, and one file's content.
  await page.getByRole("button", { name: /smoke-tools v2/ }).click();
  await expect(page.getByText("~ bin/run.sh")).toBeVisible();
  await page.getByRole("button", { name: "查看" }).first().click();
  await expect(page.getByText("echo two")).toBeVisible();
  const download = page.waitForEvent("download");
  // The whole snapshot (the row's button), not one file.
  await page.locator('a[href$="/archive"]').last().click();
  expect((await download).suggestedFilename()).toBe("smoke-tools-v2.zip");
  await page.screenshot({ path: `${shots}/attachments.png`, fullPage: true });
});

// Comparisons keep their settings in shared URLs and their frozen evidence status
// in every layout, even when the underlying run has since been deleted.
const comparisonFixture = {
  comparison_version: 2, grouping_version: 2, comparable: true,
  blockers: [], warnings: [], set_aside: [], groups: [],
  settings: { exclude: [], scope: "common", baseline_run_id: "run_a",
    layout: { mode: "leaderboard", rows: null, columns: null, sort: null } },
  varying: ["model", "harness"],
  dimensions: [
    { key: "model", varies: true, paths: ["/model/model_id"],
      labels: { run_a: "M1", run_b: "M2", run_c: "M3" },
      values: { run_a: { model: { endpoint: "https://comparison.example/v1", model_id: "M1" } },
        run_b: { model: { endpoint: "https://comparison.example/v2", model_id: "M2" } } } },
    { key: "harness", varies: true, paths: ["/harness/name"],
      labels: { run_a: "H1", run_b: "H2", run_c: "H1" }, values: {} },
  ],
  runs: [0.2, 0.8, null].map((mean, i) => ({
    run_id: `run_${"abc"[i]}`, name: `run ${"abc"[i]}`, title: "ABC"[i], label: null,
    owner_username: "u", state: "finished", terminal: true, relation: "initial",
    accepted_at: "2026-10-22T00:00:00Z", evidence: "available",
    task_count: 1, planned_trials: 1, compared_trials: 1, valid_trials: mean == null ? 0 : 1,
    scored_trials: mean == null ? 0 : 1, passed_trials: 0, pass_rate: 0,
    failed_trials: 0, excluded_trials: 0, pending_trials: 0, mean, stddev: null,
    failed_by_group: {}, excluded_by_group: {}, actual_models: [], difference: null,
    usage: { known_trials: 0, known_totals: {} },
  })),
  tasks: [{ task_key: "one", status: "compared", cells: Object.fromEntries(
    ["a", "b", "c"].map((id) => [`run_${id}`, {
      planned_trials: 1, scored_trials: 1, valid_trials: 1, failed_trials: 0,
      excluded_trials: 0, pending_trials: 0, mean: 1, passed_trials: 1,
      failed_by_group: {}, excluded_by_group: {}, trial_ids: [`trl_${id}`], scores: [1],
    }]),
  ) }], common_tasks: 1, all_tasks: 1, dropped_tasks: 0,
};

test("comparison sorting survives URL changes and reloads", async ({ page }) => {
  await serveFixtures(page, sessionRoutes);
  await page.route("**/api/v1/comparisons", (route) => {
    const body = route.request().postDataJSON();
    return route.fulfill({ json: { meta, data: {
      ...comparisonFixture,
      settings: { ...comparisonFixture.settings, ...body },
    } } });
  });
  await page.goto("/compare?runs=run_a,run_b,run_c");
  const titles = page.getByRole("table").first().locator("tbody tr td:first-child > a");
  await expect(titles).toHaveText(["A", "B", "C"]);
  await page.getByRole("button", { name: "均分", exact: true }).click();
  await expect(page).toHaveURL(/sort=mean&descending=true/);
  await expect(titles).toHaveText(["B", "A", "C"]); // missing scores stay last
  await page.reload();
  await expect(titles).toHaveText(["B", "A", "C"]);
  await page.getByRole("button", { name: "均分 ↓", exact: true }).click();
  await expect(titles).toHaveText(["A", "B", "C"]);
});

for (const state of ["none", "deleted", "purged"]) {
  test(`snapshot ${state} evidence has no links in any layout`, async ({ page }) => {
    await serveFixtures(page, {
      ...sessionRoutes,
      "/api/v1/analysis-snapshots/snap_1": {
        id: "snap_1", name: "frozen", owner_username: "u", pat_id: null,
        created_at: "2026-10-22T00:00:00Z", run_count: 3, collection_id: null,
        spec: null, result: comparisonFixture, notes: [], allowed_actions: [], deletion: null,
        evidence: { run_a: "available", run_b: state, run_c: "available" },
      },
    });
    await page.goto("/snapshots/snap_1");
    await expect(page.getByRole("heading", { name: "快照：frozen" })).toBeVisible();
    const broken = page.locator('a[href^="/runs/run_b"]');
    await expect(broken).toHaveCount(0); // leaderboard and task cells
    await expect(page.locator('a[href^="/runs/run_a"]').first()).toBeVisible();
    await page.getByText("条件明细", { exact: true }).click();
    await page.getByText("模型", { exact: true }).last().click();
    await expect(page.getByText(/https:\/\/comparison.example\/v2/)).toBeVisible();
    await page.getByRole("button", { name: "矩阵", exact: true }).click();
    await expect(page.getByRole("columnheader", { name: /模型.*Harness/ })).toBeVisible();
    await expect(broken).toHaveCount(0); // matrix and task cells use current status too
  });
}

test("incomparable runs show raw summaries without comparison filters", async ({ page }) => {
  await serveFixtures(page, {
    ...sessionRoutes,
    "/api/v1/comparisons": { ...comparisonFixture, comparable: false, tasks: [],
      blockers: [{ code: "DATASET_DIFFERS", dimension: "dataset" }],
      settings: { ...comparisonFixture.settings, scope: "all" } },
  });
  await page.goto("/compare?runs=run_a,run_b");
  await expect(page.getByText("条件不同，显示各运行的原始汇总")).toBeVisible();
  await expect(page.getByRole("button", { name: "原始均分" })).toBeVisible();
  await expect(page.getByText(/失败剔除：/)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "共同任务", exact: true })).toHaveCount(0);
});

test("copying a collection edits fixed resources through the configuration form", async ({ page }) => {
  const collection = {
    id: "col_1", name: "one CPU", description: "baseline", scenario: "models",
    variable_dimensions: ["model"], fixed: { resources: { overrides: { cpus: 1 } } },
    fixed_labels: { resources: "cpus=1" }, owner_username: "u", run_count: 1,
    template: { dataset_version_id: "dsv_1", execution_mode: "agent",
      harness_version_id: "har_1", resources: { cpus: 1 }, repetitions: 1,
      timeouts: { verifier_sec: 30.5 } },
    members: [], allowed_actions: ["copy"], deletion: null,
    settings: comparisonFixture.settings, base_run_id: null, copied_from: null,
    created_at: "2026-10-22T00:00:00Z",
  };
  await serveFixtures(page, {
    ...sessionRoutes,
    "/api/v1/collections/col_1": collection,
    "/api/v1/collections/col_1/comparison": comparisonFixture,
    "/api/v1/collections/col_1/events": [], "/api/v1/analysis-snapshots": [],
    "/api/v1/plans": [], "/api/v1/credentials": [], "/api/v1/attachments": [],
    "/api/v1/datasets": [{ id: "ds_1", namespace: "demo", name: "arith", versions: [
      { id: "dsv_1", upstream_ref: "v1", status: "ready", created_at: "2026-10-22T00:00:00Z",
        task_count: 0, submittable_task_count: 0 },
    ] }],
    "/api/v1/dataset-versions/dsv_1": { task_count: 0, admitted_task_count: 0, submittable_task_count: 0 },
    "/api/v1/dataset-versions/dsv_1/tasks": [],
    "/api/v1/model-configs": [{ id: "mdl_1", name: "model", protocol: "openai", revision: 1 }],
    "/api/v1/harness-versions": [{ id: "har_1", name: "harness", version: "1", enabled: true,
      protocols: ["openai"], parameter_schema: { parameters: {} } }],
  });
  await page.route("**/api/v1/collections/col_1/copy", (route) => route.fulfill({ json: { meta,
    data: { ...collection, id: "col_2", name: "two CPUs", run_count: 0,
      not_carried: [{ run_id: "run_a", name: "old run", mismatches: [
        { dimension: "resources", path: "/overrides/cpus" },
      ] }] },
  } }));
  await page.goto("/collections/col_1");
  await page.getByRole("link", { name: "复制并修改规格" }).click();
  await expect(page.getByRole("heading", { name: "复制并修改集合规格" })).toBeVisible();
  await expect(page.locator("#run-cpus")).toHaveValue("1");
  await expect(page.locator("#run-cpus")).toBeEnabled();
  await expect(page.getByLabel("验证时限（秒）", { exact: true })).toHaveValue("30.5");
  await page.locator("#run-cpus").fill("2");
  await page.getByLabel("新集合名称").fill("two CPUs");
  const copied = page.waitForRequest((r) => r.url().endsWith("/collections/col_1/copy"));
  await page.getByRole("button", { name: "建立副本" }).click();
  const body = (await copied).postDataJSON();
  expect(body.base.request.resources.cpus).toBe(2);
  expect(body.base.request.timeouts.verifier_sec).toBe(30.5);
  await expect(page.getByText("old run：资源与时限不同")).toBeVisible();
  await expect(page.getByRole("link", { name: "打开新集合" })).toHaveAttribute("href", "/collections/col_2");
});

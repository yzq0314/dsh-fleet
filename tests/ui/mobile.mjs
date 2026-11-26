import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";
import { checkTopologyEnrollment } from "./topology.mjs";
const require = createRequire(
  new URL("../../apps/web-pwa/package.json", import.meta.url),
);
const { chromium } = require("playwright");
const state = process.env.FLEET_DEMO_JSON;
if (!state)
  throw Error(
    "Run python tests/ui/run.py to launch the dedicated demo cluster.",
  );
const { hub_url, password } = JSON.parse(await fs.readFile(state, "utf8"));
if (!["127.0.0.1", "localhost"].includes(new URL(hub_url).hostname))
  throw Error("Browser tests require an isolated loopback demo.");
const out = process.env.FLEET_UI_SCREENSHOTS || fileURLToPath(new URL("../screenshots/", import.meta.url));
await fs.mkdir(out, { recursive: true });
const launch = process.env.PLAYWRIGHT_CHANNEL
  ? { channel: process.env.PLAYWRIGHT_CHANNEL }
  : {};
let browser;
try {
  browser = await chromium.launch(launch);
} catch (e) {
  if (process.env.PLAYWRIGHT_CHANNEL) throw e;
  browser = await chromium.launch({ channel: "chrome" });
}
try {
  const ctx = await browser.newContext({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 2,
    isMobile: true,
    hasTouch: true,
    locale: "zh-CN",
    reducedMotion: "reduce",
  });
  const p = await ctx.newPage();
  const choice = name => p.locator('.choice[aria-label="' + name + '"]');
  async function pick(name,value){
    const control=choice(name), trigger=control.locator(".choice-trigger");
    if (await trigger.count()) {
      await trigger.tap();
      const menu = p.getByRole("listbox", {name, exact:true});
      await menu.locator('[data-option="'+value+'"]').tap();
      await menu.waitFor({state:"hidden"});
    } else {
      await control.locator('[data-option="'+value+'"]').tap();
    }
    assert.equal(await control.getAttribute("data-value"), value);
  }
  const errors = [];
  p.on("pageerror", (e) => errors.push(e.message));
  p.on("console", (m) => {
    if (m.type() === "error") {
      const source = m.location().url;
      const where = source && URL.canParse(source) ? new URL(source).pathname : "";
      errors.push([m.text(), where].filter(Boolean).join(" · "));
    }
  });
  p.setDefaultTimeout(12000);
  async function snap(name, { mask = [], fullPage = false } = {}) {
    assert.ok(
      await p.evaluate(
        () =>
          document.documentElement.scrollWidth <=
          document.documentElement.clientWidth,
      ),
      "Horizontal overflow on " + name,
    );
    if ((await p.viewportSize()).width <= 760) {
      const small = await p.evaluate(() => {
        const scope =
          Array.from(document.querySelectorAll("dialog[open]")).at(-1) ||
          document;
        return Array.from(
          scope.querySelectorAll(
            "button:not(:disabled), a[href], summary, select:not(:disabled), input:not(:disabled),textarea:not(:disabled)",
          ),
        )
          .map((el) =>
            el.matches("input[type=checkbox]") ? el.closest("label") || el : el,
          )
          .filter(
            (el) =>
              el.getClientRects().length > 0 &&
              getComputedStyle(el).visibility === "visible",
          )
          .map((el) => ({
            label:
              el.getAttribute("aria-label") ||
              el.textContent?.trim().slice(0, 24) ||
              el.tagName,
            box: el.getBoundingClientRect(),
          }))
          .filter(({ box }) => box.width < 44 || box.height < 44)
          .map(({ label, box }) => [label, box.width, box.height]);
      });
      assert.deepEqual(small, [], "Touch targets on " + name);
    }
    await p.screenshot({
      path: path.join(out, name + ".png"),
      mask,
      maskColor: "#526359",
      fullPage,
      animations: "disabled",
    });
  }
  const nav = (name) =>
    p
      .getByRole("navigation")
      .getByRole("button", { name, exact: true })
      .click();
  const button = (name) => p.getByRole("button", { name, exact: true });
  await p.goto(hub_url, { waitUntil: "domcontentloaded" });
  await p.getByLabel("管理员密码 / PIN").waitFor();
  await snap("01-login");
  await p.getByLabel("管理员密码 / PIN").fill(password);
  await button("登录").click();
  await p.getByRole("heading", { name: "Atlas", exact: true }).waitFor();
  await p.getByRole("heading", { name: "演示 NAS" }).waitFor();
  const topology = p.getByRole("region", { name: "设备拓扑" });
  await topology.getByRole("button", { name: "查看节点 Atlas", exact: true }).waitFor();
  for (const mode of ["下发", "回传", "全景"]) {
    const control = topology.getByRole("button", { name: mode, exact: true });
    await control.click();
    assert.equal(await control.getAttribute("aria-pressed"), "true");
  }
  await topology.scrollIntoViewIfNeeded();
  await snap("21-topology");
  const hardware=p.locator(".hardware-card");
  for(const [width,height,columns] of [[390,844,2],[1440,900,5],[820,900,3]]){
    await p.setViewportSize({width,height});
    await hardware.scrollIntoViewIfNeeded();
    assert.equal(await hardware.locator(".gpu").count(),10);
    assert.equal(await hardware.locator(".gpu-grid").evaluate(el=>getComputedStyle(el).gridTemplateColumns.split(" ").length),columns);
    assert.equal(await hardware.locator('.host-meters > [data-level="critical"]').count(),1);
    if(width!==820){
      const bell=p.getByRole("button",{name:"蹲守空闲 GPU 2",exact:true});
      await bell.click();
      const armed=p.getByRole("button",{name:"取消蹲守 GPU 2",exact:true});
      await armed.waitFor(); await snap("4b-watch-"+width); await armed.click(); await bell.waitFor();
    }
    await snap("4a-hardware-"+width);
  }
  await hardware.getByRole("button",{name:/^空闲 \d/}).click();
  assert.equal(await hardware.locator(".gpu").count(),1);
  await hardware.getByRole("button",{name:"全部 10",exact:true}).click();
  await p.setViewportSize({width:390,height:844});
  await snap("02-fleet");
  await p.getByRole("heading", { name: "演示 NAS" }).scrollIntoViewIfNeeded();
  await snap("02-services");
  await button("添加设备").click();
  await button("生成配对码").click();
  await p.getByText("一次性配对码", { exact: true }).waitFor();
  // Browser screenshot masks live outside the top layer of a native dialog.
  // Replace its two visible secret-bearing text nodes in this test document.
  const secrets = p.getByRole("dialog").locator(".pair-secret");
  assert.equal(await secrets.count(), 2);
  const pairCode = await secrets.first().textContent();
  assert.ok(pairCode && pairCode.length >= 16);
  // Emulate clipboard restrictions on a plain HTTP overlay origin.
  const command = await secrets.last().textContent();
  await p.evaluate(() => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    document.execCommand = (action) => action === "copy"
      && document.activeElement instanceof HTMLTextAreaElement
      && document.activeElement.selectionEnd === document.activeElement.value.length;
  });
  await button("复制接入命令").click();
  await button("已复制").waitFor();
  await p.evaluate(() => { document.execCommand = () => false; });
  await button("已复制").click();
  const manual = p.getByRole("textbox", { name: "复制接入命令的文本" });
  assert.ok(await manual.isVisible() && await manual.inputValue() === command);
  // Close the manual fallback before taking a shareable screenshot.
  await p.evaluate(() => { document.execCommand = () => true; });
  await button("复制接入命令").click();
  await button("已复制").waitFor();
  await p.evaluate(() => { delete document.execCommand; delete navigator.clipboard; });
  await secrets.evaluateAll((elements) => {
    elements[0].textContent = "〈一次性配对码已隐藏〉";
    elements[1].textContent = "〈含配对码的注册命令已隐藏〉";
  });
  assert.equal(
    (await p.getByRole("dialog").innerText()).includes(pairCode),
    false,
  );
  await snap("03-enrollment");
  await button("关闭弹窗").click();
  await checkTopologyEnrollment(p, topology, hub_url, pairCode, snap);
  await button("编辑 Atlas").click();
  await p.getByLabel("别名", { exact: true }).fill("Atlas Studio");
  await snap("04-node-edit", { mask: [p.getByLabel("节点地址")] });
  await button("保存节点").click();
  await p.getByRole("heading", { name: "Atlas Studio" }).waitFor();
  await button("打开 Atlas Studio 的 DSH 工作台").click();
  const station = p.getByRole("dialog", { name: "Atlas Studio · DSH 工作台" });
  await station.waitFor();
  assert.equal(await station.getByRole("link", { name: "打开 DSH Web" }).count(), 0);
  await snap("22-host-station");
  await station.getByRole("button", { name: "向本机派单" }).click();
  assert.equal(await choice("目标节点").getAttribute("data-value"), "demo-a");
  assert.equal(await choice("Agent").getAttribute("data-value"), "dsh");
  const handoff = p.getByRole("checkbox", { name: /在 DSH App 中续聊/ });
  assert.ok(await handoff.isChecked());
  await handoff.uncheck();
  assert.equal(await handoff.isChecked(), false);
  await button("仓库概览").click();
  await p
    .getByLabel("repo", { exact: true })
    .fill("demo-project [approval] [artifact]");
  await button("应用模板").click();
  await pick("派单模式","single");
  await pick("目标节点","demo-a");
  await pick("Agent","mock");
  await snap("05-dispatch");
  const streamUpdates = [];
  const cdp = await ctx.newCDPSession(p);
  await cdp.send("Network.enable");
  cdp.on("Network.eventSourceMessageReceived", (e) =>
    streamUpdates.push(e.data),
  );
  const dispatchResponse = p.waitForResponse(
    (r) =>
      new URL(r.url()).pathname === "/api/dispatch" &&
      r.request().method() === "POST",
  );
  await button("派发任务").click();
  const sent = await dispatchResponse;
  assert.equal(sent.status(), 200, "Dispatch failed: " + (await sent.text()));
  const approvalBanner = button("1 项操作等待审批");
  await approvalBanner.waitFor();
  await nav("设备");
  // The sample is waiting for approval after a real dispatch to the demo Sidecar.
  const dispatchBranch = topology.locator(".topo-branch").filter({
    has: p.getByRole("button", { name: "查看节点 Atlas Studio", exact: true }),
  });
  for (const direction of ["out", "back"]) {
    await dispatchBranch.locator(`.topo-pulse[data-direction="${direction}"][data-event-id]`).waitFor({ state: "attached" });
  }
  await topology.getByRole("button", { name: "查看节点 Atlas Studio", exact: true }).click();
  await p.getByRole("dialog").getByRole("button",{name:"关闭弹窗",exact:true}).click();
  await snap("23-topology-active");
  await nav("任务");
  if(await button("返回多栏").isVisible())await button("返回多栏").click();
  await button("待审批").click();await p.locator(".task-head").first().waitFor();await button("全部").click();
  await p.evaluate(() =>
    window.scrollTo(0, document.documentElement.scrollHeight),
  );
  const bannerBox = await approvalBanner.boundingBox();
  assert.ok(
    bannerBox && bannerBox.y >= 0 && bannerBox.y + bannerBox.height <= 844,
    "Approval remains reachable while scrolled",
  );
  await snap("05-pending-banner");
  await approvalBanner.click();
  await button("允许一次").waitFor();
  // Preserve field layout while removing the local runner's absolute path.
  await p.locator(".private-workspace").evaluate((el) => {
    el.textContent = "〈演示工作区〉 / demo-a / work";
  });
  await snap("06-approvals");
  await button("允许一次").click();
  await button("确认允许一次").click();
  await p.getByText("当前没有待审批请求").waitFor();
  await nav("任务");
  await p.locator(".task-head").filter({hasText:"Atlas Studio · 已完成"}).first().click();
  await button("终端").click();
  await p.locator(".console-line").first().waitFor();
  await button("对话").click();
  assert.ok(
    streamUpdates.length > 0,
    "A live SSE event must reach the browser",
  );
  await snap("07-tasks");
  await nav("设备");
  await button("打开 Birch 的 DSH 工作台").click();
  let workbench = p.getByRole("dialog", {name: "Birch · DSH 工作台"});
  assert.equal(await workbench.getByRole("button", {name: /demo-project/}).count(), 0);
  await workbench.getByRole("button", {name: "关闭弹窗"}).click();
  await button("打开 Atlas Studio 的 DSH 工作台").click();
  workbench = p.getByRole("dialog", {name: "Atlas Studio · DSH 工作台"});
  await workbench.getByRole("button", {name: /demo-project/}).click();
  await button("终端").waitFor();
  const outputs = p.getByRole("region", { name: "任务产物" });
  const summary = outputs.getByRole("button", {name:"任务产物", exact:true});
  await summary.waitFor();
  assert.equal(await summary.getAttribute("aria-expanded"), "false");
  for (const [width,height] of [[1440,900],[390,844]]) {
    await p.setViewportSize({width,height});
    await summary.scrollIntoViewIfNeeded();
    await snap("4c-"+width+"-artifacts-closed");
    await summary.tap();
    assert.ok(await outputs.locator("article").count() >= 2);
    await snap("4c-"+width+"-artifacts-open");
    await summary.tap();
    assert.equal(await outputs.locator("article").count(), 0);
  }
  await summary.tap();
  const artifact = (ext) =>
    outputs.locator("article").filter({
      has: p.locator(".artifact-head strong", {
        hasText: new RegExp("\\." + ext + "$"),
      }),
    });
  const openOutput = async (ext) => {
    const a = artifact(ext);
    await a.locator(".artifact-head").click();
    return a;
  };
  const md = await openOutput("md");
  await md.getByRole("heading", { name: "节点检查报告" }).waitFor();
  await md.scrollIntoViewIfNeeded();
  await snap("12-artifact-markdown");
  const csv = await openOutput("csv");
  await csv.getByRole("columnheader", { name: "检查项" }).waitFor();
  await csv.scrollIntoViewIfNeeded();
  await snap("13-artifact-csv");
  const downloadPromise = p.waitForEvent("download");
  await csv.getByRole("link", { name: "下载文件" }).click();
  const download = await downloadPromise;
  const bytes = await fs.readFile(await download.path());
  assert.ok(bytes.toString("utf8").includes("节点,检查项,状态"));
  await csv.getByRole("button", { name: "转存 NAS" }).click();
  await pick("目标设备","demo-b");
  await pick("目标可写卷","nas");
  await p.getByLabel("卷内保存路径").fill("check-summary.csv");
  await snap("14-artifact-transfer");
  await button("确认转存").click();
  await p.getByText("已转存至 Birch / nas / check-summary.csv").waitFor();
  const stored = await ctx.request.get(
    hub_url + "/api/nodes/demo-b/volumes/nas/download?path=check-summary.csv",
  );
  assert.deepEqual(await stored.body(), bytes);
  for (const ext of ["png", "svg"]) {
    const a = await openOutput(ext);
    await a.getByRole("button", { name: /放大图片/ }).click();
    const enlarged = p.getByRole("dialog");
    await enlarged.locator("img").waitFor();
    assert.ok(
      await enlarged.locator("img").evaluate(async (img) => {
        await img.decode();
        return img.naturalWidth > 0;
      }),
    );
    if (ext === "svg") await snap("15-artifact-image");
    await enlarged.getByRole("button", { name: "查看原始尺寸" }).click();
    await enlarged.getByRole("button", { name: "适应屏幕" }).click();
    await enlarged.getByRole("button", { name: "关闭弹窗" }).click();
  }
  await nav("文件");
  const nodeTrigger = choice("文件节点").locator(".choice-trigger");
  const previousNode = await nodeTrigger.innerText();
  const loadedVolumes = p.waitForResponse(r => new URL(r.url()).pathname === "/api/nodes/demo-b/volumes" && r.ok());
  await nodeTrigger.tap();
  const menu = p.getByRole("listbox", {name:"文件节点"});
  const birch = menu.getByRole("option", {name:"Birch",exact:true});
  assert.ok((await birch.boundingBox()).height >= 48);
  await snap("4c-390-file-node-sheet");
  await birch.tap();
  await loadedVolumes;
  assert.notEqual(await nodeTrigger.innerText(), previousNode);
  assert.equal((await nodeTrigger.innerText()).trim(), "Birch");
  const singleLine = await nodeTrigger.locator("span").evaluate(el => {
    const range = document.createRange(); range.selectNodeContents(el);
    return range.getClientRects().length === 1 && el.scrollWidth <= el.clientWidth;
  });
  assert.ok(singleLine, "Selected node must fit on one line");
  await pick("挂载卷","nas");
  await p.getByText("welcome.txt", { exact: true }).waitFor();
  let trackedImageRequests = 0;
  p.on("request", (req) => {
    if (req.url().includes("/preview-tracker-test")) trackedImageRequests++;
  });
  await p.getByLabel("上传文件", { exact: true }).setInputFiles({
    name: "preview-safety.md",
    mimeType: "text/markdown",
    buffer: Buffer.from(
      '# 预览测试\n\n![外部图片](/preview-tracker-test)\n\n<img src="/preview-tracker-test">\n\n<strong>隐藏标签</strong>',
    ),
  });
  await p.getByText("preview-safety.md", { exact: true }).click();
  await p
    .getByRole("dialog")
    .getByRole("heading", { name: "预览测试", exact: true })
    .waitFor();
  assert.equal(
    await p
      .getByRole("dialog")
      .locator(".markdown-preview img,.markdown-preview strong")
      .count(),
    0,
  );
  assert.equal(trackedImageRequests, 0);
  await button("关闭弹窗").click();
  const fixturePage = await ctx.newPage();
  await fixturePage.setViewportSize({ width: 160, height: 80 });
  await fixturePage.setContent(
    '<div style="height:60px;background:#64b889"></div>',
  );
  const jpegBytes = await fixturePage.screenshot({
    type: "jpeg",
    scale: "css",
  });
  await fixturePage.close();
  await p.getByLabel("上传文件", { exact: true }).setInputFiles({
    name: "sample-chart.jpg",
    mimeType: "image/jpeg",
    buffer: jpegBytes,
  });
  await p.getByText("sample-chart.jpg", { exact: true }).click();
  await p.getByRole("button", { name: "放大图片 sample-chart.jpg" }).click();
  assert.ok(
    await p.locator(".image-pan img").evaluate(async (img) => {
      await img.decode();
      return img.naturalWidth === 160;
    }),
  );
  await p
    .getByRole("dialog")
    .last()
    .getByRole("button", { name: "关闭弹窗" })
    .click();
  await button("关闭弹窗").click();
  await p.getByLabel("上传文件", { exact: true }).setInputFiles({
    name: "mobile-notes.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(
      'node,status\r\nBirch,"ready, verified\nnext"\r\n',
      "utf8",
    ),
  });
  await p.getByText("mobile-notes.csv", { exact: true }).waitFor();
  await snap("08-files");
  await p.getByText("mobile-notes.csv", { exact: true }).click();
  await p
    .getByRole("dialog")
    .getByRole("cell", { name: "ready, verified next" })
    .waitFor();
  await snap("09-preview");
  await button("交给 DSH 处理").click();
  await p.getByText("nas/mobile-notes.csv", { exact: true }).waitFor();
  assert.equal(
    await choice("Agent").getAttribute("data-value"),
    "dsh",
  );
  await pick("派单模式","auto");
  await p.getByLabel("标签过滤").fill("nas");
  await pick("Agent","mock");
  await p.getByLabel("任务指令").fill("核对上传的模拟附件");
  await p.getByLabel("添加任务附件").setInputFiles({
    name: "attachment.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("Fleet mobile attachment"),
  });
  await pick("附件目标卷","nas");
  const upload = p.waitForResponse(
    (r) =>
      new URL(r.url()).pathname === "/api/nodes/demo-b/volumes/nas/upload" &&
      r.request().method() === "PUT",
  );
  await button("派发任务").click();
  assert.equal((await upload).status(), 200);
  await p.locator(".detail-title .badge").filter({hasText:"已完成"}).waitFor();
  await nav("审批设置");
  await button("外观与皮肤").click(); await button("午夜蓝").click();
  await button("保存外观").click();
  await p.waitForFunction(
    () => document.documentElement.dataset.theme === "midnight",
  );
  await button("安全与审批").click();
  await p.waitForFunction(() => !!document.querySelector("textarea")?.value);
  await snap("10-settings");
  await p.getByLabel("高危拦截规则（每行一条正则）").scrollIntoViewIfNeeded();
  await snap("10-rules");
  const modeSelect = choice("审批模式");
  assert.equal(await modeSelect.getAttribute("data-value"), "balanced");
  const policyUrl = hub_url + "/api/nodes/demo-a/policy";
  await pick("审批模式","strict_hash");
  await button("保存规则").click();
  await button("已保存规则").waitFor();
  assert.equal(
    (await (await ctx.request.get(policyUrl)).json()).mode,
    "strict_hash",
  );
  await pick("审批模式","autonomous");
  await button("保存规则").click();
  await p.getByRole("dialog", { name: "启用自主执行" }).waitFor();
  await button("返回检查").click();
  assert.equal(
    (await (await ctx.request.get(policyUrl)).json()).mode,
    "strict_hash",
  );
  await button("保存规则").click();
  await snap("16-policy-confirm");
  await button("确认启用自主执行").click();
  await button("已保存规则").waitFor();
  assert.equal(
    (await (await ctx.request.get(policyUrl)).json()).mode,
    "autonomous",
  );
  await pick("审批模式","balanced");
  await button("保存规则").click();
  await button("已保存规则").waitFor();
  const desktop = p.getByRole("region", { name: "桌面插件状态" });
  await desktop.getByText("插件未连接").waitFor();
  assert.equal(await desktop.locator("details").getAttribute("open"),null);
  await desktop.getByText("查看安装命令",{exact:true}).tap();
  assert.match(await desktop.locator("pre").innerText(), /manage\.py install --node-config/);
  const desktopKey = process.env.FLEET_TEST_DESKTOP_KEY;
  assert.ok(
    desktopKey,
    "Use tests/ui/run.py to provision a temporary plugin identity",
  );
  const heartbeat = await ctx.request.post(
    hub_url + "/api/desktop/demo-a/heartbeat",
    {
      headers: { Authorization: "Bearer " + desktopKey },
      data: { version: "0.2.0" },
    },
  );
  assert.equal(heartbeat.status(), 200);
  await desktop.getByText("插件已连接").waitFor();
  await desktop.getByText("0.2.0", { exact: true }).waitFor();
  await desktop.scrollIntoViewIfNeeded();
  await snap("17-desktop-plugin");
  // Drive a shared desktop session through the same scoped HTTP protocol as a plugin.
  const peerPost = (suffix,data={})=>p.request.post(hub_url+"/api/desktop/demo-a/"+suffix,{
    headers:{Authorization:"Bearer "+desktopKey},data});
  const sid="mobile-ui-conversation";
  let seq=0;
  const postEvent = async(type,data)=>{
    const res=await peerPost("events",{session_id:sid,event_id:"chat-"+(++seq),type,data});
    assert.equal(res.status(),200);
    return res.json();
  };
  await peerPost("turns");
  const first=await postEvent("status",{status:"running",title:"验收会话"});
  await postEvent("message",{text:"欢迎回来"});
  await postEvent("status",{status:"completed"});
  await nav("任务");
  if(await button("返回多栏").isVisible())await button("返回多栏").click();
  await p.getByRole("button",{name:/^验收会话/}).click();
  await p.getByLabel("继续此 DSH 会话").fill("继续刚才的话题");
  await p.getByLabel("继续此 DSH 会话").press("Control+Enter");
  await p.getByText("已发送到 Hub，等待桌面接收…").waitFor();
  const [turn]=await (await peerPost("turns")).json();
  assert.equal(turn.native_id,sid);
  assert.equal(turn.task_id,first.task_id);
  await peerPost("turns/"+turn.id,{state:"accepted"});
  await postEvent("user_message",{text:turn.prompt,request_id:turn.id});
  await postEvent("message",{text:"我还记得上一轮"});
  await postEvent("result",{text:"我还记得上一轮"});
  await postEvent("status",{status:"completed"});
  await p.locator(".conversation").getByText("我还记得上一轮",{exact:true}).waitFor();
  assert.equal(await p.getByText("继续刚才的话题",{exact:true}).count(),1);
  await snap("24-conversation");
  await p.getByLabel("项目分组",{exact:true}).fill("移动端验收");
  await button("保存分组").click();
  await button("返回多栏").click();
  await pick("归类","folder");
  await p.getByRole("heading",{name:/移动端验收/}).waitFor();
  await p.getByRole("button",{name:/^验收会话/}).click();
  await button("移到最近删除").click();
  await p.getByRole("button",{name:/^验收会话/}).waitFor({state:"hidden"});
  await button("最近删除").click();
  await p.getByRole("button",{name:/^验收会话/}).click();
  await button("恢复到队列").click();
  await button("返回任务队列").click();
  await p.getByRole("button",{name:/^验收会话/}).waitFor();
  await p.evaluate(()=>{
    const original=window.fetch;
    window.fetch=(input,init)=>{
      if(String(input).endsWith("/api/overview")) {
        window.fetch=original;
        return Promise.reject(new TypeError("Failed to fetch"));
      }
      return original(input,init);
    };
  });
  await postEvent("log",{text:"connection recovery fixture"});
  const retry=p.getByText(/正在自动重连/);
  await retry.waitFor();
  await retry.waitFor({state:"hidden",timeout:20000});
  await p.reload({ waitUntil: "domcontentloaded" });
  await p.getByRole("heading", { name: "Atlas Studio" }).waitFor();
  assert.equal(await p.locator("html").getAttribute("data-theme"), "midnight");
  await p.evaluate(async () => {
    const reg = await navigator.serviceWorker.ready;
    if (!reg.active) throw Error("Service worker inactive");
    const names = await caches.keys();
    const keys = (
      await Promise.all(
        names.map(async (n) =>
          (await (await caches.open(n)).keys()).map(
            (r) => new URL(r.url).pathname,
          ),
        ),
      )
    ).flat();
    if (
      !keys.includes("/index.html") ||
      keys.some(
        (x) =>
          ![
            "/index.html",
            "/manifest.json",
            "/icon.svg",
            "/icon-192.png",
            "/icon-512.png",
          ].includes(x) && !/^\/assets\/index-[\w-]+\.(js|css)$/.test(x),
      )
    )
      throw Error("Invalid offline cache");
  });
  await p.setViewportSize({ width: 1440, height: 900 });
  await p.getByRole("heading", { name: "Atlas Studio" }).waitFor();
  await snap("11-desktop");
  async function gallery(width,height) {
    await p.setViewportSize({width,height});
    const shot=name=>snap("4a2-"+width+"-"+name,{fullPage:true});
    await nav("设备");await p.locator(".gpu").first().waitFor();await shot("fleet");
    await nav("派单");await shot("dispatch");
    if(width===1440){
      assert.ok((await button("派发任务").boundingBox()).width<=140);
      await p.locator(".candidate").filter({hasText:"Birch"}).click();
      assert.equal(await choice("目标节点").getAttribute("data-value"),"demo-b");
    }
    await nav("任务");if(await button("返回多栏").isVisible())await button("返回多栏").click();
    await shot("queue");await button("多选").click();await p.getByRole("checkbox").first().click();await shot("multi");await button("退出多选").click();
    await p.locator(".task-head").filter({hasText:"验收会话"}).click();await p.locator(".chat-bubble").first().waitFor();await shot("conversation");
    await button("终端").click();await shot("terminal");await button("返回多栏").click();
    await nav("文件");await pick("文件节点","demo-b");await pick("挂载卷","nas");await p.getByText("welcome.txt",{exact:true}).waitFor();await p.getByLabel("搜索文件").fill("welcome");await p.getByRole("button",{name:"搜索",exact:true}).click();await p.getByRole("button",{name:"清空搜索"}).click();await p.getByText("welcome.txt",{exact:true}).waitFor();await shot("files");await snap("4c-"+width+"-files",{fullPage:true});
    await nav("审批设置");await p.waitForFunction(()=>!!document.querySelector(".settings-grid textarea")?.value);await shot("security");
    async function inspectColumns(name, grid) {
      await snap("4c-"+width+"-"+name, {fullPage:true});
      if(width >= 1100){
        const boxes = await grid.locator(":scope > *").evaluateAll(cols => cols.map(c => {
          const r=c.getBoundingClientRect(); return {top:r.top,height:r.height};
        }));
        assert.ok(Math.abs(boxes[0].top-boxes[1].top)<2);
        console.log(name+" column heights: "+boxes.map(b=>Math.round(b.height)).join(", "));
      }
    }
    await inspectColumns("security", p.locator(".settings-grid:visible").first());
    const toggle=p.getByRole("button",{name:"节点离线",exact:true});
    const before=await toggle.getAttribute("aria-pressed");
    await toggle.click(); await p.getByRole("button",{name:"保存通知策略"}).click();
    await p.getByText("已保存",{exact:true}).first().waitFor();
    assert.notEqual(await toggle.getAttribute("aria-pressed"),before);
    await snap("4b-notifications-"+width);
    await button("外观与皮肤").click();await shot("skin");await inspectColumns("skin",p.locator(".appearance-editor .settings-grid:visible"));await button("布局与快捷入口").click();await shot("layout");
  }
  await gallery(390,844);await gallery(1440,900);
  await nav("派单");await snap("4a-desktop-dispatch");
  await nav("审批设置");await snap("4a-desktop-settings");
  await button("外观与皮肤").click();await button("暖白纸感").click();await button("保存外观").click();
  await p.waitForFunction(()=>document.documentElement.dataset.theme==="paper");
  await button("布局与快捷入口").click();
  await pick("布局设备","mobile");
  await p.locator(".layout-row").filter({hasText:"快捷服务"}).getByRole("button",{name:"已显示"}).click();
  await button("保存布局").click();
  await p.getByText("已同步到 Hub").waitFor();
  const phone=await browser.newContext({viewport:{width:390,height:844},isMobile:true});
  try {
    const other=await phone.newPage();
    other.on("console",m=>{if(m.type()==="error")errors.push(m.text());});
    other.on("pageerror",e=>errors.push(e.message));
    assert.ok((await phone.request.post(hub_url+"/api/login",{data:{password}})).ok());
    await other.goto(hub_url);await other.locator("[data-home-section=devices]").waitFor();
    assert.equal(await other.locator("html").getAttribute("data-theme"),"paper");
    assert.equal(await other.locator("[data-home-section=services]").count(),0);
    await other.goto(hub_url+"/?reset_theme=1");await other.locator("[data-home-section=services]").waitFor();
    assert.equal(await other.locator("html").getAttribute("data-theme"),"industrial");
  } finally {await phone.close();}
  await nav("任务");
  await button("已完成").click();await button("多选").click();await button("全选").click();
  await p.getByRole("button",{name:/^移入最近删除 \(/}).click();await p.getByText("没有匹配的任务").waitFor();
  await button("最近删除").click();await p.locator(".task-head").first().waitFor();
  await p.goto(hub_url);await p.locator("[data-home-section=devices]").waitFor();
  assert.deepEqual(
    errors,
    [],
    "Online browser flow must remain free of console and page errors",
  );
  // Navigate offline from a clean document. The SW must serve the shell itself.
  await p.goto("about:blank");
  await ctx.setOffline(true);
  await p.goto(hub_url, { waitUntil: "domcontentloaded" });
  await p.getByText("当前离线，联网后会重新连接控制中枢。").waitFor();
  assert.equal(
    await p.getByRole("heading", { name: "Atlas Studio" }).count(),
    0,
  );
  await ctx.setOffline(false);
  await p.getByRole("heading", { name: "Atlas Studio" }).waitFor();
  assert.deepEqual(errors, []);
  console.log(
    "Passed mobile dispatch, approval modes, desktop heartbeat, file previews, downloads, NAS copies, live SSE, touch targets, offline navigation and zero browser errors.",
  );
} finally {
  await browser.close();
}

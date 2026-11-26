import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import {createRequire} from "node:module";

const require = createRequire(new URL("../../apps/web-pwa/package.json", import.meta.url));
const {chromium} = require("playwright");
const {hub_url, password, api_key} = JSON.parse(await fs.readFile(process.env.FLEET_DEMO_JSON, "utf8"));
assert.equal(new URL(hub_url).hostname, "127.0.0.1");
const output = process.env.FLEET_GUIDE_IMAGES;
if (output) await fs.mkdir(output, {recursive:true});
const browser = await chromium.launch(process.env.PLAYWRIGHT_CHANNEL ? {channel:process.env.PLAYWRIGHT_CHANNEL} : {})
  .catch(() => chromium.launch({channel:"chrome"}));
const errors = [];
const nav = (page, name) => page.getByRole("navigation").getByRole("button", {name, exact:true}).click();
async function pageFor(mobile) {
  const ctx = await browser.newContext({viewport:mobile ? {width:390,height:844} : {width:1440,height:900},
    isMobile:mobile, hasTouch:mobile, locale:"zh-CN", reducedMotion:"reduce", serviceWorkers:"block"});
  const page = await ctx.newPage();
  page.on("pageerror", e => errors.push(e.message));
  page.on("console", m => {if(m.type()==="error") errors.push(m.text());});
  // Use stable, fictional telemetry and display paths for the public guide.
  await page.route("**/api/overview", async route => {
    const res = await route.fetch();
    const data = await res.json();
    data.nodes.forEach((n,i) => {
      n.rtt=[3,5,4][i]; n.health={...n.health,hostname:n.alias,os:"Linux",cpu:12+i*8,
        ram:26+i*6, gpu:[0,35,7][i], workspaces:["/workspace/demo"]};
    });
    data.services.forEach(s => {s.name="Demo NAS";});
    await route.fulfill({response:res,json:data});
  });
  await page.goto(hub_url);
  return page;
}
async function shot(page, name) {
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth), "Overflow on "+name);
  if (!output) return;
  const text = await page.locator("body").innerText();
  assert.ok(!text.includes(password) && !text.includes(api_key));
  assert.doesNotMatch(text, /[A-Z]:[\\/]|\/Users\/|\/home\//i);
  await page.screenshot({path:path.join(output,name+".png"),animations:"disabled"});
}
try {
  const desktop = await pageFor(false);
  await desktop.getByRole("button",{name:"English",exact:true}).click();
  await desktop.getByLabel("Admin password / PIN").fill(password);
  await desktop.getByRole("button",{name:"Sign in",exact:true}).click();
  await desktop.getByRole("heading",{name:"Device topology"}).waitFor();
  await shot(desktop,"fleet-en");
  await nav(desktop,"Dispatch");
  await desktop.getByText("Repository overview",{exact:true}).waitFor();
  await shot(desktop,"dispatch-en");
  await nav(desktop,"Tasks");
  await desktop.getByRole("heading",{name:"Task queue"}).waitFor();
  await nav(desktop,"Settings");
  await desktop.getByRole("button",{name:"Appearance",exact:true}).click();
  await desktop.getByRole("button",{name:"Warm paper"}).waitFor();
  const mobile = await pageFor(true);
  await mobile.getByLabel("管理员密码 / PIN").fill(password);
  await mobile.getByRole("button",{name:"登录",exact:true}).click();
  await mobile.getByRole("heading",{name:"Fleet overview"}).waitFor();
  await nav(mobile,"Files");
  async function openVolume(nodeLabel,volLabel) {
    const node=mobile.getByRole("group",{name:nodeLabel,exact:true});
    await node.locator(".choice-trigger").tap();
    await mobile.getByRole("listbox").getByRole("option",{name:"Birch",exact:true}).tap();
    const vol=mobile.getByRole("group",{name:volLabel,exact:true});
    if(await vol.locator(".choice-trigger").count()) {
      await vol.locator(".choice-trigger").tap();
      await mobile.getByRole("listbox").getByRole("option",{name:"nas",exact:true}).tap();
    } else await vol.getByRole("button",{name:"nas",exact:true}).tap();
    await mobile.getByText("welcome.txt",{exact:true}).waitFor();
  }
  await openVolume("File node","Volume");
  await shot(mobile,"files-en");
  await nav(mobile,"Settings");
  await mobile.getByRole("group",{name:"Language",exact:true}).getByRole("button",{name:"简体中文",exact:true}).tap();
  await mobile.waitForFunction(()=>document.documentElement.lang==="zh-CN");
  await nav(mobile,"文件");
  await openVolume("文件节点","挂载卷");
  await shot(mobile,"files-zh");
  await desktop.reload();
  await desktop.getByRole("heading",{name:"设备拓扑"}).waitFor();
  await shot(desktop,"fleet-zh");
  await nav(desktop,"派单");
  await desktop.getByText("仓库概览",{exact:true}).waitFor();
  await shot(desktop,"dispatch-zh");
  assert.deepEqual(errors,[]);
  console.log("Bilingual login, five tabs, Hub preference sync, mobile volume selection and screenshots passed.");
} finally { await browser.close(); }

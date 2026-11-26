import {createRequire} from "node:module";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
const require=createRequire(new URL("../../apps/web-pwa/package.json",import.meta.url));
const {chromium}=require("playwright");
const opts={args:["--no-proxy-server","--host-resolver-rules=MAP *.fleet.test 127.0.0.1"]};
let browser;
try {browser=await chromium.launch(opts);} catch {browser=await chromium.launch({...opts,channel:"chrome"});}
try {
  for(const [width,height] of [[390,844],[1440,900]]){
    const ctx=await browser.newContext({viewport:{width,height}});
    const page=await ctx.newPage();
    const errors=[];
    page.on("pageerror",e=>errors.push(e.message));
    page.on("console",m=>{if(m.type()==="error")errors.push(m.text());});
    await page.goto("http://console.fleet.test:"+process.env.FLEET_FIXTURE_PORT+"/");
    await page.locator('input[type=password]').fill(process.env.FLEET_FIXTURE_PASSWORD);
    await page.getByRole("button",{name:"登录",exact:true}).click();
    await page.getByRole("button",{name:"进入 DSH 实时同屏",exact:true}).first().click();
    const frame=page.frameLocator(".pocket-view iframe");
    await frame.locator("output").filter({hasText:"Alpha"}).waitFor();
    await page.locator('.pocket-view .choice-trigger').click();
    await page.getByRole("option",{name:"Beta"}).click();
    await frame.locator("output").filter({hasText:"Beta"}).waitFor();
    await page.getByLabel("刷新同屏").click();
    await frame.locator("output").filter({hasText:"Beta"}).waitFor();
    await page.getByLabel("折叠同屏顶栏").click(); await page.getByLabel("展开同屏顶栏").click();
    assert(await page.evaluate(()=>document.documentElement.clientWidth>=document.documentElement.scrollWidth));
    const out=process.env.FLEET_UI_SCREENSHOTS; await fs.mkdir(out,{recursive:true});
    await page.screenshot({path:path.join(out,"4b-pocket-"+width+".png")});
    await page.getByLabel("返回 Fleet").click();
    assert.deepEqual(errors,[]);
    await ctx.close();
  }
  console.log("Two isolated Pocket hosts passed HTTP, WebSocket echo and switching on both viewports");
} finally {await browser.close();}

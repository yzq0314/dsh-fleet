import assert from "node:assert/strict";
import {createServer} from "vite";
import React from "react";
import {renderToStaticMarkup} from "react-dom/server";
const server=await createServer({optimizeDeps:{noDiscovery:true,include:[]},appType:"custom",server:{middlewareMode:true,hmr:false}});
try {
  const {cleanMessage}=await server.ssrLoadModule("/src/cleanMessage.ts");
  assert.equal(cleanMessage("hello<system-reminder>private\nmeta</system-reminder> world").replace(/\s+/g," "),"hello world");
  assert.equal(cleanMessage("<available_skills>\nlist\n</available_skills>\nBrowser time zone for this request UTC\nTime sampled while preparing turn 2\nElapsed since the preceding turn 1s\nanswer"),"answer");
  const fenced="~~~xml\n<system-reminder>example</system-reminder>\n~~~";
  assert.equal(cleanMessage(fenced),fenced);
  const {SafeMarkdown}=await server.ssrLoadModule("/src/FilePreview.tsx");
  const html=renderToStaticMarkup(React.createElement(SafeMarkdown,{text:"# Heading\n\n~~~js\nprint(1)\n~~~\n![test](https://example.invalid/a)\n<script>alert(1)</script>"}));
  assert.match(html,/<h1>Heading/);assert.match(html,/<pre><code/);assert.doesNotMatch(html,/<script|<img|example.invalid/);
  const {defaults,exportTheme,importTheme,foreground}=await server.ssrLoadModule("/src/theme.ts");
  assert.deepEqual(importTheme(exportTheme(defaults)),defaults);
  assert.equal(foreground("#ffffff"),"#000000");assert.equal(foreground("#000000"),"#ffffff");
  for(const obj of [{motion:"true"},{css:"background:url(x)"},{radius:17},{layout:{mobile:["devices","devices"],desktop:[]}}])
    assert.throws(()=>importTheme(exportTheme({...defaults,...obj})));
  const {orderMacros}=await server.ssrLoadModule("/src/macroOrder.ts");
  const macros=[{position:0},{position:1,target_tags:["other"]},{position:2,target_tags:["storage"]}];
  assert.deepEqual(orderMacros(macros,new Set(["storage"]),false),[macros[2],macros[0],macros[1]]);
  assert.deepEqual(orderMacros(macros,new Set(["storage"]),true),macros);
  const {MonitorCard}=await server.ssrLoadModule("/src/MonitorCard.tsx");
  const monitor={name:"Compute Lab",fresh:true,timestamp:1,cpu:1.5,ram:80,disk:100,free_gib:1.69,
    gpu:[{used_mib:21811,total_mib:24576,utilization:96},{used_mib:512,total_mib:24576,utilization:1}]};
  const telemetry=renderToStaticMarkup(React.createElement(MonitorCard,{monitor}));
  for(const value of ["21.3 / 24.0 GiB","21811 / 24576 MiB","空间不足","满载","1.5%","1 卡空闲"]) assert.ok(telemetry.includes(value),value);
  const {uiText, setLocale} = await server.ssrLoadModule("/src/i18n.ts");
  const {displayMacro} = await server.ssrLoadModule("/src/builtinMacros.ts");
  setLocale("en");
  assert.equal(uiText("任务产物"), "Task artifacts");
  assert.equal(uiText("在 {0} 的 DSH App 中打开「{1}」。", "Birch", "报告 {0}"), "On Birch, open “报告 {0}” in DSH Desktop.");
  const edited = {name:"仓库概览",template:"我自己的说明"};
  assert.equal(displayMacro(edited),edited);
  assert.equal(displayMacro({name:"仓库概览",template:"概述 {{repo}} 的目录结构和主要入口。"}).name,"Repository overview");
  setLocale("zh-CN");
  assert.equal(uiText("任务产物"), "任务产物");
  console.log("Conversation, Markdown and theme tests passed");
} finally {await server.close();}

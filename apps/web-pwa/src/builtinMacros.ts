import { uiText } from "./i18n";
import type { Macro } from "./model";

// Only unchanged starter templates have translated copy. Keep custom macros verbatim.
const starters = new Map([
  ["仓库概览", "概述 {{repo}} 的目录结构和主要入口。"],
  ["分支审阅", "审阅 {{branch}} 的改动，总结需要人工核对的问题。"],
  ["日志摘要", "阅读 {{file}}，按时间归纳关键事件。"],
  ["测试计划", "为 {{module}} 拟定本地测试计划和验收条件。"],
]);
export function displayMacro(macro: Macro): Macro {
  return starters.get(macro.name) === macro.template
    ? { ...macro, name: uiText(macro.name), template: uiText(macro.template) } : macro;
}

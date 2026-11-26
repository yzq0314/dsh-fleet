import english from "./locales/en.json";

export const translations = {
  "zh-CN": { languageName: "简体中文" },
  en: { languageName: "English" },
} as const;
export type Locale = keyof typeof translations;
const storageKey = "fleet.language";
const listeners = new Set<() => void>();
function normalize(value: unknown): Locale {
  return value === "en" || value === "en-US" ? "en" : "zh-CN";
}
function initialLocale(): Locale {
  try { return normalize(localStorage.getItem(storageKey)); }
  catch { return "zh-CN"; }
}
let locale = initialLocale();
export const currentLocale = () => locale;
export function subscribeLocale(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
export function setLocale(value: string) {
  const next = normalize(value);
  if (typeof document !== "undefined") document.documentElement.lang = next;
  if (next === locale) return;
  locale = next;
  try { localStorage.setItem(storageKey, next); } catch { /* Storage may be unavailable. */ }
  for (const listener of listeners) listener();
}
// Pass only application-owned messages. User text, paths and event payloads stay verbatim.
export function uiText(source: string, ...args: (string | number)[]): string {
  const text = locale === "en" && Object.hasOwn(english, source)
    ? english[source as keyof typeof english] : source;
  return args.length ? text.replace(/\{(\d+)\}/g, (match, index) => String(args[Number(index)] ?? match)) : text;
}
const pages = {
  fleet: ["设备总览", "Fleet overview"], dispatch: ["任务派发", "Dispatch tasks"],
  tasks: ["任务记录", "Task activity"], files: ["文件与 NAS", "Files and NAS"],
  settings: ["审批与设置", "Approvals and settings"],
} as const;
export function pageTitle(tab: keyof typeof pages) { return pages[tab][locale === "en" ? 1 : 0]; }
export function pageCaption(tab: keyof typeof pages) { return locale === "en" ? "DSH Fleet" : pages[tab][1]; }

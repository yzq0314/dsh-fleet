import { uiText } from "./i18n";
export type Tab = "fleet" | "dispatch" | "tasks" | "files" | "settings";
export type Mode = "single" | "auto" | "fanout";
export type NodeInfo = {
  id: string;
  alias: string;
  url: string;
  dsh_web_port?: number | null;
  dsh_web_scheme?: "http" | "https";
  pocket_embed?: boolean;
  dsh_web_url?: string | null;
  tags: string[];
  icon?: string;
  enabled?: boolean;
  online: boolean;
  rtt?: number;
  mac?: string;
  broadcast?: string;
  wake_proxy?: string;
  health?: {
    pocket_status?: "ready" | "stopped" | "unconfigured";
    pocket_port?: number | null;
    pocket_hint?: string;
    pocket_lan_auth_enabled?: boolean | null;
    agent?: string;
    capabilities?: string[];
    volumes?: { name: string; read_only: boolean; available?: boolean }[];
    cpu?: number;
    ram?: number;
    gpu?: any;
    active_tasks?: number;
    workspaces?: string[];
    os?: string;
  };
};
export type HardwareMonitor = {watching?: number[]; name:string; timestamp:number; fresh:boolean; cpu:number|null; ram:number; disk:number; free_gib:number; gpu:{utilization:number; used_mib:number; total_mib:number}[]};
export type Service = {
  id: string;
  name: string;
  url: string;
  node_id?: string;
  volume?: string;
  online?: boolean | null;
};
export type Macro = {
  id: string;
  name: string;
  template: string;
  position: number;
  target_tags?: string[];
};
export type Task = {
  created?: number;
  folder?: string;
  chat_online?: boolean;
  removed?: boolean;
  id: string;
  group_id: string;
  node_id: string;
  origin?: string;
  prompt: string;
  workspace: string;
  status: string;
  result?: any;
};
export type Approval = {
  decisions?: string[];
  id: string;
  task_id: string;
  node_id: string;
  reason: string;
  command: string;
  workspace: string;
};
export type TaskEvent = {
  id: number;
  type: string;
  data: any;
  task_id?: string;
};
export type Settings = {
  theme: string;
  accent: string;
  language: string;
  default_route: Mode;
};
export type FileEntry = {
  name: string;
  path: string;
  is_dir: boolean;
  size: number;
};
export type Context = { volume: string; path: string };
export const initialSettings: Settings = {
  theme: "industrial",
  accent: "mint",
  language: "zh-CN",
  default_route: "single",
};
export async function api<T>(
  url: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  const response = await fetch("/api" + url, {
    method,
    credentials: "same-origin",
    cache: "no-store",
    headers:
      body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body === undefined ? undefined : JSON.stringify(body),
  }).catch((e: unknown) => {
    if (!(e instanceof TypeError)) throw e;
    throw new Error(method === "GET" ? uiText("暂时连不上 Fleet Hub，请检查内网连接。")
      : uiText("未收到 Hub 的确认，请先查看当前状态。"));
  });
  if (!response.ok) {
    if (response.status === 401 && url !== "/login")
      window.dispatchEvent(new Event("fleet:expired"));
    let info = "";
    try {
      const d = await response.json();
      info =
        typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail ?? d);
    } catch {}
    throw Error(info || uiText("请求失败 (") + response.status + ")");
  }
  return response.status === 204 ? (undefined as T) : response.json();
}
export function enc(s: string) {
  return encodeURIComponent(s);
}
export function safeWebURL(value: string) {
  try {
    const u = new URL(value);
    return ["http:", "https:"].includes(u.protocol) ? u.href : undefined;
  } catch {
    return undefined;
  }
}
export function errorText(e: unknown) {
  return e instanceof Error ? e.message : String(e);
}
export type Run = (f: () => Promise<void>) => void;
export function display(v: unknown) {
  return typeof v === "string" ? v : (JSON.stringify(v, null, 2) ?? "");
}
export function pct(v: unknown) {
  const n =
    typeof v === "number"
      ? v
      : typeof v === "object" && v !== null
        ? Number((v as any).percent)
        : NaN;
  return Number.isFinite(n) ? Math.max(0, Math.min(100, n)) : null;
}

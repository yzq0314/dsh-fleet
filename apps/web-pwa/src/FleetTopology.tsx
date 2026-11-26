import { uiText } from "./i18n";
import { useEffect, useId, useState } from "react";
import {
  ArrowDownLeft, ArrowUpRight, Cpu, GitFork, HardDrive, Laptop,
  Monitor, Radio, Server, SquareTerminal, type LucideIcon,
} from "lucide-react";
import type { Approval, NodeInfo, Service, Task } from "./model";
import "./fleet-topology.css";

type Direction = "out" | "back";
type View = "all" | Direction;
export type FlowEvent = {
  id: number;
  taskId: string;
  at: number;
  direction: Direction;
  label: string;
};
const eventLabels: ReadonlyMap<string, string> = new Map([
  ["dispatch_ack", "节点确认接单"], ["approval_required", "节点申请审批"],
  ["tool_call", "工具调用"], ["tool_result", "工具结果"],
  ["log", "节点日志"], ["artifact", "产物就绪"],
  ["message", "会话消息"], ["result", "任务结果"],
]);
const RECENT_MS = 12_000;

// Keep only routing metadata. SSE data can include prompts, approvals and file paths.
export function readFlowEvent(raw: string): FlowEvent | null {
  try {
    const row = JSON.parse(raw);
    const label = eventLabels.get(row?.type);
    if (!label || !Number.isSafeInteger(row.id) || row.id < 1 ||
        typeof row.task_id !== "string" || !row.task_id || row.task_id.length > 128 ||
        !Number.isFinite(row.created)) return null;
    const at = row.created * 1000;
    const age = Date.now() - at;
    if (age < -1000 || age > RECENT_MS) return null;
    return { id: row.id, taskId: row.task_id, at, label,
      direction: row.type === "dispatch_ack" ? "out" : "back" };
  } catch {
    return null;
  }
}

type Props = {
  monitors: {name:string; fresh:boolean}[];
  onNode:(id:string)=>void;
  nodes: NodeInfo[];
  services: Service[];
  tasks: Task[];
  approvals: Approval[];
  live: boolean;
  events: FlowEvent[];
};
type Place = {
  key: string;
  name: string;
  subtitle: string;
  icon: LucideIcon;
  details: string;
  role: "client" | "hub" | "node" | "service";
  badges?: string[];
  offline?: boolean;
};
const modes = [
  { value: "all", label: "全景", icon: GitFork },
  { value: "out", label: "下发", icon: ArrowUpRight },
  { value: "back", label: "回传", icon: ArrowDownLeft },
] as const;

function Endpoint({ place, selected, choose }: {
  place: Place; selected: boolean; choose: () => void;
}) {
  const Icon = place.icon;
  const roleName = { client: uiText("控制台"), hub: uiText("中枢"), node: uiText("节点"), service: uiText("服务") }[place.role];
  return (
    <button type="button" className={`topo-endpoint topo-${place.role}`}
      aria-label={uiText("查看{0} {1}", roleName, place.name)} aria-pressed={selected}
      data-offline={place.offline || undefined} onClick={choose}>
      <Icon size={18} aria-hidden="true" />
      <span className="topo-name">
        <strong>{place.name}</strong>
        <small>{place.subtitle}{place.badges?.map(b=><small className="volume-badge" key={b}>{b}</small>)}</small>
      </span>
    </button>
  );
}

function Lanes({ events, connected, segment = "arm" }: {
  events: FlowEvent[]; connected: boolean; segment?: "arm" | "stem" | "client-link";
}) {
  return (
    <div className={`topo-lanes topo-${segment}`} data-connected={connected} aria-hidden="true">
      {(["out", "back"] as const).map((direction) => {
        const last = events.find((e) => e.direction === direction);
        return <span className={`topo-lane topo-${direction}`} key={direction} data-active={!!last && connected}>
          {last && connected && segment !== "stem" && <i key={last.id} className="topo-pulse" data-direction={direction} data-event-id={last.id} />}
        </span>;
      })}
    </div>
  );
}

function servicePlace(service: Service, node?: NodeInfo): Place {
  const state = service.online == null ? uiText("待探测") : service.online ? uiText("可连接") : uiText("未连通");
  const location = node ? uiText("关联节点 {0}{1}", node.alias, service.volume ? (" " + uiText("· 卷") + " ") + service.volume : "") : uiText("独立服务书签");
  return { key: "service/" + service.id, role: "service", name: service.name, icon: HardDrive,
    subtitle: state, details: uiText("{0} · Hub 探测{1}", location, state), offline: service.online === false };
}

export function FleetTopology({ monitors, onNode, nodes, services, tasks, approvals, live, events }: Props) {
  const heading = useId();
  const [showMonitor,setShowMonitor]=useState(false);
  const [view, setView] = useState<View>("all");
  const [selected, setSelected] = useState("hub");
  const [, refresh] = useState(0);
  const now = Date.now();
  const recent = events.filter((e) => e.at + RECENT_MS > now).slice().sort((a, b) => b.id - a.id);
  // Expire activity even when the stream goes quiet or the peer disconnects.
  const expiry = recent.length ? Math.min(...recent.map((e) => e.at)) + RECENT_MS : 0;
  useEffect(() => {
    if (!expiry) return;
    const timer = window.setTimeout(() => refresh((value) => value + 1), Math.max(0, expiry - Date.now()) + 1);
    return () => window.clearTimeout(timer);
  }, [expiry]);

  const client: Place = { key: "client", role: "client", icon: SquareTerminal, name: uiText("此控制台"),
    subtitle: live ? uiText("SSE 已连接") : uiText("SSE 重连中"),
    details: live ? uiText("已连接 Hub 事件流，可接收任务和审批更新。") : uiText("等待 Hub 事件流重连，设备清单仍会定时刷新。") };
  const hub: Place = { key: "hub", role: "hub", icon: Cpu, name: "Fleet Hub", subtitle: uiText("调度中枢"),
    details: uiText("{0} 个注册节点 · {1} 项待审批 · {2} 个服务书签", nodes.length, approvals.length, services.length) };
  const taskNodes = new Map(tasks.map((t) => [t.id, t.node_id]));
  const byNode = new Map<string, FlowEvent[]>();
  for (const event of recent) {
    const id = taskNodes.get(event.taskId);
    if (id) {
      if (!byNode.has(id)) byNode.set(id, []);
      byNode.get(id)!.push(event);
    }
  }
  const servicePlaces = services.map((service) => ({
    node: service.node_id, place: servicePlace(service, nodes.find((n) => n.id === service.node_id)),
  }));
  const branches = nodes.map((node) => {
    const pending = approvals.filter((a) => a.node_id === node.id).length;
    const activity = node.health?.active_tasks;
    const state = node.enabled === false ? uiText("已停用") : node.online ? uiText("在线") : uiText("离线");
    const kind = node.icon || node.health?.os || "";
    const subtitle = pending ? uiText("{0} · {1} 待审批", state, pending) :
      node.online && activity ? uiText("{0} · {1} 运行中", state, activity) : state;
    const info = [state, node.online && node.rtt != null ? `${Math.round(node.rtt)} ms` : null,
      node.online && activity != null ? uiText("{0} 个活跃任务", activity) : null, uiText("{0} 项待审批", pending)];
    const badges=(node.health?.volumes||[]).filter(v=>/nas/i.test(v.name)||services.some(s=>s.node_id===node.id && s.volume===v.name)).map(v=>v.name+" · "+(v.read_only?uiText("只读"):uiText("可写")));
    const place: Place = { badges, key: "node/" + node.id, name: node.alias, subtitle, role: "node",
      icon: /laptop|notebook/i.test(kind) ? Laptop : /server|linux/i.test(kind) ? Server : Monitor,
      offline: !node.online || node.enabled === false, details: info.filter(Boolean).join(" · ") };
    return { place, activity: byNode.get(node.id) || [],
      services: servicePlaces.filter((s) => s.node === node.id).map((s) => s.place) };
  });
  const standalone = servicePlaces.filter((s) => !nodes.some((n) => n.id === s.node));
  const places = [client, hub, ...branches.map((b) => b.place), ...servicePlaces.map((s) => s.place)];
  const focus = places.find((place) => place.key === selected) || hub;
  const endpoint = (place: Place) => <Endpoint key={place.key} place={place}
    selected={place.key === focus.key} choose={() => {setSelected(place.key);if(place.role==="node")onNode(place.key.slice(5));}} />;
  const visible = (items: FlowEvent[]) => items.filter((e) => view === "all" || e.direction === view);
  const selectedEvents = focus.role === "node" ? byNode.get(focus.key.slice(5)) || [] : recent;
  const latest = focus.role === "service" ? undefined : visible(selectedEvents)[0];
  const peer = latest && nodes.find((n) => n.id === taskNodes.get(latest.taskId));
  // Each branch shares the trunk only as far as its junction. A disconnected
  // leaf must not turn off the shared link to a reachable node farther down.
  const reachableFrom = new Array<boolean>(branches.length + 1);
  reachableFrom[branches.length] = false;
  for (let i = branches.length - 1; i >= 0; i--) {
    reachableFrom[i] = !branches[i].place.offline || reachableFrom[i + 1];
  }
  const onlineIds = new Set(nodes.filter((n) => n.online && n.enabled !== false).map((n) => n.id));
  const trunkEvents = recent.filter((e) => onlineIds.has(taskNodes.get(e.taskId) ?? ""));
  return (
    <section className="fleet-topology panel" aria-labelledby={heading} data-view={view}>
      <header className="topo-header">
        <h2 id={heading}><GitFork size={19} aria-hidden="true" />{uiText("设备拓扑")}</h2>
        <div className="topo-switch" role="group" aria-label={uiText("消息流向")}>
          {modes.map(({ value, label, icon: Icon }) => <button type="button" key={value}
            aria-pressed={view === value} onClick={() => setView(value)}><Icon size={14} />{uiText(label)}</button>)}
        </div>
      </header>
      <div className="topo-canvas">
        <div className="topo-source">
          {endpoint(client)}
          <Lanes segment="client-link" connected={live} events={live && view !== "out" && recent.length
            ? [{ ...recent[0], direction: "back" }] : []} />
          {endpoint(hub)}
          {branches.length>0 && <div className="center-stem"><Lanes segment="stem" connected={reachableFrom[0]} events={visible(trunkEvents)}/></div>}
        </div>
        <div className="topo-branches">
          {branches.map(({ place, services: attached, activity }, index) => <div key={place.key} className="topo-branch topo-device">
            {index === 0 && <Lanes segment="stem" connected={reachableFrom[0]} events={visible(trunkEvents)} />}
            <span aria-hidden="true" className="topo-spine topo-upper" data-connected={reachableFrom[index]} />
            <span aria-hidden="true" className="topo-spine topo-lower" data-connected={reachableFrom[index + 1]} />
            <Lanes connected={!place.offline} events={visible(activity)} />
            <div className="topo-leaves">
              {endpoint(place)}
              {attached.length > 0 && <div className="topo-attached">{attached.map(endpoint)}</div>}
            </div>
          </div>)}
          {standalone.map(({ place }) => <div key={place.key} className="topo-branch topo-bookmark">
            {endpoint(place)}
          </div>)}
          {!branches.length && !standalone.length && <span className="topo-empty">{uiText("等待设备接入")}</span>}
        </div>
      </div>
      {monitors.length>0 && <div className="monitor-branch"><button className="action" aria-expanded={showMonitor} onClick={()=>setShowMonitor(!showMonitor)}>{uiText("只读监测 ·") + " "}{monitors.length}</button>{showMonitor && monitors.map(m=><span key={m.name}>{m.name} · {m.fresh?uiText("采集中"):uiText("待刷新")}</span>)}</div>}
      <div className="topo-detail">
        <span className="topo-legend">
          {view !== "back" && <span className="topo-key-out">{uiText("下发")}</span>}
          {view !== "out" && <span className="topo-key-back">{uiText("回传")}</span>}
        </span>
        <div><strong>{focus.name}</strong><p>{focus.details}</p></div>
      </div>
      <div className="topo-recent">
        <Radio size={14} aria-hidden="true" />
        {latest ? <span><b>{uiText("刚刚")}</b>{peer ? ` · ${peer.alias}` : ""} · {uiText(latest.label)}</span>
          : <span>{focus.role === "service" ? uiText("服务状态来自 Hub 连通性探测") : uiText("虚线表示流向 · 近 12 秒无相关任务事件")}</span>}
      </div>
    </section>
  );
}

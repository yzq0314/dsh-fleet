import { uiText } from "./i18n";
import { AppearanceProvider } from "./Appearance";
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { createRoot } from "react-dom/client";
import {
  ArrowRight,
  ChevronRight,
  FolderOpen,
  LockKeyhole,
  Network,
  Send,
  ShieldCheck,
  Terminal,
  X,
} from "lucide-react";
import {
  api,
  initialSettings,
  errorText,
  type Approval,
  type Macro,
  type HardwareMonitor,
  type NodeInfo,
  type Run,
  type Service,
  type Settings as Prefs,
  type Tab,
  type Task,
} from "./model";
import { Brand, Label } from "./shared";
import { Fleet } from "./Fleet";
import { readFlowEvent, type FlowEvent } from "./FleetTopology";
import { Dispatch, type Draft } from "./Dispatch";
import { Tasks } from "./Tasks";
import { Files } from "./Files";
import { Settings } from "./Settings";
import "./style.css";
import "./appearance.css";
import "./console.css";
import "./workspace-controls.css";
import { currentLocale, pageCaption, pageTitle, setLocale, subscribeLocale, translations } from "./i18n";
const tabs = [
  { id: "fleet", name: "设备", Icon: Network },
  { id: "dispatch", name: "派单", Icon: Send },
  { id: "tasks", name: "任务", Icon: Terminal },
  { id: "files", name: "文件", Icon: FolderOpen },
  { id: "settings", name: "审批设置", Icon: ShieldCheck },
] as const;
function Login({ ok }: { ok: () => void }) {
  const [chosen, setChosen] = useState(false);
  const [password, setPassword] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <main className="login">
      <Brand />
      <div className="login-card panel">
        <div className="device-icon">
          <LockKeyhole size={23} />
        </div>
        <div className="login-languages" role="group" aria-label="Language / 语言">
          {Object.entries(translations).map(([id, item]) => <button type="button" key={id} lang={id} aria-pressed={currentLocale() === id} onClick={() => { setLocale(id); setChosen(true); }}>{item.languageName}</button>)}
        </div>
        <p className="eyebrow">ADMIN ACCESS</p>
        <h1>{uiText("登录控制台")}</h1>
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            try {
              await api("/login", "POST", { password });
              if (chosen) await api("/settings", "PATCH", { language: currentLocale() });
              ok();
            } catch (e) {
              setErr(errorText(e));
            } finally {
              setBusy(false);
            }
          }}
        >
          <Label name={uiText("管理员密码 / PIN")}>
            <input
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </Label>
          {err && (
            <p role="alert" className="error">
              {err}
            </p>
          )}
          <button className="primary" disabled={busy}>{uiText("登录")}<ArrowRight size={17} />
          </button>
        </form>
      </div>
    </main>
  );
}
function Console({ signout }: { signout: () => void }) {
  const [tab, setTab] = useState<Tab>("fleet");
  const [nodes, setNodes] = useState<NodeInfo[]>([]);
  const [monitors, setMonitors] = useState<HardwareMonitor[]>([]);
  const [services, setServices] = useState<Service[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [macros, setMacros] = useState<Macro[]>([]);
  const [prefs, setPrefs] = useState<Prefs>(initialSettings);

  const [error, setError] = useState("");
  const [refreshError, setRefreshError] = useState("");
  const [tick, setTick] = useState(0);
  const [task, setTask] = useState("");
  const [draft, setDraft] = useState<Draft>();
  const [fileView, setFileView] = useState({ node: "", volume: "", path: "" });
  const [live, setLive] = useState(false);
  const [flowEvents, setFlowEvents] = useState<FlowEvent[]>([]);
  const [ready, setReady] = useState(false);
  const rev = useRef(0);
  const run: Run = (f) => {
    setError("");
    void f().catch((e) => setError(errorText(e)));
  };
  const reload = useCallback(async () => {
    const seq = ++rev.current;
    const snapshot = await api<{nodes:NodeInfo[]; monitors?:HardwareMonitor[]; services:Service[]; tasks:Task[];
      approvals:Approval[]; macros:Macro[]; settings:Prefs}>("/overview");
    if (seq !== rev.current) return;
    setNodes(snapshot.nodes);
    setMonitors(snapshot.monitors ?? []);
    setServices(snapshot.services);
    setTasks(snapshot.tasks);
    setApprovals(snapshot.approvals);
    setMacros(snapshot.macros);
    setLocale(snapshot.settings.language || "zh-CN");
    setPrefs({ ...initialSettings, ...snapshot.settings });
    setRefreshError("");
    setReady(true);
    setTick((t) => t + 1);
  }, []);
  useEffect(() => {
    let timer: number | undefined;
    let stopped = false;
    let refreshing = false;
    let refreshQueued = false;
    let pendingFlows: FlowEvent[] = [];
    const update = () => {
      if (stopped) return;
      // Let a slow inventory response reach the screen during a busy event stream.
      if (refreshing) {
        refreshQueued = true;
        return;
      }
      refreshing = true;
      void reload()
        .catch((e) => {
          if (!stopped) setRefreshError(errorText(e));
        })
        .finally(() => {
          refreshing = false;
          if (refreshQueued) {
            refreshQueued = false;
            update();
          }
        });
    };
    update();
    const stream = new EventSource("/api/events?live=true");
    stream.onopen = () => {
      setLive(true);
      update();
    };
    stream.onerror = () => setLive(false);
    stream.onmessage = (message) => {
      const flow = readFlowEvent(message.data);
      if (flow) {
        pendingFlows.push(flow);
        if (pendingFlows.length > 32) pendingFlows.shift();
      }
      if (!timer)
        timer = window.setTimeout(() => {
          timer = undefined;
          if (pendingFlows.length) {
            const batch = pendingFlows;
            pendingFlows = [];
            setFlowEvents((prior) => [...prior, ...batch].slice(-32));
          }
          update();
        }, 500);
    };
    const poll = window.setInterval(update, 10000);
    return () => {
      stopped = true;
      rev.current++;
      clearInterval(poll);
      clearTimeout(timer);
      stream.close();
    };
  }, [reload]);
  const page = (t: Tab) => {
    setTab(t);
    setError("");
    window.scrollTo(0, 0);
  };
  const openFiles = (node: string, volume = "", path = "") => {
    setFileView({ node, volume, path });
    page("files");
  };
  return (
    <div className="shell">
      <header className="mobile-head">
        <Brand />
        <span className="live">
          <i className={"dot " + (live ? "" : "off")} />
          {live ? uiText("已连接") : uiText("连接中")}
        </span>
      </header>
      <nav className="sidebar" aria-label={uiText("主导航")}>
        <Brand />
        <small className="eyebrow">{uiText("工作空间")}</small>
        {tabs.map(({ id, name, Icon }) => (
          <button
            key={id}
            className={id === tab ? "on" : ""}
            onClick={() => page(id)}
          >
            <Icon size={18} />
            {uiText(name)}
          </button>
        ))}
        <span className="sidebar-state">
          <i className={"dot " + (live ? "" : "off")} />
          {live ? uiText("Hub 已连接") : uiText("连接中")}
        </span>
      </nav>
      <main className="content">
        <div className="title-row">
          <div>
            <div className="eyebrow">{pageCaption(tab)}</div>
            <h1>{pageTitle(tab)}</h1>
          </div>
          <span className="page-glyph">
            {tab === "fleet" ? (
              <Network />
            ) : tab === "files" ? (
              <FolderOpen />
            ) : tab === "settings" ? (
              <ShieldCheck />
            ) : tab === "tasks" ? (
              <Terminal />
            ) : (
              <Send />
            )}
          </span>
        </div>
        {approvals.length > 0 && tab !== "settings" && (
          <button
            className="alertbar"
            aria-label={approvals.length + (" " + uiText("项操作等待审批"))}
            onClick={() => page("settings")}
          >
            <ShieldCheck size={18} />
            <span className="grow">
              <strong>{approvals.length}{" " + uiText("项操作等待审批")}</strong>
              <small>
                {nodes.find((n) => n.id === approvals[0].node_id)?.alias ||
                  approvals[0].node_id}{" "}
                · {approvals[0].reason}
              </small>
            </span>
            <ChevronRight size={16} />
          </button>
        )}
        {refreshError && <div className="panel" role="status">{refreshError}{" " + uiText("正在自动重连…")}</div>}
        {error && (
          <div className="error toast" role="alert">
            {error}
            <button
              className="icon"
              aria-label={uiText("关闭提示")}
              onClick={() => setError("")}
            >
              <X size={16} />
            </button>
          </div>
        )}
        {!ready ? (
          <p className="caption">{uiText("正在连接控制中枢…")}</p>
        ) : tab === "fleet" ? (
          <Fleet
            nodes={nodes}
            services={services}
            monitors={monitors}
            macros={macros}
            tasks={tasks}
            approvals={approvals}
            live={live}
            flowEvents={flowEvents}
            onOpenTask={(id) => {
              setTask(id);
              page("tasks");
            }}
            onDispatchNode={(id,macroId) => {
              setDraft({ node: id, workspace: "", agent: "dsh", macroId });
              page("dispatch");
            }}
            run={run}
            reload={reload}
            onFiles={openFiles}
          />
        ) : tab === "dispatch" ? (
          <Dispatch
            nodes={nodes}
            macros={macros}
            settings={prefs}
            draft={draft}
            run={run}
            reload={reload}
            onFiles={openFiles}
            onTask={(id) => {
              setTask(id);
              setDraft(undefined);
              page("tasks");
            }}
          />
        ) : tab === "tasks" ? (
          <Tasks
            approvals={approvals}
            tasks={tasks}
            nodes={nodes}
            selected={task}
            onSelect={setTask}
            tick={tick}
            reload={reload}
            run={run}
          />
        ) : tab === "files" ? (
          <Files
            nodes={nodes}
            initialNode={fileView.node}
            initialVolume={fileView.volume}
            initialPath={fileView.path}
            run={run}
            onDispatch={(n, c) => {
              setDraft({ node: n, workspace: "", context: c });
              page("dispatch");
            }}
          />
        ) : (
          <Settings
            nodes={nodes} services={services} macros={macros}
            approvals={approvals}
            settings={prefs}
            run={run}
            reload={reload}
            logout={() =>
              run(async () => {
                await api("/logout", "POST", {});
                signout();
              })
            }
          />
        )}
      </main>
      <nav className="mobile-nav" aria-label={uiText("主导航")}>
        {tabs.map(({ id, name, Icon }) => (
          <button
            key={id}
            className={tab === id ? "on" : ""}
            aria-current={tab === id ? "page" : undefined}
            onClick={() => page(id)}
          >
            <Icon size={19} />
            <span>{uiText(name)}</span>
          </button>
        ))}
      </nav>
    </div>
  );
}
function App() {
  const language = useSyncExternalStore(subscribeLocale, currentLocale);
  useEffect(() => { document.documentElement.lang = language; }, [language]);
  const [auth, setAuth] = useState<boolean | null>(null);
  const [failure, setFailure] = useState("");
  const check = () => {
    if (!navigator.onLine) {
      setAuth(null);
      setFailure("offline");
      return;
    }
    setFailure("");
    api<{ authenticated: boolean }>("/me")
      .then((r) => setAuth(r.authenticated))
      .catch((e) => setFailure(errorText(e)));
  };
  useEffect(() => {
    check();
    const expired = () => setAuth(false);
    window.addEventListener("fleet:expired", expired);
    window.addEventListener("online", check);
    window.addEventListener("offline", check);
    return () => {
      window.removeEventListener("fleet:expired", expired);
      window.removeEventListener("online", check);
      window.removeEventListener("offline", check);
    };
  }, []);
  if (auth === null)
    return (
      <main className="login">
        <Brand />
        <p>
          {failure === "offline"
            ? uiText("当前离线，联网后会重新连接控制中枢。")
            : failure
              ? uiText("暂时无法连接控制中枢，请检查内网连接。")
              : uiText("正在连接…")}
        </p>
        {failure && (
          <button className="action" onClick={check}>{uiText("重新连接")}</button>
        )}
      </main>
    );
  return auth ? (
    <AppearanceProvider><Console signout={() => setAuth(false)} /></AppearanceProvider>
  ) : (
    <Login ok={() => setAuth(true)} />
  );
}
createRoot(document.getElementById("root")!).render(<App />);
if (
  import.meta.env.PROD &&
  "serviceWorker" in navigator &&
  window.isSecureContext
)
  navigator.serviceWorker.register("/sw.js").catch(() => {});

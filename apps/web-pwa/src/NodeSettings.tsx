import { currentLocale, uiText } from "./i18n";
import { NotificationRules } from "./NotificationRules";
import { Choice } from "./Choice";
import { useEffect, useState } from "react";
import { Check, Monitor, ShieldAlert } from "lucide-react";
import { api, enc, type NodeInfo } from "./model";
import { Label, Modal, Section } from "./shared";
type Mode = "balanced" | "strict_hash" | "autonomous";
type Policy = { mode: Mode; rules: string[]; runners?: string[] };
type Desktop = {
  online: boolean;
  last_seen: number | null;
  version?: string;
  sessions: number;
  install_hint?: string;
};
const policies: Record<Mode, { name: string; hint: string }> = {
  balanced: { name: "均衡", hint: "按节点的目录与高危操作规则逐次判断。" },
  strict_hash: { name: "严格哈希", hint: "仅运行节点管理员登记且摘要匹配的脚本。" },
  autonomous: {
    name: "自主执行",
    hint: "启用后，受节点执行边界约束的操作可自动通过。",
  },
};
export function NodeSettings({ nodes, account }: { nodes: NodeInfo[]; account: React.ReactNode }) {
  const [node, setNode] = useState(nodes[0]?.id || "");
  const [savedMode, setSavedMode] = useState<Mode>("balanced");
  const [mode, setMode] = useState<Mode>("balanced");
  const [rules, setRules] = useState("");
  const [runners, setRunners] = useState<string[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [desktop, setDesktop] = useState<Desktop | null>(null);
  const [linkError, setLinkError] = useState("");
  useEffect(() => {
    let live = true;
    setLoaded(false);
    setSaved(false);
    setError("");
    setConfirm(false);
    setDesktop(null);
    setLinkError("");
    setRules("");
    setRunners([]);
    if (!node) return;
    const base = "/nodes/" + enc(node);
    api<Policy>(base + "/policy")
      .then((p) => {
        if (live) {
          setSavedMode(p.mode || "balanced");
          setMode(p.mode || "balanced");
          setRules(p.rules.join("\n"));
          setRunners(p.runners ?? []);
          setLoaded(true);
        }
      })
      .catch((e) => {
        if (live) setError(e.message);
      });
    const status = () =>
      api<Desktop>(base + "/desktop")
        .then((d) => {
          if (live) {
            setDesktop(d);
            setLinkError("");
          }
        })
        .catch((e) => {
          if (live) setLinkError(e.message);
        });
    void status();
    const timer = window.setInterval(status, 5000);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [node]);
  async function save(confirmed = false) {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await api("/nodes/" + enc(node) + "/policy", "PUT", {
        mode,
        rules: rules.split("\n").filter((s) => s.trim()),
        ...(confirmed ? { confirm_autonomous: true } : {}),
      });
      setSavedMode(mode);
      setSaved(true);
      setConfirm(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : uiText("策略保存失败"));
      setConfirm(false);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="settings-grid"><div><Section title={uiText("审批规则")} />
      <form
        className="panel config policy-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (mode === "autonomous" && savedMode !== mode) setConfirm(true);
          else void save();
        }}
      >
        <Label name={uiText("策略节点")}>
          <Choice
            aria-label={uiText("策略节点")}
            disabled={busy}
            value={node}
            onChange={(e) => setNode(e.target.value)}
          >
            {nodes.map((n) => (
              <option key={n.id} value={n.id}>
                {n.alias}
              </option>
            ))}
          </Choice>
        </Label>
        <Label name={uiText("审批模式")}>
          <Choice
            aria-label={uiText("审批模式")}
            disabled={!loaded || busy}
            value={mode}
            onChange={(e) => {
              setMode(e.target.value as Mode);
              setSaved(false);
            }}
          >
            {Object.entries(policies).map(([id, p]) => (
              <option key={id} value={id}>
                {uiText(p.name)}
              </option>
            ))}
          </Choice>
        </Label>
        <small>{uiText(policies[mode].hint)}{" " + uiText("更改档位或规则会中止本节点正在运行的任务。")}</small>
        {mode === "strict_hash" && (
          <small>{uiText("已登记脚本 ·") + " "}{runners.length ? runners.join("、") : uiText("暂无")}
          </small>
        )}
        <Label name={uiText("高危拦截规则（每行一条正则）")}>
          <textarea
            rows={4}
            spellCheck={false}
            disabled={!loaded || busy}
            value={rules}
            onChange={(e) => {
              setRules(e.target.value);
              setSaved(false);
            }}
          />
        </Label>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        <button className="primary" disabled={!loaded || busy}>
          <Check size={16} />
          {saved ? uiText("已保存规则") : uiText("保存规则")}
        </button>
      </form>
      <NotificationRules />
      </div><div><Section title={uiText("桌面插件")} />
      <section className="panel desktop-status" aria-label={uiText("桌面插件状态")}>
        <div className="row">
          <span className="device-icon">
            <Monitor size={20} />
          </span>
          <div>
            <h3>{nodes.find((n) => n.id === node)?.alias || uiText("当前节点")}</h3>
            <small>
              {linkError
                ? uiText("状态读取失败")
                : desktop
                  ? desktop.online
                    ? uiText("插件已连接")
                    : uiText("插件未连接")
                  : uiText("正在读取插件状态…")}
            </small>
          </div>
        </div>
        {linkError ? (
          <p role="alert">{linkError}</p>
        ) : (
          desktop && (
            <>
              <dl>
                <div>
                  <dt>{uiText("版本")}</dt>
                  <dd>{desktop.version || "—"}</dd>
                </div>
                <div>
                  <dt>{uiText("同步会话")}</dt>
                  <dd>{desktop.sessions ?? 0}</dd>
                </div>
                <div>
                  <dt>{uiText("最近心跳")}</dt>
                  <dd>
                    {desktop.last_seen
                      ? new Date(desktop.last_seen * 1000).toLocaleString(currentLocale())
                      : uiText("尚无记录")}
                  </dd>
                </div>
              </dl>
              {!desktop.online && desktop.install_hint && (
                <details className="plugin-install">
                  <summary>{uiText("查看安装命令")}</summary>
                  <p>{uiText("在该设备的仓库目录中，用登录 DSH 的用户运行下列命令，再重启桌面应用。使用自定义节点配置时请替换最后一个路径。")}</p>
                  <pre>{desktop.install_hint}</pre>
                </details>
              )}
            </>
          )
        )}
      </section>
      {account}</div></div>
      {confirm && (
        <Modal
          title={uiText("启用自主执行")}
          close={() => {
            if (!busy) setConfirm(false);
          }}
        >
          <div className="dialog-body">
            <ShieldAlert size={25} />
            <p>{uiText("将对") + " "}{nodes.find((n) => n.id === node)?.alias}{" "}{uiText("启用自主执行。请确认该节点的目录和运行器授权范围。")}</p>
            <button
              className="primary"
              disabled={busy}
              onClick={() => void save(true)}
            >{uiText("确认启用自主执行")}</button>
            <button
              className="action"
              disabled={busy}
              onClick={() => setConfirm(false)}
            >{uiText("返回检查")}</button>
          </div>
        </Modal>
      )}
    </>
  );
}

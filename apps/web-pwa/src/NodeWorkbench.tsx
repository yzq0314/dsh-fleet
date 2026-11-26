import { uiText } from "./i18n";
import { ArrowUpRight, ChevronRight, MessagesSquare, Send, Settings2 } from "lucide-react";
import { type NodeInfo, type Task } from "./model";
import "./node-workbench.css";

function entryURL(node: NodeInfo) {
  if (!node.dsh_web_url) return;
  try {
    const url = new URL(node.dsh_web_url);
    if (
      ["https:", "http:"].includes(url.protocol) &&
      !url.username && !url.password && !url.search && !url.hash &&
      url.pathname === "/" && url.hostname === new URL(node.url).hostname
    ) return url;
  } catch { /* Node records may predate this feature. */ }
}

function History({ title, tasks, open }: {
  title: string;
  tasks: Task[];
  open: (id: string) => void;
}) {
  return (
    <section className="workbench-history">
      <h3>{title} <small>{tasks.length}</small></h3>
      {tasks.length === 0 ? <p>{uiText("暂无记录")}</p> : (
        <ul>{tasks.map((task) => (
          <li key={task.id}>
            <button onClick={() => open(task.id)}>
              <span><b>{task.prompt}</b>{task.workspace && <small>{task.workspace}</small>}</span>
              <ChevronRight size={16} aria-hidden />
            </button>
          </li>
        ))}</ul>
      )}
    </section>
  );
}

export function NodeWorkbench({ node, tasks, onDispatch, onOpenTask, onConfigure, onPocket }: {
  node: NodeInfo;
  tasks: Task[];
  onDispatch: () => void;
  onOpenTask: (id: string) => void;
  onConfigure: () => void;
  onPocket: () => void;
}) {
  const url = entryURL(node);
  // HTTP cookies are scoped by hostname, including across ports of that host.
  const sameHost = url?.hostname === window.location.hostname.replace(/\.$/, "");
  return (
    <div className="dialog-body node-workbench">
      <div className="workbench-actions">
        {node.online && node.pocket_embed && node.health?.pocket_status==="ready" && <button className="action" onClick={onPocket}>{uiText("进入 DSH 实时同屏")}</button>}
        <button className="primary" onClick={onDispatch} disabled={node.enabled === false}>
          <Send size={16} />{" " + uiText("向本机派单")}</button>
        {url && !sameHost ? (
          <a href={url.href} className="action subtle" target="_blank" rel="noopener noreferrer">
            <MessagesSquare size={16} />{" " + uiText("打开 DSH Web") + " "}<ArrowUpRight size={14} />
          </a>
        ) : (
          <button className="action subtle" onClick={onConfigure}>
            <Settings2 size={16} />{" " + uiText("配置 DSH Web")}</button>
        )}
      </div>
      <p>{sameHost
        ? uiText("浏览器会跨端口发送同主机 Cookie。请为 DSH Web 使用独立的内网主机名。")
        : url
          ? uiText("首次登录时，在此手机浏览器打开 DSH Web 本机生成的登录链接。")
          : uiText("此设备尚未配置 DSH Web。启动后可在节点设置填写端口。")}</p>
      <p>DSH Pocket · {node.health?.pocket_status === "ready" ? uiText("本机 HTTP 已响应") : node.health?.pocket_status === "stopped" ? uiText("服务未响应") : node.health?.pocket_status === "unconfigured" ? uiText("未配置") : uiText("等待节点上报")} · {uiText(node.health?.pocket_hint || "")}</p>
      <History title={uiText("桌面共享会话")} tasks={tasks.filter((t) => t.origin === "desktop")} open={onOpenTask} />
      <History title={uiText("Fleet 任务")} tasks={tasks.filter((t) => t.origin !== "desktop")} open={onOpenTask} />
    </div>
  );
}

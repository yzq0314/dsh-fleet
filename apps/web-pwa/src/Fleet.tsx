import { displayMacro } from "./builtinMacros";
import { currentLocale, uiText } from "./i18n";
import { useAppearance } from "./Appearance";
import { type Block } from "./theme";
import { useEffect } from "react";
import { Meter, MonitorCard } from "./MonitorCard";
import { Choice } from "./Choice";
import { useState } from "react";
import { CopyButton } from "./CopyButton";
import {
  Plus,
  Monitor,
  Laptop,
  Server,
  HardDrive,
  ArrowUpRight,
  Activity,
  SlidersHorizontal,
  Wifi,
  Power,
  Pencil,
  Layers,
  FolderOpen,
  MessagesSquare,
} from "lucide-react";
import {
  api,
  enc,
  type Macro,
  safeWebURL,
  type NodeInfo,
  type HardwareMonitor,
  type Service,
  type Approval,
  type Task,
  type Run,
} from "./model";
import { Section, Label, Modal, Empty } from "./shared";
import { FleetTopology, type FlowEvent } from "./FleetTopology";
import { PocketView } from "./PocketView";
import { NodeWorkbench } from "./NodeWorkbench";
type Props = {
  nodes: NodeInfo[];
  monitors?: HardwareMonitor[];
  services: Service[];
  tasks?: Task[];
  approvals?: Approval[];
  live?: boolean;
  flowEvents?: FlowEvent[];
  run: Run;
  reload: () => Promise<void>;
  onFiles: (node: string, volume: string) => void;
  onOpenTask: (id: string) => void;
  macros: Macro[];
  onDispatchNode: (id: string, macroId?:string) => void;
};
function Device({
  node,
  edit,
  wake, shortcuts,
}: {
  node: NodeInfo;
  shortcuts:React.ReactNode;
  edit: () => void;
  wake: () => void;
}) {
  const icon = node.icon || node.health?.os || "";
  const Icon = /laptop|notebook/i.test(icon)
    ? Laptop
    : /server|linux/i.test(icon)
      ? Server
      : Monitor;
  const health = node.health;
  return (
    <article className="device panel">
      <div className="row">
        <div className="device-icon">
          <Icon size={22} />
        </div>
        <div className="grow">
          <h3>{node.alias}</h3>
          <span className="node-line">
            <i className={"dot " + (node.online ? "" : "off")} />
            {node.online ? uiText("在线") : uiText("离线")}
            {node.online && node.rtt != null && (
              <> · {Number(node.rtt).toFixed(0)} ms</>
            )}
          </span>
        </div>
        <button
          className="icon"
          aria-label={(uiText("编辑") + " ") + node.alias}
          onClick={edit}
        >
          <SlidersHorizontal size={17} />
        </button>
      </div>
      <div className="tags">
        {(node.tags || []).map((t) => (
          <span key={t}>{t}</span>
        ))}
      </div>
      <div className="meters">
        <Meter name="CPU" value={health?.cpu} />
        <Meter name="RAM" value={health?.ram} />
        <Meter name="GPU" value={health?.gpu} />
      </div>
      <footer>
        <span>
          <Activity size={14} />
          {health?.active_tasks ?? 0}{" " + uiText("个活跃任务")}</span>
        {shortcuts}
        {!node.online && (
          <button onClick={wake}>
            <Power size={13} />{uiText("唤醒")}</button>
        )}
      </footer>
    </article>
  );
}
export function Fleet({ nodes, macros, monitors = [], services, tasks = [], approvals = [], live = false, flowEvents = [], run, reload, onFiles, onOpenTask, onDispatchNode }: Props) {
  const [modal, setModal] = useState<"pair" | "edit" | "service" | "workbench" | null>(null);
  const [join, setJoin] = useState<{
    command: string;
    token: string;
    expires_at: string;
  } | null>(null);
  const [node, setNode] = useState<NodeInfo | null>(null);
  const [service, setService] = useState<Service | null>(null);
  const [confirm, setConfirm] = useState(false);
  const online = nodes.filter((n) => n.online).length;
  const currentNode = nodes.find((n) => n.id === node?.id);
  const close = () => {
    setModal(null);
    setJoin(null);
    setConfirm(false);
  };
  function generate() {
    run(async () => setJoin(await api("/join-tokens", "POST", {})));
  }
  function saveNode(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    run(async () => {
      await api("/nodes/" + enc(node!.id), "PATCH", {
        alias: f.get("alias"),
        url: f.get("url"),
        dsh_web_port: f.get("dshWebPort") ? Number(f.get("dshWebPort")) : null,
        dsh_web_scheme: f.get("dshWebScheme"),
        icon: f.get("icon"),
        tags: String(f.get("tags"))
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
        mac: f.get("mac") || null,
        broadcast: f.get("broadcast") || null,
        wake_proxy: f.get("wake_proxy") || null,
      });
      close();
      await reload();
    });
  }
  function saveService(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    run(async () => {
      await api(
        "/services" + (service?.id ? "/" + enc(service.id) : ""),
        service?.id ? "PATCH" : "POST",
        {
          name: f.get("name"),
          url: f.get("url"),
          node_id: f.get("node_id") || null,
          volume: f.get("volume") || null,
        },
      );
      close();
      await reload();
    });
  }
  const [screen,setScreen]=useState<string|null>(null);
  const {theme}=useAppearance();
  const [mobile,setMobile]=useState(()=>matchMedia("(max-width: 1099px)").matches);
  useEffect(()=>{const mq=matchMedia("(max-width: 1099px)");const update=()=>setMobile(mq.matches);mq.addEventListener("change",update);return ()=>mq.removeEventListener("change",update);},[]);
  function actions(n:NodeInfo){
    const links=(theme.shortcuts[n.id]??["workbench"]).map(key=>{
      if(key==="workbench")return <button key={key} onClick={()=>{setNode(n);setModal("workbench");}} aria-label={(uiText("打开") + " ")+n.alias+(" " + uiText("的 DSH 工作台"))}><MessagesSquare size={15}/>{" " + uiText("DSH 工作台")}</button>;
      const [kind,id]=key.split(":");
      if(kind==="service"){const s=services.find(x=>x.id===id);const url=s && safeWebURL(s.url);return url?<a key={key} href={url} target="_blank" rel="noopener noreferrer">{s?.name}</a>:null;}
      if(kind==="volume")return <button key={key} onClick={()=>onFiles(n.id,id)}>{id}</button>;
      const m=macros.find(x=>x.id===id);return m?<button key={key} onClick={()=>onDispatchNode(n.id,id)}>{displayMacro(m).name}</button>:null;
    });
    if(n.online && n.pocket_embed && n.health?.pocket_status==="ready")
      links.unshift(<button key="pocket" className="action" onClick={()=>setScreen(n.id)}>{uiText("进入 DSH 实时同屏")}</button>);
    return links;
  }
  const sections:Record<Block,React.ReactNode>={
overview:<><div className="overview">
        <div>
          <div className="overview-title">
            <span className="live-ring" />
            <span>{uiText("集群状态")}</span>
          </div>
          <div className="online-number">
            {online}
            <small> / {nodes.length}</small>
          </div>
          <div className="caption">{uiText("在线节点")}</div>
        </div>
        <div className="overview-stats">
          <span>
            <Activity size={15} />{uiText("活跃任务")}{" "}
            <b>
              {nodes.reduce((s, n) => s + (n.health?.active_tasks || 0), 0)}
            </b>
          </span>
          <span>
            <Layers size={15} />{uiText("关联服务") + " "}<b>{services.length}</b>
          </span>
        </div>
      </div></>,
topology:<><FleetTopology monitors={monitors} onNode={id=>{setNode(nodes.find(n=>n.id===id)||null);setModal("workbench");}} nodes={nodes} services={services} tasks={tasks} approvals={approvals} live={live} events={flowEvents} /></>,
devices:<><Section
        title={uiText("设备节点")}
        count={nodes.length}
        action={
          <button
            className="action subtle"
            onClick={() => {
              setModal("pair");
              setJoin(null);
            }}
          >
            <Plus size={15} />{uiText("添加设备")}</button>
        }
      />
      <div className="device-grid">
        {nodes.map((n) => (
          <Device
            key={n.id}
            node={n}
            shortcuts={actions(n)}
            edit={() => {
              setNode(n);
              setModal("edit");
            }}
            wake={() =>
              run(async () => {
                await api("/nodes/" + enc(n.id) + "/wake", "POST", {});
                await reload();
              })
            }
          />
        ))}
      </div>
      {!nodes.length && <Empty>{uiText("尚未接入设备")}</Empty>}</>,
hardware:<>{monitors.length > 0 && <>
        <Section title={uiText("只读硬件监控")} count={monitors.length} />
        <div className="monitor-grid">{monitors.map(m=><MonitorCard key={m.name} monitor={m} reload={reload} />)}</div>
      </>}</>,
services:<><Section
        title={uiText("快捷服务")}
        count={services.length}
        action={
          <button
            className="icon"
            aria-label={uiText("添加服务")}
            onClick={() => {
              setService(null);
              setModal("service");
            }}
          >
            <Plus size={19} />
          </button>
        }
      />
      <div className="service-grid">
        {services.map((s) => (
          <article key={s.id} className="panel service">
            <div className="row">
              <span className="device-icon amber">
                <HardDrive size={21} />
              </span>
              <div className="grow">
                <h3>{s.name}</h3>
                <small>
                  <i className={"dot " + (s.online ? "" : "off")} />
                  {s.online
                    ? uiText("可连接")
                    : s.online === false
                      ? uiText("未连通")
                      : uiText("待探测")}
                </small>
              </div>
              <button
                className="icon"
                aria-label={(uiText("编辑服务") + " ") + s.name}
                onClick={() => {
                  setService(s);
                  setModal("service");
                }}
              >
                <Pencil size={15} />
              </button>
            </div>
            <div className="service-links">
              {safeWebURL(s.url) && (
                <a
                  href={safeWebURL(s.url)}
                  target="_blank"
                  rel="noopener noreferrer"
                >{uiText("访问服务") + " "}<ArrowUpRight size={15} />
                </a>
              )}
              {s.node_id && s.volume && (
                <button onClick={() => onFiles(s.node_id!, s.volume!)}>
                  <FolderOpen size={15} />{uiText("浏览挂载卷")}</button>
              )}
            </div>
          </article>
        ))}
      </div>
      {!services.length && <Empty>{uiText("暂无快捷服务")}</Empty>}</>
  };
  const order = theme.layout[mobile ? "mobile" : "desktop"];
  const block = (k: Block) => <div key={k} data-home-section={k}>{sections[k]}</div>;
  const side = order.filter(k => k === "overview" || k === "services");
  const bento = !mobile && order.includes("topology") && side.length > 0;
  const top = [...side, "topology"];
  return (<>
    {order.map(k => !bento || !top.includes(k) ? block(k) :
      k === order.find(item=>top.includes(item)) ? <div key="bento" className="fleet-bento"><div className="bento-rail">{side.map(block)}</div>{block("topology")}</div> : null)}
      {modal === "pair" && (
        <Modal title={uiText("添加新设备")} close={close}>
          <div className="dialog-body">
            <p>{uiText("在新设备上运行接入命令。配对码只能使用一次。")}</p>
            {join ? (
              <>
                <Label name={uiText("一次性配对码")}>
                  <code className="code pair-secret">{join.token}</code>
                </Label>
                <Label name={uiText("注册命令")}>
                  <pre className="pair-secret">{join.command}</pre>
                </Label>
                <small>{uiText("有效期限 ·")}{" "}
                  {new Date(
                    typeof join.expires_at === "number"
                      ? join.expires_at * 1000
                      : join.expires_at,
                  ).toLocaleString(currentLocale())}
                </small>
                <CopyButton key={join.command} className="primary" text={join.command} label={uiText("复制接入命令")} />
              </>
            ) : (
              <button className="primary" onClick={generate}>
                <Wifi size={16} />{uiText("生成配对码")}</button>
            )}
          </div>
        </Modal>
      )}
      {screen && <PocketView nodes={nodes} selected={screen} close={()=>setScreen(null)} />}
      {modal === "workbench" && currentNode && (
        <Modal title={currentNode.alias + (" " + uiText("· DSH 工作台"))} close={close}>
          <NodeWorkbench onPocket={()=>{setModal(null);setScreen(currentNode.id);}}
            node={currentNode}
            tasks={tasks.filter((t) => t.node_id === currentNode.id)}
            onDispatch={() => { close(); onDispatchNode(currentNode.id); }}
            onOpenTask={(id) => { close(); onOpenTask(id); }}
            onConfigure={() => { setNode(currentNode); setModal("edit"); }}
          />
        </Modal>
      )}
      {modal === "edit" && node && (
        <Modal title={uiText("编辑节点")} close={close}>
          <form onSubmit={saveNode}>
            <Label name={uiText("别名")}>
              <input name="alias" defaultValue={node.alias} required />
            </Label>
            <Label name={uiText("节点地址")}>
              <input name="url" type="url" defaultValue={node.url} required />
            </Label>
            <div className="fields-two">
              <Label name={uiText("DSH Web 端口")}>
                <input type="number" min={1} max={65535} name="dshWebPort"
                  defaultValue={node.dsh_web_port ?? ""} placeholder={uiText("未启用")} />
              </Label>
              <Label name={uiText("DSH Web 协议")}>
                <Choice aria-label={uiText("DSH Web 协议")} name="dshWebScheme" defaultValue={node.dsh_web_scheme ?? "https"}>
                  <option value="https">HTTPS</option>
                  <option value="http">HTTP</option>
                </Choice>
              </Label>
            </div>
            <Label name={uiText("标签（逗号分隔）")}>
              <input name="tags" defaultValue={(node.tags || []).join(", ")} />
            </Label>
            <Label name={uiText("设备图标")}>
              <Choice aria-label={uiText("设备图标")} name="icon" defaultValue={node.icon || "monitor"}>
                <option value="monitor">{uiText("工作站")}</option>
                <option value="laptop">{uiText("笔记本")}</option>
                <option value="server">{uiText("服务器")}</option>
              </Choice>
            </Label>
            <div className="fields-two">
              <Label name={uiText("MAC 地址")}>
                <input name="mac" defaultValue={node.mac ?? ""} />
              </Label>
              <Label name={uiText("广播地址")}>
                <input name="broadcast" defaultValue={node.broadcast ?? ""} />
              </Label>
            </div>
            <Label name={uiText("唤醒代理")}>
              <Choice aria-label={uiText("唤醒代理")} name="wake_proxy" defaultValue={node.wake_proxy || ""}>
                <option value="">{uiText("Hub 本机")}</option>
                {nodes
                  .filter((n) => n.id !== node.id)
                  .map((n) => (
                    <option key={n.id} value={n.id}>
                      {n.alias}
                    </option>
                  ))}
              </Choice>
            </Label>
            <button className="primary">{uiText("保存节点")}</button>
            <button
              className="danger"
              type="button"
              onClick={() =>
                confirm
                  ? run(async () => {
                      await api("/nodes/" + enc(node.id), "DELETE");
                      close();
                      await reload();
                    })
                  : setConfirm(true)
              }
            >
              {confirm ? uiText("确认移除并吊销凭据") : uiText("移除节点")}
            </button>
          </form>
        </Modal>
      )}
      {modal === "service" && (
        <Modal title={service ? uiText("编辑服务") : uiText("添加服务")} close={close}>
          <form onSubmit={saveService}>
            <Label name={uiText("服务名称")}>
              <input name="name" defaultValue={service?.name} required />
            </Label>
            <Label name={uiText("服务 URL")}>
              <input
                name="url"
                type="url"
                defaultValue={service?.url}
                required
              />
            </Label>
            <Label name={uiText("关联节点")}>
              <Choice aria-label={uiText("关联节点")} name="node_id" defaultValue={service?.node_id || ""}>
                <option value="">{uiText("无")}</option>
                {nodes.map((n) => (
                  <option key={n.id} value={n.id}>
                    {n.alias}
                  </option>
                ))}
              </Choice>
            </Label>
            <Label name={uiText("关联卷")}>
              <input name="volume" defaultValue={service?.volume} />
            </Label>
            <button className="primary">{uiText("保存服务")}</button>
            {service && (
              <button
                className="danger"
                type="button"
                onClick={() =>
                  confirm
                    ? run(async () => {
                        await api("/services/" + enc(service.id), "DELETE");
                        close();
                        await reload();
                      })
                    : setConfirm(true)
                }
              >
                {confirm ? uiText("确认删除服务") : uiText("删除服务")}
              </button>
            )}
          </form>
        </Modal>
      )}
    </>
  );
}

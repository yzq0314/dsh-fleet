import { displayMacro } from "./builtinMacros";
import { uiText } from "./i18n";
import { matchTags, orderMacros } from "./macroOrder";
import { Choice } from "./Choice";
import { useEffect, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  ArrowUpRight,
  FileText,
  FolderOpen,
  GitBranch,
  Layers,
  Paperclip,
  Pencil,
  Play,
  Plus,
  SquareTerminal,
  X,
} from "lucide-react";
import {
  api,
  enc,
  type Context,
  type Macro,
  type Mode,
  type NodeInfo,
  type Run,
  type Settings,
} from "./model";
import { Label, Modal, Section } from "./shared";
export type Draft = {
  node: string;
  workspace: string;
  context?: Context;
  prompt?: string;
  macroId?: string;
  agent?: "dsh";
};
type Props = {
  nodes: NodeInfo[];
  macros: Macro[];
  settings: Settings;
  draft?: Draft;
  run: Run;
  reload: () => Promise<void>;
  onTask: (id: string) => void;
  onFiles: (node: string) => void;
};
export function Dispatch({
  nodes,
  macros,
  settings,
  draft,
  run,
  reload,
  onTask,
  onFiles,
}: Props) {
  const [mode, setMode] = useState<Mode>(
    draft ? "single" : settings.default_route,
  );
  const [selected, setSelected] = useState<string[]>(
    [draft?.node || nodes[0]?.id].filter(Boolean),
  );
  const [workspace, setWorkspace] = useState(draft?.workspace || "");
  const [agentOverride, setAgent] = useState(
    draft?.agent || (draft?.context ? "dsh" : ""),
  );
  const [desktopHandoff, setDesktopHandoff] = useState(true);
  const [prompt, setPrompt] = useState(draft?.prompt || "");
  const [tags, setTags] = useState("");
  const [context, setContext] = useState<Context | undefined>(draft?.context);
  const [edit, setEdit] = useState<Macro | null>();
  const [manage, setManage] = useState(false);
  const [macro, setMacro] = useState<Macro | null>(null);
  const [params, setParams] = useState<Record<string, string>>({});
  const [file, setFile] = useState<File | null>(null);
  const [volumeChoice, setVolumeChoice] = useState("");
  const [busy, setBusy] = useState(false);
  const [remove, setRemove] = useState(false);
  const tagFilter =
    mode === "auto"
      ? tags
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean)
      : [];
  const candidates = (
    mode === "auto" ? nodes : nodes.filter((n) => selected.includes(n.id))
  ).filter(
    (n) =>
      n.enabled !== false && tagFilter.every((tag) => n.tags.includes(tag)),
  );
  const ranked = candidates
    .filter((n) => n.online)
    .sort(
      (a, b) =>
        (a.health?.active_tasks ?? 0) - (b.health?.active_tasks ?? 0) ||
        (a.health?.cpu ?? 0) - (b.health?.cpu ?? 0) ||
        a.id.localeCompare(b.id),
    );
  const targets = mode === "auto" ? ranked.slice(0, 1) : candidates;
  const writable = (n: NodeInfo) =>
    (n.health?.volumes ?? [])
      .filter((v) => !v.read_only && v.available !== false)
      .map((v) => v.name);
  const commonVolumes = targets.length
    ? writable(targets[0]).filter((name) =>
        targets.every((n) => writable(n).includes(name)),
      )
    : [];
  const uploadVolume = commonVolumes.includes(volumeChoice)
    ? volumeChoice
    : commonVolumes.includes("inbox")
      ? "inbox"
      : commonVolumes[0] || "";
  const agent = agentOverride || targets[0]?.health?.agent || "dsh";
  const needsHandoffUpdate = targets.filter(
    (node) =>
      node.online && !node.health?.capabilities?.includes("desktop_handoff_v1"),
  );
  const spaces = [
    ...new Set(candidates.flatMap((n) => n.health?.workspaces || [])),
  ];
  const routeReady =
    mode === "auto"
      ? candidates.some((n) => n.online)
      : candidates.length > 0 && candidates.every((n) => n.online);
  const relevantTags = new Set(mode === "auto" ? tagFilter : candidates.flatMap(node => node.tags));
  const matches = (m: Macro) => matchTags(m, relevantTags);
  const ordered = orderMacros(macros, relevantTags, manage);
  const keys = macro
    ? [
        ...new Set(
          Array.from(
            macro.template.matchAll(/\{\{\s*([\w-]+)\s*\}\}/g),
            (m) => m[1],
          ),
        ),
      ]
    : [];
  useEffect(() => {
    if (!selected.length && nodes[0] && mode === "single")
      setSelected([nodes[0].id]);
  }, [nodes, mode, selected]);
  function chooseMode(m: Mode) {
    setMode(m);
    setContext(undefined);
    setFile(null);
    if (m === "single")
      setSelected([selected[0] || nodes[0]?.id].filter(Boolean));
    setWorkspace("");
  }
  function loadMacro(stored: Macro) {
    const m = displayMacro(stored);
    if (m.template.includes("{{")) {
      setMacro(m);
      setParams({target: targets.map(node => node.alias).join('、')});
    } else setPrompt(m.template);
  }
  useEffect(() => {
    const preset = macros.find((m) => m.id === draft?.macroId);
    if (preset) loadMacro(preset);
  }, [draft?.macroId]);
  function send(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    run(async () => {
      try {
        let attached = context;
        let routes = mode === "auto" ? [] : selected;
        if (file) {
          if (!uploadVolume || !targets.length)
            throw Error(
              uiText("目标节点没有共同的可写卷，请在文件页注册卷或更换节点。"),
            );
          routes = targets.map((n) => n.id);
          const path =
            Array.from(crypto.getRandomValues(new Uint32Array(4)), (x) =>
              x.toString(16).padStart(8, "0"),
            ).join("") +
            "-" +
            file.name;
          for (const target of targets) {
            const response = await fetch(
              "/api/nodes/" +
                enc(target.id) +
                "/volumes/" +
                enc(uploadVolume) +
                "/upload?path=" +
                enc(path),
              { method: "PUT", body: file },
            );
            if (!response.ok)
              throw Error(
                target.alias +
                  (" " + uiText("附件写入失败 (")) +
                  response.status +
                  uiText(")。已写入的附件可从对应卷查看。"),
              );
          }
          attached = { volume: uploadVolume, path };
        }
        const res = await api<{ tasks: string[] }>("/dispatch", "POST", {
          mode,
          node_ids: routes,
          tags: tagFilter,
          workspace,
          agent,
          ...(agent === "dsh" ? { desktop_handoff: desktopHandoff } : {}),
          prompt,
          context: attached,
        });
        await reload();
        onTask(res.tasks[0]);
      } finally {
        setBusy(false);
      }
    });
  }
  function save(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    run(async () => {
      await api(
        "/macros" + (edit ? "/" + enc(edit.id) : ""),
        edit ? "PATCH" : "POST",
        {
          name: f.get("name"),
          template: f.get("template"),
          target_tags: String(f.get("target_tags") ?? "").split(/[,，]/).map(tag => tag.trim()).filter(Boolean),
          position: edit?.position ?? ordered.length,
        },
      );
      setEdit(undefined);
      await reload();
    });
  }
  async function move(i: number, j: number) {
    const reordered = [...ordered];
    [reordered[i], reordered[j]] = [reordered[j], reordered[i]];
    for (let x = 0; x < reordered.length; x++)
      if (reordered[x].position !== x)
        await api("/macros/" + enc(reordered[x].id), "PATCH", { position: x });
    await reload();
  }
  return (
    <>
      <div className="dispatch-layout">
        <aside>
          <section className="panel macro-panel">
            <Section
              title={uiText("快捷指令")}
              count={macros.length}
              action={
                <button
                  className="action subtle"
                  onClick={() => setManage(!manage)}
                >
                  <Pencil size={14} />
                  {manage ? uiText("完成") : uiText("管理")}
                </button>
              }
            />
            <div className="macro-grid">
              {ordered.map((m, i) => {
                const Icon = [FileText, GitBranch, SquareTerminal, Layers][
                  i % 4
                ];
                return (
                  <div key={m.id} className="macro panel">
                    <button onClick={() => loadMacro(m)}>
                      <Icon size={18} />
                      <strong>{displayMacro(m).name}{matches(m).length > 0 && <small className="badge idle">{matches(m).join(' · ')}</small>}</strong>
                      <ArrowUpRight size={15} />
                    </button>
                    {manage && (
                      <div className="row">
                        <button
                          className="icon"
                          aria-label={(uiText("编辑") + " ") + displayMacro(m).name}
                          onClick={() => {
                            setEdit(m);
                            setRemove(false);
                          }}
                        >
                          <Pencil size={14} />
                        </button>
                        <button
                          className="icon"
                          aria-label={uiText("向前移动")}
                          disabled={i === 0}
                          onClick={() => run(() => move(i, i - 1))}
                        >
                          <ArrowUp size={14} />
                        </button>
                        <button
                          className="icon"
                          aria-label={uiText("向后移动")}
                          disabled={i === ordered.length - 1}
                          onClick={() => run(() => move(i, i + 1))}
                        >
                          <ArrowDown size={14} />
                        </button>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
            {manage && (
              <button className="action" onClick={() => setEdit(null)}>
                <Plus size={15} />{uiText("新建指令")}</button>
            )}
          </section>
          <section className="panel route-preview">
            <h3>{uiText("候选设备")}</h3>
            {nodes.map((n) => (
              <button
                key={n.id}
                className="candidate"
                type="button"
                disabled={n.enabled === false}
                aria-pressed={mode === "single" && selected[0] === n.id}
                onClick={() => {
                  chooseMode("single");
                  setSelected([n.id]);
                }}
              >
                <i className={"dot " + (n.online ? "" : "off")} />
                <b>{n.alias}</b>
                <small>
                  {n.online && n.rtt != null
                    ? Math.round(n.rtt) + " ms"
                    : n.online
                      ? uiText("在线")
                      : uiText("离线")}
                </small>
                <span
                  className="load-line"
                  title={"CPU " + (n.health?.cpu ?? "—") + "%"}
                >
                  <i style={{ width: (n.health?.cpu ?? 0) + "%" }} />
                </span>
              </button>
            ))}
          </section>
        </aside>
        <section>
          <Section title={uiText("新任务")} />
          <form className="panel compose" onSubmit={send}>
            <Label name={uiText("派单模式")}>
              <Choice
                aria-label={uiText("派单模式")}
                value={mode}
                onChange={(e) => chooseMode(e.target.value as Mode)}
              >
                <option value="single">{uiText("指定单机")}</option>
                <option value="auto">{uiText("Auto · 自动选择")}</option>
                <option value="fanout">{uiText("Fanout · 多机并行")}</option>
              </Choice>
            </Label>
            {mode === "single" ? (
              <Label name={uiText("目标节点")}>
                <Choice
                  aria-label={uiText("目标节点")}
                  value={selected[0] || ""}
                  onChange={(e) => {
                    setSelected([e.target.value]);
                    setWorkspace("");
                    setContext(undefined);
                    setFile(null);
                  }}
                >
                  {nodes.map((n) => (
                    <option key={n.id} value={n.id}>
                      {n.alias}
                    </option>
                  ))}
                </Choice>
              </Label>
            ) : mode === "fanout" ? (
              <fieldset>
                <legend>{uiText("目标节点")}</legend>
                <div className="chips">
                  {nodes.map((n) => (
                    <label key={n.id}>
                      <input
                        type="checkbox"
                        checked={selected.includes(n.id)}
                        onChange={(e) =>
                          setSelected(
                            e.target.checked
                              ? [...selected, n.id]
                              : selected.filter((s) => s !== n.id),
                          )
                        }
                      />
                      {n.alias}
                    </label>
                  ))}
                </div>
              </fieldset>
            ) : (
              <Label name={uiText("标签过滤")}>
                <input
                  value={tags}
                  onChange={(e) => setTags(e.target.value)}
                  placeholder={uiText("用逗号分隔")}
                />
              </Label>
            )}
            <Label name={uiText("工作目录")}>
              <input
                list="workspaces"
                autoComplete="off"
                value={workspace}
                onChange={(e) => setWorkspace(e.target.value)}
                placeholder={uiText("默认使用各节点首个可信目录")}
              />
            </Label>
            <datalist id="workspaces">
              {spaces.map((s) => (
                <option key={s} value={s} />
              ))}
            </datalist>
            <Label name="Agent">
              <Choice
                aria-label="Agent"
                value={agent}
                onChange={(e) => setAgent(e.target.value)}
              >
                <option value="mock">Mock Agent</option>
                <option value="dsh">DSH · DeepSeek Harness</option>
              </Choice>
            </Label>
            {agent === "dsh" && (
              <label className="handoff-option">
                <input
                  type="checkbox"
                  checked={desktopHandoff}
                  onChange={(e) => setDesktopHandoff(e.target.checked)}
                />
                <span>
                  <strong>{uiText("在 DSH App 中续聊")}</strong>
                  <small>
                    {needsHandoffUpdate.length
                      ? uiText("{0} 的 Sidecar 需更新后才能交接。", needsHandoffUpdate.map((n) => n.alias).join("、"))
                      : uiText("任务完成后，将对话交给目标电脑上的桌面插件。")}
                  </small>
                </span>
              </label>
            )}
            <Label name={uiText("任务指令")}>
              <textarea
                rows={4}
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                required
                placeholder={uiText("描述希望 Agent 完成的工作…")}
              />
            </Label>
            {
              <>
                <div className="dispatch-bar">
                  <label className="action file-input">
                    <Paperclip size={15} />{uiText("添加附件")}<input
                      aria-label={uiText("添加任务附件")}
                      type="file"
                      onChange={(e) => {
                        setFile(e.target.files?.[0] || null);
                        setContext(undefined);
                      }}
                    />
                  </label>
                  {mode === "single" && (
                    <button
                      className="action"
                      type="button"
                      onClick={() => onFiles(selected[0])}
                    >
                      <FolderOpen size={15} />{uiText("浏览卷")}</button>
                  )}
                  <button
                    className="primary"
                    disabled={
                      busy ||
                      !prompt.trim() ||
                      !routeReady ||
                      (!!file && !uploadVolume) ||
                      (mode !== "auto" && !selected.length)
                    }
                  >
                    <Play size={16} />
                    {busy ? uiText("正在派发…") : uiText("派发任务")}
                  </button>
                </div>
                {(file || context) && (
                  <div className="context">
                    <span>
                      {file?.name || context?.volume + "/" + context?.path}
                    </span>
                    <button
                      type="button"
                      className="icon"
                      onClick={() => {
                        setFile(null);
                        setContext(undefined);
                      }}
                      aria-label={uiText("移除上下文")}
                    >
                      <X size={14} />
                    </button>
                  </div>
                )}
                {file && (
                  <>
                    <Label name={uiText("附件目标卷")}>
                      <Choice
                        aria-label={uiText("附件目标卷")}
                        value={uploadVolume}
                        onChange={(e) => setVolumeChoice(e.target.value)}
                      >
                        {commonVolumes.map((v) => (
                          <option key={v}>{v}</option>
                        ))}
                      </Choice>
                    </Label>
                    <small>
                      {uploadVolume
                        ? (uiText("接收节点 ·") + " ") + targets.map((n) => n.alias).join("、")
                        : uiText("目标节点没有共同的可写卷，请在文件页注册卷或更换节点。")}
                    </small>
                  </>
                )}
              </>
            }
          </form>
        </section>
      </div>
      {macro && (
        <Modal title={macro.name} close={() => setMacro(null)}>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              setPrompt(
                macro.template.replace(
                  /\{\{\s*([\w-]+)\s*\}\}/g,
                  (_, k) => params[k] ?? "",
                ),
              );
              setMacro(null);
            }}
          >
            {keys.map((k) => (
              <Label key={k} name={k}>
                <input
                  required
                  value={params[k] || ""}
                  onChange={(e) =>
                    setParams({ ...params, [k]: e.target.value })
                  }
                />
              </Label>
            ))}
            <button className="primary">{uiText("应用模板")}</button>
          </form>
        </Modal>
      )}
      {edit !== undefined && (
        <Modal
          title={edit ? uiText("编辑快捷指令") : uiText("新建快捷指令")}
          close={() => setEdit(undefined)}
        >
          <form onSubmit={save}>
            <Label name={uiText("名称")}>
              <input name="name" defaultValue={edit?.name} required />
            </Label>
            <Label name={uiText("设备标签")}><input name="target_tags" defaultValue={edit?.target_tags?.join(", ")} placeholder={uiText("多个标签用逗号分隔")} /></Label>
            <Label name={uiText("模板内容")}>
              <textarea
                name="template"
                defaultValue={edit?.template}
                required
                rows={5}
                placeholder={uiText("用 {{repo}} 表示参数")}
              />
            </Label>
            <button className="primary">{uiText("保存指令")}</button>
            {edit && (
              <button
                type="button"
                className="danger"
                onClick={() =>
                  remove
                    ? run(async () => {
                        await api("/macros/" + enc(edit.id), "DELETE");
                        setEdit(undefined);
                        await reload();
                      })
                    : setRemove(true)
                }
              >
                {remove ? uiText("确认删除指令") : uiText("删除指令")}
              </button>
            )}
          </form>
        </Modal>
      )}
    </>
  );
}

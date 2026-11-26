import { uiText } from "./i18n";
import { Choice } from "./Choice";
import { useEffect, useMemo, useState } from "react";
import { Check, ChevronDown, Download, FileBox, HardDrive } from "lucide-react";
import { api, enc, type NodeInfo, type TaskEvent, type Task } from "./model";
import { Label, Modal, displayBytes } from "./shared";
import { fileEndpoint, FilePreview } from "./FilePreview";
import type { Volume } from "./VolumeEditor";
type Artifact = { volume: string; path: string; name: string; size?: number };
function copyKey(a: Artifact) {
  return JSON.stringify([a.volume, a.path]);
}
function Transfer({
  task,
  file,
  nodes,
  close,
  saved,
}: {
  task: Task;
  file: Artifact;
  nodes: NodeInfo[];
  close: () => void;
  saved: (d: string) => void;
}) {
  const online = nodes.filter((n) => n.online && n.enabled !== false);
  const [node, setNode] = useState(
    online.find((n) => n.tags.includes("nas") || n.tags.includes("nas-bridge"))
      ?.id ||
      online[0]?.id ||
      "",
  );
  const [volumes, setVolumes] = useState<Volume[]>([]);
  const [volume, setVolume] = useState("");
  const [path, setPath] = useState(file.name);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true;
    setVolumes([]);
    setVolume("");
    setError("");
    if (node)
      api<(Volume & { available?: boolean })[]>(
        "/nodes/" + enc(node) + "/volumes",
      )
        .then((all) => {
          if (active) {
            const writable = all.filter(
              (v) => !v.read_only && v.available !== false,
            );
            setVolumes(writable);
            setVolume(
              writable.find((v) => v.name === "nas")?.name ||
                writable[0]?.name ||
                "",
            );
          }
        })
        .catch((e) => {
          if (active) setError(e.message);
        });
    return () => {
      active = false;
    };
  }, [node]);
  return (
    <Modal
      title={uiText("转存到 NAS 或挂载卷")}
      close={() => {
        if (!busy) close();
      }}
    >
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          if (busy) return;
          setBusy(true);
          setError("");
          try {
            await api("/tasks/" + enc(task.id) + "/artifacts/copy", "POST", {
              source_volume: file.volume,
              source_path: file.path,
              target_node: node,
              target_volume: volume,
              target_path: path,
            });
            saved(
              (nodes.find((n) => n.id === node)?.alias || node) +
                " / " +
                volume +
                " / " +
                path,
            );
            close();
          } catch (e) {
            setError(e instanceof Error ? e.message : uiText("转存失败"));
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="file-source">
          <FileBox size={18} />
          <span>{file.name}</span>
        </div>
        <Label name={uiText("目标设备")}>
          <Choice
            aria-label={uiText("目标设备")}
            disabled={busy}
            value={node}
            onChange={(e) => setNode(e.target.value)}
          >
            {online.map((n) => (
              <option key={n.id} value={n.id}>
                {n.alias}
              </option>
            ))}
          </Choice>
        </Label>
        <Label name={uiText("目标可写卷")}>
          <Choice
            aria-label={uiText("目标可写卷")}
            disabled={busy}
            value={volume}
            onChange={(e) => setVolume(e.target.value)}
          >
            {!volumes.length && <option value="">{uiText("没有可用的可写卷")}</option>}
            {volumes.map((v) => (
              <option key={v.name}>{v.name}</option>
            ))}
          </Choice>
        </Label>
        <Label name={uiText("卷内保存路径")}>
          <input
            required
            disabled={busy}
            value={path}
            onChange={(e) => setPath(e.target.value)}
          />
        </Label>
        <small>{uiText("同名文件将保留，可更改目标文件名。")}</small>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        <button
          className="primary"
          disabled={busy || !node || !volume || !path.trim()}
        >
          <HardDrive size={16} />
          {busy ? uiText("正在转存…") : uiText("确认转存")}
        </button>
      </form>
    </Modal>
  );
}
export function Artifacts({
  events,
  nodes,
  task,
}: {
  events: TaskEvent[];
  nodes: NodeInfo[];
  task: Task;
}) {
  const files = useMemo(() => {
    const unique = new Map<string, Artifact>();
    for (const { type, data } of events) {
      if (
        type !== "artifact" ||
        typeof data?.volume !== "string" ||
        typeof data?.path !== "string"
      )
        continue;
      const file = {
        volume: data.volume,
        path: data.path,
        name: data.name || data.path.split("/").pop(),
        size: data.size,
      };
      unique.set(copyKey(file), file);
    }
    return [...unique.values()];
  }, [events]);
  const [active, setActive] = useState("");
  const [target, setTarget] = useState<Artifact | null>(null);
  const [notice, setNotice] = useState("");
  const storageKey = "fleet:artifacts:" + task.id;
  const [preference, setPreference] = useState<boolean | null>(() => {
    try {
      const saved = sessionStorage.getItem(storageKey);
      return saved === null ? null : saved === "open";
    } catch { return null; }
  });
  const expanded = preference ?? files.length === 1;
  function toggle() {
    setPreference(!expanded);
    try { sessionStorage.setItem(storageKey, expanded ? "closed" : "open"); } catch { /* Private browsing may disable storage. */ }
  }
  if (!files.length) return null;
  return (
    <section className="artifact-section" aria-label={uiText("任务产物")}>
      <button className="artifact-summary" type="button" onClick={toggle} aria-expanded={expanded} aria-label={uiText("任务产物")}>
        <FileBox size={18}/><b>{uiText("任务产物")}</b><span className="count">{files.length}</span>
        {!expanded && <span className="artifact-chips" aria-hidden="true">
          {files.slice(0, 2).map(f => <span key={copyKey(f)}>{f.name}</span>)}
          {files.length > 2 && <small>+{files.length - 2}</small>}
        </span>}
        <ChevronDown className="disclosure-arrow" size={17}/>
      </button>
      {notice && (
        <p className="transfer-success" role="status">
          <Check size={15} />{uiText("已转存至") + " "}{notice}
        </p>
      )}
      {expanded && files.map((file) => {
        const key = copyKey(file);
        const open = active === key;
        return (
          <article className="artifact" key={key} aria-label={file.name}>
            <div className="artifact-row">
            <button
              className="artifact-head"
              aria-expanded={open}
              onClick={() => setActive(open ? "" : key)}
            >
              <span className="device-icon">
                <FileBox size={18} />
              </span>
              <span className="grow">
                <strong>{file.name}</strong>
                <small>
                  {file.volume}
                  {file.size != null ? " · " + displayBytes(file.size) : ""}
                </small>
              </span>
              <ChevronDown size={16} />
            </button>
            <div className="artifact-actions">
              <a
                href={fileEndpoint(
                  task.node_id,
                  file.volume,
                  file.path,
                  "download",
                )}
                download={file.name} aria-label={uiText("下载文件")} title={uiText("下载文件")}
              >
                <Download size={15} />
              </a>
              <button aria-label={uiText("转存 NAS")} title={uiText("转存 NAS")} onClick={() => setTarget(file)}>
                <HardDrive size={15} />
              </button>
            </div>
            </div>
            {open && (
              <div className="artifact-preview">
                <FilePreview
                  node={task.node_id}
                  volume={file.volume}
                  path={file.path}
                />
              </div>
            )}

          </article>
        );
      })}
      {target && (
        <Transfer
          file={target}
          nodes={nodes}
          task={task}
          close={() => setTarget(null)}
          saved={setNotice}
        />
      )}
    </section>
  );
}

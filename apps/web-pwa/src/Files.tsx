import { uiText } from "./i18n";
import { Choice } from "./Choice";
import { useEffect, useState } from "react";
import {
  ArrowLeft,
  Download,
  File,
  Folder,
  FolderOpen,
  Search,
  X,
  Eye,
  Send,
  Upload,
} from "lucide-react";
import {
  api,
  enc,
  type Context,
  type NodeInfo,
  type Run,
  type FileEntry,
} from "./model";
import { Empty, Label, Modal, Section, displayBytes } from "./shared";
import { FilePreview } from "./FilePreview";
import { VolumeEditor, type Volume } from "./VolumeEditor";
export function Files({
  nodes,
  initialNode,
  initialVolume,
  initialPath,
  onDispatch,
  run,
}: {
  nodes: NodeInfo[];
  initialNode: string;
  initialVolume: string;
  initialPath: string;
  onDispatch: (n: string, c: Context) => void;
  run: Run;
}) {
  const [node, setNode] = useState(initialNode || nodes[0]?.id || "");
  const [volumes, setVolumes] = useState<Volume[]>([]);
  const [volume, setVolume] = useState(initialVolume);
  const [path, setPath] = useState("");
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [entries, setEntries] = useState<FileEntry[]>([]);
  const [version, setVersion] = useState(0);
  const [file, setFile] = useState<Pick<FileEntry, "name" | "path"> | null>(
    initialPath
      ? { path: initialPath, name: initialPath.split("/").pop() || initialPath }
      : null,
  );
  const [problem, setProblem] = useState("");
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [registry, setRegistry] = useState(0);
  const updated = (next?: string) => {
    setRegistry((i) => i + 1);
    setVolume(next || "");
    openDir("");
  };
  const root = "/nodes/" + enc(node) + "/volumes/" + enc(volume);
  const fileUrl = file
    ? "/api" + root + "/download?path=" + enc(file.path)
    : "";
  const ro = volumes.find((v) => v.name === volume)?.read_only;
  useEffect(() => {
    let active = true;
    setVolumes([]);
    setProblem("");
    if (node)
      api<Volume[]>("/nodes/" + enc(node) + "/volumes")
        .then((result) => {
          if (active) {
            setVolumes(result);
            setVolume((v) =>
              result.some((x) => x.name === v) ? v : result[0]?.name || "",
            );
          }
        })
        .catch((e) => {
          if (active) setProblem(e.message);
        });
    return () => {
      active = false;
    };
  }, [node, registry]);
  useEffect(() => {
    let active = true;
    setEntries([]);
    if (!node || !volume || !volumes.some((v) => v.name === volume)) return;
    setLoading(true);
    setProblem("");
    api<{ entries: FileEntry[] }>(
      root +
        "/" +
        (search ? "search" : "list") +
        "?path=" +
        enc(path) +
        "&q=" +
        enc(search),
    )
      .then((r) => {
        if (active) setEntries(r.entries);
      })
      .catch((e) => {
        if (active) setProblem(e.message);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [node, volume, volumes, path, search, version]);
  function openDir(p: string) {
    setPath(p);
    setSearch("");
    setQuery("");
    setFile(null);
  }
  return (
    <>
      <Section title={uiText("存储位置")} />
      <div className="panel location">
        <div className="fields-two">
          <Label name={uiText("文件节点")}>
            <Choice
              popover
              aria-label={uiText("文件节点")}
              value={node}
              onChange={(e) => {
                if (e.target.value === node) return;
                setVolumes([]);
                setEntries([]);
                setLoading(false);
                setNode(e.target.value);
                setVolume("");
                openDir("");
              }}
            >
              {nodes.map((n) => (
                <option key={n.id} value={n.id}>
                  {n.alias}
                </option>
              ))}
            </Choice>
          </Label>
          <Label name={uiText("挂载卷")}>
            <Choice
              popover
              aria-label={uiText("挂载卷")}
              value={volume}
              onChange={(e) => {
                setVolume(e.target.value);
                openDir("");
              }}
            >
              {volumes.map((v) => (
                <option key={v.name}>{v.name}</option>
              ))}
            </Choice>
          </Label>
        </div>
        {volume && (
          <small>
            <FolderOpen size={15} />
            {volume} · {ro ? uiText("只读卷") : uiText("可读写")}
          </small>
        )}
        <div className="row wrap volume-actions">
          {volume && !ro ? (
            <label className="action subtle file-input">
              <Upload size={15} />
              {uploading ? uiText("上传中…") : uiText("上传文件")}
              <input
                aria-label={uiText("上传文件")}
                type="file"
                disabled={uploading}
                onChange={(e) => {
                  const el = e.currentTarget;
                  const f = el.files?.[0];
                  if (f) {
                    setUploading(true);
                    run(async () => {
                      try {
                        const target = [path, f.name].filter(Boolean).join("/");
                        const r = await fetch(
                          "/api" + root + "/upload?path=" + enc(target),
                          { method: "PUT", body: f },
                        );
                        if (!r.ok) throw Error(uiText("上传失败 (") + r.status + ")");
                        setVersion((v) => v + 1);
                      } finally {
                        setUploading(false);
                        el.value = "";
                      }
                    });
                  }
                }}
              />
            </label>
          ) : undefined}
          <VolumeEditor node={node} run={run} updated={updated} />
          {volumes.find((v) => v.name === volume) && (
            <VolumeEditor
              node={node}
              volume={volumes.find((v) => v.name === volume)}
              run={run}
              updated={updated}
            />
          )}
        </div>
      </div>
      <Section
        title={search ? uiText("搜索结果") : uiText("卷内文件")}
        count={entries.length}
      />
      <form
        className="search"
        onSubmit={(e) => {
          e.preventDefault();
          setSearch(query.trim());
        }}
      >
        <input
          aria-label={uiText("搜索文件")}
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            if (!e.target.value) setSearch("");
          }}
          placeholder={uiText("按名称搜索文件")}
        />
        {query && (
          <button
            type="button"
            className="icon"
            aria-label={uiText("清空搜索")}
            onClick={() => {
              setQuery("");
              setSearch("");
            }}
          >
            <X size={14} />
          </button>
        )}
        <button className="icon search-go" aria-label={uiText("搜索")} type="submit">
          <Search size={17} />
        </button>
      </form>
      {volume && (
        <div className="path">
          <button
            className="icon"
            aria-label={uiText("上级目录")}
            disabled={!path}
            onClick={() => openDir(path.split("/").slice(0, -1).join("/"))}
          >
            <ArrowLeft size={16} />
          </button>
          <code>
            {volume}/{path}
          </code>
          <button
            className="icon"
            aria-label={uiText("用当前目录派单")}
            onClick={() => onDispatch(node, { volume, path })}
          >
            <Send size={16} />
          </button>
        </div>
      )}
      <div className="panel file-list">
        {problem ? (
          <p role="alert">{problem}</p>
        ) : (
          entries.map((e) => (
            <div className="file-row" key={e.path}>
              <button onClick={() => (e.is_dir ? openDir(e.path) : setFile(e))}>
                {e.is_dir ? <Folder size={21} /> : <File size={21} />}
                <span>
                  <strong>{e.name}</strong>
                  <small>{e.is_dir ? uiText("目录") : displayBytes(e.size)}</small>
                </span>
              </button>
              {!e.is_dir && (
                <>
                  <button
                    className="icon file-quick"
                    aria-label={(uiText("预览") + " ") + e.name}
                    onClick={() => setFile(e)}
                  >
                    <Eye size={15} />
                  </button>
                  <a
                    className="icon file-quick"
                    aria-label={(uiText("下载") + " ") + e.name}
                    href={"/api" + root + "/download?path=" + enc(e.path)}
                    download={e.name}
                  >
                    <Download size={15} />
                  </a>
                </>
              )}
              <button
                className="icon file-quick"
                aria-label={(uiText("交给 DSH") + " ") + e.name}
                onClick={() => onDispatch(node, { volume, path: e.path })}
              >
                <Send size={15} />
              </button>
            </div>
          ))
        )}
        {!problem && !entries.length && (
          <Empty>
            {loading ? uiText("正在读取…") : search ? uiText("没有匹配的文件") : uiText("卷内暂无文件")}
          </Empty>
        )}
      </div>
      {file && (
        <Modal title={file.name} close={() => setFile(null)}>
          <div className="dialog-body">
            <FilePreview node={node} volume={volume} path={file.path} />
            <div className="row wrap">
              <a className="action" href={fileUrl} download={file.name}>
                <Download size={16} />{uiText("下载文件")}</a>
              <button
                className="action subtle"
                onClick={() => onDispatch(node, { volume, path: file.path })}
              >
                <Send size={16} />{uiText("交给 DSH 处理")}</button>
            </div>
          </div>
        </Modal>
      )}
    </>
  );
}

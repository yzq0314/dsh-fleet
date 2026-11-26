import { currentLocale, uiText } from "./i18n";
import { cleanMessage } from "./cleanMessage";
import { Choice } from "./Choice";
import { useEffect, useRef, useState } from "react";
import {
  ChevronRight,
  Trash2,
  Search,
  ListChecks,
  X,
  Undo2,
  Square,
  Check,
  Save,
} from "lucide-react";
import { CopyButton } from "./CopyButton";
import { TaskConversation } from "./TaskConversation";
import {
  api,
  enc,
  display,
  type Task,
  type TaskEvent,
  type NodeInfo,
  type Approval,
  type Run,
} from "./model";
import { Artifacts } from "./Artifacts";
import { Empty, Section } from "./shared";
const statuses: Record<string, string> = {
  pending: "等待中",
  queued: "已排队",
  starting: "启动中",
  running: "运行中",
  awaiting_approval: "等待审批",
  waiting_approval: "等待审批",
  approval_required: "等待审批",
  completed: "已完成",
  done: "已完成",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已中止",
  canceled: "已中止",
  denied: "已拒绝",
};
const handoffReasons: Record<string, string> = {
  desktop_unavailable:
    "请确认该节点正在使用桌面模型，DSH App 已运行且桌面插件支持会话交接。",
  history_too_large: "这段对话超过桌面交接的大小限制，完整任务记录可在此查看。",
  unsupported_history:
    "这段对话包含当前导入器无法还原的消息，任务记录可在此查看。",
  desktop_handoff_failed: "桌面未确认保存会话，请检查目标电脑上的 DSH App。",
};
export function Tasks({
  approvals,
  tasks,
  nodes,
  selected,
  onSelect,
  tick,
  run,
  reload,
}: {
  approvals: Approval[];
  tasks: Task[];
  nodes: NodeInfo[];
  selected: string;
  onSelect: (s: string) => void;
  tick: number;
  run: Run;
  reload: () => Promise<void>;
}) {
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const lastEvent = useRef(0);
  const refreshEvents = useRef<() => void>();
  const [confirm, setConfirm] = useState(false);
  const [error, setError] = useState("");
  const [machine, setMachine] = useState("");
  const [groupBy, setGroupBy] = useState("machine");
  const [status, setStatus] = useState("");
  const [multi, setMulti] = useState(false);
  const [trashCount, setTrashCount] = useState<number | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const [removed, setRemoved] = useState(false);
  const [pages, setPages] = useState(1);
  const [history, setHistory] = useState<Task[]>([]);
  const [mayLoad, setMayLoad] = useState(false);
  const [listingRevision, refreshListing] = useState(0);
  useEffect(() => {
    let current = true;
    if (removed || pages > 1)
      void (async () => {
        const rows: Task[] = [];
        let batch: Task[] = [];
        for (let i = removed ? 0 : 1; i < pages; i++) {
          batch = await api<Task[]>(
            "/tasks?removed=" + removed + "&offset=" + i * 500,
          );
          if (!current) return;
          rows.push(...batch);
          if (batch.length < 500) break;
        }
        setHistory(rows);
        setMayLoad(batch.length === 500);
      })().catch((e) => {
        if (current) setError(e.message);
      });
    return () => {
      current = false;
    };
  }, [removed, pages, listingRevision]);
  useEffect(() => {
    let active = true;
    api<Task[]>("/tasks?removed=true&offset=0")
      .then((rows) => {
        if (active) setTrashCount(rows.length);
      })
      .catch(() => {
        if (active) setTrashCount(null);
      });
    return () => {
      active = false;
    };
  }, [listingRevision, removed]);
  const all = [
    ...new Map(
      [...(removed ? [] : tasks), ...(removed || pages > 1 ? history : [])].map(
        (x) => [x.id, x],
      ),
    ).values(),
  ];
  const t = all.find((x) => x.id === selected);
  const handoff = [...events]
    .reverse()
    .find((event) => event.type === "desktop_handoff")?.data;
  useEffect(() => {
    setConfirm(false);
    setEvents([]);
    lastEvent.current = 0;
    setError("");
  }, [selected]);
  useEffect(() => {
    let current = true;
    if (!selected) return;
    let loading = false,
      queued = false;
    async function load() {
      let after = lastEvent.current;
      while (current) {
        const batch = await api<TaskEvent[]>(
          "/tasks/" + enc(selected) + "/events?after=" + after,
        );
        if (!current) return;
        if (batch.length) {
          after = batch[batch.length - 1].id;
          lastEvent.current = after;
          setEvents((prior) => [...prior, ...batch]);
        }
        if (batch.length < 2000) break;
      }
      if (current) setError("");
    }
    function refresh() {
      if (loading) {
        queued = true;
        return;
      }
      loading = true;
      void load()
        .catch((e) => {
          if (current) setError(e.message);
        })
        .finally(() => {
          loading = false;
          if (current && queued) {
            queued = false;
            refresh();
          }
        });
    }
    refreshEvents.current = refresh;
    refresh();
    return () => {
      current = false;
      refreshEvents.current = undefined;
    };
  }, [selected]);
  useEffect(() => {
    refreshEvents.current?.();
  }, [tick]);
  const groups = new Map<string, Task[]>();
  const aliases = new Map(nodes.map((n) => [n.id, n.alias]));
  const pending = new Set(approvals.map((a) => a.task_id));
  const statusKey = (task: Task) =>
    pending.has(task.id) ? "等待审批" : statuses[task.status] || task.status;
  const statusLabel = (task: Task) => uiText(statusKey(task));
  for (const task of all) {
    if (status && statusKey(task) !== status) continue;
    if (machine && task.node_id !== machine) continue;
    if (
      search &&
      !(task.prompt + " " + (task.folder ?? ""))
        .toLowerCase()
        .includes(search.toLowerCase())
    )
      continue;
    const key =
      groupBy === "machine"
        ? aliases.get(task.node_id) || task.node_id
        : task.folder || uiText("未分组");
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(task);
  }
  const visible = [...groups.values()].flat();
  const eligible = visible.filter((x) =>
    ["failed", "completed", "cancelled"].includes(x.status),
  );
  const eligibleIds = new Set(eligible.map((task) => task.id));
  function toggleTask(id: string) {
    if (eligibleIds.has(id))
      setPicked((previous) =>
        previous.includes(id)
          ? previous.filter((item) => item !== id)
          : [...previous, id],
      );
  }
  async function move(ids: string[]) {
    try {
      for (const id of ids)
        await api("/tasks/" + enc(id) + "/filing", "PATCH", { removed: true });
    } finally {
      setPicked([]);
      onSelect("");
      refreshListing((v) => v + 1);
      await reload();
    }
  }
  return (
    <>
      <Section title={removed ? uiText("最近删除") : uiText("任务队列")} count={all.length} />
      <div className={"task-browser " + (t ? "focused" : "")}>
        <div className="queue-toolbar">
          <div className="seg-tabs">
            {["", "运行中", "等待审批", "已完成", "失败"].map((key) => (
              <button
                key={key}
                aria-pressed={status === key}
                onClick={() => {
                  setStatus(key);
                  setPicked([]);
                  onSelect("");
                }}
              >
                {uiText(key === "等待审批" ? "待审批" : key || "全部")}
              </button>
            ))}
          </div>
          <Choice
            popover
            aria-label={uiText("设备")}
            value={machine}
            onChange={(e) => {
              setMachine(e.target.value);
              onSelect("");
            }}
          >
            <option value="">{uiText("全部设备")}</option>
            {nodes.map((n) => (
              <option key={n.id} value={n.id}>
                {n.alias}
              </option>
            ))}
          </Choice>
          <Choice
            aria-label={uiText("归类")}
            value={groupBy}
            onChange={(e) => setGroupBy(e.target.value)}
          >
            <option value="machine">{uiText("按设备")}</option>
            <option value="folder">{uiText("按项目")}</option>
          </Choice>
          <label className="queue-search">
            <Search size={15} />
            <input
              aria-label={uiText("搜索任务")}
              type="search"
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                onSelect("");
              }}
              placeholder={uiText("搜索任务")}
            />
          </label>
          {!removed && (
            <button
              className="btn-sm"
              aria-pressed={multi}
              onClick={() => {
                setMulti(!multi);
                setPicked([]);
                onSelect("");
              }}
            >
              <ListChecks size={15} />{uiText("多选")}</button>
          )}
          <button
            className="btn-icon trash-toggle"
            title={removed ? uiText("返回任务队列") : uiText("最近删除")}
            aria-label={removed ? uiText("返回任务队列") : uiText("最近删除")}
            onClick={() => {
              onSelect("");
              setMulti(false);
              setPicked([]);
              setHistory([]);
              setPages(1);
              setMayLoad(false);
              setRemoved(!removed);
            }}
          >
            {removed ? <Undo2 size={15} /> : <Trash2 size={15} />}
            <sup>
              {trashCount === null
                ? "…"
                : trashCount === 500
                  ? "500+"
                  : trashCount}
            </sup>
          </button>
        </div>
        {multi && (
          <div className="batch-bar">
            <button onClick={() => setPicked([...eligibleIds])}>{uiText("全选")}</button>
            <button
              onClick={() =>
                setPicked(
                  eligible
                    .filter((x) => x.status === "failed")
                    .map((x) => x.id),
                )
              }
            >{uiText("勾选失败项")}</button>
            <button
              disabled={!picked.some((id) => eligibleIds.has(id))}
              onClick={() =>
                run(async () => {
                  await move(picked.filter((id) => eligibleIds.has(id)));
                  setMulti(false);
                })
              }
            >{uiText("移入最近删除 (")}{picked.filter((id) => eligibleIds.has(id)).length})
            </button>
            <button
              onClick={() => {
                setMulti(false);
                setPicked([]);
              }}
            >{uiText("退出多选")}</button>
          </div>
        )}
        <div className="queue-workspace">
          <div className="queue-list">
            {!groups.size && (
              <Empty>{removed ? uiText("没有已删除的任务") : uiText("没有匹配的任务")}</Empty>
            )}
            {[...groups].map(([id, g]) => (
              <div className="task-group" key={id}>
                <h3>
                  {id} <small>{g.length}</small>
                </h3>
                <div className="task-grid">
                  {g.map((x) => (
                    <article
                      key={x.id}
                      data-status={x.status}
                      className={
                        "panel task " + (selected === x.id ? "selected" : "")
                      }
                    >
                      {multi && (
                        <button
                          className="task-check"
                          role="checkbox"
                          aria-checked={picked.includes(x.id)}
                          disabled={!eligibleIds.has(x.id)}
                          aria-label={(uiText("选择任务") + " ") + cleanMessage(x.prompt)}
                          onClick={() => toggleTask(x.id)}
                        >
                          <span>
                            {picked.includes(x.id) && <Check size={12} />}
                          </span>
                        </button>
                      )}
                      <button
                        className="task-head"
                        onClick={() =>
                          multi
                            ? toggleTask(x.id)
                            : onSelect(selected === x.id ? "" : x.id)
                        }
                      >
                        <span className="grow">
                          <strong>
                            {cleanMessage(x.prompt) || uiText("桌面任务")}
                          </strong>
                          <small>
                            {nodes.find((n) => n.id === x.node_id)?.alias ||
                              x.node_id}{" "}
                            · {statusLabel(x)}
                          </small>
                        </span>
                        {x.created && (
                          <time>
                            {new Date(x.created * 1000).toLocaleTimeString(currentLocale(), {
                              hour: "2-digit",
                              minute: "2-digit",
                            })}
                          </time>
                        )}
                        <ChevronRight size={15} />
                      </button>
                    </article>
                  ))}
                </div>
              </div>
            ))}
            {((!removed && pages === 1 && tasks.length === 500) || mayLoad) && (
              <button className="action" onClick={() => setPages((p) => p + 1)}>{uiText("加载更早的任务")}</button>
            )}
          </div>
          {t && (
            <article className="panel task-detail" key={t.id}>
              <header className="detail-title">
                <h3>{cleanMessage(t.prompt) || uiText("桌面任务")}</h3>
                <span className="badge">{statusLabel(t)}</span>
                <button
                  className="btn-icon"
                  title={uiText("返回多栏")}
                  aria-label={uiText("返回多栏")}
                  onClick={() => onSelect("")}
                >
                  <X size={17} />
                </button>
              </header>
              <div className="detail-tools">
                <form
                  className="filing"
                  key={t.id + "-" + t.folder}
                  onSubmit={(e) => {
                    e.preventDefault();
                    const folder = String(
                      new FormData(e.currentTarget).get("folder") || "",
                    );
                    run(async () => {
                      await api("/tasks/" + enc(t.id) + "/filing", "PATCH", {
                        folder,
                      });
                      refreshListing((v) => v + 1);
                      await reload();
                    });
                  }}
                >
                  <label>
                    <input
                      aria-label={uiText("项目分组")}
                      name="folder"
                      maxLength={64}
                      defaultValue={t.folder || ""}
                      placeholder={uiText("未分组")}
                    />
                  </label>
                  <button
                    className="btn-icon"
                    aria-label={uiText("保存分组")}
                    title={uiText("保存分组")}
                  >
                    <Save size={14} />
                  </button>
                  <button
                    type="button"
                    className="btn-icon"
                    aria-label={removed ? uiText("恢复到队列") : uiText("移到最近删除")}
                    title={removed ? uiText("恢复到队列") : uiText("移到最近删除")}
                    disabled={
                      !removed &&
                      !["completed", "failed", "cancelled"].includes(t.status)
                    }
                    onClick={() =>
                      run(async () => {
                        await api("/tasks/" + enc(t.id) + "/filing", "PATCH", {
                          removed: !removed,
                        });
                        onSelect("");
                        setHistory((prior) =>
                          prior.filter((x) => x.id !== t.id),
                        );
                        refreshListing((v) => v + 1);
                        await reload();
                      })
                    }
                  >
                    {removed ? <Undo2 size={15} /> : <Trash2 size={15} />}
                  </button>
                </form>
                {![
                  "completed",
                  "succeeded",
                  "done",
                  "failed",
                  "cancelled",
                  "canceled",
                  "denied",
                ].includes(t.status) && (
                  <button
                    className="action"
                    onClick={() =>
                      confirm
                        ? run(async () => {
                            await api(
                              "/tasks/" + enc(t.id) + "/cancel",
                              "POST",
                              {},
                            );
                            await reload();
                          })
                        : setConfirm(true)
                    }
                  >
                    <Square size={13} />
                    {confirm ? uiText("确认中止") : uiText("中止任务")}
                  </button>
                )}
                <CopyButton
                  key={t.id}
                  className="btn-icon copy-icon"
                  label={uiText("复制摘要")}
                  text={
                    display(t.result) ||
                    events.map((e) => display(e.data)).join("\n")
                  }
                />
              </div>
              <TaskConversation
                key={t.id}
                task={t}
                events={events}
                error={error}
                linked={handoff?.status === "ready" || t.origin === "desktop"}
              />
              {handoff && (
                <details className="handoff-info">
                  <summary>{uiText("桌面交接详情")}</summary>

                  <div>
                    <strong>
                      {handoff.status === "ready"
                        ? uiText("已加入 DSH App 对话列表")
                        : uiText("桌面交接未完成")}
                    </strong>
                    {handoff.status === "ready" ? (
                      <>
                        <span>{uiText("在 {0} 的 DSH App 中打开「{1}」。",
                          nodes.find((n) => n.id === t.node_id)?.alias || t.node_id,
                          handoff.title || t.prompt)}</span>
                        {typeof handoff.workspace === "string" && (
                          <code>{handoff.workspace}</code>
                        )}
                      </>
                    ) : (
                      <span>
                        {uiText(handoffReasons[handoff.reason] || "") ||
                          uiText("请在目标电脑检查 DSH App 和桌面插件。任务记录可在此查看。")}
                      </span>
                    )}
                  </div>
                </details>
              )}
              <Artifacts key={t.id} events={events} nodes={nodes} task={t} />
            </article>
          )}
        </div>
      </div>
    </>
  );
}

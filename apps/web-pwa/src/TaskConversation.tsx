import { uiText } from "./i18n";
import { SafeMarkdown } from "./FilePreview";
import { cleanMessage } from "./cleanMessage";
import { useEffect, useRef, useState } from "react";
import {
  api,
  display,
  enc,
  errorText,
  type Task,
  type TaskEvent,
} from "./model";
import "./task-conversation.css";

type Message = { who: "你" | "DSH"; text: string };

function transcript(task: Task, events: TaskEvent[]) {
  const messages: Message[] =
    task.origin === "desktop" ? [] : [{ who: "你", text: task.prompt }];
  const queued = new Set<string>();
  for (const event of events) {
    const text = event.data.text;
    if (
      event.type === "chat_delivery" &&
      event.data.state === "queued" &&
      text
    ) {
      queued.add(event.data.request_id);
      messages.push({ who: "你", text });
      continue;
    }
    if (event.type === "user_message" && queued.has(event.data.request_id))
      continue;
    if (!text || !["user_message", "message", "result"].includes(event.type))
      continue;
    const who = event.type === "user_message" ? "你" : "DSH";
    const last = messages[messages.length - 1];
    if (event.type === "result" && last?.who === who && last.text === text)
      continue;
    messages.push({ who, text });
  }
  if (!messages.some((m) => m.who === "DSH") && task.result)
    messages.push({ who: "DSH", text: display(task.result) });
  return messages;
}

function line(event: TaskEvent) {
  const d = event.data;
  if (event.type === "status")
    return [d.status, d.message].filter(Boolean).join(" · ");
  if (event.type === "chat_delivery") return d.state;
  return d.text || d.command || d.name || display(d);
}

const delivery: Record<string, string> = {
  queued: "已发送到 Hub，等待桌面接收…",
  accepted: "桌面已接收",
  rejected: "桌面未接收此消息，请检查原会话是否仍在共享。",
  expired: "等待桌面超时，请重新发送。",
};

export function TaskConversation({
  task,
  events,
  error,
  linked,
}: {
  task: Task;
  events: TaskEvent[];
  error: string;
  linked: boolean;
}) {
  const [terminal, setTerminal] = useState(false);
  const [follow, setFollow] = useState(true);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [notice, setNotice] = useState("");
  const [fault, setFault] = useState("");
  const request = useRef({ text: "", id: "" });
  const scroll = useRef<HTMLDivElement>(null);
  const busy = useRef(false);
  const messages = transcript(task, events)
    .map((m) => ({ ...m, text: cleanMessage(m.text) }))
    .filter((m) => m.text);
  const lastDelivery = [...events]
    .reverse()
    .find((e) => e.type === "chat_delivery");
  useEffect(() => {
    if (follow && scroll.current)
      scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [events.length, follow, terminal]);
  useEffect(() => {
    if (lastDelivery) setNotice("");
  }, [lastDelivery?.id]);
  const canSend =
    task.chat_online &&
    !sending &&
    !!draft.trim() &&
    ["completed", "failed", "cancelled"].includes(task.status);
  async function send() {
    const text = draft.trim();
    if (!canSend || !text || busy.current) return;
    if (request.current.text !== text) {
      const bytes = crypto.getRandomValues(new Uint8Array(16));
      request.current = {
        text,
        id: Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join(""),
      };
    }
    setFault("");
    setSending(true);
    busy.current = true;
    try {
      const response = await api<{ state: string }>(
        "/tasks/" + enc(task.id) + "/replies",
        "POST",
        { request_id: request.current.id, text },
      );
      setNotice(delivery[response.state]);
      setDraft("");
      request.current = { text: "", id: "" };
      setFollow(true);
    } catch (e) {
      setFault(errorText(e));
    } finally {
      busy.current = false;
      setSending(false);
    }
  }
  return (
    <section className="conversation">
      <div className="row wrap">
        <button
          className="action"
          aria-pressed={!terminal}
          onClick={() => setTerminal(false)}
        >{uiText("对话")}</button>
        <button
          className="action"
          aria-pressed={terminal}
          onClick={() => setTerminal(true)}
        >{uiText("终端")}</button>
        {terminal && (
          <button
            className="action follow-pill"
            role="switch"
            aria-checked={follow}
            onClick={() => setFollow(!follow)}
          >
            <i />{uiText("跟随输出")}</button>
        )}
      </div>
      <div
        className={
          terminal ? "conversation-scroll console-lines" : "conversation-scroll"
        }
        ref={scroll}
        onScroll={(e) => {
          const el = e.currentTarget;
          setFollow(el.scrollHeight - el.clientHeight - el.scrollTop < 40);
        }}
      >
        {terminal && <pre className="console-line">{task.prompt}</pre>}
        {terminal
          ? events.map((e) => (
              <div className="console-line" key={e.id}>
                <b>{e.type}</b>
                <span>{line(e)}</span>
              </div>
            ))
          : messages.map((m, i) => (
              <div className="chat-bubble" data-speaker={m.who} key={i}>
                <small>{m.who === "DSH" ? "DSH" : uiText("你")}</small>
                <SafeMarkdown text={m.text} />
              </div>
            ))}
        {terminal && !events.length && <small>{uiText("等待节点输出…")}</small>}
      </div>
      {error && <small role="alert">{error}</small>}
      {!terminal && linked && !task.removed && (
        <form
          className="docked-composer"
          onSubmit={(e) => {
            e.preventDefault();
            void send();
          }}
        >
          {task.chat_online ? (
            <>
              <label>
                <textarea
                  aria-label={uiText("继续此 DSH 会话")}
                  onKeyDown={(e) => {
                    if (
                      e.key === "Enter" &&
                      !e.shiftKey &&
                      !e.nativeEvent.isComposing &&
                      e.nativeEvent.keyCode !== 229
                    ) {
                      e.preventDefault();
                      void send();
                    }
                  }}
                  value={draft}
                  maxLength={16000}
                  rows={2}
                  onChange={(e) => setDraft(e.target.value)}
                  placeholder={uiText("给这台机器上的 DSH 发消息…")}
                />
              </label>
              <div className="composer-foot">
                <small>
                  <span className="badge idle">{uiText("已加入 DSH App")}</span>{" " + uiText("Shift+Enter 换行")}</small>
                <button
                  className="primary"
                  aria-label={uiText("发送消息")}
                  disabled={!canSend}
                >{uiText("发送")}</button>
              </div>
            </>
          ) : (
            <small>{uiText("桌面续聊未连接。请打开这台机器的 DSH Desktop，并使用支持续聊的 Fleet 插件。")}</small>
          )}
          <small role="status">
            {fault || uiText(notice || delivery[lastDelivery?.data.state] || "")}
          </small>
        </form>
      )}
    </section>
  );
}

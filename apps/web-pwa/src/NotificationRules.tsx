import { uiText } from "./i18n";
import { useEffect, useState } from "react";
import { api } from "./model";
import { Section } from "./shared";

type Rules = {disk: boolean; gpu: boolean; failed: boolean; offline: boolean; threshold: number; cooldown_hours: number};
const flags = { disk: "服务器磁盘告警", gpu: "GPU 空闲蹲守", failed: "任务失败", offline: "节点离线" } as const;

export function NotificationRules() {
  const [rules, setRules] = useState<Rules | null>(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  useEffect(() => {
    let active = true;
    api<Rules>("/notification-rules").then(r => { if (active) setRules(r); })
      .catch(() => { if (active) setStatus(uiText("读取通知策略失败")); });
    return () => { active = false; };
  }, []);
  async function save() {
    setBusy(true);
    try {
      setRules(await api<Rules>("/notification-rules", "PUT", rules));
      setStatus(uiText("已保存"));
    } catch { setStatus(uiText("保存失败，请重试")); }
    finally { setBusy(false); }
  }
  return <><Section title={uiText("通知策略")}/><div className="panel config">
    <small>{uiText("待审批 · 始终推送至本人私聊")}</small>
    {rules && <fieldset className="notice-flags" disabled={busy}>
      {Object.entries(flags).map(([k, title]) => {
        const key = k as keyof typeof flags;
        return <button key={key} type="button" className="action subtle" aria-pressed={rules[key]}
          onClick={() => {setRules({...rules, [key]: !rules[key]}); setStatus("");}}>
          <i className={"dot " + (rules[key] ? "" : "off")} />{uiText(title)}</button>;
      })}
      <label>{uiText("磁盘阈值（%）")}<input aria-label={uiText("磁盘阈值")} type="number" min="1" max="100" value={rules.threshold}
        onChange={e => setRules({...rules, threshold: Number(e.target.value)})}/></label>
      <button className="action" onClick={save} disabled={rules.threshold < 1 || rules.threshold > 100}>{uiText("保存通知策略")}</button>
    </fieldset>}
    {status && <small role="status">{status}</small>}
  </div></>;
}

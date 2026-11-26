import { uiText } from "./i18n";
import { AppearancePanel } from "./AppearancePanel";
import type { Service, Macro } from "./model";
import { Choice } from "./Choice";
import { useEffect, useState } from "react";
import { LogOut, ShieldAlert, ShieldCheck } from "lucide-react";
import {
  api,
  enc,
  type NodeInfo,
  type Settings as Prefs,
  type Approval,
  type Run,
} from "./model";
import { Label, Modal, Section } from "./shared";
import { NodeSettings } from "./NodeSettings";
import { translations } from "./i18n";
const choices = [
  ["approve_once", "允许一次"],
  ["approve_session_dir", "本会话信任目录"],
  ["deny", "拒绝"],
] as const;
export function Settings({
  nodes,
  approvals,
  services,
  macros,
  settings,
  run,
  reload,
  logout,
}: {
  nodes: NodeInfo[];
  approvals: Approval[];
  services: Service[];
  macros: Macro[];
  settings: Prefs;
  run: Run;
  reload: () => Promise<void>;
  logout: () => void;
}) {
  const [pending, setPending] = useState<{
    a: Approval;
    decision: string;
    label: string;
  } | null>(null);
  const [tab, setTab] = useState<"security" | "skin" | "layout">("security");
  const [prefs, setPrefs] = useState(settings);
  useEffect(
    () => setPrefs(settings),
    [
      settings.theme,
      settings.accent,
      settings.language,
      settings.default_route,
    ],
  );
  function update(key: keyof Prefs, value: string) {
    const next = { ...prefs, [key]: value };
    setPrefs(next);
    run(async () => {
      await api("/settings", "PATCH", { [key]: value });
      await reload();
    });
  }
  return (
    <>
      <section>
        {approvals.length > 0 && (
          <Section title={uiText("待审批请求")} count={approvals.length} />
        )}
        {approvals.length ? (
          approvals.map((a) => (
            <article className="panel approval" key={a.id}>
              <div className="row">
                <ShieldAlert size={21} />
                <h3>
                  {nodes.find((n) => n.id === a.node_id)?.alias || a.node_id}
                </h3>
              </div>
              <p>{a.reason}</p>
              <small>{uiText("工作目录")}</small>
              <code className="code private-workspace">{a.workspace}</code>
              <small>{uiText("请求命令")}</small>
              <pre>{a.command}</pre>
              <div className="decision">
                {choices
                  .filter(([k]) => !a.decisions || a.decisions.includes(k))
                  .map(([k, s]) => (
                    <button
                      key={k}
                      onClick={() => setPending({ a, decision: k, label: uiText(s) })}
                      className={
                        k === "approve_once" ? "action subtle" : "action"
                      }
                    >
                      {uiText(s)}
                    </button>
                  ))}
              </div>
            </article>
          ))
        ) : (
          <div className="panel ok">
            <ShieldCheck size={22} />{uiText("当前没有待审批请求")}</div>
        )}
      </section>
      <div className="settings-tabs seg-tabs" aria-label={uiText("设置分类")}>
        {(["security", "skin", "layout"] as const).map((k, i) => (
          <button key={k} aria-pressed={tab === k} onClick={() => setTab(k)}>
            {[uiText("安全与审批"), uiText("外观与皮肤"), uiText("布局与快捷入口")][i]}
          </button>
        ))}
      </div>
      <div hidden={tab !== "security"}>
        <NodeSettings
          nodes={nodes}
          account={
            <>
              <Section title={uiText("账号与会话")} />
              <div className="panel config account-card">
                <Label name={uiText("默认路由")}>
                  <Choice
                    aria-label={uiText("默认路由")}
                    value={prefs.default_route}
                    onChange={(e) => update("default_route", e.target.value)}
                  >
                    <option value="single">{uiText("指定单机")}</option>
                    <option value="auto">{uiText("Auto · 自动选择")}</option>
                    <option value="fanout">{uiText("Fanout · 多机并行")}</option>
                  </Choice>
                </Label>
                <Label name={uiText("语言")}>
                  <Choice
                    aria-label={uiText("语言")}
                    value={prefs.language}
                    onChange={(e) => update("language", e.target.value)}
                  >
                    {Object.entries(translations).map(([locale, strings]) => (
                      <option value={locale} key={locale}>
                        {strings.languageName}
                      </option>
                    ))}
                  </Choice>
                </Label>

                <button className="action logout" onClick={logout}>
                  <LogOut size={16} />{uiText("退出登录")}</button>
              </div>
            </>
          }
        />
      </div>
      <div hidden={tab === "security"}>
        <AppearancePanel
          tab={tab === "layout" ? "layout" : "skin"}
          nodes={nodes}
          services={services}
          macros={macros}
        />
      </div>
      {pending && approvals.some((a) => a.id === pending.a.id) && (
        <Modal title={uiText("确认审批决定")} close={() => setPending(null)}>
          <div className="dialog-body">
            <h3>{pending.label}</h3>
            <p>{pending.a.reason}</p>
            <pre>{pending.a.command}</pre>
            <button
              className="primary"
              onClick={() =>
                run(async () => {
                  await api("/approvals/" + enc(pending.a.id), "POST", {
                    decision: pending.decision,
                  });
                  setPending(null);
                  await reload();
                })
              }
            >{uiText("确认")}{pending.label}
            </button>
          </div>
        </Modal>
      )}
    </>
  );
}

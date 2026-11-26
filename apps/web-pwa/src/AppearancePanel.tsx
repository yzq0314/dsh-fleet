import { displayMacro } from "./builtinMacros";
import { uiText } from "./i18n";
import { useState, type CSSProperties } from "react";
import { Upload, Download, RotateCcw, ImagePlus, Check } from "lucide-react";
import { Choice } from "./Choice";
import { useAppearance } from "./Appearance";
import {
  accents,
  blocks,
  blockNames,
  defaults,
  exportTheme,
  foreground,
  palette,
  importTheme,
  skins,
  type Theme,
} from "./theme";
import type { NodeInfo, Service, Macro } from "./model";

export function AppearancePanel({
  tab,
  nodes,
  services,
  macros,
}: {
  tab: "skin" | "layout";
  nodes: NodeInfo[];
  services: Service[];
  macros: Macro[];
}) {
  const { theme, save, refreshWallpaper, wallVersion } = useAppearance();
  const [draft, setDraft] = useState(theme),
    [msg, status] = useState(""),
    [busy, setBusy] = useState(false);
  const [screen, screenTo] = useState<"desktop" | "mobile">("mobile"),
    [host, setHost] = useState(nodes[0]?.id || "");
  const node = nodes.find((n) => n.id === host);
  const order = draft.layout[screen];
  function change<K extends keyof Theme>(key: K, value: Theme[K]) {
    setDraft((p) => ({ ...p, [key]: value }));
    status("");
  }
  async function commit(v: Theme) {
    try {
      setBusy(true);
      await save(importTheme(exportTheme(v)));
      setDraft(v);
      status(uiText("已同步到 Hub"));
    } catch (e) {
      status(String(e));
    } finally {
      setBusy(false);
    }
  }
  function choices(
    label: string,
    value: string,
    options: Record<string, string>,
    apply: (s: string) => void,
  ) {
    return (
      <div className="field">
        <span>{label}</span>
        <Choice
          aria-label={label}
          value={value}
          onChange={(e) => apply(e.target.value)}
        >
          {Object.entries(options).map(([k, text]) => (
            <option key={k} value={k}>
              {text}
            </option>
          ))}
        </Choice>
      </div>
    );
  }
  const pins = draft.shortcuts[host] ?? ["workbench"];
  const targets: Record<string, string> = {
    workbench: uiText("DSH 工作台"),
    ...Object.fromEntries(services.map((s) => ["service:" + s.id, s.name])),
    ...Object.fromEntries(
      (node?.health?.volumes || []).map((v) => [
        "volume:" + v.name,
        (uiText("卷 ·") + " ") + v.name,
      ]),
    ),
    ...Object.fromEntries(
      macros.map((m) => ["macro:" + m.id, (uiText("派单 ·") + " ") + displayMacro(m).name]),
    ),
  };
  let colors: Record<string, string> = {};
  try {
    colors = palette(draft.css);
  } catch {
    /* Keep the preview usable while a declaration is being typed. */
  }
  const preview: CSSProperties = {
    ...colors,
    borderRadius: draft.radius,
    fontFamily: draft.font === "mono" ? "monospace" : "inherit",
    padding: draft.density === "compact" ? 8 : 16,
  };
  if (draft.theme === "glass") {
    Object.assign(preview, {
      "--preview-blur": draft.blur + "px",
      backgroundImage:
        "radial-gradient(ellipse,transparent,rgba(0,0,0," +
        draft.shade / 100 +
        "))," +
        (draft.wallpaper
          ? 'url("/api/ui-preferences/wallpaper?v=' + wallVersion + '")'
          : "linear-gradient(125deg,#406f84,#182338)"),
    });
  }
  const saving = (
    <button
      className="primary btn-md"
      disabled={busy}
      onClick={() => commit(draft)}
    >
      {tab === "skin" ? uiText("保存外观") : uiText("保存布局")}
    </button>
  );
  return (
    <section className="appearance-editor">
      {tab === "skin" ? (
        <div className="settings-grid">
          <div className="panel config">
            <h3>{uiText("主题风格")}</h3>
            <div className="skin-options" role="group" aria-label={uiText("主题风格")}>
              {Object.entries(skins).map(([key, label]) => (
                <button
                  key={key}
                  data-skin={key}
                  aria-pressed={draft.theme === key}
                  onClick={() => change("theme", key as Theme["theme"])}
                >
                  <span className="mini-window" aria-hidden="true">
                    <i />
                    <b />
                    <em />
                  </span>
                  <span>
                    {uiText(label)}
                    {draft.theme === key && <Check size={13} />}
                  </span>
                </button>
              ))}
            </div>
            <h3>{uiText("主强调色")}</h3>
            <div className="accent-row">
              <div className="color-swatches">
                {Object.entries(accents).map(([name, color]) => (
                  <button
                    key={name}
                    aria-label={uiText(name)}
                    title={uiText(name)}
                    aria-pressed={draft.accent === color}
                    style={{ background: color, color: foreground(color) }}
                    onClick={() => change("accent", color)}
                  >
                    {draft.accent === color && <Check size={13} />}
                  </button>
                ))}
              </div>
              <label className="hex-field">
                Hex
                <input
                  aria-label={uiText("自定义 Hex")}
                  value={draft.accent}
                  maxLength={7}
                  onChange={(e) => change("accent", e.target.value)}
                />
              </label>
            </div>
            <div className="fine-controls">
              {choices(
                uiText("圆角"),
                String(draft.radius),
                { "4": uiText("硬朗"), "10": uiText("默认"), "16": uiText("柔和") },
                (s) => change("radius", Number(s) as Theme["radius"]),
              )}
              {choices(
                uiText("信息密度"),
                draft.density,
                { compact: uiText("紧凑"), comfortable: uiText("舒适") },
                (s) => change("density", s as Theme["density"]),
              )}
              {choices(
                uiText("字体"),
                draft.font,
                { system: uiText("系统无衬线"), mono: uiText("等宽") },
                (s) => change("font", s as Theme["font"]),
              )}
              <div className="field"><span>{uiText("动效")}</span><div className="row wrap">
                {(["motion", "glow"] as const).map((k, i) => (
                  <button
                    className="action"
                    key={k}
                    role="switch"
                    aria-checked={draft[k]}
                    onClick={() => change(k, !draft[k])}
                  >
                    {[uiText("界面动效"), uiText("拓扑流光")][i]} · {draft[k] ? uiText("开") : uiText("关")}
                  </button>
                ))}
              </div></div>
            </div>
          </div>
          <div className="skin-right">
          <aside className="panel config preview-pane">
            <h3>{uiText("实时预览")}</h3>
            <div
              className="skin-preview"
              data-preview={draft.theme}
              style={preview}
            >
              <div className="preview-device"><strong>{uiText("设备工作台")}</strong><small>{uiText("● 在线")}</small></div>
              <div className="mini-meter">
                <i style={{ background: draft.accent }} />
              </div>
              <div className="preview-chat">
                <p>{uiText("检查当前工作区")}</p><p>{uiText("工作区已就绪。")}</p>
              </div>
              <span
                style={{
                  background: draft.accent,
                  color: foreground(draft.accent),
                  borderRadius: draft.radius,
                }}
              >{uiText("向本机派单")}</span>
            </div>
            <div className="row wrap">
              {saving}
              <button
                className="action"
                disabled={busy}
                onClick={() => commit(defaults)}
              >
                <RotateCcw size={14} />{uiText("恢复默认")}</button>
              <button
                className="action"
                onClick={() => {
                  try {
                    const data = exportTheme(importTheme(exportTheme(draft)));
                    const url = URL.createObjectURL(
                      new Blob([data], { type: "application/json" }),
                    );
                    const a = document.createElement("a");
                    a.href = url;
                    a.download = "fleet-theme.json";
                    a.click();
                    setTimeout(() => URL.revokeObjectURL(url), 500);
                  } catch (e) {
                    status(String(e));
                  }
                }}
              >
                <Download size={14} />{uiText("导出")}</button>
              <label className="action file-input">
                <Upload size={14} />{uiText("导入")}<input
                  aria-label={uiText("导入主题包")}
                  type="file"
                  accept=".json"
                  onChange={async (e) => {
                    const f = e.target.files?.[0];
                    if (!f) return;
                    try {
                      if (f.size > 131072) throw Error(uiText("主题包过大"));
                      setDraft(importTheme(await f.text()));
                      status(uiText("已导入预览，保存后同步"));
                    } catch (err) {
                      status(String(err));
                    }
                  }}
                />
              </label>
            </div>
          </aside>
            <section className="panel config skin-background">
              <h3>{uiText("背景与高级定制")}</h3>
              <div className="row wrap">
                <label className="action file-input">
                  <ImagePlus size={15} />{uiText("上传壁纸")}<input
                    aria-label={uiText("上传壁纸")}
                    type="file"
                    accept="image/png,image/jpeg,image/webp"
                    onChange={async (e) => {
                      const f = e.target.files?.[0];
                      if (!f) return;
                      try {
                        if (f.size > 2097152) throw Error(uiText("壁纸超过 2 MiB"));
                        const r = await fetch("/api/ui-preferences/wallpaper", {
                          method: "PUT",
                          headers: { "Content-Type": f.type },
                          body: f,
                        });
                        if (!r.ok)
                          throw Error(uiText("壁纸上传失败 (") + r.status + ")");
                        refreshWallpaper();
                        setDraft(current => ({...current, theme:"glass", wallpaper:true}));
                        status(uiText("壁纸已载入预览，保存后同步"));
                      } catch (err) {
                        status(String(err));
                      }
                    }}
                  />
                </label>
                <button className="action" disabled={!draft.wallpaper} onClick={() => change("wallpaper", false)}>{uiText("移除壁纸")}</button>
              </div>
              <div className="glass-sliders">
                <label>{uiText("模糊") + " "}{draft.blur}px
                  <input
                    type="range" disabled={draft.theme !== "glass"}
                    min="0"
                    max="32"
                    value={draft.blur}
                    onChange={(e) => change("blur", +e.target.value)}
                  />
                </label>
                <label>{uiText("暗角") + " "}{draft.shade}%
                  <input
                    type="range" disabled={draft.theme !== "glass"}
                    min="0"
                    max="90"
                    value={draft.shade}
                    onChange={(e) => change("shade", +e.target.value)}
                  />
                </label>
              </div>
            <details><summary>{uiText("受控 CSS 配色")}</summary>
              <textarea aria-label={uiText("受控 CSS 配色")}
                value={draft.css}
                maxLength={512}
                placeholder="--panel: #202828;"
                onChange={(e) => change("css", e.target.value)}
              />
            </details>
            </section>
          </div>
        </div>
      ) : (
        <div className="settings-grid">
          <div className="panel config">
            {" "}
            <h3>{uiText("首页区块")}</h3>
            {choices(
              uiText("布局设备"),
              screen,
              { mobile: uiText("手机"), desktop: uiText("桌面") },
              (s) => screenTo(s as typeof screen),
            )}
            {[...order, ...blocks.filter((b) => !order.includes(b))].map(
              (b, i) => (
                <div className="layout-row" key={b}>
                  <span>{uiText(blockNames[b])}</span>
                  <button
                    className="action"
                    aria-pressed={order.includes(b)}
                    onClick={() =>
                      change("layout", {
                        ...draft.layout,
                        [screen]: order.includes(b)
                          ? order.filter((k) => k !== b)
                          : [...order, b],
                      })
                    }
                  >
                    {order.includes(b) ? uiText("已显示") : uiText("已隐藏")}
                  </button>
                  <button
                    className="action"
                    aria-label={uiText("上移") + uiText(blockNames[b])}
                    disabled={i === 0 || i >= order.length}
                    onClick={() => {
                      const a = [...order];
                      [a[i - 1], a[i]] = [a[i], a[i - 1]];
                      change("layout", { ...draft.layout, [screen]: a });
                    }}
                  >
                    ↑
                  </button>
                </div>
              ),
            )}
          </div>
          <div className="panel config">
            <h3>{uiText("设备卡快捷入口")}</h3>
            {choices(
              uiText("设备卡"),
              host,
              Object.fromEntries(nodes.map((n) => [n.id, n.alias])),
              setHost,
            )}
            {node &&
              [0, 1].map((i) => (
                <div key={i}>
                  {choices(
                    (uiText("入口") + " ") + (i + 1),
                    pins[i] || "",
                    i ? { "": uiText("无"), ...targets } : targets,
                    (v) => {
                      const a = [...pins];
                      if (v) a[i] = v;
                      else a.splice(i, 1);
                      change("shortcuts", {
                        ...draft.shortcuts,
                        [host]: [...new Set(a)],
                      });
                    },
                  )}
                </div>
              ))}
            {saving}
          </div>
        </div>
      )}
      {msg && <small role="status">{msg}</small>}
    </section>
  );
}

import { currentLocale, uiText } from "./i18n";
import "./round4b.css";
import { useState } from "react";
import { Bell, BellRing, Server } from "lucide-react";
import { api, pct, type HardwareMonitor } from "./model";

function level(n: number | null) {
  return n === null
    ? "unknown"
    : n >= 95
      ? "critical"
      : n >= 80
        ? "warning"
        : "normal";
}
function percent(n: number | null) {
  return n === null ? "—" : Number(n.toFixed(1)) + "%";
}
export function Meter({
  name,
  value,
  reading,
  detail,
}: {
  name: string;
  value: unknown;
  reading?: string;
  detail?: string;
}) {
  const n = pct(value);
  return (
    <div className="meter" data-level={level(n)} title={detail}>
      <div>
        <span>{name}</span>
        <b>{reading ?? percent(n)}</b>
      </div>
      <i>
        <span style={{ width: (n ?? 0) + "%" }} />
      </i>
    </div>
  );
}
export function MonitorCard({ monitor: m, reload }: { monitor: HardwareMonitor; reload: () => Promise<void> }) {
  const [idleOnly, setIdleOnly] = useState(false);
  const [pending, setPending] = useState<number | null>(null);
  const [error, setError] = useState("");
  async function watch(index: number, enabled: boolean) {
    setPending(index); setError("");
    try {
      const query = new URLSearchParams({monitor: m.name, enabled: String(enabled)});
      await api("/gpu-watch/" + index + "?" + query, "PUT");
      await reload();
    } catch (e) { setError(e instanceof Error ? e.message : uiText("蹲守状态更新失败")); }
    finally { setPending(null); }
  }
  const cards = m.gpu.map((g, index) => ({
    ...g,
    index,
    idle: g.used_mib < 1024 && g.utilization < 10,
  }));
  const idle = cards.filter((g) => g.idle).length;
  const shown = cards.filter((g) => !idleOnly || cards.length < 4 || g.idle);
  return (
    <article className="panel hardware-card">
      <header className="telemetry-heading">
        <span className="device-icon">
          <Server size={19} />
        </span>
        <div className="grow">
          <h3>{m.name}</h3>
          <small>
            <i className={"dot " + (m.fresh ? "pulse" : "off")} />
            {m.fresh ? uiText("采集中") : uiText("采样过期")} ·{" "}
            <time>{new Date(m.timestamp * 1000).toLocaleTimeString(currentLocale())}</time>
          </small>
        </div>
        <span className="badge idle">{idle}{" " + uiText("卡空闲")}</span>
        {cards.length >= 4 && (
          <div className="seg-tabs">
            {[false, true].map((v) => (
              <button
                key={String(v)}
                aria-pressed={idleOnly === v}
                onClick={() => setIdleOnly(v)}
              >
                {v ? (uiText("空闲") + " ") + idle : (uiText("全部") + " ") + cards.length}
              </button>
            ))}
          </div>
        )}
      </header>
      <div className="host-meters">
        {[
          ["CPU", m.cpu, uiText("处理器利用率")],
          [uiText("内存"), m.ram, uiText("物理内存占用")],
          [uiText("根分区"), m.disk, (uiText("可用") + " ") + m.free_gib + " GiB"],
        ].map(([name, num, desc]) => (
          <section
            className="system-metric"
            key={String(name)}
            data-level={level(pct(num))}
          >
            <Meter name={String(name)} value={num} />
            <small>
              {desc}
              {(pct(num) ?? 0) >= 95 && (
                <em className="badge critical">
                  {name === uiText("根分区") ? uiText("空间不足") : uiText("高占用")}
                </em>
              )}
            </small>
          </section>
        ))}
      </div>
      <div className="gpu-grid">
        {shown.map((g) => {
          const used = g.total_mib ? (100 * g.used_mib) / g.total_mib : null;
          const full = g.utilization >= 95 || (used ?? 0) >= 95;
          return (
            <section key={g.index} className="gpu">
              <header>
                <b>GPU {g.index}</b>
                <span
                  className={
                    "badge " + (full ? "critical" : g.idle ? "idle" : "")
                  }
                >
                  {full ? uiText("满载") : g.idle ? uiText("空闲") : uiText("运行")}
                </span>
              </header>
              {(!g.idle || m.watching?.includes(g.index)) && <button className="gpu-watch icon"
                aria-label={(m.watching?.includes(g.index) ? (uiText("取消蹲守 GPU") + " ") : (uiText("蹲守空闲 GPU") + " ")) + g.index}
                aria-pressed={!!m.watching?.includes(g.index)} disabled={!m.fresh || pending !== null}
                onClick={() => watch(g.index, !m.watching?.includes(g.index))}>
                {m.watching?.includes(g.index) ? <BellRing size={14}/> : <Bell size={14}/>}
              </button>}
              <Meter name={uiText("算力")} value={g.utilization} />
              <Meter
                name={uiText("显存")}
                value={used}
                reading={
                  (g.used_mib / 1024).toFixed(1) +
                  " / " +
                  (g.total_mib / 1024).toFixed(1) +
                  " GiB"
                }
                detail={g.used_mib + " / " + g.total_mib + " MiB"}
              />
            </section>
          );
        })}
      </div>
      {error && <small role="alert">{error}</small>}
      {!shown.length && idleOnly && <small>{uiText("当前没有空闲卡")}</small>}
    </article>
  );
}
